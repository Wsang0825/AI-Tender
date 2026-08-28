"""核心框架 CLI。"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from sqlalchemy import inspect, select

from tender_ai.config_loader import RegionRegistry, load_keyword_catalog
from tender_ai.models import TenderRecord
from tender_ai.sources.registry import SourceRegistry
from tender_ai.status.engine import recalculate_status
from tender_ai.status.time import now_shanghai, parse_datetime
from tender_ai.storage.database import create_engine_for, initialize_database, resolve_database_url, session_scope
from tender_ai.storage.models import Project, Source
from tender_ai.storage.repository import add_status_history, project_to_record


app = typer.Typer(add_completion=False, no_args_is_help=True, help="西北五省新能源招投标核心框架命令行工具")


@app.command()
def doctor() -> None:
    """检查配置、核心依赖和数据库结构。"""

    registry = RegionRegistry.from_file()
    keywords = load_keyword_catalog()
    sources = SourceRegistry.from_file()
    engine = create_engine_for()
    table_names = set(inspect(engine).get_table_names())
    required_tables = {"projects", "announcements", "sources", "project_sources", "attachments", "evidence", "status_history", "change_history", "crawl_runs", "crawl_errors", "discovered_sources", "search_queries"}
    payload = {
        "ok": required_tables.issubset(table_names) or not table_names,
        "database_url": resolve_database_url(),
        "database_initialized": required_tables.issubset(table_names),
        "regions": registry.counts(),
        "keyword_count": len(keywords["all"]),
        "source_count": len(sources.definitions),
        "required_table_count": len(required_tables),
    }
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    if not payload["ok"]:
        raise typer.Exit(code=1)


@app.command("init-db")
def init_db(database: str | None = typer.Option(None, "--database", help="SQLite 文件路径或 SQLAlchemy URL")) -> None:
    """创建核心 SQLite 表。"""

    engine = create_engine_for(database)
    initialize_database(engine)
    registry = SourceRegistry.from_file()
    with session_scope(engine) as session:
        for definition in registry.definitions:
            row = session.get(Source, definition.source_id)
            if row is None:
                session.add(Source(**definition.model_dump()))
            else:
                for key, value in definition.model_dump().items():
                    setattr(row, key, value)
    typer.echo(f"database initialized: {resolve_database_url(database)}")


@app.command()
def recalc(
    database: str | None = typer.Option(None, "--database", help="SQLite 文件路径或 SQLAlchemy URL"),
    now: str | None = typer.Option(None, "--now", help="测试用当前时间，支持中文或 ISO 日期"),
) -> None:
    """按确定性时间规则重新计算已有项目状态。"""

    engine = initialize_database(create_engine_for(database))
    reference = parse_datetime(now) if now else now_shanghai()
    changed = 0
    total = 0
    with session_scope(engine) as session:
        projects = list(session.scalars(select(Project)).all())
        for project in projects:
            total += 1
            record = project_to_record(project)
            decision = recalculate_status(record, reference)
            if project.status != decision.status.value:
                old_status = project.status
                project.status = decision.status.value
                project.updated_at = reference
                add_status_history(session, project.project_id, old_status, project.status, decision.reason, reference)
                changed += 1
    typer.echo(json.dumps({"total": total, "changed": changed, "evaluated_at": reference.isoformat()}, ensure_ascii=False))


@app.command()
def sources(as_json: bool = typer.Option(False, "--json", help="以 JSON 输出")) -> None:
    """查看来源注册表；不会访问来源网站。"""

    registry = SourceRegistry.from_file()
    rows = [item.model_dump() for item in registry.definitions]
    if as_json:
        typer.echo(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    for item in registry.definitions:
        state = "enabled" if item.enabled else "disabled"
        typer.echo(f"{item.source_id:28} {item.source_name:24} {item.category:10} {state:8} {item.status}")


def main() -> None:
    app()


__all__ = ["app", "main"]
