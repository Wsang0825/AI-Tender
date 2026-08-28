"""确定性招标状态引擎；LLM 不参与最终状态判定。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from tender_ai.status.time import as_shanghai, now_shanghai


class TenderStatus(str, Enum):
    OPEN = "OPEN"
    UNKNOWN = "UNKNOWN"
    CLOSED = "CLOSED"


@dataclass(frozen=True)
class StatusDecision:
    status: TenderStatus
    reason: str
    expired_fields: tuple[str, ...] = ()
    active_fields: tuple[str, ...] = ()
    reason_code: str = "UNKNOWN_NO_DEADLINE"


def _window_is_active(start: datetime | None, deadline: datetime | None, now: datetime) -> bool:
    if deadline is None or deadline <= now:
        return False
    return start is None or start <= now


def recalculate_status(record: object, now: datetime | None = None) -> StatusDecision:
    """按明确时间字段计算公开状态，并返回可审计的内部原因码。"""

    current = as_shanghai(now) if now else now_shanghai()
    expiry_fields = (
        "qualification_deadline",
        "registration_deadline",
        "document_deadline",
        "bid_deadline",
        "open_time",
    )
    conflicting_pairs = (
        ("qualification_start", "qualification_deadline"),
        ("registration_start", "registration_deadline"),
        ("document_start", "document_deadline"),
    )
    conflicts = tuple(
        f"{start}/{deadline}"
        for start, deadline in conflicting_pairs
        if getattr(record, start, None) is not None
        and getattr(record, deadline, None) is not None
        and as_shanghai(getattr(record, start)) > as_shanghai(getattr(record, deadline))
    )
    if conflicts:
        return StatusDecision(TenderStatus.UNKNOWN, "时间字段互相冲突，无法可靠判断", reason_code="UNKNOWN_CONFLICTING_DATES")

    expired = tuple(
        field
        for field in expiry_fields
        if getattr(record, field, None) is not None and as_shanghai(getattr(record, field)) <= current
    )
    if expired:
        reason_map = {
            "qualification_deadline": "CLOSED_QUALIFICATION_EXPIRED",
            "registration_deadline": "CLOSED_REGISTRATION_EXPIRED",
            "document_deadline": "CLOSED_DOCUMENT_DEADLINE_EXPIRED",
            "bid_deadline": "CLOSED_BID_DEADLINE_EXPIRED",
            "open_time": "CLOSED_OPENED",
        }
        first = expired[0]
        return StatusDecision(
            TenderStatus.CLOSED,
            f"已超过显式截止时间: {', '.join(expired)}",
            expired_fields=expired,
            reason_code=reason_map[first],
        )

    windows = (
        ("registration", getattr(record, "registration_start", None), getattr(record, "registration_deadline", None)),
        ("document", getattr(record, "document_start", None), getattr(record, "document_deadline", None)),
    )
    active = tuple(name for name, start, deadline in windows if _window_is_active(start, deadline, current))
    if active:
        reason_map = {
            "registration": "OPEN_REGISTRATION_ACTIVE",
            "document": "OPEN_DOCUMENT_DOWNLOAD_ACTIVE",
        }
        return StatusDecision(
            TenderStatus.OPEN,
            f"当前处于{', '.join(active)}窗口",
            active_fields=active,
            reason_code=reason_map[active[0]],
        )
    if any(getattr(record, field, None) is not None for field in expiry_fields):
        return StatusDecision(TenderStatus.UNKNOWN, "来源缺少当前可参与窗口的完整信息", reason_code="UNKNOWN_SOURCE_INCOMPLETE")
    return StatusDecision(TenderStatus.UNKNOWN, "没有足够的当前可参与窗口信息", reason_code="UNKNOWN_NO_DEADLINE")


__all__ = ["StatusDecision", "TenderStatus", "recalculate_status"]
