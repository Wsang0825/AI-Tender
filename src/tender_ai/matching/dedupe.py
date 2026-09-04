"""规则优先、RapidFuzz 辅助的项目去重。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from rapidfuzz.fuzz import token_set_ratio
from sqlalchemy import select
from sqlalchemy.orm import Session

from tender_ai.models import TenderRecord


class DedupeOutcome:
    EXACT_MATCH = "EXACT_MATCH"
    PROBABLE_MATCH = "PROBABLE_MATCH"
    NO_MATCH = "NO_MATCH"


@dataclass(frozen=True)
class DedupeResult:
    outcome: str
    score: float
    reason: str


def normalize_identity(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"[\s\-—_()（）【】\[\]，。,.;；:：/\\]+", "", value).lower()


def _same_nonempty(left: str | None, right: str | None) -> bool:
    return bool(left and right and normalize_identity(left) == normalize_identity(right))


def compare_records(left: TenderRecord, right: TenderRecord) -> DedupeResult:
    if _same_nonempty(left.tender_code, right.tender_code):
        return DedupeResult(DedupeOutcome.EXACT_MATCH, 100.0, "tender_code 完全一致")
    if _same_nonempty(left.project_code, right.project_code):
        return DedupeResult(DedupeOutcome.EXACT_MATCH, 99.0, "project_code 完全一致")

    name_score = token_set_ratio(normalize_identity(left.project_name), normalize_identity(right.project_name))
    owner_same = _same_nonempty(left.owner or left.purchaser, right.owner or right.purchaser)
    location_same = any(_same_nonempty(getattr(left, field), getattr(right, field)) for field in ("province", "city", "county"))
    capacity_same = left.capacity_mw is not None and right.capacity_mw is not None and abs(left.capacity_mw - right.capacity_mw) <= max(0.1, max(left.capacity_mw, right.capacity_mw) * 0.01)
    publish_same = isinstance(left.publish_time, datetime) and isinstance(right.publish_time, datetime) and abs((left.publish_time - right.publish_time).total_seconds()) <= 86400 * 3
    supporting_signals = sum((owner_same, location_same, capacity_same, publish_same))
    if name_score >= 85 and supporting_signals >= 1:
        return DedupeResult(DedupeOutcome.PROBABLE_MATCH, float(name_score), f"项目名称相似度 {name_score:.1f}，辅助信号 {supporting_signals} 项")
    return DedupeResult(DedupeOutcome.NO_MATCH, float(name_score), "没有达到确定或疑似重复阈值")


def find_project_match(session: Session, record: TenderRecord, *, exclude_project_id: str | None = None) -> tuple[str, DedupeResult] | None:
    """在跨来源项目中寻找最强匹配；没有达到阈值时返回 None。"""

    from tender_ai.storage.models import Project
    from tender_ai.storage.repository import project_to_record

    best: tuple[str, DedupeResult] | None = None
    for project in session.scalars(select(Project)).all():
        if project.project_id == exclude_project_id:
            continue
        outcome = compare_records(record, project_to_record(project))
        if outcome.outcome == DedupeOutcome.NO_MATCH:
            continue
        if best is None or (outcome.outcome == DedupeOutcome.EXACT_MATCH and best[1].outcome != DedupeOutcome.EXACT_MATCH) or outcome.score > best[1].score:
            best = (project.project_id, outcome)
    return best


def _move_project_rows(session: Session, model: Any, loser_id: str, winner_id: str) -> int:
    moved = 0
    for row in session.scalars(select(model).where(model.project_id == loser_id)).all():
        row.project_id = winner_id
        moved += 1
    return moved


def merge_projects(session: Session, winner_id: str, loser_id: str) -> bool:
    """将重复项目的证据、公告、附件、时间线和核验记录移到保留项目。"""

    from tender_ai.storage.models import (
        Announcement, Attachment, ChangeHistory, Evidence, ManualOverride, Project, ProjectSource,
        StatusHistory, TimeFieldMetadata, TimelineEvent, VerificationResult, VerificationTask,
        Candidate, SearchSessionProject,
    )

    if winner_id == loser_id:
        return False
    winner = session.get(Project, winner_id)
    loser = session.get(Project, loser_id)
    if winner is None or loser is None:
        return False
    for field_name in (
        "owner", "purchaser", "agency", "project_type", "project_scale", "capacity_mw", "capacity_mwh", "budget",
        "project_code", "tender_code", "registration_deadline", "document_deadline", "bid_deadline", "open_time",
        "qualification_summary", "participation_method",
    ):
        if getattr(winner, field_name, None) in (None, "") and getattr(loser, field_name, None) not in (None, ""):
            setattr(winner, field_name, getattr(loser, field_name))
    for model in (Announcement, Attachment, Evidence, StatusHistory, ChangeHistory, TimelineEvent, VerificationTask, VerificationResult, ManualOverride, SearchSessionProject):
        _move_project_rows(session, model, loser_id, winner_id)
    for row in session.scalars(select(Candidate).where(Candidate.project_id == loser_id)).all():
        row.project_id = winner_id
    for row in session.scalars(select(TimeFieldMetadata).where(TimeFieldMetadata.project_id == loser_id)).all():
        duplicate_time = session.scalar(
            select(TimeFieldMetadata).where(
                TimeFieldMetadata.project_id == winner_id,
                TimeFieldMetadata.field_name == row.field_name,
                TimeFieldMetadata.value == row.value,
            )
        )
        if duplicate_time is not None:
            session.delete(row)
        else:
            row.project_id = winner_id
    for row in session.scalars(select(ProjectSource).where(ProjectSource.project_id == loser_id)).all():
        existing = session.get(ProjectSource, {"project_id": winner_id, "source_id": row.source_id})
        if existing is None:
            row.project_id = winner_id
        else:
            if not existing.source_url and row.source_url:
                existing.source_url = row.source_url
            if not existing.content_hash and row.content_hash:
                existing.content_hash = row.content_hash
            session.delete(row)
    # 先把所有引用落库，再删除重复项目，避免 SQLite 外键检查看到旧的 project_id。
    session.flush()
    session.delete(loser)
    session.flush()
    return True


def consolidate_projects(session: Session) -> int:
    """按确定编号或高置信项目相似度合并重复项目，返回合并数。"""

    from tender_ai.storage.models import Project
    from tender_ai.storage.repository import project_to_record

    merged = 0
    projects = list(session.scalars(select(Project).order_by(Project.created_at, Project.project_id)).all())
    for index, left in enumerate(projects):
        if session.get(Project, left.project_id) is None:
            continue
        left_record = project_to_record(left)
        for right in projects[index + 1:]:
            if session.get(Project, right.project_id) is None:
                continue
            result = compare_records(left_record, project_to_record(right))
            # 仅有编号完全一致时自动合并。项目名相似只能进入 Codex Review，
            # 否则同名但不同标段/不同阶段的项目会被静默吞掉。
            if result.outcome != DedupeOutcome.EXACT_MATCH:
                continue
            if merge_projects(session, left.project_id, right.project_id):
                merged += 1
    return merged


__all__ = ["DedupeOutcome", "DedupeResult", "compare_records", "consolidate_projects", "find_project_match", "merge_projects", "normalize_identity"]
