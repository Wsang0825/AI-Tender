"""按需搜索编排器。

这里不理解复杂业务语义，也不调用模型 API。Codex 负责把自然语言变成
``SearchRequest``；本模块负责调用已有 Crawl/Discovery/Extraction 管线，
重算状态，并输出适合 Codex 阅读的 Search Session 文件。
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse
from uuid import uuid4

from sqlalchemy import select

from tender_ai.candidates import CandidateStore, candidate_dict, classify_relevance
from tender_ai.config_loader import APP_ROOT, RegionRegistry, load_industry_profiles, load_region_catalog, load_search_profiles
from tender_ai.concepts import RELATION_TYPES, load_concept_catalog
from tender_ai.crawlers.runner import CrawlRunner
from tender_ai.discovery.runner import DiscoveryRunner
from tender_ai.enrichment import EnrichmentEngine
from tender_ai.extractors.runner import ExtractionRunner
from tender_ai.matching.dedupe import normalize_identity
from tender_ai.review import ensure_review_item, review_item_dict, write_review_files
from tender_ai.status.engine import evidence_strength, recalculate_status, with_manual_evidence
from tender_ai.status.time import as_shanghai, now_shanghai, parse_datetime
from tender_ai.result_layers import LAYER_LABELS, result_bucket
from tender_ai.quality_metrics import calculate_quality_metrics
from tender_ai.storage.database import create_engine_for, initialize_database, session_scope
from tender_ai.storage.models import Announcement, Candidate, CodexReviewItem, Evidence, ManualOverride, Project, ProjectSource, SearchSession, SearchSessionProject, Source
from tender_ai.storage.repository import add_status_history, project_to_record
from tender_ai.sources.registry import SourceDefinition, SourceRegistry, configured_manual_action, configured_manual_http_status
from tender_ai.sources.browser_profiles import DEFAULT_BROWSER, browser_profile_path


INDUSTRY_ALIASES: dict[str, tuple[str, ...]] = {
    "renewable_general": ("新能源", "可再生能源", "清洁能源"),
    "solar": ("光伏", "太阳能", "太阳能发电", "PV"),
    "storage": ("储能", "储能系统", "储能电站", "PCS", "EMS", "BMS"),
    "wind_power": ("风电", "风力发电", "风机"),
    "epc": ("EPC", "PC", "工程总承包", "施工"),
    "equipment": ("设备采购", "组件", "支架", "逆变器", "PCS", "EMS", "BMS", "变压器"),
    "grid": ("升压站", "变电站", "输变电", "并网"),
}
EQUIPMENT_TERMS = ("组件", "光伏支架", "固定支架", "跟踪支架", "逆变器", "PCS", "EMS", "BMS", "变压器", "升压站", "开关站")
PROJECT_TYPE_TERMS = ("EPC", "PC", "施工", "设备采购", "工程总承包", "监理", "设计", "运维")
SEARCH_MODES = {"exact", "broad", "opportunity"}
RESULT_MODES = {"FULL_RESULT", "DELTA_RESULT", "AUTO"}


def normalize_search_mode(value: str | None) -> str:
    """Normalize the public Search Intent CLI contract."""

    normalized = str(value or "opportunity").strip().lower().replace("-", "_")
    return normalized if normalized in SEARCH_MODES else "opportunity"


def normalize_result_mode(value: str | None) -> str:
    """Normalize the public ``full``/``delta`` CLI aliases.

    The database keeps the explicit internal names so old Search Sessions and
    the delivery layer have one stable contract.
    """

    normalized = str(value or "AUTO").strip().upper().replace("-", "_")
    return {
        "FULL": "FULL_RESULT",
        "FULL_RESULT": "FULL_RESULT",
        "DELTA": "DELTA_RESULT",
        "DELTA_RESULT": "DELTA_RESULT",
        "AUTO": "AUTO",
    }.get(normalized, "AUTO")


@dataclass(frozen=True)
class SearchRequest:
    request_id: str = field(default_factory=lambda: f"request_{uuid4().hex}")
    raw_query: str | None = None
    profile_id: str = "northwest_energy"
    search_mode: str = "opportunity"
    result_mode: str = "AUTO"
    concept_id: str | None = None
    region: str | None = None
    city: str | None = None
    county: str | None = None
    regions: tuple[str, ...] = ()
    region_codes: tuple[str, ...] = ()
    cities: tuple[str, ...] = ()
    counties: tuple[str, ...] = ()
    days: int = 30
    date_from: str | None = None
    date_to: str | None = None
    industries: tuple[str, ...] = ()
    project_types: tuple[str, ...] = ()
    equipment: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    exclude_keywords: tuple[str, ...] = ()
    source_level: str | None = None
    source_categories: tuple[str, ...] = ()
    announcement_types: tuple[str, ...] = ()
    include_unknown: bool = False
    only_open: bool = False
    discovery: bool = False
    wechat: bool = False
    deep: bool = False
    relation_types: tuple[str, ...] = ()
    max_enrichments: int | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SearchRequest":
        """从 SearchSession JSON 恢复请求，兼容第4步旧字段名。"""
        values = dict(payload or {})
        aliases = {
            "search_mode": ("search_mode", "mode", "intent"),
            "result_mode": ("result_mode", "output_mode"),
            "relation_types": ("relation_types", "relations", "relation"),
            "industries": ("industries", "industry", "industry_groups"),
            "project_types": ("project_types", "project_type"),
            "equipment": ("equipment", "equipment_types"),
            "keywords": ("keywords", "keyword", "include_keywords"),
            "exclude_keywords": ("exclude_keywords", "exclude_keyword"),
            "source_categories": ("source_categories", "source_category"),
            "announcement_types": ("announcement_types",),
        }
        for target, names in aliases.items():
            if target not in values:
                for name in names:
                    if name in values:
                        values[target] = values[name]
                        break
        list_fields = {
            "regions", "region_codes", "cities", "counties", "industries", "project_types", "equipment",
            "keywords", "exclude_keywords", "source_categories", "announcement_types", "relation_types",
        }
        for key in list_fields:
            value = values.get(key, ())
            if isinstance(value, str):
                value = (value,)
            values[key] = tuple(str(item) for item in (value or ()) if str(item).strip())
        values["request_id"] = str(values.get("request_id") or f"request_{uuid4().hex}")
        values["search_mode"] = normalize_search_mode(values.get("search_mode"))
        values["result_mode"] = normalize_result_mode(values.get("result_mode"))
        values["relation_types"] = tuple(value for value in values.get("relation_types", ()) if value in RELATION_TYPES)
        allowed = set(cls.__dataclass_fields__)
        return cls(**{key: value for key, value in values.items() if key in allowed})

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "raw_query": self.raw_query,
            "profile_id": self.profile_id,
            "search_mode": normalize_search_mode(self.search_mode),
            "result_mode": normalize_result_mode(self.result_mode),
            "concept_id": self.concept_id,
            "region": self.region,
            "city": self.city,
            "county": self.county,
            "regions": list(self.regions or ((self.region,) if self.region else ())),
            "region_codes": list(self.region_codes),
            "cities": list(self.cities or ((self.city,) if self.city else ())),
            "counties": list(self.counties or ((self.county,) if self.county else ())),
            "days": self.days,
            "date_from": self.date_from,
            "date_to": self.date_to,
            "industries": list(self.industries),
            "industry": list(self.industries),
            "project_type": list(self.project_types),
            "equipment": list(self.equipment),
            "equipment_types": list(self.equipment),
            "keyword": list(self.keywords),
            "keywords": list(self.keywords),
            "include_keywords": list(self.keywords),
            "exclude_keyword": list(self.exclude_keywords),
            "exclude_keywords": list(self.exclude_keywords),
            "source_level": self.source_level,
            "source_levels": [self.source_level] if self.source_level else [],
            "source_categories": list(self.source_categories),
            "announcement_types": list(self.announcement_types),
            "industry_groups": list(self.industries),
            "project_types": list(self.project_types),
            "include_unknown": self.include_unknown,
            "only_open": self.only_open,
            "discovery": self.discovery,
            "wechat": self.wechat,
            "deep": self.deep,
            "relation_types": list(self.relation_types),
            "relations": list(self.relation_types),
            "max_enrichments": self.max_enrichments,
        }


@dataclass
class SearchSummary:
    session_id: str
    request: dict[str, Any]
    dry_run: bool = False
    query_count: int = 0
    candidate_count: int = 0
    open_count: int = 0
    unknown_count: int = 0
    closed_count: int = 0
    review_count: int = 0
    errors: list[str] = field(default_factory=list)
    sources: list[dict[str, Any]] = field(default_factory=list)
    output_dir: str | None = None
    summary_path: str | None = None
    report_path: str | None = None
    results_path: str | None = None
    review_markdown_path: str | None = None
    review_json_path: str | None = None
    sources_planned: int = 0
    sources_completed: int = 0
    sources_failed: int = 0
    projects_found: int = 0
    verification_count: int = 0
    source_plan: list[dict[str, Any]] = field(default_factory=list)
    open_projects_path: str | None = None
    unknown_projects_path: str | None = None
    errors_path: str | None = None
    sources_path: str | None = None
    valuable_leads_path: str | None = None
    candidate_pool_path: str | None = None
    layers_path: str | None = None
    suppressed_unchanged_count: int = 0
    suppressed_project_ids: list[str] = field(default_factory=list)
    suppression_reasons: dict[str, str] = field(default_factory=dict)
    suppressed_valuable_lead_count: int = 0
    manual_action_sources: list[dict[str, Any]] = field(default_factory=list)
    official_trace_matches: list[dict[str, Any]] = field(default_factory=list)
    valuable_lead_count: int = 0
    valuable_leads: list[dict[str, Any]] = field(default_factory=list)
    search_mode: str = "opportunity"
    result_mode: str = "DELTA_RESULT"
    candidate_pool_count: int = 0
    new_candidate_count: int = 0
    updated_candidate_count: int = 0
    reopened_candidate_count: int = 0
    enrichment_count: int = 0
    layers: dict[str, int] = field(default_factory=dict)
    coverage_manifest: dict[str, Any] = field(default_factory=dict)
    quality_metrics: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _scope_values(payload: dict[str, Any], *names: str) -> list[str]:
    values: list[str] = []
    for name in names:
        value = payload.get(name)
        if isinstance(value, str):
            value = [value]
        values.extend(str(item) for item in (value or ()) if str(item).strip())
    return sorted({normalize_identity(item) for item in values if normalize_identity(item)})


def search_scope_key(request_or_payload: SearchRequest | dict[str, Any]) -> str:
    """生成用于 DELTA_RESULT 重复搜索抑制的内容范围指纹。

    只把检索内容范围纳入指纹；``deep``、Discovery、公众号和 OPEN/UNKNOWN
    过滤属于执行/展示策略。同一地区和关键词在 DELTA_RESULT 下会抑制
    已经报告且没有变化的项目，但新发现、状态变化和延期项目会正常出现；
    FULL_RESULT 不使用这个抑制结果。
    """

    payload = request_or_payload.to_dict() if isinstance(request_or_payload, SearchRequest) else dict(request_or_payload or {})
    scope = {
        "profile_id": str(payload.get("profile_id") or "northwest_energy"),
        "search_mode": str(payload.get("search_mode") or payload.get("mode") or "opportunity"),
        "concept_id": str(payload.get("concept_id") or ""),
        "relation_types": _scope_values(payload, "relation_types", "relations"),
        "regions": _scope_values(payload, "region", "regions"),
        "region_codes": _scope_values(payload, "region_codes"),
        "cities": _scope_values(payload, "city", "cities"),
        "counties": _scope_values(payload, "county", "counties"),
        "days": int(payload.get("days") or 30),
        "date_from": str(payload.get("date_from") or ""),
        "date_to": str(payload.get("date_to") or ""),
        "industries": _scope_values(payload, "industries", "industry", "industry_groups"),
        "project_types": _scope_values(payload, "project_types", "project_type"),
        "equipment": _scope_values(payload, "equipment", "equipment_types"),
        "keywords": _scope_values(payload, "keywords", "keyword", "include_keywords"),
        "exclude_keywords": _scope_values(payload, "exclude_keywords", "exclude_keyword"),
        "source_level": normalize_identity(str(payload.get("source_level") or "")),
        "source_categories": _scope_values(payload, "source_categories", "source_category"),
        "announcement_types": _scope_values(payload, "announcement_types"),
    }
    return json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _resolve_region(text: str) -> tuple[str | None, str | None, str | None]:
    if any(token in (text or "") for token in ("全国", "全网", "全国范围")):
        return "全国", None, None
    try:
        catalog = load_region_catalog()
        matches = []
        normalized = normalize_identity(text)
        for entry in catalog.entries:
            labels = (entry.name, entry.short_name, *entry.aliases)
            for label in labels:
                if label and normalize_identity(label) in normalized:
                    matches.append((len(normalize_identity(label)), entry))
        if matches:
            entry = max(matches, key=lambda item: item[0])[1]
            parent = catalog.get(entry.parent_code) if entry.parent_code else None
            if entry.level in {"city", "prefecture", "county", "district"} and parent is not None:
                return parent.name, entry.name, None if entry.level != "county" else entry.name
            return entry.name, None, None
    except Exception:
        pass
    try:
        match = RegionRegistry.from_file().match(text)
    except Exception:
        match = None
    if match is None:
        # An unrecognised region must not become the whole natural-language
        # query.  Codex may later provide an explicit structured region; the
        # local shortcut parser should leave the geographic dimension empty.
        return None, None, None
    return match.province, match.city, match.county


def _region_code_for_text(text: str | None) -> str | None:
    if not text:
        return None
    catalog = load_region_catalog()
    normalized = normalize_identity(text)
    candidates: list[tuple[int, str]] = []
    for entry in catalog.entries:
        for label in (entry.name, entry.short_name, *entry.aliases):
            label_normalized = normalize_identity(label)
            if label_normalized and (label_normalized == normalized or label_normalized in normalized):
                candidates.append((len(label_normalized), entry.region_code))
    return max(candidates)[1] if candidates else None


def parse_search_text(text: str, *, profile_id: str = "northwest_energy") -> SearchRequest:
    """解析常见中文快捷搜索语句；复杂语义由 Codex 翻译成结构化参数。"""

    raw = (text or "").strip()
    region, city, county = _resolve_region(raw)
    day_match = re.search(r"最近\s*(\d+)\s*(?:天|日)", raw)
    if day_match:
        days = max(1, int(day_match.group(1)))
    elif any(token in raw for token in ("一个月", "一月", "近一月", "最近一月")):
        days = 30
    elif any(token in raw for token in ("一周", "一星期", "7天")):
        days = 7
    else:
        days = 30

    configured_aliases = dict(INDUSTRY_ALIASES)
    try:
        configured_aliases.update({item.group_id: item.terms for item in load_industry_profiles().profiles})
    except Exception:
        pass
    industries: list[str] = []
    for group_id, aliases in configured_aliases.items():
        if any(alias.casefold() in raw.casefold() for alias in aliases):
            industries.append(group_id)
    project_types = [term for term in PROJECT_TYPE_TERMS if term.casefold() in raw.casefold()]
    equipment = [term for term in EQUIPMENT_TERMS if term.casefold() in raw.casefold()]
    known_terms = set(token for aliases in configured_aliases.values() for token in aliases) | set(PROJECT_TYPE_TERMS) | set(EQUIPMENT_TERMS)
    keyword_text = raw
    region_variants = [region or ""]
    if region:
        region_variants.extend((region.replace("自治区", ""), region.replace("省", ""), region.replace("市", "")))
    for token in (*known_terms, *region_variants, city or "", county or ""):
        if token:
            keyword_text = keyword_text.replace(token, " ")
    keyword_text = re.sub(r"最近\s*\d+\s*(?:天|日)|最近\s*(?:一个月|一月|一周|一星期)|只看[^，。；;]*", " ", keyword_text)
    explicit_keywords = [
        token
        for token in re.findall(r"[\u4e00-\u9fffA-Za-z0-9+]{2,20}", keyword_text)
        if token not in known_terms and token not in {region, city, county, "最近", "项目", "招标", "采购", "查看", "看看", "帮我", "找"}
    ]
    include_unknown = any(token in raw for token in ("未知", "不确定", "UNKNOWN"))
    only_open = any(token in raw for token in ("只看还能", "只看开放", "只看OPEN", "还能报名", "还能参与", "可参与", "当前可参与"))
    deep = any(token in raw for token in ("深度", "尽量找全", "不要漏", "全面查", "全网"))
    wechat = any(token in raw for token in ("公众号", "微信"))
    discovery = deep or any(token in raw for token in ("全网", "发现", "其他地方"))
    if any(token in raw for token in ("精确", "严格匹配", "exact")):
        search_mode = "exact"
    elif any(token in raw for token in ("广泛", "相关", "关联", "尽量找全", "不要漏", "全面", "所有")):
        search_mode = "broad"
    else:
        search_mode = "opportunity"
    relation_types: list[str] = []
    relation_markers = {
        "direct": ("直接采购", "直接招标", "支架采购"),
        "component": ("支架基础", "预埋件", "连接件", "檩条"),
        "embedded": ("EPC内含", "包内", "嵌入", "施工总承包"),
        "structural_related": ("结构相关", "加固", "改造", "车棚", "钢结构"),
        "parent_project": ("光伏项目", "光伏电站", "分布式光伏", "农光互补"),
        "contract_scope": ("EPC", "PC", "总承包", "专业分包"),
        "adjacent": ("相近结构", "箱变平台", "设备钢结构"),
    }
    for relation, markers in relation_markers.items():
        if any(marker.casefold() in raw.casefold() for marker in markers):
            relation_types.append(relation)
    result_mode = "FULL_RESULT" if any(token in raw for token in (
        "全部", "所有", "找全", "尽量找全", "不要漏", "全面查", "给我完整结果",
        "最近两周有哪些", "最近一个月有哪些", "最近N天有哪些", "有哪些",
    )) else "DELTA_RESULT" if any(token in raw for token in (
        "新增", "新的", "今天变化", "相比上次", "更新了什么", "最近新增", "新出现",
    )) else "AUTO"
    concept_id = "photovoltaic_support" if any(token in raw for token in ("光伏", "支架", "车棚", "预埋件", "光伏材料")) else None
    return SearchRequest(
        raw_query=raw,
        profile_id=profile_id,
        search_mode=search_mode,
        result_mode=result_mode,
        concept_id=concept_id,
        region=region,
        city=city,
        county=county,
        regions=(region,) if region else (),
        region_codes=tuple(code for code in (_region_code_for_text(region), _region_code_for_text(city), _region_code_for_text(county)) if code),
        cities=(city,) if city else (),
        counties=(county,) if county else (),
        days=days,
        industries=tuple(dict.fromkeys(industries)),
        project_types=tuple(dict.fromkeys(project_types)),
        equipment=tuple(dict.fromkeys(equipment)),
        keywords=tuple(dict.fromkeys(explicit_keywords)),
        include_unknown=include_unknown,
        only_open=only_open,
        discovery=discovery,
        wechat=wechat,
        deep=deep,
        relation_types=tuple(dict.fromkeys(relation_types)),
    )


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _terms(request: SearchRequest) -> tuple[str, ...]:
    profile = load_search_profiles().get(request.profile_id)
    group_ids = request.industries or profile.industry_groups
    terms: list[str] = []
    area = " ".join(item for item in (request.region, request.city, request.county) if item)
    concept_id = request.concept_id
    if concept_id:
        try:
            concept_queries = load_concept_catalog(concept_id=concept_id).expand_queries(
                group_ids=(),
                relation_types=request.relation_types,
                extra_terms=(*request.keywords, *request.equipment, *request.project_types),
                area=area or None,
                max_queries=20 if request.deep else 12,
            )
            terms.extend(item.text for item in concept_queries)
        except Exception:
            pass
    try:
        catalog = load_industry_profiles()
        for group_id in group_ids:
            try:
                terms.extend(catalog.get(group_id).terms[:6])
            except KeyError:
                terms.extend(INDUSTRY_ALIASES.get(group_id, (group_id,)))
    except Exception:
        for group_id in group_ids:
            terms.extend(INDUSTRY_ALIASES.get(group_id, (group_id,)))
    terms.extend(request.project_types)
    terms.extend(request.equipment)
    terms.extend(request.keywords)
    terms.extend(profile.include_keywords)
    if not terms:
        terms.extend(("新能源", "光伏", "储能"))
    excluded = set(request.exclude_keywords) | set(profile.exclude_keywords)
    terms = [term for term in dict.fromkeys(terms) if term and term not in excluded]
    if area:
        terms = [term if area.casefold() in term.casefold() else f"{area} {term}" for term in terms]
    return tuple(terms[: (20 if request.deep else 12)])


def resolve_result_mode(request: SearchRequest | dict[str, Any]) -> str:
    payload = request.to_dict() if isinstance(request, SearchRequest) else dict(request or {})
    configured = normalize_result_mode(payload.get("result_mode"))
    if configured in {"FULL_RESULT", "DELTA_RESULT"}:
        return configured
    raw = str(payload.get("raw_query") or "")
    if any(token in raw for token in ("全部", "所有", "找全", "尽量找全", "不要漏", "全面查", "给我完整结果", "最近两周有哪些", "最近一个月有哪些", "最近N天有哪些", "有哪些")):
        return "FULL_RESULT"
    if any(token in raw for token in ("新增", "新的", "今天变化", "相比上次", "更新了什么", "最近新增", "新出现")):
        return "DELTA_RESULT"
    # On-demand conversational searches default to delta delivery so a repeat
    # tomorrow does not consume the user's attention on unchanged records.
    return "DELTA_RESULT"


def resolve_search_mode(request: SearchRequest | dict[str, Any]) -> str:
    payload = request.to_dict() if isinstance(request, SearchRequest) else dict(request or {})
    mode = str(payload.get("search_mode") or payload.get("mode") or "opportunity").lower()
    return mode if mode in SEARCH_MODES else "opportunity"


def _label_in_text(label: str | None, text: str) -> bool:
    return bool(label) and normalize_identity(label) in normalize_identity(text)


def _request_region_text(request: SearchRequest) -> str:
    return " ".join(item for item in (request.region, request.city, request.county, *request.regions, *request.cities, *request.counties) if item)


def _source_region_matches(definition: SourceDefinition, request: SearchRequest, profile: Any) -> bool:
    """选择与本次请求有关的来源，不把无关省份来源带入计划。"""
    source_region = (definition.region or "全国").strip()
    requested_region = normalize_identity(_request_region_text(request))
    if requested_region in {"全国", "全国性", "全网"}:
        return True
    if normalize_identity(source_region) in {"全国", "全国性"}:
        return True
    requested_text = _request_region_text(request)
    if requested_text:
        if _label_in_text(source_region, requested_text):
            return True
        try:
            match = load_region_catalog().match(requested_text)
            if match and any(_label_in_text(source_region, value) for value in match.path):
                return True
        except Exception:
            pass
        return False
    if request.region_codes:
        try:
            catalog = load_region_catalog()
            labels: set[str] = set()
            for code in request.region_codes:
                entry = catalog.get(code)
                while entry is not None:
                    labels.update(normalize_identity(label) for label in (entry.name, entry.short_name, *entry.aliases) if label)
                    entry = catalog.get(entry.parent_code) if entry.parent_code else None
            return normalize_identity(source_region) in labels or any(normalize_identity(source_region) in label for label in labels)
        except Exception:
            return True
    try:
        selected = load_region_catalog().selected(profile.regions, profile.excluded_regions)
        return any(
            _label_in_text(source_region, label)
            for entry in selected
            for label in (entry.name, entry.short_name, *entry.aliases)
        )
    except Exception:
        return True


def build_source_plan(request: SearchRequest) -> list[dict[str, Any]]:
    """按地区、行业 Profile 和来源配置生成一次性 Source Plan。"""
    profile = load_search_profiles().get(request.profile_id)
    registry = SourceRegistry.from_file()
    categories = set(request.source_categories or profile.source_categories)
    rows: list[dict[str, Any]] = []
    for definition in registry.definitions:
        if not profile.allows_source(definition.source_id, definition.category):
            continue
        if categories and definition.category not in categories:
            continue
        if not _source_region_matches(definition, request, profile):
            continue
        manual_action = configured_manual_action(definition)
        runnable = bool(definition.enabled and definition.crawl_enabled)
        if not definition.enabled:
            reason = "DISABLED"
        elif manual_action:
            reason = "MANUAL_ACTION_REQUIRED"
            # 已知需要登录、验证码或人工安全验证的来源必须停在边界外，
            # 不能因为它有一个看似可用的 URL 就自动发起请求。
            runnable = False
        elif definition.adapter_level.upper() == "CATALOG" or definition.access_method == "catalog_discovery":
            reason = "CATALOG_ONLY"
            runnable = False
        elif not definition.crawl_enabled or definition.adapter in {"configured", "registry_catalog"}:
            reason = "ADAPTER_NOT_CONFIGURED"
            runnable = False
        else:
            reason = "READY"
        reason_detail = {
            "DISABLED": "来源被禁用",
            "MANUAL_ACTION_REQUIRED": "已有来源入口，但需要用户完成登录、验证码或浏览器安全验证后定向重试",
            "CATALOG_ONLY": "这是来源家族目录，不是具体公告站点；需要按目录发现并单独配置子站",
            "ADAPTER_NOT_CONFIGURED": "来源已登记，但当前没有经过核验的在线 Adapter；不代表本轮访问失败",
            "READY": "已配置可运行 Adapter",
        }[reason]
        rows.append({
            "source_id": definition.source_id,
            "source_name": definition.source_name,
            "category": definition.category,
            "region": definition.region,
            "priority": definition.priority,
            "adapter": definition.adapter,
            "adapter_level": definition.adapter_level,
            "requires_login": definition.requires_login,
            "manual_action_required": manual_action is not None,
            "manual_action_type": manual_action,
            "manual_action_status": "PENDING_USER" if manual_action else None,
            "manual_action_url": definition.base_url if manual_action else None,
            "manual_action_http_status": configured_manual_http_status(definition) if manual_action else None,
            "browser_profile_path": definition.browser_profile_path or str(browser_profile_path(definition.source_id)),
            "manual_browser": DEFAULT_BROWSER,
            "configuration_status": definition.status,
            "notes": definition.notes,
            "selected": runnable,
            "reason": reason,
            "reason_detail": reason_detail,
        })
    rows.sort(key=lambda row: (not row["selected"], row["priority"], row["source_id"]))
    return rows


def build_coverage_manifest(
    source_plan: Iterable[dict[str, Any]],
    source_results: Iterable[dict[str, Any]] = (),
    *,
    errors: Iterable[str] = (),
    query_count: int = 0,
    candidate_yield: int = 0,
    enrichment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return an auditable execution manifest.

    A source that was not selected, not configured, blocked, or returned a
    suspicious zero result is never folded into the successful-source count.
    This is deliberately a pure function so reports and tests can use it
    without touching the network or the database.
    """

    plan_rows = [dict(row) for row in source_plan]
    result_rows = [dict(row) for row in source_results]
    result_by_id = {
        str(row.get("source_id")): row
        for row in result_rows
        if row.get("source_id")
    }
    attempted: list[dict[str, Any]] = []
    successful: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    adapter_not_configured: list[dict[str, Any]] = []
    suspicious_zero_results: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    query_counts: dict[str, int] = {}

    for planned in plan_rows:
        source_id = str(planned.get("source_id") or "")
        actual = result_by_id.get(source_id)
        base = {
            "source_id": source_id,
            "source_name": planned.get("source_name"),
            "reason": planned.get("reason"),
            "reason_detail": planned.get("reason_detail"),
            "status": (actual or {}).get("status") or planned.get("configuration_status"),
            "health_reason": (actual or {}).get("health_reason"),
            "last_http_status": (actual or {}).get("last_http_status"),
            "error": (actual or {}).get("error"),
            "query_count": int((actual or {}).get("query_count") or 0),
        }
        if base["query_count"]:
            query_counts[source_id] = base["query_count"]
        if planned.get("reason") == "ADAPTER_NOT_CONFIGURED":
            adapter_not_configured.append(base)
            continue
        if planned.get("reason") == "MANUAL_ACTION_REQUIRED" or planned.get("manual_action_required"):
            base.update({
                "manual_action_required": True,
                "manual_action_type": planned.get("manual_action_type"),
                "manual_action_url": planned.get("manual_action_url"),
                "manual_browser": planned.get("manual_browser") or "Microsoft Edge",
                "browser_profile_path": planned.get("browser_profile_path"),
            })
            blocked.append(base)
            continue
        if not planned.get("selected"):
            skipped.append(base)
            continue
        if actual is None:
            base["reason"] = "NOT_RETURNED"
            skipped.append(base)
            continue
        attempted.append(base)
        if actual.get("manual_action_required") or actual.get("health_reason") in {
            "CAPTCHA", "LOGIN_REQUIRED", "VERIFICATION_REQUIRED", "HTTP_412", "HTTP_419", "HTTP_403", "HTTP_429",
        }:
            blocked.append(base)
        elif actual.get("error") or int(actual.get("failures") or 0) > 0:
            failed.append(base)
        elif actual.get("health_reason") == "SUSPECT_ZERO_RESULTS":
            suspicious_zero_results.append(base)
            successful.append(base)
        else:
            successful.append(base)

    # Provider-level Discovery is not part of the fixed-source plan.  Keep it
    # in the attempted/failed accounting when a summary row is supplied.
    for actual in result_rows:
        source_id = str(actual.get("source_id") or "")
        if not source_id or source_id in result_by_id and any(str(row.get("source_id")) == source_id for row in plan_rows):
            continue
        row = {
            "source_id": source_id,
            "source_name": actual.get("source_name") or actual.get("provider") or source_id,
            "status": actual.get("status"),
            "health_reason": actual.get("health_reason"),
            "last_http_status": actual.get("last_http_status"),
            "error": actual.get("error"),
            "query_count": int(actual.get("query_count") or 0),
            "manual_action_required": bool(actual.get("manual_action_required")),
            "manual_action_type": actual.get("manual_action_type"),
            "manual_action_url": actual.get("manual_action_url"),
            "manual_browser": actual.get("manual_browser") or "Microsoft Edge",
        }
        query_counts[source_id] = row["query_count"]
        attempted.append(row)
        if actual.get("manual_action_required"):
            blocked.append(row)
        elif actual.get("error"):
            failed.append(row)
        else:
            successful.append(row)

    error_list = list(errors)
    planned_count = len([row for row in plan_rows if row.get("selected")])
    planned_ids = {str(row.get("source_id")) for row in plan_rows if row.get("source_id")}
    provider_target_count = len({str(row.get("source_id")) for row in result_rows if row.get("source_id") and str(row.get("source_id")) not in planned_ids})
    target_count = planned_count + provider_target_count
    return {
        "target": {
            "planned_sources": planned_count,
            "planned_total": len(plan_rows),
            "provider_sources": provider_target_count,
            "manual_action_sources": len(blocked),
            "adapter_not_configured_sources": len(adapter_not_configured),
        },
        "attempted": attempted,
        "successful": successful,
        "failed": failed,
        "blocked": blocked,
        "adapter_not_configured": adapter_not_configured,
        "suspicious_zero_results": suspicious_zero_results,
        "skipped": skipped,
        "query_count": int(query_count),
        "query_counts_by_source": query_counts,
        "candidate_yield": int(candidate_yield),
        "enrichment": dict(enrichment or {}),
        "errors": error_list,
        # ``coverage_rate`` is against the selected runnable target, while
        # ``execution_rate`` exposes how much of that target was actually
        # attempted.  Blocked and unconfigured sources never count as success.
        "coverage_rate": round(len(successful) / max(1, target_count), 4),
        "execution_rate": round(len(attempted) / max(1, target_count), 4),
    }


def _start_of_day(value: datetime) -> datetime:
    local = as_shanghai(value)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def _end_of_day(value: datetime) -> datetime:
    local = as_shanghai(value)
    return local.replace(hour=23, minute=59, second=59, microsecond=999999)


def _date_bounds(request: SearchRequest) -> tuple[datetime, datetime]:
    current = now_shanghai()
    if request.date_from:
        lower = parse_datetime(request.date_from)
        if lower is None:
            lower = current - timedelta(days=request.days)
        elif len(request.date_from) <= 10:
            lower = _start_of_day(lower)
    else:
        lower = current - timedelta(days=request.days)
    if request.date_to:
        upper = parse_datetime(request.date_to)
        if upper is None:
            upper = current
        elif len(request.date_to) <= 10:
            upper = _end_of_day(upper)
    else:
        upper = current
    return as_shanghai(lower), as_shanghai(upper)


def _matches_region(project: Project, request: SearchRequest) -> bool:
    if normalize_identity(_request_region_text(request)) in {"全国", "全国性", "全网"}:
        return True
    requested = (request.region, request.city, request.county)
    actual = (project.province, project.city, project.county)
    if not any(requested):
        if not request.region_codes:
            return True
        try:
            catalog = load_region_catalog()
            selected = catalog.selected(request.region_codes)
            labels = {
                normalize_identity(label)
                for entry in selected
                for label in (entry.name, entry.short_name, *entry.aliases)
                if label
            }
            actual_text = normalize_identity(" ".join(item for item in (*actual, project.project_location, project.location) if item))
            if not actual_text:
                return request.search_mode != "exact"
            return any(label in actual_text for label in labels)
        except Exception:
            return True
    actual_text = normalize_identity(" ".join(item for item in (*actual, project.project_location, project.location) if item))
    if not actual_text:
        # 高召回模式保留没有可靠地区字段的候选；精确模式才把它排除。
        return request.search_mode != "exact"
    for wanted, got in zip(requested, actual):
        if not wanted:
            continue
        wanted_text = normalize_identity(wanted)
        if wanted_text in actual_text:
            continue
        # 已明确填入其他省/市/县时排除；该层级为空时不要把不完整地理
        # 信息误当成不匹配。
        if got and not (wanted_text in normalize_identity(got) or normalize_identity(got) in wanted_text):
            return False
    return True


def _matches_profile_regions(project: Project, request: SearchRequest, profile: Any) -> bool:
    if any((request.region, request.city, request.county)) or not profile.regions:
        return True
    try:
        catalog = load_region_catalog()
        selected = catalog.selected(profile.regions, profile.excluded_regions)
        labels = {
            normalize_identity(label)
            for entry in selected
            for label in (entry.name, entry.short_name, *entry.aliases)
            if label
        }
        actual = normalize_identity(" ".join(item for item in (project.province, project.city, project.county, project.project_location, project.location) if item))
        if not actual:
            return request.search_mode != "exact"
        return any(label in actual for label in labels)
    except Exception:
        return True


def _project_text(project: Project, announcement: Announcement | None) -> str:
    return " ".join(
        _text(getattr(project, field_name, None))
        for field_name in (
            "project_name", "raw_project_name", "owner", "purchaser", "tenderer", "agency", "industry", "project_type", "project_scale", "project_code", "tender_code", "qualification_summary", "participation_method", "project_location", "tenderer_location", "agency_location", "source_location", "location",
        )
    ) + " " + (_text(announcement.clean_text if announcement is not None else ""))


def _matched_terms(project: Project, announcement: Announcement | None, request: SearchRequest) -> list[str]:
    haystack = _project_text(project, announcement).casefold()
    terms = list(request.keywords) + list(request.equipment) + list(request.project_types)
    group_ids = request.industries or load_search_profiles().get(request.profile_id).industry_groups
    try:
        catalog = load_industry_profiles()
        for group_id in group_ids:
            terms.extend(catalog.get(group_id).terms)
    except Exception:
        for group_id in group_ids:
            terms.extend(INDUSTRY_ALIASES.get(group_id, (group_id,)))
    return list(dict.fromkeys(term for term in terms if term and term.casefold() in haystack))


def _source_allowed(project: Project, request: SearchRequest) -> bool:
    profile = load_search_profiles().get(request.profile_id)
    required = (request.source_level or profile.min_source_level or "").upper()
    if not required:
        return True
    ranks = {"A": 1, "B": 2, "C": 3, "D": 4, "E": 5}
    return ranks.get((project.source_level or "E").upper(), 9) <= ranks.get(required, 5)


def _result_source_level(result: Any) -> str:
    metadata = getattr(result, "metadata", None) or {}
    configured = str(metadata.get("source_level") or metadata.get("sourceLevel") or "").upper()
    if configured in {"A", "B", "C", "D", "E"}:
        return configured
    url = str(getattr(result, "url", "") or "")
    domain = (urlparse(url).netloc or "").lower().split(":", 1)[0]
    if domain.endswith((".gov.cn", ".gov", ".mil.cn")):
        return "A"
    if domain == "mp.weixin.qq.com":
        return "B"
    if any(token in f"{getattr(result, 'title', '')} {getattr(result, 'snippet', '')}" for token in ("招标代理", "代理机构")):
        return "C"
    return "E"


def _status_allowed(project: Project, request: SearchRequest) -> bool:
    if request.only_open:
        return project.status == "OPEN"
    if request.include_unknown:
        return project.status in {"OPEN", "UNKNOWN"}
    return project.status == "OPEN"


def _next_deadline(project: Project) -> datetime | None:
    values = [
        getattr(project, field_name, None)
        for field_name in ("qualification_deadline", "registration_deadline", "document_deadline", "bid_deadline", "open_time")
    ]
    values = [as_shanghai(value) for value in values if isinstance(value, datetime)]
    return min(values) if values else None


def _compact_project(session: Any, project: Project, announcement: Announcement | None, review_items: list[dict[str, Any]]) -> dict[str, Any]:
    def value(field_name: str) -> Any:
        item = getattr(project, field_name, None)
        return item.isoformat() if isinstance(item, datetime) else str(item) if item is not None else None

    source_rows = []
    for link in session.scalars(select(ProjectSource).where(ProjectSource.project_id == project.project_id)).all():
        source = session.get(Source, link.source_id)
        source_rows.append({
            "source_id": link.source_id,
            "source_name": source.source_name if source else None,
            "source_level": project.source_level,
            "source_url": link.source_url or (source.base_url if source else None),
        })
    evidence_rows = list(session.scalars(select(Evidence).where(Evidence.project_id == project.project_id)).all())
    evidence_ids = [row.id for row in evidence_rows]
    critical_evidence_fields = [
        row.field_name for row in evidence_rows
        if row.field_name in {"qualification_deadline", "registration_deadline", "document_deadline", "bid_deadline", "open_time"}
        and evidence_strength(row) == "STRONG"
    ]
    deadline = _next_deadline(project)
    remaining_hours = None
    if deadline is not None:
        remaining_hours = round((deadline - now_shanghai()).total_seconds() / 3600, 2)
    return {
        "project_id": project.project_id,
        "announcement_id": announcement.id if announcement else None,
        "project_name": project.project_name,
        "raw_project_name": project.raw_project_name,
        "canonical_project_name": project.canonical_project_name,
        "province": project.province,
        "city": project.city,
        "county": project.county,
        "location": project.location,
        "project_location": project.project_location,
        "tenderer_location": project.tenderer_location,
        "agency_location": project.agency_location,
        "source_location": project.source_location,
        "owner": project.owner,
        "purchaser": project.purchaser,
        "tenderer": project.tenderer,
        "agency": project.agency,
        "project_type": project.project_type,
        "announcement_type": project.announcement_type,
        "industry": project.industry,
        "capacity_mw": project.capacity_mw,
        "capacity_mwh": project.capacity_mwh,
        "budget": str(project.budget) if project.budget is not None else None,
        "project_code": project.project_code,
        "tender_code": project.tender_code,
        "publish_time": value("publish_time"),
        "qualification_start": value("qualification_start"),
        "qualification_deadline": value("qualification_deadline"),
        "registration_start": value("registration_start"),
        "registration_deadline": value("registration_deadline"),
        "document_start": value("document_start"),
        "document_deadline": value("document_deadline"),
        "bid_deadline": value("bid_deadline"),
        "open_time": value("open_time"),
        "participation_method": project.participation_method,
        "status": project.status,
        "tender_status": project.tender_status or project.status,
        "status_reason": project.status_reason,
        "source_level": project.source_level,
        "source_name": project.source_name,
        "source_type": project.source_type,
        "source_url": (announcement.source_url if announcement else None) or project.source_url,
        "content_hash": project.content_hash or (announcement.content_hash if announcement else None),
        "lifecycle_state": project.lifecycle_state,
        "needs_codex_review": project.needs_codex_review,
        "review_reasons": [item.get("reason") for item in review_items],
        "completeness_score": project.completeness_score,
        "evidence_ids": evidence_ids,
        "critical_evidence_fields": list(dict.fromkeys(critical_evidence_fields)),
        "overall_confidence": project.overall_confidence,
        "field_confidence": project.field_confidence,
        "source_confidence": project.source_confidence,
        "project_match_confidence": project.project_match_confidence,
        "relevance_class": project.relevance_class,
        "verification_status": project.verification_status,
        "enrichment_state": project.enrichment_state,
        "blocker": project.blocker,
        "next_action": project.next_action,
        "identity_status": project.identity_status,
        "identity_confidence": project.identity_confidence,
        "relation_types": json.loads(project.relation_types_json or "[]") if project.relation_types_json else [],
        "matched_concepts": json.loads(project.matched_concepts_json or "[]") if project.matched_concepts_json else [],
        "missing_fields": json.loads(project.missing_fields_json or "[]") if project.missing_fields_json else [],
        "rank_score": project.rank_score,
        "remaining_hours": remaining_hours,
        "source_count": len(source_rows) or (1 if project.source_url else 0),
        "sources": source_rows,
        "best_source_url": (source_rows[0].get("source_url") if source_rows else None) or project.source_url,
        "is_new": project.lifecycle_state == "NEW",
        "is_updated": project.lifecycle_state == "UPDATED",
        "is_reopened": project.lifecycle_state == "REOPENED",
    }


class SearchRunner:
    def __init__(self, *, database: str | None = None):
        self.engine = initialize_database(create_engine_for(database))

    def plan(self, request: SearchRequest) -> dict[str, Any]:
        profile = load_search_profiles().get(request.profile_id)
        terms = _terms(request)
        source_plan = build_source_plan(request)
        runnable_sources = [item for item in source_plan if item["selected"]]
        day_start = _start_of_day(now_shanghai())
        with session_scope(self.engine) as session:
            daily_used = sum(
                int(value or 0)
                for value in session.scalars(
                    select(SearchSession.query_count).where(
                        SearchSession.started_at >= day_start,
                        SearchSession.request_json.like(f'%"profile_id": "{request.profile_id}"%'),
                    )
                ).all()
            )
        daily_remaining = max(0, profile.max_queries_per_day - daily_used)
        coverage_budget = min(profile.coverage_budget or profile.query_budget, daily_remaining)
        discovery_requested = bool(request.discovery or request.deep)
        discovery_cap = min(profile.discovery_budget, 12 if request.deep else 6, max(0, daily_remaining - coverage_budget)) if discovery_requested else 0
        crawl_budget = coverage_budget
        terms_per_source = max(1, crawl_budget // max(1, len(runnable_sources))) if runnable_sources and crawl_budget else 0
        crawl_terms = list(terms[:terms_per_source]) if terms_per_source else []
        crawl_query_count = len(runnable_sources) * len(crawl_terms)
        discovery_query_count = min(discovery_cap, max(0, daily_remaining - crawl_query_count))
        return {
            "profile_id": profile.profile_id,
            "region": request.region,
            "city": request.city,
            "county": request.county,
            "query_budget": coverage_budget + discovery_query_count,
            "coverage_budget": coverage_budget,
            "discovery_budget": discovery_query_count,
            "enrichment_budget": min(profile.enrichment_budget, max(0, daily_remaining - crawl_query_count - discovery_query_count)),
            "verification_budget": min(profile.verification_budget, max(0, daily_remaining - crawl_query_count - discovery_query_count)),
            "daily_query_limit": profile.max_queries_per_day,
            "daily_queries_used": daily_used,
            "daily_queries_remaining": daily_remaining,
            "query_terms": crawl_terms,
            "crawl_sources": [item["source_id"] for item in runnable_sources],
            "crawl_query_count_estimate": crawl_query_count,
            "discovery_enabled": discovery_requested,
            "discovery_query_count_estimate": discovery_query_count,
            "wechat_enabled": request.wechat or request.deep,
            "max_results_per_query": profile.max_results_per_query,
            "source_plan": source_plan,
            "omitted_sources": [item for item in source_plan if not item["selected"]],
            "dry_run": True,
        }

    def _create_session(self, request: SearchRequest, session_id: str, plan: dict[str, Any]) -> None:
        with session_scope(self.engine) as session:
            session.add(
                SearchSession(
                    session_id=session_id,
                    request_id=request.request_id,
                    request_json=json.dumps(request.to_dict(), ensure_ascii=False),
                    search_mode=resolve_search_mode(request),
                    result_mode=resolve_result_mode(request),
                    started_at=now_shanghai(),
                    status="RUNNING",
                    sources_planned=len(plan.get("crawl_sources", [])),
                    source_plan_json=json.dumps(plan.get("source_plan", []), ensure_ascii=False),
                )
            )

    def _recall_projects(self, session: Any, request: SearchRequest) -> list[tuple[Project, Announcement | None, list[str]]]:
        """Build the durable recall pool before presentation filters.

        Status, source level, missing dates, and missing official sources are
        intentionally not gates here.  They are classification dimensions in
        Candidate and result layers.
        """
        lower, upper = _date_bounds(request)
        profile = load_search_profiles().get(request.profile_id)
        candidates: list[tuple[Project, Announcement | None, list[str]]] = []
        prior_candidates = {
            row.project_id: row
            for row in session.scalars(select(Candidate).where(Candidate.project_id.is_not(None)).order_by(Candidate.updated_at.desc())).all()
            if row.project_id
        }
        for project in session.scalars(select(Project).order_by(Project.updated_at.desc())).all():
            if project.ignored:
                continue
            if not _matches_region(project, request) or not _matches_profile_regions(project, request, profile):
                continue
            announcements = list(session.scalars(select(Announcement).where(Announcement.project_id == project.project_id).order_by(Announcement.published_at.desc(), Announcement.id.desc())).all())
            announcement = announcements[0] if announcements else None
            # 变更、延期、澄清等公告可以让一个较早创建的项目重新进入
            # 搜索窗口；不能只用 Project.publish_time 把它漏掉。
            dated_values = [
                as_shanghai(value)
                for value in [project.publish_time, *(row.published_at for row in announcements)]
                if value is not None
            ]
            if dated_values and not any(lower <= value <= upper for value in dated_values):
                continue
            if request.announcement_types and not any(
                normalize_identity(value) in normalize_identity(str(announcement.announcement_type if announcement else project.announcement_type or ""))
                for value in request.announcement_types
            ):
                continue
            haystack = _project_text(project, announcement).casefold()
            excluded = list(dict.fromkeys((*request.exclude_keywords, *profile.exclude_keywords)))
            matched = _matched_terms(project, announcement, request)
            required_terms = list(request.keywords) + list(request.equipment) + list(request.project_types)
            group_ids = request.industries or profile.industry_groups
            if group_ids:
                try:
                    catalog = load_industry_profiles()
                    required_terms.extend(term for group_id in group_ids for term in catalog.get(group_id).terms)
                except Exception:
                    required_terms.extend(term for group_id in group_ids for term in INDUSTRY_ALIASES.get(group_id, (group_id,)))
            relevance, _, _ = classify_relevance(haystack)
            excluded_hits = [term for term in excluded if term and term.casefold() in haystack]
            if required_terms and not matched and request.search_mode == "exact":
                continue
            if not required_terms and relevance == "IRRELEVANT":
                continue
            prior_candidate = prior_candidates.get(project.project_id)
            prior_relevant = prior_candidate is not None and prior_candidate.relevance_class not in {None, "IRRELEVANT"}
            if required_terms and not matched and relevance == "IRRELEVANT" and not prior_relevant:
                continue
            # Exclusions affect visibility but not recall.  Keep the marker so
            # the output layer can explain why a candidate was hidden.
            if excluded_hits:
                matched = list(matched) + [f"EXCLUDED:{term}" for term in excluded_hits]
            record = project_to_record(project)
            evidence_rows = list(session.scalars(select(Evidence).where(Evidence.project_id == project.project_id)).all())
            overrides = list(session.scalars(select(ManualOverride).where(ManualOverride.project_id == project.project_id, ManualOverride.active.is_(True))).all())
            decision = recalculate_status(
                record,
                evidences=with_manual_evidence(evidence_rows, overrides, source_url=project.source_url),
                require_evidence=True,
            )
            if project.status != decision.status.value:
                old_status = project.status
                project.status = decision.status.value
                add_status_history(session, project.project_id, old_status, project.status, decision.reason, now_shanghai())
            project.status_reason = decision.reason_code
            project.tender_status = project.status
            project.status_evaluated_at = now_shanghai()
            project.status_rule_version = "status.v2"
            candidates.append((project, announcement, matched))
        return candidates

    def _select_projects(self, session: Any, request: SearchRequest) -> list[tuple[Project, Announcement | None, list[str]]]:
        """Legacy presentation selector; persistence uses ``_recall_projects``."""

        candidates = [
            item for item in self._recall_projects(session, request)
            if _source_allowed(item[0], request) and _status_allowed(item[0], request)
        ]
        status_rank = {"OPEN": 0, "UNKNOWN": 1, "CLOSED": 2}
        candidates.sort(
            key=lambda item: (
                status_rank.get(item[0].status, 9),
                _next_deadline(item[0]) or datetime.max.replace(tzinfo=now_shanghai().tzinfo),
                {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}.get((item[0].source_level or "E").upper(), 9),
                -(as_shanghai(item[0].publish_time).timestamp() if item[0].publish_time else 0),
                -(item[0].completeness_score or 0),
            )
        )
        return candidates

    @staticmethod
    def _previously_reported(session: Any, request: SearchRequest, current_session_id: str) -> dict[str, dict[str, Any]]:
        """读取同一检索范围以前已经交付给用户的项目。"""

        current_key = search_scope_key(request)
        index: dict[str, dict[str, Any]] = {}
        sessions = session.scalars(
            select(SearchSession)
            .where(SearchSession.status.in_(("COMPLETED", "PARTIAL")))
            .order_by(SearchSession.finished_at.desc(), SearchSession.started_at.desc())
        ).all()
        for previous in sessions:
            if previous.session_id == current_session_id:
                continue
            try:
                payload = json.loads(previous.request_json or "{}")
            except (TypeError, ValueError):
                continue
            if search_scope_key(payload) != current_key:
                continue
            reported_at = previous.finished_at or previous.started_at
            if reported_at is None:
                continue
            reported_at = as_shanghai(reported_at)
            links = session.scalars(
                select(SearchSessionProject).where(SearchSessionProject.session_id == previous.session_id)
            ).all()
            for link in links:
                current = index.get(link.project_id)
                if current is None or reported_at > current["reported_at"]:
                    index[link.project_id] = {
                        "reported_at": reported_at,
                        "status_at_search": link.status_at_search,
                        "announcement_ids": {link.announcement_id} if link.announcement_id is not None else set(),
                        "session_id": previous.session_id,
                    }
                elif reported_at == current["reported_at"] and link.announcement_id is not None:
                    current["announcement_ids"].add(link.announcement_id)
        return index

    @staticmethod
    def _changed_since_report(
        session: Any,
        project: Project,
        announcement: Announcement | None,
        previous: dict[str, Any],
    ) -> bool:
        """只要项目有实质变化，就允许再次进入结果。"""

        reported_at = previous["reported_at"]
        last_change_at = getattr(project, "last_change_at", None)
        if last_change_at is not None and as_shanghai(last_change_at) > reported_at:
            return True
        if previous.get("status_at_search") and project.status != previous["status_at_search"]:
            return True
        if announcement is not None and announcement.id not in previous.get("announcement_ids", set()):
            return True
        if session.scalar(
            select(ProjectSource)
            .where(ProjectSource.project_id == project.project_id, ProjectSource.first_seen_at > reported_at)
        ) is not None:
            return True
        if session.scalar(
            select(ManualOverride)
            .where(ManualOverride.project_id == project.project_id, ManualOverride.changed_at > reported_at)
        ) is not None:
            return True
        return False

    def _filter_repeated_unchanged(
        self,
        session: Any,
        request: SearchRequest,
        session_id: str,
    selected: list[tuple[Project, Announcement | None, list[str]]],
    ) -> tuple[list[tuple[Project, Announcement | None, list[str]]], list[str]]:
        """过滤 DELTA_RESULT 中已交付且没有变化的项目，保留变化项目。"""

        previous = self._previously_reported(session, request, session_id)
        if not previous:
            return selected, []
        visible: list[tuple[Project, Announcement | None, list[str]]] = []
        suppressed: list[str] = []
        for item in selected:
            project, announcement, _ = item
            previous_row = previous.get(project.project_id)
            if previous_row is not None and not self._changed_since_report(session, project, announcement, previous_row):
                suppressed.append(project.project_id)
                continue
            visible.append(item)
        return visible, suppressed

    @staticmethod
    def _valuable_lead_key(lead: dict[str, Any]) -> str:
        """内容未变化判断：同一来源改了摘要或 URL 时允许重新上报。"""

        return json.dumps(
            {
                "lead_type": lead.get("lead_type"),
                "canonical_url": lead.get("canonical_url") or lead.get("source_url"),
                "source_text": lead.get("source_text") or "",
                "source_title": lead.get("source_title") or "",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def _previously_reported_valuable_leads(
        cls,
        session: Any,
        request: SearchRequest,
        current_session_id: str,
    ) -> set[str]:
        """从同一搜索范围的历史 Session 读取已交付且未变化的价值线索。"""

        current_key = search_scope_key(request)
        keys: set[str] = set()
        sessions = session.scalars(
            select(SearchSession)
            .where(SearchSession.status.in_(("COMPLETED", "PARTIAL")))
            .order_by(SearchSession.finished_at.desc(), SearchSession.started_at.desc())
        ).all()
        for previous in sessions:
            if previous.session_id == current_session_id:
                continue
            try:
                payload = json.loads(previous.request_json or "{}")
            except (TypeError, ValueError):
                continue
            if search_scope_key(payload) != current_key:
                continue
            try:
                source_rows = json.loads(previous.sources_json or "[]")
            except (TypeError, ValueError):
                continue
            if not isinstance(source_rows, list):
                continue
            for source in source_rows:
                if not isinstance(source, dict):
                    continue
                for lead in source.get("valuable_leads") or ():
                    if isinstance(lead, dict):
                        keys.add(cls._valuable_lead_key(lead))
        return keys

    @classmethod
    def _filter_repeated_unchanged_valuable_leads(
        cls,
        session: Any,
        request: SearchRequest,
        current_session_id: str,
        leads: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], int]:
        previous = cls._previously_reported_valuable_leads(session, request, current_session_id)
        if not previous:
            return leads, 0
        visible: list[dict[str, Any]] = []
        suppressed = 0
        for lead in leads:
            if cls._valuable_lead_key(lead) in previous:
                suppressed += 1
            else:
                visible.append(lead)
        return visible, suppressed

    def _write_outputs(
        self,
        session: Any,
        request: SearchRequest,
        session_id: str,
        summary: SearchSummary,
        selected: list[tuple[Project, Announcement | None, list[str]]],
        reportable: list[tuple[Project, Announcement | None, list[str]]] | None = None,
        candidate_pool: list[Candidate] | None = None,
    ) -> None:
        output_dir = APP_ROOT.parent / "output" / "sessions" / session_id
        output_dir.mkdir(parents=True, exist_ok=True)
        summary_path = output_dir / "summary.md"
        report_path = output_dir / "search_report.md"
        reportable = selected if reportable is None else reportable
        valuable_leads_path = output_dir / "valuable_leads.json"
        candidate_pool_path = output_dir / "candidate_pool.json"
        layers_path = output_dir / "layers.json"
        review_statement = select(CodexReviewItem).where(CodexReviewItem.search_session_id == session_id)
        review_rows = [review_item_dict(session, item) for item in session.scalars(review_statement.order_by(CodexReviewItem.priority, CodexReviewItem.created_at)).all()]
        summary.review_count = len(review_rows)
        open_projects: list[dict[str, Any]] = []
        unknown_projects: list[dict[str, Any]] = []
        all_projects: list[dict[str, Any]] = []
        for project, announcement, matched in reportable:
            item_rows = [row for row in review_rows if row["project_id"] == project.project_id]
            compact = _compact_project(session, project, announcement, item_rows)
            compact["matched_keywords"] = matched
            compact["matched_region"] = " / ".join(item for item in (project.province, project.city, project.county) if item)
            candidate = session.scalar(
                select(Candidate)
                .where(Candidate.project_id == project.project_id)
                .order_by(Candidate.updated_at.desc())
            )
            if candidate is not None:
                compact["candidate_id"] = candidate.candidate_id
                compact["candidate_class"] = candidate.candidate_class
                compact["candidate_content_hash"] = candidate.content_hash
            compact["result_bucket"] = result_bucket(compact)
            all_projects.append(compact)
            if project.status == "OPEN":
                open_projects.append(compact)
            elif project.status == "UNKNOWN":
                unknown_projects.append(compact)
        pool_rows = candidate_pool if candidate_pool is not None else list(session.scalars(select(Candidate).where(Candidate.search_session_id == session_id).order_by(Candidate.rank_score.desc(), Candidate.updated_at.desc())).all())
        candidate_rows: list[dict[str, Any]] = []
        for candidate in pool_rows:
            if candidate.project_id:
                project = session.get(Project, candidate.project_id)
                announcement = session.get(Announcement, candidate.announcement_id) if candidate.announcement_id else None
                if project is not None:
                    row = _compact_project(session, project, announcement, [])
                    session_link = session.scalar(
                        select(SearchSessionProject).where(
                            SearchSessionProject.session_id == session_id,
                            SearchSessionProject.candidate_id == candidate.candidate_id,
                        ).order_by(SearchSessionProject.id.desc())
                    )
                    if session_link is not None:
                        try:
                            row["matched_keywords"] = json.loads(session_link.matched_keywords or "[]")
                        except (TypeError, ValueError):
                            row["matched_keywords"] = []
                    row["candidate_id"] = candidate.candidate_id
                    row["candidate_key"] = candidate.candidate_key
                    row["relevance_class"] = candidate.relevance_class
                    row["verification_status"] = candidate.verification_status
                    row["enrichment_state"] = candidate.enrichment_state
                    row["blocker"] = candidate.blocker
                    row["next_action"] = candidate.next_action
                    row["missing_fields"] = json.loads(candidate.missing_fields_json or "[]") if candidate.missing_fields_json else []
                    row["candidate_values"] = json.loads(candidate.candidate_values_json or "{}") if candidate.candidate_values_json else {}
                    row["evidence_ids"] = json.loads(candidate.evidence_ids_json or "[]") if candidate.evidence_ids_json else []
                    row["candidate_content_hash"] = candidate.content_hash
                    row["candidate_pool"] = True
                    row["result_bucket"] = result_bucket(row)
                    candidate_rows.append(row)
                    continue
            row = candidate_dict(candidate)
            row["result_bucket"] = result_bucket(row)
            candidate_rows.append(row)
        # Delivery is a separate view from recall.  The pool is retained in
        # JSON even when DELTA_RESULT suppresses an unchanged project.  A
        # project candidate is delivered only through ``reportable``; this is
        # what makes the delta mode actually suppress unchanged projects.
        delivery_rows = list(all_projects)
        delivery_project_ids = {row.get("project_id") for row in delivery_rows}
        for row in candidate_rows:
            if row.get("project_id") in delivery_project_ids:
                continue
            if row.get("project_id"):
                continue
            if summary.result_mode == "DELTA_RESULT":
                session_start = session.get(SearchSession, session_id)
                started_at = session_start.started_at if session_start is not None else None
                first_seen = row.get("first_seen_at")
                last_change = row.get("last_change_at")
                if started_at is not None:
                    current_mark = as_shanghai(started_at).isoformat()
                    if not ((first_seen and str(first_seen) >= current_mark) or (last_change and str(last_change) >= current_mark)):
                        continue
            delivery_rows.append(row)
        layers: dict[str, list[dict[str, Any]]] = {key: [] for key in LAYER_LABELS}
        for row in delivery_rows:
            layers.setdefault(result_bucket(row), []).append(row)
        summary.layers = {key: len(value) for key, value in layers.items()}
        summary.candidate_pool_count = len(candidate_rows) or len(pool_rows)
        official_conversions = sum(
            1
            for row in candidate_rows
            if row.get("candidate_class") == "SECONDARY_LEAD"
            and row.get("verification_status") in {"OFFICIAL_VERIFIED", "OFFICIAL_PARTIAL", "MULTI_SOURCE_CONFIRMED"}
        )
        summary.quality_metrics = calculate_quality_metrics(
            candidates=candidate_rows,
            sources=summary.sources,
            enrichment_queries=summary.enrichment_count,
            official_conversions=official_conversions,
        )
        incomplete_coverage = bool(
            summary.errors
            or summary.manual_action_sources
            or summary.coverage_manifest.get("failed")
            or summary.coverage_manifest.get("blocked")
            or summary.coverage_manifest.get("adapter_not_configured")
        )
        results_payload = {
            "session": {
                "session_id": session_id,
                "request_id": request.request_id,
                "request": request.to_dict(),
                "query_count": summary.query_count,
                "queries_generated": summary.query_count,
                "search_mode": summary.search_mode,
                "result_mode": summary.result_mode,
                "candidate_count": len(delivery_rows),
                "candidate_pool_count": summary.candidate_pool_count,
                "projects_found": len(all_projects),
                "raw_candidate_count": len(selected),
                "suppressed_unchanged_count": summary.suppressed_unchanged_count,
                "suppression_reasons": summary.suppression_reasons,
                "sources_planned": summary.sources_planned,
                "sources_completed": summary.sources_completed,
                "sources_failed": summary.sources_failed,
                "verification_count": summary.verification_count,
                "valuable_lead_count": summary.valuable_lead_count,
                "suppressed_valuable_lead_count": summary.suppressed_valuable_lead_count,
                "status": "PARTIAL" if incomplete_coverage else "COMPLETED",
                "source_plan": summary.source_plan,
                "report_path": str(report_path),
                "manual_action_sources": summary.manual_action_sources,
                "layers": summary.layers,
                "coverage_manifest": summary.coverage_manifest,
                "quality_metrics": summary.quality_metrics,
            },
            "open_projects": open_projects,
            "unknown_projects": unknown_projects,
            "closed_projects": [row for row in delivery_rows if row.get("status") == "CLOSED" or row.get("tender_status") == "CLOSED"],
            "candidate_pool": candidate_rows,
            "candidates": delivery_rows,
            "layers": layers,
            "full_result": candidate_rows,
            "delta_result": delivery_rows if summary.result_mode == "DELTA_RESULT" else [],
            "review_items": review_rows,
            "valuable_leads": summary.valuable_leads,
            "official_trace_matches": summary.official_trace_matches,
            "errors": summary.errors,
            "sources": summary.sources,
            "source_health": summary.sources,
            "manual_action_sources": summary.manual_action_sources,
            "suppression_reasons": summary.suppression_reasons,
            "coverage_manifest": summary.coverage_manifest,
            "quality_metrics": summary.quality_metrics,
        }
        results_path = output_dir / "results.json"
        results_path.write_text(json.dumps(results_payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        valuable_leads_path.write_text(json.dumps(summary.valuable_leads, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        candidate_pool_path.write_text(json.dumps(candidate_rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        layers_path.write_text(json.dumps(layers, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        open_projects_path = output_dir / "open_projects.json"
        unknown_projects_path = output_dir / "unknown_projects.json"
        errors_path = output_dir / "errors.json"
        sources_path = output_dir / "sources.json"
        open_projects_path.write_text(json.dumps(open_projects, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        unknown_projects_path.write_text(json.dumps(unknown_projects, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        errors_path.write_text(json.dumps(summary.errors, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        sources_path.write_text(json.dumps({"source_plan": summary.source_plan, "source_health": summary.sources}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        lines = [
            f"search_mode: {summary.search_mode}",
            f"result_mode: {summary.result_mode}",
            f"candidate_pool_count: {summary.candidate_pool_count}",
            f"new/updated/reopened: {summary.new_candidate_count}/{summary.updated_candidate_count}/{summary.reopened_candidate_count}",
            f"layers: {json.dumps(summary.layers, ensure_ascii=False)}",
            f"coverage_manifest: {json.dumps(summary.coverage_manifest, ensure_ascii=False, default=str)}",
            f"quality_metrics: {json.dumps(summary.quality_metrics, ensure_ascii=False, default=str)}",
            f"# Search Session {session_id}",
            "",
            f"Request ID：{request.request_id}",
            f"搜索条件：{json.dumps(request.to_dict(), ensure_ascii=False)}",
            f"地区：{' / '.join(item for item in (request.region, request.city, request.county) if item) or '按 Profile'}",
            f"时间范围：{request.date_from or f'最近 {request.days} 天'} 至 {request.date_to or '现在'}",
            f"行业：{', '.join(request.industries) or 'Profile 默认行业'}；Deep：{'是' if request.deep else '否'}",
            f"来源计划：{summary.sources_planned} 个；成功：{summary.sources_completed} 个；失败：{summary.sources_failed} 个",
            f"查询数量：{summary.query_count}",
            f"候选数量：{len(all_projects)}",
            f"本轮原始候选：{len(selected)}；自动忽略上次已报告且未变化：{summary.suppressed_unchanged_count}",
            f"抑制原因：{json.dumps(summary.suppression_reasons, ensure_ascii=False)}",
            f"OPEN：{len(open_projects)}",
            f"UNKNOWN：{len(unknown_projects)}",
            f"Review 数量：{len(review_rows)}",
            f"二手/公众号线索官方追源命中：{len(summary.official_trace_matches)}",
            f"有价值但非直接组件支架采购线索：{summary.valuable_lead_count}",
            f"自动忽略上次已报告且未变化的价值线索：{summary.suppressed_valuable_lead_count}",
            f"Verification 数量：{summary.verification_count}",
            f"失败来源/错误：{len(summary.errors)}",
            "",
            "## A-J 结果分层",
            "",
            "候选池保留全部合理候选；分层只改变展示和后续动作，不改变持久化。",
            "",
            *[f"- {bucket}：{LAYER_LABELS[bucket]}（{summary.layers.get(bucket, 0)}）" for bucket in LAYER_LABELS],
            "",
            "## 输出文件",
            "",
            f"- results.json：{results_path}",
            f"- search_report.md：{report_path}",
            f"- open_projects.json：{open_projects_path}",
            f"- unknown_projects.json：{unknown_projects_path}",
            f"- codex_review.md：{output_dir / 'codex_review.md'}",
            f"- valuable_leads.json：{valuable_leads_path}",
            f"- errors.json：{errors_path}",
            f"- sources.json：{sources_path}",
            "",
        ]
        if summary.manual_action_sources:
            lines.extend(["## 需要人工处理的来源", "", "以下来源没有被伪装成成功，必须先完成人工登录或浏览器验证：", ""])
            for item in summary.manual_action_sources:
                lines.append(
                    f"- {item.get('source_name') or item.get('source_id') or '未知来源'}："
                    f"{item.get('manual_action_type') or item.get('health_reason') or 'MANUAL_ACTION_REQUIRED'}；"
                    f"HTTP {item.get('last_http_status') or item.get('manual_action_http_status') or '未知'}；{item.get('error') or '请人工处理后重试'}；"
                    f"人工验证浏览器：{item.get('manual_browser') or 'Microsoft Edge'}；"
                    f"打开地址：{item.get('manual_action_url') or '未提供'}；"
                    f"独立 Profile：{item.get('browser_profile_path') or '未提供'}；"
                    f"完成后回复‘已完成人工验证’，再只重试来源 {item.get('source_id') or '该来源'}"
                )
            lines.append("")
        for item in all_projects:
            lines.extend([
                f"## {item['project_name']}",
                "",
                f"地区：{' / '.join(value for value in (item.get('province'), item.get('city'), item.get('county')) if value) or '未知'}",
                f"招标人：{item.get('owner') or '未知'}",
                f"项目类型：{item.get('project_type') or '未知'}；规模：{item.get('capacity_mw') or item.get('capacity_mwh') or item.get('project_scale') or '未知'}",
                f"发布时间：{item.get('publish_time') or '未知'}",
                f"报名截止：{item.get('registration_deadline') or '未知'}；文件截止：{item.get('document_deadline') or '未知'}",
                f"投标截止：{item.get('bid_deadline') or '未知'}；开标：{item.get('open_time') or '未知'}",
                f"当前状态：{item.get('status')}；状态原因：{item.get('status_reason')}",
                f"来源等级：{item.get('source_level') or '未知'}；来源：{item.get('source_name') or ''}",
                f"URL：{item.get('source_url') or ''}",
                f"需要Review：{'是' if item.get('needs_codex_review') else '否'}；匹配关键词：{', '.join(item.get('matched_keywords') or []) or '无'}",
                "",
            ])
        if candidate_rows:
            lines.extend(["## Candidate Pool (full recall)", "", f"Total: {len(candidate_rows)}", ""])
            for candidate in candidate_rows:
                lines.extend([
                    f"- {candidate.get('project_name') or candidate.get('title') or 'UNKNOWN'}",
                    f"  - bucket: {candidate.get('result_bucket')}",
                    f"  - status: {candidate.get('status') or candidate.get('tender_status')} / {candidate.get('status_reason') or ''}",
                    f"  - relevance: {candidate.get('relevance_class')}; verification: {candidate.get('verification_status')}; enrichment: {candidate.get('enrichment_state')}",
                    f"  - blocker: {candidate.get('blocker') or ''}; next_action: {candidate.get('next_action') or ''}",
                ])
            lines.append("")
        if summary.official_trace_matches:
            lines.extend(["## 二手线索官方追源", "", "以下链接是依据二手网站/公众号线索按项目身份追查到的官方或法定来源；二手链接不作为最终事实来源。", ""])
            for item in summary.official_trace_matches:
                lines.extend([
                    f"- 线索：{item.get('lead_title') or '未知'}",
                    f"  - 线索地址：{item.get('lead_url') or ''}",
                    f"  - 官方标题：{item.get('title') or ''}",
                    f"  - 官方地址：{item.get('url') or ''}",
                    f"  - 来源等级：{item.get('source_level') or '未知'}",
                ])
            lines.append("")
        if summary.valuable_leads:
            lines.extend([
                "## 有价值但非直接组件支架采购线索",
                "",
                "以下结果单独上报，不能改写为直接组件支架采购或当前 OPEN 标的；每条均保留原始出处。",
                "",
            ])
            for lead in summary.valuable_leads:
                lines.extend([
                    f"### {lead.get('lead_label') or lead.get('lead_type')}: {lead.get('project_identity') or lead.get('source_title') or '未知项目'}",
                    "",
                    f"地区：{lead.get('region') or '未知'}；来源等级：{lead.get('source_level') or '未知'}；来源：{lead.get('source_domain') or '未知'}",
                    f"是否直接组件支架采购：否（当前证据未建立）；采购关系：{lead.get('procurement_status') or '未确认'}",
                    f"价值线索原因：{lead.get('reason') or ''}",
                    f"范围说明：{lead.get('scope_warning') or ''}",
                    f"原始标题：{lead.get('source_title') or ''}",
                    f"原始 URL：{lead.get('source_url') or ''}",
                    f"原文摘要：{lead.get('source_text') or '未提供'}",
                    f"后续追踪：{'；'.join(lead.get('follow_up_queries') or []) or '需人工补充项目身份'}",
                    "",
                ])
        report_text = "\n".join(lines)
        summary_path.write_text(report_text, encoding="utf-8")
        report_path.write_text(report_text, encoding="utf-8")
        review_md, review_json, _ = write_review_files(session, session_id)
        summary.output_dir = str(output_dir)
        summary.summary_path = str(summary_path)
        summary.report_path = str(report_path)
        summary.results_path = str(results_path)
        summary.review_markdown_path = str(review_md)
        summary.review_json_path = str(review_json)
        summary.open_projects_path = str(open_projects_path)
        summary.unknown_projects_path = str(unknown_projects_path)
        summary.errors_path = str(errors_path)
        summary.sources_path = str(sources_path)
        summary.valuable_leads_path = str(valuable_leads_path)
        summary.candidate_pool_path = str(candidate_pool_path)
        summary.layers_path = str(layers_path)

    def _run_recall_enrichment(self, request: SearchRequest, *, dry_run: bool = False) -> SearchSummary:
        session_id = f"search_{now_shanghai().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
        summary = SearchSummary(
            session_id=session_id,
            request=request.to_dict(),
            dry_run=dry_run,
            search_mode=resolve_search_mode(request),
            result_mode=resolve_result_mode(request),
        )
        plan = self.plan(request)
        summary.source_plan = list(plan.get("source_plan", []))
        summary.sources_planned = len(plan.get("crawl_sources", []))
        summary.manual_action_sources = [
            dict(item)
            for item in summary.source_plan
            if item.get("reason") == "MANUAL_ACTION_REQUIRED" or item.get("manual_action_required")
        ]
        if dry_run:
            summary.query_count = int(plan["crawl_query_count_estimate"] + plan["discovery_query_count_estimate"])
            summary.sources = [dict(item) for item in plan.get("source_plan", []) if item.get("selected")]
            summary.coverage_manifest = {
                "dry_run": True,
                "planned_sources": summary.sources_planned,
                "planned_queries": summary.query_count,
                "coverage_budget": plan.get("coverage_budget", 0),
                "discovery_budget": plan.get("discovery_budget", 0),
                "enrichment_budget": plan.get("enrichment_budget", 0),
                "verification_budget": plan.get("verification_budget", 0),
            }
            return summary

        self._create_session(request, session_id, plan)
        terms = tuple(plan.get("query_terms", []))
        discovery_candidate_results: list[Any] = []
        crawl_source_rows: list[dict[str, Any]] = []
        try:
            lower, _ = _date_bounds(request)
            lookback_days = max(1, int((now_shanghai() - lower).total_seconds() // 86400) + 1)
            crawl = CrawlRunner(database=str(self.engine.url)).run(
                source_ids=tuple(plan.get("crawl_sources", [])),
                profile_id=request.profile_id,
                since_days=lookback_days,
                max_items=200 if request.deep else 80,
                query_terms=terms,
                download_attachments=True,
                only_active_opportunities=False,
            )
            summary.query_count += sum(item.query_count for item in crawl.sources)
            for item in crawl.sources:
                source_payload = dict(item.__dict__)
                crawl_source_rows.append(source_payload)
                summary.sources.append(source_payload)
                if item.error:
                    summary.errors.append(f"{item.source_id}: {item.error}")
                if item.manual_action_required:
                    if not any(row.get("source_id") == item.source_id for row in summary.manual_action_sources):
                        summary.manual_action_sources.append(source_payload)
            summary.sources_completed += sum(1 for item in crawl.sources if not item.error and not item.manual_action_required)
            summary.sources_failed += sum(1 for item in crawl.sources if item.error or item.failures or item.manual_action_required)
        except Exception as exc:
            summary.errors.append(f"crawl: {exc}")
            summary.sources_failed = summary.sources_planned

        if request.discovery or request.deep:
            try:
                discovery_limit = int(plan.get("discovery_query_count_estimate", 0))
                discovery_queries = tuple(f'"{item}" 招标 采购' for item in terms[:discovery_limit])
                if discovery_queries:
                    discovery = DiscoveryRunner(database=str(self.engine.url)).run(
                        profile_id=request.profile_id,
                        max_queries=discovery_limit,
                        max_results=plan.get("max_results_per_query", 8),
                        custom_queries=discovery_queries,
                        wechat_enabled=request.wechat or request.deep,
                    )
                    summary.query_count += discovery.query_count
                    summary.errors.extend(discovery.errors)
                    discovery_candidate_results.extend(discovery.candidate_results)
                    discovery_source = {
                        "source_id": "provider:discovery",
                        "source_name": "Discovery Search Providers",
                        "provider": "discovery",
                        "query_count": discovery.query_count,
                        "result_count": discovery.result_count,
                        "wechat_candidates": discovery.wechat_candidate_count,
                        "secondary_lead_count": discovery.secondary_lead_count,
                        "secondary_trace_query_count": discovery.secondary_trace_query_count,
                        "secondary_official_match_count": discovery.secondary_official_match_count,
                        "secondary_unresolved_count": discovery.secondary_unresolved_count,
                        "secondary_trace_skipped_count": discovery.secondary_trace_skipped_count,
                        "secondary_blocked_count": discovery.secondary_blocked_count,
                        "official_trace_matches": discovery.secondary_trace_matches,
                        "valuable_lead_count": discovery.valuable_lead_count,
                        "valuable_leads": discovery.valuable_leads,
                        "status": "ACTIVE" if not discovery.errors else "DEGRADED",
                        "manual_action_required": discovery.manual_action_required,
                        "manual_action_type": discovery.manual_action_type,
                        "manual_action_provider": discovery.manual_action_provider,
                        "last_http_status": discovery.manual_action_http_status,
                        "manual_action_url": discovery.manual_action_url,
                        "manual_browser": discovery.manual_browser,
                        "error": "; ".join(discovery.errors) if discovery.errors else None,
                    }
                    summary.official_trace_matches.extend(discovery.secondary_trace_matches)
                    for lead in discovery.valuable_leads:
                        if lead not in summary.valuable_leads:
                            summary.valuable_leads.append(lead)
                    summary.valuable_lead_count = len(summary.valuable_leads)
                    summary.sources.append(discovery_source)
                    if discovery.manual_action_required:
                        summary.manual_action_sources.append({
                            "source_id": "provider:discovery",
                            "source_name": "Discovery Search Providers",
                            "status": "NEEDS_ATTENTION",
                            "health_reason": discovery.manual_action_type,
                            "manual_action_required": True,
                            "manual_action_type": discovery.manual_action_type,
                            "last_http_status": discovery.manual_action_http_status,
                            "manual_action_url": discovery.manual_action_url,
                            "manual_browser": discovery.manual_browser,
                            "error": "; ".join(discovery.errors) or "Discovery 需要人工登录、验证码或浏览器验证",
                        })
            except Exception as exc:
                summary.errors.append(f"discovery: {exc}")

        try:
            ExtractionRunner(database=str(self.engine.url)).run(sample_size=30, dry_run=False, consolidate=False, reuse_cached=True)
        except Exception as exc:
            summary.errors.append(f"extract: {exc}")

        project_candidate_ids: list[str] = []
        discovery_candidate_ids: list[str] = []
        with session_scope(self.engine) as session:
            recalled = self._recall_projects(session, request)
            for project, announcement, matched in recalled:
                candidate = CandidateStore.upsert_project(
                    session,
                    project,
                    announcement=announcement,
                    search_session_id=session_id,
                    matched=matched,
                    source_location=project.source_location,
                )
                project_candidate_ids.append(candidate.candidate_id)
                ensure_review_item(session, project, announcement=announcement, search_session_id=session_id)
                link = session.scalar(
                    select(SearchSessionProject).where(
                        SearchSessionProject.session_id == session_id,
                        SearchSessionProject.project_id == project.project_id,
                        SearchSessionProject.announcement_id == (announcement.id if announcement else None),
                    )
                )
                values = {
                    "relevance_class": candidate.relevance_class,
                    "verification_status": candidate.verification_status,
                    "enrichment_state": candidate.enrichment_state,
                    "blocker": candidate.blocker,
                    "next_action": candidate.next_action,
                    "result_bucket": result_bucket(candidate),
                    "match_type": "EXACT" if matched else "CONTEXTUAL",
                }
                if link is None:
                    session.add(SearchSessionProject(
                        session_id=session_id,
                        project_id=project.project_id,
                        candidate_id=candidate.candidate_id,
                        announcement_id=announcement.id if announcement else None,
                        found_via="crawl_recall",
                        match_score=100.0 if matched else 60.0,
                        matched_keywords=json.dumps(matched, ensure_ascii=False),
                        matched_region=" / ".join(item for item in (project.province, project.city, project.county) if item),
                        status_at_search=project.status,
                        is_new=project.lifecycle_state == "NEW",
                        is_updated=project.lifecycle_state == "UPDATED",
                        is_reopened=project.lifecycle_state == "REOPENED",
                        **values,
                    ))
                else:
                    for key, value in values.items():
                        setattr(link, key, value)

            lead_types = {
                str(item.get("canonical_url") or item.get("source_url")): item.get("lead_type")
                for item in summary.valuable_leads
                if isinstance(item, dict)
            }
            seen_discovery: set[str] = set()
            for result in discovery_candidate_results:
                url = str(getattr(result, "url", "") or "")
                if not url or url in seen_discovery:
                    continue
                seen_discovery.add(url)
                candidate = CandidateStore.upsert_search_result(
                    session,
                    result,
                    search_session_id=session_id,
                    source_level=_result_source_level(result),
                    region=request.region,
                    lead_type=lead_types.get(url),
                )
                discovery_candidate_ids.append(candidate.candidate_id)
            session.flush()

        enrichment_report: dict[str, Any] = {
            "budget": int(request.max_enrichments if request.max_enrichments is not None else plan.get("enrichment_budget", 0)),
            "targets": 0,
            "processed": 0,
            "blocked": 0,
            "exhausted": 0,
            "errors": [],
        }
        if (request.deep or request.discovery) and enrichment_report["budget"] > 0:
            target_ids = list(dict.fromkeys(project_candidate_ids + discovery_candidate_ids))
            with session_scope(self.engine) as session:
                # UNKNOWN candidates are an operational queue, not merely a
                # presentation bucket.  Process the highest-priority
                # blockers first, then use the recall score as the tie-breaker
                # so a deep run spends its finite enrichment budget where it
                # can resolve the most valuable uncertainty.
                candidate_rows = list(
                    session.scalars(
                        select(Candidate)
                        .where(Candidate.candidate_id.in_(target_ids))
                        .order_by(
                            Candidate.review_priority.asc().nullslast(),
                            Candidate.rank_score.desc(),
                            Candidate.updated_at.desc(),
                        )
                    ).all()
                ) if target_ids else []
                if not request.deep:
                    candidate_rows = [row for row in candidate_rows if row.project_id is None or row.verification_status in {"SECONDARY_ONLY", "DISCOVERY_LEAD", "UNVERIFIED"}]
                enrichment_report["targets"] = len(candidate_rows)
                target_ids = [row.candidate_id for row in candidate_rows]
            remaining = enrichment_report["budget"]
            for candidate_id in target_ids:
                if remaining <= 0:
                    break
                engine = EnrichmentEngine(database=str(self.engine.url))
                result = engine.run(
                    candidate_id,
                    search_session_id=session_id,
                    max_queries=min(18, remaining),
                    max_results=int(plan.get("max_results_per_query", 8)),
                    max_rounds=4,
                    process_attachments=request.deep,
                )
                summary.enrichment_count += result.query_count
                remaining -= result.query_count
                enrichment_report["processed"] += 1
                enrichment_report["blocked"] += int(result.blocked)
                enrichment_report["exhausted"] += int(result.exhausted)
                if result.errors:
                    enrichment_report["errors"].extend(result.errors)
                    summary.errors.extend(f"enrichment {candidate_id}: {error}" for error in result.errors)
            enrichment_report["used"] = enrichment_report["budget"] - remaining
            enrichment_report["remaining"] = remaining
        else:
            enrichment_report["used"] = 0
            enrichment_report["remaining"] = enrichment_report["budget"]

        with session_scope(self.engine) as session:
            recalled = self._recall_projects(session, request)
            for project, announcement, matched in recalled:
                CandidateStore.upsert_project(session, project, announcement=announcement, search_session_id=session_id, matched=matched, source_location=project.source_location)
                ensure_review_item(session, project, announcement=announcement, search_session_id=session_id)
            candidate_pool = list(session.scalars(select(Candidate).where(Candidate.search_session_id == session_id).order_by(Candidate.rank_score.desc(), Candidate.updated_at.desc())).all())
            if resolve_result_mode(request) == "DELTA_RESULT":
                reportable, suppressed_ids = self._filter_repeated_unchanged(session, request, session_id, recalled)
            else:
                reportable, suppressed_ids = recalled, []
            reportable = [
                item for item in reportable
                if _source_allowed(item[0], request)
                and (not request.only_open or item[0].status == "OPEN")
                and not any(str(value).startswith("EXCLUDED:") for value in item[2])
            ]
            summary.suppressed_project_ids = suppressed_ids
            summary.suppressed_unchanged_count = len(suppressed_ids)
            summary.suppression_reasons = {
                project_id: "UNCHANGED_ALREADY_REPORTED"
                for project_id in suppressed_ids
            }
            summary.valuable_leads, summary.suppressed_valuable_lead_count = self._filter_repeated_unchanged_valuable_leads(session, request, session_id, summary.valuable_leads) if resolve_result_mode(request) == "DELTA_RESULT" else (summary.valuable_leads, 0)
            summary.valuable_lead_count = len(summary.valuable_leads)
            for source in summary.sources:
                if source.get("provider") == "discovery":
                    source["valuable_leads"] = summary.valuable_leads
                    source["valuable_lead_count"] = summary.valuable_lead_count
            summary.candidate_count = len(reportable)
            summary.projects_found = len(reportable)
            summary.open_count = sum(1 for project, _, _ in reportable if project.status == "OPEN")
            summary.unknown_count = sum(1 for project, _, _ in reportable if project.status == "UNKNOWN")
            summary.closed_count = sum(1 for project, _, _ in reportable if project.status == "CLOSED")
            summary.new_candidate_count = sum(1 for project, _, _ in recalled if project.lifecycle_state == "NEW")
            summary.updated_candidate_count = sum(1 for project, _, _ in recalled if project.lifecycle_state in {"UPDATED", "REOPENED"})
            summary.reopened_candidate_count = sum(1 for project, _, _ in recalled if project.lifecycle_state == "REOPENED")
            summary.coverage_manifest = build_coverage_manifest(
                summary.source_plan,
                summary.sources,
                errors=summary.errors,
                query_count=summary.query_count,
                candidate_yield=len(recalled) + len(discovery_candidate_ids),
                enrichment=enrichment_report,
            )
            self._write_outputs(session, request, session_id, summary, recalled, reportable, candidate_pool=candidate_pool)
            row = session.get(SearchSession, session_id)
            row.finished_at = now_shanghai()
            # A run with blocked/manual or unconfigured sources is a valid
            # partial run, never a false claim of complete national coverage.
            incomplete_coverage = bool(
                summary.errors
                or summary.manual_action_sources
                or summary.coverage_manifest.get("failed")
                or summary.coverage_manifest.get("blocked")
                or summary.coverage_manifest.get("adapter_not_configured")
            )
            row.status = "PARTIAL" if incomplete_coverage else "COMPLETED"
            row.request_id = request.request_id
            row.search_mode = summary.search_mode
            row.result_mode = summary.result_mode
            row.sources_planned = summary.sources_planned
            row.sources_completed = summary.sources_completed
            row.sources_failed = summary.sources_failed
            row.queries_generated = summary.query_count
            row.query_count = summary.query_count
            row.candidate_count = summary.candidate_count
            row.projects_found = summary.projects_found
            row.open_count = summary.open_count
            row.unknown_count = summary.unknown_count
            row.closed_count = summary.closed_count
            row.review_count = summary.review_count
            row.verification_count = summary.verification_count
            row.candidate_pool_count = summary.candidate_pool_count
            row.new_candidate_count = summary.new_candidate_count
            row.updated_candidate_count = summary.updated_candidate_count
            row.reopened_candidate_count = summary.reopened_candidate_count
            row.enrichment_count = summary.enrichment_count
            row.coverage_manifest_json = json.dumps(summary.coverage_manifest, ensure_ascii=False, default=str)
            row.quality_metrics_json = json.dumps(summary.quality_metrics, ensure_ascii=False, default=str)
            row.errors_json = json.dumps(summary.errors, ensure_ascii=False)
            row.sources_json = json.dumps(summary.sources, ensure_ascii=False, default=str)
            row.source_plan_json = json.dumps(summary.source_plan, ensure_ascii=False)
        return summary

    def run(self, request: SearchRequest, *, dry_run: bool = False) -> SearchSummary:
        return self._run_recall_enrichment(request, dry_run=dry_run)
        session_id = f"search_{now_shanghai().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
        summary = SearchSummary(session_id=session_id, request=request.to_dict(), dry_run=dry_run)
        plan = self.plan(request)
        summary.source_plan = list(plan.get("source_plan", []))
        summary.sources_planned = len(plan.get("crawl_sources", []))
        if dry_run:
            summary.query_count = int(plan["crawl_query_count_estimate"] + plan["discovery_query_count_estimate"])
            summary.sources = [dict(item) for item in plan.get("source_plan", []) if item.get("selected")]
            summary.manual_action_sources = [
                dict(item)
                for item in plan.get("source_plan", [])
                if item.get("selected") and item.get("manual_action_required")
            ]
            summary.errors = []
            return summary

        self._create_session(request, session_id, plan)
        terms = tuple(plan.get("query_terms", []))
        try:
            crawl = CrawlRunner(database=str(self.engine.url)).run(
                source_ids=tuple(plan.get("crawl_sources", [])),
                profile_id=request.profile_id,
                since_days=request.days,
                max_items=200 if request.deep else 80,
                query_terms=terms,
                download_attachments=True,
                only_active_opportunities=False,
            )
            summary.query_count += sum(item.query_count for item in crawl.sources)
            for item in crawl.sources:
                if item.error:
                    action = (
                        f"；需要人工处理：{item.manual_action_type or item.health_reason}"
                        if item.manual_action_required and "需要人工处理" not in item.error
                        else ""
                    )
                    summary.errors.append(f"{item.source_id}: {item.error}{action}")
                source_payload = dict(item.__dict__)
                summary.sources.append(source_payload)
                if item.manual_action_required:
                    summary.manual_action_sources.append(source_payload)
            summary.sources_completed += sum(1 for item in crawl.sources if not item.error)
            summary.sources_failed += sum(1 for item in crawl.sources if item.error or item.failures)
        except Exception as exc:
            summary.errors.append(f"crawl: {exc}")
            summary.sources_failed = summary.sources_planned
        if request.discovery or request.deep:
            try:
                discovery_limit = int(plan.get("discovery_query_count_estimate", 0))
                discovery_queries = tuple(
                    f'"{item}" 招标 采购' for item in terms[:discovery_limit]
                )
                if discovery_queries:
                    discovery = DiscoveryRunner(database=str(self.engine.url)).run(
                        profile_id=request.profile_id,
                        max_queries=discovery_limit,
                        max_results=plan.get("max_results_per_query", 8),
                        custom_queries=discovery_queries,
                        wechat_enabled=request.wechat or request.deep,
                    )
                    summary.query_count += discovery.query_count
                    summary.errors.extend(discovery.errors)
                    discovery_source = {
                        "provider": "discovery",
                        "result_count": discovery.result_count,
                        "wechat_candidates": discovery.wechat_candidate_count,
                        "secondary_lead_count": discovery.secondary_lead_count,
                        "secondary_trace_query_count": discovery.secondary_trace_query_count,
                        "secondary_official_match_count": discovery.secondary_official_match_count,
                        "secondary_unresolved_count": discovery.secondary_unresolved_count,
                        "secondary_trace_skipped_count": discovery.secondary_trace_skipped_count,
                        "secondary_blocked_count": discovery.secondary_blocked_count,
                        "official_trace_matches": discovery.secondary_trace_matches,
                        "valuable_lead_count": discovery.valuable_lead_count,
                        "valuable_leads": discovery.valuable_leads,
                        "status": "ACTIVE" if not discovery.errors else "DEGRADED",
                        "manual_action_required": discovery.manual_action_required,
                        "manual_action_type": discovery.manual_action_type,
                        "manual_action_provider": discovery.manual_action_provider,
                        "last_http_status": discovery.manual_action_http_status,
                        "manual_action_url": discovery.manual_action_url,
                        "manual_browser": discovery.manual_browser,
                    }
                    summary.official_trace_matches.extend(discovery.secondary_trace_matches)
                    for lead in discovery.valuable_leads:
                        if lead not in summary.valuable_leads:
                            summary.valuable_leads.append(lead)
                    summary.valuable_lead_count = len(summary.valuable_leads)
                    summary.sources.append(discovery_source)
                    if discovery.manual_action_required:
                        summary.manual_action_sources.append({
                            "source_id": f"provider:{discovery.manual_action_provider or 'discovery'}",
                            "source_name": f"Discovery Provider {discovery.manual_action_provider or 'discovery'}",
                            "status": "NEEDS_ATTENTION",
                            "health_reason": discovery.manual_action_type,
                            "manual_action_required": True,
                            "manual_action_type": discovery.manual_action_type,
                            "last_http_status": discovery.manual_action_http_status,
                            "manual_action_url": discovery.manual_action_url,
                            "manual_browser": discovery.manual_browser,
                            "error": "搜索服务需要人工登录、验证码或浏览器验证",
                        })
            except Exception as exc:
                summary.errors.append(f"discovery: {exc}")
        try:
            ExtractionRunner(database=str(self.engine.url)).run(sample_size=30, dry_run=False, consolidate=False, reuse_cached=True)
        except Exception as exc:
            summary.errors.append(f"extract: {exc}")
        with session_scope(self.engine) as session:
            selected = self._select_projects(session, request)
            for project, announcement, matched in selected:
                ensure_review_item(session, project, announcement=announcement, search_session_id=session_id)
                exists = session.scalar(select(SearchSessionProject).where(SearchSessionProject.session_id == session_id, SearchSessionProject.project_id == project.project_id, SearchSessionProject.announcement_id == (announcement.id if announcement else None)))
                if exists is None:
                    session.add(
                        SearchSessionProject(
                            session_id=session_id,
                            project_id=project.project_id,
                            announcement_id=announcement.id if announcement else None,
                            found_via="crawl_and_discovery",
                            match_score=100.0 if matched else 50.0,
                            matched_keywords=json.dumps(matched, ensure_ascii=False),
                            matched_region=" / ".join(item for item in (project.province, project.city, project.county) if item),
                            status_at_search=project.status,
                            is_new=project.lifecycle_state == "NEW",
                            is_updated=project.lifecycle_state in {"UPDATED", "REOPENED"},
                            is_reopened=project.lifecycle_state == "REOPENED",
                        )
                    )
            # session factory 明确关闭 autoflush；已有 Review Item 的
            # search_session_id 是本轮刚更新的，写交接文件前必须让查询看到它。
            session.flush()
            reportable, suppressed_ids = self._filter_repeated_unchanged(session, request, session_id, selected)
            summary.suppressed_project_ids = suppressed_ids
            summary.suppressed_unchanged_count = len(suppressed_ids)
            summary.valuable_leads, summary.suppressed_valuable_lead_count = self._filter_repeated_unchanged_valuable_leads(
                session,
                request,
                session_id,
                summary.valuable_leads,
            )
            summary.valuable_lead_count = len(summary.valuable_leads)
            for source in summary.sources:
                if source.get("provider") == "discovery":
                    source["valuable_leads"] = summary.valuable_leads
                    source["valuable_lead_count"] = summary.valuable_lead_count
            summary.candidate_count = len(reportable)
            summary.projects_found = summary.candidate_count
            summary.open_count = sum(1 for project, _, _ in reportable if project.status == "OPEN")
            summary.unknown_count = sum(1 for project, _, _ in reportable if project.status == "UNKNOWN")
            summary.closed_count = sum(1 for project, _, _ in reportable if project.status == "CLOSED")
            self._write_outputs(session, request, session_id, summary, selected, reportable)
            row = session.get(SearchSession, session_id)
            row.finished_at = now_shanghai()
            row.status = "COMPLETED" if not summary.errors else "PARTIAL"
            row.request_id = request.request_id
            row.sources_planned = summary.sources_planned
            row.sources_completed = summary.sources_completed
            row.sources_failed = summary.sources_failed
            row.queries_generated = summary.query_count
            row.query_count = summary.query_count
            row.candidate_count = summary.candidate_count
            row.projects_found = summary.projects_found
            row.open_count = summary.open_count
            row.unknown_count = summary.unknown_count
            row.closed_count = summary.closed_count
            row.review_count = summary.review_count
            row.verification_count = summary.verification_count
            row.errors_json = json.dumps(summary.errors, ensure_ascii=False)
            row.sources_json = json.dumps(summary.sources, ensure_ascii=False, default=str)
            row.source_plan_json = json.dumps(summary.source_plan, ensure_ascii=False)
        return summary


__all__ = [
    "SearchRequest", "SearchRunner", "SearchSummary", "build_source_plan", "build_coverage_manifest",
    "normalize_result_mode", "normalize_search_mode", "parse_search_text", "resolve_result_mode",
    "resolve_search_mode", "search_scope_key",
]
