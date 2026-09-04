"""Candidate detail/attachment closure.

Discovery candidates are allowed to exist before a Project identity is
resolved.  This module therefore keeps their detail snapshots, attachments,
document parses and Evidence under ``candidate_id`` instead of manufacturing a
project merely to hold a PDF.
"""

from __future__ import annotations

import json
import shutil
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from pathlib import PurePosixPath

from bs4 import BeautifulSoup
from sqlalchemy import select

from tender_ai.candidates import _load_json, completeness_score, missing_fields, next_action_for
from tender_ai.config_loader import load_region_catalog
from tender_ai.crawlers.http import HttpClient, HttpFetchError
from tender_ai.documents.download import DOWNLOAD_ROOT, DownloadedAttachment, download_attachment
from tender_ai.documents.parser import parse_path
from tender_ai.extractors.runner import _upsert_document_parse
from tender_ai.extractors.tender import normalize_detail
from tender_ai.sources.adapters import _extract_attachments
from tender_ai.sources.contracts import DetailPayload
from tender_ai.sources.registry import SourceDefinition, SourceRegistry
from tender_ai.snapshots.store import SnapshotStore
from tender_ai.status.engine import recalculate_status, with_manual_evidence
from tender_ai.status.time import now_shanghai
from tender_ai.storage.database import create_engine_for, initialize_database, session_scope
from tender_ai.storage.models import (
    Candidate,
    CandidateAttachment,
    CandidateEnrichmentResult,
    CandidateFact,
    DocumentParse,
    Evidence,
    ManualOverride,
    Project,
)
from tender_ai.storage.repository import project_to_record, save_evidence
from tender_ai.urls import canonicalize_url


SUPPORTED_ATTACHMENT_SUFFIXES = {".pdf", ".docx", ".xlsx", ".xlsm", ".xls", ".zip"}
PARSEABLE_ATTACHMENT_SUFFIXES = SUPPORTED_ATTACHMENT_SUFFIXES - {".zip"}
ZIP_MAX_FILES = 50
ZIP_MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024


@dataclass
class CandidateDocumentSummary:
    candidate_id: str
    result_id: int | None = None
    detail_snapshot_id: str | None = None
    attachment_count: int = 0
    downloaded_count: int = 0
    parsed_count: int = 0
    evidence_count: int = 0
    fact_count: int = 0
    status: str = "SKIPPED"
    blocker: str | None = None
    error: str | None = None
    attachment_ids: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "result_id": self.result_id,
            "detail_snapshot_id": self.detail_snapshot_id,
            "attachment_count": self.attachment_count,
            "downloaded_count": self.downloaded_count,
            "parsed_count": self.parsed_count,
            "evidence_count": self.evidence_count,
            "fact_count": self.fact_count,
            "status": self.status,
            "blocker": self.blocker,
            "error": self.error,
            "attachment_ids": self.attachment_ids,
        }


def _suffix(url: str, file_name: str | None = None, mime_type: str | None = None) -> str:
    suffix = Path((file_name or url).split("?", 1)[0]).suffix.lower()
    if suffix:
        return suffix
    media = (mime_type or "").split(";", 1)[0].lower()
    return {
        "application/pdf": ".pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "application/zip": ".zip",
    }.get(media, "")


def _source_definition(candidate: Candidate, result: CandidateEnrichmentResult, registry: SourceRegistry | None) -> SourceDefinition:
    if registry is not None and candidate.source_id:
        try:
            return registry.get(candidate.source_id)
        except KeyError:
            pass
    return SourceDefinition(
        source_id=candidate.source_id or "candidate_discovery",
        source_name="候选来源详情",
        category="discovered",
        base_url=f"{urlparse(result.source_url).scheme}://{urlparse(result.source_url).netloc}",
    )


def _safe_extract_zip(archive: Path, destination: Path) -> tuple[list[Path], str | None]:
    """Extract only safe, parseable ZIP members under a fixed directory."""

    root = (destination / f"{archive.stem}_unzipped").resolve()
    root.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    total_size = 0
    rejected: list[str] = []
    seen_targets: set[Path] = set()
    try:
        with zipfile.ZipFile(archive) as zipped:
            members = [item for item in zipped.infolist() if not item.is_dir()]
            if len(members) > ZIP_MAX_FILES:
                return [], f"ZIP 文件数超过安全上限 {ZIP_MAX_FILES}"
            for info in members:
                # ZIP paths are POSIX paths even on Windows.  Reject absolute,
                # parent-traversal and symlink entries before touching disk.
                name = str(info.filename or "").replace("\\", "/")
                pure = PurePosixPath(name)
                mode = (info.external_attr >> 16) & 0o170000
                invalid_path = (
                    not name
                    or not pure.parts
                    or pure.is_absolute()
                    or ":" in pure.parts[0]
                    or ".." in pure.parts
                    or mode == stat.S_IFLNK
                )
                if invalid_path:
                    rejected.append(name or "<empty>")
                    continue
                suffix = Path(pure.name).suffix.lower()
                if suffix not in PARSEABLE_ATTACHMENT_SUFFIXES:
                    continue
                total_size += int(info.file_size or 0)
                if total_size > ZIP_MAX_UNCOMPRESSED_BYTES:
                    return [], f"ZIP 解压后大小超过安全上限 {ZIP_MAX_UNCOMPRESSED_BYTES} bytes"
                target = (root / Path(*pure.parts)).resolve()
                if target != root and root not in target.parents:
                    rejected.append(name)
                    continue
                if target in seen_targets:
                    rejected.append(name)
                    continue
                seen_targets.add(target)
                target.parent.mkdir(parents=True, exist_ok=True)
                with zipped.open(info, "r") as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                extracted.append(target)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        return [], f"ZIP_SAFE_EXTRACT_ERROR: {exc}"
    if not extracted:
        return [], "ZIP 中没有可安全解析的 PDF/DOCX/XLSX 文件"
    return extracted, (f"已跳过不安全或不支持的 ZIP 成员: {', '.join(rejected[:5])}" if rejected else None)


def _persist_fact(session: Any, candidate: Candidate, field_name: str, value: Any, *, raw_value: Any, source_url: str, source_level: str | None, evidence_id: int | None) -> bool:
    if value in (None, ""):
        return False
    normalized = "".join(str(value).split()).casefold()
    if session.scalar(select(CandidateFact).where(CandidateFact.candidate_id == candidate.candidate_id, CandidateFact.field_name == field_name, CandidateFact.normalized_value == normalized)) is not None:
        return False
    session.add(CandidateFact(candidate_id=candidate.candidate_id, field_name=field_name, value=str(value), normalized_value=normalized, raw_value=str(raw_value if raw_value not in (None, "") else value), evidence_id=evidence_id, source_url=source_url, source_level=source_level, confidence=0.85 if evidence_id else 0.55, is_current=True, created_at=now_shanghai()))
    return True


def _merge_project_facts(session: Any, candidate: Candidate, record: Any) -> None:
    project = session.get(Project, candidate.project_id) if candidate.project_id else None
    if project is None:
        return
    manual_fields = set(session.scalars(select(ManualOverride.field_name).where(ManualOverride.project_id == project.project_id, ManualOverride.active.is_(True))).all())
    fields = (
        "project_name", "province", "city", "county", "location", "owner", "purchaser", "tenderer", "agency",
        "industry", "project_type", "project_scale", "capacity_mw", "capacity_mwh", "budget", "project_code",
        "tender_code", "qualification_summary", "participation_method", "qualification_deadline",
        "registration_deadline", "document_deadline", "bid_deadline", "open_time",
    )
    for field_name in fields:
        value = getattr(record, field_name, None)
        if value not in (None, "") and field_name not in manual_fields and getattr(project, field_name, None) in (None, ""):
            setattr(project, field_name, value)
    project.updated_at = now_shanghai()


def _apply_parsed_document(
    session: Any,
    candidate: Candidate,
    parsed: Any,
    *,
    source_url: str,
    source_file: str,
    snapshot: Any,
    content_hash_value: str | None = None,
    definition: SourceDefinition,
    summary: CandidateDocumentSummary,
) -> tuple[DocumentParse, bool]:
    """Persist one parsed document and merge only evidence-backed facts."""

    parse_row = _upsert_document_parse(
        session,
        announcement_id=None,
        attachment_id=None,
        candidate_id=candidate.candidate_id,
        document=parsed,
        content_hash_value=content_hash_value or parsed.content_hash,
        project_id=candidate.project_id,
        source_id=candidate.source_id,
    )
    has_text = bool(parsed.text)
    summary.parsed_count += int(has_text)
    if not has_text:
        candidate.blocker = "EXTRACTION_FAILED"
        return parse_row, False
    payload = DetailPayload(title=candidate.title, url=source_url, text=parsed.text, metadata={})
    extraction = normalize_detail(
        payload,
        definition,
        load_region_catalog(),
        document=parsed,
        source_file=source_file,
        parser=parsed.parser,
    )
    evidence_ids: list[int] = []
    evidence_by_field: dict[str, int] = {}
    for evidence in extraction.evidences:
        enriched = evidence.model_copy(
            update={
                "snapshot_id": snapshot.snapshot_id,
                "document_id": parse_row.document_id,
                "source_file": source_file,
            }
        )
        evidence_row = save_evidence(
            session,
            enriched,
            project_id=candidate.project_id,
            candidate_id=candidate.candidate_id,
        )
        evidence_ids.append(evidence_row.id)
        evidence_by_field.setdefault(evidence_row.field_name, evidence_row.id)
    summary.evidence_count += len(evidence_ids)
    record_values = extraction.record.model_dump(mode="python")
    values = _load_json(candidate.candidate_values_json, {})
    if not isinstance(values, dict):
        values = {}
    for field_name, value in record_values.items():
        if value in (None, "") or field_name in {"status", "status_reason", "status_evaluated_at"}:
            continue
        values.setdefault(field_name, value)
        if _persist_fact(
            session,
            candidate,
            field_name,
            value,
            raw_value=value,
            source_url=source_url,
            source_level=candidate.source_level,
            evidence_id=evidence_by_field.get(field_name),
        ):
            summary.fact_count += 1
    candidate.candidate_values_json = json.dumps(values, ensure_ascii=False, default=str)
    candidate.evidence_ids_json = json.dumps(
        list(dict.fromkeys([*(_load_json(candidate.evidence_ids_json, [])), *evidence_ids])),
        ensure_ascii=False,
    )
    _merge_project_facts(session, candidate, extraction.record)
    return parse_row, True


def process_candidate_enrichment_result(
    candidate_id: str,
    result_id: int,
    *,
    database: str | None = None,
    client: HttpClient | None = None,
    snapshot_store: SnapshotStore | None = None,
    registry: SourceRegistry | None = None,
    max_attachments: int = 10,
    download_destination: Path | None = None,
    dry_run: bool = False,
) -> CandidateDocumentSummary:
    """Fetch one enrichment result and close its detail/document/Evidence loop.

    The caller decides which results are worth deep processing.  This keeps
    ordinary discovery cheap while making the deep path deterministic and
    replayable.
    """

    summary = CandidateDocumentSummary(candidate_id=candidate_id, result_id=result_id)
    owns_client = client is None
    http = client or HttpClient()
    store = snapshot_store or SnapshotStore()
    try:
        with session_scope(initialize_database(create_engine_for(database))) as session:
            candidate = session.get(Candidate, candidate_id)
            result = session.get(CandidateEnrichmentResult, result_id)
            if candidate is None or result is None or result.candidate_id != candidate_id:
                summary.status = "FAILED"
                summary.error = "candidate or enrichment result not found"
                return summary
            if dry_run:
                summary.status = "PLANNED"
                return summary
            response = http.get(result.source_url, cache_namespace=f"candidate:{candidate_id}:detail", cache_expire=900.0)
            content_type = (response.headers.get("content-type") or "text/html").split(";", 1)[0].lower()
            detail_snapshot = store.save_bytes(
                session,
                source_url=result.source_url,
                content=response.content,
                content_type=content_type,
                source_id=candidate.source_id,
                candidate_id=candidate_id,
                metadata={"candidate_id": candidate_id, "enrichment_result_id": result_id, "provider": result.provider},
            )
            summary.detail_snapshot_id = detail_snapshot.snapshot_id
            detail_doc = parse_path(detail_snapshot.file_path, source_url=result.source_url, content_type=content_type, expected_terms=("招标", "采购", "截止", "文件"))
            detail_parse = _upsert_document_parse(
                session,
                announcement_id=None,
                attachment_id=None,
                candidate_id=candidate_id,
                document=detail_doc,
                content_hash_value=detail_snapshot.sha256,
                project_id=candidate.project_id,
                source_id=candidate.source_id,
            )
            # A result URL can be an HTML detail page even when the search
            # provider only returned a short snippet.  Save the detail parse
            # before looking at attachments so it is useful when a download is
            # blocked later.
            soup = BeautifulSoup(response.text, "lxml") if "html" in content_type or response.text.lstrip().startswith("<") else None
            links = _extract_attachments(response.text, result.source_url) if soup is not None else []
            recognized = [link for link in links if _suffix(link.url, link.file_name, link.mime_type) in SUPPORTED_ATTACHMENT_SUFFIXES]
            recognized = recognized[:max(0, max_attachments)]
            summary.attachment_count = len(recognized)
            if not recognized:
                candidate.blocker = "MISSING_ATTACHMENT"
                candidate.next_action = next_action_for(blocker=candidate.blocker, identity_status=candidate.identity_status, verification_status=candidate.verification_status, missing=_load_json(candidate.missing_fields_json, []))
                candidate.enrichment_stop_reason = "NO_MORE_SOURCES"
                summary.status = "NO_ATTACHMENT"
                session.flush()
                return summary
            definition = _source_definition(candidate, result, registry)
            for link in recognized:
                canonical = canonicalize_url(link.url) or link.url
                row = session.scalar(select(CandidateAttachment).where(CandidateAttachment.candidate_id == candidate_id, CandidateAttachment.canonical_url == canonical))
                if row is None:
                    row = CandidateAttachment(candidate_id=candidate_id, enrichment_result_id=result_id, source_url=link.url, canonical_url=canonical, file_name=link.file_name, mime_type=link.mime_type, download_status="DISCOVERED", parse_status="PENDING", discovered_at=now_shanghai(), metadata_json=json.dumps({"detail_snapshot_id": detail_snapshot.snapshot_id}, ensure_ascii=False))
                    session.add(row)
                    session.flush()
                summary.attachment_ids.append(row.id)
                try:
                    downloaded: DownloadedAttachment = download_attachment(http, link.url, suggested_name=link.file_name, mime_type=link.mime_type, destination=download_destination or DOWNLOAD_ROOT)
                    row.file_name = downloaded.file_name
                    row.local_path = str(downloaded.local_path)
                    row.mime_type = downloaded.mime_type or row.mime_type
                    row.content_hash = downloaded.content_hash
                    row.download_status = "DOWNLOADED"
                    row.downloaded_at = now_shanghai()
                    summary.downloaded_count += 1
                    attachment_snapshot = store.save_bytes(session, source_url=link.url, content=downloaded.local_path.read_bytes(), content_type=row.mime_type or "application/octet-stream", source_id=candidate.source_id, candidate_id=candidate_id, metadata={"candidate_attachment_id": row.id, "detail_snapshot_id": detail_snapshot.snapshot_id})
                    row.snapshot_id = attachment_snapshot.snapshot_id
                    suffix = _suffix(downloaded.local_path.name, row.file_name, row.mime_type)
                    if suffix == ".zip":
                        inner_paths, zip_notice = _safe_extract_zip(downloaded.local_path, downloaded.local_path.parent)
                        metadata = _load_json(row.metadata_json, {})
                        if not isinstance(metadata, dict):
                            metadata = {}
                        metadata["inner_documents"] = []
                        if zip_notice:
                            metadata["zip_notice"] = zip_notice
                        parsed_inner = 0
                        for inner_path in inner_paths:
                            inner_suffix = inner_path.suffix.lower()
                            inner_mime = {
                                ".pdf": "application/pdf",
                                ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                ".xlsm": "application/vnd.ms-excel.sheet.macroenabled.12",
                            }.get(inner_suffix, "application/octet-stream")
                            inner_content = inner_path.read_bytes()
                            inner_snapshot = store.save_bytes(
                                session,
                                source_url=f"{link.url}#/{inner_path.name}",
                                content=inner_content,
                                content_type=inner_mime,
                                source_id=candidate.source_id,
                                candidate_id=candidate_id,
                                metadata={"candidate_attachment_id": row.id, "archive_snapshot_id": attachment_snapshot.snapshot_id},
                            )
                            inner_doc = parse_path(
                                inner_path,
                                source_url=link.url,
                                content_type=inner_mime,
                                expected_terms=("报名", "获取", "投标", "开标", "截止"),
                            )
                            inner_parse, inner_ok = _apply_parsed_document(
                                session,
                                candidate,
                                inner_doc,
                                source_url=link.url,
                                source_file=str(inner_path),
                                snapshot=inner_snapshot,
                                content_hash_value=inner_snapshot.sha256,
                                definition=definition,
                                summary=summary,
                            )
                            if row.document_id is None:
                                row.document_id = inner_parse.document_id
                            metadata["inner_documents"].append({
                                "path": str(inner_path),
                                "document_id": inner_parse.document_id,
                                "snapshot_id": inner_snapshot.snapshot_id,
                                "parse_status": inner_parse.parse_status,
                            })
                            parsed_inner += int(inner_ok)
                        row.metadata_json = json.dumps(metadata, ensure_ascii=False, default=str)
                        row.parse_status = "SUCCESS" if parsed_inner else "FAILED"
                        row.parse_error = None if parsed_inner and not zip_notice else zip_notice or "ZIP中没有可解析的文档"
                        continue
                    parsed = parse_path(downloaded.local_path, source_url=link.url, content_type=row.mime_type, expected_terms=("报名", "获取", "投标", "开标", "截止"))
                    parse_row, parsed_ok = _apply_parsed_document(
                        session,
                        candidate,
                        parsed,
                        source_url=link.url,
                        source_file=str(downloaded.local_path),
                        snapshot=attachment_snapshot,
                        content_hash_value=row.content_hash,
                        definition=definition,
                        summary=summary,
                    )
                    row.document_id = parse_row.document_id
                    row.parse_status = "SUCCESS" if parsed_ok else "FAILED"
                    row.parse_error = parsed.error
                except HttpFetchError as exc:
                    row.download_status = "BLOCKED" if exc.manual_action_required else "FAILED"
                    row.parse_status = "BLOCKED" if exc.manual_action_required else "FAILED"
                    row.parse_error = str(exc)[:2000]
                    candidate.blocker = "ACCESS_BLOCKED" if exc.manual_action_required else "EXTRACTION_FAILED"
                    summary.status = "BLOCKED" if exc.manual_action_required else "FAILED"
                    summary.blocker = candidate.blocker
                    summary.error = str(exc)
                    if exc.manual_action_required:
                        session.flush()
                        return summary
                except Exception as exc:  # one attachment must not abort siblings
                    row.download_status = "FAILED"
                    row.parse_status = "FAILED"
                    row.parse_error = str(exc)[:2000]
                    candidate.blocker = "EXTRACTION_FAILED"
                    summary.error = str(exc)[:2000]
            values = _load_json(candidate.candidate_values_json, {})
            if not isinstance(values, dict):
                values = {}
            values["source"] = candidate.source_url
            values["has_attachment"] = summary.downloaded_count > 0
            candidate.completeness_score = completeness_score(values, has_attachment=values["has_attachment"], has_evidence=summary.evidence_count > 0)
            candidate.rank_score = candidate.completeness_score
            candidate.missing_fields_json = json.dumps(missing_fields(values), ensure_ascii=False)
            if candidate.project_id:
                project = session.get(Project, candidate.project_id)
                if project is not None:
                    evidence_rows = list(session.scalars(select(Evidence).where(Evidence.project_id == project.project_id)).all())
                    overrides = list(session.scalars(select(ManualOverride).where(ManualOverride.project_id == project.project_id, ManualOverride.active.is_(True))).all())
                    decision = recalculate_status(project_to_record(project), evidences=with_manual_evidence(evidence_rows, overrides, source_url=project.source_url), require_evidence=True)
                    project.status = decision.status.value
                    project.tender_status = project.status
                    project.status_reason = decision.reason_code
                    project.status_evaluated_at = now_shanghai()
            if summary.status == "SKIPPED":
                summary.status = "COMPLETED"
            summary.blocker = candidate.blocker
            candidate.next_action = next_action_for(blocker=candidate.blocker, identity_status=candidate.identity_status, verification_status=candidate.verification_status, missing=_load_json(candidate.missing_fields_json, []))
            candidate.updated_at = now_shanghai()
            session.flush()
    except HttpFetchError as exc:
        summary.status = "BLOCKED" if exc.manual_action_required else "FAILED"
        summary.blocker = "ACCESS_BLOCKED" if exc.manual_action_required else None
        summary.error = str(exc)
    except Exception as exc:
        summary.status = "FAILED"
        summary.error = str(exc)
    finally:
        if owns_client:
            http.close()
    return summary


__all__ = ["CandidateDocumentSummary", "PARSEABLE_ATTACHMENT_SUFFIXES", "SUPPORTED_ATTACHMENT_SUFFIXES", "process_candidate_enrichment_result"]
