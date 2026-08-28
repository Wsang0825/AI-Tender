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
    reason_code: str = "UNKNOWN_NO_PARTICIPATION_DEADLINE"


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

    bid_deadline = getattr(record, "bid_deadline", None)
    open_time = getattr(record, "open_time", None)
    bid_active = bid_deadline is not None and as_shanghai(bid_deadline) > current
    opening_not_started = open_time is None or as_shanghai(open_time) > current
    windows = (
        ("registration", getattr(record, "registration_start", None), getattr(record, "registration_deadline", None)),
        ("document", getattr(record, "document_start", None), getattr(record, "document_deadline", None)),
    )
    active = tuple(name for name, start, deadline in windows if _window_is_active(start, deadline, current) and bid_active and opening_not_started)
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
    participation_text = str(getattr(record, "participation_method", None) or "")
    if bid_active and opening_not_started and any(marker in participation_text for marker in ("投标截止前", "开标前", "截止前下载", "自行下载")):
        return StatusDecision(
            TenderStatus.OPEN,
            "公告明确允许在投标/开标前参与或下载文件",
            active_fields=("participation",),
            reason_code="OPEN_PARTICIPATION_ACTIVE",
        )
    has_participation_window = any(
        getattr(record, field, None) is not None
        for field in ("qualification_deadline", "registration_deadline", "document_deadline")
    )
    if not bid_active and has_participation_window:
        return StatusDecision(TenderStatus.UNKNOWN, "未提供仍可参与所需的有效投标截止时间", reason_code="UNKNOWN_NO_BID_DEADLINE")
    if bid_active and not has_participation_window and not any(marker in participation_text for marker in ("投标截止前", "开标前", "截止前下载", "自行下载")):
        return StatusDecision(TenderStatus.UNKNOWN, "没有明确的新参与或文件获取截止信息", reason_code="UNKNOWN_NO_PARTICIPATION_DEADLINE")
    if any(getattr(record, field, None) is not None for field in expiry_fields):
        return StatusDecision(TenderStatus.UNKNOWN, "来源缺少当前可参与窗口的完整信息", reason_code="UNKNOWN_SOURCE_INCOMPLETE")
    return StatusDecision(TenderStatus.UNKNOWN, "没有足够的当前可参与窗口信息", reason_code="UNKNOWN_NO_PARTICIPATION_DEADLINE")


__all__ = ["StatusDecision", "TenderStatus", "recalculate_status"]
