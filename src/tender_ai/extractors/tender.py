"""面向公开公告的确定性字段抽取。

抽取结果只来自公告正文、列表 JSON 或页面元数据；规则无法确认的字段保持空值。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from html import unescape
from typing import Any

from bs4 import BeautifulSoup

from tender_ai.config_loader import RegionRegistry
from tender_ai.evidence.models import EvidenceRecord
from tender_ai.models import TenderRecord
from tender_ai.sources.contracts import DetailPayload
from tender_ai.sources.registry import SourceDefinition
from tender_ai.status.engine import recalculate_status
from tender_ai.status.time import now_shanghai, parse_datetime
from tender_ai.urls import canonicalize_url, content_hash


_DATE_RE = re.compile(
    r"\d{4}\s*[年/-]\s*\d{1,2}\s*[月/-]\s*\d{1,2}\s*日?(?:\s*(?:上午|下午|早上|晚上|PM|pm|AM|am)\s*)?(?:\d{1,2}\s*(?:[:：点时]\s*\d{1,2}\s*分?)?)?"
)
_CAPACITY_RE = re.compile(r"(?<![\d.])(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>GW|GWp|MW|MWp|MWh|kW|kWh|兆瓦时|兆瓦|千瓦时|千瓦)", re.I)
_MONEY_RE = re.compile(r"(?P<value>\d[\d,，]*(?:\.\d+)?)\s*(?P<unit>亿元|万元|万?元)")


@dataclass(frozen=True)
class ExtractionResult:
    record: TenderRecord
    evidences: tuple[EvidenceRecord, ...]


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = unescape(str(value))
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \t\r\n:：;；，,。")


def _metadata(payload: DetailPayload) -> dict[str, Any]:
    values = dict(payload.metadata or {})
    values.setdefault("title", payload.title)
    values.setdefault("url", payload.url)
    return values


def _body_text(payload: DetailPayload) -> str:
    if payload.text:
        return _clean(payload.text)
    if payload.html:
        return _clean(BeautifulSoup(payload.html, "lxml").get_text(" ", strip=True))
    return ""


def _first_value(metadata: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = metadata.get(name)
        if value not in (None, ""):
            return _clean(value)
    return None


def _label_value(text: str, labels: tuple[str, ...], *, limit: int = 180) -> tuple[str | None, str | None]:
    label = "|".join(re.escape(item) for item in labels)
    match = re.search(rf"(?:{label})\s*[：:、]?\s*", text, re.I)
    if not match:
        return None, None
    tail = text[match.end(): match.end() + limit]
    # 正文常把多个字段连续排在同一行；在下一个明确标签处截断。
    tail = re.split(r"(?=(?:采购单位|采购人|招标人|招标代理机构|代理机构名称|项目编号|招标编号|预算金额|行政区域|公告时间|项目名称)\s*[：:])", tail, maxsplit=1, flags=re.I)[0]
    value = _clean(tail)
    return (value or None), (value or None)


def _date_values(text: str) -> list[datetime]:
    result: list[datetime] = []
    for match in _DATE_RE.finditer(text):
        try:
            value = parse_datetime(match.group(0))
        except (TypeError, ValueError):
            value = None
        if value and value not in result:
            result.append(value)
    return result


def _field_dates(text: str, labels: tuple[str, ...]) -> tuple[datetime | None, datetime | None, str | None]:
    label = "|".join(re.escape(item) for item in labels)
    match = re.search(rf"(?:{label})\s*[：:、]?\s*", text, re.I)
    if not match:
        return None, None, None
    window = text[match.start(): match.end() + 220]
    values = _date_values(window)
    if not values:
        return None, None, None
    raw = _clean(window)
    return values[0], values[-1], raw


def _money(value: str | None) -> Decimal | None:
    if not value:
        return None
    match = _MONEY_RE.search(value.replace(" ", ""))
    if not match:
        try:
            return Decimal(value.replace(",", "").replace("，", ""))
        except Exception:
            return None
    number = Decimal(match.group("value").replace(",", "").replace("，", ""))
    unit = match.group("unit")
    if unit == "亿元":
        number *= 100_000_000
    elif unit in {"万元", "万"}:
        number *= 10_000
    return number


def _capacity(text: str) -> tuple[float | None, float | None, str | None]:
    mw: float | None = None
    mwh: float | None = None
    matches: list[str] = []
    for match in _CAPACITY_RE.finditer(text):
        value = float(match.group("value"))
        unit = match.group("unit").lower()
        matches.append(match.group(0))
        if "mwh" in unit or "兆瓦时" in unit or "kwh" in unit or "千瓦时" in unit:
            mwh = max(mwh or 0.0, value / 1000 if unit in {"kwh", "千瓦时"} else value)
        elif unit in {"gw", "gwp"}:
            mw = max(mw or 0.0, value * 1000)
        elif unit in {"kw", "千瓦"}:
            mw = max(mw or 0.0, value / 1000)
        else:
            mw = max(mw or 0.0, value)
    return mw, mwh, "、".join(matches) or None


def _announcement_type(title: str) -> str:
    for marker, value in (
        ("延期", "延期公告"),
        ("变更", "变更公告"),
        ("更正", "更正公告"),
        ("澄清", "澄清公告"),
        ("补充", "补充公告"),
        ("中标候选人", "中标候选人公示"),
        ("中标", "中标公告"),
        ("成交", "成交公告"),
        ("资格预审", "资格预审公告"),
        ("询比", "询比公告"),
        ("询价", "询价公告"),
        ("竞争性磋商", "竞争性磋商公告"),
        ("竞争性谈判", "竞争性谈判公告"),
        ("采购公告", "采购公告"),
        ("招标公告", "招标公告"),
    ):
        if marker in title:
            return value
    return "公告"


def _project_type(text: str) -> str | None:
    for marker in ("EPC", "工程总承包", "施工", "设备采购", "物资采购", "监理", "设计", "运维", "服务"):
        if marker.lower() in text.lower():
            return marker
    return None


def _stable_project_id(code: str | None, name: str, url: str) -> str:
    identity = f"code:{code}" if code else f"name:{re.sub(r'\\s+', '', name).lower()}"
    if not code:
        identity += f"|url:{url}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def _source_level(definition: SourceDefinition) -> str:
    if definition.category in {"national", "regional"}:
        return "A"
    if definition.category == "enterprise":
        return "B"
    return "D"


def normalize_detail(payload: DetailPayload, definition: SourceDefinition, regions: RegionRegistry) -> ExtractionResult:
    metadata = _metadata(payload)
    text = _body_text(payload)
    combined = _clean(" ".join(str(value) for value in [payload.title, text, *metadata.values()] if value not in (None, "")))
    title = _clean(_first_value(metadata, "customtitle", "title", "projectname") or payload.title) or "未命名公告"

    project_name = _first_value(metadata, "projectname", "project_name", "工程名称", "项目名称")
    if not project_name:
        project_name, _ = _label_value(text, ("项目名称", "工程名称", "采购项目名称", "招标项目名称"))
    project_name = project_name or title
    owner = _first_value(metadata, "owner", "tenderer", "招标人", "建设单位", "采购单位", "建设单位名称")
    if not owner:
        owner, _ = _label_value(text, ("招标人", "采购单位", "采购人", "建设单位", "项目法人"))
    agency = _first_value(metadata, "agency", "agent", "招标代理机构", "代理机构名称")
    if not agency:
        agency, _ = _label_value(text, ("招标代理机构", "代理机构名称", "采购代理机构"))
    purchaser = _first_value(metadata, "purchaser", "采购人")
    if not purchaser:
        purchaser, _ = _label_value(text, ("采购人",))
    code = _first_value(metadata, "projectnum", "project_code", "projectcode", "tender_code", "tendercode")
    if not code:
        code, _ = _label_value(text, ("项目编号", "招标编号", "采购编号", "项目代码", "标段编号"), limit=100)
    location = _first_value(metadata, "xiaquname", "region", "行政区域", "项目区域", "location")
    region_match = regions.match(_clean(" ".join(item for item in [location, title, text[:4000]] if item)))

    published_raw = _first_value(metadata, "webdate", "infodate", "noticeTime", "published_at", "publish_time")
    try:
        published_at = parse_datetime(published_raw) if published_raw else None
    except (TypeError, ValueError):
        published_at = None
    budget_raw = _first_value(metadata, "budget", "budgetamount", "预算金额", "最高限价")
    if not budget_raw:
        budget_raw, _ = _label_value(text, ("预算金额", "采购预算", "最高限价", "项目预算"), limit=100)
    budget = _money(budget_raw)
    capacity_mw, capacity_mwh, scale_match = _capacity(combined)

    q_start, q_deadline, q_raw = _field_dates(text, ("资格预审申请文件递交截止时间", "资格预审报名时间", "资格预审时间"))
    d_start, d_deadline, d_raw = _field_dates(text, ("获取采购文件时间", "获取招标文件时间", "获取文件时间", "文件获取时间", "报名时间"))
    b_start, b_deadline, b_raw = _field_dates(text, ("投标文件递交截止时间", "投标截止时间", "响应文件递交截止时间", "提交投标文件截止时间", "递交响应文件截止时间"))
    _, open_time, o_raw = _field_dates(text, ("开标时间", "响应文件开启时间", "开标日期"))

    if not b_deadline:
        raw_deadline = _first_value(metadata, "bid_deadline", "projectstatus")
        try:
            b_deadline = parse_datetime(raw_deadline) if raw_deadline else None
        except (TypeError, ValueError):
            b_deadline = None
    record = TenderRecord(
        project_id=_stable_project_id(code, project_name, payload.url),
        project_name=project_name,
        province=region_match.province if region_match else definition.region,
        city=region_match.city if region_match else None,
        county=region_match.county if region_match else None,
        location=location or ("/".join(region_match.path) if region_match else definition.region),
        owner=owner,
        purchaser=purchaser,
        tenderer=owner,
        agency=agency,
        industry="新能源" if any(word in combined for word in ("新能源", "光伏", "风电", "储能")) else None,
        project_type=_project_type(combined),
        announcement_type=_announcement_type(title),
        project_scale=scale_match,
        capacity_mw=capacity_mw,
        capacity_mwh=capacity_mwh,
        budget=budget,
        project_code=code,
        tender_code=code,
        publish_time=published_at or payload.metadata.get("published_at"),
        qualification_start=q_start,
        qualification_deadline=q_deadline,
        document_start=d_start,
        document_deadline=d_deadline,
        bid_deadline=b_deadline,
        open_time=open_time,
        qualification_summary=_clean(text[:1500]) or None,
        participation_method="在线公开信息" if payload.url.startswith("http") else None,
        source_name=definition.source_name,
        source_type=definition.category,
        source_level=_source_level(definition),
        source_url=payload.url,
        original_url=payload.url,
        canonical_url=canonicalize_url(payload.url),
        content_hash=content_hash(payload.html or payload.text or payload.title),
        first_seen_at=now_shanghai(),
        last_seen_at=now_shanghai(),
        confidence_score=0.82 if text else 0.62,
    )
    decision = recalculate_status(record)
    record.status = decision.status
    record.status_reason = decision.reason_code
    record.status_evaluated_at = now_shanghai()

    evidence_values: list[tuple[str, Any, str | None, float]] = [
        ("project_name", record.project_name, project_name, 0.92),
        ("owner", record.owner, owner, 0.82),
        ("agency", record.agency, agency, 0.82),
        ("project_code", record.project_code, code, 0.9),
        ("budget", record.budget, budget_raw, 0.82),
        ("publish_time", record.publish_time, published_raw, 0.9),
        ("document_deadline", record.document_deadline, d_raw, 0.8),
        ("bid_deadline", record.bid_deadline, b_raw, 0.86),
        ("open_time", record.open_time, o_raw, 0.86),
        ("capacity_mw", record.capacity_mw, scale_match, 0.75),
    ]
    evidences: list[EvidenceRecord] = []
    for field_name, value, raw_hint, confidence in evidence_values:
        if value in (None, ""):
            continue
        normalized = value.isoformat() if isinstance(value, datetime) else str(value)
        raw = _clean(raw_hint) if raw_hint else normalized
        position = text.find(str(raw).strip()) if raw else -1
        source_text = text[max(0, position - 80): position + 240] if position >= 0 else text[:320]
        evidences.append(EvidenceRecord(field_name=field_name, normalized_value=normalized, raw_value=raw, source_url=payload.url, source_text=source_text, extractor="rule.public_notice", confidence=confidence))
    return ExtractionResult(record=record, evidences=tuple(evidences))


__all__ = ["ExtractionResult", "normalize_detail"]
