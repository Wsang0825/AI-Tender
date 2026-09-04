"""确定性招标状态引擎；LLM 不参与最终状态判定。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Iterable, Mapping

from tender_ai.status.time import as_shanghai, now_shanghai


class TenderStatus(str, Enum):
    OPEN = "OPEN"
    UNKNOWN = "UNKNOWN"
    CLOSED = "CLOSED"


# These are the facts that may change the answer to "can a new bidder still
# enter the process?".  A value without reliable, traceable evidence must not
# be allowed to force OPEN or CLOSED in the production path.
STATUS_CRITICAL_FIELDS = (
    "qualification_deadline",
    "registration_deadline",
    "document_deadline",
    "bid_deadline",
    "open_time",
    "extension_deadline",
)
STATUS_TIME_FIELDS = STATUS_CRITICAL_FIELDS + (
    "qualification_start",
    "registration_start",
    "document_start",
)
STRONG_EVIDENCE_TYPES = {
    "DIRECT_STRUCTURED",
    "DIRECT_TEXT",
    "DIRECT_TABLE",
    "DIRECT_DOCUMENT",
    "CODEX_REVIEW_CONFIRMED",
    "SOURCE_FIELD",
    "API_FIELD",
    "JSON_FIELD",
    "HTML_SELECTOR",
    "REGEX",
    "TABLE",
    "RULE",
    "MANUAL",
    "CODEX_REVIEW",
}
WEAK_EVIDENCE_TYPES = {"INFERRED", "SECONDARY_ONLY", "NO_EVIDENCE"}


@dataclass(frozen=True)
class StatusDecision:
    status: TenderStatus
    reason: str
    expired_fields: tuple[str, ...] = ()
    active_fields: tuple[str, ...] = ()
    reason_code: str = "UNKNOWN_NO_PARTICIPATION_DEADLINE"
    evidence_missing_fields: tuple[str, ...] = ()
    evidence_weak_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvidenceGateResult:
    """Evidence quality for status-critical facts.

    The gate accepts old rows created by the earlier rule extractor when they
    contain a real source URL and source text, but never treats an inferred or
    secondary-only value as a hard fact.
    """

    reliable_fields: tuple[str, ...] = ()
    weak_fields: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return bool(self.weak_fields or self.missing_fields)


def evidence_strength(evidence: Any) -> str:
    """Return ``STRONG``/``WEAK``/``NONE`` without depending on ORM classes."""

    def read(name: str, default: Any = None) -> Any:
        if isinstance(evidence, Mapping):
            return evidence.get(name, default)
        return getattr(evidence, name, default)

    raw_type = str(read("extractor_type") or read("extractor") or "").strip().upper()
    if raw_type in WEAK_EVIDENCE_TYPES or "SECONDARY" in raw_type or "INFERRED" in raw_type:
        return "WEAK"
    if raw_type in STRONG_EVIDENCE_TYPES or any(
        token in raw_type for token in ("API", "JSON", "SELECTOR", "REGEX", "TABLE", "DOCUMENT", "PDF", "DOCX", "XLSX", "CODEX", "MANUAL")
    ):
        return "STRONG"
    # Legacy evidence often used ``rule.public_notice`` or left
    # extractor_type empty.  A non-empty URL and minimal context make it a
    # direct text observation; an orphaned row remains unusable.
    if read("source_text") and (read("source_url") or read("source_file") or read("snapshot_id") or read("document_id")):
        return "STRONG"
    return "NONE"


def evidence_gate(record: object, evidences: Iterable[Any] | None) -> EvidenceGateResult:
    """Check evidence for every populated status-relevant time field."""

    by_field: dict[str, list[Any]] = {}
    for item in evidences or ():
        field_name = str(item.get("field_name") if isinstance(item, Mapping) else getattr(item, "field_name", ""))
        if field_name:
            by_field.setdefault(field_name, []).append(item)
    reliable: list[str] = []
    weak: list[str] = []
    missing: list[str] = []
    for field_name in STATUS_TIME_FIELDS:
        value = getattr(record, field_name, None)
        if value is None:
            continue
        rows = by_field.get(field_name, [])
        strengths = {evidence_strength(row) for row in rows}
        if "STRONG" in strengths:
            reliable.append(field_name)
        elif "WEAK" in strengths:
            weak.append(field_name)
        else:
            missing.append(field_name)
    participation_text = str(getattr(record, "participation_method", None) or "")
    participation_markers = ("投标截止前", "开标前", "截止前下载", "自行下载")
    if participation_text and any(marker in participation_text for marker in participation_markers):
        rows = by_field.get("participation_method", [])
        if "STRONG" not in {evidence_strength(row) for row in rows}:
            missing.append("participation_method")
    return EvidenceGateResult(tuple(reliable), tuple(weak), tuple(missing))


def with_manual_evidence(
    evidences: Iterable[Any],
    overrides: Iterable[Any] = (),
    *,
    source_url: str | None = None,
) -> list[Any]:
    """Add an auditable strong marker for active manual field overrides."""

    rows = list(evidences)
    rows.extend(
        {
            "field_name": getattr(row, "field_name", None) or (row.get("field_name") if isinstance(row, Mapping) else None),
            "extractor_type": "MANUAL",
            "source_text": getattr(row, "reason", None) or (row.get("reason") if isinstance(row, Mapping) else None) or "人工修正",
            "source_url": source_url,
        }
        for row in overrides
    )
    return rows


def _window_is_active(start: datetime | None, deadline: datetime | None, now: datetime) -> bool:
    if deadline is None or deadline <= now:
        return False
    return start is None or start <= now


def recalculate_status(
    record: object,
    now: datetime | None = None,
    evidences: Iterable[Any] | None = None,
    *,
    require_evidence: bool = False,
    evidence: Iterable[Any] | None = None,
) -> StatusDecision:
    """按明确时间字段计算公开状态，并返回可审计的内部原因码。"""

    if evidences is None and evidence is not None:
        evidences = evidence
    gate = evidence_gate(record, evidences) if require_evidence or evidences is not None else EvidenceGateResult()
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
        return StatusDecision(
            TenderStatus.UNKNOWN,
            "时间字段互相冲突，无法可靠判断",
            reason_code="UNKNOWN_CONFLICTING_DATES",
            evidence_missing_fields=gate.missing_fields,
            evidence_weak_fields=gate.weak_fields,
        )
    if (require_evidence or evidences is not None) and gate.blocked:
        blocked_fields = tuple(dict.fromkeys((*gate.weak_fields, *gate.missing_fields)))
        return StatusDecision(
            TenderStatus.UNKNOWN,
            f"状态关键时间缺少可靠Evidence: {', '.join(blocked_fields)}",
            reason_code="UNKNOWN_NEEDS_CODEX_REVIEW",
            evidence_missing_fields=gate.missing_fields,
            evidence_weak_fields=gate.weak_fields,
        )

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
            evidence_missing_fields=gate.missing_fields,
            evidence_weak_fields=gate.weak_fields,
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
            evidence_missing_fields=gate.missing_fields,
            evidence_weak_fields=gate.weak_fields,
        )
    participation_text = str(getattr(record, "participation_method", None) or "")
    if bid_active and opening_not_started and any(marker in participation_text for marker in ("投标截止前", "开标前", "截止前下载", "自行下载")):
        return StatusDecision(
            TenderStatus.OPEN,
            "公告明确允许在投标/开标前参与或下载文件",
            active_fields=("participation",),
            reason_code="OPEN_PARTICIPATION_ACTIVE",
            evidence_missing_fields=gate.missing_fields,
            evidence_weak_fields=gate.weak_fields,
        )
    has_participation_window = any(
        getattr(record, field, None) is not None
        for field in ("qualification_deadline", "registration_deadline", "document_deadline")
    )
    if not bid_active and has_participation_window:
        return StatusDecision(TenderStatus.UNKNOWN, "未提供仍可参与所需的有效投标截止时间", reason_code="UNKNOWN_NO_BID_DEADLINE", evidence_missing_fields=gate.missing_fields, evidence_weak_fields=gate.weak_fields)
    if bid_active and not has_participation_window and not any(marker in participation_text for marker in ("投标截止前", "开标前", "截止前下载", "自行下载")):
        return StatusDecision(TenderStatus.UNKNOWN, "没有明确的新参与或文件获取截止信息", reason_code="UNKNOWN_NO_PARTICIPATION_DEADLINE", evidence_missing_fields=gate.missing_fields, evidence_weak_fields=gate.weak_fields)
    if any(getattr(record, field, None) is not None for field in expiry_fields):
        return StatusDecision(TenderStatus.UNKNOWN, "来源缺少当前可参与窗口的完整信息", reason_code="UNKNOWN_SOURCE_INCOMPLETE", evidence_missing_fields=gate.missing_fields, evidence_weak_fields=gate.weak_fields)
    return StatusDecision(TenderStatus.UNKNOWN, "没有足够的当前可参与窗口信息", reason_code="UNKNOWN_NO_PARTICIPATION_DEADLINE", evidence_missing_fields=gate.missing_fields, evidence_weak_fields=gate.weak_fields)


__all__ = [
    "EvidenceGateResult", "STATUS_CRITICAL_FIELDS", "STATUS_TIME_FIELDS",
    "STRONG_EVIDENCE_TYPES", "WEAK_EVIDENCE_TYPES", "StatusDecision",
    "TenderStatus", "evidence_gate", "evidence_strength", "recalculate_status", "with_manual_evidence",
]
