"""第4步：文档解析、时间线与二次核验运行时结构。"""

from alembic import op
import sqlalchemy as sa


revision = "0004_extraction_verification"
down_revision = "0003_architecture_resilience"
branch_labels = None
depends_on = None


def _add_columns(table: str, columns: list[tuple[str, sa.types.TypeEngine, dict]]) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns(table)}
    for name, column_type, kwargs in columns:
        if name not in existing:
            op.add_column(table, sa.Column(name, column_type, nullable=True, **kwargs))


def _index_if_missing(name: str, table: str, columns: list[str]) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if name not in {item["name"] for item in inspector.get_indexes(table)}:
        op.create_index(name, table, columns)


def upgrade() -> None:
    _add_columns(
        "projects",
        [
            ("document_quality_score", sa.Float(), {}),
            ("extraction_version", sa.String(64), {}),
            ("extraction_method", sa.String(128), {}),
            ("last_extracted_at", sa.DateTime(timezone=True), {}),
            ("verification_required", sa.Boolean(), {"server_default": "0"}),
            ("verification_reason", sa.Text(), {}),
            ("llm_extracted", sa.Boolean(), {"server_default": "0"}),
        ],
    )
    _add_columns(
        "announcements",
        [
            ("extraction_status", sa.String(32), {"server_default": "PENDING"}),
            ("extraction_parser", sa.String(128), {}),
            ("document_quality_score", sa.Float(), {}),
            ("extraction_version", sa.String(64), {}),
            ("processed_at", sa.DateTime(timezone=True), {}),
        ],
    )
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "document_parses" not in tables:
        op.create_table(
            "document_parses",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("announcement_id", sa.Integer(), sa.ForeignKey("announcements.id"), nullable=True),
            sa.Column("attachment_id", sa.Integer(), sa.ForeignKey("attachments.id"), nullable=True),
            sa.Column("source_url", sa.Text(), nullable=True),
            sa.Column("source_file", sa.Text(), nullable=True),
            sa.Column("content_hash", sa.String(128), nullable=False),
            sa.Column("content_type", sa.String(128), nullable=False),
            sa.Column("parser", sa.String(128), nullable=False),
            sa.Column("quality_score", sa.Float(), nullable=False, server_default="0"),
            sa.Column("page_count", sa.Integer(), nullable=True),
            sa.Column("text_length", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("used_ocr", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("used_mineru", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("parse_status", sa.String(32), nullable=False, server_default="SUCCESS"),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("metadata_json", sa.Text(), nullable=True),
            sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "timeline_events" not in tables:
        op.create_table(
            "timeline_events",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("project_id", sa.String(128), sa.ForeignKey("projects.project_id"), nullable=False),
            sa.Column("announcement_id", sa.Integer(), sa.ForeignKey("announcements.id"), nullable=True),
            sa.Column("event_type", sa.String(32), nullable=False),
            sa.Column("event_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("title", sa.String(500), nullable=False),
            sa.Column("source_url", sa.Text(), nullable=True),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column("deadline_snapshot_json", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "verification_tasks" not in tables:
        op.create_table(
            "verification_tasks",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("project_id", sa.String(128), sa.ForeignKey("projects.project_id"), nullable=False),
            sa.Column("reason", sa.String(128), nullable=False),
            sa.Column("status", sa.String(32), nullable=False, server_default="PENDING"),
            sa.Column("query_texts_json", sa.Text(), nullable=True),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("result_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "verification_results" not in tables:
        op.create_table(
            "verification_results",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("task_id", sa.Integer(), sa.ForeignKey("verification_tasks.id"), nullable=False),
            sa.Column("project_id", sa.String(128), sa.ForeignKey("projects.project_id"), nullable=False),
            sa.Column("query_text", sa.String(500), nullable=False),
            sa.Column("title", sa.String(500), nullable=False),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("snippet", sa.Text(), nullable=True),
            sa.Column("provider", sa.String(64), nullable=True),
            sa.Column("published_at", sa.String(64), nullable=True),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    for name, table, columns in [
        ("ix_document_parses_announcement_id", "document_parses", ["announcement_id"]),
        ("ix_document_parses_attachment_id", "document_parses", ["attachment_id"]),
        ("ix_document_parses_content_hash", "document_parses", ["content_hash"]),
        ("ix_timeline_events_project_id", "timeline_events", ["project_id"]),
        ("ix_timeline_events_event_type", "timeline_events", ["event_type"]),
        ("ix_verification_tasks_project_id", "verification_tasks", ["project_id"]),
        ("ix_verification_tasks_reason", "verification_tasks", ["reason"]),
        ("ix_verification_tasks_status", "verification_tasks", ["status"]),
        ("ix_verification_results_task_id", "verification_results", ["task_id"]),
        ("ix_verification_results_project_id", "verification_results", ["project_id"]),
    ]:
        _index_if_missing(name, table, columns)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table in ("verification_results", "verification_tasks", "timeline_events", "document_parses"):
        if table in inspector.get_table_names():
            op.drop_table(table)
