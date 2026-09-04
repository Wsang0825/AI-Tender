"""离线 Snapshot Replay：不访问网站，重新运行字段解析和状态计算。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from sqlalchemy import select

from tender_ai.config_loader import load_region_catalog
from tender_ai.documents.parser import ParsedDocument, parse_path
from tender_ai.extractors.runner import ExtractionSummary, _save_extraction, _upsert_document_parse
from tender_ai.extractors.tender import ExtractionResult, normalize_detail
from tender_ai.sources.contracts import DetailPayload
from tender_ai.sources.registry import SourceDefinition, SourceRegistry
from tender_ai.status.time import now_shanghai
from tender_ai.status.engine import recalculate_status, with_manual_evidence
from tender_ai.storage.database import create_engine_for, initialize_database, session_scope
from tender_ai.storage.models import Announcement, Evidence, ManualOverride, Project, Snapshot
from tender_ai.storage.repository import add_status_history, project_to_record
from tender_ai.versioning import STATUS_RULE_VERSION


@dataclass
class ReplaySummary:
    snapshot_count: int = 0
    replayed_count: int = 0
    failed_count: int = 0
    dry_run: bool = False
    errors: list[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_count": self.snapshot_count,
            "replayed_count": self.replayed_count,
            "failed_count": self.failed_count,
            "dry_run": self.dry_run,
            "errors": self.errors or [],
        }


def _payload_from_snapshot(snapshot: Snapshot) -> tuple[DetailPayload, ParsedDocument]:
    document = parse_path(
        snapshot.file_path,
        source_url=snapshot.source_url,
        content_type=snapshot.content_type,
        expected_terms=("报名", "获取", "投标", "开标", "截止"),
    )
    content = Path(snapshot.file_path).read_bytes()
    content_type = snapshot.content_type.lower()
    if "json" in content_type:
        try:
            parsed = json.loads(content.decode("utf-8", errors="replace"))
            title = str(parsed.get("title") or parsed.get("projectname") or snapshot.source_url) if isinstance(parsed, dict) else snapshot.source_url
            return DetailPayload(title=title, url=snapshot.source_url, text=document.text, metadata=parsed if isinstance(parsed, dict) else {}), document
        except json.JSONDecodeError:
            pass
    html = content.decode("utf-8", errors="replace") if "html" in content_type else ""
    soup = BeautifulSoup(html, "lxml") if html else None
    title = (soup.select_one("h1") or soup.select_one("title")) if soup else None
    title_text = title.get_text(" ", strip=True) if title else snapshot.source_url
    if not title_text and document.text:
        title_text = document.text.splitlines()[0][:500]
    return DetailPayload(title=title_text, url=snapshot.source_url, html=html, text=document.text), document


class ReplayRunner:
    def __init__(self, *, database: str | None = None):
        self.engine = initialize_database(create_engine_for(database))

    @staticmethod
    def _recalculate_project(session: Any, project: Project) -> None:
        evidence_rows = list(session.scalars(select(Evidence).where(Evidence.project_id == project.project_id)).all())
        overrides = list(session.scalars(select(ManualOverride).where(ManualOverride.project_id == project.project_id, ManualOverride.active.is_(True))).all())
        gate_evidence = with_manual_evidence(evidence_rows, overrides, source_url=project.source_url)
        decision = recalculate_status(project_to_record(project), now_shanghai(), evidences=gate_evidence, require_evidence=True)
        old_status = project.status
        project.status = decision.status.value
        project.tender_status = project.status
        project.status_reason = decision.reason_code
        project.status_evaluated_at = now_shanghai()
        project.status_rule_version = STATUS_RULE_VERSION
        project.updated_at = now_shanghai()
        if old_status != project.status:
            add_status_history(session, project.project_id, old_status, project.status, decision.reason)

    def run(
        self,
        *,
        announcement_id: int | None = None,
        source_id: str | None = None,
        dry_run: bool = False,
        reextract: bool = True,
        recalculate_status: bool = True,
        rules_only: bool = True,
    ) -> ReplaySummary:
        registry = SourceRegistry.from_file()
        regions = load_region_catalog()
        summary = ReplaySummary(dry_run=dry_run, errors=[])
        # 当前主路径没有外部模型；参数用于让 Codex 明确选择“只跑规则”的
        # 离线模式，并保证 Replay 永远不访问网络。
        _ = rules_only
        with session_scope(self.engine) as session:
            query = select(Snapshot).order_by(Snapshot.captured_at)
            if announcement_id is not None:
                query = query.where(Snapshot.announcement_id == announcement_id)
            if source_id:
                query = query.where(Snapshot.source_id == source_id)
            snapshots = list(session.scalars(query).all())
            summary.snapshot_count = len(snapshots)
            for snapshot in snapshots:
                try:
                    announcement = session.get(Announcement, snapshot.announcement_id) if snapshot.announcement_id else None
                    if not reextract:
                        if not dry_run and recalculate_status and announcement is not None:
                            project = session.get(Project, announcement.project_id)
                            if project is not None:
                                self._recalculate_project(session, project)
                        summary.replayed_count += 1
                        continue
                    payload, document = _payload_from_snapshot(snapshot)
                    if snapshot.source_id:
                        definition = registry.get(snapshot.source_id)
                    else:
                        definition = SourceDefinition(source_id="replay", source_name="Snapshot Replay", category="discovered", base_url=payload.url)
                    extraction = normalize_detail(
                        payload,
                        definition,
                        regions,
                        document=document,
                        source_file=document.source_file,
                        parser=document.parser,
                    )
                    if not dry_run:
                        if announcement is not None:
                            extraction.record.project_id = announcement.project_id
                            extraction.record.source_url = announcement.source_url or payload.url
                            extraction.record.original_url = announcement.original_url or payload.url
                            extraction.record.canonical_url = announcement.canonical_url
                            extraction.record.content_hash = snapshot.sha256
                            announcement.clean_text = document.text or payload.html
                            announcement.raw_content = (payload.html or document.text)[:2_000_000]
                            announcement.content_hash = snapshot.sha256
                            announcement.created_at = announcement.created_at or now_shanghai()
                        _upsert_document_parse(
                            session,
                            announcement_id=snapshot.announcement_id,
                            attachment_id=None,
                            document=document,
                            content_hash_value=snapshot.sha256,
                            project_id=announcement.project_id if announcement is not None else extraction.record.project_id,
                            source_id=snapshot.source_id,
                        )
                        summary_row = ExtractionSummary()
                        _save_extraction(
                            session,
                            announcement,
                            extraction,
                            summary_row,
                            source_id=snapshot.source_id,
                            dry_run=False,
                        ) if announcement is not None else None
                        if announcement is None:
                            from tender_ai.storage.repository import save_tender_record

                            save_tender_record(session, extraction.record, status_reason=extraction.record.status_reason, change_type="change")
                    summary.replayed_count += 1
                except Exception as exc:
                    summary.failed_count += 1
                    summary.errors.append(f"{snapshot.snapshot_id}: {exc}")
        return summary


__all__ = ["ReplayRunner", "ReplaySummary"]
