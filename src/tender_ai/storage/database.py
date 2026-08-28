"""SQLite 数据库连接和初始化。"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from tender_ai.config_loader import APP_ROOT
from tender_ai.storage.models import Base


DEFAULT_DB_PATH = APP_ROOT.parent / "data" / "tender.db"


def resolve_database_url(database: str | Path | None = None) -> str:
    value = database or os.environ.get("TENDER_DATABASE_URL") or os.environ.get("TENDER_DB_PATH")
    if not value:
        return f"sqlite:///{DEFAULT_DB_PATH.as_posix()}"
    value = str(value)
    if "://" in value:
        return value
    return f"sqlite:///{Path(value).expanduser().resolve().as_posix()}"


def create_engine_for(database: str | Path | None = None, *, echo: bool = False) -> Engine:
    url = resolve_database_url(database)
    if url.startswith("sqlite:///") and ":memory:" not in url:
        db_path = Path(url.removeprefix("sqlite:///"))
        db_path.parent.mkdir(parents=True, exist_ok=True)
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, echo=echo, future=True, connect_args=connect_args)


def initialize_database(engine: Engine | None = None) -> Engine:
    target = engine or create_engine_for()
    Base.metadata.create_all(target)
    return target


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
