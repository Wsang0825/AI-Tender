"""SQLAlchemy 核心表模型。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from tender_ai.status.time import now_shanghai


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (CheckConstraint("status IN ('OPEN', 'UNKNOWN', 'CLOSED')", name="ck_projects_status"),)

    project_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    project_name: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    province: Mapped[str | None] = mapped_column(String(64), index=True)
    city: Mapped[str | None] = mapped_column(String(128), index=True)
    county: Mapped[str | None] = mapped_column(String(128), index=True)
    location: Mapped[str | None] = mapped_column(String(500))
    owner: Mapped[str | None] = mapped_column(String(500), index=True)
    purchaser: Mapped[str | None] = mapped_column(String(500))
    tenderer: Mapped[str | None] = mapped_column(String(500))
    agency: Mapped[str | None] = mapped_column(String(500))
    industry: Mapped[str | None] = mapped_column(String(128), index=True)
    sub_industry: Mapped[str | None] = mapped_column(String(128))
    project_type: Mapped[str | None] = mapped_column(String(128))
    announcement_type: Mapped[str | None] = mapped_column(String(128), index=True)
    project_scale: Mapped[str | None] = mapped_column(String(500))
    capacity_mw: Mapped[float | None] = mapped_column(Float)
    capacity_mwh: Mapped[float | None] = mapped_column(Float)
    budget: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    project_code: Mapped[str | None] = mapped_column(String(256), index=True)
    tender_code: Mapped[str | None] = mapped_column(String(256), index=True)
    publish_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    qualification_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    qualification_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    registration_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    registration_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    document_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    document_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bid_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    open_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    qualification_summary: Mapped[str | None] = mapped_column(Text)
    participation_method: Mapped[str | None] = mapped_column(Text)
    source_name: Mapped[str | None] = mapped_column(String(256))
    source_type: Mapped[str | None] = mapped_column(String(64))
    source_level: Mapped[str | None] = mapped_column(String(64))
    source_url: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="UNKNOWN", index=True)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, onupdate=now_shanghai, nullable=False)


class Announcement(Base):
    __tablename__ = "announcements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"), nullable=False, index=True)
    announcement_type: Mapped[str | None] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    content_hash: Mapped[str | None] = mapped_column(String(128), index=True)
    raw_content: Mapped[str | None] = mapped_column(Text)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)


class Source(Base):
    __tablename__ = "sources"

    source_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source_name: Mapped[str] = mapped_column(String(256), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    region: Mapped[str | None] = mapped_column(String(128), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    access_method: Mapped[str] = mapped_column(String(64), nullable=False, default="http_public")
    requires_login: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    adapter: Mapped[str] = mapped_column(String(256), nullable=False, default="configured")
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="registry_only")
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)


class ProjectSource(Base):
    __tablename__ = "project_sources"
    __table_args__ = (UniqueConstraint("project_id", "source_id", name="uq_project_source"),)

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.source_id"), primary_key=True)
    source_url: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, onupdate=now_shanghai, nullable=False)


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"), nullable=False, index=True)
    announcement_id: Mapped[int | None] = mapped_column(ForeignKey("announcements.id"), index=True)
    file_name: Mapped[str | None] = mapped_column(String(500))
    source_url: Mapped[str | None] = mapped_column(Text)
    local_path: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(String(128))
    content_hash: Mapped[str | None] = mapped_column(String(128), index=True)
    page_count: Mapped[int | None] = mapped_column(Integer)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Evidence(Base):
    __tablename__ = "evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.project_id"), index=True)
    announcement_id: Mapped[int | None] = mapped_column(ForeignKey("announcements.id"), index=True)
    field_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    normalized_value: Mapped[str | None] = mapped_column(Text)
    raw_value: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    source_file: Mapped[str | None] = mapped_column(Text)
    page_number: Mapped[int | None] = mapped_column(Integer)
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    extractor: Mapped[str] = mapped_column(String(128), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)


class StatusHistory(Base):
    __tablename__ = "status_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"), nullable=False, index=True)
    old_status: Mapped[str | None] = mapped_column(String(16))
    new_status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)


class ChangeHistory(Base):
    __tablename__ = "change_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"), nullable=False, index=True)
    announcement_id: Mapped[int | None] = mapped_column(ForeignKey("announcements.id"), index=True)
    change_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    field_name: Mapped[str] = mapped_column(String(128), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)


class CrawlRun(Base):
    __tablename__ = "crawl_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.source_id"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="RUNNING")
    query_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notes: Mapped[str | None] = mapped_column(Text)


class CrawlError(Base):
    __tablename__ = "crawl_errors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    crawl_run_id: Mapped[int | None] = mapped_column(ForeignKey("crawl_runs.id"), index=True)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.source_id"), index=True)
    url: Mapped[str | None] = mapped_column(Text)
    error_type: Mapped[str] = mapped_column(String(128), nullable=False)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)


class DiscoveredSource(Base):
    __tablename__ = "discovered_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    source_name: Mapped[str | None] = mapped_column(String(256))
    discovery_method: Mapped[str | None] = mapped_column(String(128))
    region: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="DISCOVERED")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)


class SearchQuery(Base):
    __tablename__ = "search_queries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    query_text: Mapped[str] = mapped_column(String(500), nullable=False)
    category: Mapped[str | None] = mapped_column(String(128))
    region: Mapped[str | None] = mapped_column(String(128), index=True)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.source_id"), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)


__all__ = [
    "Announcement",
    "Attachment",
    "Base",
    "CrawlError",
    "CrawlRun",
    "ChangeHistory",
    "DiscoveredSource",
    "Evidence",
    "Project",
    "ProjectSource",
    "SearchQuery",
    "Source",
    "StatusHistory",
]
