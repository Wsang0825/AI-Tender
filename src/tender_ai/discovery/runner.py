"""Discovery 两阶段编排：先保存 URL 元数据，再由后续候选处理决定是否抓正文。"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select

from tender_ai.config_loader import APP_ROOT, load_search_profiles, load_yaml
from tender_ai.discovery.providers import DDGSProvider, FallbackSearchProvider, SearchProviderError, SearXNGProvider, WeixinSearchProvider
from tender_ai.discovery.queries import DiscoveryQuery, generate_discovery_queries
from tender_ai.discovery.contracts import SearchResult
from tender_ai.discovery.leads import build_valuable_lead, valuable_lead_key
from tender_ai.matching.dedupe import normalize_identity
from tender_ai.sources.browser_profiles import DEFAULT_BROWSER
from tender_ai.sources.registry import SourceRegistry
from tender_ai.status.time import now_shanghai
from tender_ai.storage.database import create_engine_for, initialize_database, session_scope
from tender_ai.storage.models import DiscoveredSource, SearchQuery
from tender_ai.urls import canonicalize_url


@dataclass
class DiscoverySummary:
    profile_id: str = "northwest_energy"
    query_count: int = 0
    successful_queries: int = 0
    result_count: int = 0
    new_result_count: int = 0
    new_domain_count: int = 0
    wechat_candidate_count: int = 0
    total_domain_count: int = 0
    total_wechat_candidate_count: int = 0
    errors: list[str] = field(default_factory=list)
    manual_action_required: bool = False
    manual_action_type: str | None = None
    manual_action_provider: str | None = None
    manual_action_http_status: int | None = None
    manual_action_url: str | None = None
    manual_browser: str = DEFAULT_BROWSER
    secondary_lead_count: int = 0
    secondary_trace_query_count: int = 0
    secondary_official_match_count: int = 0
    secondary_unresolved_count: int = 0
    secondary_trace_skipped_count: int = 0
    secondary_blocked_count: int = 0
    secondary_trace_matches: list[dict[str, Any]] = field(default_factory=list)
    valuable_lead_count: int = 0
    valuable_leads: list[dict[str, Any]] = field(default_factory=list)
    candidate_results: list[SearchResult] = field(default_factory=list)


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower().split(":", 1)[0]


def _domain_key(domain: str) -> str:
    return domain.lower().removeprefix("www.")


def _looks_like_project(title: str, snippet: str) -> bool:
    text = f"{title} {snippet}"
    return any(word in text for word in ("招标", "采购", "询比", "资格预审", "EPC", "中标")) and any(word in text for word in ("光伏", "风电", "储能", "新能源"))


def _is_candidate_result(result: SearchResult, valuable_lead: dict[str, Any] | None = None) -> bool:
    """严格招采候选之外，保留需要单独上报的项目级价值线索。"""

    return _looks_like_project(result.title, result.snippet) or valuable_lead is not None


def _guess_level(domain: str, title: str, snippet: str) -> str:
    text = f"{title} {snippet}"
    if domain.endswith(".gov.cn") or domain.endswith(".gov"):
        return "A"
    if domain == "mp.weixin.qq.com":
        return "B"
    if any(word in text for word in ("招标代理", "代理机构")):
        return "C"
    if any(word in domain for word in ("bjx", "北极星", "能源界", "索比")):
        return "D"
    return "E"


def _known_domains(registry: SourceRegistry) -> set[str]:
    return {_domain_key(_domain(item.base_url)) for item in registry.definitions if item.base_url} | {"ctbpsp.com"}


def _is_official_domain(domain: str, known_domains: set[str]) -> bool:
    key = _domain_key(domain)
    return key in known_domains or key.endswith(".gov.cn") or key.endswith(".gov") or key.endswith(".mil.cn")


def _is_official_result(result: SearchResult, known_domains: set[str]) -> bool:
    return _is_official_domain(_domain(result.url), known_domains)


def _secondary_identity(result: SearchResult) -> str:
    metadata = result.metadata or {}
    for key in ("project_name", "projectname", "项目名称", "project", "name"):
        value = str(metadata.get(key) or "").strip()
        if len(value) >= 4:
            return re.sub(r"\s+", " ", value)[:120]
    text = f"{result.title} {result.snippet}"
    for pattern in (
        r"(?:项目名称|项目名|工程名称|采购项目名称)\s*[：:]\s*([^，。；;]{4,120})",
        r"(?:项目名称|项目名|工程名称|采购项目名称)\s+([^，。；;]{4,120})",
    ):
        match = re.search(pattern, text, re.I)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip()[:120]
    # 二手标题通常把“招标公告/采购公告/中标结果”等放在项目名后面。
    title = re.sub(r"^[【\[][^】\]]+[】\]]\s*", "", result.title).strip()
    title = re.split(r"\s*[-|｜丨_]\s*", title, maxsplit=1)[0]
    title = re.sub(r"(?:招标|采购|询比|资格预审|中标)(?:公告|结果|候选人公示)?\s*$", "", title).strip()
    return title[:120]


def _secondary_codes(result: SearchResult) -> list[str]:
    text = f"{result.title} {result.snippet}"
    values: list[str] = []
    metadata = result.metadata or {}
    for key in ("tender_code", "tendercode", "project_code", "projectnum", "message_no", "招标编号", "项目编号"):
        value = str(metadata.get(key) or "").strip()
        if value and value not in values:
            values.append(value)
    for pattern in (r"(?:招标编号|项目编号|采购编号)\s*[：:]\s*([A-Za-z0-9][A-Za-z0-9._/-]{3,80})",):
        for match in re.finditer(pattern, text, re.I):
            value = match.group(1).strip()
            if value not in values:
                values.append(value)
    return values[:2]


def _secondary_owner(result: SearchResult) -> str:
    metadata = result.metadata or {}
    for key in ("owner", "purchaser", "tenderer", "招标人", "采购人"):
        value = str(metadata.get(key) or "").strip()
        if value:
            return value[:80]
    match = re.search(r"(?:招标人|采购人|建设单位|业主单位)\s*[：:]\s*([^，。；;]{3,80})", f"{result.title} {result.snippet}")
    return match.group(1).strip()[:80] if match else ""


def official_trace_queries(result: SearchResult) -> list[str]:
    """为 D/E 或公众号线索生成有限的官方追源查询，不把线索当事实源。"""

    name = _secondary_identity(result)
    codes = _secondary_codes(result)
    owner = _secondary_owner(result)
    queries: list[str] = []
    for code in codes:
        queries.append(f'"{code}" 招标公告')
    if name and len(normalize_identity(name)) >= 4:
        queries.extend((f'"{name}" 招标公告', f'"{name}" 采购公告'))
        if owner:
            queries.append(f'"{name}" "{owner}"')
    return list(dict.fromkeys(queries))[:3]


def _is_secondary_lead(result: SearchResult, known_domains: set[str]) -> bool:
    domain = _domain(result.url)
    # 公众号属于 B 级线索，但仍必须按项目身份追查法定/官方公告。
    return _is_candidate_result(result, build_valuable_lead(result)) and (
        domain == "mp.weixin.qq.com" or not _is_official_domain(domain, known_domains)
    )


def _identity_matches(lead: SearchResult, candidate: SearchResult) -> bool:
    candidate_text = normalize_identity(f"{candidate.title} {candidate.snippet}")
    for code in _secondary_codes(lead):
        if normalize_identity(code) and normalize_identity(code) in candidate_text:
            return True
    name = normalize_identity(_secondary_identity(lead))
    if name and name in candidate_text:
        return True
    # 搜索引擎常截断标题；项目名较长时用头部和尾部的稳定片段辅助判断。
    if len(name) >= 12:
        return name[:10] in candidate_text and name[-6:] in candidate_text
    return False


def _result_notes(result: SearchResult, valuable_lead: dict[str, Any] | None = None) -> str:
    payload: dict[str, Any] = {
        "snippet": (result.snippet or "")[:1000],
        "title": result.title[:256],
        "provider": result.provider,
        "published_at": result.published_at,
    }
    if valuable_lead is not None:
        payload["valuable_lead"] = valuable_lead
    return json.dumps(payload, ensure_ascii=False)


def _merge_notes(row: DiscoveredSource, values: dict[str, Any]) -> None:
    current: dict[str, Any]
    try:
        parsed = json.loads(row.notes or "")
        current = parsed if isinstance(parsed, dict) else {"snippet": row.notes}
    except (TypeError, ValueError):
        current = {"snippet": row.notes} if row.notes else {}
    current.update(values)
    row.notes = json.dumps(current, ensure_ascii=False)


def _register_valuable_lead(summary: DiscoverySummary, lead: dict[str, Any]) -> None:
    """在一次 Discovery 中去重记录，但不合并不同来源的出处。"""

    key = valuable_lead_key(lead)
    if any(valuable_lead_key(item) == key for item in summary.valuable_leads):
        return
    summary.valuable_leads.append(lead)
    summary.valuable_lead_count = len(summary.valuable_leads)


class DiscoveryRunner:
    def __init__(self, *, database: str | None = None):
        self.engine = initialize_database(create_engine_for(database))

    def plan(self, *, profile_id: str = "northwest_energy", max_queries: int | None = None) -> list[str]:
        profile = load_search_profiles().get(profile_id)
        return [item.text for item in generate_discovery_queries(max_queries=max_queries or profile.query_budget, profile_id=profile_id)]

    @staticmethod
    def _provider() -> FallbackSearchProvider:
        # Provider 由 YAML 控制；没有启用可用 Provider 时保持失败可见，不会偷偷切换后台任务。
        providers: list[object] = []
        try:
            configured = load_yaml("search_providers.yaml").get("providers") or []
        except Exception:
            configured = []
        for item in sorted(configured, key=lambda row: int(row.get("priority", 9))):
            if not item.get("enabled", False):
                continue
            name = str(item.get("name") or "").lower()
            if name == "ddgs":
                provider = DDGSProvider()
            elif name == "searxng":
                provider = SearXNGProvider()
            else:
                continue
            provider.cooldown = float(item.get("cooldown_seconds", 0) or 0)
            providers.append(provider)
        if not providers and not configured:
            providers = [DDGSProvider()]
        return FallbackSearchProvider(providers)

    @staticmethod
    def _provider_enabled(provider_name: str, *, default: bool = True) -> bool:
        """读取 Provider 开关；未配置时保持兼容默认值。"""

        try:
            configured = load_yaml("search_providers.yaml").get("providers") or []
        except Exception:
            return default
        rows = [item for item in configured if str(item.get("name") or "").lower() == provider_name.lower()]
        if not rows:
            return default
        return bool(rows[0].get("enabled", default))

    @staticmethod
    def _record_provider_error(
        summary: DiscoverySummary,
        provider_name: str,
        error: SearchProviderError,
        *,
        context: str | None = None,
    ) -> None:
        label = provider_name if not context else f"{provider_name} ({context})"
        summary.errors.append(f"{label}: {error}")
        if not error.manual_action_required:
            return
        summary.manual_action_required = True
        summary.manual_action_type = error.manual_action_type or "MANUAL_ACTION_REQUIRED"
        summary.manual_action_provider = provider_name
        summary.manual_action_http_status = error.http_status
        summary.manual_action_url = error.url
        if getattr(summary, "_manual_alert_emitted", False):
            return
        setattr(summary, "_manual_alert_emitted", True)
        print(
            f"[AI-Tender][人工处理提醒] Discovery Provider {label}："
            f"{summary.manual_action_type}；"
            f"HTTP {summary.manual_action_http_status or '未知'}；{error}\n"
            f"人工验证浏览器：{summary.manual_browser}\n"
            f"打开地址：{summary.manual_action_url or '未提供'}\n"
            "请人工完成登录、验证码或浏览器安全验证；完成后回复‘已完成人工验证’，再重试 Discovery。",
            file=sys.stderr,
            flush=True,
        )

    def run(
        self,
        *,
        profile_id: str = "northwest_energy",
        max_queries: int | None = None,
        max_results: int | None = None,
        rotation_day: int | None = None,
        custom_queries: tuple[str, ...] | None = None,
        wechat_enabled: bool | None = None,
    ) -> DiscoverySummary:
        profile = load_search_profiles().get(profile_id)
        if custom_queries:
            queries = [DiscoveryQuery(text=item, category="user_search", region=None, priority=1) for item in dict.fromkeys(custom_queries) if item.strip()]
            query_budget = max_queries or profile.query_budget
        else:
            query_budget = max_queries or profile.query_budget
            queries = generate_discovery_queries(max_queries=query_budget, rotation_day=rotation_day, profile_id=profile_id)
        # 为二手线索官方追源预留少量预算；不会把整批低质量搜索结果再逐条深挖。
        trace_budget = min(8, max(0, query_budget // 3))
        if custom_queries and trace_budget:
            queries = queries[: max(1, query_budget - trace_budget)]
        else:
            queries = queries[:query_budget]
        summary = DiscoverySummary(profile_id=profile_id, query_count=len(queries))
        if not profile.discovery_enabled:
            self._write_discovery_report(summary)
            return summary
        result_limit = max_results or profile.max_results_per_query
        provider = self._provider()
        wechat_allowed = profile.wechat_discovery_enabled if wechat_enabled is None else wechat_enabled
        wechat_allowed = wechat_allowed and self._provider_enabled("weixin_public_index", default=True)
        weixin = WeixinSearchProvider(provider) if wechat_allowed else None
        registry = SourceRegistry.from_file()
        known = _known_domains(registry)
        new_domains: set[str] = set()
        seen_urls: set[str] = set()
        trace_attempted: set[str] = set()
        try:
            with session_scope(self.engine) as session:
                for plan in queries:
                    row = session.scalar(select(SearchQuery).where(SearchQuery.query_text == plan.text, SearchQuery.source_id.is_(None), SearchQuery.profile_id == profile_id))
                    if row is None:
                        row = SearchQuery(query_text=plan.text, category=plan.category, region=plan.region, profile_id=profile_id, priority=plan.priority)
                        session.add(row)
                        session.flush()
                    row.last_run_at = now_shanghai()
                    row.run_count = (row.run_count or 0) + 1
                    try:
                        results = provider.search(plan.text, max_results=result_limit)
                        if plan.category == "weixin_candidate" and weixin is not None:
                            results.extend(weixin.search(plan.text, max_results=result_limit))
                        summary.successful_queries += 1
                        row.last_success_at = now_shanghai()
                        row.last_error = None
                    except SearchProviderError as exc:
                        self._record_provider_error(summary, provider.name, exc, context=plan.text)
                        row.last_error = str(exc)[:1000]
                        row.new_results_count = 0
                        continue
                    row.results_count = len(results)
                    row.new_results_count = 0
                    candidate_count = 0
                    for result in results:
                        canonical = canonicalize_url(result.url)
                        if not canonical or canonical in seen_urls:
                            continue
                        seen_urls.add(canonical)
                        summary.result_count += 1
                        domain = _domain(canonical)
                        is_wechat = domain == "mp.weixin.qq.com"
                        if is_wechat:
                            summary.wechat_candidate_count += 1
                        discovered = session.scalar(select(DiscoveredSource).where(DiscoveredSource.source_url == result.url))
                        valuable_lead = build_valuable_lead(
                            result,
                            canonical_url=canonical,
                            source_level=_guess_level(domain, result.title, result.snippet),
                            region=plan.region,
                        )
                        candidate = _is_candidate_result(result, valuable_lead)
                        if valuable_lead is not None:
                            _register_valuable_lead(summary, valuable_lead)
                        if discovered is None:
                            discovered = DiscoveredSource(
                                source_url=result.url,
                                original_url=result.url,
                                canonical_url=canonical,
                                source_name=result.title[:256],
                                domain=domain,
                                discovery_method=result.provider,
                                region=plan.region,
                                projects_found=1 if candidate else 0,
                                source_level_guess=_guess_level(domain, result.title, result.snippet),
                                confidence=0.7 if candidate else 0.35,
                                status="DISCOVERED",
                                last_checked_at=now_shanghai(),
                                last_seen_at=now_shanghai(),
                                notes=_result_notes(result, valuable_lead),
                            )
                            session.add(discovered)
                            row.new_results_count += 1
                            summary.new_result_count += 1
                            if domain not in known:
                                new_domains.add(domain)
                        else:
                            discovered.last_checked_at = now_shanghai()
                            discovered.last_seen_at = now_shanghai()
                            if candidate and discovered.projects_found == 0:
                                discovered.projects_found = 1
                            if valuable_lead is not None:
                                _merge_notes(discovered, {"valuable_lead": valuable_lead})
                        if candidate:
                            summary.candidate_results.append(result)
                            candidate_count += 1
                        if _is_secondary_lead(result, known):
                            summary.secondary_lead_count += 1
                            lead_key = canonical
                            if lead_key in trace_attempted:
                                continue
                            trace_attempted.add(lead_key)
                            if trace_budget <= 0:
                                summary.secondary_trace_skipped_count += 1
                                if discovered is not None:
                                    _merge_notes(
                                        discovered,
                                        {
                                            "secondary_lead": True,
                                            "official_trace": {"status": "BUDGET_SKIPPED", "queries": [], "official_matches": []},
                                        },
                                    )
                                continue
                            trace_budget -= 1
                            trace = self._trace_secondary_lead(
                                session,
                                provider,
                                result,
                                plan.region,
                                seen_urls,
                                summary,
                                new_domains,
                                known,
                                result_limit,
                            )
                            if discovered is not None:
                                _merge_notes(
                                    discovered,
                                    {
                                        "secondary_lead": True,
                                        "official_trace": trace,
                                    },
                                )
                    row.new_project_count = (row.new_project_count or 0) + candidate_count
                    if candidate_count:
                        row.priority = max(1, row.priority - 1)
                    elif not results:
                        row.priority = min(9, row.priority + 1)
                    if plan.category == "weixin_candidate" and weixin is not None:
                        for result in results[:3]:
                            for follow_up in WeixinSearchProvider.follow_up_queries(result)[:2]:
                                try:
                                    follow_results = provider.search(f'"{follow_up}"', max_results=3)
                                except SearchProviderError as exc:
                                    self._record_provider_error(summary, provider.name, exc, context=f"公众号追源 {follow_up}")
                                    continue
                                self._store_results(session, follow_results, plan.region, seen_urls, summary, new_domains, known, method="weixin_follow_up")
                rows = list(session.scalars(select(DiscoveredSource)).all())
                domain_counts: dict[str, int] = {}
                for item in rows:
                    if item.domain:
                        domain_counts[item.domain] = domain_counts.get(item.domain, 0) + item.projects_found
                for item in rows:
                    if item.domain not in known and domain_counts.get(item.domain or "", 0) >= 3:
                        item.status = "RECOMMENDED_NEW_SOURCE"
                    elif item.domain in known and item.status == "RECOMMENDED_NEW_SOURCE":
                        item.status = "DISCOVERED"
                summary.total_domain_count = len({item.domain for item in rows if item.domain})
                summary.total_wechat_candidate_count = sum(1 for item in rows if item.domain == "mp.weixin.qq.com")
        finally:
            if weixin is not None:
                weixin.close()
            provider.close()
        summary.new_domain_count = len(new_domains)
        summary.valuable_lead_count = len(summary.valuable_leads)
        self._write_discovery_report(summary)
        return summary

    def _trace_secondary_lead(
        self,
        session: Any,
        provider: object,
        lead: SearchResult,
        region: str | None,
        seen_urls: set[str],
        summary: DiscoverySummary,
        new_domains: set[str],
        known: set[str],
        result_limit: int,
    ) -> dict[str, Any]:
        queries = official_trace_queries(lead)
        trace_results: list[SearchResult] = []
        official_matches: list[dict[str, Any]] = []
        blocked = False
        if not queries:
            summary.secondary_unresolved_count += 1
            return {"status": "NO_IDENTITY", "queries": [], "official_matches": []}
        for query in queries:
            summary.query_count += 1
            summary.secondary_trace_query_count += 1
            try:
                rows = provider.search(query, max_results=min(5, result_limit))
                summary.successful_queries += 1
            except SearchProviderError as exc:
                if exc.manual_action_required:
                    summary.secondary_blocked_count += 1
                    blocked = True
                self._record_provider_error(summary, getattr(provider, "name", "discovery"), exc, context=f"二手线索官方追源 {query}")
                break
            trace_results.extend(rows)
            for candidate in rows:
                if not _is_official_result(candidate, known) or not _identity_matches(lead, candidate):
                    continue
                match = {
                    "title": candidate.title,
                    "url": candidate.url,
                    "domain": _domain(candidate.url),
                    "provider": candidate.provider,
                    "published_at": candidate.published_at,
                    "source_level": "A" if _domain(candidate.url).endswith((".gov.cn", ".gov", ".mil.cn")) else "B",
                }
                if match not in official_matches:
                    official_matches.append(match)
            # 首个精确官方命中已经完成追源；继续追查会浪费配额。
            if official_matches:
                break
        if trace_results:
            self._store_results(session, trace_results, region, seen_urls, summary, new_domains, known, method="secondary_official_trace")
        if official_matches:
            summary.secondary_official_match_count += 1
            status = "FOUND_OFFICIAL"
        elif blocked:
            summary.secondary_unresolved_count += 1
            status = "BLOCKED"
        else:
            summary.secondary_unresolved_count += 1
            status = "NOT_FOUND"
        trace_payload = {
            "status": status,
            "queries": queries,
            "official_matches": official_matches,
            "lead_title": lead.title,
            "lead_url": lead.url,
        }
        for match in official_matches:
            summary.secondary_trace_matches.append(
                {"lead_title": lead.title, "lead_url": lead.url, **match}
            )
        return trace_payload

    @staticmethod
    def _store_results(session, results: list[SearchResult], region: str | None, seen_urls: set[str], summary: DiscoverySummary, new_domains: set[str], known: set[str], *, method: str) -> None:
        for result in results:
            canonical = canonicalize_url(result.url)
            if not canonical or canonical in seen_urls:
                continue
            seen_urls.add(canonical)
            domain = _domain(canonical)
            summary.result_count += 1
            if domain == "mp.weixin.qq.com":
                summary.wechat_candidate_count += 1
            valuable_lead = build_valuable_lead(
                result,
                canonical_url=canonical,
                source_level=_guess_level(domain, result.title, result.snippet),
                region=region,
            )
            candidate = _is_candidate_result(result, valuable_lead)
            if candidate:
                summary.candidate_results.append(result)
            if valuable_lead is not None:
                _register_valuable_lead(summary, valuable_lead)
            discovered = session.scalar(select(DiscoveredSource).where(DiscoveredSource.source_url == result.url))
            if discovered is None:
                session.add(DiscoveredSource(source_url=result.url, original_url=result.url, canonical_url=canonical, source_name=result.title[:256], domain=domain, discovery_method=method, region=region, projects_found=1 if candidate else 0, source_level_guess=_guess_level(domain, result.title, result.snippet), confidence=0.65, status="DISCOVERED", last_checked_at=now_shanghai(), last_seen_at=now_shanghai(), notes=_result_notes(result, valuable_lead)))
                summary.new_result_count += 1
                if domain not in known:
                    new_domains.add(domain)
            else:
                discovered.last_checked_at = now_shanghai()
                discovered.last_seen_at = now_shanghai()
                if candidate and discovered.projects_found == 0:
                    discovered.projects_found = 1
                if valuable_lead is not None:
                    _merge_notes(discovered, {"valuable_lead": valuable_lead})

    @staticmethod
    def _write_discovery_report(summary: DiscoverySummary) -> None:
        report_path = APP_ROOT.parent / "CRAWL_REPORT.md"
        if not report_path.exists():
            return
        current = report_path.read_text(encoding="utf-8")
        marker = "## Discovery"
        if marker in current:
            current = current.split(marker, 1)[0].rstrip()
        block = "\n\n## Discovery\n\n" + "\n".join([
            f"- Profile：{summary.profile_id}", f"- 查询数：{summary.query_count}", f"- 成功查询数：{summary.successful_queries}",
            f"- 搜索结果数：{summary.result_count}", f"- 新结果数：{summary.new_result_count}", f"- 新发现域名数：{summary.new_domain_count}",
            f"- 公众号候选数：{summary.wechat_candidate_count}", f"- 累计发现域名数：{summary.total_domain_count}",
            f"- 累计公众号候选数：{summary.total_wechat_candidate_count}", f"- 错误数：{len(summary.errors)}",
            f"- 二手/公众号线索数：{summary.secondary_lead_count}",
            f"- 官方追源查询数：{summary.secondary_trace_query_count}",
            f"- 官方命中线索数：{summary.secondary_official_match_count}",
            f"- 未找到或待确认线索数：{summary.secondary_unresolved_count}",
            f"- 因预算跳过追源数：{summary.secondary_trace_skipped_count}",
            f"- 官方追源命中链接数：{len(summary.secondary_trace_matches)}",
            f"- 有价值但非直接组件支架采购线索数：{summary.valuable_lead_count}",
        ]) + "\n"
        if summary.manual_action_required:
            block += "\n## 需要人工处理的 Discovery\n\n"
            block += (
                f"- Provider：{summary.manual_action_provider or '未知'}\n"
                f"- 动作：{summary.manual_action_type or 'MANUAL_ACTION_REQUIRED'}\n"
                f"- HTTP：{summary.manual_action_http_status or '未知'}\n"
                f"- 人工验证浏览器：{summary.manual_browser}\n"
                f"- 打开地址：{summary.manual_action_url or '未提供'}\n"
                "- 处理：请人工完成登录、验证码或浏览器验证后再重试；本次 Discovery 不视为完整覆盖\n"
            )
        if summary.valuable_leads:
            block += "\n## 有价值但非直接组件支架采购线索\n\n"
            block += "以下线索必须单独上报，不能改写成组件支架采购公告或当前 OPEN 标的：\n\n"
            for lead in summary.valuable_leads:
                block += (
                    f"- {lead.get('lead_label') or lead.get('lead_type')}: {lead.get('project_identity') or lead.get('source_title') or '未知项目'}\n"
                    f"  - 价值线索原因：{lead.get('reason') or ''}\n"
                    f"  - 范围说明：{lead.get('scope_warning') or ''}\n"
                    f"  - 是否直接组件支架采购：否（当前证据未建立）\n"
                    f"  - 地区：{lead.get('region') or '未知'}；来源等级：{lead.get('source_level') or '未知'}；来源：{lead.get('source_domain') or '未知'}\n"
                    f"  - 原始标题：{lead.get('source_title') or ''}\n"
                    f"  - 原始 URL：{lead.get('source_url') or ''}\n"
                    f"  - 原文摘要：{lead.get('source_text') or '未提供'}\n"
                    f"  - 后续追踪：{'；'.join(lead.get('follow_up_queries') or []) or '需人工补充项目身份'}\n"
                )
        report_path.write_text(current + block, encoding="utf-8")


__all__ = ["DiscoveryRunner", "DiscoverySummary", "official_trace_queries"]
