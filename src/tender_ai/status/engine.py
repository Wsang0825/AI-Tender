"""确定性招标状态引擎，AI 不参与最终状态判定。"""

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


def _window_is_active(start: datetime | None, deadline: datetime | None, now: datetime) -> bool:
    if deadline is None or deadline <= now:
        return False
    return start is None or start <= now


def recalculate_status(record: object, now: datetime | None = None) -> StatusDecision:
    """根据显式时间字段计算 OPEN/CLOSED/UNKNOWN。

    任何已过的资格、报名、文件获取、投标截止或开标时间都会使记录 CLOSED。
    只有当前确实处于报名或文件获取窗口，且没有过期字段时才是 OPEN。
    """

    current = as_shanghai(now) if now else now_shanghai()
    expiry_fields = (
        "qualification_deadline",
        "registration_deadline",
        "document_deadline",
        "bid_deadline",
        "open_time",
    )
    expired = tuple(field for field in expiry_fields if getattr(record, field, None) is not None and as_shanghai(getattr(record, field)) <= current)
    if expired:
        return StatusDecision(TenderStatus.CLOSED, f"已超过显式截止时间: {', '.join(expired)}", expired_fields=expired)

    windows = (
        ("registration", getattr(record, "registration_start", None), getattr(record, "registration_deadline", None)),
        ("document", getattr(record, "document_start", None), getattr(record, "document_deadline", None)),
    )
    active = tuple(name for name, start, deadline in windows if _window_is_active(start, deadline, current))
    if active:
        return StatusDecision(TenderStatus.OPEN, f"当前处于{', '.join(active)}窗口", active_fields=active)
    return StatusDecision(TenderStatus.UNKNOWN, "没有足够的当前可参与窗口信息")
