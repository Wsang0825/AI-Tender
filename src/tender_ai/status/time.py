"""面向招投标截止时间的确定性时间解析。"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

try:
    import dateparser
except ImportError:  # pragma: no cover - 项目依赖已声明，保留清晰的降级错误
    dateparser = None


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def now_shanghai() -> datetime:
    return datetime.now(SHANGHAI_TZ)


def as_shanghai(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=SHANGHAI_TZ)
    return value.astimezone(SHANGHAI_TZ)


def _parse_clock(text: str) -> tuple[int | None, int, int, bool]:
    match = re.search(r"(?P<hour>\d{1,2})(?:点|时|:)(?:(?P<minute>\d{1,2})(?:分)?)?", text)
    if not match:
        return None, 0, 0, False
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    if "半" in text[match.start(): match.end() + 1]:
        minute = 30
    afternoon = bool(re.search(r"下午|晚上|傍晚|PM|pm", text))
    morning = bool(re.search(r"上午|早上|AM|am", text))
    if afternoon and hour < 12:
        hour += 12
    if morning and hour == 12:
        hour = 0
    return hour, minute, 0, hour == 24


def parse_datetime(value: Any, *, default_hour: int = 17) -> datetime | None:
    """解析常见中英文日期，结果始终带 Asia/Shanghai 时区。

    支持 ISO 日期、中文年月日、上午/下午、17点、17:00 和 24:00。
    日期只有年月日时，按业务默认截止时刻 default_hour 处理。
    """

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return as_shanghai(value)
    if isinstance(value, date):
        return datetime.combine(value, time(default_hour), tzinfo=SHANGHAI_TZ)
    text = str(value).strip()
    if not text:
        return None
    text = re.sub(r"星期[一二三四五六日天]", "", text)
    text = re.sub(r"周[一二三四五六日天]", "", text)
    # 带 T 和时区偏移的 ISO 时间必须先按时区换算，不能被中文日期分支截断。
    if "T" in text or re.search(r"[+-]\d{2}:?\d{2}$", text):
        try:
            iso_parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            iso_parsed = None
        if iso_parsed is not None:
            return as_shanghai(iso_parsed)
    date_match = re.search(r"(?P<year>\d{4})\s*[年/-]\s*(?P<month>\d{1,2})\s*[月/-]\s*(?P<day>\d{1,2})\s*日?", text)
    if date_match:
        parsed_date = date(int(date_match.group("year")), int(date_match.group("month")), int(date_match.group("day")))
        hour, minute, second, is_24 = _parse_clock(text[date_match.end():])
        if hour is None:
            hour, minute, second, is_24 = default_hour, 0, 0, False
        if is_24:
            return datetime.combine(parsed_date + timedelta(days=1), time(0), tzinfo=SHANGHAI_TZ)
        if hour > 23 or minute > 59:
            raise ValueError(f"时间超出范围: {value}")
        return datetime.combine(parsed_date, time(hour, minute, second), tzinfo=SHANGHAI_TZ)

    iso_text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso_text)
    except ValueError:
        parsed = None
    if parsed is not None:
        return as_shanghai(parsed)

    if dateparser is None:
        raise ValueError(f"无法解析日期: {value}")
    parsed = dateparser.parse(text, languages=["zh", "en"], settings={"RETURN_AS_TIMEZONE_AWARE": True, "TIMEZONE": "Asia/Shanghai", "DATE_ORDER": "YMD"})
    if parsed is None:
        raise ValueError(f"无法解析日期: {value}")
    return as_shanghai(parsed)
