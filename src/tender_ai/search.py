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
from uuid import uuid4

from sqlalchemy import select

from tender_ai.config_loader import APP_ROOT, RegionRegistry, load_industry_profiles, load_region_catalog, load_search_profiles
from tender_ai.crawlers.runner import CrawlRunner
from tender_ai.discovery.runner import DiscoveryRunner
from tender_ai.extractors.runner import ExtractionRunner
from tender_ai.matching.dedupe import normalize_identity
from tender_ai.review import ensure_review_item, review_item_dict, write_review_files
from tender_ai.status.engine import recalculate_status
from tender_ai.status.time import as_shanghai, now_shanghai, parse_datetime
from tender_ai.storage.database import create_engine_for, initialize_database, session_scope
from tender_ai.storage.models import Announcement, CodexReviewItem, Project, SearchSession, SearchSessionProject, Source
from tender_ai.storage.repository import add_status_history, project_to_record


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


@dataclass(frozen=True)
class SearchRequest:
    raw_query: str | None = None
    profile_id: str = "northwest_energy"
    region: str | None = None
    city: str | None = None
    county: str | None = None
    days: int = 30
    date_from: str | None = None
    date_to: str | None = None
    industries: tuple[str, ...] = ()
    project_types: tuple[str, ...] = ()
    equipment: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    exclude_keywords: tuple[str, ...] = ()
    source_level: str | None = None
    include_unknown: bool = False
    only_open: bool = False
    discovery: bool = False
    wechat: bool = False
    deep: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_query": self.raw_query,
            "profile_id": self.profile_id,
            "region": self.region,
            "city": self.city,
            "county": self.county,
            "days": self.days,
            "date_from": self.date_from,
            "date_to": self.date_to,
            "industry": list(self.industries),
            "project_type": list(self.project_types),
            "equipment": list(self.equipment),
            "keyword": list(self.keywords),
            "exclude_keyword": list(self.exclude_keywords),
            "source_level": self.source_level,
            "include_unknown": self.include_unknown,
            "only_open": self.only_open,
            "discovery": self.discovery,
            "wechat": self.wechat,
            "deep": self.deep,
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
    results_path: str | None = None
    review_markdown_path: str | None = None
    review_json_path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _resolve_region(text: str) -> tuple[str | None, str | None, str | None]:
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
        return text or None, None, None
    return match.province, match.city, match.county


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
    return SearchRequest(
        raw_query=raw,
        profile_id=profile_id,
        region=region,
        city=city,
        county=county,
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
    )


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _terms(request: SearchRequest) -> tuple[str, ...]:
    profile = load_search_profiles().get(request.profile_id)
    group_ids = request.industries or profile.industry_groups
    terms: list[str] = []
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
    area = " ".join(item for item in (request.region, request.city, request.county) if item)
    if area:
        terms = [f"{area} {term}" for term in terms]
    return tuple(terms[: (12 if request.deep else 8)])


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
    requested = (request.region, request.city, request.county)
    actual = (project.province, project.city, project.county)
    if not any(requested):
        return True
    for wanted, got in zip(requested, actual):
        if wanted and not (got and (normalize_identity(wanted) in normalize_identity(got) or normalize_identity(got) in normalize_identity(wanted))):
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
        actual = normalize_identity(" ".join(item for item in (project.province, project.city, project.county, project.location) if item))
        return bool(actual) and any(label in actual for label in labels)
    except Exception:
        return True


def _project_text(project: Project, announcement: Announcement | None) -> str:
    return " ".join(
        _text(getattr(project, field_name, None))
        for field_name in (
            "project_name", "raw_project_name", "owner", "purchaser", "tenderer", "agency", "industry", "project_type", "project_scale", "project_code", "tender_code", "qualification_summary", "participation_method", "location",
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
    required = (request.source_level or "").upper()
    if not required:
        return True
    ranks = {"A": 1, "B": 2, "C": 3, "D": 4, "E": 5}
    return ranks.get((project.source_level or "E").upper(), 9) <= ranks.get(required, 5)


def _status_allowed(project: Project, request: SearchRequest) -> bool:
    if request.only_open:
        return project.status == "OPEN"
    if request.include_unknown:
        return project.status in {"OPEN", "UNKNOWN"}
    return project.status == "OPEN"


def _compact_project(project: Project, announcement: Announcement | None, review_items: list[dict[str, Any]]) -> dict[str, Any]:
    def value(field_name: str) -> Any:
        item = getattr(project, field_name, None)
        return item.isoformat() if isinstance(item, datetime) else str(item) if item is not None else None

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
        "owner": project.owner,
        "agency": project.agency,
        "project_type": project.project_type,
        "industry": project.industry,
        "capacity_mw": project.capacity_mw,
        "capacity_mwh": project.capacity_mwh,
        "budget": str(project.budget) if project.budget is not None else None,
        "project_code": project.project_code,
        "tender_code": project.tender_code,
        "publish_time": value("publish_time"),
        "registration_deadline": value("registration_deadline"),
        "document_deadline": value("document_deadline"),
        "bid_deadline": value("bid_deadline"),
        "open_time": value("open_time"),
        "participation_method": project.participation_method,
        "status": project.status,
        "status_reason": project.status_reason,
        "source_level": project.source_level,
        "source_name": project.source_name,
        "source_url": (announcement.source_url if announcement else None) or project.source_url,
        "lifecycle_state": project.lifecycle_state,
        "needs_codex_review": project.needs_codex_review,
        "review_reasons": [item.get("reason") for item in review_items],
        "completeness_score": project.completeness_score,
        "overall_confidence": project.overall_confidence,
    }


class SearchRunner:
    def __init__(self, *, database: str | None = None):
        self.engine = initialize_database(create_engine_for(database))

    def plan(self, request: SearchRequest) -> dict[str, Any]:
        profile = load_search_profiles().get(request.profile_id)
        terms = _terms(request)
        crawl_plan = CrawlRunner(database=str(self.engine.url)).plan(profile_id=profile.profile_id)
        discovery_queries = DiscoveryRunner(database=str(self.engine.url)).plan(profile_id=profile.profile_id, max_queries=12 if request.deep else 6)
        return {
            "profile_id": profile.profile_id,
            "region": request.region,
            "city": request.city,
            "county": request.county,
            "query_terms": list(terms),
            "crawl_sources": crawl_plan["sources"],
            "crawl_query_count_estimate": len(crawl_plan["sources"]) * len(terms),
            "discovery_enabled": request.discovery or request.deep,
            "discovery_query_count_estimate": len(discovery_queries) if request.discovery or request.deep else 0,
            "wechat_enabled": request.wechat or request.deep,
            "dry_run": True,
        }

    def _create_session(self, request: SearchRequest, session_id: str) -> None:
        with session_scope(self.engine) as session:
            session.add(SearchSession(session_id=session_id, request_json=json.dumps(request.to_dict(), ensure_ascii=False), started_at=now_shanghai(), status="RUNNING"))

    def _select_projects(self, session: Any, request: SearchRequest) -> list[tuple[Project, Announcement | None, list[str]]]:
        lower, upper = _date_bounds(request)
        profile = load_search_profiles().get(request.profile_id)
        candidates: list[tuple[Project, Announcement | None, list[str]]] = []
        for project in session.scalars(select(Project).order_by(Project.updated_at.desc())).all():
            if not _matches_region(project, request) or not _matches_profile_regions(project, request, profile) or not _source_allowed(project, request):
                continue
            publish_time = project.publish_time
            if publish_time is not None and not (lower <= as_shanghai(publish_time) <= upper):
                continue
            announcements = list(session.scalars(select(Announcement).where(Announcement.project_id == project.project_id).order_by(Announcement.published_at.desc(), Announcement.id.desc())).all())
            announcement = announcements[0] if announcements else None
            if publish_time is None and announcement is not None and announcement.published_at is not None and not (lower <= as_shanghai(announcement.published_at) <= upper):
                continue
            haystack = _project_text(project, announcement).casefold()
            excluded = list(dict.fromkeys((*request.exclude_keywords, *profile.exclude_keywords)))
            if any(term.casefold() in haystack for term in excluded):
                continue
            matched = _matched_terms(project, announcement, request)
            required_terms = list(request.keywords) + list(request.equipment) + list(request.project_types)
            group_ids = request.industries or profile.industry_groups
            if group_ids:
                try:
                    catalog = load_industry_profiles()
                    required_terms.extend(term for group_id in group_ids for term in catalog.get(group_id).terms)
                except Exception:
                    required_terms.extend(term for group_id in group_ids for term in INDUSTRY_ALIASES.get(group_id, (group_id,)))
            if required_terms and not matched:
                continue
            record = project_to_record(project)
            decision = recalculate_status(record)
            if project.status != decision.status.value:
                old_status = project.status
                project.status = decision.status.value
                add_status_history(session, project.project_id, old_status, project.status, decision.reason, now_shanghai())
            project.status_reason = decision.reason_code
            project.status_evaluated_at = now_shanghai()
            project.status_rule_version = "status.v2"
            if not _status_allowed(project, request):
                continue
            candidates.append((project, announcement, matched))
        return candidates

    def _write_outputs(self, session: Any, request: SearchRequest, session_id: str, summary: SearchSummary, selected: list[tuple[Project, Announcement | None, list[str]]]) -> None:
        output_dir = APP_ROOT.parent / "output" / "sessions" / session_id
        output_dir.mkdir(parents=True, exist_ok=True)
        review_rows = [review_item_dict(session, item) for item in session.scalars(select(CodexReviewItem).where(CodexReviewItem.search_session_id == session_id).order_by(CodexReviewItem.priority, CodexReviewItem.created_at)).all()]
        summary.review_count = len(review_rows)
        open_projects: list[dict[str, Any]] = []
        unknown_projects: list[dict[str, Any]] = []
        all_projects: list[dict[str, Any]] = []
        for project, announcement, matched in selected:
            item_rows = [row for row in review_rows if row["project_id"] == project.project_id]
            compact = _compact_project(project, announcement, item_rows)
            compact["matched_keywords"] = matched
            compact["matched_region"] = " / ".join(item for item in (project.province, project.city, project.county) if item)
            all_projects.append(compact)
            if project.status == "OPEN":
                open_projects.append(compact)
            elif project.status == "UNKNOWN":
                unknown_projects.append(compact)
        results_payload = {
            "session": {"session_id": session_id, "request": request.to_dict(), "query_count": summary.query_count, "candidate_count": len(all_projects)},
            "open_projects": open_projects,
            "unknown_projects": unknown_projects,
            "review_items": review_rows,
            "errors": summary.errors,
            "sources": summary.sources,
        }
        results_path = output_dir / "results.json"
        results_path.write_text(json.dumps(results_payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        summary_path = output_dir / "summary.md"
        lines = [
            f"# Search Session {session_id}",
            "",
            f"搜索条件：{json.dumps(request.to_dict(), ensure_ascii=False)}",
            f"查询数量：{summary.query_count}",
            f"候选数量：{len(all_projects)}",
            f"OPEN：{len(open_projects)}",
            f"UNKNOWN：{len(unknown_projects)}",
            f"Review 数量：{len(review_rows)}",
            f"失败来源/错误：{len(summary.errors)}",
            "",
        ]
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
        summary_path.write_text("\n".join(lines), encoding="utf-8")
        review_md, review_json, _ = write_review_files(session, session_id)
        summary.output_dir = str(output_dir)
        summary.summary_path = str(summary_path)
        summary.results_path = str(results_path)
        summary.review_markdown_path = str(review_md)
        summary.review_json_path = str(review_json)

    def run(self, request: SearchRequest, *, dry_run: bool = False) -> SearchSummary:
        session_id = f"search_{now_shanghai().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
        summary = SearchSummary(session_id=session_id, request=request.to_dict(), dry_run=dry_run)
        if dry_run:
            plan = self.plan(request)
            summary.query_count = int(plan["crawl_query_count_estimate"] + plan["discovery_query_count_estimate"])
            summary.sources = [{"source_id": source_id} for source_id in plan["crawl_sources"]]
            summary.errors = []
            return summary

        self._create_session(request, session_id)
        terms = _terms(request)
        try:
            crawl = CrawlRunner(database=str(self.engine.url)).run(
                profile_id=request.profile_id,
                since_days=request.days,
                max_items=200 if request.deep else 80,
                query_terms=terms,
                download_attachments=True,
                only_active_opportunities=False,
            )
            summary.query_count += sum(item.query_count for item in crawl.sources)
            summary.errors.extend(f"{item.source_id}: {item.error}" for item in crawl.sources if item.error)
            summary.sources.extend(item.__dict__ for item in crawl.sources)
        except Exception as exc:
            summary.errors.append(f"crawl: {exc}")
        if request.discovery or request.deep:
            try:
                discovery_queries = tuple(
                    f'"{item}" 招标 采购' for item in terms[: (12 if request.deep else 6)]
                )
                discovery = DiscoveryRunner(database=str(self.engine.url)).run(
                    profile_id=request.profile_id,
                    max_queries=12 if request.deep else 6,
                    max_results=12 if request.deep else 8,
                    custom_queries=discovery_queries,
                    wechat_enabled=request.wechat or request.deep,
                )
                summary.query_count += discovery.query_count
                summary.errors.extend(discovery.errors)
                summary.sources.append({"provider": "discovery", "result_count": discovery.result_count, "wechat_candidates": discovery.wechat_candidate_count})
            except Exception as exc:
                summary.errors.append(f"discovery: {exc}")
        try:
            ExtractionRunner(database=str(self.engine.url)).run(sample_size=30, dry_run=False, consolidate=False)
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
                        )
                    )
            summary.candidate_count = len(selected)
            summary.open_count = sum(1 for project, _, _ in selected if project.status == "OPEN")
            summary.unknown_count = sum(1 for project, _, _ in selected if project.status == "UNKNOWN")
            summary.closed_count = sum(1 for project, _, _ in selected if project.status == "CLOSED")
            self._write_outputs(session, request, session_id, summary, selected)
            row = session.get(SearchSession, session_id)
            row.finished_at = now_shanghai()
            row.status = "COMPLETED" if not summary.errors else "COMPLETED_WITH_ERRORS"
            row.query_count = summary.query_count
            row.candidate_count = summary.candidate_count
            row.open_count = summary.open_count
            row.unknown_count = summary.unknown_count
            row.closed_count = summary.closed_count
            row.review_count = summary.review_count
            row.errors_json = json.dumps(summary.errors, ensure_ascii=False)
            row.sources_json = json.dumps(summary.sources, ensure_ascii=False, default=str)
        return summary


__all__ = ["SearchRequest", "SearchRunner", "SearchSummary", "parse_search_text"]
