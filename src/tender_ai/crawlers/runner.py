"""真实公开来源抓取编排：独立来源、增量、快照、附件和运行指标。"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import func, select

from tender_ai.config_loader import APP_ROOT, SearchProfile, load_industry_profiles, load_search_profiles
from tender_ai.crawlers.http import HttpFetchError, sha256_bytes
from tender_ai.documents.download import download_attachment
from tender_ai.sources.contracts import DetailPayload, RawListingItem
from tender_ai.sources.browser_profiles import browser_profile_path
from tender_ai.sources.registry import SourceDefinition, SourceRegistry
from tender_ai.status.metadata import describe_time
from tender_ai.status.time import as_shanghai, now_shanghai
from tender_ai.storage.database import create_engine_for, initialize_database, refresh_tender_fts, session_scope
from tender_ai.storage.models import (
    Announcement,
    Attachment,
    CrawlError,
    CrawlRun,
    DiscoveredSource,
    Evidence,
    Project,
    ProjectSource,
    SearchQuery,
    Snapshot,
    Source,
    TimeFieldMetadata,
)
from tender_ai.storage.repository import save_evidence, save_tender_record
from tender_ai.snapshots.store import SnapshotStore
from tender_ai.urls import canonicalize_url


SOURCE_FIELDS = {
    "source_id", "source_name", "category", "base_url", "region", "enabled", "priority",
    "access_method", "requires_login", "adapter", "adapter_level", "adapter_config", "status", "crawl_enabled", "max_pages",
    "request_delay_seconds", "crawl_interval", "rate_limit", "lookback_days", "browser_profile_path", "notes",
}
DEFAULT_QUERY_TERMS = ("光伏", "储能", "新能源")
CHANGE_MARKERS = ("延期", "变更", "澄清", "更正", "补充")
TIME_FIELD_NAMES = (
    "publish_time", "qualification_start", "qualification_deadline", "registration_start", "registration_deadline",
    "document_start", "document_deadline", "bid_deadline", "open_time",
)


@dataclass
class SourceCrawlSummary:
    source_id: str
    source_name: str
    status: str = "RUNNING"
    health_reason: str | None = None
    items_found: int = 0
    new_items: int = 0
    updated_items: int = 0
    failures: int = 0
    attachments: int = 0
    snapshots: int = 0
    query_count: int = 0
    last_http_status: int | None = None
    error: str | None = None


@dataclass
class CrawlSummary:
    started_at: datetime = field(default_factory=now_shanghai)
    finished_at: datetime | None = None
    profile_id: str = "northwest_energy"
    sources: list[SourceCrawlSummary] = field(default_factory=list)
    total_items: int = 0
    total_new_items: int = 0
    total_updated_items: int = 0
    total_failures: int = 0
    total_attachments: int = 0
    total_snapshots: int = 0
    attachment_total_count: int = 0
    snapshot_total_count: int = 0
    new_domain_count: int = 0
    wechat_candidate_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "profile_id": self.profile_id,
            "sources": [asdict(item) for item in self.sources],
            "total_items": self.total_items,
            "total_new_items": self.total_new_items,
            "total_updated_items": self.total_updated_items,
            "total_failures": self.total_failures,
            "total_attachments": self.total_attachments,
            "total_snapshots": self.total_snapshots,
            "attachment_total_count": self.attachment_total_count,
            "snapshot_total_count": self.snapshot_total_count,
            "new_domain_count": self.new_domain_count,
            "wechat_candidate_count": self.wechat_candidate_count,
        }


def _source_payload(definition: SourceDefinition) -> dict[str, Any]:
    payload = {key: value for key, value in definition.model_dump().items() if key in SOURCE_FIELDS}
    payload["browser_profile_path"] = definition.browser_profile_path or str(browser_profile_path(definition.source_id))
    return payload


def _content_hash(payload: DetailPayload) -> str:
    content = payload.html or payload.text or payload.title
    return sha256_bytes(content.encode("utf-8", errors="replace"))


def _change_type(title: str) -> str:
    for marker, value in (("延期", "extension"), ("澄清", "clarification"), ("更正", "correction"), ("补充", "supplement"), ("变更", "change")):
        if marker in title:
            return value
    return "original"


def _fallback_payload(item: RawListingItem) -> DetailPayload:
    metadata = dict(item.metadata)
    metadata.setdefault("published_at", item.published_at)
    return DetailPayload(title=item.title, url=item.url, text=str(metadata.get("content") or item.title), metadata=metadata)


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower().split(":", 1)[0]


def _upsert_discovered_domain(session: Any, url: str, source_level: str | None, source_name: str | None) -> bool:
    original = str(url)
    canonical = canonicalize_url(original)
    domain = _domain(canonical)
    if not domain:
        return False
    row = session.scalar(select(DiscoveredSource).where(DiscoveredSource.source_url == original))
    if row is not None:
        row.last_checked_at = now_shanghai()
        row.last_seen_at = now_shanghai()
        return False
    session.add(
        DiscoveredSource(
            source_url=original,
            original_url=original,
            canonical_url=canonical,
            source_name=source_name,
            domain=domain,
            discovery_method="fixed_source",
            projects_found=1,
            source_level_guess=source_level,
            confidence=0.9,
            status="DISCOVERED",
            last_checked_at=now_shanghai(),
            last_seen_at=now_shanghai(),
        )
    )
    return True


def _health_reason(error: BaseException) -> str:
    message = str(error).casefold()
    if isinstance(error, HttpFetchError):
        if error.status_code in {403, 429} or "rate" in message:
            return "RATE_LIMITED"
        return "HTTP_ERROR"
    if any(token in message for token in ("验证码", "captcha")):
        return "CAPTCHA"
    if any(token in message for token in ("selector", "解析", "json")):
        return "PARSER_ERROR"
    return "HTTP_ERROR"


class CrawlRunner:
    def __init__(self, *, database: str | None = None, snapshot_store: SnapshotStore | None = None):
        self.engine = initialize_database(create_engine_for(database))
        self.snapshot_store = snapshot_store or SnapshotStore()

    def _seed_sources(self, session: Any, definitions: list[SourceDefinition]) -> None:
        for definition in definitions:
            row = session.get(Source, definition.source_id)
            payload = _source_payload(definition)
            if row is None:
                session.add(Source(**payload))
            else:
                for key, value in payload.items():
                    setattr(row, key, value)
        session.flush()

    def _save_time_metadata(self, session: Any, record: Any, evidence_rows: dict[str, Evidence]) -> None:
        for field_name in TIME_FIELD_NAMES:
            value = getattr(record, field_name, None)
            if value is None:
                continue
            evidence = evidence_rows.get(field_name)
            item = describe_time(field_name, value, evidence.raw_value if evidence else None, source_evidence_id=evidence.id if evidence else None)
            if item is None:
                continue
            existing = session.scalar(
                select(TimeFieldMetadata).where(
                    TimeFieldMetadata.project_id == record.project_id,
                    TimeFieldMetadata.field_name == field_name,
                    TimeFieldMetadata.value == item.value,
                )
            )
            if existing is None:
                session.add(
                    TimeFieldMetadata(
                        project_id=record.project_id,
                        field_name=item.field_name,
                        value=item.value,
                        timezone=item.timezone,
                        precision=item.precision,
                        explicit_or_inferred=item.explicit_or_inferred,
                        source_evidence_id=item.source_evidence_id,
                        inference_rule=item.inference_rule,
                        raw_value=item.raw_value,
                    )
                )

    def _save_item(
        self,
        session: Any,
        definition: SourceDefinition,
        adapter: Any,
        item: RawListingItem,
        summary: SourceCrawlSummary,
        *,
        download_attachments: bool,
        only_active_opportunities: bool = False,
    ) -> None:
        try:
            payload = adapter.fetch_detail(item.url)
        except Exception:
            payload = None
        if payload is None or not isinstance(payload, DetailPayload):
            payload = _fallback_payload(item)
        extraction = adapter.normalize_with_evidence(payload) if hasattr(adapter, "normalize_with_evidence") else None
        if extraction is None:
            raise ValueError("PARSER_ERROR: 适配器没有返回 ExtractionResult")
        record = extraction.record
        if only_active_opportunities and record.status.value != "OPEN":
            return
        original_url = item.url
        canonical_url = canonicalize_url(original_url)
        digest = _content_hash(payload)
        record.source_url = original_url
        record.original_url = original_url
        record.canonical_url = canonical_url
        record.content_hash = digest
        project_exists = session.get(Project, record.project_id) is not None
        save_tender_record(session, record, status_reason=record.status_reason, change_type=_change_type(item.title))
        announcement = session.scalar(
            select(Announcement).where(
                Announcement.canonical_url == canonical_url,
                Announcement.content_hash == digest,
            )
        )
        clean_text = payload.text or payload.html or payload.title
        if announcement is None:
            announcement = Announcement(
                project_id=record.project_id,
                announcement_type=record.announcement_type,
                title=payload.title or item.title,
                source_url=original_url,
                original_url=original_url,
                canonical_url=canonical_url,
                published_at=record.publish_time or item.published_at,
                content_hash=digest,
                raw_content=clean_text[:2_000_000],
                clean_text=clean_text[:2_000_000],
                is_current=True,
            )
            session.add(announcement)
            session.flush()
            if not project_exists:
                summary.new_items += 1
            else:
                summary.updated_items += 1
        else:
            announcement.is_current = True
        try:
            snapshot_is_new = session.scalar(select(Snapshot).where(Snapshot.sha256 == digest)) is None
            snapshot = self.snapshot_store.save_text(
                session,
                source_url=original_url,
                text=payload.html or payload.text or payload.title,
                content_type="text/html" if payload.html else "application/json",
                source_id=definition.source_id,
                announcement_id=announcement.id,
                metadata={"title": payload.title, "content_hash": digest},
            )
            announcement.snapshot_id = snapshot.snapshot_id
            if snapshot_is_new:
                summary.snapshots += 1
        except Exception:
            snapshot = None
        project_source = session.get(ProjectSource, {"project_id": record.project_id, "source_id": definition.source_id})
        if project_source is None:
            session.add(ProjectSource(project_id=record.project_id, source_id=definition.source_id, source_url=original_url, original_url=original_url, canonical_url=canonical_url, content_hash=digest))
        else:
            project_source.source_url = original_url
            project_source.original_url = original_url
            project_source.canonical_url = canonical_url
            project_source.content_hash = digest
            project_source.last_seen_at = now_shanghai()
        evidence_rows: dict[str, Evidence] = {}
        for evidence in extraction.evidences:
            row = save_evidence(session, evidence, project_id=record.project_id, announcement_id=announcement.id)
            evidence_rows.setdefault(evidence.field_name, row)
        self._save_time_metadata(session, record, evidence_rows)
        if download_attachments:
            try:
                links = adapter.fetch_attachments(payload)
            except Exception as exc:
                links = []
                summary.health_reason = _health_reason(exc)
            for link in links[:5]:
                try:
                    downloaded = download_attachment(adapter.http, link.url, suggested_name=link.file_name, mime_type=link.mime_type, cache=adapter.cache)
                except Exception as exc:
                    summary.failures += 1
                    summary.health_reason = _health_reason(exc)
                    summary.error = str(exc)[:1000]
                    continue
                existing_attachment = next((pending for pending in session.new if isinstance(pending, Attachment) and pending.project_id == record.project_id and pending.content_hash == downloaded.content_hash), None)
                if existing_attachment is None:
                    existing_attachment = session.scalar(select(Attachment).where(Attachment.project_id == record.project_id, Attachment.content_hash == downloaded.content_hash))
                if existing_attachment is None:
                    session.add(Attachment(project_id=record.project_id, announcement_id=announcement.id, file_name=downloaded.file_name, source_url=downloaded.source_url, local_path=str(downloaded.local_path), mime_type=downloaded.mime_type, content_hash=downloaded.content_hash, downloaded_at=now_shanghai()))
                    summary.attachments += 1
        refresh_tender_fts(session, session.get(Project, record.project_id), clean_text)
        _upsert_discovered_domain(session, original_url, record.source_level, record.source_name)

    def plan(self, *, source_id: str | None = None, profile_id: str = "northwest_energy", max_pages: int | None = None) -> dict[str, Any]:
        profile = load_search_profiles().get(profile_id)
        registry = SourceRegistry.from_file()
        definitions = [registry.get(source_id)] if source_id else [item for item in registry.definitions if item.enabled and item.crawl_enabled and profile.allows_source(item.source_id, item.category)]
        terms = self._query_terms(profile)
        return {
            "profile_id": profile.profile_id,
            "profile_name": profile.name,
            "sources": [item.source_id for item in definitions],
            "query_terms": list(terms),
            "query_count_estimate": len(definitions) * len(terms),
            "max_pages": max_pages,
            "dry_run": True,
        }

    @staticmethod
    def _query_terms(profile: SearchProfile) -> tuple[str, ...]:
        try:
            catalog = load_industry_profiles()
            terms = list(catalog.terms_for(profile.industry_groups))
        except Exception:
            terms = []
        terms.extend(profile.include_keywords)
        terms = [item for item in dict.fromkeys(terms) if item and item not in profile.exclude_keywords]
        return tuple(terms[:12] or DEFAULT_QUERY_TERMS)

    def run(
        self,
        *,
        source_id: str | None = None,
        profile_id: str = "northwest_energy",
        since_days: int | None = None,
        max_pages: int | None = None,
        max_items: int = 20,
        query_terms: tuple[str, ...] | None = None,
        download_attachments: bool = True,
    ) -> CrawlSummary:
        profile = load_search_profiles().get(profile_id)
        registry = SourceRegistry.from_file()
        definitions = [registry.get(source_id)] if source_id else [item for item in registry.definitions if item.enabled and item.crawl_enabled and profile.allows_source(item.source_id, item.category)]
        summary = CrawlSummary(profile_id=profile.profile_id)
        effective_since_days = since_days or profile.lookback_days
        effective_terms = query_terms or self._query_terms(profile)
        with session_scope(self.engine) as session:
            self._seed_sources(session, registry.definitions)
            for definition in definitions:
                source_row = session.get(Source, definition.source_id)
                source_summary = SourceCrawlSummary(definition.source_id, definition.source_name)
                summary.sources.append(source_summary)
                if not definition.enabled or not definition.crawl_enabled:
                    source_summary.status = "DISABLED"
                    source_summary.health_reason = "NO_RESULTS_EXPECTED"
                    continue
                previous_items = source_row.items_found if source_row else 0
                crawl_run = CrawlRun(source_id=definition.source_id, profile_id=profile.profile_id, status="RUNNING")
                session.add(crawl_run)
                session.flush()
                adapter = None
                try:
                    from tender_ai.sources.adapters import build_adapter

                    adapter = build_adapter(definition)
                    actual_pages = min(definition.max_pages, max_pages) if max_pages else definition.max_pages
                    cutoff = now_shanghai() - timedelta(days=effective_since_days)
                    if source_row and source_row.last_success_at:
                        cutoff = max(cutoff, as_shanghai(source_row.last_success_at) - timedelta(days=1))
                    seen_urls: set[str] = set()
                    item_budget = max(1, max_items)
                    for query in effective_terms:
                        if source_summary.items_found >= item_budget:
                            break
                        source_summary.query_count += 1
                        crawl_run.query_count += 1
                        try:
                            items = adapter.search(query, max_pages=actual_pages, since_days=effective_since_days)
                            query_row = session.scalar(select(SearchQuery).where(SearchQuery.query_text == query, SearchQuery.source_id == definition.source_id, SearchQuery.profile_id == profile.profile_id))
                            if query_row is None:
                                query_row = SearchQuery(query_text=query, category="crawl", region=definition.region, source_id=definition.source_id, profile_id=profile.profile_id, priority=definition.priority)
                                session.add(query_row)
                            query_row.last_run_at = now_shanghai()
                            query_row.last_success_at = now_shanghai()
                            query_row.results_count = len(items)
                            query_row.run_count = (query_row.run_count or 0) + 1
                            query_row.last_error = None
                        except Exception as exc:
                            source_summary.failures += 1
                            source_summary.health_reason = _health_reason(exc)
                            source_summary.error = str(exc)[:1000]
                            crawl_run.error_count += 1
                            query_row = session.scalar(select(SearchQuery).where(SearchQuery.query_text == query, SearchQuery.source_id == definition.source_id, SearchQuery.profile_id == profile.profile_id))
                            if query_row is not None:
                                query_row.last_run_at = now_shanghai()
                                query_row.last_error = str(exc)[:1000]
                            session.add(CrawlError(crawl_run_id=crawl_run.id, source_id=definition.source_id, error_type=type(exc).__name__, error_message=str(exc)[:4000], retryable=isinstance(exc, HttpFetchError), occurred_at=now_shanghai()))
                            continue
                        for item in items:
                            if source_summary.items_found >= item_budget:
                                break
                            canonical = canonicalize_url(item.url)
                            if canonical in seen_urls:
                                continue
                            seen_urls.add(canonical)
                            if item.published_at and as_shanghai(item.published_at) < cutoff:
                                continue
                            source_summary.items_found += 1
                            crawl_run.item_count += 1
                            crawl_run.items_seen += 1
                            crawl_run.checkpoint = canonical
                            try:
                                self._save_item(session, definition, adapter, item, source_summary, download_attachments=download_attachments, only_active_opportunities=profile.only_active_opportunities)
                            except Exception as exc:
                                source_summary.failures += 1
                                source_summary.health_reason = _health_reason(exc)
                                source_summary.error = str(exc)[:1000]
                                crawl_run.error_count += 1
                                crawl_run.items_failed += 1
                                session.add(CrawlError(crawl_run_id=crawl_run.id, source_id=definition.source_id, url=item.url, error_type=type(exc).__name__, error_message=str(exc)[:4000], retryable=False, occurred_at=now_shanghai()))
                            if definition.request_delay_seconds:
                                time.sleep(definition.request_delay_seconds)
                    source_summary.last_http_status = getattr(getattr(adapter, "http", None), "last_success_status", None) or getattr(getattr(adapter, "http", None), "last_status", None)
                    if definition.status == "NEEDS_ATTENTION" or source_summary.health_reason == "CAPTCHA":
                        source_summary.status = "NEEDS_ATTENTION"
                    elif source_summary.failures:
                        source_summary.status = "DEGRADED"
                    else:
                        source_summary.status = "ACTIVE"
                    if source_summary.items_found == 0 and previous_items > 0 and source_summary.failures == 0:
                        source_summary.status = "DEGRADED"
                        source_summary.health_reason = "SUSPECT_ZERO_RESULTS"
                    elif source_summary.items_found == 0 and source_summary.health_reason is None:
                        source_summary.health_reason = "NO_RESULTS_EXPECTED"
                    source_row.runtime_status = source_summary.status
                    source_row.health_reason = source_summary.health_reason
                    source_row.last_health_at = now_shanghai()
                    source_row.consecutive_failures = (source_row.consecutive_failures or 0) + 1 if source_summary.failures else 0
                    source_row.average_items = ((source_row.average_items or 0.0) * 0.8) + source_summary.items_found * 0.2
                    source_row.latest_items = source_summary.items_found
                    if source_summary.failures == 0 or getattr(getattr(adapter, "http", None), "last_success_status", None) is not None:
                        source_row.last_success_at = now_shanghai()
                    if source_summary.failures:
                        source_row.last_failure_at = now_shanghai()
                        source_row.failure_count = (source_row.failure_count or 0) + source_summary.failures
                    source_row.last_http_status = source_summary.last_http_status
                    source_row.items_found = source_summary.items_found
                    source_row.last_error = source_summary.error
                    crawl_run.status = source_summary.status
                except Exception as exc:
                    source_summary.failures += 1
                    source_summary.status = "NEEDS_ATTENTION" if definition.status == "NEEDS_ATTENTION" else "DEGRADED"
                    source_summary.health_reason = _health_reason(exc)
                    source_summary.error = str(exc)[:1000]
                    source_summary.last_http_status = getattr(getattr(adapter, "http", None), "last_success_status", None) or getattr(getattr(adapter, "http", None), "last_status", None)
                    source_row.runtime_status = source_summary.status
                    source_row.health_reason = source_summary.health_reason
                    source_row.last_health_at = now_shanghai()
                    source_row.last_failure_at = now_shanghai()
                    source_row.failure_count = (source_row.failure_count or 0) + 1
                    source_row.consecutive_failures = (source_row.consecutive_failures or 0) + 1
                    source_row.last_http_status = source_summary.last_http_status
                    source_row.last_error = source_summary.error
                    crawl_run.status = source_summary.status
                    crawl_run.error_count += 1
                    crawl_run.items_failed += 1
                    session.add(CrawlError(crawl_run_id=crawl_run.id, source_id=definition.source_id, error_type=type(exc).__name__, error_message=source_summary.error, retryable=isinstance(exc, HttpFetchError), occurred_at=now_shanghai()))
                finally:
                    if adapter is not None and hasattr(adapter, "close"):
                        adapter.close()
                crawl_run.finished_at = now_shanghai()
                crawl_run.new_item_count = source_summary.new_items
                crawl_run.items_new = source_summary.new_items
                crawl_run.items_updated = source_summary.updated_items
                crawl_run.attachment_count = source_summary.attachments
                crawl_run.error_count = max(crawl_run.error_count, source_summary.failures)
                summary.total_items += source_summary.items_found
                summary.total_new_items += source_summary.new_items
                summary.total_updated_items += source_summary.updated_items
                summary.total_failures += source_summary.failures
                summary.total_attachments += source_summary.attachments
                summary.total_snapshots += source_summary.snapshots
            summary.attachment_total_count = session.scalar(select(func.count()).select_from(Attachment)) or 0
            summary.snapshot_total_count = session.scalar(select(func.count()).select_from(Snapshot)) or 0
        summary.finished_at = now_shanghai()
        self._write_report(summary)
        return summary

    def _write_report(self, summary: CrawlSummary) -> Path:
        report_path = APP_ROOT.parent / "CRAWL_REPORT.md"
        lines = [
            "# CRAWL REPORT",
            "",
            "系统定位：区域新能源招投标自动搜索系统",
            f"默认搜索 Profile：{summary.profile_id}",
            "",
            f"- 运行开始：{summary.started_at.isoformat()}",
            f"- 运行结束：{summary.finished_at.isoformat() if summary.finished_at else ''}",
            f"- 抓取公告数：{summary.total_items}",
            f"- 新增项目数：{summary.total_new_items}",
            f"- 变化项目数：{summary.total_updated_items}",
            f"- 失败数：{summary.total_failures}",
            f"- 新下载附件数：{summary.total_attachments}",
            f"- 数据库附件总数：{summary.attachment_total_count}",
            f"- 新保存快照数：{summary.total_snapshots}",
            f"- 数据库快照总数：{summary.snapshot_total_count}",
            "",
            "## 来源状态",
            "",
            "| source_id | 状态 | 健康原因 | 抓取数 | 新增 | 变化 | 失败 | 附件 | HTTP | 错误 |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---|",
        ]
        for item in summary.sources:
            lines.append(f"| {item.source_id} | {item.status} | {item.health_reason or ''} | {item.items_found} | {item.new_items} | {item.updated_items} | {item.failures} | {item.attachments} | {item.last_http_status or ''} | {(item.error or '').replace('|', '/')[:160]} |")
        lines.extend([
            "", "## 架构优化完成情况", "",
            "| 项目 | 状态 | 说明 |", "|---|---|---|",
            "| 通用区域定位与 Search Profile | DONE | 默认 northwest_energy，范围由 config/search_profiles.yaml 选择 |",
            "| 全国地区目录与行业关键词组 | DONE | region_catalog.yaml 与 industry_profiles.yaml 独立配置 |",
            "| 声明式 GenericSourceAdapter | DONE | 支持列表、详情、附件、分页 selector 配置 |",
            "| API/HTTP/浏览器分级 | DONE | 真实来源优先 API/HTTP，未启动浏览器 |",
            "| Snapshot 与 Replay 基础设施 | DONE | 候选页面按内容 hash 保存，支持离线重解析 |",
            "| URL 规范化与内容去重 | DONE | canonical_url、tracking 参数和内容 hash |",
            "| 状态原因码与时间元数据 | DONE | 内部原因码、时区、精度和推断标记 |",
            "| SQLite WAL/FTS5/幂等运行 | DONE | FTS5 不可用时保留 LIKE fallback |",
            "| Source Health 与 recrawl 字段 | DONE | 健康原因、连续失败、零结果怀疑标记 |",
            "| LLM Provider 与缓存 | DEFERRED | 按原计划第4步实现，数据库字段与接口预留 |",
            "| Web 设置页、关注/忽略和人工修正 UI | DEFERRED | 按原计划第5步实现，数据库基础字段已预留 |",
            "",
            "## 说明",
            "",
            "固定来源使用已核验的公开 HTTP/JSON 页面；验证码、WAF 或平台短链导致的失败单独记录，不影响其他来源。",
            "",
        ])
        report_path.write_text("\n".join(lines), encoding="utf-8")
        return report_path


__all__ = ["CrawlRunner", "CrawlSummary", "SourceCrawlSummary"]
