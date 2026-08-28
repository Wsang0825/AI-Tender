"""Add on-demand search session fields, search templates and lifecycle flags."""

from alembic import op
import sqlalchemy as sa


revision = "0007_stage5_product"
down_revision = "0006_timeline_evidence"
branch_labels = None
depends_on = None


def _columns(bind, table_name: str) -> set[str]:
    return {row[1] for row in bind.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()}


def upgrade() -> None:
    bind = op.get_bind()
    tables = {row[0] for row in bind.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "search_sessions" in tables:
        existing = _columns(bind, "search_sessions")
        additions = {
            "request_id": sa.Column("request_id", sa.String(128), nullable=True),
            "sources_planned": sa.Column("sources_planned", sa.Integer(), nullable=True, server_default="0"),
            "sources_completed": sa.Column("sources_completed", sa.Integer(), nullable=True, server_default="0"),
            "sources_failed": sa.Column("sources_failed", sa.Integer(), nullable=True, server_default="0"),
            "queries_generated": sa.Column("queries_generated", sa.Integer(), nullable=True, server_default="0"),
            "projects_found": sa.Column("projects_found", sa.Integer(), nullable=True, server_default="0"),
            "verification_count": sa.Column("verification_count", sa.Integer(), nullable=True, server_default="0"),
            "source_plan_json": sa.Column("source_plan_json", sa.Text(), nullable=True),
        }
        for name, column in additions.items():
            if name not in existing:
                op.add_column("search_sessions", column)
    if "search_session_projects" in tables:
        if "is_reopened" not in _columns(bind, "search_session_projects"):
            op.add_column("search_session_projects", sa.Column("is_reopened", sa.Boolean(), nullable=True, server_default="0"))
    if "search_templates" not in tables:
        op.create_table(
            "search_templates",
            sa.Column("template_id", sa.String(64), primary_key=True),
            sa.Column("name", sa.String(200), nullable=False, unique=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("request_json", sa.Text(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_search_templates_enabled", "search_templates", ["enabled"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = {row[0] for row in bind.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "search_templates" in tables:
        op.drop_index("ix_search_templates_enabled", table_name="search_templates")
        op.drop_table("search_templates")
    if "search_session_projects" in tables and "is_reopened" in _columns(bind, "search_session_projects"):
        op.drop_column("search_session_projects", "is_reopened")
    if "search_sessions" in tables:
        for name in ("source_plan_json", "verification_count", "projects_found", "queries_generated", "sources_failed", "sources_completed", "sources_planned", "request_id"):
            if name in _columns(bind, "search_sessions"):
                op.drop_column("search_sessions", name)
