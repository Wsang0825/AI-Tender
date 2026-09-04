"""Deterministic Codex Review queue and compact offline hand-off files.

The Python process never calls an AI service. It only identifies records that
need a human/Codex reading pass and exposes the evidence needed for that pass.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from sqlalchemy import select

from tender_ai.config_loader import APP_ROOT
from tender_ai.status.time import as_shanghai, now_shanghai
from tender_ai.status.engine import STATUS_CRITICAL_FIELDS, evidence_strength
from tender_ai.storage.models import (
    Announcement,
    Attachment,
    CodexReviewItem,
    DocumentParse,
    Evidence,
    FieldConflict,
    Project,
    SearchSession,
    Snapshot,
)


REVIEW_SCHEMA_VERSION = "codex_review_v1"
REVIEW_STATUS = {"PENDING", "RESOLVED", "SKIPPED"}
REVIEW_FIELDS = (
    "project_name", "province", "city", "county", "location", "owner", "purchaser", "tenderer", "agency",
    "project_type", "industry", "capacity_mw", "capacity_mwh", "budget", "project_code", "tender_code",
    "publish_time", "qualification_deadline", "registration_deadline", "document_deadline", "bid_deadline",
    "open_time", "participation_method", "status", "status_reason", "source_level",
)
REQUIRED_PARTICIPATION_FIELDS = ("qualification_deadline", "registration_deadline", "document_deadline")


def _value(value: Any) -> Any:
    if isinstance(value, datetime):
        return as_shanghai(value).isoformat()
    return value


def _project_values(project: Project) -> dict[str, Any]:
    return {field_name: _value(getattr(project, field_name, None)) for field_name in REVIEW_FIELDS}


def _document_rows(session: Any, project: Project, announcement: Announcement | None) -> list[DocumentParse]:
    statement = select(DocumentParse).where(DocumentParse.project_id == project.project_id)
    rows = list(session.scalars(statement).all())
    if announcement is not None:
        rows.extend(session.scalars(select(DocumentParse).where(DocumentParse.announcement_id == announcement.id)).all())
    unique: dict[int, DocumentParse] = {row.id: row for row in rows}
    return list(unique.values())


def review_reasons(
    project: Project,
    *,
    document_rows: Iterable[DocumentParse] = (),
    conflict_rows: Iterable[FieldConflict] = (),
    evidence_rows: Iterable[Evidence] = (),
    now: datetime | None = None,
) -> tuple[str, ...]:
    reasons: list[str] = []
    conflicts = list(conflict_rows)
    documents = list(document_rows)
    evidence = list(evidence_rows)
    if any(row.field_name == "project_identity" for row in conflicts):
        reasons.append("PROBABLE_MATCH")
    if any(row.field_name != "project_identity" for row in conflicts) or project.status_reason == "UNKNOWN_CONFLICTING_DATES":
        reasons.append("CONFLICTING_DATE")
    if any(row.parse_status != "SUCCESS" for row in documents):
        reasons.append("UNKNOWN_DOCUMENT_PARSE_FAILED")
    if any(row.content_type.lower().endswith("pdf") and row.quality_score < 45 for row in documents):
        reasons.append("PDF_PARSE_LOW_QUALITY")
    if project.status == "UNKNOWN":
        participation = any(getattr(project, field_name, None) for field_name in REQUIRED_PARTICIPATION_FIELDS)
        if not participation:
            reasons.append("SOURCE_INCOMPLETE")
        if getattr(project, "bid_deadline", None) and not any(
            getattr(project, field_name, None) for field_name in ("registration_deadline", "document_deadline")
        ):
            reasons.append("UNKNOWN_PARTICIPATION_RULE")
    critical_without_evidence = [
        field_name
        for field_name in STATUS_CRITICAL_FIELDS
        if getattr(project, field_name, None) is not None
        and not any(row.field_name == field_name and evidence_strength(row) == "STRONG" for row in evidence)
    ]
    if critical_without_evidence:
        reasons.append("MISSING_CRITICAL_EVIDENCE")
    if (project.source_level or "").upper() in {"D", "E"}:
        reasons.append("ONLY_SECONDARY_SOURCE")
    if project.status == "OPEN" and not any(
        getattr(project, field_name, None) for field_name in ("registration_deadline", "document_deadline")
    ):
        reasons.append("OPEN_PARTICIPATION_UNCLEAR")
    reference = as_shanghai(now) if now else now_shanghai()
    for field_name in ("qualification_deadline", "registration_deadline", "document_deadline", "bid_deadline", "open_time"):
        deadline = getattr(project, field_name, None)
        if deadline is not None and 0 <= (as_shanghai(deadline) - reference).total_seconds() <= 3 * 86400:
            reasons.append("DEADLINE_WITHIN_3_DAYS")
            break
    return tuple(dict.fromkeys(reasons))


def _priority(project: Project, reasons: tuple[str, ...]) -> int:
    if any(reason in reasons for reason in ("CONFLICTING_DATE", "UNKNOWN_DOCUMENT_PARSE_FAILED", "PDF_PARSE_LOW_QUALITY")):
        return 1
    if "DEADLINE_WITHIN_3_DAYS" in reasons:
        return 1
    if (project.capacity_mw or 0) >= 100 or (project.budget or 0) >= 10_000_000:
        return 2
    if project.status == "UNKNOWN":
        return 3
    if "ONLY_SECONDARY_SOURCE" in reasons:
        return 4
    return 5


def _paths(session: Any, project: Project, announcement: Announcement | None, documents: list[DocumentParse]) -> list[str]:
    values: list[str] = []
    if announcement is not None and announcement.snapshot_id:
        snapshot = session.get(Snapshot, announcement.snapshot_id)
        if snapshot is not None:
            values.append(snapshot.file_path)
    for row in documents:
        for path in (row.file_path, row.source_file, row.markdown_path, row.clean_text_path):
            if path:
                values.append(path)
    attachments = session.scalars(
        select(Attachment).where(
            Attachment.project_id == project.project_id,
            (Attachment.announcement_id == announcement.id) if announcement is not None else True,
        )
    ).all()
    for row in attachments:
        if row.local_path:
            values.append(row.local_path)
    return list(dict.fromkeys(values))


def _candidate_values(session: Any, project: Project, reasons: tuple[str, ...], evidence_ids: list[int]) -> dict[str, Any]:
    missing = [field_name for field_name in REQUIRED_PARTICIPATION_FIELDS + ("bid_deadline", "open_time") if not getattr(project, field_name, None)]
    conflicts = []
    for row in session.scalars(select(FieldConflict).where(FieldConflict.project_id == project.project_id, FieldConflict.resolution_status == "PENDING")).all():
        conflicts.append({
            "field_name": row.field_name,
            "candidate_values": json.loads(row.candidate_values_json or "[]"),
            "evidence_ids": json.loads(row.evidence_ids_json or "[]"),
        })
    return {
        "fields": _project_values(project),
        "missing_fields": list(dict.fromkeys(missing)),
        "review_reasons": list(reasons),
        "conflicts": conflicts,
        "evidence_ids": evidence_ids,
        "missing_evidence_fields": [
            field_name for field_name in STATUS_CRITICAL_FIELDS
            if getattr(project, field_name, None) is not None
            and not any(row.field_name == field_name and evidence_strength(row) == "STRONG" for row in session.scalars(select(Evidence).where(Evidence.project_id == project.project_id)).all())
        ],
    }


def ensure_review_item(
    session: Any,
    project: Project,
    *,
    announcement: Announcement | None = None,
    search_session_id: str | None = None,
) -> CodexReviewItem | None:
    documents = _document_rows(session, project, announcement)
    conflicts = list(session.scalars(select(FieldConflict).where(FieldConflict.project_id == project.project_id, FieldConflict.resolution_status == "PENDING")).all())
    evidence_query = select(Evidence).where(Evidence.project_id == project.project_id)
    if announcement is not None:
        evidence_query = evidence_query.where(Evidence.announcement_id == announcement.id)
    evidence_rows = list(session.scalars(evidence_query).all())
    reasons = review_reasons(project, document_rows=documents, conflict_rows=conflicts, evidence_rows=evidence_rows)
    if not reasons:
        project.needs_codex_review = False
        project.review_reason = None
        return None
    evidence_statement = select(Evidence.id).where(Evidence.project_id == project.project_id)
    if announcement is not None:
        evidence_statement = evidence_statement.where(Evidence.announcement_id == announcement.id)
    evidence_ids = [int(value) for value in session.scalars(evidence_statement).all()]
    snapshot = session.get(Snapshot, announcement.snapshot_id) if announcement is not None and announcement.snapshot_id else None
    content_hash = (snapshot.sha256 if snapshot is not None else None) or (announcement.content_hash if announcement is not None else None) or project.content_hash
    existing = session.scalar(
        select(CodexReviewItem)
        .where(
            CodexReviewItem.project_id == project.project_id,
            CodexReviewItem.announcement_id == (announcement.id if announcement is not None else None),
            CodexReviewItem.review_type == "CODEX_REVIEW",
        )
        .order_by(CodexReviewItem.created_at.desc())
    )
    values = _candidate_values(session, project, reasons, evidence_ids)
    document_paths = _paths(session, project, announcement, documents)
    if existing is None:
        existing = CodexReviewItem(
            review_id=uuid4().hex,
            project_id=project.project_id,
            announcement_id=announcement.id if announcement is not None else None,
            search_session_id=search_session_id,
            review_type="CODEX_REVIEW",
            reason=reasons[0],
            priority=_priority(project, reasons),
            source_url=(announcement.source_url if announcement is not None else None) or project.source_url,
            snapshot_id=snapshot.snapshot_id if snapshot is not None else None,
            document_paths=json.dumps(document_paths, ensure_ascii=False),
            candidate_values=json.dumps(values, ensure_ascii=False, default=str),
            evidence_ids=json.dumps(evidence_ids, ensure_ascii=False),
            content_hash=content_hash,
            review_schema_version=REVIEW_SCHEMA_VERSION,
            created_at=now_shanghai(),
            status="PENDING",
        )
        session.add(existing)
        session.flush()
    else:
        if existing.content_hash != content_hash:
            existing.status = "PENDING"
            existing.resolved_at = None
            existing.reviewed_at = None
            existing.resolution = "CONTENT_CHANGED_REQUIRES_REVIEW"
        if search_session_id:
            existing.search_session_id = search_session_id
        existing.reason = reasons[0]
        existing.priority = _priority(project, reasons)
        existing.source_url = (announcement.source_url if announcement is not None else None) or project.source_url
        existing.snapshot_id = snapshot.snapshot_id if snapshot is not None else None
        existing.document_paths = json.dumps(document_paths, ensure_ascii=False)
        existing.candidate_values = json.dumps(values, ensure_ascii=False, default=str)
        existing.evidence_ids = json.dumps(evidence_ids, ensure_ascii=False)
        existing.content_hash = content_hash
    project.needs_codex_review = existing.status not in {"RESOLVED", "SKIPPED"}
    project.review_reason = ",".join(reasons)
    return existing


def review_item_dict(session: Any, item: CodexReviewItem) -> dict[str, Any]:
    project = session.get(Project, item.project_id)
    announcement = session.get(Announcement, item.announcement_id) if item.announcement_id else None
    evidence_ids = json.loads(item.evidence_ids or "[]")
    evidence = []
    if evidence_ids:
        evidence_rows = session.scalars(select(Evidence).where(Evidence.id.in_(evidence_ids))).all()
        evidence = [
            {
                "evidence_id": row.id,
                "field_name": row.field_name,
                "raw_value": row.raw_value,
                "source_text": row.source_text,
                "source_url": row.source_url,
                "source_file": row.source_file,
                "page_number": row.page_number,
                "extractor": row.extractor,
                "confidence": row.confidence,
            }
            for row in evidence_rows
        ]
    values = json.loads(item.candidate_values or "{}")
    document_paths = json.loads(item.document_paths or "[]")
    snapshot = session.get(Snapshot, item.snapshot_id) if item.snapshot_id else None
    original_url = (announcement.original_url if announcement is not None else None) or item.source_url
    pdf_paths = [path for path in document_paths if Path(path).suffix.lower() == ".pdf"]
    return {
        "review_id": item.review_id,
        "project_id": item.project_id,
        "announcement_id": item.announcement_id,
        "search_session_id": item.search_session_id,
        "review_type": item.review_type,
        "reason": item.reason,
        "priority": item.priority,
        "status": item.status,
        "source_url": item.source_url,
        "snapshot_id": item.snapshot_id,
        "document_paths": document_paths,
        "pdf_paths": pdf_paths,
        "snapshot_path": snapshot.file_path if snapshot is not None else None,
        "original_url": original_url,
        "candidate_values": values,
        "evidence_ids": evidence_ids,
        "evidence": evidence,
        "content_hash": item.content_hash,
        "review_schema_version": item.review_schema_version,
        "created_at": _value(item.created_at),
        "resolved_at": _value(item.resolved_at),
        "reviewed_at": _value(item.reviewed_at),
        "resolution": item.resolution,
        "project": _project_values(project) if project is not None else {},
        "announcement_title": announcement.title if announcement is not None else None,
    }


def write_review_files(session: Any, session_id: str, *, project_ids: Iterable[str] | None = None) -> tuple[Path, Path, list[dict[str, Any]]]:
    statement = select(CodexReviewItem).where(CodexReviewItem.search_session_id == session_id).order_by(CodexReviewItem.priority, CodexReviewItem.created_at)
    if project_ids is not None:
        statement = statement.where(CodexReviewItem.project_id.in_(list(project_ids)))
    items = [review_item_dict(session, item) for item in session.scalars(statement).all()]
    search_session = session.get(SearchSession, session_id)
    output_dir = APP_ROOT.parent / "output" / "sessions" / session_id
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": session_id,
        "request": json.loads(search_session.request_json) if search_session is not None else {},
        "review_items": items,
    }
    json_path = output_dir / "codex_review.json"
    md_path = output_dir / "codex_review.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    lines = [f"# Codex Review - {session_id}", "", f"Review 数量：{len(items)}", ""]
    for item in items:
        fields = item["candidate_values"].get("fields", {})
        missing = item["candidate_values"].get("missing_fields", [])
        reasons = item["candidate_values"].get("review_reasons", [])
        lines.extend([
            f"## {fields.get('project_name') or item.get('announcement_title') or item['project_id']}",
            "",
            f"- review_id：{item['review_id']}",
            f"- project_id：{item['project_id']}",
            f"- announcement_id：{item.get('announcement_id') or ''}",
            f"- 地区：{fields.get('province') or ''} {fields.get('city') or ''} {fields.get('county') or ''}",
            f"- 当前状态：{fields.get('status') or ''} / {fields.get('status_reason') or ''}",
            f"- 来源等级：{fields.get('source_level') or ''}",
            f"- 原始URL：{item.get('original_url') or item.get('source_url') or ''}",
            f"- Snapshot：{item.get('snapshot_id') or ''}",
            f"- Snapshot路径：{item.get('snapshot_path') or ''}",
            f"- 本地文件：{'; '.join(item.get('document_paths') or [])}",
            f"- PDF路径：{'; '.join(item.get('pdf_paths') or []) or '无'}",
            f"- 缺失字段：{', '.join(missing) or '无'}",
            f"- Review原因：{', '.join(reasons) or item['reason']}",
            "- 当前字段：" + json.dumps(fields, ensure_ascii=False, default=str),
            "- 关键Evidence：",
        ])
        for evidence in item["evidence"][:12]:
            lines.append(
                f"  - [{evidence['field_name']}] {evidence.get('raw_value') or ''}；{evidence.get('source_url') or ''}；页码 {evidence.get('page_number') or ''}；{evidence.get('source_file') or ''}"
            )
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path, json_path, items


def resolve_review_item(session: Any, review_id: str, *, status: str, resolution: str) -> CodexReviewItem:
    if status not in REVIEW_STATUS:
        raise ValueError(f"invalid review status: {status}")
    item = session.get(CodexReviewItem, review_id)
    if item is None:
        raise KeyError(f"unknown review_id: {review_id}")
    item.status = status
    item.resolution = resolution
    item.resolved_at = now_shanghai() if status in {"RESOLVED", "SKIPPED"} else None
    item.reviewed_at = now_shanghai()
    project = session.get(Project, item.project_id)
    if project is not None:
        project.needs_codex_review = status == "PENDING"
        if status != "PENDING":
            project.review_reason = None
    session.flush()
    return item


__all__ = [
    "REVIEW_SCHEMA_VERSION",
    "REVIEW_STATUS",
    "ensure_review_item",
    "resolve_review_item",
    "review_item_dict",
    "review_reasons",
    "write_review_files",
]
