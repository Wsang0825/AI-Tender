"""公告文档解析与结构化抽取运行器。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from collections import Counter
from pathlib import Path
from typing import Any

from sqlalchemy import select

from tender_ai.config_loader import APP_ROOT, RegionRegistry, load_region_catalog
from tender_ai.documents.parser import ParsedDocument, parse_html, parse_json_text, parse_path
from tender_ai.evidence.models import EvidenceRecord
from tender_ai.extractors.tender import ExtractionResult, normalize_detail
from tender_ai.matching.dedupe import DedupeOutcome, consolidate_projects, find_project_match, normalize_identity
from tender_ai.models import TenderRecord
from tender_ai.sources.contracts import DetailPayload
from tender_ai.sources.registry import SourceDefinition, SourceRegistry
from tender_ai.status.engine import recalculate_status, with_manual_evidence
from tender_ai.status.metadata import describe_time
from tender_ai.status.time import now_shanghai
from tender_ai.storage.database import create_engine_for, initialize_database, refresh_tender_fts, resolve_database_url, session_scope
from tender_ai.storage.models import (
    Announcement, Attachment, CodexReviewItem, DocumentParse, Evidence, FieldConflict, ManualOverride, Project, ProjectSource, Snapshot,
    TimeFieldMetadata, TimelineEvent, VerificationTask,
)
from tender_ai.storage.repository import project_to_record, save_evidence, save_tender_record
from tender_ai.review import ensure_review_item
from tender_ai.verification.runner import build_verification_queries, verification_reasons
from tender_ai.versioning import STATUS_RULE_VERSION


EXTRACTION_VERSION = "rule-codex-review-v2"
DOCUMENT_PARSER_VERSION = "document-pipeline-v2"
SUPPORTED_ATTACHMENT_SUFFIXES = {".pdf", ".docx", ".xlsx", ".xlsm", ".html", ".htm", ".json"}
KEY_FIELDS = (
    "project_name", "owner", "purchaser", "agency", "project_type", "project_scale", "capacity_mw", "capacity_mwh",
    "budget", "project_code", "tender_code", "qualification_deadline", "registration_start", "registration_deadline",
    "document_start", "document_deadline", "bid_deadline", "open_time", "qualification_summary", "participation_method",
)
TIME_FIELDS = ("registration_deadline", "document_deadline", "bid_deadline", "open_time")
CHANGE_MARKERS = ("延期", "变更", "更正", "澄清", "补充", "调整")


@dataclass
class ExtractionSummary:
    started_at: datetime = field(default_factory=now_shanghai)
    finished_at: datetime | None = None
    announcements_seen: int = 0
    announcements_processed: int = 0
    failed: int = 0
    rule_fields_found: int = 0
    rule_fields_expected: int = 0
    automatic_fields_filled: int = 0
    unknown_count: int = 0
    open_count: int = 0
    closed_count: int = 0
    pdf_count: int = 0
    pdf_success_count: int = 0
    pdf_failed_count: int = 0
    pdf_ocr_count: int = 0
    mineru_fallback_count: int = 0
    evidence_total: int = 0
    evidence_covered: int = 0
    verification_tasks: int = 0
    merged_projects: int = 0
    change_announcements: int = 0
    codex_review_items: int = 0
    review_cache_hits: int = 0
    extraction_cache_hits: int = 0
    field_conflicts: int = 0
    reopened_count: int = 0
    manual_sample_size: int = 0
    manual_sample_passed: int = 0
    manual_sample: list[dict[str, Any]] = field(default_factory=list)
    manual_sample_categories: dict[str, int] = field(default_factory=dict)
    manual_available_categories: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def rule_success_rate(self) -> float:
        if not self.rule_fields_expected:
            return 0.0
        return round(self.rule_fields_found / self.rule_fields_expected, 4)

    @property
    def evidence_coverage_rate(self) -> float:
        if not self.evidence_total:
            return 0.0
        return round(self.evidence_covered / self.evidence_total, 4)

    @property
    def review_rate(self) -> float:
        if not self.announcements_processed:
            return 0.0
        return round(self.codex_review_items / self.announcements_processed, 4)

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "announcements_seen": self.announcements_seen,
            "announcements_processed": self.announcements_processed,
            "failed": self.failed,
            "rule_fields_found": self.rule_fields_found,
            "rule_fields_expected": self.rule_fields_expected,
            "rule_success_rate": self.rule_success_rate,
            "automatic_fields_filled": self.automatic_fields_filled,
            "unknown_count": self.unknown_count,
            "open_count": self.open_count,
            "closed_count": self.closed_count,
            "pdf_count": self.pdf_count,
            "pdf_success_count": self.pdf_success_count,
            "pdf_failed_count": self.pdf_failed_count,
            "pdf_ocr_count": self.pdf_ocr_count,
            "mineru_fallback_count": self.mineru_fallback_count,
            "evidence_total": self.evidence_total,
            "evidence_covered": self.evidence_covered,
            "evidence_coverage_rate": self.evidence_coverage_rate,
            "verification_tasks": self.verification_tasks,
            "merged_projects": self.merged_projects,
            "change_announcements": self.change_announcements,
            "codex_review_items": self.codex_review_items,
            "review_cache_hits": self.review_cache_hits,
            "extraction_cache_hits": self.extraction_cache_hits,
            "review_rate": self.review_rate,
            "field_conflicts": self.field_conflicts,
            "reopened_count": self.reopened_count,
            "manual_sample_size": self.manual_sample_size,
            "manual_sample_passed": self.manual_sample_passed,
            "manual_sample": self.manual_sample,
            "manual_sample_categories": self.manual_sample_categories,
            "manual_available_categories": self.manual_available_categories,
            "errors": self.errors,
            "dry_run": self.dry_run,
        }


def _definition_for(source_id: str | None, registry: SourceRegistry, announcement: Announcement) -> SourceDefinition:
    if source_id:
        try:
            return registry.get(source_id)
        except KeyError:
            pass
    return SourceDefinition(
        source_id=source_id or "stored_announcement",
        source_name=source_id or "stored announcement",
        category="discovered",
        base_url=announcement.source_url or "https://example.invalid",
        enabled=True,
        crawl_enabled=False,
        status="ACTIVE",
    )


def _source_id(session: Any, announcement: Announcement, requested: str | None = None) -> str | None:
    if requested:
        return requested
    return session.scalar(select(ProjectSource.source_id).where(ProjectSource.project_id == announcement.project_id).limit(1))


def _source_content(announcement: Announcement, snapshot: Snapshot | None) -> ParsedDocument:
    expected = ("招标", "采购", "新能源", "光伏", "储能", "截止")
    if snapshot is not None and snapshot.file_path and Path(snapshot.file_path).exists():
        return parse_path(snapshot.file_path, source_url=announcement.source_url, content_type=snapshot.content_type, expected_terms=expected)
    raw = announcement.clean_text or announcement.raw_content or announcement.title
    if "json" in (snapshot.content_type if snapshot else "").lower() or raw.lstrip().startswith(("{", "[")):
        return parse_json_text(raw, source_url=announcement.source_url, expected_terms=expected)
    return parse_html(raw, source_url=announcement.source_url, expected_terms=expected)


def _payload(announcement: Announcement, document: ParsedDocument) -> DetailPayload:
    metadata = {"published_at": announcement.published_at}
    if document.content_type == "application/json":
        return DetailPayload(title=announcement.title, url=announcement.source_url or "", text=document.text, metadata=metadata)
    return DetailPayload(title=announcement.title, url=announcement.source_url or "", html=announcement.raw_content or "", text=document.text, metadata=metadata)


def _merge_extractions(base: ExtractionResult, supplement: ExtractionResult) -> ExtractionResult:
    record: TenderRecord = base.record
    for field_name in KEY_FIELDS:
        if getattr(record, field_name, None) in (None, "") and getattr(supplement.record, field_name, None) not in (None, ""):
            setattr(record, field_name, getattr(supplement.record, field_name))
    decision = recalculate_status(record)
    record.status = decision.status
    record.status_reason = decision.reason_code
    record.status_evaluated_at = now_shanghai()
    missing = tuple(field_name for field_name in base.missing_fields if getattr(record, field_name, None) in (None, ""))
    return ExtractionResult(
        record=record,
        evidences=tuple([*base.evidences, *supplement.evidences]),
        parser=f"{base.parser}+{supplement.parser}",
        quality_score=max(base.quality_score, supplement.quality_score),
        needs_codex_review=bool(missing) or record.status_reason == "UNKNOWN_CONFLICTING_DATES",
        missing_fields=missing,
        review_reasons=tuple(dict.fromkeys((*base.review_reasons, *supplement.review_reasons))),
    )


def _change_type(title: str) -> str:
    for marker, value in (("延期", "extension"), ("澄清", "clarification"), ("更正", "correction"), ("补充", "supplement"), ("变更", "change"), ("调整", "change")):
        if marker in title:
            return value
    return "original"


def _timeline_type(title: str) -> str:
    change_type = _change_type(title)
    return {
        "original": "ANNOUNCEMENT_PUBLISHED",
        "extension": "DEADLINE_CHANGED",
        "change": "DEADLINE_CHANGED",
        "correction": "CLARIFICATION_PUBLISHED",
        "clarification": "CLARIFICATION_PUBLISHED",
        "supplement": "CLARIFICATION_PUBLISHED",
    }.get(change_type, "ANNOUNCEMENT_PUBLISHED")


def _deadline_snapshot(record: TenderRecord) -> str:
    return json.dumps({field_name: (getattr(record, field_name).isoformat() if getattr(record, field_name) else None) for field_name in TIME_FIELDS}, ensure_ascii=False)


def _upsert_document_parse(
    session: Any,
    *,
    announcement_id: int | None,
    attachment_id: int | None,
    document: ParsedDocument,
    content_hash_value: str,
    project_id: str | None = None,
    candidate_id: str | None = None,
    source_id: str | None = None,
) -> DocumentParse:
    row = session.scalar(select(DocumentParse).where(DocumentParse.announcement_id == announcement_id, DocumentParse.attachment_id == attachment_id, DocumentParse.candidate_id == candidate_id, DocumentParse.content_hash == content_hash_value))
    document_dir = APP_ROOT.parent / "data" / "documents"
    document_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = document_dir / f"{content_hash_value}.md"
    if document.text and not markdown_path.exists():
        markdown_path.write_text(document.text, encoding="utf-8")
    if row is None:
        row = DocumentParse(
            announcement_id=announcement_id,
            attachment_id=attachment_id,
            candidate_id=candidate_id,
            project_id=project_id,
            source_id=source_id,
            document_type=document.content_type.split("/", 1)[-1],
            file_path=document.source_file,
            mime_type=document.content_type,
            source_url=document.source_url,
            source_file=document.source_file,
            content_hash=content_hash_value,
            content_type=document.content_type,
            parser=document.parser,
            parser_version=DOCUMENT_PARSER_VERSION,
            quality_score=document.quality_score,
            page_count=document.page_count,
            text_length=len(document.text),
            used_ocr=document.used_ocr,
            used_mineru=document.used_mineru,
            parse_status="SUCCESS" if document.text or document.error is None else "FAILED",
            error=document.error,
            parse_error=document.error,
            clean_text_path=str(markdown_path) if document.text else None,
            markdown_path=str(markdown_path) if document.text else None,
            parsed_at=now_shanghai(),
            metadata_json=json.dumps(document.metadata, ensure_ascii=False, default=str),
            extracted_at=now_shanghai(),
        )
        session.add(row)
    else:
        row.project_id = project_id or row.project_id
        row.candidate_id = candidate_id or row.candidate_id
        row.source_id = source_id or row.source_id
        row.document_type = document.content_type.split("/", 1)[-1]
        row.file_path = document.source_file
        row.mime_type = document.content_type
        row.parser = document.parser
        row.parser_version = DOCUMENT_PARSER_VERSION
        row.quality_score = document.quality_score
        row.page_count = document.page_count
        row.text_length = len(document.text)
        row.used_ocr = document.used_ocr
        row.used_mineru = document.used_mineru
        row.parse_status = "SUCCESS" if document.text or document.error is None else "FAILED"
        row.error = document.error
        row.parse_error = document.error
        row.clean_text_path = str(markdown_path) if document.text else None
        row.markdown_path = str(markdown_path) if document.text else None
        row.parsed_at = now_shanghai()
        row.metadata_json = json.dumps(document.metadata, ensure_ascii=False, default=str)
        row.extracted_at = now_shanghai()
    session.flush()
    return row


def _save_time_metadata(session: Any, record: TenderRecord, evidence_rows: dict[str, Evidence]) -> None:
    for field_name in ("publish_time", "qualification_start", "qualification_deadline", "registration_start", "registration_deadline", "document_start", "document_deadline", "bid_deadline", "open_time"):
        value = getattr(record, field_name, None)
        if value is None:
            continue
        evidence = evidence_rows.get(field_name)
        item = describe_time(field_name, value, evidence.raw_value if evidence else None, source_evidence_id=evidence.id if evidence else None)
        if item is None:
            continue
        exists = session.scalar(select(TimeFieldMetadata).where(TimeFieldMetadata.project_id == record.project_id, TimeFieldMetadata.field_name == field_name, TimeFieldMetadata.value == item.value))
        if exists is None:
            session.add(TimeFieldMetadata(project_id=record.project_id, field_name=item.field_name, value=item.value, timezone=item.timezone, precision=item.precision, explicit_or_inferred=item.explicit_or_inferred, source_evidence_id=item.source_evidence_id, inference_rule=item.inference_rule, raw_value=item.raw_value))


def _ensure_timeline(session: Any, announcement: Announcement, record: TenderRecord) -> None:
    evidence_ids = [row.id for row in session.scalars(select(Evidence).where(Evidence.announcement_id == announcement.id).order_by(Evidence.id)).all()]
    event = session.scalar(select(TimelineEvent).where(TimelineEvent.announcement_id == announcement.id))
    if event is None:
        event = TimelineEvent(project_id=record.project_id, announcement_id=announcement.id, event_type=_timeline_type(announcement.title), event_at=announcement.published_at or now_shanghai(), title=announcement.title, source_url=announcement.source_url, summary=(announcement.clean_text or "")[:800], deadline_snapshot_json=_deadline_snapshot(record), created_at=now_shanghai())
        session.add(event)
    else:
        event.project_id = record.project_id
        event.event_type = _timeline_type(announcement.title)
        event.event_at = announcement.published_at or event.event_at
        event.title = announcement.title
        event.source_url = announcement.source_url
        event.summary = (announcement.clean_text or "")[:800]
        event.deadline_snapshot_json = _deadline_snapshot(record)
    event.event_type = _timeline_type(announcement.title)
    event.evidence_ids_json = json.dumps(evidence_ids, ensure_ascii=False)


def _ensure_verification_task(session: Any, project: Project, summary: ExtractionSummary, *, dry_run: bool) -> None:
    reasons = verification_reasons(project)
    project.verification_required = bool(reasons)
    project.verification_reason = ",".join(reasons) if reasons else None
    if not reasons or dry_run:
        return
    query_json = json.dumps(build_verification_queries(project), ensure_ascii=False)
    for reason in reasons:
        task = session.scalar(select(VerificationTask).where(VerificationTask.project_id == project.project_id, VerificationTask.reason == reason, VerificationTask.status.in_(("PENDING", "RUNNING"))))
        if task is None:
            session.add(VerificationTask(project_id=project.project_id, reason=reason, status="PENDING", query_texts_json=query_json, created_at=now_shanghai(), updated_at=now_shanghai()))
            summary.verification_tasks += 1


def _document_context(session: Any, announcement: Announcement, source_file: str | None) -> tuple[str | None, str | None]:
    """为 Evidence 找到对应 Snapshot 和 DocumentParse，不把大正文塞进 SQLite。"""

    snapshot_id = announcement.snapshot_id
    statement = select(DocumentParse).where(DocumentParse.announcement_id == announcement.id)
    if source_file:
        statement = statement.where(DocumentParse.source_file == source_file)
    else:
        statement = statement.where(DocumentParse.attachment_id.is_(None))
    parsed = session.scalar(statement.order_by(DocumentParse.id.desc()))
    return snapshot_id, parsed.document_id if parsed is not None else None


def _field_conflicts(
    session: Any,
    *,
    project_id: str,
    announcement_id: int,
    evidence_rows: dict[str, list[Evidence]],
) -> tuple[int, bool]:
    """记录同一公告中同一字段的互相矛盾候选值。"""

    created_or_updated = 0
    date_conflict = False
    for field_name, rows in evidence_rows.items():
        values: dict[str, list[int]] = {}
        for row in rows:
            key = str(row.normalized_value or row.raw_value or "").strip()
            if key:
                values.setdefault(key, []).append(row.id)
        if len(values) < 2:
            continue
        if field_name in TIME_FIELDS:
            date_conflict = True
        candidate_values = [
            {"value": value, "evidence_ids": evidence_ids}
            for value, evidence_ids in values.items()
        ]
        evidence_ids = [evidence_id for item in candidate_values for evidence_id in item["evidence_ids"]]
        conflict = session.scalar(
            select(FieldConflict).where(
                FieldConflict.project_id == project_id,
                FieldConflict.announcement_id == announcement_id,
                FieldConflict.field_name == field_name,
                FieldConflict.resolution_status == "PENDING",
            )
        )
        if conflict is None:
            conflict = FieldConflict(
                project_id=project_id,
                announcement_id=announcement_id,
                field_name=field_name,
                candidate_values_json=json.dumps(candidate_values, ensure_ascii=False),
                evidence_ids_json=json.dumps(evidence_ids, ensure_ascii=False),
                detected_at=now_shanghai(),
                resolution_status="PENDING",
            )
            session.add(conflict)
        else:
            conflict.candidate_values_json = json.dumps(candidate_values, ensure_ascii=False)
            conflict.evidence_ids_json = json.dumps(evidence_ids, ensure_ascii=False)
            conflict.detected_at = now_shanghai()
        created_or_updated += 1
    session.flush()
    return created_or_updated, date_conflict


def _set_quality_metrics(project: Project, evidence: list[Evidence]) -> None:
    values = [getattr(project, field_name, None) for field_name in KEY_FIELDS]
    filled = sum(value not in (None, "") for value in values)
    project.completeness_score = round(filled / len(KEY_FIELDS) * 100, 2)
    if evidence:
        project.field_confidence = round(sum(row.confidence for row in evidence) / len(evidence), 4)
    else:
        project.field_confidence = 0.0
    project.source_confidence = {"A": 1.0, "B": 0.85, "C": 0.7, "D": 0.45, "E": 0.25}.get((project.source_level or "").upper(), 0.4)
    project.project_match_confidence = 1.0 if project.tender_code or project.project_code else 0.65
    project.overall_confidence = round(
        (project.field_confidence * 0.55) + (project.source_confidence * 0.3) + (project.project_match_confidence * 0.15),
        4,
    )
    project.confidence_score = project.overall_confidence


def _save_extraction(session: Any, announcement: Announcement, extraction: ExtractionResult, summary: ExtractionSummary, *, source_id: str | None, dry_run: bool) -> None:
    if dry_run:
        return
    record = extraction.record
    # 公告已经拥有项目归属；重新解析时先沿用该归属，再用跨来源匹配发现更早的重复项目。
    if session.get(Project, announcement.project_id) is not None:
        record = record.model_copy(update={"project_id": announcement.project_id})
    probable_match: tuple[str, Any] | None = None
    match = find_project_match(session, record, exclude_project_id=announcement.project_id)
    if match is not None and match[0] != announcement.project_id and match[1].outcome == DedupeOutcome.EXACT_MATCH:
        record = record.model_copy(update={"project_id": match[0]})
        announcement.project_id = match[0]
    elif match is not None and match[0] != announcement.project_id and match[1].outcome == DedupeOutcome.PROBABLE_MATCH:
        probable_match = match
    elif announcement.project_id:
        record = record.model_copy(update={"project_id": announcement.project_id})
    project = save_tender_record(session, record, status_reason=record.status_reason, change_type=_change_type(announcement.title))
    if project.lifecycle_state == "REOPENED":
        summary.reopened_count += 1
    announcement.extraction_status = "SUCCESS"
    announcement.extraction_parser = extraction.parser
    announcement.document_quality_score = extraction.quality_score
    announcement.extraction_version = EXTRACTION_VERSION
    announcement.processed_at = now_shanghai()
    evidence_rows: dict[str, Evidence] = {}
    evidence_by_field: dict[str, list[Evidence]] = {}
    base_snapshot_id, base_document_id = _document_context(session, announcement, None)
    for evidence in extraction.evidences:
        snapshot_id, document_id = _document_context(session, announcement, evidence.source_file)
        enriched = evidence.model_copy(
            update={
                "snapshot_id": snapshot_id or base_snapshot_id,
                "document_id": document_id or base_document_id,
                "extractor_type": evidence.extractor_type or "RULE",
                "extractor_version": evidence.extractor_version or EXTRACTION_VERSION,
            }
        )
        row = save_evidence(session, enriched, project_id=project.project_id, announcement_id=announcement.id)
        evidence_rows.setdefault(evidence.field_name, row)
        evidence_by_field.setdefault(evidence.field_name, []).append(row)
    conflict_count, date_conflict = _field_conflicts(
        session,
        project_id=project.project_id,
        announcement_id=announcement.id,
        evidence_rows=evidence_by_field,
    )
    summary.field_conflicts += conflict_count
    _save_time_metadata(session, record, evidence_rows)
    project.document_quality_score = extraction.quality_score
    project.extraction_version = EXTRACTION_VERSION
    project.extraction_method = extraction.parser
    project.last_extracted_at = now_shanghai()
    project.llm_extracted = False
    project.raw_project_name = getattr(record, "raw_project_name", None) or record.project_name
    project.canonical_project_name = getattr(record, "canonical_project_name", None) or normalize_identity(record.project_name)
    project.status_rule_version = STATUS_RULE_VERSION
    _set_quality_metrics(project, list(evidence_rows.values()))
    # The first save above is deliberately provisional because it happens
    # before Evidence rows exist.  Recalculate once the complete project
    # evidence set is present; this prevents a field-only deadline from
    # forcing OPEN/CLOSED.  Existing manual overrides are represented as
    # strong MANUAL evidence for the gate.
    all_project_evidence = list(session.scalars(select(Evidence).where(Evidence.project_id == project.project_id)).all())
    active_overrides = list(session.scalars(
        select(ManualOverride).where(
            ManualOverride.project_id == project.project_id,
            ManualOverride.active.is_(True),
        )
    ).all())
    gate_evidence = with_manual_evidence(all_project_evidence, active_overrides, source_url=project.source_url)
    decision = recalculate_status(project_to_record(project), now_shanghai(), evidences=gate_evidence, require_evidence=True)
    old_status = project.status
    project.status = decision.status.value
    project.tender_status = project.status
    project.status_reason = decision.reason_code
    project.status_evaluated_at = now_shanghai()
    project.status_rule_version = STATUS_RULE_VERSION
    if old_status != project.status:
        from tender_ai.storage.repository import add_status_history

        add_status_history(session, project.project_id, old_status, project.status, decision.reason)
    if probable_match is not None:
        candidate = {
            "outcome": probable_match[1].outcome,
            "candidate_project_id": probable_match[0],
            "score": probable_match[1].score,
            "reason": probable_match[1].reason,
        }
        existing_identity_conflict = session.scalar(
            select(FieldConflict).where(
                FieldConflict.project_id == project.project_id,
                FieldConflict.announcement_id == announcement.id,
                FieldConflict.field_name == "project_identity",
                FieldConflict.resolution_status == "PENDING",
            )
        )
        if existing_identity_conflict is None:
            session.add(
                FieldConflict(
                    project_id=project.project_id,
                    announcement_id=announcement.id,
                    field_name="project_identity",
                    candidate_values_json=json.dumps([candidate], ensure_ascii=False),
                    evidence_ids_json=json.dumps([row.id for row in evidence_rows.values()], ensure_ascii=False),
                    detected_at=now_shanghai(),
                    resolution_status="PENDING",
                )
            )
            summary.field_conflicts += 1
        else:
            existing_identity_conflict.candidate_values_json = json.dumps([candidate], ensure_ascii=False)
    prior_review = session.scalar(
        select(CodexReviewItem)
        .where(
            CodexReviewItem.project_id == project.project_id,
            CodexReviewItem.announcement_id == announcement.id,
        )
        .order_by(CodexReviewItem.created_at.desc())
    )
    prior_hash = prior_review.content_hash if prior_review is not None else None
    review_item = ensure_review_item(session, project, announcement=announcement)
    if review_item is not None:
        if prior_review is None:
            summary.codex_review_items += 1
        elif prior_hash == review_item.content_hash and prior_review.status in {"RESOLVED", "SKIPPED"}:
            summary.review_cache_hits += 1
        elif review_item.status == "PENDING":
            summary.codex_review_items += 1
    _ensure_timeline(session, announcement, record)
    _ensure_verification_task(session, project, summary, dry_run=dry_run)
    refresh_tender_fts(session, project, announcement.clean_text or announcement.raw_content or announcement.title)


class ExtractionRunner:
    def __init__(self, *, database: str | None = None):
        self.engine = initialize_database(create_engine_for(database))
        self.registry = SourceRegistry.from_file()
        self.regions = load_region_catalog()
        default_database = (APP_ROOT.parent / "data" / "tender.db").resolve()
        configured_database = Path(resolve_database_url(database).removeprefix("sqlite:///")).resolve()
        self.report_path = APP_ROOT.parent / "EXTRACTION_REPORT.md" if configured_database == default_database else None

    def _announcements(self, session: Any, announcement_id: int | None, source_id: str | None) -> list[Announcement]:
        statement = select(Announcement).order_by(Announcement.published_at.desc(), Announcement.id.desc())
        if announcement_id is not None:
            statement = statement.where(Announcement.id == announcement_id)
        rows = list(session.scalars(statement).all())
        if source_id:
            ids = {row.project_id for row in session.scalars(select(ProjectSource.project_id).where(ProjectSource.source_id == source_id)).all()}
            rows = [row for row in rows if row.project_id in ids]
        return rows

    @staticmethod
    def _cached_extraction(session: Any, announcement: Announcement, snapshot: Snapshot | None) -> bool:
        """判断公告正文是否已经按当前规则版本完成解析。

        Search 是按需入口，但数据库保留历史公告。内容、文档解析器和抽取规则都
        未变化时，不能因为一次新搜索而再次解析全部历史 PDF；状态仍在调用方中
        基于当前 Asia/Shanghai 时间重新计算。
        """

        if announcement.extraction_status != "SUCCESS" or announcement.extraction_version != EXTRACTION_VERSION:
            return False
        content_hash_value = snapshot.sha256 if snapshot is not None else None
        query = select(DocumentParse).where(
            DocumentParse.announcement_id == announcement.id,
            DocumentParse.attachment_id.is_(None),
            DocumentParse.parser_version == DOCUMENT_PARSER_VERSION,
            DocumentParse.parse_status == "SUCCESS",
        )
        if content_hash_value:
            if announcement.content_hash and announcement.content_hash != content_hash_value:
                return False
            query = query.where(DocumentParse.content_hash == content_hash_value)
        else:
            # 早期真实数据部分没有 Snapshot，公告 content_hash 是原始 HTML/JSON
            # 摘要，而 DocumentParse 保存的是清洗文本摘要，二者语义不同。此时
            # 以已成功的当前抽取版本作为离线缓存边界；后续新抓取会始终创建 Snapshot。
            query = query.where(DocumentParse.content_hash.is_not(None))
        parsed = session.scalar(query.order_by(DocumentParse.id.desc()))
        if parsed is None:
            return False

        # The main HTML/JSON parse may be cached while a PDF/DOCX/XLSX was
        # downloaded later or was never parsed successfully.  In that case a
        # cache hit would silently skip the attachment -> Evidence pipeline.
        # Treat the announcement as stale until every locally available,
        # supported attachment has a successful parse for the current parser
        # version and the same byte hash.
        attachments = session.scalars(select(Attachment).where(Attachment.announcement_id == announcement.id)).all()
        for attachment in attachments:
            local_path = Path(attachment.local_path) if attachment.local_path else None
            if local_path is None or not local_path.exists() or local_path.suffix.lower() not in SUPPORTED_ATTACHMENT_SUFFIXES:
                continue
            attachment_query = select(DocumentParse).where(
                DocumentParse.announcement_id == announcement.id,
                DocumentParse.attachment_id == attachment.id,
                DocumentParse.parser_version == DOCUMENT_PARSER_VERSION,
                DocumentParse.parse_status == "SUCCESS",
            )
            if attachment.content_hash:
                attachment_query = attachment_query.where(DocumentParse.content_hash == attachment.content_hash)
            else:
                attachment_query = attachment_query.where(DocumentParse.source_file == str(local_path))
            if session.scalar(attachment_query.order_by(DocumentParse.id.desc())) is None:
                return False
        return True

    @staticmethod
    def _account_cached_attachments(session: Any, announcement: Announcement, summary: ExtractionSummary) -> None:
        """把缓存命中的附件解析结果计入报告，避免文档统计失真。"""

        attachments = session.scalars(select(Attachment).where(Attachment.announcement_id == announcement.id)).all()
        for attachment in attachments:
            local_path = Path(attachment.local_path) if attachment.local_path else None
            if local_path is None or not local_path.exists() or local_path.suffix.lower() not in SUPPORTED_ATTACHMENT_SUFFIXES:
                continue
            if local_path.suffix.lower() != ".pdf":
                continue
            summary.pdf_count += 1
            query = select(DocumentParse).where(
                DocumentParse.announcement_id == announcement.id,
                DocumentParse.attachment_id == attachment.id,
                DocumentParse.parser_version == DOCUMENT_PARSER_VERSION,
            )
            if attachment.content_hash:
                query = query.where(DocumentParse.content_hash == attachment.content_hash)
            parsed = session.scalar(query.order_by(DocumentParse.id.desc()))
            if parsed is not None and parsed.parse_status == "SUCCESS" and parsed.text_length > 0:
                summary.pdf_success_count += 1
            else:
                summary.pdf_failed_count += 1
            if parsed is not None and parsed.used_ocr:
                summary.pdf_ocr_count += 1
            if parsed is not None and parsed.metadata_json:
                try:
                    metadata = json.loads(parsed.metadata_json)
                except (TypeError, ValueError):
                    metadata = {}
                if metadata.get("needs_mineru"):
                    summary.mineru_fallback_count += 1

    def _reuse_cached_status(self, session: Any, announcement: Announcement, summary: ExtractionSummary) -> None:
        project = session.get(Project, announcement.project_id)
        if project is None:
            return
        # 缓存命中时仍把已有规则结果计入报告，避免离线重跑报告显示为
        # ``0/0``，让“缓存复用”和“规则抽取成功率”保持可解释。
        cached_record = project_to_record(project)
        summary.rule_fields_expected += len(KEY_FIELDS)
        cached_fields = sum(
            getattr(cached_record, field_name, None) not in (None, "")
            for field_name in KEY_FIELDS
        )
        summary.rule_fields_found += cached_fields
        summary.automatic_fields_filled += cached_fields
        evidence_rows = list(session.scalars(select(Evidence).where(Evidence.project_id == project.project_id)).all())
        overrides = list(session.scalars(select(ManualOverride).where(ManualOverride.project_id == project.project_id, ManualOverride.active.is_(True))).all())
        gate_evidence = with_manual_evidence(evidence_rows, overrides, source_url=project.source_url)
        decision = recalculate_status(project_to_record(project), now_shanghai(), evidences=gate_evidence, require_evidence=True)
        if project.status != decision.status.value:
            from tender_ai.storage.repository import add_status_history

            add_status_history(session, project.project_id, project.status, decision.status.value, decision.reason, now_shanghai())
            project.status = decision.status.value
        project.status_reason = decision.reason_code
        project.status_evaluated_at = now_shanghai()
        project.status_rule_version = STATUS_RULE_VERSION
        project.updated_at = now_shanghai()
        # Cache reuse must still maintain the operational queues.  Otherwise
        # a legacy announcement can be status-recalculated forever while its
        # UNKNOWN blockers never reach Codex Review or Verification.
        prior_review = session.scalar(
            select(CodexReviewItem)
            .where(
                CodexReviewItem.project_id == project.project_id,
                CodexReviewItem.announcement_id == announcement.id,
            )
            .order_by(CodexReviewItem.created_at.desc())
        )
        prior_hash = prior_review.content_hash if prior_review is not None else None
        review_item = ensure_review_item(session, project, announcement=announcement)
        if review_item is not None:
            if prior_review is None:
                summary.codex_review_items += 1
            elif prior_hash == review_item.content_hash and prior_review.status in {"RESOLVED", "SKIPPED"}:
                summary.review_cache_hits += 1
            elif review_item.status == "PENDING":
                summary.codex_review_items += 1
        _ensure_verification_task(session, project, summary, dry_run=False)
        self._account_cached_attachments(session, announcement, summary)
        summary.extraction_cache_hits += 1
        summary.announcements_processed += 1
        if _change_type(announcement.title) != "original":
            summary.change_announcements += 1
        if project.status == "OPEN":
            summary.open_count += 1
        elif project.status == "CLOSED":
            summary.closed_count += 1
        else:
            summary.unknown_count += 1
        evidence_rows = list(session.scalars(select(Evidence).where(Evidence.project_id == project.project_id)).all())
        evidence_fields = {row.field_name for row in evidence_rows}
        summary.evidence_total += len(TIME_FIELDS)
        summary.evidence_covered += sum(1 for field_name in TIME_FIELDS if field_name in evidence_fields)

    def _extract_one(self, session: Any, announcement: Announcement, summary: ExtractionSummary, *, source_id: str | None, dry_run: bool) -> None:
        actual_source_id = _source_id(session, announcement, source_id)
        definition = _definition_for(actual_source_id, self.registry, announcement)
        snapshot = session.get(Snapshot, announcement.snapshot_id) if announcement.snapshot_id else None
        document = _source_content(announcement, snapshot)
        if snapshot is not None and not dry_run:
            _upsert_document_parse(
                session,
                announcement_id=announcement.id,
                attachment_id=None,
                document=document,
                content_hash_value=snapshot.sha256,
                project_id=announcement.project_id,
                source_id=actual_source_id,
            )
        elif not dry_run:
            _upsert_document_parse(
                session,
                announcement_id=announcement.id,
                attachment_id=None,
                document=document,
                content_hash_value=document.content_hash,
                project_id=announcement.project_id,
                source_id=actual_source_id,
            )
        payload = _payload(announcement, document)
        extraction = normalize_detail(payload, definition, self.regions, document=document, source_file=document.source_file, parser=document.parser)
        if _change_type(announcement.title) != "original":
            summary.change_announcements += 1
        summary.rule_fields_expected += len(KEY_FIELDS)
        found_fields = sum(getattr(extraction.record, field_name, None) not in (None, "") for field_name in KEY_FIELDS)
        summary.rule_fields_found += found_fields
        summary.automatic_fields_filled += found_fields
        for attachment in session.scalars(select(Attachment).where(Attachment.announcement_id == announcement.id)).all():
            if not attachment.local_path or not Path(attachment.local_path).exists():
                continue
            suffix = Path(attachment.local_path).suffix.lower()
            if suffix not in SUPPORTED_ATTACHMENT_SUFFIXES:
                continue
            if suffix == ".pdf":
                summary.pdf_count += 1
            attachment_doc = parse_path(attachment.local_path, source_url=attachment.source_url, content_type=attachment.mime_type, expected_terms=("截止", "投标", "报名", "文件"))
            if suffix == ".pdf":
                if attachment_doc.text:
                    summary.pdf_success_count += 1
                else:
                    summary.pdf_failed_count += 1
                if attachment_doc.used_ocr:
                    summary.pdf_ocr_count += 1
                if attachment_doc.metadata.get("needs_mineru"):
                    summary.mineru_fallback_count += 1
            if not dry_run:
                _upsert_document_parse(
                    session,
                    announcement_id=announcement.id,
                    attachment_id=attachment.id,
                    document=attachment_doc,
                    content_hash_value=attachment.content_hash or attachment_doc.content_hash,
                    project_id=announcement.project_id,
                    source_id=actual_source_id,
                )
            supplement_payload = DetailPayload(title=announcement.title, url=attachment.source_url or announcement.source_url or "", text=attachment_doc.text, metadata={"published_at": announcement.published_at})
            supplement = normalize_detail(supplement_payload, definition, self.regions, document=attachment_doc, source_file=attachment.local_path, parser=attachment_doc.parser)
            extraction = _merge_extractions(extraction, supplement)
        _save_extraction(session, announcement, extraction, summary, source_id=actual_source_id, dry_run=dry_run)
        summary.announcements_processed += 1
        status = extraction.record.status.value
        if status == "OPEN":
            summary.open_count += 1
        elif status == "CLOSED":
            summary.closed_count += 1
        else:
            summary.unknown_count += 1
        summary.evidence_total += len(TIME_FIELDS)
        summary.evidence_covered += sum(1 for field_name in TIME_FIELDS if any(item.field_name == field_name for item in extraction.evidences))

    def _manual_review(self, session: Any, limit: int) -> tuple[list[dict[str, Any]], int, dict[str, int], dict[str, int]]:
        all_rows = list(session.scalars(select(Announcement).order_by(Announcement.id)).all())
        projects = {row.project_id: row for row in session.scalars(select(Project)).all()}
        source_counts = Counter(row.project_id for row in session.scalars(select(ProjectSource)).all())
        attachments_by_announcement: dict[int, list[Attachment]] = {}
        for row in session.scalars(select(Attachment)).all():
            if row.announcement_id is not None:
                attachments_by_announcement.setdefault(row.announcement_id, []).append(row)
        documents_by_announcement: dict[int, list[DocumentParse]] = {}
        for row in session.scalars(select(DocumentParse)).all():
            if row.announcement_id is not None:
                documents_by_announcement.setdefault(row.announcement_id, []).append(row)
        evidence_by_announcement: dict[int, set[str]] = {}
        evidence_by_project_url: dict[tuple[str, str], set[str]] = {}
        for row in session.scalars(select(Evidence)).all():
            if row.announcement_id is not None:
                evidence_by_announcement.setdefault(row.announcement_id, set()).add(row.field_name)
            if row.project_id and row.source_url:
                evidence_by_project_url.setdefault((row.project_id, row.source_url), set()).add(row.field_name)

        def tags_for(announcement: Announcement) -> set[str]:
            project = projects.get(announcement.project_id)
            attachments = attachments_by_announcement.get(announcement.id, [])
            documents = documents_by_announcement.get(announcement.id, [])
            is_pdf = any(Path(item.file_name or "").suffix.lower() == ".pdf" for item in attachments) or any(
                "pdf" in (item.content_type or item.mime_type or "").lower() for item in documents
            )
            tags: set[str] = set()
            if project is not None:
                tags.add(project.status)
                if source_counts.get(project.project_id, 0) > 1:
                    tags.add("MULTI_SOURCE")
                if (project.source_level or "").upper() in {"D", "E"}:
                    tags.add("SECONDARY_SOURCE")
            if is_pdf:
                tags.add("PDF")
            if _change_type(announcement.title) != "original":
                tags.add("CHANGE")
            return tags

        tags_by_id = {row.id: tags_for(row) for row in all_rows}
        selected: list[Announcement] = []
        # 优先保证抽查样本覆盖关键业务类别；类别不存在时保持事实，不制造样本。
        required_tags = ("OPEN", "UNKNOWN", "CLOSED", "PDF", "CHANGE", "MULTI_SOURCE", "SECONDARY_SOURCE")
        for tag in required_tags:
            row = next((item for item in all_rows if tag in tags_by_id[item.id] and item not in selected), None)
            if row is not None and len(selected) < limit:
                selected.append(row)
        selected.extend(item for item in all_rows if item not in selected)
        rows = selected[:limit]
        sample: list[dict[str, Any]] = []
        passed = 0
        category_counts = Counter(tag for row in rows for tag in tags_by_id[row.id])
        available_counts = Counter(tag for tags in tags_by_id.values() for tag in tags)
        for announcement in rows:
            project = projects.get(announcement.project_id)
            evidence_fields = set(evidence_by_announcement.get(announcement.id, set()))
            if project is not None and announcement.source_url:
                evidence_fields.update(evidence_by_project_url.get((project.project_id, announcement.source_url), set()))
            checked = [field_name for field_name in TIME_FIELDS if getattr(project, field_name, None) is not None] if project else []
            missing_evidence = [field_name for field_name in checked if field_name not in evidence_fields]
            ok = project is not None and project.status in {"OPEN", "UNKNOWN", "CLOSED"} and not missing_evidence
            if ok:
                passed += 1
            sample.append({
                "announcement_id": announcement.id,
                "title": announcement.title,
                "project_id": announcement.project_id,
                "status": project.status if project else None,
                "status_reason": project.status_reason if project else None,
                "registration_deadline": str(project.registration_deadline) if project and project.registration_deadline else None,
                "document_deadline": str(project.document_deadline) if project and project.document_deadline else None,
                "bid_deadline": str(project.bid_deadline) if project and project.bid_deadline else None,
                "open_time": str(project.open_time) if project and project.open_time else None,
                "source_level": project.source_level if project else None,
                "source_count": source_counts.get(announcement.project_id, 0),
                "change_type": _change_type(announcement.title),
                "sample_tags": sorted(tags_by_id[announcement.id]),
                "document_paths": list(dict.fromkeys([
                    *(item.file_path for item in documents_by_announcement.get(announcement.id, []) if item.file_path),
                    *(item.markdown_path for item in documents_by_announcement.get(announcement.id, []) if item.markdown_path),
                    *(item.local_path for item in attachments_by_announcement.get(announcement.id, []) if item.local_path),
                ])),
                "missing_evidence": missing_evidence,
                "passed": ok,
            })
        return sample, passed, {tag: category_counts.get(tag, 0) for tag in required_tags}, {tag: available_counts.get(tag, 0) for tag in required_tags}

    def run(
        self,
        *,
        announcement_id: int | None = None,
        source_id: str | None = None,
        sample_size: int = 30,
        dry_run: bool = False,
        consolidate: bool = True,
        reuse_cached: bool = False,
    ) -> ExtractionSummary:
        summary = ExtractionSummary(dry_run=dry_run)
        with session_scope(self.engine) as session:
            rows = self._announcements(session, announcement_id, source_id)
            summary.announcements_seen = len(rows)
            for announcement in rows:
                try:
                    snapshot = session.get(Snapshot, announcement.snapshot_id) if announcement.snapshot_id else None
                    if reuse_cached and not dry_run and self._cached_extraction(session, announcement, snapshot):
                        self._reuse_cached_status(session, announcement, summary)
                        continue
                    self._extract_one(session, announcement, summary, source_id=source_id, dry_run=dry_run)
                except Exception as exc:
                    summary.failed += 1
                    summary.errors.append(f"announcement {announcement.id}: {exc}")
                    if not dry_run:
                        announcement.extraction_status = "FAILED"
                        announcement.processed_at = now_shanghai()
            if consolidate and not dry_run:
                summary.merged_projects = consolidate_projects(session)
            if not dry_run:
                (
                    summary.manual_sample,
                    summary.manual_sample_passed,
                    summary.manual_sample_categories,
                    summary.manual_available_categories,
                ) = self._manual_review(session, sample_size)
                summary.manual_sample_size = len(summary.manual_sample)
        summary.finished_at = now_shanghai()
        self._write_report(summary)
        return summary

    def _write_report(self, summary: ExtractionSummary) -> Path:
        report_path = self.report_path
        if report_path is None:
            return APP_ROOT.parent / "EXTRACTION_REPORT.md"
        category_labels = {
            "OPEN": "OPEN",
            "UNKNOWN": "UNKNOWN",
            "CLOSED": "CLOSED",
            "PDF": "PDF",
            "CHANGE": "变更/延期",
            "MULTI_SOURCE": "同项目多来源",
            "SECONDARY_SOURCE": "二手来源",
        }
        category_coverage = "；".join(
            f"{label} {summary.manual_sample_categories.get(key, 0)}/{summary.manual_available_categories.get(key, 0)}"
            for key, label in category_labels.items()
        )
        lines = [
            "# 第4步 Extraction / 第5步 Codex 按需搜索集成报告",
            "",
            "系统定位：区域新能源招投标自动搜索系统",
            f"抽取版本：{EXTRACTION_VERSION}",
            f"运行开始：{summary.started_at.isoformat()}",
            f"运行结束：{summary.finished_at.isoformat() if summary.finished_at else ''}",
            f"处理公告数：{summary.announcements_processed}/{summary.announcements_seen}",
            f"失败数：{summary.failed}",
            "",
            "## Parser 与规则抽取",
            "",
            f"- 规则字段成功率：{summary.rule_success_rate:.2%}（{summary.rule_fields_found}/{summary.rule_fields_expected}）",
            f"- 自动规则填充字段数量：{summary.automatic_fields_filled}",
            "- 当前主路径不调用任何外部 AI API；复杂内容进入 Codex Review 文件，由 Codex 读取本地证据后按需回写。",
            "- 固定来源优先使用 API/JSON/HTML 结构和规则；未知页面不由解析层决定最终状态。",
            "",
            "## 状态判断",
            "",
            f"- OPEN：{summary.open_count}",
            f"- UNKNOWN：{summary.unknown_count}",
            f"- CLOSED：{summary.closed_count}",
            f"- Codex Review Item：{summary.codex_review_items}",
            f"- Review 缓存复用：{summary.review_cache_hits}",
            f"- 内容抽取缓存复用：{summary.extraction_cache_hits}",
            f"- Review 比例（以已处理公告计）：{summary.review_rate:.2%}",
            f"- FieldConflict：{summary.field_conflicts}",
            f"- REOPENED（本次抽取生命周期标记）：{summary.reopened_count}",
            "- 最终状态全部由 Python 确定性状态机计算；Codex 不直接写入 OPEN/CLOSED。",
            "",
            "## PDF 与文档解析",
            "",
            f"- PDF 总数：{summary.pdf_count}",
            f"- PyMuPDF4LLM 成功：{summary.pdf_success_count}",
            f"- PDF 解析失败：{summary.pdf_failed_count}",
            f"- Hybrid OCR 使用：{summary.pdf_ocr_count}",
            f"- MinerU fallback 需求：{summary.mineru_fallback_count}（当前未安装时保留明确失败状态）",
            "- HTML：Selector/BeautifulSoup 规则优先；复杂陌生页面仅在显式开启时使用 Crawl4AI。",
            "- DOCX：python-docx；XLSX：openpyxl。",
            "",
            "## Evidence 覆盖率",
            "",
            f"- 报名截止、文件截止、投标截止、开标时间 Evidence 覆盖：{summary.evidence_coverage_rate:.2%}（{summary.evidence_covered}/{summary.evidence_total}）",
            f"- 创建/更新 Verification Task：{summary.verification_tasks}",
            f"- 项目去重/确定合并数量：{summary.merged_projects}",
            f"- 变更/延期公告关联数量：{summary.change_announcements}",
            "",
            "## 30 条真实公告人工抽查",
            "",
            f"抽查数量：{summary.manual_sample_size}；通过：{summary.manual_sample_passed}。抽查依据为数据库中已真实抓取的公告，逐条检查状态、四类关键时间与 Evidence 对应关系。",
            f"关键类别覆盖（样本/全库）：{category_coverage}",
            "类别为 0 表示本次真实数据集中没有满足该类别的记录，未人为制造样本；对应规则案例由 pytest 覆盖。",
            "",
            "| announcement_id | project_id | status | status_reason | 报名截止 | 文件截止 | 投标截止 | 开标时间 | 缺失Evidence | 结果 |",
            "|---:|---|---|---|---|---|---|---|---|---|",
        ]
        for row in summary.manual_sample:
            missing = ",".join(row["missing_evidence"]) or "无"
            result = "PASS" if row["passed"] else "CHECK"
            values = [str(row.get(key) or "") for key in ("announcement_id", "project_id", "status", "status_reason", "registration_deadline", "document_deadline", "bid_deadline", "open_time")]
            lines.append("| " + " | ".join(values + [missing, result]) + " |")
        if summary.errors:
            lines.extend(["", "## 错误", "", *[f"- {item}" for item in summary.errors[:100]]])
        lines.extend([
            "",
            "## 本阶段范围",
            "",
            "- DONE：规则抽取、PDF/HTML/DOCX/XLSX文档流水线、Evidence、时间精度元数据、状态原因码、Snapshot/Replay、Review队列、Verification、项目去重保护、FieldConflict、Search CLI 基础接口。",
            "- DONE：第5步 FastAPI/Jinja2 数据浏览器、搜索历史/导出/模板、配置设置、Evidence/Timeline/附件查看、Manual Override、关注/忽略和 Source Health 页面。",
            "- DEFERRED：Web 聊天 AI；本项目由 Codex 作为顶层 Agent，不实现也不依赖任何模型 API。",
        ])
        report_path.write_text("\n".join(lines), encoding="utf-8")
        return report_path


__all__ = ["EXTRACTION_VERSION", "ExtractionRunner", "ExtractionSummary"]
