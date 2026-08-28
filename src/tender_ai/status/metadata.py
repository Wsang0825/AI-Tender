"""关键时间字段的精度、时区和推断标记。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tender_ai.status.time import as_shanghai


@dataclass(frozen=True)
class TimeMetadata:
    field_name: str
    value: datetime
    timezone: str = "Asia/Shanghai"
    precision: str = "UNKNOWN"
    explicit_or_inferred: str = "EXPLICIT"
    inference_rule: str | None = None
    raw_value: str | None = None
    source_evidence_id: int | None = None


_CLOCK_RE = re.compile(r"\d{1,2}\s*(?::|：|点|时)|上午|下午|晚上|AM|PM", re.I)
_RANGE_RE = re.compile(r"(?<!截)至|到|~|～|(?<=\s)-(?=\s)", re.I)


def describe_time(field_name: str, value: Any, raw_value: Any = None, *, source_evidence_id: int | None = None) -> TimeMetadata | None:
    if value is None:
        return None
    normalized = as_shanghai(value)
    raw = str(raw_value).strip() if raw_value not in (None, "") else ""
    if _RANGE_RE.search(raw):
        precision = "RANGE"
    elif _CLOCK_RE.search(raw):
        precision = "DATETIME"
    else:
        precision = "DATE_ONLY"
    inferred = precision == "DATE_ONLY"
    return TimeMetadata(
        field_name=field_name,
        value=normalized,
        precision=precision,
        explicit_or_inferred="INFERRED" if inferred else "EXPLICIT",
        inference_rule="date_only_default_deadline_17:00_AsiaShanghai" if inferred else None,
        raw_value=raw_value,
        source_evidence_id=source_evidence_id,
    )


__all__ = ["TimeMetadata", "describe_time"]
