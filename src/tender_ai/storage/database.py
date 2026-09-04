"""SQLite 连接、迁移兼容、WAL 与 FTS5 初始化。"""

from __future__ import annotations

import os
import sys
import threading
import time
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

if os.name == "nt":
    import msvcrt
else:  # pragma: no cover - exercised on non-Windows hosts
    import fcntl


DEFAULT_DB_PATH = APP_ROOT.parent / "data" / "tender.db"
SQLITE_BUSY_TIMEOUT_MS = 30_000
DEFAULT_DATABASE_LOCK_TIMEOUT_SECONDS = 1_800.0
DATABASE_LOCK_POLL_SECONDS = 0.25


class DatabaseLockTimeoutError(TimeoutError):
    """本地数据库队列等待超时。"""


_LOCAL_LOCKS: dict[str, threading.RLock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()
_LOCK_DEPTH = threading.local()


def _sqlite_database_path(engine: Engine) -> Path | None:
    if engine.dialect.name != "sqlite":
        return None
    database = engine.url.database
    if not database or database == ":memory:":
        return None
    return Path(database).expanduser().resolve()


def _local_lock_for(key: str) -> threading.RLock:
    with _LOCAL_LOCKS_GUARD:
        lock = _LOCAL_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCAL_LOCKS[key] = lock
        return lock


def _lock_timeout_seconds(value: float | None) -> float | None:
    if value is not None:
        return max(0.0, float(value))
    raw = os.environ.get("TENDER_DB_LOCK_TIMEOUT_SECONDS")
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return DEFAULT_DATABASE_LOCK_TIMEOUT_SECONDS


def _try_os_lock(handle: object) -> bool:
    try:
        handle.seek(0)  # type: ignore[attr-defined]
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        else:  # pragma: no cover - exercised on non-Windows hosts
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
        return True
    except (OSError, BlockingIOError):
        return False


def _release_os_lock(handle: object) -> None:
    try:
        handle.seek(0)  # type: ignore[attr-defined]
        if os.name == "nt":
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        else:  # pragma: no cover - exercised on non-Windows hosts
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
    except (OSError, ValueError):
        return


def _write_lock_metadata(handle: object, *, purpose: str | None) -> None:
    metadata = f"pid={os.getpid()};purpose={(purpose or 'database-write').replace(chr(10), ' ')};started_at={time.time():.3f}\n".encode("utf-8")
    try:
        # Byte 0 is the byte locked by msvcrt on Windows; keep it intact.
        handle.seek(1)  # type: ignore[attr-defined]
        handle.truncate(1)  # type: ignore[attr-defined]
        handle.write(metadata)  # type: ignore[attr-defined]
        handle.flush()  # type: ignore[attr-defined]
    except (OSError, ValueError):
        return


@contextmanager
def database_write_lock(
    engine: Engine | None = None,
    *,
    timeout_seconds: float | None = None,
    purpose: str | None = None,
) -> Iterator[None]:
    """为同一 SQLite 文件提供跨进程单写入队列。

    SQLite WAL 允许读写并行，但多个长事务仍可能互相争抢写锁。锁文件由操作系统
    自动随进程退出释放，因此不会留下需要人工清理的“死锁”。同一线程的嵌套调用
    可重入，避免初始化和 Session 组合使用时自锁。
    """

    target = engine or create_engine_for()
    database_path = _sqlite_database_path(target)
    if database_path is None:
        yield
        return

    key = str(database_path)
    local_lock = _local_lock_for(key)
    local_lock.acquire()
    depths = getattr(_LOCK_DEPTH, "values", None)
    if depths is None:
        depths = {}
        _LOCK_DEPTH.values = depths
    depth = int(depths.get(key, 0))
    if depth:
        depths[key] = depth + 1
        try:
            yield
        finally:
            if depth == 1:
                depths.pop(key, None)
            else:
                depths[key] = depth
            local_lock.release()
        return

    lock_path = database_path.with_name(database_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = None
    acquired = False
    warned = False
    started = time.monotonic()
    timeout = _lock_timeout_seconds(timeout_seconds)
    try:
        handle = lock_path.open("a+b")
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        while not _try_os_lock(handle):
            elapsed = time.monotonic() - started
            if not warned and elapsed >= 1.0:
                warned = True
                print(
                    f"[AI-Tender][数据库队列] 已有本地任务正在使用数据库，当前任务等待中：{database_path}；不会终止已有任务。",
                    file=sys.stderr,
                    flush=True,
                )
            if timeout is not None and elapsed >= timeout:
                raise DatabaseLockTimeoutError(
                    f"等待数据库锁超时（{timeout:.0f} 秒）：{database_path}；请检查其他搜索任务是否仍在运行。"
                )
            time.sleep(DATABASE_LOCK_POLL_SECONDS)
        acquired = True
        _write_lock_metadata(handle, purpose=purpose)
        depths[key] = 1
        if warned:
            print(
                f"[AI-Tender][数据库队列] 数据库锁已释放，当前任务继续：{database_path}",
                file=sys.stderr,
                flush=True,
            )
        yield
    finally:
        if depths.get(key) == 1:
            depths.pop(key, None)
        if acquired:
            _release_os_lock(handle)
        if handle is not None:
            handle.close()
        local_lock.release()


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
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def create_engine_for(database: str | Path | None = None, *, echo: bool = False) -> Engine:
    url = resolve_database_url(database)
    if url.startswith("sqlite:///") and ":memory:" not in url:
        db_path = Path(url.removeprefix("sqlite:///"))
        db_path.parent.mkdir(parents=True, exist_ok=True)
    connect_args = (
        {"check_same_thread": False, "timeout": SQLITE_BUSY_TIMEOUT_MS / 1000}
        if url.startswith("sqlite")
        else {}
    )
    engine = create_engine(url, echo=echo, future=True, connect_args=connect_args)
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _configure_sqlite_connection)
    return engine


def initialize_database(engine: Engine | None = None) -> Engine:
    target = engine or create_engine_for()
    with database_write_lock(target, purpose="initialize"):
        if target.dialect.name == "sqlite":
            with target.begin() as connection:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                connection.exec_driver_sql(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
                # WAL 只在初始化阶段设置一次，避免每个连接重复切换数据库日志模式。
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
            "document_quality_score": "FLOAT", "extraction_version": "VARCHAR(64)",
            "extraction_method": "VARCHAR(128)", "last_extracted_at": "DATETIME",
            "verification_required": "BOOLEAN DEFAULT 0", "verification_reason": "TEXT",
            "llm_extracted": "BOOLEAN DEFAULT 0",
            "raw_project_name": "VARCHAR(500)", "canonical_project_name": "VARCHAR(500)",
            "field_confidence": "FLOAT", "source_confidence": "FLOAT",
            "project_match_confidence": "FLOAT", "overall_confidence": "FLOAT",
            "completeness_score": "FLOAT", "needs_codex_review": "BOOLEAN DEFAULT 0",
            "review_reason": "TEXT", "status_rule_version": "VARCHAR(64)",
            "tender_status": "VARCHAR(16)", "relevance_class": "VARCHAR(32)",
            "verification_status": "VARCHAR(32)", "enrichment_state": "VARCHAR(32)",
            "blocker": "VARCHAR(64)", "next_action": "TEXT", "identity_status": "VARCHAR(32)",
            "identity_confidence": "FLOAT", "relation_types_json": "TEXT", "matched_concepts_json": "TEXT",
            "missing_fields_json": "TEXT", "project_location": "VARCHAR(500)",
            "tenderer_location": "VARCHAR(500)", "agency_location": "VARCHAR(500)",
            "source_location": "VARCHAR(500)", "rank_score": "FLOAT",
        },
        "announcements": {
            "original_url": "TEXT", "canonical_url": "TEXT", "clean_text": "TEXT", "snapshot_id": "VARCHAR(64)",
            "extraction_status": "VARCHAR(32) DEFAULT 'PENDING'", "extraction_parser": "VARCHAR(128)",
            "document_quality_score": "FLOAT", "extraction_version": "VARCHAR(64)", "processed_at": "DATETIME",
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
        "evidence": {
            "snapshot_id": "VARCHAR(64)", "document_id": "VARCHAR(64)",
            "candidate_id": "VARCHAR(128)",
            "sheet_name": "VARCHAR(256)", "cell_range": "VARCHAR(256)",
            "extractor_type": "VARCHAR(64)", "extractor_version": "VARCHAR(64)",
        },
        "manual_overrides": {
            "automatic_value": "TEXT", "manual_value": "TEXT", "changed_by": "VARCHAR(32) DEFAULT 'USER'",
        },
        "document_parses": {
            "document_id": "VARCHAR(64)", "project_id": "VARCHAR(128)", "source_id": "VARCHAR(128)",
            "document_type": "VARCHAR(64)", "file_path": "TEXT", "mime_type": "VARCHAR(128)",
            "parser_version": "VARCHAR(64)", "parse_error": "TEXT",
            "clean_text_path": "TEXT", "markdown_path": "TEXT", "parsed_at": "DATETIME",
            "candidate_id": "VARCHAR(128)",
        },
        "timeline_events": {
            "evidence_ids_json": "TEXT",
        },
        "search_sessions": {
            "request_id": "VARCHAR(128)",
            "search_mode": "VARCHAR(32)", "result_mode": "VARCHAR(32)",
            "sources_planned": "INTEGER DEFAULT 0",
            "sources_completed": "INTEGER DEFAULT 0",
            "sources_failed": "INTEGER DEFAULT 0",
            "queries_generated": "INTEGER DEFAULT 0",
            "projects_found": "INTEGER DEFAULT 0",
            "verification_count": "INTEGER DEFAULT 0",
            "candidate_pool_count": "INTEGER DEFAULT 0", "new_candidate_count": "INTEGER DEFAULT 0",
            "updated_candidate_count": "INTEGER DEFAULT 0", "reopened_candidate_count": "INTEGER DEFAULT 0",
            "enrichment_count": "INTEGER DEFAULT 0", "coverage_manifest_json": "TEXT", "quality_metrics_json": "TEXT",
            "source_plan_json": "TEXT",
        },
        "search_session_projects": {
            "is_reopened": "BOOLEAN DEFAULT 0",
            "candidate_id": "VARCHAR(128)", "relevance_class": "VARCHAR(32)",
            "verification_status": "VARCHAR(32)", "enrichment_state": "VARCHAR(32)",
            "blocker": "VARCHAR(64)", "next_action": "TEXT", "result_bucket": "VARCHAR(8)",
            "match_type": "VARCHAR(32)",
        },
        "candidate_enrichment_queries": {
            "content_hash": "VARCHAR(128)",
        },
        "candidates": {
            "content_hash": "VARCHAR(128)",
            "identity_confidence": "FLOAT DEFAULT 0",
            "completeness_score": "FLOAT DEFAULT 0",
            "enrichment_stop_reason": "VARCHAR(64)",
            "review_priority": "INTEGER DEFAULT 5",
        },
        "snapshots": {"candidate_id": "VARCHAR(128)"},
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
        "ix_projects_tender_status": "projects(tender_status)",
        "ix_projects_relevance_class": "projects(relevance_class)",
        "ix_projects_verification_status": "projects(verification_status)",
        "ix_projects_enrichment_state": "projects(enrichment_state)",
        "ix_projects_project_location": "projects(project_location)",
        "ix_announcements_canonical_url": "announcements(canonical_url)",
        "ix_project_sources_canonical_url": "project_sources(canonical_url)",
        "ix_projects_needs_codex_review": "projects(needs_codex_review)",
        "ix_evidence_snapshot_id": "evidence(snapshot_id)",
        "ix_evidence_document_id": "evidence(document_id)",
        "ix_document_parses_document_id": "document_parses(document_id)",
        "ix_document_parses_project_id": "document_parses(project_id)",
        "ix_document_parses_candidate_id": "document_parses(candidate_id)",
        "ix_snapshots_candidate_id": "snapshots(candidate_id)",
        "ix_search_sessions_started_at": "search_sessions(started_at)",
        "ix_search_sessions_request_id": "search_sessions(request_id)",
        "ix_search_session_projects_reopened": "search_session_projects(is_reopened)",
        "ix_search_session_projects_result_bucket": "search_session_projects(result_bucket)",
        "ix_candidates_canonical_url": "candidates(canonical_url)",
        "ix_candidates_content_hash": "candidates(content_hash)",
        "ix_candidates_tender_status": "candidates(tender_status)",
        "ix_candidates_enrichment_state": "candidates(enrichment_state)",
        "ix_candidates_completeness_score": "candidates(completeness_score)",
        "ix_candidates_enrichment_stop_reason": "candidates(enrichment_stop_reason)",
        "ix_candidates_review_priority": "candidates(review_priority)",
        "ix_candidate_sources_source_id": "candidate_sources(source_id)",
        "ix_candidate_enrichment_queries_candidate": "candidate_enrichment_queries(candidate_id)",
        "ix_candidate_enrichment_queries_content_hash": "candidate_enrichment_queries(content_hash)",
        "ix_candidate_enrichment_results_query": "candidate_enrichment_results(query_id)",
        "ix_candidate_enrichment_results_candidate": "candidate_enrichment_results(candidate_id)",
        "ix_candidate_enrichment_results_canonical_url": "candidate_enrichment_results(canonical_url)",
        "ix_candidate_facts_field": "candidate_facts(field_name)",
        "ix_candidate_attachments_candidate_id": "candidate_attachments(candidate_id)",
        "ix_candidate_attachments_content_hash": "candidate_attachments(content_hash)",
        "ix_candidate_attachments_snapshot_id": "candidate_attachments(snapshot_id)",
    }
    with engine.begin() as connection:
        for name, expression in indexes.items():
            connection.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS {name} ON {expression}")


def _ensure_fts5(engine: Engine) -> bool:
    if engine.dialect.name != "sqlite":
        return False
    try:
        with engine.begin() as connection:
            existing = connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table' AND name='tender_fts'").fetchone()
            if existing:
                columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(tender_fts)").fetchall()}
                if "canonical_project_name" not in columns or "raw_project_name" not in columns:
                    connection.exec_driver_sql("DROP TABLE tender_fts")
            connection.exec_driver_sql(
                "CREATE VIRTUAL TABLE IF NOT EXISTS tender_fts USING fts5("
                "project_id UNINDEXED, canonical_project_name, raw_project_name, project_name, owner, agency, qualification_summary, announcement_text)"
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
            "SELECT p.project_id, COALESCE(p.canonical_project_name, ''), COALESCE(p.raw_project_name, ''), p.project_name, p.owner, p.agency, p.qualification_summary, "
            "COALESCE(group_concat(a.clean_text, ' '), '') "
            "FROM projects p LEFT JOIN announcements a ON a.project_id=p.project_id GROUP BY p.project_id"
        ).fetchall()
        for row in rows:
            connection.exec_driver_sql(
                "INSERT INTO tender_fts(project_id, canonical_project_name, raw_project_name, project_name, owner, agency, qualification_summary, announcement_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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
            "INSERT INTO tender_fts(project_id, canonical_project_name, raw_project_name, project_name, owner, agency, qualification_summary, announcement_text) "
            "VALUES (:project_id, :canonical_project_name, :raw_project_name, :project_name, :owner, :agency, :qualification_summary, :announcement_text)"
        ),
        {
            "project_id": project_id,
            "canonical_project_name": getattr(project, "canonical_project_name", "") or "",
            "raw_project_name": getattr(project, "raw_project_name", "") or "",
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
        try:
            rows = session.execute(sql_text("SELECT project_id FROM tender_fts WHERE tender_fts MATCH :query LIMIT :limit"), {"query": query, "limit": limit}).all()
            matches = [str(row[0]) for row in rows]
            if matches:
                return matches
        except Exception:
            # 中文连续文本、短横线和用户输入的特殊字符可能让 FTS5
            # 分词器返回空集或语法错误，继续使用安全的参数化 LIKE。
            pass
    tokens = [token for token in query.split() if token] or [query]
    clauses: list[str] = []
    params: dict[str, object] = {"limit": limit}
    for index, token in enumerate(tokens):
        key = f"q{index}"
        params[key] = f"%{token}%"
        clauses.append(
            f"(project_name LIKE :{key} OR raw_project_name LIKE :{key} OR owner LIKE :{key} "
            f"OR agency LIKE :{key} OR qualification_summary LIKE :{key})"
        )
    rows = session.execute(
        sql_text(f"SELECT project_id FROM projects WHERE {' AND '.join(clauses)} LIMIT :limit"),
        params,
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
def session_scope(
    engine: Engine | None = None,
    *,
    lock_timeout_seconds: float | None = None,
) -> Iterator[Session]:
    """创建受本地单写入队列保护的数据库会话。

    当前抓取器会在会话上下文中执行网络请求。对 SQLite 而言，让多个进程
    同时持有长事务会产生锁竞争，因此在会话生命周期外层使用跨进程锁，
    将多个 Codex 对话的写入安全排队。
    """

    target = engine or create_engine_for()
    with database_write_lock(target, timeout_seconds=lock_timeout_seconds, purpose="session"):
        session = session_factory(target)()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


__all__ = [
    "DEFAULT_DB_PATH", "SQLITE_BUSY_TIMEOUT_MS", "DatabaseLockTimeoutError", "database_write_lock",
    "create_engine_for", "fts5_available", "initialize_database",
    "refresh_tender_fts", "resolve_database_url", "search_projects", "session_factory", "session_scope",
]
