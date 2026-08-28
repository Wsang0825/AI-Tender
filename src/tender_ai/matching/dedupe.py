"""规则优先、RapidFuzz 辅助的项目去重。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from rapidfuzz.fuzz import token_set_ratio

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
