"""Deterministic identity extraction for incomplete tender candidates."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from tender_ai.config_loader import RegionRegistry, load_region_catalog
from tender_ai.matching.dedupe import normalize_identity


_NOTICE_SUFFIXES = re.compile(
    r"(?:招标|采购|询比|资格预审|中标候选人|中标结果|结果|成交|流标|废标|更正|延期|澄清|补充|变更)(?:公告|公示|文件)?$"
)
_LEADING_DECORATION = re.compile(r"^[\s【\[]*(?:招标公告|采购公告|项目公告|转载|推荐|最新)\s*[】\]]?\s*")
_LEADING_NUMERIC_DECORATION = re.compile(
    r"^\s*(?:(?:人民币)?[\d,.]+\s*(?:亿元|万元|万|元(?:\s*/\s*(?:瓦|kW|kw|kWh|度))?)|[\d,.]+\s*(?:MWp?|MWh|千瓦|兆瓦))\s*"
)
_CODE_PATTERNS = (
    ("tender_code", re.compile(r"(?:招标编号|采购编号|项目招标编号|标段编号)\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9._/()\-]{2,100})", re.I)),
    ("project_code", re.compile(r"(?:项目编号|项目代码|工程编号|采购项目编号)\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9._/()\-]{2,100})", re.I)),
)
_ENTITY_PATTERNS = {
    "tenderer": re.compile(r"(?:招标人|采购人|建设单位|项目业主|招标单位)\s*[:：]?\s*([^\n。；;，,]{2,120})"),
    "owner": re.compile(r"(?:业主单位|项目单位)\s*[:：]?\s*([^\n。；;，,]{2,120})"),
    "agency": re.compile(r"(?:招标代理机构|采购代理机构|代理机构)\s*[:：]?\s*([^\n。；;，,]{2,120})"),
}
_PROJECT_LOCATION_PATTERN = re.compile(
    r"(?:项目所在地|项目位置|建设地点|项目地点|实施地点|工程地点|项目区域)\s*[:：]?\s*([^\n。；;]{2,160})"
)


@dataclass(frozen=True)
class IdentityResolution:
    canonical_project_name: str | None = None
    raw_project_name: str | None = None
    project_code: str | None = None
    tender_code: str | None = None
    tenderer: str | None = None
    owner: str | None = None
    agency: str | None = None
    project_location: str | None = None
    province: str | None = None
    city: str | None = None
    county: str | None = None
    identity_status: str = "AMBIGUOUS"
    confidence: float = 0.0
    unique_phrases: tuple[str, ...] = field(default_factory=tuple)
    reason: str | None = None
    query_seed: str | None = None


def _clean(value: Any, limit: int = 500) -> str:
    return " ".join(str(value or "").replace("\u3000", " ").split()).strip()[:limit]


def canonicalize_project_name(value: str | None) -> str | None:
    """Remove announcement/media decoration without inventing a name."""

    text = _clean(value)
    if not text:
        return None
    text = _LEADING_DECORATION.sub("", text)
    # A price/capacity prefix is metadata, not identity.  Repeat because a
    # headline can start with both budget and capacity.
    for _ in range(3):
        updated = _LEADING_NUMERIC_DECORATION.sub("", text)
        if updated == text:
            break
        text = updated
    # Media headlines frequently use a pipe-like separator after a price or
    # capacity prefix, for example ``3.31元/瓦丨陕西82.5MW...``.  The prefix
    # is not an identity and must not become the query seed.
    text = re.sub(r"^\s*[^丨|｜]{0,32}(?:元\s*/\s*(?:瓦|kW|kw|kWh|度))\s*[丨|｜]\s*", "", text, flags=re.I)
    text = re.sub(r"\s*[-|｜丨_]\s*(?:招标|采购|中标|结果|公告|公示).*$", "", text)
    text = _NOTICE_SUFFIXES.sub("", text).strip(" -|｜丨_")
    text = re.sub(r"^(?:关于|有关)\s*", "", text)
    # Keep the actual project phrase if a title ends with an announcement
    # label.  Never turn an all-number headline into a searchable identity.
    if not text or len(normalize_identity(text)) < 4 or re.fullmatch(r"[\d.,+\-_/() ]+", text):
        return None
    if re.match(r"^(?:[\d,.]+\s*(?:亿元|万元|MWp?|MWh))", text):
        return None
    return text[:500]


def _metadata_value(metadata: Mapping[str, Any], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = _clean(metadata.get(name))
        if value:
            return value
    return None


def _project_location_hint(raw_title: str, source_text: str, metadata: Mapping[str, Any]) -> str:
    """Prefer project-location evidence over agency/source addresses."""

    explicit = _metadata_value(metadata, ("project_location", "projectLocation", "location", "项目地点", "建设地点"))
    if explicit:
        return explicit
    match = _PROJECT_LOCATION_PATTERN.search(source_text)
    if match:
        return _clean(match.group(1), 160)
    # A title is the safest fallback.  The rest of an announcement often
    # contains the agency's address (for example Xi'an) and must not silently
    # replace a project located in another city (for example Weinan).
    return raw_title


def resolve_identity(
    title: str | None,
    text: str | None = None,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> IdentityResolution:
    metadata = metadata or {}
    raw_title = _clean(_metadata_value(metadata, ("project_name", "projectname", "name")) or title)
    source_text = f"{raw_title} {_clean(text, 4000)}"
    codes: dict[str, str] = {}
    for field_name, pattern in _CODE_PATTERNS:
        match = pattern.search(source_text)
        if match:
            codes[field_name] = _clean(match.group(1), 120)
    for field_name, names in {
        "project_code": ("project_code", "projectnum", "project_number"),
        "tender_code": ("tender_code", "tendercode", "message_no"),
    }.items():
        value = _metadata_value(metadata, names)
        if value:
            codes[field_name] = value

    canonical = canonicalize_project_name(raw_title)
    tenderer = _metadata_value(metadata, ("tenderer", "owner", "purchaser"))
    owner = _metadata_value(metadata, ("owner", "purchaser", "tenderer"))
    agency = _metadata_value(metadata, ("agency", "agent"))
    for field_name, pattern in _ENTITY_PATTERNS.items():
        match = pattern.search(source_text)
        if not match:
            continue
        value = _clean(match.group(1), 180)
        if field_name == "owner" and not owner:
            owner = value
        elif field_name == "tenderer" and not tenderer:
            tenderer = value
        elif field_name == "agency" and not agency:
            agency = value

    location = None
    province = city = county = None
    try:
        match = load_region_catalog().match(_project_location_hint(raw_title, source_text, metadata))
    except Exception:
        match = None
    if match is None:
        # The national catalogue is intentionally conservative.  A maintained
        # legacy alias tree may contain a city/county not yet copied into it;
        # use that configured fallback, still based on the project title or
        # explicit project-location text rather than agency addresses.
        try:
            legacy_match = RegionRegistry.from_file().match(_project_location_hint(raw_title, source_text, metadata))
        except Exception:
            legacy_match = None
        if legacy_match is not None:
            province, city, county = legacy_match.province, legacy_match.city, legacy_match.county
            try:
                catalog = load_region_catalog()
                province_entry = next(
                    (
                        entry
                        for entry in catalog.entries
                        if entry.level in {"province", "special_region"}
                        and any(normalize_identity(label) == normalize_identity(province) for label in (entry.name, entry.short_name, *entry.aliases))
                    ),
                    None,
                )
                if province_entry is not None:
                    province = province_entry.name
            except Exception:
                pass
            if city and not city.endswith(("市", "州", "盟", "地区", "示范区")):
                city = f"{city}市"
            location = " / ".join(item for item in (province, city, county) if item)
    if match is not None:
        province, city, county = match.province, match.city, match.county
        location = " / ".join(match.path)

    unique_phrases: list[str] = []
    for value in (codes.get("tender_code"), codes.get("project_code"), canonical, tenderer, agency):
        cleaned = _clean(value, 160)
        if cleaned and cleaned not in unique_phrases:
            unique_phrases.append(cleaned)
    if codes.get("tender_code") or codes.get("project_code"):
        status, confidence, reason = "RESOLVED", 1.0, None
    elif canonical and tenderer:
        status, confidence, reason = "RESOLVED", 0.85, None
    elif canonical and len(normalize_identity(canonical)) >= 8:
        status, confidence, reason = "PARTIAL", 0.65, "缺少招标人或可核验编号"
    else:
        status, confidence, reason = "AMBIGUOUS", 0.15, "项目身份不足，禁止生成无意义追踪查询"
    query_seed = canonical if status != "AMBIGUOUS" else None
    return IdentityResolution(
        canonical_project_name=canonical,
        raw_project_name=raw_title or None,
        project_code=codes.get("project_code"),
        tender_code=codes.get("tender_code"),
        tenderer=tenderer,
        owner=owner,
        agency=agency,
        project_location=location,
        province=province,
        city=city,
        county=county,
        identity_status=status,
        confidence=confidence,
        unique_phrases=tuple(unique_phrases),
        reason=reason,
        query_seed=query_seed,
    )


__all__ = ["IdentityResolution", "canonicalize_project_name", "resolve_identity"]
