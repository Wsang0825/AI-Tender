"""Add Codex review and on-demand search session storage."""

from alembic import op
import sqlalchemy as sa


revision = "0005_codex_review_search"
down_revision = "0004_extraction_verification"
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
            ("raw_project_name", sa.String(500), {}),
            ("canonical_project_name", sa.String(500), {}),
            ("field_confidence", sa.Float(), {}),
            ("source_confidence", sa.Float(), {}),
            ("project_match_confidence", sa.Float(), {}),
            ("overall_confidence", sa.Float(), {}),
            ("completeness_score", sa.Float(), {}),
            ("needs_codex_review", sa.Boolean(), {"server_default": "0"}),
            ("review_reason", sa.Text(), {}),
            ("status_rule_version", sa.String(64), {}),
        ],
    )
    _add_columns(
        "evidence",
        [
            ("snapshot_id", sa.String(64), {}),
            ("document_id", sa.String(64), {}),
            ("sheet_name", sa.String(256), {}),
            ("cell_range", sa.String(256), {}),
            ("extractor_type", sa.String(64), {}),
            ("extractor_version", sa.String(64), {}),
        ],
    )
    _add_columns(
        "manual_overrides",
        [
            ("automatic_value", sa.Text(), {}),
            ("manual_value", sa.Text(), {}),
            ("changed_by", sa.String(32), {"server_default": "USER"}),
        ],
    )
    _add_columns(
        "document_parses",
        [
            ("document_id", sa.String(64), {}),
            ("project_id", sa.String(128), {}),
            ("source_id", sa.String(128), {}),
            ("document_type", sa.String(64), {}),
            ("file_path", sa.Text(), {}),
            ("mime_type", sa.String(128), {}),
            ("parser_version", sa.String(64), {}),
            ("parse_error", sa.Text(), {}),
            ("clean_text_path", sa.Text(), {}),
            ("markdown_path", sa.Text(), {}),
            ("parsed_at", sa.DateTime(timezone=True), {}),
        ],
    )
    bind = op.get_bind()
    if "document_parses" in sa.inspect(bind).get_table_names():
        bind.exec_driver_sql("UPDATE document_parses SET document_id=lower(hex(randomblob(16))) WHERE document_id IS NULL")

    tables = set(sa.inspect(bind).get_table_names())
    if "field_conflicts" not in tables:
        op.create_table(
            "field_conflicts",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("project_id", sa.String(128), sa.ForeignKey("projects.project_id"), nullable=False),
            sa.Column("announcement_id", sa.Integer(), sa.ForeignKey("announcements.id"), nullable=True),
            sa.Column("field_name", sa.String(128), nullable=False),
            sa.Column("candidate_values_json", sa.Text(), nullable=False),
            sa.Column("evidence_ids_json", sa.Text(), nullable=True),
            sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("resolution_status", sa.String(32), nullable=False, server_default="PENDING"),
            sa.Column("resolution", sa.Text(), nullable=True),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "search_sessions" not in tables:
        op.create_table(
            "search_sessions",
            sa.Column("session_id", sa.String(64), primary_key=True),
            sa.Column("request_json", sa.Text(), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", sa.String(32), nullable=False, server_default="RUNNING"),
            sa.Column("query_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("candidate_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("open_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("unknown_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("closed_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("review_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("errors_json", sa.Text(), nullable=True),
            sa.Column("sources_json", sa.Text(), nullable=True),
        )
    if "search_session_projects" not in tables:
        op.create_table(
            "search_session_projects",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("session_id", sa.String(64), sa.ForeignKey("search_sessions.session_id"), nullable=False),
            sa.Column("project_id", sa.String(128), sa.ForeignKey("projects.project_id"), nullable=False),
            sa.Column("announcement_id", sa.Integer(), sa.ForeignKey("announcements.id"), nullable=True),
            sa.Column("found_via", sa.String(64), nullable=True),
            sa.Column("match_score", sa.Float(), nullable=True),
            sa.Column("matched_keywords", sa.Text(), nullable=True),
            sa.Column("matched_region", sa.String(256), nullable=True),
            sa.Column("status_at_search", sa.String(16), nullable=False),
            sa.Column("is_new", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("is_updated", sa.Boolean(), nullable=False, server_default="0"),
        )
    if "codex_review_items" not in tables:
        op.create_table(
            "codex_review_items",
            sa.Column("review_id", sa.String(64), primary_key=True),
            sa.Column("project_id", sa.String(128), sa.ForeignKey("projects.project_id"), nullable=False),
            sa.Column("announcement_id", sa.Integer(), sa.ForeignKey("announcements.id"), nullable=True),
            sa.Column("search_session_id", sa.String(64), sa.ForeignKey("search_sessions.session_id"), nullable=True),
            sa.Column("review_type", sa.String(64), nullable=False),
            sa.Column("reason", sa.String(128), nullable=False),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("source_url", sa.Text(), nullable=True),
            sa.Column("snapshot_id", sa.String(64), sa.ForeignKey("snapshots.snapshot_id"), nullable=True),
            sa.Column("document_paths", sa.Text(), nullable=True),
            sa.Column("candidate_values", sa.Text(), nullable=True),
            sa.Column("evidence_ids", sa.Text(), nullable=True),
            sa.Column("content_hash", sa.String(128), nullable=True),
            sa.Column("review_schema_version", sa.String(64), nullable=False, server_default="codex_review_v1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("resolution", sa.Text(), nullable=True),
            sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        )
    for name, table, columns in [
        ("ix_projects_canonical_project_name", "projects", ["canonical_project_name"]),
        ("ix_projects_needs_codex_review", "projects", ["needs_codex_review"]),
        ("ix_evidence_snapshot_id", "evidence", ["snapshot_id"]),
        ("ix_evidence_document_id", "evidence", ["document_id"]),
        ("ix_document_parses_document_id", "document_parses", ["document_id"]),
        ("ix_document_parses_project_id", "document_parses", ["project_id"]),
        ("ix_field_conflicts_project_id", "field_conflicts", ["project_id"]),
        ("ix_field_conflicts_resolution_status", "field_conflicts", ["resolution_status"]),
        ("ix_search_sessions_status", "search_sessions", ["status"]),
        ("ix_search_session_projects_session_id", "search_session_projects", ["session_id"]),
        ("ix_search_session_projects_project_id", "search_session_projects", ["project_id"]),
        ("ix_codex_review_items_project_id", "codex_review_items", ["project_id"]),
        ("ix_codex_review_items_search_session_id", "codex_review_items", ["search_session_id"]),
        ("ix_codex_review_items_reason", "codex_review_items", ["reason"]),
        ("ix_codex_review_items_status", "codex_review_items", ["status"]),
        ("ix_codex_review_items_content_hash", "codex_review_items", ["content_hash"]),
    ]:
        if table in sa.inspect(bind).get_table_names():
            _index_if_missing(name, table, columns)


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    for table in ("codex_review_items", "search_session_projects", "search_sessions", "field_conflicts"):
        if table in tables:
            op.drop_table(table)
