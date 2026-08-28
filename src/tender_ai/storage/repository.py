"""统一领域记录与 SQLAlchemy 模型之间的持久化边界。"""

from __future__ import annotations

from datetime import datetime

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
    "lifecycle_state", "last_change_at",
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
        payload["status_reason"] = record.status_reason or status_reason or "UNKNOWN_NO_DEADLINE"
        payload["lifecycle_state"] = "NEW"
        payload["last_change_at"] = now_shanghai()
        project = Project(**payload)
        session.add(project)
        changed = True
    else:
        for field in DEADLINE_FIELDS:
            old_value = getattr(project, field)
            new_value = payload.get(field)
            if old_value != new_value:
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
            if key not in manual_fields:
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
    if not manual_fields or project.status_reason is None:
        project.status_reason = record.status_reason or status_reason or project.status_reason or "UNKNOWN_NO_DEADLINE"
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
    payload = {field: getattr(project, field) for field in PROJECT_FIELDS if hasattr(project, field)}
    payload["status"] = TenderStatus(project.status)
    return TenderRecord(**payload)


def save_evidence(session: Session, evidence: EvidenceRecord, *, project_id: str | None = None, announcement_id: int | None = None) -> Evidence:
    row = Evidence(
        project_id=project_id,
        announcement_id=announcement_id,
        field_name=evidence.field_name,
        normalized_value=evidence.normalized_value,
        raw_value=evidence.raw_value,
        source_url=evidence.source_url,
        source_file=evidence.source_file,
        page_number=evidence.page_number,
        source_text=evidence.source_text,
        extractor=evidence.extractor,
        confidence=evidence.confidence,
        captured_at=evidence.captured_at,
        content_hash=evidence.content_hash or "",
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


def save_manual_override(session: Session, project_id: str, field_name: str, new_value: str | None, *, reason: str | None = None) -> ManualOverride:
    project = session.get(Project, project_id)
    if project is None or not hasattr(project, field_name):
        raise KeyError(f"未知项目字段: {project_id}/{field_name}")
    old_value = getattr(project, field_name)
    row = ManualOverride(
        project_id=project_id,
        field_name=field_name,
        old_value=str(old_value) if old_value is not None else None,
        new_value=new_value,
        reason=reason,
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
    session.flush()


__all__ = [
    "CHANGE_TYPES", "DEADLINE_FIELDS", "PROJECT_FIELDS", "add_change_history", "add_status_history",
    "clear_manual_override", "project_to_record", "save_evidence", "save_manual_override", "save_tender_record",
]
