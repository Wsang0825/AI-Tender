"""Stable A-J result layering for full candidate recall reports."""

from __future__ import annotations

from typing import Any


def result_bucket(value: Any) -> str:
    """Classify a candidate without allowing status to erase it."""

    def read(name: str, default: Any = None) -> Any:
        if isinstance(value, dict):
            return value.get(name, default)
        return getattr(value, name, default)

    relevance = str(read("relevance_class", "POSSIBLE") or "POSSIBLE").upper()
    verification = str(read("verification_status", "DISCOVERY_LEAD") or "DISCOVERY_LEAD").upper()
    status = str(read("tender_status", None) or read("status", "UNKNOWN") or "UNKNOWN").upper()
    blocker = str(read("blocker", "") or "").upper()
    matched = read("matched_keywords", ()) or ()
    if isinstance(matched, str):
        matched = (matched,)
    if any(str(item).startswith("EXCLUDED:") for item in matched):
        return "I"
    if blocker in {"ACCESS_BLOCKED", "LOGIN_REQUIRED", "CAPTCHA", "HTTP_412", "HTTP_403", "HTTP_429"} or verification == "BLOCKED":
        return "J"
    announcement_type = str(read("announcement_type", "") or "").upper()
    title = str(read("title", "") or read("project_name", "") or "")
    if any(marker in f"{announcement_type} {title}" for marker in ("中标", "成交", "结果", "流标", "废标", "历史", "竣工", "完工")):
        return "E"
    if status == "CLOSED":
        return "H"
    if verification == "SECONDARY_ONLY":
        return "F"
    if verification == "DISCOVERY_LEAD" or verification == "UNVERIFIED":
        return "G"
    if relevance == "EMBEDDED":
        return "C"
    if relevance in {"STRUCTURAL_RELATED", "ADJACENT"}:
        return "D"
    if relevance == "PARENT_PROJECT" and verification in {"OFFICIAL_VERIFIED", "OFFICIAL_PARTIAL", "MULTI_SOURCE_CONFIRMED"}:
        return "C"
    if relevance in {"DIRECT", "POSSIBLE", "PARENT_PROJECT"} and verification in {"OFFICIAL_VERIFIED", "MULTI_SOURCE_CONFIRMED"} and status == "OPEN":
        return "A"
    if verification in {"OFFICIAL_VERIFIED", "OFFICIAL_PARTIAL", "MULTI_SOURCE_CONFIRMED"}:
        return "B"
    return "G"


LAYER_LABELS = {
    "A": "官方确认、当前可参与",
    "B": "官方确认、状态或时间待补充",
    "C": "EPC/施工总包/嵌入式机会",
    "D": "车棚、钢结构、加固等结构相关项目",
    "E": "中标/结果/历史项目",
    "F": "第三方线索、官方待追查",
    "G": "低置信Discovery线索",
    "H": "已关闭项目",
    "I": "已排除项目",
    "J": "未覆盖或被阻断来源",
}


def layer_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {key: 0 for key in LAYER_LABELS}
    for row in rows:
        key = result_bucket(row)
        counts[key] = counts.get(key, 0) + 1
    return counts


__all__ = ["LAYER_LABELS", "layer_counts", "result_bucket"]
