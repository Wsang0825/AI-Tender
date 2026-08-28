"""统一领域记录与 SQLAlchemy 模型之间的持久化边界。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_ai.evidence.models import EvidenceRecord
from tender_ai.models import TenderRecord
from tender_ai.status.engine import TenderStatus
from tender_ai.status.time import now_shanghai
from tender_ai.storage.database import refresh_tender_fts
from tender_ai.storage.models import ChangeHistory, Evidence, ManualOverride, Project, StatusHistory


PROJECT_FIELDS = {
    "project_id", "project_name", "province", "city", "county", "location", "owner", "purchaser", "tenderer", "agency",
    "industry", "sub_industry", "project_type", "announcement_type", "project_scale", "capacity_mw", "capacity_mwh", "budget",
    "project_code", "tender_code", "publish_time", "qualification_start", "qualification_deadline", "registration_start",
    "registration_deadline", "document_start", "document_deadline", "bid_deadline", "open_time", "qualification_summary",
    "participation_method", "source_name", "source_type", "source_level", "source_url", "original_url", "canonical_url",
    "content_hash", "first_seen_at", "last_seen_at", "status", "status_reason", "status_evaluated_at", "confidence_score",
    "lifecycle_state", "last_change_at", "document_quality_score", "extraction_version", "extraction_method",
    "last_extracted_at", "verification_required", "verification_reason", "llm_extracted",
    "raw_project_name", "canonical_project_name", "field_confidence", "source_confidence",
    "project_match_confidence", "overall_confidence", "completeness_score", "needs_codex_review",
    "review_reason", "status_rule_version",
}
DEADLINE_FIELDS = {"qualification_deadline", "registration_deadline", "document_deadline", "bid_deadline", "open_time"}
CHANGE_TYPES = {"original", "extension", "clarification", "change", "correction", "supplement"}


def save_tender_record(session: Session, record: TenderRecord, *, status_reason: str | None = None, change_type: str = "change") -> Project:
    if change_type not in CHANGE_TYPES:
        raise ValueError(f"未知公告变更类型: {change_type}")
    payload = record.model_dump(mode="python")
    payload["status"] = record.status.value
    payload = {key: value for key, value in payload.items() if key in PROJECT_FIELDS}
    project = session.get(Project, record.project_id)
    old_status = project.status if project else None
    old_payload = {field: getattr(project, field, None) for field in PROJECT_FIELDS} if project else {}
    manual_fields: set[str] = set()
    if project is not None:
        manual_fields = set(
            session.scalars(
                select(ManualOverride.field_name).where(
                    ManualOverride.project_id == project.project_id,
                    ManualOverride.active.is_(True),
                )
            ).all()
        )
    if project is None:
        payload["status_reason"] = record.status_reason or status_reason or "UNKNOWN_NO_PARTICIPATION_DEADLINE"
        payload["lifecycle_state"] = "NEW"
        payload["last_change_at"] = now_shanghai()
        project = Project(**payload)
        session.add(project)
        changed = True
    else:
        for field in DEADLINE_FIELDS:
            old_value = getattr(project, field)
            new_value = payload.get(field)
            if field not in manual_fields and old_value != new_value:
                session.add(
                    ChangeHistory(
                        project_id=project.project_id,
                        change_type=change_type,
                        field_name=field,
                        old_value=str(old_value) if old_value is not None else None,
                        new_value=str(new_value) if new_value is not None else None,
                        source_url=record.source_url,
                    )
                )
        for key, value in payload.items():
            if key in manual_fields:
                for override in session.scalars(
                    select(ManualOverride).where(
                        ManualOverride.project_id == project.project_id,
                        ManualOverride.field_name == key,
                        ManualOverride.active.is_(True),
                    )
                ).all():
                    override.automatic_value = _serialize_value(value)
                continue
            setattr(project, key, value)
        changed = any(
            old_payload.get(key) != getattr(project, key, None)
            for key in PROJECT_FIELDS
            if key not in {"lifecycle_state", "last_change_at", "last_seen_at", "status_evaluated_at"}
        )
        if old_status == "CLOSED" and project.status == "OPEN":
            project.lifecycle_state = "REOPENED"
        elif changed:
            project.lifecycle_state = "UPDATED"
        else:
            project.lifecycle_state = "UNCHANGED"
        if changed:
            project.last_change_at = now_shanghai()
        project.updated_at = now_shanghai()
    if "status" not in manual_fields:
        project.status_reason = record.status_reason or status_reason or project.status_reason or "UNKNOWN_NO_PARTICIPATION_DEADLINE"
    project.status_evaluated_at = record.status_evaluated_at or now_shanghai()
    session.flush()
    if old_status != project.status:
        session.add(
            StatusHistory(
                project_id=project.project_id,
                old_status=old_status,
                new_status=project.status,
                reason=status_reason or project.status_reason or "记录状态更新",
            )
        )
    refresh_tender_fts(session, project)
    return project


def project_to_record(project: Project) -> TenderRecord:
    record_fields = set(TenderRecord.model_fields)
    payload = {field: getattr(project, field) for field in PROJECT_FIELDS if field in record_fields and hasattr(project, field)}
    payload["status"] = TenderStatus(project.status)
    return TenderRecord(**payload)


def save_evidence(session: Session, evidence: EvidenceRecord, *, project_id: str | None = None, announcement_id: int | None = None) -> Evidence:
    content_hash = evidence.content_hash or ""
    existing_query = select(Evidence).where(
        Evidence.project_id == project_id,
        Evidence.announcement_id == announcement_id,
        Evidence.field_name == evidence.field_name,
        Evidence.source_url == evidence.source_url,
        Evidence.source_text == evidence.source_text,
    )
    if evidence.normalized_value is None:
        existing_query = existing_query.where(Evidence.normalized_value.is_(None))
    else:
        existing_query = existing_query.where(Evidence.normalized_value == evidence.normalized_value)
    existing = session.scalar(existing_query.order_by(Evidence.id))
    if existing is not None:
        if existing.page_number is None and evidence.page_number is not None:
            existing.page_number = evidence.page_number
        if not existing.source_file and evidence.source_file:
            existing.source_file = evidence.source_file
        if not existing.snapshot_id and evidence.snapshot_id:
            existing.snapshot_id = evidence.snapshot_id
        if not existing.document_id and evidence.document_id:
            existing.document_id = evidence.document_id
        if not existing.sheet_name and evidence.sheet_name:
            existing.sheet_name = evidence.sheet_name
        if not existing.cell_range and evidence.cell_range:
            existing.cell_range = evidence.cell_range
        return existing
    row = Evidence(
        project_id=project_id,
        announcement_id=announcement_id,
        field_name=evidence.field_name,
        normalized_value=evidence.normalized_value,
        raw_value=evidence.raw_value,
        source_url=evidence.source_url,
        source_file=evidence.source_file,
        snapshot_id=evidence.snapshot_id,
        document_id=evidence.document_id,
        page_number=evidence.page_number,
        sheet_name=evidence.sheet_name,
        cell_range=evidence.cell_range,
        source_text=evidence.source_text,
        extractor=evidence.extractor,
        extractor_type=evidence.extractor_type or evidence.extractor.split(".", 1)[0],
        extractor_version=evidence.extractor_version,
        confidence=evidence.confidence,
        captured_at=evidence.captured_at,
        content_hash=content_hash,
    )
    session.add(row)
    session.flush()
    return row


def add_status_history(session: Session, project_id: str, old_status: TenderStatus | str | None, new_status: TenderStatus | str, reason: str, changed_at: datetime | None = None) -> StatusHistory:
    row = StatusHistory(
        project_id=project_id,
        old_status=old_status.value if isinstance(old_status, TenderStatus) else old_status,
        new_status=new_status.value if isinstance(new_status, TenderStatus) else new_status,
        reason=reason,
        changed_at=changed_at or now_shanghai(),
    )
    session.add(row)
    session.flush()
    return row


def add_change_history(session: Session, project_id: str, change_type: str, field_name: str, old_value: str | None, new_value: str | None, *, source_url: str | None = None, announcement_id: int | None = None, updated_at: datetime | None = None) -> ChangeHistory:
    if change_type not in CHANGE_TYPES:
        raise ValueError(f"未知公告变更类型: {change_type}")
    row = ChangeHistory(
        project_id=project_id,
        announcement_id=announcement_id,
        change_type=change_type,
        field_name=field_name,
        old_value=old_value,
        new_value=new_value,
        source_url=source_url,
        updated_at=updated_at or now_shanghai(),
    )
    session.add(row)
    session.flush()
    return row


def _serialize_value(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _coerce_field_value(field_name: str, value: str | None) -> object:
    if value in (None, ""):
        return None
    if field_name in DEADLINE_FIELDS or field_name in {"publish_time", "qualification_start", "registration_start", "document_start"}:
        from tender_ai.status.time import parse_datetime

        return parse_datetime(value)
    if field_name in {"capacity_mw", "capacity_mwh"}:
        return float(value)
    if field_name == "budget":
        return Decimal(str(value).replace(",", ""))
    return value


def save_manual_override(session: Session, project_id: str, field_name: str, new_value: object, *, reason: str | None = None, changed_by: str = "USER") -> ManualOverride:
    project = session.get(Project, project_id)
    if project is None or not hasattr(project, field_name):
        raise KeyError(f"未知项目字段: {project_id}/{field_name}")
    old_value = getattr(project, field_name)
    active_rows = session.scalars(
        select(ManualOverride).where(
            ManualOverride.project_id == project_id,
            ManualOverride.field_name == field_name,
            ManualOverride.active.is_(True),
        )
    ).all()
    automatic_value = next((row.automatic_value for row in reversed(active_rows) if row.automatic_value is not None), None)
    if automatic_value is None:
        automatic_value = _serialize_value(old_value)
    for active_row in active_rows:
        active_row.active = False
    manual_value = _serialize_value(new_value)
    row = ManualOverride(
        project_id=project_id,
        field_name=field_name,
        old_value=automatic_value,
        new_value=manual_value,
        automatic_value=automatic_value,
        manual_value=manual_value,
        reason=reason,
        changed_by=changed_by,
    )
    session.add(row)
    setattr(project, field_name, new_value)
    project.updated_at = now_shanghai()
    session.flush()
    return row


def clear_manual_override(session: Session, project_id: str, field_name: str) -> None:
    rows = session.scalars(
        select(ManualOverride).where(
            ManualOverride.project_id == project_id,
            ManualOverride.field_name == field_name,
            ManualOverride.active.is_(True),
        )
    ).all()
    for row in rows:
        row.active = False
        project = session.get(Project, row.project_id)
        if project is not None:
            setattr(project, row.field_name, _coerce_field_value(row.field_name, row.automatic_value or row.old_value))
            project.updated_at = now_shanghai()
    session.flush()


def remove_manual_override(session: Session, project_id: str, field_name: str) -> None:
    """兼容业务层命名：取消人工修正并恢复最近一次自动值。"""

    clear_manual_override(session, project_id, field_name)


__all__ = [
    "CHANGE_TYPES", "DEADLINE_FIELDS", "PROJECT_FIELDS", "add_change_history", "add_status_history",
    "clear_manual_override", "remove_manual_override", "project_to_record", "save_evidence", "save_manual_override", "save_tender_record",
]
