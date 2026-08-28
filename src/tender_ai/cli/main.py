"""区域新能源招投标自动搜索系统 CLI。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import typer
from sqlalchemy import inspect, select

from tender_ai.config_loader import (
    APP_ROOT,
    RegionRegistry,
    load_industry_profiles,
    load_keyword_catalog,
    load_region_catalog,
    load_search_profiles,
)
from tender_ai.crawlers.runner import CrawlRunner
from tender_ai.discovery.queries import generate_discovery_queries
from tender_ai.discovery.runner import DiscoveryRunner
from tender_ai.models import TenderRecord
from tender_ai.replay import ReplayRunner
from tender_ai.sources.registry import SourceRegistry
from tender_ai.status.engine import recalculate_status
from tender_ai.status.time import as_shanghai, now_shanghai, parse_datetime
from tender_ai.storage.database import create_engine_for, fts5_available, initialize_database, resolve_database_url, session_scope
from tender_ai.storage.models import CrawlRun, Project, Source
from tender_ai.storage.repository import add_status_history, project_to_record


app = typer.Typer(add_completion=False, no_args_is_help=True, help="区域新能源招投标自动搜索系统命令行工具")


@app.command()
def doctor(database: str | None = typer.Option(None, "--database", help="SQLite 文件路径或 SQLAlchemy URL")) -> None:
    """检查环境、配置、数据库、Provider 和最近运行状态。"""

    errors: list[str] = []
    checks: dict[str, object] = {}
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
            "time_field_metadata", "manual_overrides", "llm_extraction_cache", "system_metadata",
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
            checks["failed_sources"] = [row.source_id for row in session.scalars(select(Source).where(Source.runtime_status.in_(["DEGRADED", "NEEDS_ATTENTION"]))).all()]
            checks["suspect_zero_results_sources"] = [row.source_id for row in session.scalars(select(Source).where(Source.health_reason == "SUSPECT_ZERO_RESULTS")).all()]
            latest_run = session.scalar(select(CrawlRun).order_by(CrawlRun.started_at.desc()))
            checks["latest_crawl"] = {"run_id": latest_run.run_id, "status": latest_run.status, "started_at": latest_run.started_at.isoformat()} if latest_run else None
        if missing:
            errors.append("数据库缺少表: " + ", ".join(missing))
    except Exception as exc:
        errors.append(f"数据库: {exc}")

    checks["browser"] = {"browsers_dir": str(APP_ROOT.parent / "browsers"), "available": (APP_ROOT.parent / "browsers").exists()}
    checks["browser_profiles_root"] = str(APP_ROOT.parent / "data" / "browser_profiles")
    try:
        import ddgs  # noqa: F401
        checks["search_provider_ddgs"] = "AVAILABLE"
    except Exception as exc:
        checks["search_provider_ddgs"] = f"ERROR: {exc}"
    try:
        import pymupdf4llm  # noqa: F401
        checks["pdf_parser_pymupdf4llm"] = "AVAILABLE"
    except Exception as exc:
        checks["pdf_parser_pymupdf4llm"] = f"ERROR: {exc}"
    try:
        import diskcache  # noqa: F401
        checks["disk_cache"] = "AVAILABLE"
    except Exception as exc:
        checks["disk_cache"] = f"ERROR: {exc}"
    checks["llm_provider"] = "INTERFACE_RESERVED_FOR_STAGE_4"
    checks["task_scheduler"] = "AVAILABLE" if shutil.which("schtasks") else "NOT_FOUND"
    checks["migrations"] = sorted(path.name for path in (APP_ROOT.parent / "app" / "migrations" / "versions").glob("*.py"))
    payload = {"ok": not errors, "checks": checks, "errors": errors}
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    if errors:
        raise typer.Exit(code=1)


@app.command("init-db")
def init_db(database: str | None = typer.Option(None, "--database", help="SQLite 文件路径或 SQLAlchemy URL")) -> None:
    """创建核心 SQLite 表并同步来源注册表。"""

    engine = create_engine_for(database)
    initialize_database(engine)
    registry = SourceRegistry.from_file()
    with session_scope(engine) as session:
        for definition in registry.definitions:
            row = session.get(Source, definition.source_id)
            values = {key: value for key, value in definition.model_dump().items() if hasattr(Source, key)}
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
    """不访问网站，按当前状态规则重算已有项目。"""

    engine = initialize_database(create_engine_for(database))
    reference = parse_datetime(now) if now else now_shanghai()
    changed = 0
    total = 0
    with session_scope(engine) as session:
        for project in session.scalars(select(Project)).all():
            total += 1
            record = project_to_record(project)
            decision = recalculate_status(record, reference)
            if project.status != decision.status.value:
                old_status = project.status
                project.status = decision.status.value
                add_status_history(session, project.project_id, old_status, project.status, decision.reason, reference)
                changed += 1
            project.status_reason = decision.reason_code
            project.status_evaluated_at = as_shanghai(reference)
            project.updated_at = as_shanghai(reference)
    typer.echo(json.dumps({"total": total, "changed": changed, "evaluated_at": as_shanghai(reference).isoformat()}, ensure_ascii=False))


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
    """从已核验的公开来源真实采集公告。"""

    runner = CrawlRunner(database=database)
    if dry_run:
        typer.echo(json.dumps(runner.plan(source_id=source, profile_id=profile, max_pages=max_pages), ensure_ascii=False, indent=2))
        return
    summary = runner.run(source_id=source, profile_id=profile, since_days=since_days, max_pages=max_pages, max_items=max_items, download_attachments=download_attachments)
    typer.echo(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2, default=str))


@app.command()
def discovery(
    profile: str = typer.Option("northwest_energy", "--profile", help="Search Profile ID"),
    max_queries: int | None = typer.Option(None, "--max-queries", min=1, max=500, help="本轮最大查询数；默认使用 Profile 预算"),
    max_results: int | None = typer.Option(None, "--max-results", min=1, max=50, help="每个查询最大结果数"),
    database: str | None = typer.Option(None, "--database", help="SQLite 文件路径或 SQLAlchemy URL"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只显示查询计划，不访问搜索服务、不写业务数据"),
) -> None:
    """通过可替换 SearchProvider 发现未知网址与候选公告。"""

    if dry_run:
        profile_row = load_search_profiles().get(profile)
        queries = generate_discovery_queries(max_queries=max_queries or profile_row.query_budget, profile_id=profile)
        typer.echo(json.dumps({"profile_id": profile, "query_count": len(queries), "queries": [item.text for item in queries]}, ensure_ascii=False, indent=2))
        return
    summary = DiscoveryRunner(database=database).run(profile_id=profile, max_queries=max_queries, max_results=max_results)
    typer.echo(json.dumps(summary.__dict__, ensure_ascii=False, indent=2, default=str))


@app.command()
def replay(
    announcement_id: int | None = typer.Option(None, "--announcement-id", help="只回放一个公告 ID"),
    source: str | None = typer.Option(None, "--source", help="只回放一个 source_id"),
    database: str | None = typer.Option(None, "--database", help="SQLite 文件路径或 SQLAlchemy URL"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只检查快照，不更新项目"),
) -> None:
    """针对已保存 HTML/JSON 快照离线重跑解析和状态计算。"""

    summary = ReplayRunner(database=database).run(announcement_id=announcement_id, source_id=source, dry_run=dry_run)
    typer.echo(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2))


@app.command()
def sources(as_json: bool = typer.Option(False, "--json", help="使用 JSON 输出")) -> None:
    """查看来源注册表和运行健康度，不访问来源网站。"""

    registry = SourceRegistry.from_file()
    engine = initialize_database(create_engine_for())
    with session_scope(engine) as session:
        rows = []
        for item in registry.definitions:
            payload = item.model_dump()
            runtime = session.get(Source, item.source_id)
            if runtime is not None:
                payload.update({
                    "last_success_at": runtime.last_success_at, "last_failure_at": runtime.last_failure_at,
                    "failure_count": runtime.failure_count, "items_found": runtime.items_found,
                    "last_http_status": runtime.last_http_status, "runtime_status": runtime.runtime_status,
                    "health_reason": runtime.health_reason, "consecutive_failures": runtime.consecutive_failures,
                    "average_items": runtime.average_items, "latest_items": runtime.latest_items,
                    "last_error": runtime.last_error,
                })
            rows.append(payload)
    if as_json:
        typer.echo(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        return
    for item in registry.definitions:
        state = "enabled" if item.enabled and item.crawl_enabled else "disabled"
        typer.echo(f"{item.source_id:28} {item.source_name:24} {item.category:10} {state:8} {item.status}")


def main() -> None:
    app()


__all__ = ["app", "main"]
