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
    raw_project_name: Mapped[str | None] = mapped_column(String(500))
    canonical_project_name: Mapped[str | None] = mapped_column(String(500), index=True)
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
    # Candidate discovery and tender status are deliberately independent.  The
    # legacy ``status`` column remains the canonical status-engine field for
    # backwards compatibility; ``tender_status`` is the explicit public name
    # used by the candidate/enrichment layer.
    tender_status: Mapped[str | None] = mapped_column(String(16), index=True)
    status_reason: Mapped[str | None] = mapped_column(String(128), index=True)
    status_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lifecycle_state: Mapped[str] = mapped_column(String(16), nullable=False, default="NEW", index=True)
    last_change_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    favorite: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    ignored: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    ignore_reason: Mapped[str | None] = mapped_column(Text)
    document_quality_score: Mapped[float | None] = mapped_column(Float)
    extraction_version: Mapped[str | None] = mapped_column(String(64))
    extraction_method: Mapped[str | None] = mapped_column(String(128))
    last_extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    verification_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    verification_reason: Mapped[str | None] = mapped_column(Text)
    llm_extracted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    field_confidence: Mapped[float | None] = mapped_column(Float)
    source_confidence: Mapped[float | None] = mapped_column(Float)
    project_match_confidence: Mapped[float | None] = mapped_column(Float)
    overall_confidence: Mapped[float | None] = mapped_column(Float)
    completeness_score: Mapped[float | None] = mapped_column(Float)
    needs_codex_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    review_reason: Mapped[str | None] = mapped_column(Text)
    status_rule_version: Mapped[str | None] = mapped_column(String(64))
    relevance_class: Mapped[str | None] = mapped_column(String(32), index=True)
    verification_status: Mapped[str | None] = mapped_column(String(32), index=True)
    enrichment_state: Mapped[str | None] = mapped_column(String(32), index=True)
    blocker: Mapped[str | None] = mapped_column(String(64), index=True)
    next_action: Mapped[str | None] = mapped_column(Text)
    identity_status: Mapped[str | None] = mapped_column(String(32), index=True)
    identity_confidence: Mapped[float | None] = mapped_column(Float)
    relation_types_json: Mapped[str | None] = mapped_column(Text)
    matched_concepts_json: Mapped[str | None] = mapped_column(Text)
    missing_fields_json: Mapped[str | None] = mapped_column(Text)
    project_location: Mapped[str | None] = mapped_column(String(500), index=True)
    tenderer_location: Mapped[str | None] = mapped_column(String(500))
    agency_location: Mapped[str | None] = mapped_column(String(500))
    source_location: Mapped[str | None] = mapped_column(String(500))
    rank_score: Mapped[float | None] = mapped_column(Float)
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
    extraction_status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING", index=True)
    extraction_parser: Mapped[str | None] = mapped_column(String(128))
    document_quality_score: Mapped[float | None] = mapped_column(Float)
    extraction_version: Mapped[str | None] = mapped_column(String(64))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    candidate_id: Mapped[str | None] = mapped_column(ForeignKey("candidates.candidate_id"), index=True)
    announcement_id: Mapped[int | None] = mapped_column(ForeignKey("announcements.id"), index=True)
    field_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    normalized_value: Mapped[str | None] = mapped_column(Text)
    raw_value: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    source_file: Mapped[str | None] = mapped_column(Text)
    snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("snapshots.snapshot_id"), index=True)
    document_id: Mapped[str | None] = mapped_column(String(64), index=True)
    page_number: Mapped[int | None] = mapped_column(Integer)
    sheet_name: Mapped[str | None] = mapped_column(String(256))
    cell_range: Mapped[str | None] = mapped_column(String(256))
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    extractor: Mapped[str] = mapped_column(String(128), nullable=False)
    extractor_type: Mapped[str | None] = mapped_column(String(64))
    extractor_version: Mapped[str | None] = mapped_column(String(64))
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
    candidate_id: Mapped[str | None] = mapped_column(ForeignKey("candidates.candidate_id"), index=True)
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
    automatic_value: Mapped[str | None] = mapped_column(Text)
    manual_value: Mapped[str | None] = mapped_column(Text)
    changed_by: Mapped[str] = mapped_column(String(32), nullable=False, default="USER")
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


class DocumentParse(Base):
    __tablename__ = "document_parses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, default=lambda: uuid4().hex)
    announcement_id: Mapped[int | None] = mapped_column(ForeignKey("announcements.id"), index=True)
    attachment_id: Mapped[int | None] = mapped_column(ForeignKey("attachments.id"), index=True)
    candidate_id: Mapped[str | None] = mapped_column(ForeignKey("candidates.candidate_id"), index=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.project_id"), index=True)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.source_id"), index=True)
    document_type: Mapped[str | None] = mapped_column(String(64))
    file_path: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(String(128))
    source_url: Mapped[str | None] = mapped_column(Text)
    source_file: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    parser: Mapped[str] = mapped_column(String(128), nullable=False)
    parser_version: Mapped[str | None] = mapped_column(String(64))
    quality_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    page_count: Mapped[int | None] = mapped_column(Integer)
    text_length: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    used_ocr: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    used_mineru: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    parse_status: Mapped[str] = mapped_column(String(32), nullable=False, default="SUCCESS", index=True)
    error: Mapped[str | None] = mapped_column(Text)
    parse_error: Mapped[str | None] = mapped_column(Text)
    clean_text_path: Mapped[str | None] = mapped_column(Text)
    markdown_path: Mapped[str | None] = mapped_column(Text)
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[str | None] = mapped_column(Text)
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)


class TimelineEvent(Base):
    __tablename__ = "timeline_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"), nullable=False, index=True)
    announcement_id: Mapped[int | None] = mapped_column(ForeignKey("announcements.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    deadline_snapshot_json: Mapped[str | None] = mapped_column(Text)
    evidence_ids_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)


class VerificationTask(Base):
    __tablename__ = "verification_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING", index=True)
    query_texts_json: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, onupdate=now_shanghai, nullable=False)


class VerificationResult(Base):
    __tablename__ = "verification_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("verification_tasks.id"), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"), nullable=False, index=True)
    query_text: Mapped[str] = mapped_column(String(500), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    snippet: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(String(64))
    published_at: Mapped[str | None] = mapped_column(String(64))
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)


class FieldConflict(Base):
    __tablename__ = "field_conflicts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"), nullable=False, index=True)
    announcement_id: Mapped[int | None] = mapped_column(ForeignKey("announcements.id"), index=True)
    field_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    candidate_values_json: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_ids_json: Mapped[str | None] = mapped_column(Text)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)
    resolution_status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING", index=True)
    resolution: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SearchSession(Base):
    __tablename__ = "search_sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str | None] = mapped_column(String(128), index=True)
    request_json: Mapped[str] = mapped_column(Text, nullable=False)
    search_mode: Mapped[str | None] = mapped_column(String(32), index=True)
    result_mode: Mapped[str | None] = mapped_column(String(32), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="RUNNING", index=True)
    sources_planned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sources_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sources_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    queries_generated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    query_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    projects_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    open_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unknown_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    closed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    review_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    verification_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidate_pool_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    new_candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reopened_candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enrichment_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errors_json: Mapped[str | None] = mapped_column(Text)
    sources_json: Mapped[str | None] = mapped_column(Text)
    source_plan_json: Mapped[str | None] = mapped_column(Text)
    coverage_manifest_json: Mapped[str | None] = mapped_column(Text)
    quality_metrics_json: Mapped[str | None] = mapped_column(Text)


class Candidate(Base):
    """Durable recall layer between discovery and verified Project records.

    A Candidate is intentionally allowed to be incomplete, secondary-only,
    blocked, or unrelated to a persisted Project.  Search filters may hide it
    from a particular presentation, but they must not delete the recall fact.
    """

    __tablename__ = "candidates"
    __table_args__ = (UniqueConstraint("candidate_key", name="uq_candidates_candidate_key"),)

    candidate_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    candidate_key: Mapped[str] = mapped_column(String(256), nullable=False, unique=True, index=True)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.project_id"), index=True)
    announcement_id: Mapped[int | None] = mapped_column(ForeignKey("announcements.id"), index=True)
    search_session_id: Mapped[str | None] = mapped_column(ForeignKey("search_sessions.session_id"), index=True)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.source_id"), index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    raw_title: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    original_url: Mapped[str | None] = mapped_column(Text)
    canonical_url: Mapped[str | None] = mapped_column(Text, index=True)
    snippet: Mapped[str | None] = mapped_column(Text)
    source_domain: Mapped[str | None] = mapped_column(String(256), index=True)
    source_level: Mapped[str | None] = mapped_column(String(8), index=True)
    content_hash: Mapped[str | None] = mapped_column(String(128), index=True)
    relevance_class: Mapped[str] = mapped_column(String(32), nullable=False, default="POSSIBLE", index=True)
    verification_status: Mapped[str] = mapped_column(String(32), nullable=False, default="DISCOVERY_LEAD", index=True)
    tender_status: Mapped[str] = mapped_column(String(16), nullable=False, default="UNKNOWN", index=True)
    enrichment_state: Mapped[str] = mapped_column(String(32), nullable=False, default="NEW", index=True)
    identity_status: Mapped[str] = mapped_column(String(32), nullable=False, default="UNRESOLVED", index=True)
    identity_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    blocker: Mapped[str | None] = mapped_column(String(64), index=True)
    next_action: Mapped[str | None] = mapped_column(Text)
    candidate_class: Mapped[str | None] = mapped_column(String(64), index=True)
    relation_types_json: Mapped[str | None] = mapped_column(Text)
    matched_concepts_json: Mapped[str | None] = mapped_column(Text)
    missing_fields_json: Mapped[str | None] = mapped_column(Text)
    candidate_values_json: Mapped[str | None] = mapped_column(Text)
    evidence_ids_json: Mapped[str | None] = mapped_column(Text)
    source_ids_json: Mapped[str | None] = mapped_column(Text)
    official_found: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    rank_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, index=True)
    completeness_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, index=True)
    enrichment_stop_reason: Mapped[str | None] = mapped_column(String(64), index=True)
    review_priority: Mapped[int] = mapped_column(Integer, nullable=False, default=5, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, onupdate=now_shanghai, nullable=False)
    last_enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_change_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    persisted_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, onupdate=now_shanghai, nullable=False)


class CandidateSource(Base):
    """Source pivot for all URLs that support one Candidate identity."""

    __tablename__ = "candidate_sources"
    __table_args__ = (UniqueConstraint("candidate_id", "canonical_url", name="uq_candidate_source_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.candidate_id"), nullable=False, index=True)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.source_id"), index=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    original_url: Mapped[str | None] = mapped_column(Text)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    source_domain: Mapped[str | None] = mapped_column(String(256), index=True)
    source_name: Mapped[str | None] = mapped_column(String(256))
    source_level: Mapped[str | None] = mapped_column(String(8), index=True)
    source_type: Mapped[str | None] = mapped_column(String(64))
    provider: Mapped[str | None] = mapped_column(String(64))
    source_title: Mapped[str | None] = mapped_column(String(500))
    snippet: Mapped[str | None] = mapped_column(Text)
    source_location: Mapped[str | None] = mapped_column(String(500))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    content_hash: Mapped[str | None] = mapped_column(String(128), index=True)
    is_official: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    is_secondary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    access_status: Mapped[str] = mapped_column(String(32), nullable=False, default="DISCOVERED")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, onupdate=now_shanghai, nullable=False)


class CandidateEnrichmentQuery(Base):
    __tablename__ = "candidate_enrichment_queries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.candidate_id"), nullable=False, index=True)
    search_session_id: Mapped[str | None] = mapped_column(ForeignKey("search_sessions.session_id"), index=True)
    parent_query_id: Mapped[int | None] = mapped_column(ForeignKey("candidate_enrichment_queries.id"), index=True)
    query_text: Mapped[str] = mapped_column(String(1000), nullable=False)
    strategy: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    round_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    provider: Mapped[str | None] = mapped_column(String(64))
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.source_id"), index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING", index=True)
    results_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidate_hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    new_fact_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    new_source_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    query_hash: Mapped[str | None] = mapped_column(String(128), index=True)
    content_hash: Mapped[str | None] = mapped_column(String(128), index=True)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)


class CandidateEnrichmentResult(Base):
    """One durable result returned by one recursive enrichment query.

    ``CandidateSource`` stores the source pivot; this table keeps the query
    provenance as well, so a later audit can answer which strategy found a
    URL and which seed candidate it was meant to enrich.
    """

    __tablename__ = "candidate_enrichment_results"
    __table_args__ = (UniqueConstraint("query_id", "canonical_url", name="uq_candidate_enrichment_result"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    query_id: Mapped[int] = mapped_column(ForeignKey("candidate_enrichment_queries.id"), nullable=False, index=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.candidate_id"), nullable=False, index=True)
    discovered_candidate_id: Mapped[str | None] = mapped_column(ForeignKey("candidates.candidate_id"), index=True)
    search_session_id: Mapped[str | None] = mapped_column(ForeignKey("search_sessions.session_id"), index=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    snippet: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(String(64))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    source_level: Mapped[str | None] = mapped_column(String(8), index=True)
    content_hash: Mapped[str | None] = mapped_column(String(128), index=True)
    identity_status: Mapped[str | None] = mapped_column(String(32), index=True)
    relevance_class: Mapped[str | None] = mapped_column(String(32), index=True)
    is_official: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    is_secondary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    match_type: Mapped[str | None] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)


class CandidateAttachment(Base):
    """附件闭环 for a candidate that has not yet become a Project.

    Project ``Attachment`` remains the canonical project-level table.  This
    small parallel record is necessary because discovery candidates may be
    secondary-only or incomplete and therefore have no project_id yet.
    """

    __tablename__ = "candidate_attachments"
    __table_args__ = (UniqueConstraint("candidate_id", "canonical_url", name="uq_candidate_attachment_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.candidate_id"), nullable=False, index=True)
    enrichment_result_id: Mapped[int | None] = mapped_column(ForeignKey("candidate_enrichment_results.id"), index=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    file_name: Mapped[str | None] = mapped_column(String(500))
    local_path: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(String(128))
    content_hash: Mapped[str | None] = mapped_column(String(128), index=True)
    snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("snapshots.snapshot_id"), index=True)
    document_id: Mapped[str | None] = mapped_column(String(64), index=True)
    download_status: Mapped[str] = mapped_column(String(32), nullable=False, default="DISCOVERED", index=True)
    parse_status: Mapped[str] = mapped_column(String(32), nullable=False, default="PENDING", index=True)
    parse_error: Mapped[str | None] = mapped_column(Text)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[str | None] = mapped_column(Text)


class CandidateFact(Base):
    __tablename__ = "candidate_facts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.candidate_id"), nullable=False, index=True)
    field_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    value: Mapped[str | None] = mapped_column(Text)
    normalized_value: Mapped[str | None] = mapped_column(Text)
    raw_value: Mapped[str | None] = mapped_column(Text)
    evidence_id: Mapped[int | None] = mapped_column(ForeignKey("evidence.id"), index=True)
    source_url: Mapped[str | None] = mapped_column(Text)
    source_level: Mapped[str | None] = mapped_column(String(8), index=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)


class SourcePivot(Base):
    __tablename__ = "source_pivots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidates.candidate_id"), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    entity_value: Mapped[str] = mapped_column(String(500), nullable=False, index=True)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.source_id"), index=True)
    discovered_url: Mapped[str | None] = mapped_column(Text)
    domain: Mapped[str | None] = mapped_column(String(256), index=True)
    strategy: Mapped[str | None] = mapped_column(String(64))
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="DISCOVERED", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)


class SearchSessionProject(Base):
    __tablename__ = "search_session_projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("search_sessions.session_id"), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"), nullable=False, index=True)
    candidate_id: Mapped[str | None] = mapped_column(ForeignKey("candidates.candidate_id"), index=True)
    announcement_id: Mapped[int | None] = mapped_column(ForeignKey("announcements.id"), index=True)
    found_via: Mapped[str | None] = mapped_column(String(64))
    match_score: Mapped[float | None] = mapped_column(Float)
    matched_keywords: Mapped[str | None] = mapped_column(Text)
    matched_region: Mapped[str | None] = mapped_column(String(256))
    status_at_search: Mapped[str] = mapped_column(String(16), nullable=False)
    is_new: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_updated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_reopened: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    relevance_class: Mapped[str | None] = mapped_column(String(32), index=True)
    verification_status: Mapped[str | None] = mapped_column(String(32), index=True)
    enrichment_state: Mapped[str | None] = mapped_column(String(32), index=True)
    blocker: Mapped[str | None] = mapped_column(String(64), index=True)
    next_action: Mapped[str | None] = mapped_column(Text)
    result_bucket: Mapped[str | None] = mapped_column(String(8), index=True)
    match_type: Mapped[str | None] = mapped_column(String(32), index=True)


class CodexReviewItem(Base):
    __tablename__ = "codex_review_items"

    review_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.project_id"), nullable=False, index=True)
    announcement_id: Mapped[int | None] = mapped_column(ForeignKey("announcements.id"), index=True)
    search_session_id: Mapped[str | None] = mapped_column(ForeignKey("search_sessions.session_id"), index=True)
    review_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=5, index=True)
    source_url: Mapped[str | None] = mapped_column(Text)
    snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("snapshots.snapshot_id"), index=True)
    document_paths: Mapped[str | None] = mapped_column(Text)
    candidate_values: Mapped[str | None] = mapped_column(Text)
    evidence_ids: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String(128), index=True)
    review_schema_version: Mapped[str] = mapped_column(String(64), nullable=False, default="codex_review_v1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING", index=True)


class SearchTemplate(Base):
    __tablename__ = "search_templates"

    template_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    request_json: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, onupdate=now_shanghai, nullable=False)


class SystemMetadata(Base):
    __tablename__ = "system_metadata"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_shanghai, onupdate=now_shanghai, nullable=False)


__all__ = [
    "Announcement",
    "Attachment",
    "Base",
    "Candidate",
    "CandidateAttachment",
    "CandidateEnrichmentQuery",
    "CandidateEnrichmentResult",
    "CandidateFact",
    "CandidateSource",
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
    "DocumentParse",
    "TimelineEvent",
    "VerificationTask",
    "VerificationResult",
    "FieldConflict",
    "SearchSession",
    "SearchSessionProject",
    "SourcePivot",
    "CodexReviewItem",
    "SearchTemplate",
    "SystemMetadata",
]
