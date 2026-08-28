"""SQLite 连接、迁移兼容、WAL 与 FTS5 初始化。"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from sqlalchemy import create_engine, event, inspect, text as sql_text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from tender_ai.config_loader import APP_ROOT
from tender_ai.storage.models import Base
from tender_ai.versioning import APP_VERSION, CONFIG_VERSION, EXTRACTOR_VERSION, SCHEMA_VERSION, STATUS_RULE_VERSION


DEFAULT_DB_PATH = APP_ROOT.parent / "data" / "tender.db"
SQLITE_BUSY_TIMEOUT_MS = 10_000


def resolve_database_url(database: str | Path | None = None) -> str:
    value = database or os.environ.get("TENDER_DATABASE_URL") or os.environ.get("TENDER_DB_PATH")
    if not value:
        return f"sqlite:///{DEFAULT_DB_PATH.as_posix()}"
    value = str(value)
    if "://" in value:
        return value
    return f"sqlite:///{Path(value).expanduser().resolve().as_posix()}"


def _configure_sqlite_connection(dbapi_connection: object, _connection_record: object) -> None:
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def create_engine_for(database: str | Path | None = None, *, echo: bool = False) -> Engine:
    url = resolve_database_url(database)
    if url.startswith("sqlite:///") and ":memory:" not in url:
        db_path = Path(url.removeprefix("sqlite:///"))
        db_path.parent.mkdir(parents=True, exist_ok=True)
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, echo=echo, future=True, connect_args=connect_args)
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _configure_sqlite_connection)
    return engine


def initialize_database(engine: Engine | None = None) -> Engine:
    target = engine or create_engine_for()
    if target.dialect.name == "sqlite":
        with target.begin() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.exec_driver_sql(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
            connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            connection.exec_driver_sql("PRAGMA synchronous=NORMAL")
    Base.metadata.create_all(target)
    _ensure_runtime_columns(target)
    _ensure_indexes(target)
    _ensure_fts5(target)
    _rebuild_fts_if_empty(target)
    _write_system_metadata(target)
    return target


def _ensure_runtime_columns(engine: Engine) -> None:
    """让已经存在的第1/2步 SQLite 库无须删除即可接收新增字段。"""

    additions = {
        "projects": {
            "original_url": "TEXT", "canonical_url": "TEXT", "content_hash": "VARCHAR(128)",
            "status_reason": "VARCHAR(128)", "status_evaluated_at": "DATETIME",
            "lifecycle_state": "VARCHAR(16) DEFAULT 'NEW'", "last_change_at": "DATETIME",
            "favorite": "BOOLEAN DEFAULT 0", "ignored": "BOOLEAN DEFAULT 0", "ignore_reason": "TEXT",
        },
        "announcements": {
            "original_url": "TEXT", "canonical_url": "TEXT", "clean_text": "TEXT", "snapshot_id": "VARCHAR(64)",
        },
        "sources": {
            "adapter_level": "VARCHAR(32) DEFAULT 'CUSTOM_HTTP'", "adapter_config": "TEXT",
            "crawl_interval": "INTEGER DEFAULT 86400", "rate_limit": "FLOAT DEFAULT 0.2", "lookback_days": "INTEGER DEFAULT 30",
            "browser_profile_path": "TEXT", "health_reason": "VARCHAR(64)", "consecutive_failures": "INTEGER DEFAULT 0",
            "average_items": "FLOAT DEFAULT 0", "latest_items": "INTEGER DEFAULT 0", "last_health_at": "DATETIME",
        },
        "project_sources": {
            "original_url": "TEXT", "canonical_url": "TEXT", "content_hash": "VARCHAR(128)",
        },
        "crawl_runs": {
            "run_id": "VARCHAR(64)", "profile_id": "VARCHAR(128)", "checkpoint": "TEXT",
            "items_seen": "INTEGER DEFAULT 0", "items_new": "INTEGER DEFAULT 0", "items_updated": "INTEGER DEFAULT 0", "items_failed": "INTEGER DEFAULT 0",
        },
        "discovered_sources": {"original_url": "TEXT", "canonical_url": "TEXT", "last_seen_at": "DATETIME"},
        "search_queries": {
            "profile_id": "VARCHAR(128)", "last_success_at": "DATETIME", "new_project_count": "INTEGER DEFAULT 0",
            "run_count": "INTEGER DEFAULT 0", "last_error": "TEXT", "cooldown_until": "DATETIME",
        },
    }
    inspector = inspect(engine)
    with engine.begin() as connection:
        for table, columns in additions.items():
            if table not in inspector.get_table_names():
                continue
            existing = {column["name"] for column in inspector.get_columns(table)}
            for name, column_type in columns.items():
                if name not in existing:
                    connection.execute(sql_text(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {column_type}'))
        # 旧库中的 run_id 允许为 NULL；为历史运行补发可追踪 ID。
        if "crawl_runs" in inspector.get_table_names():
            missing_run_ids = connection.exec_driver_sql("SELECT id FROM crawl_runs WHERE run_id IS NULL").fetchall()
            for (row_id,) in missing_run_ids:
                connection.exec_driver_sql("UPDATE crawl_runs SET run_id=? WHERE id=?", (uuid4().hex, row_id))


def _ensure_indexes(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return
    indexes = {
        "ix_projects_status": "projects(status)",
        "ix_projects_province": "projects(province)",
        "ix_projects_city": "projects(city)",
        "ix_projects_publish_time": "projects(publish_time)",
        "ix_projects_document_deadline": "projects(document_deadline)",
        "ix_projects_bid_deadline": "projects(bid_deadline)",
        "ix_projects_content_hash": "projects(content_hash)",
        "ix_announcements_canonical_url": "announcements(canonical_url)",
        "ix_project_sources_canonical_url": "project_sources(canonical_url)",
    }
    with engine.begin() as connection:
        for name, expression in indexes.items():
            connection.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS {name} ON {expression}")


def _ensure_fts5(engine: Engine) -> bool:
    if engine.dialect.name != "sqlite":
        return False
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE VIRTUAL TABLE IF NOT EXISTS tender_fts USING fts5(" 
                "project_id UNINDEXED, project_name, owner, agency, qualification_summary, announcement_text)"
            )
        return True
    except Exception:
        return False


def fts5_available(engine: Engine) -> bool:
    if engine.dialect.name != "sqlite":
        return False
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT project_id FROM tender_fts LIMIT 0")
        return True
    except Exception:
        return False


def _rebuild_fts_if_empty(engine: Engine) -> None:
    if not fts5_available(engine):
        return
    with engine.begin() as connection:
        count = connection.exec_driver_sql("SELECT count(*) FROM tender_fts").scalar_one()
        if count:
            return
        rows = connection.exec_driver_sql(
            "SELECT p.project_id, p.project_name, p.owner, p.agency, p.qualification_summary, "
            "COALESCE(group_concat(a.clean_text, ' '), '') "
            "FROM projects p LEFT JOIN announcements a ON a.project_id=p.project_id GROUP BY p.project_id"
        ).fetchall()
        for row in rows:
            connection.exec_driver_sql(
                "INSERT INTO tender_fts(project_id, project_name, owner, agency, qualification_summary, announcement_text) VALUES (?, ?, ?, ?, ?, ?)",
                tuple(row),
            )


def refresh_tender_fts(session: Session, project: object, announcement_text: str | None = None) -> None:
    """在项目或公告写入后刷新 FTS；不可用时由上层使用 LIKE。"""

    bind = session.get_bind()
    if bind.dialect.name != "sqlite" or not fts5_available(bind):
        return
    project_id = str(getattr(project, "project_id"))
    if announcement_text is None:
        row = session.execute(
            sql_text("SELECT COALESCE(group_concat(clean_text, ' '), '') FROM announcements WHERE project_id=:project_id"),
            {"project_id": project_id},
        ).scalar_one()
        announcement_text = str(row or "")
    session.execute(sql_text("DELETE FROM tender_fts WHERE project_id=:project_id"), {"project_id": project_id})
    session.execute(
        sql_text(
            "INSERT INTO tender_fts(project_id, project_name, owner, agency, qualification_summary, announcement_text) "
            "VALUES (:project_id, :project_name, :owner, :agency, :qualification_summary, :announcement_text)"
        ),
        {
            "project_id": project_id,
            "project_name": getattr(project, "project_name", ""),
            "owner": getattr(project, "owner", "") or "",
            "agency": getattr(project, "agency", "") or "",
            "qualification_summary": getattr(project, "qualification_summary", "") or "",
            "announcement_text": announcement_text,
        },
    )


def search_projects(session: Session, query: str, *, limit: int = 50) -> list[str]:
    """Web 层可直接调用的 FTS5 查询；无 FTS5 时退回 LIKE。"""

    if not query.strip():
        return []
    bind = session.get_bind()
    if bind.dialect.name == "sqlite" and fts5_available(bind):
        rows = session.execute(sql_text("SELECT project_id FROM tender_fts WHERE tender_fts MATCH :query LIMIT :limit"), {"query": query, "limit": limit}).all()
        return [str(row[0]) for row in rows]
    rows = session.execute(
        sql_text(
            "SELECT project_id FROM projects WHERE project_name LIKE :q OR owner LIKE :q OR agency LIKE :q "
            "OR qualification_summary LIKE :q LIMIT :limit"
        ),
        {"q": f"%{query}%", "limit": limit},
    ).all()
    return [str(row[0]) for row in rows]


def _write_system_metadata(engine: Engine) -> None:
    values = {
        "app_version": APP_VERSION,
        "schema_version": SCHEMA_VERSION,
        "config_version": CONFIG_VERSION,
        "extractor_version": EXTRACTOR_VERSION,
        "status_rule_version": STATUS_RULE_VERSION,
    }
    with engine.begin() as connection:
        for key, value in values.items():
            if engine.dialect.name == "sqlite":
                connection.exec_driver_sql(
                    "INSERT INTO system_metadata(key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (key, value),
                )
            else:
                result = connection.execute(sql_text("UPDATE system_metadata SET value=:value, updated_at=CURRENT_TIMESTAMP WHERE key=:key"), {"key": key, "value": value})
                if result.rowcount == 0:
                    connection.execute(sql_text("INSERT INTO system_metadata(key, value, updated_at) VALUES (:key, :value, CURRENT_TIMESTAMP)"), {"key": key, "value": value})


def session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=engine or create_engine_for(), autoflush=False, expire_on_commit=False, class_=Session)


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    session = session_factory(engine)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


__all__ = [
    "DEFAULT_DB_PATH", "SQLITE_BUSY_TIMEOUT_MS", "create_engine_for", "fts5_available", "initialize_database",
    "refresh_tender_fts", "resolve_database_url", "search_projects", "session_factory", "session_scope",
]
