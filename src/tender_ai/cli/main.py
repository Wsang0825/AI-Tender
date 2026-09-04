"""区域新能源招投标系统 CLI。

Python 负责可重复的搜索、采集、规则抽取和状态计算；Codex 负责自然语言
理解、复杂公告阅读以及带证据的 Review 写回。本模块不调用任何 AI API。
"""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import typer
from sqlalchemy import inspect, select, text

from tender_ai.config_loader import (
    APP_ROOT,
    RegionRegistry,
    load_industry_profiles,
    load_keyword_catalog,
    load_region_catalog,
    load_search_profiles,
)
from tender_ai.crawlers.runner import CrawlRunner
from tender_ai.candidates import candidate_dict
from tender_ai.discovery.queries import generate_discovery_queries
from tender_ai.discovery.runner import DiscoveryRunner
from tender_ai.evidence.models import EvidenceRecord
from tender_ai.export import export_search_session
from tender_ai.extractors.runner import ExtractionRunner
from tender_ai.matching.dedupe import normalize_identity
from tender_ai.models import TenderRecord
from tender_ai.replay import ReplayRunner
from tender_ai.recall_benchmark import DEFAULT_BENCHMARK_PATH, DEFAULT_REPORT_PATH, payload_json, run_recall_benchmark, write_recall_report
from tender_ai.review import resolve_review_item, review_item_dict, write_review_files
from tender_ai.search import SearchRequest, SearchRunner, normalize_result_mode, normalize_search_mode, parse_search_text
from tender_ai.sources.registry import SourceRegistry
from tender_ai.status.engine import recalculate_status, with_manual_evidence
from tender_ai.status.metadata import describe_time
from tender_ai.status.time import as_shanghai, now_shanghai, parse_datetime
from tender_ai.storage.database import create_engine_for, fts5_available, initialize_database, resolve_database_url, session_scope
from tender_ai.storage.models import (
    Announcement,
    Attachment,
    Candidate,
    CandidateEnrichmentQuery,
    CandidateEnrichmentResult,
    CandidateFact,
    CandidateSource,
    CodexReviewItem,
    CrawlRun,
    DocumentParse,
    Evidence,
    FieldConflict,
    ManualOverride,
    Project,
    ProjectSource,
    SearchSession,
    Snapshot,
    Source,
    SourcePivot,
    TimeFieldMetadata,
    TimelineEvent,
)
from tender_ai.storage.repository import (
    _coerce_field_value,
    add_status_history,
    clear_manual_override,
    project_to_record,
    save_evidence,
    save_manual_override,
)
from tender_ai.verification.runner import VerificationRunner
from tender_ai.versioning import SCHEMA_VERSION, STATUS_RULE_VERSION
from tender_ai.templates import (
    list_templates,
    request_from_template,
    save_template,
    set_template_enabled,
)


def _configure_utf8_stdio() -> None:
    """Keep JSON/Markdown CLI output readable to Codex on Windows consoles."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue


_configure_utf8_stdio()


app = typer.Typer(add_completion=False, no_args_is_help=True, help="区域新能源招投标自动搜索系统命令行工具")


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return as_shanghai(value).isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "value"):
        return value.value
    return str(value)


def _project_payload(project: Project) -> dict[str, Any]:
    fields = (
        "project_id", "project_name", "raw_project_name", "canonical_project_name", "province", "city", "county", "location",
        "owner", "purchaser", "tenderer", "agency", "industry", "sub_industry", "project_type", "announcement_type",
        "project_scale", "capacity_mw", "capacity_mwh", "budget", "project_code", "tender_code", "publish_time",
        "qualification_start", "qualification_deadline", "registration_start", "registration_deadline", "document_start",
        "document_deadline", "bid_deadline", "open_time", "qualification_summary", "participation_method", "source_name",
        "source_type", "source_level", "source_url", "original_url", "canonical_url", "content_hash", "status", "status_reason",
        "status_evaluated_at", "status_rule_version", "lifecycle_state", "last_change_at", "favorite", "ignored", "ignore_reason",
        "document_quality_score", "extraction_version", "extraction_method", "last_extracted_at", "verification_required",
        "verification_reason", "field_confidence", "source_confidence", "project_match_confidence", "overall_confidence",
        "completeness_score", "needs_codex_review", "review_reason", "tender_status", "relevance_class",
        "verification_status", "enrichment_state", "blocker", "next_action", "identity_status", "identity_confidence",
        "relation_types_json", "matched_concepts_json", "missing_fields_json", "project_location", "tenderer_location",
        "agency_location", "source_location", "rank_score", "created_at", "updated_at",
    )
    return {field_name: _json_default(getattr(project, field_name, None)) if getattr(project, field_name, None) is not None else None for field_name in fields}


def _evidence_payload(row: Evidence) -> dict[str, Any]:
    return {
        "evidence_id": row.id,
        "field_name": row.field_name,
        "normalized_value": row.normalized_value,
        "raw_value": row.raw_value,
        "source_url": row.source_url,
        "source_file": row.source_file,
        "snapshot_id": row.snapshot_id,
        "document_id": row.document_id,
        "page_number": row.page_number,
        "sheet_name": row.sheet_name,
        "cell_range": row.cell_range,
        "source_text": row.source_text,
        "extractor": row.extractor,
        "extractor_type": row.extractor_type,
        "extractor_version": row.extractor_version,
        "confidence": row.confidence,
        "captured_at": _json_default(row.captured_at),
        "content_hash": row.content_hash,
    }


@app.command()
def doctor(database: str | None = typer.Option(None, "--database", help="SQLite 文件路径或 SQLAlchemy URL")) -> None:
    """检查本地环境、SQLite、配置、解析器、Search Provider 和最近运行状态。"""

    errors: list[str] = []
    checks: dict[str, Any] = {"ai_api": "DISABLED_BY_DESIGN", "codex_as_top_level_agent": True}
    try:
        regions = RegionRegistry.from_file()
        catalog = load_region_catalog()
        keywords = load_keyword_catalog()
        industries = load_industry_profiles()
        profiles = load_search_profiles()
        sources = SourceRegistry.from_file()
        checks.update({
            "regions": regions.counts(),
            "region_catalog_count": len(catalog.entries),
            "keyword_count": len(keywords["all"]),
            "industry_profile_count": len(industries.profiles),
            "search_profiles": [item.profile_id for item in profiles.profiles],
            "enabled_search_profiles": [item.profile_id for item in profiles.enabled()],
            "source_count": len(sources.definitions),
            "enabled_source_count": sum(1 for item in sources.definitions if item.enabled and item.crawl_enabled),
        })
    except Exception as exc:
        errors.append(f"配置: {exc}")
        checks["config"] = "ERROR"

    try:
        engine = initialize_database(create_engine_for(database))
        table_names = set(inspect(engine).get_table_names())
        required_tables = {
            "projects", "announcements", "sources", "project_sources", "attachments", "evidence", "status_history",
            "change_history", "crawl_runs", "crawl_errors", "discovered_sources", "search_queries", "snapshots",
            "time_field_metadata", "manual_overrides", "system_metadata", "document_parses", "timeline_events",
            "verification_tasks", "verification_results", "field_conflicts", "search_sessions", "search_session_projects",
            "codex_review_items", "search_templates", "candidates", "candidate_sources", "candidate_enrichment_queries",
            "candidate_enrichment_results", "candidate_facts", "source_pivots",
            "candidate_attachments",
        }
        missing = sorted(required_tables - table_names)
        checks.update({
            "database_url": resolve_database_url(database),
            "database_initialized": not missing,
            "missing_tables": missing,
            "fts5_available": fts5_available(engine),
            "sqlite_fallback": not fts5_available(engine),
        })
        with session_scope(engine) as session:
            migration_row = session.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).first() if "alembic_version" in table_names else None
            checks["migration_current"] = migration_row[0] if migration_row else "UNSTAMPED_RUNTIME_SCHEMA"
            checks["migration_expected"] = SCHEMA_VERSION
            checks["failed_sources"] = [row.source_id for row in session.scalars(select(Source).where(Source.runtime_status.in_(["DEGRADED", "NEEDS_ATTENTION"]))).all()]
            checks["suspect_zero_results_sources"] = [row.source_id for row in session.scalars(select(Source).where(Source.health_reason == "SUSPECT_ZERO_RESULTS")).all()]
            latest_run = session.scalar(select(CrawlRun).where(CrawlRun.finished_at.is_not(None)).order_by(CrawlRun.finished_at.desc()))
            checks["latest_crawl"] = {
                "run_id": latest_run.run_id,
                "status": latest_run.status,
                "started_at": _json_default(latest_run.started_at),
                "finished_at": _json_default(latest_run.finished_at),
                "complete": True,
            } if latest_run else None
        if missing:
            errors.append("数据库缺少表: " + ", ".join(missing))
    except Exception as exc:
        errors.append(f"数据库: {exc}")

    checks["AGENTS.md"] = (APP_ROOT / "AGENTS.md").exists()
    checks["CODEX_SEARCH_GUIDE.md"] = (APP_ROOT.parent / "CODEX_SEARCH_GUIDE.md").exists()
    checks["web_app"] = (APP_ROOT / "src" / "tender_ai" / "web" / "app.py").exists()
    checks["scheduling_mode"] = "ON_DEMAND_ONLY"
    checks["browser"] = {"browsers_dir": str(APP_ROOT.parent / "browsers"), "available": (APP_ROOT.parent / "browsers").exists()}
    checks["browser_profiles_root"] = str(APP_ROOT.parent / "data" / "browser_profiles")
    checks["network_client"] = "httpx" if _module_available("httpx") else "ERROR"
    checks["search_provider_ddgs"] = "AVAILABLE" if _module_available("ddgs") else "ERROR"
    checks["search_provider_searxng"] = "CONFIGURED" if shutil.which("curl") else "OPTIONAL"
    checks["pdf_parser_pymupdf4llm"] = "AVAILABLE" if _module_available("pymupdf4llm") else "ERROR"
    checks["pdf_parser_mineru"] = "AVAILABLE" if _module_available("mineru") else "OPTIONAL_NOT_INSTALLED"
    checks["disk_cache"] = "AVAILABLE" if _module_available("diskcache") else "ERROR"
    checks["scrapling"] = "AVAILABLE" if _module_available("scrapling") else "OPTIONAL_NOT_INSTALLED"
    checks["crawl4ai"] = "AVAILABLE" if _module_available("crawl4ai") else "OPTIONAL_NOT_INSTALLED"
    checks["document_parsers"] = {
        "python-docx": _module_available("docx"),
        "openpyxl": _module_available("openpyxl"),
    }
    checks["task_scheduler"] = "AVAILABLE" if shutil.which("schtasks") else "NOT_FOUND"
    checks["migrations"] = sorted(path.name for path in (APP_ROOT.parent / "app" / "migrations" / "versions").glob("*.py"))
    payload = {"ok": not errors, "checks": checks, "errors": errors}
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))
    if errors:
        raise typer.Exit(code=1)


@app.command("recall-regression")
def recall_regression(
    benchmark: str | None = typer.Option(None, "--benchmark", help="Recall 基准 YAML 路径"),
    database: str | None = typer.Option(None, "--database"),
    report: str | None = typer.Option(None, "--report", help="输出 Markdown 报告路径"),
) -> None:
    """在当前数据库和已保存真实报告/快照上运行 Recall 回归。"""

    benchmark_path = Path(benchmark).expanduser() if benchmark else DEFAULT_BENCHMARK_PATH
    payload = run_recall_benchmark(benchmark_path=benchmark_path, database=database)
    report_path = write_recall_report(payload, Path(report).expanduser() if report else DEFAULT_REPORT_PATH)
    payload = {**payload, "report_path": str(report_path)}
    typer.echo(payload_json(payload))


def _module_available(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


@app.command("init-db")
def init_db(database: str | None = typer.Option(None, "--database", help="SQLite 文件路径或 SQLAlchemy URL")) -> None:
    """创建核心 SQLite 表并同步来源注册表。"""

    engine = initialize_database(create_engine_for(database))
    registry = SourceRegistry.from_file()
    with session_scope(engine) as session:
        for definition in registry.definitions:
            row = session.get(Source, definition.source_id)
            values = {key: value for key, value in definition.model_dump().items() if hasattr(Source, key)}
            values["browser_profile_path"] = definition.browser_profile_path or str(APP_ROOT.parent / "data" / "browser_profiles" / definition.source_id)
            if row is None:
                session.add(Source(**values))
            else:
                for key, value in values.items():
                    setattr(row, key, value)
    typer.echo(f"database initialized: {resolve_database_url(database)}")


@app.command()
def recalc(
    database: str | None = typer.Option(None, "--database", help="SQLite 文件路径或 SQLAlchemy URL"),
    now: str | None = typer.Option(None, "--now", help="测试用当前时间，支持 ISO 日期"),
) -> None:
    """不访问网站，按当前确定性状态规则重算已有项目。"""

    engine = initialize_database(create_engine_for(database))
    reference = parse_datetime(now) if now else now_shanghai()
    changed = 0
    total = 0
    with session_scope(engine) as session:
        for project in session.scalars(select(Project)).all():
            total += 1
            record = project_to_record(project)
            evidence_rows = list(session.scalars(select(Evidence).where(Evidence.project_id == project.project_id)).all())
            overrides = list(session.scalars(select(ManualOverride).where(ManualOverride.project_id == project.project_id, ManualOverride.active.is_(True))).all())
            gate_evidence = with_manual_evidence(evidence_rows, overrides, source_url=project.source_url)
            decision = recalculate_status(record, reference, evidences=gate_evidence, require_evidence=True)
            if project.status != decision.status.value:
                old_status = project.status
                project.status = decision.status.value
                add_status_history(session, project.project_id, old_status, project.status, decision.reason, reference)
                changed += 1
            project.status_reason = decision.reason_code
            project.tender_status = project.status
            project.status_evaluated_at = as_shanghai(reference)
            project.status_rule_version = STATUS_RULE_VERSION
            project.updated_at = as_shanghai(reference)
    typer.echo(json.dumps({"total": total, "changed": changed, "evaluated_at": as_shanghai(reference).isoformat(), "status_rule_version": STATUS_RULE_VERSION}, ensure_ascii=False))


@app.command()
def extract(
    announcement_id: int | None = typer.Option(None, "--announcement-id", help="只处理一个公告 ID"),
    source: str | None = typer.Option(None, "--source", help="只处理一个 source_id 关联的公告"),
    sample_size: int = typer.Option(30, "--sample-size", min=1, max=200, help="真实公告抽查数量"),
    consolidate: bool = typer.Option(True, "--consolidate/--no-consolidate", help="是否执行确定编号的跨来源项目合并"),
    reuse_cached: bool = typer.Option(True, "--reuse-cached/--no-reuse-cached", help="内容、解析器和规则版本未变化时复用已有抽取结果"),
    database: str | None = typer.Option(None, "--database", help="SQLite 文件路径或 SQLAlchemy URL"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只解析并统计，不写项目、Evidence或文档记录"),
) -> None:
    """使用确定性规则解析已保存公告和附件，生成 Evidence 与 Codex Review。"""

    summary = ExtractionRunner(database=database).run(announcement_id=announcement_id, source_id=source, sample_size=sample_size, dry_run=dry_run, consolidate=consolidate, reuse_cached=reuse_cached)
    typer.echo(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2, default=_json_default))


@app.command()
def verify(
    project: str | None = typer.Option(None, "--project", help="只核验一个 project_id"),
    max_tasks: int | None = typer.Option(None, "--max-tasks", min=1, max=200, help="最多处理多少个待核验项目"),
    max_results: int = typer.Option(5, "--max-results", min=1, max=20, help="每个精确查询最多结果数"),
    database: str | None = typer.Option(None, "--database", help="SQLite 文件路径或 SQLAlchemy URL"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只显示会进入核验的项目，不访问搜索服务、不写数据"),
) -> None:
    """对 UNKNOWN/弱来源/冲突项目执行精确项目核验。"""

    summary = VerificationRunner(database=database).run(project_id=project, max_tasks=max_tasks, max_results=max_results, dry_run=dry_run)
    typer.echo(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2, default=_json_default))


@app.command()
def crawl(
    source: str | None = typer.Option(None, "--source", help="只抓取一个 source_id"),
    profile: str = typer.Option("northwest_energy", "--profile", help="Search Profile ID"),
    since_days: int | None = typer.Option(None, "--since-days", min=1, max=3650, help="回溯天数；默认使用 Profile 配置"),
    max_pages: int | None = typer.Option(None, "--max-pages", min=1, max=100, help="覆盖来源配置的最大页数"),
    max_items: int = typer.Option(20, "--max-items", min=1, max=500, help="每个来源本轮最多处理公告数"),
    database: str | None = typer.Option(None, "--database", help="SQLite 文件路径或 SQLAlchemy URL"),
    download_attachments: bool = typer.Option(True, "--attachments/--no-attachments", help="是否下载公开附件"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只显示扫描计划，不访问网站、不写业务数据"),
) -> None:
    """从已核验公开来源采集公告。"""

    runner = CrawlRunner(database=database)
    if dry_run:
        typer.echo(json.dumps(runner.plan(source_id=source, profile_id=profile, max_pages=max_pages), ensure_ascii=False, indent=2))
        return
    summary = runner.run(source_id=source, profile_id=profile, since_days=since_days, max_pages=max_pages, max_items=max_items, download_attachments=download_attachments)
    typer.echo(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2, default=_json_default))


@app.command()
def discovery(
    profile: str = typer.Option("northwest_energy", "--profile", help="Search Profile ID"),
    max_queries: int | None = typer.Option(None, "--max-queries", min=1, max=500, help="本轮最大查询数；默认使用 Profile 预算"),
    max_results: int | None = typer.Option(None, "--max-results", min=1, max=50, help="每个查询最大结果数"),
    database: str | None = typer.Option(None, "--database", help="SQLite 文件路径或 SQLAlchemy URL"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只显示查询计划，不访问搜索服务、不写业务数据"),
) -> None:
    """通过可替换 SearchProvider 发现未知网址和候选公告。"""

    if dry_run:
        profile_row = load_search_profiles().get(profile)
        queries = generate_discovery_queries(max_queries=max_queries or profile_row.query_budget, profile_id=profile)
        typer.echo(json.dumps({"profile_id": profile, "query_count": len(queries), "queries": [item.text for item in queries], "dry_run": True}, ensure_ascii=False, indent=2))
        return
    summary = DiscoveryRunner(database=database).run(profile_id=profile, max_queries=max_queries, max_results=max_results)
    typer.echo(json.dumps(summary.__dict__, ensure_ascii=False, indent=2, default=_json_default))


@app.command()
def replay(
    announcement_id: int | None = typer.Option(None, "--announcement-id", help="只回放一个公告 ID"),
    source: str | None = typer.Option(None, "--source", help="只回放一个 source_id"),
    database: str | None = typer.Option(None, "--database", help="SQLite 文件路径或 SQLAlchemy URL"),
    reextract: bool = typer.Option(True, "--reextract/--no-reextract", help="重新运行规则抽取"),
    recalculate_status: bool = typer.Option(True, "--recalculate-status/--no-recalculate-status", help="重新计算状态"),
    rules_only: bool = typer.Option(True, "--rules-only/--no-rules-only", help="只运行确定性规则"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只检查快照，不更新项目"),
) -> None:
    """优先从本地 Snapshot/附件离线重跑解析、Evidence 和状态。"""

    summary = ReplayRunner(database=database).run(
        announcement_id=announcement_id,
        source_id=source,
        dry_run=dry_run,
        reextract=reextract,
        recalculate_status=recalculate_status,
        rules_only=rules_only,
    )
    typer.echo(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2, default=_json_default))


def _request_from_options(
    query: str | None,
    *,
    profile: str,
    region: str | None,
    city: str | None,
    county: str | None,
    days: int | None,
    date_from: str | None,
    date_to: str | None,
    industries: list[str],
    project_types: list[str],
    equipment: list[str],
    keywords: list[str],
    exclude_keywords: list[str],
    source_level: str | None,
    source_categories: list[str],
    announcement_types: list[str],
    include_unknown: bool,
    only_open: bool,
    discovery_enabled: bool,
    wechat: bool,
    deep: bool,
    search_mode: str | None = None,
    result_mode: str | None = None,
    concept_id: str | None = None,
    relations: list[str] | None = None,
    max_enrichments: int | None = None,
) -> SearchRequest:
    base = parse_search_text(query, profile_id=profile) if query else SearchRequest(profile_id=profile)
    profile_defaults = load_search_profiles().get(profile)
    return SearchRequest(
        raw_query=query,
        profile_id=profile,
        search_mode=normalize_search_mode(search_mode or base.search_mode or profile_defaults.default_search_mode),
        result_mode=normalize_result_mode(result_mode or base.result_mode or profile_defaults.default_result_mode),
        concept_id=concept_id or base.concept_id,
        region=region or base.region,
        city=city or base.city,
        county=county or base.county,
        regions=tuple(dict.fromkeys(([region] if region else list(base.regions)) or ([base.region] if base.region else []))),
        region_codes=tuple(base.region_codes),
        cities=tuple(dict.fromkeys(([city] if city else list(base.cities)) or ([base.city] if base.city else []))),
        counties=tuple(dict.fromkeys(([county] if county else list(base.counties)) or ([base.county] if base.county else []))),
        days=days or base.days,
        date_from=date_from or base.date_from,
        date_to=date_to or base.date_to,
        industries=tuple(dict.fromkeys(industries or base.industries or profile_defaults.industry_groups)),
        project_types=tuple(dict.fromkeys(project_types or base.project_types)),
        equipment=tuple(dict.fromkeys(equipment or base.equipment)),
        keywords=tuple(dict.fromkeys(keywords or base.keywords)),
        exclude_keywords=tuple(dict.fromkeys(exclude_keywords or base.exclude_keywords)),
        source_level=source_level or base.source_level,
        source_categories=tuple(dict.fromkeys(source_categories or base.source_categories)),
        announcement_types=tuple(dict.fromkeys(announcement_types or base.announcement_types)),
        include_unknown=include_unknown or base.include_unknown,
        only_open=only_open or base.only_open,
        discovery=discovery_enabled or base.discovery,
        wechat=wechat or base.wechat,
        deep=deep or base.deep,
        relation_types=tuple(dict.fromkeys((relations or list(base.relation_types)))),
        max_enrichments=max_enrichments,
    )


def _search_command(
    query: str | None,
    *,
    profile: str,
    region: str | None,
    city: str | None,
    county: str | None,
    days: int | None,
    date_from: str | None,
    date_to: str | None,
    industries: list[str],
    project_types: list[str],
    equipment: list[str],
    keywords: list[str],
    exclude_keywords: list[str],
    source_level: str | None,
    source_categories: list[str],
    announcement_types: list[str],
    include_unknown: bool,
    only_open: bool,
    discovery_enabled: bool,
    wechat: bool,
    deep: bool,
    search_mode: str | None,
    result_mode: str | None,
    concept_id: str | None,
    relations: list[str],
    max_enrichments: int | None,
    database: str | None,
    dry_run: bool,
    codex_output: bool = False,
) -> None:
    request = _request_from_options(query, profile=profile, region=region, city=city, county=county, days=days, date_from=date_from, date_to=date_to, industries=industries, project_types=project_types, equipment=equipment, keywords=keywords, exclude_keywords=exclude_keywords, source_level=source_level, source_categories=source_categories, announcement_types=announcement_types, include_unknown=include_unknown, only_open=only_open, discovery_enabled=discovery_enabled, wechat=wechat, deep=deep, search_mode=search_mode, result_mode=result_mode, concept_id=concept_id, relations=relations, max_enrichments=max_enrichments)
    summary = SearchRunner(database=database).run(request, dry_run=dry_run)
    payload = summary.as_dict()
    if codex_output:
        next_actions = [
            "读取 search_report.md、summary.md 和 results.json",
            "先处理 manual_action_sources；需要登录或浏览器验证的来源必须立即告知用户",
            "优先检查 OPEN 项目及临近截止项目",
            "读取 codex_review.md 中的 PENDING 项目 Snapshot/PDF",
            "必要时执行 python -m tender_ai verify --project PROJECT_ID",
            "确认事实后使用 set-field 写回 Evidence，再执行 python -m tender_ai recalc",
        ]
        if summary.suppressed_unchanged_count:
            next_actions.append(f"本轮已自动隐藏 {summary.suppressed_unchanged_count} 个上次已报告且未变化项目，不要重复阅读或输出")
        if summary.suppressed_valuable_lead_count:
            next_actions.append(f"本轮已自动隐藏 {summary.suppressed_valuable_lead_count} 条上次已报告且原文未变化的价值线索，不要重复阅读或输出")
        if summary.manual_action_sources:
            next_actions.insert(0, "立即把人工处理来源的打开地址、HTTP 状态和操作说明反馈给用户；等待用户完成验证并回复‘已完成人工验证’后，只重试对应来源")
        next_actions.append("二手网站或公众号线索只能作为线索；检查 official_trace，未命中官方来源时明确标注‘官方公告未找到/待核验’，不要只输出二手链接")
        if summary.valuable_lead_count:
            next_actions.append("读取 valuable_leads；把项目级EPC、前期跟踪、已建成历史安装、箱变/设备钢平台等线索单独上报，保留出处，不能混入直接组件支架采购或OPEN清单")
        payload["NEXT_ACTIONS_FOR_CODEX"] = next_actions
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


@app.command()
def search(
    query: str | None = typer.Argument(None, help="中文快捷搜索语句；复杂语义建议由 Codex 转成结构化参数"),
    profile: str = typer.Option("northwest_energy", "--profile"),
    region: str | None = typer.Option(None, "--region"),
    city: str | None = typer.Option(None, "--city"),
    county: str | None = typer.Option(None, "--county"),
    days: int | None = typer.Option(None, "--days", min=1, max=3650),
    date_from: str | None = typer.Option(None, "--date-from"),
    date_to: str | None = typer.Option(None, "--date-to"),
    industries: list[str] = typer.Option([], "--industry"),
    project_types: list[str] = typer.Option([], "--project-type"),
    equipment: list[str] = typer.Option([], "--equipment"),
    keywords: list[str] = typer.Option([], "--keyword"),
    exclude_keywords: list[str] = typer.Option([], "--exclude-keyword"),
    source_level: str | None = typer.Option(None, "--source-level"),
    source_categories: list[str] = typer.Option([], "--source-category"),
    announcement_types: list[str] = typer.Option([], "--announcement-type"),
    include_unknown: bool = typer.Option(False, "--include-unknown"),
    only_open: bool = typer.Option(False, "--only-open"),
    discovery_enabled: bool = typer.Option(False, "--discovery"),
    wechat: bool = typer.Option(False, "--wechat"),
    deep: bool = typer.Option(False, "--deep"),
    search_mode: str | None = typer.Option(None, "--search-mode", help="exact/broad/opportunity"),
    result_mode: str | None = typer.Option(None, "--result-mode", help="full/delta（也接受 FULL_RESULT/DELTA_RESULT）"),
    concept_id: str | None = typer.Option(None, "--concept"),
    relations: list[str] = typer.Option([], "--relation"),
    max_enrichments: int | None = typer.Option(None, "--max-enrichments", min=0, max=500),
    database: str | None = typer.Option(None, "--database"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """按需执行真实采集、规则抽取和状态筛选。"""

    _search_command(query, profile=profile, region=region, city=city, county=county, days=days, date_from=date_from, date_to=date_to, industries=industries, project_types=project_types, equipment=equipment, keywords=keywords, exclude_keywords=exclude_keywords, source_level=source_level, source_categories=source_categories, announcement_types=announcement_types, include_unknown=include_unknown, only_open=only_open, discovery_enabled=discovery_enabled, wechat=wechat, deep=deep, search_mode=search_mode, result_mode=result_mode, concept_id=concept_id, relations=relations, max_enrichments=max_enrichments, database=database, dry_run=dry_run, codex_output=True)


@app.command("codex-search")
def codex_search(
    query: str | None = typer.Argument(None),
    profile: str = typer.Option("northwest_energy", "--profile"),
    region: str | None = typer.Option(None, "--region"),
    city: str | None = typer.Option(None, "--city"),
    county: str | None = typer.Option(None, "--county"),
    days: int | None = typer.Option(None, "--days", min=1, max=3650),
    date_from: str | None = typer.Option(None, "--date-from"),
    date_to: str | None = typer.Option(None, "--date-to"),
    industries: list[str] = typer.Option([], "--industry"),
    project_types: list[str] = typer.Option([], "--project-type"),
    equipment: list[str] = typer.Option([], "--equipment"),
    keywords: list[str] = typer.Option([], "--keyword"),
    exclude_keywords: list[str] = typer.Option([], "--exclude-keyword"),
    source_level: str | None = typer.Option(None, "--source-level"),
    source_categories: list[str] = typer.Option([], "--source-category"),
    announcement_types: list[str] = typer.Option([], "--announcement-type"),
    include_unknown: bool = typer.Option(False, "--include-unknown"),
    only_open: bool = typer.Option(False, "--only-open"),
    discovery_enabled: bool = typer.Option(False, "--discovery"),
    wechat: bool = typer.Option(False, "--wechat"),
    deep: bool = typer.Option(False, "--deep"),
    search_mode: str | None = typer.Option(None, "--search-mode", help="exact/broad/opportunity"),
    result_mode: str | None = typer.Option(None, "--result-mode", help="full/delta（也接受 FULL_RESULT/DELTA_RESULT）"),
    concept_id: str | None = typer.Option(None, "--concept"),
    relations: list[str] = typer.Option([], "--relation"),
    max_enrichments: int | None = typer.Option(None, "--max-enrichments", min=0, max=500),
    database: str | None = typer.Option(None, "--database"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Codex 首选的总控搜索命令，输出 sessions/results/review 文件。"""

    _search_command(query, profile=profile, region=region, city=city, county=county, days=days, date_from=date_from, date_to=date_to, industries=industries, project_types=project_types, equipment=equipment, keywords=keywords, exclude_keywords=exclude_keywords, source_level=source_level, source_categories=source_categories, announcement_types=announcement_types, include_unknown=include_unknown, only_open=only_open, discovery_enabled=discovery_enabled, wechat=wechat, deep=deep, search_mode=search_mode, result_mode=result_mode, concept_id=concept_id, relations=relations, max_enrichments=max_enrichments, database=database, dry_run=dry_run, codex_output=True)


@app.command("review")
def review_session(
    session_id: str = typer.Option(..., "--session"),
    database: str | None = typer.Option(None, "--database"),
) -> None:
    """重新生成指定 Search Session 的 Codex Review 文件，不访问网络。"""

    engine = initialize_database(create_engine_for(database))
    with session_scope(engine) as session:
        search_session = session.get(SearchSession, session_id)
        if search_session is None:
            raise typer.BadParameter(f"Search Session 不存在: {session_id}")
        md_path, json_path, items = write_review_files(session, session_id)
        typer.echo(json.dumps({
            "session_id": session_id,
            "review_count": len(items),
            "markdown": str(md_path),
            "json": str(json_path),
            "network_accessed": False,
        }, ensure_ascii=False, indent=2))


def _inspection(session: Any, *, project: Project | None = None, announcement: Announcement | None = None) -> dict[str, Any]:
    if project is None and announcement is not None:
        project = session.get(Project, announcement.project_id)
    if project is None:
        raise KeyError("project not found")
    announcements = list(session.scalars(select(Announcement).where(Announcement.project_id == project.project_id).order_by(Announcement.published_at.desc(), Announcement.id.desc())).all())
    selected_announcement = announcement or (announcements[0] if announcements else None)
    announcement_ids = [row.id for row in announcements]
    evidence_rows = list(session.scalars(select(Evidence).where(Evidence.project_id == project.project_id).order_by(Evidence.id)).all())
    if announcement is not None:
        evidence_rows = [
            row for row in evidence_rows
            if row.announcement_id == announcement.id
            or (row.announcement_id is None and row.source_url and row.source_url == announcement.source_url)
        ]
    evidence = [_evidence_payload(row) for row in evidence_rows]
    timeline = []
    for row in session.scalars(select(TimelineEvent).where(TimelineEvent.project_id == project.project_id).order_by(TimelineEvent.event_at, TimelineEvent.id)).all():
        timeline.append({"event_id": row.id, "event_type": row.event_type, "event_time": _json_default(row.event_at), "announcement_id": row.announcement_id, "source_url": row.source_url, "title": row.title, "summary": row.summary, "deadline_snapshot": row.deadline_snapshot_json, "evidence_ids": json.loads(row.evidence_ids_json or "[]")})
    documents = []
    document_query = select(DocumentParse).where(DocumentParse.project_id == project.project_id)
    if announcement is not None:
        document_query = document_query.where(DocumentParse.announcement_id == announcement.id)
    for row in session.scalars(document_query.order_by(DocumentParse.id)).all():
        documents.append({"document_id": row.document_id, "announcement_id": row.announcement_id, "file_path": row.file_path, "source_file": row.source_file, "source_url": row.source_url, "content_hash": row.content_hash, "mime_type": row.mime_type or row.content_type, "parser": row.parser, "parser_version": row.parser_version, "quality_score": row.quality_score, "page_count": row.page_count, "clean_text_path": row.clean_text_path, "markdown_path": row.markdown_path, "parse_status": row.parse_status, "parse_error": row.parse_error or row.error, "parsed_at": _json_default(row.parsed_at or row.extracted_at) if (row.parsed_at or row.extracted_at) else None})
    attachments = [{"id": row.id, "file_name": row.file_name, "source_url": row.source_url, "local_path": row.local_path, "mime_type": row.mime_type, "content_hash": row.content_hash} for row in session.scalars(select(Attachment).where(Attachment.project_id == project.project_id).order_by(Attachment.id)).all()]
    sources = []
    for link in session.scalars(select(ProjectSource).where(ProjectSource.project_id == project.project_id)).all():
        source = session.get(Source, link.source_id)
        sources.append({"source_id": link.source_id, "source_name": source.source_name if source else None, "category": source.category if source else None, "source_level": project.source_level, "source_url": link.source_url, "canonical_url": link.canonical_url, "content_hash": link.content_hash})
    reviews = [review_item_dict(session, row) for row in session.scalars(select(CodexReviewItem).where(CodexReviewItem.project_id == project.project_id).order_by(CodexReviewItem.priority, CodexReviewItem.created_at)).all()]
    conflicts = [{"id": row.id, "field_name": row.field_name, "candidate_values": json.loads(row.candidate_values_json or "[]"), "evidence_ids": json.loads(row.evidence_ids_json or "[]"), "resolution_status": row.resolution_status, "detected_at": _json_default(row.detected_at)} for row in session.scalars(select(FieldConflict).where(FieldConflict.project_id == project.project_id).order_by(FieldConflict.id)).all()]
    candidate = session.scalar(select(Candidate).where(Candidate.project_id == project.project_id).order_by(Candidate.updated_at.desc(), Candidate.created_at.desc()))
    candidate_payload = candidate_dict(candidate) if candidate is not None else None
    candidate_sources = []
    enrichment_queries = []
    enrichment_results = []
    candidate_facts = []
    source_pivots = []
    if candidate is not None:
        candidate_sources = [{
            "id": row.id,
            "source_id": row.source_id,
            "source_url": row.source_url,
            "original_url": row.original_url,
            "canonical_url": row.canonical_url,
            "source_domain": row.source_domain,
            "source_name": row.source_name,
            "source_level": row.source_level,
            "source_type": row.source_type,
            "provider": row.provider,
            "source_title": row.source_title,
            "snippet": row.snippet,
            "source_location": row.source_location,
            "published_at": _json_default(row.published_at) if row.published_at else None,
            "content_hash": row.content_hash,
            "is_official": row.is_official,
            "is_secondary": row.is_secondary,
            "access_status": row.access_status,
            "first_seen_at": _json_default(row.first_seen_at),
            "last_seen_at": _json_default(row.last_seen_at),
        } for row in session.scalars(select(CandidateSource).where(CandidateSource.candidate_id == candidate.candidate_id).order_by(CandidateSource.id)).all()]
        enrichment_queries = [{
            "id": row.id,
            "search_session_id": row.search_session_id,
            "parent_query_id": row.parent_query_id,
            "query_text": row.query_text,
            "strategy": row.strategy,
            "round_no": row.round_no,
            "provider": row.provider,
            "source_id": row.source_id,
            "status": row.status,
            "results_count": row.results_count,
            "candidate_hits": row.candidate_hits,
            "new_fact_count": row.new_fact_count,
            "new_source_count": row.new_source_count,
            "query_hash": row.query_hash,
            "content_hash": row.content_hash,
            "executed_at": _json_default(row.executed_at),
            "error": row.error,
        } for row in session.scalars(select(CandidateEnrichmentQuery).where(CandidateEnrichmentQuery.candidate_id == candidate.candidate_id).order_by(CandidateEnrichmentQuery.id)).all()]
        enrichment_results = [{
            "id": row.id,
            "query_id": row.query_id,
            "candidate_id": row.candidate_id,
            "discovered_candidate_id": row.discovered_candidate_id,
            "search_session_id": row.search_session_id,
            "title": row.title,
            "source_url": row.source_url,
            "canonical_url": row.canonical_url,
            "snippet": row.snippet,
            "provider": row.provider,
            "published_at": _json_default(row.published_at) if row.published_at else None,
            "source_level": row.source_level,
            "content_hash": row.content_hash,
            "identity_status": row.identity_status,
            "relevance_class": row.relevance_class,
            "is_official": row.is_official,
            "is_secondary": row.is_secondary,
            "match_type": row.match_type,
            "created_at": _json_default(row.created_at),
        } for row in session.scalars(select(CandidateEnrichmentResult).where(CandidateEnrichmentResult.candidate_id == candidate.candidate_id).order_by(CandidateEnrichmentResult.id)).all()]
        candidate_facts = [{
            "id": row.id,
            "field_name": row.field_name,
            "value": row.value,
            "normalized_value": row.normalized_value,
            "raw_value": row.raw_value,
            "evidence_id": row.evidence_id,
            "source_url": row.source_url,
            "source_level": row.source_level,
            "confidence": row.confidence,
            "is_current": row.is_current,
            "created_at": _json_default(row.created_at),
        } for row in session.scalars(select(CandidateFact).where(CandidateFact.candidate_id == candidate.candidate_id).order_by(CandidateFact.id)).all()]
        source_pivots = [{
            "id": row.id,
            "entity_type": row.entity_type,
            "entity_value": row.entity_value,
            "source_id": row.source_id,
            "discovered_url": row.discovered_url,
            "domain": row.domain,
            "strategy": row.strategy,
            "confidence": row.confidence,
            "status": row.status,
            "created_at": _json_default(row.created_at),
        } for row in session.scalars(select(SourcePivot).where(SourcePivot.candidate_id == candidate.candidate_id).order_by(SourcePivot.id)).all()]
    return {
        "project": _project_payload(project),
        "announcement": {"id": selected_announcement.id, "title": selected_announcement.title, "announcement_type": selected_announcement.announcement_type, "source_url": selected_announcement.source_url, "original_url": selected_announcement.original_url, "canonical_url": selected_announcement.canonical_url, "published_at": _json_default(selected_announcement.published_at) if selected_announcement.published_at else None, "content_hash": selected_announcement.content_hash, "snapshot_id": selected_announcement.snapshot_id, "clean_text": (selected_announcement.clean_text or "")[:2000]} if selected_announcement else None,
        "announcements": [{"id": row.id, "title": row.title, "source_url": row.source_url, "published_at": _json_default(row.published_at) if row.published_at else None, "content_hash": row.content_hash, "snapshot_id": row.snapshot_id} for row in announcements],
        "evidence": evidence,
        "timeline": timeline,
        "sources": sources,
        "documents": documents,
        "attachments": attachments,
        "codex_review": reviews,
        "field_conflicts": conflicts,
        "candidate": candidate_payload,
        "candidate_sources": candidate_sources,
        "enrichment_queries": enrichment_queries,
        "enrichment_results": enrichment_results,
        "candidate_facts": candidate_facts,
        "source_pivots": source_pivots,
        "local_paths": list(dict.fromkeys([item["file_path"] for item in documents if item.get("file_path")] + [item["clean_text_path"] for item in documents if item.get("clean_text_path")] + [item["markdown_path"] for item in documents if item.get("markdown_path")] + [item["local_path"] for item in attachments if item.get("local_path")])),
        "announcement_count": len(announcement_ids),
    }


@app.command("inspect")
def inspect_command(
    project: str | None = typer.Option(None, "--project"),
    announcement: int | None = typer.Option(None, "--announcement"),
    as_json: bool = typer.Option(False, "--json"),
    database: str | None = typer.Option(None, "--database"),
) -> None:
    """输出项目或公告的紧凑 Project/Evidence/Timeline/本地文件视图。"""

    if bool(project) == bool(announcement):
        raise typer.BadParameter("必须且只能提供 --project 或 --announcement")
    engine = initialize_database(create_engine_for(database))
    with session_scope(engine) as session:
        row = session.get(Project, project) if project else session.get(Announcement, announcement)
        if row is None:
            raise typer.BadParameter("目标不存在")
        payload = _inspection(session, project=row if isinstance(row, Project) else None, announcement=row if isinstance(row, Announcement) else None)
    if as_json:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))
        return
    p = payload["project"]
    lines = [
        f"# {p.get('project_name')}",
        "",
        f"project_id：{p.get('project_id')}",
        f"地区：{' / '.join(value for value in (p.get('province'), p.get('city'), p.get('county')) if value) or '未知'}",
        f"招标人：{p.get('owner') or '未知'}；代理：{p.get('agency') or '未知'}",
        f"项目类型：{p.get('project_type') or '未知'}；规模：{p.get('project_scale') or p.get('capacity_mw') or p.get('capacity_mwh') or '未知'}",
        f"报名截止：{p.get('registration_deadline') or '未知'}；文件截止：{p.get('document_deadline') or '未知'}",
        f"投标截止：{p.get('bid_deadline') or '未知'}；开标：{p.get('open_time') or '未知'}",
        f"状态：{p.get('status')}；原因：{p.get('status_reason')}",
        f"来源等级：{p.get('source_level') or '未知'}；需要Review：{'是' if p.get('needs_codex_review') else '否'}",
        "",
        f"公告数：{len(payload['announcements'])}；Evidence：{len(payload['evidence'])}；Timeline：{len(payload['timeline'])}",
        f"候选状态：{(payload.get('candidate') or {}).get('enrichment_state', '无候选记录')}；候选来源：{len(payload.get('candidate_sources', []))}；递归查询：{len(payload.get('enrichment_queries', []))}",
        "",
        "## Timeline",
        "",
    ]
    lines.extend(f"- {event['event_time']} {event['event_type']} {event['title'] or ''}" for event in payload["timeline"])
    lines.extend(["", "## 本地文件", "", *[f"- {path}" for path in payload["local_paths"]]])
    lines.extend(["", "## Review", "", *[f"- {item['review_id']}：{item['status']} / {item['reason']}" for item in payload["codex_review"]]])
    typer.echo("\n".join(lines))


@app.command("set-field")
def set_field(
    project: str | None = typer.Option(None, "--project"),
    announcement: int | None = typer.Option(None, "--announcement"),
    field: str = typer.Option(..., "--field"),
    value: str = typer.Option(..., "--value"),
    evidence_text: str = typer.Option(..., "--evidence-text"),
    source_url: str = typer.Option(..., "--source-url"),
    snapshot_id: str | None = typer.Option(None, "--snapshot-id"),
    document_id: str | None = typer.Option(None, "--document-id"),
    page_number: int | None = typer.Option(None, "--page-number", min=1),
    precision: str = typer.Option("EXPLICIT", "--precision"),
    resolution_source: str = typer.Option("CODEX_REVIEW", "--resolution-source"),
    review_id: str | None = typer.Option(None, "--review-id"),
    reason: str | None = typer.Option(None, "--reason"),
    database: str | None = typer.Option(None, "--database"),
) -> None:
    """带真实原文 Evidence 写回事实字段；禁止直接写入 OPEN/CLOSED。"""

    if bool(project) == bool(announcement):
        raise typer.BadParameter("必须且只能提供 --project 或 --announcement")
    if field in {"status", "status_reason", "status_evaluated_at", "lifecycle_state"}:
        raise typer.BadParameter("状态不能由 Codex 直接写入，请写回事实字段后运行 recalc")
    if resolution_source != "CODEX_REVIEW":
        raise typer.BadParameter("本命令要求 resolution_source=CODEX_REVIEW")
    if not evidence_text.strip() or not source_url.strip():
        raise typer.BadParameter("evidence-text 和 source-url 不能为空")
    engine = initialize_database(create_engine_for(database))
    with session_scope(engine) as session:
        announcement_row = session.get(Announcement, announcement) if announcement else None
        target_project = session.get(Project, project) if project else (session.get(Project, announcement_row.project_id) if announcement_row else None)
        if target_project is None:
            raise typer.BadParameter("目标项目不存在")
        if not hasattr(target_project, field):
            raise typer.BadParameter(f"未知项目字段: {field}")
        try:
            typed_value = _coerce_field_value(field, value)
        except Exception as exc:
            raise typer.BadParameter(f"字段值无法解析: {exc}") from exc
        target_announcement_id = announcement_row.id if announcement_row else None
        if snapshot_id and session.get(Snapshot, snapshot_id) is None:
            raise typer.BadParameter("snapshot_id 不存在")
        evidence = EvidenceRecord(
            field_name=field,
            normalized_value=_json_default(typed_value),
            raw_value=value,
            source_url=source_url,
            source_file=session.get(Snapshot, snapshot_id).file_path if snapshot_id else None,
            snapshot_id=snapshot_id,
            document_id=document_id,
            page_number=page_number,
            source_text=evidence_text.strip(),
            extractor="CODEX_REVIEW",
            extractor_type="CODEX_REVIEW",
            extractor_version="codex_review_v1",
            confidence=1.0,
            captured_at=now_shanghai(),
        )
        evidence_row = save_evidence(session, evidence, project_id=target_project.project_id, announcement_id=target_announcement_id)
        save_manual_override(session, target_project.project_id, field, typed_value, reason=reason or "Codex 根据原文核验", changed_by="CODEX_REVIEW")
        if field in {"publish_time", "qualification_start", "qualification_deadline", "registration_start", "registration_deadline", "document_start", "document_deadline", "bid_deadline", "open_time"}:
            metadata = describe_time(field, typed_value, value, source_evidence_id=evidence_row.id)
            if metadata is not None:
                session.add(TimeFieldMetadata(project_id=target_project.project_id, field_name=field, value=metadata.value, timezone=metadata.timezone, precision=precision if precision in {"DATETIME", "DATE_ONLY", "RANGE", "UNKNOWN"} else metadata.precision, explicit_or_inferred="INFERRED" if precision == "INFERRED" else metadata.explicit_or_inferred, source_evidence_id=evidence_row.id, inference_rule=metadata.inference_rule, raw_value=metadata.raw_value))
        for conflict in session.scalars(select(FieldConflict).where(FieldConflict.project_id == target_project.project_id, FieldConflict.field_name == field, FieldConflict.resolution_status == "PENDING")).all():
            conflict.resolution_status = "RESOLVED"
            conflict.resolution = f"CODEX_REVIEW Evidence {evidence_row.id}"
            conflict.resolved_at = now_shanghai()
        old_status = target_project.status
        evidence_rows = list(session.scalars(select(Evidence).where(Evidence.project_id == target_project.project_id)).all())
        overrides = list(session.scalars(select(ManualOverride).where(ManualOverride.project_id == target_project.project_id, ManualOverride.active.is_(True))).all())
        gate_evidence = with_manual_evidence(evidence_rows, overrides, source_url=target_project.source_url)
        decision = recalculate_status(project_to_record(target_project), evidences=gate_evidence, require_evidence=True)
        target_project.status = decision.status.value
        target_project.status_reason = decision.reason_code
        target_project.status_evaluated_at = now_shanghai()
        target_project.status_rule_version = STATUS_RULE_VERSION
        if old_status != target_project.status:
            add_status_history(session, target_project.project_id, old_status, target_project.status, decision.reason)
        if review_id:
            resolve_review_item(session, review_id, status="RESOLVED", resolution=f"{field} 已由 Codex Review 写回；Evidence {evidence_row.id}")
        typer.echo(json.dumps({"project_id": target_project.project_id, "field": field, "value": _json_default(typed_value), "evidence_id": evidence_row.id, "status": target_project.status, "status_reason": target_project.status_reason, "resolution_source": resolution_source}, ensure_ascii=False, indent=2, default=_json_default))


@app.command("resolve-review")
def resolve_review(
    review_id: str = typer.Option(..., "--review-id"),
    status: str = typer.Option(..., "--status", help="PENDING、RESOLVED 或 SKIPPED"),
    resolution: str = typer.Option(..., "--resolution"),
    database: str | None = typer.Option(None, "--database"),
) -> None:
    """更新 Codex Review 队列状态；不会替代事实字段写回。"""

    engine = initialize_database(create_engine_for(database))
    with session_scope(engine) as session:
        item = resolve_review_item(session, review_id, status=status, resolution=resolution)
        typer.echo(json.dumps({"review_id": item.review_id, "status": item.status, "resolution": item.resolution, "resolved_at": _json_default(item.resolved_at) if item.resolved_at else None}, ensure_ascii=False, indent=2))


@app.command("sources")
def sources(as_json: bool = typer.Option(False, "--json", help="使用 JSON 输出"), database: str | None = typer.Option(None, "--database")) -> None:
    """查看来源注册表和运行健康度，不访问来源网站。"""

    registry = SourceRegistry.from_file()
    engine = initialize_database(create_engine_for(database))
    with session_scope(engine) as session:
        rows = []
        for item in registry.definitions:
            payload = item.model_dump()
            runtime = session.get(Source, item.source_id)
            if runtime is not None:
                payload.update({
                    "last_success_at": runtime.last_success_at, "last_failure_at": runtime.last_failure_at, "failure_count": runtime.failure_count,
                    "items_found": runtime.items_found, "last_http_status": runtime.last_http_status, "runtime_status": runtime.runtime_status,
                    "health_reason": runtime.health_reason, "consecutive_failures": runtime.consecutive_failures, "average_items": runtime.average_items,
                    "latest_items": runtime.latest_items, "last_error": runtime.last_error,
                })
            rows.append(payload)
    if as_json:
        typer.echo(json.dumps(rows, ensure_ascii=False, indent=2, default=_json_default))
        return
    for item in registry.definitions:
        state = "enabled" if item.enabled and item.crawl_enabled else "disabled"
        typer.echo(f"{item.source_id:36} {item.source_name:32} {item.category:16} {state:8} {item.status}")


@app.command("sessions")
def sessions(
    limit: int = typer.Option(20, "--limit", min=1, max=200),
    database: str | None = typer.Option(None, "--database"),
) -> None:
    """查看按需搜索历史；本命令只读数据库，不触发网络访问。"""

    engine = initialize_database(create_engine_for(database))
    with session_scope(engine) as session:
        rows = session.scalars(select(SearchSession).order_by(SearchSession.started_at.desc()).limit(limit)).all()
        payload = []
        for row in rows:
            try:
                request = json.loads(row.request_json or "{}")
            except json.JSONDecodeError:
                request = {}
            try:
                errors = json.loads(row.errors_json or "[]")
            except json.JSONDecodeError:
                errors = []
            payload.append({
                "session_id": row.session_id,
                "request_id": row.request_id,
                "request": request,
                "started_at": _json_default(row.started_at),
                "finished_at": _json_default(row.finished_at) if row.finished_at else None,
                "status": row.status,
                "query_count": row.query_count,
                "candidate_count": row.candidate_count,
                "open_count": row.open_count,
                "unknown_count": row.unknown_count,
                "closed_count": row.closed_count,
                "review_count": row.review_count,
                "verification_count": row.verification_count,
                "sources_planned": row.sources_planned,
                "sources_completed": row.sources_completed,
                "sources_failed": row.sources_failed,
                "errors": errors,
            })
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


@app.command("history")
def history_command(
    limit: int = typer.Option(20, "--limit", min=1, max=200),
    database: str | None = typer.Option(None, "--database"),
) -> None:
    """sessions 的中文工作流别名；只读，不触发网络访问。"""

    sessions(limit=limit, database=database)


@app.command("export")
def export_command(
    session_id: str = typer.Option(..., "--session", help="Search Session ID"),
    include_unknown: bool = typer.Option(False, "--include-unknown", help="同时导出 UNKNOWN 项目"),
    output: str | None = typer.Option(None, "--output", help="可选 XLSX 输出路径"),
    database: str | None = typer.Option(None, "--database"),
) -> None:
    """将一次搜索结果导出为 Excel；默认只导出 OPEN。"""

    result = export_search_session(session_id, database=database, include_unknown=include_unknown, output_path=output)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2, default=_json_default))


@app.command("expand-search")
def expand_search(
    session_id: str = typer.Option(..., "--session", help="要扩展的历史 Search Session ID"),
    deep: bool = typer.Option(False, "--deep"),
    discovery: bool = typer.Option(False, "--discovery"),
    wechat: bool = typer.Option(False, "--wechat"),
    include_unknown: bool = typer.Option(False, "--include-unknown"),
    database: str | None = typer.Option(None, "--database"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """基于历史条件扩大一次搜索；不会修改历史 Session。"""

    engine = initialize_database(create_engine_for(database))
    with session_scope(engine) as session:
        row = session.get(SearchSession, session_id)
        if row is None:
            raise typer.BadParameter(f"Search Session 不存在: {session_id}")
        try:
            request = SearchRequest.from_dict(json.loads(row.request_json or "{}"))
        except (TypeError, json.JSONDecodeError) as exc:
            raise typer.BadParameter("历史 Session 的 request_json 无法解析") from exc
    request = replace(
        request,
        deep=request.deep or deep,
        discovery=request.discovery or discovery or deep,
        wechat=request.wechat or wechat or deep,
        include_unknown=request.include_unknown or include_unknown,
    )
    summary = SearchRunner(database=database).run(request, dry_run=dry_run)
    typer.echo(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2, default=_json_default))


@app.command("templates")
def templates_command(
    database: str | None = typer.Option(None, "--database"),
) -> None:
    """列出搜索模板；模板只保存条件，不会自动执行。"""

    typer.echo(json.dumps(list_templates(database=database), ensure_ascii=False, indent=2, default=_json_default))


@app.command("template-save")
def template_save_command(
    name: str = typer.Argument(..., help="模板名称"),
    query: str | None = typer.Argument(None, help="可选中文快捷条件"),
    profile: str = typer.Option("northwest_energy", "--profile"),
    database: str | None = typer.Option(None, "--database"),
) -> None:
    """保存一个按需搜索模板，不执行网络搜索。"""

    request = parse_search_text(query, profile_id=profile) if query else SearchRequest(profile_id=profile)
    result = save_template(name, request, database=database)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2, default=_json_default))


@app.command("template-run")
def template_run_command(
    template_id: str = typer.Option(..., "--template"),
    database: str | None = typer.Option(None, "--database"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """执行一个已保存模板；只有显式运行才会访问来源。"""

    templates = list_templates(database=database, enabled_only=True)
    selected = next((item for item in templates if item["template_id"] == template_id), None)
    if selected is None:
        raise typer.BadParameter(f"启用的模板不存在: {template_id}")
    summary = SearchRunner(database=database).run(request_from_template(selected), dry_run=dry_run)
    typer.echo(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2, default=_json_default))


@app.command("template-toggle")
def template_toggle_command(
    template_id: str = typer.Option(..., "--template"),
    enabled: bool = typer.Option(..., "--enabled/--disabled"),
    database: str | None = typer.Option(None, "--database"),
) -> None:
    """启用或停用搜索模板，不触发搜索。"""

    typer.echo(json.dumps(set_template_enabled(template_id, enabled, database=database), ensure_ascii=False, indent=2, default=_json_default))


@app.command("web")
def web_command(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8765, "--port", min=1, max=65535),
    database: str | None = typer.Option(None, "--database"),
) -> None:
    """启动本地数据浏览器；启动过程不执行搜索。"""

    import uvicorn

    from tender_ai.web.app import create_app

    uvicorn.run(create_app(database=database), host=host, port=port, reload=False, log_level="info")


def main() -> None:
    app()


__all__ = ["app", "main"]
