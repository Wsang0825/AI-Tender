"""Discovery 两阶段编排：先保存 URL 元数据，再由后续候选处理决定是否抓正文。"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from sqlalchemy import select

from tender_ai.config_loader import APP_ROOT, load_search_profiles
from tender_ai.discovery.providers import DDGSProvider, FallbackSearchProvider, SearchProviderError, SearXNGProvider, WeixinSearchProvider
from tender_ai.discovery.queries import generate_discovery_queries
from tender_ai.discovery.contracts import SearchResult
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


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower().split(":", 1)[0]


def _looks_like_project(title: str, snippet: str) -> bool:
    text = f"{title} {snippet}"
    return any(word in text for word in ("招标", "采购", "询比", "资格预审", "EPC", "中标")) and any(word in text for word in ("光伏", "风电", "储能", "新能源"))


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
    return {_domain(item.base_url) for item in registry.definitions if item.base_url} | {"ctbpsp.com"}


class DiscoveryRunner:
    def __init__(self, *, database: str | None = None):
        self.engine = initialize_database(create_engine_for(database))

    def plan(self, *, profile_id: str = "northwest_energy", max_queries: int | None = None) -> list[str]:
        profile = load_search_profiles().get(profile_id)
        return [item.text for item in generate_discovery_queries(max_queries=max_queries or profile.query_budget, profile_id=profile_id)]

    @staticmethod
    def _provider() -> FallbackSearchProvider:
        # DDGS 是当前默认；SearXNG/Custom 保持可替换，不要求安装或配置即可运行。
        return FallbackSearchProvider([DDGSProvider(), SearXNGProvider()])

    def run(
        self,
        *,
        profile_id: str = "northwest_energy",
        max_queries: int | None = None,
        max_results: int | None = None,
        rotation_day: int | None = None,
    ) -> DiscoverySummary:
        profile = load_search_profiles().get(profile_id)
        queries = generate_discovery_queries(max_queries=max_queries or profile.query_budget, rotation_day=rotation_day, profile_id=profile_id)
        summary = DiscoverySummary(profile_id=profile_id, query_count=len(queries))
        if not profile.discovery_enabled:
            self._write_discovery_report(summary)
            return summary
        result_limit = max_results or profile.max_results_per_query
        provider = self._provider()
        weixin = WeixinSearchProvider(provider) if profile.wechat_discovery_enabled else None
        registry = SourceRegistry.from_file()
        known = _known_domains(registry)
        new_domains: set[str] = set()
        seen_urls: set[str] = set()
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
                        summary.errors.append(f"{plan.text}: {exc}")
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
                        candidate = _looks_like_project(result.title, result.snippet)
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
                                notes=result.snippet[:1000],
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
                        if candidate:
                            candidate_count += 1
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
                                    summary.errors.append(f"公众号追源 {follow_up}: {exc}")
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
        self._write_discovery_report(summary)
        return summary

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
            candidate = _looks_like_project(result.title, result.snippet)
            if session.scalar(select(DiscoveredSource).where(DiscoveredSource.source_url == result.url)) is None:
                session.add(DiscoveredSource(source_url=result.url, original_url=result.url, canonical_url=canonical, source_name=result.title[:256], domain=domain, discovery_method=method, region=region, projects_found=1 if candidate else 0, source_level_guess=_guess_level(domain, result.title, result.snippet), confidence=0.65, status="DISCOVERED", last_checked_at=now_shanghai(), last_seen_at=now_shanghai(), notes=result.snippet[:1000]))
                summary.new_result_count += 1
                if domain not in known:
                    new_domains.add(domain)

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
        ]) + "\n"
        report_path.write_text(current + block, encoding="utf-8")


__all__ = ["DiscoveryRunner", "DiscoverySummary"]
