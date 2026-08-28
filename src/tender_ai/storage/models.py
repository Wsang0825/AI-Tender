"""SQLAlchemy 核心表模型。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import uuid4

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
    original_url: Mapped[str | None] = mapped_column(Text)
    canonical_url: Mapped[str | None] = mapped_column(Text, index=True)
    content_hash: Mapped[str | None] = mapped_column(String(128), index=True)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="UNKNOWN", index=True)
    status_reason: Mapped[str | None] = mapped_column(String(128), index=True)
    status_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lifecycle_state: Mapped[str] = mapped_column(String(16), nullable=False, default="NEW", index=True)
    last_change_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    favorite: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    ignored: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    ignore_reason: Mapped[str | None] = mapped_column(Text)
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
    original_url: Mapped[str | None] = mapped_column(Text)
    canonical_url: Mapped[str | None] = mapped_column(Text, index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    content_hash: Mapped[str | None] = mapped_column(String(128), index=True)
    raw_content: Mapped[str | None] = mapped_column(Text)
    clean_text: Mapped[str | None] = mapped_column(Text)
    snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("snapshots.snapshot_id"), index=True)
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
    adapter_level: Mapped[str] = mapped_column(String(32), nullable=False, default="CUSTOM_HTTP")
    adapter_config: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(64), nullable=False, default="registry_only")
    crawl_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    max_pages: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    request_delay_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.2)
    crawl_interval: Mapped[int] = mapped_column(Integer, nullable=False, default=86400)
    rate_limit: Mapped[float] = mapped_column(Float, nullable=False, default=0.2)
    lookback_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    browser_profile_path: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_http_status: Mapped[int | None] = mapped_column(Integer)
    runtime_status: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE", index=True)
    health_reason: Mapped[str | None] = mapped_column(String(64), index=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    average_items: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    latest_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_health_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)


class ProjectSource(Base):
    __tablename__ = "project_sources"
    __table_args__ = (UniqueConstraint("project_id", "source_id", name="uq_project_source"),)

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.source_id"), primary_key=True)
    source_url: Mapped[str | None] = mapped_column(Text)
    original_url: Mapped[str | None] = mapped_column(Text)
    canonical_url: Mapped[str | None] = mapped_column(Text, index=True)
    content_hash: Mapped[str | None] = mapped_column(String(128), index=True)
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
    run_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, default=lambda: uuid4().hex)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.source_id"), index=True)
    profile_id: Mapped[str | None] = mapped_column(String(128), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="RUNNING")
    query_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    new_item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attachment_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    new_domain_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    wechat_candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    checkpoint: Mapped[str | None] = mapped_column(Text)
    items_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_new: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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
    original_url: Mapped[str | None] = mapped_column(Text)
    canonical_url: Mapped[str | None] = mapped_column(Text, index=True)
    source_name: Mapped[str | None] = mapped_column(String(256))
    domain: Mapped[str | None] = mapped_column(String(256), index=True)
    discovery_method: Mapped[str | None] = mapped_column(String(128))
    region: Mapped[str | None] = mapped_column(String(128))
    projects_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_level_guess: Mapped[str | None] = mapped_column(String(8))
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="DISCOVERED")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)


class SearchQuery(Base):
    __tablename__ = "search_queries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    query_text: Mapped[str] = mapped_column(String(500), nullable=False)
    category: Mapped[str | None] = mapped_column(String(128))
    region: Mapped[str | None] = mapped_column(String(128), index=True)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.source_id"), index=True)
    profile_id: Mapped[str | None] = mapped_column(String(128), index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    results_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    new_results_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=5, index=True)
    new_project_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    run_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)


class Snapshot(Base):
    __tablename__ = "snapshots"
    __table_args__ = (UniqueConstraint("sha256", name="uq_snapshots_sha256"),)

    snapshot_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str | None] = mapped_column(Text, index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.source_id"), index=True)
    announcement_id: Mapped[int | None] = mapped_column(ForeignKey("announcements.id"), index=True)
    metadata_json: Mapped[str | None] = mapped_column(Text)


class TimeFieldMetadata(Base):
    __tablename__ = "time_field_metadata"
    __table_args__ = (UniqueConstraint("project_id", "field_name", "value", name="uq_time_field_value"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"), nullable=False, index=True)
    field_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    value: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Shanghai")
    precision: Mapped[str] = mapped_column(String(16), nullable=False, default="UNKNOWN")
    explicit_or_inferred: Mapped[str] = mapped_column(String(16), nullable=False, default="EXPLICIT")
    source_evidence_id: Mapped[int | None] = mapped_column(ForeignKey("evidence.id"), index=True)
    inference_rule: Mapped[str | None] = mapped_column(Text)
    raw_value: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)


class ManualOverride(Base):
    __tablename__ = "manual_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"), nullable=False, index=True)
    field_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)


class LLMExtractionCache(Base):
    __tablename__ = "llm_extraction_cache"
    __table_args__ = (UniqueConstraint("content_hash", "model", "prompt_version", name="uq_llm_cache_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    model: Mapped[str] = mapped_column(String(256), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(128), nullable=False)
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)


class SystemMetadata(Base):
    __tablename__ = "system_metadata"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, onupdate=now_shanghai, nullable=False)


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
    "Snapshot",
    "SearchQuery",
    "Source",
    "StatusHistory",
    "TimeFieldMetadata",
    "ManualOverride",
    "LLMExtractionCache",
    "SystemMetadata",
]
