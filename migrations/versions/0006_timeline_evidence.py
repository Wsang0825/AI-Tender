"""Add evidence references to project timeline events."""

from alembic import op
import sqlalchemy as sa


revision = "0006_timeline_evidence"
down_revision = "0005_codex_review_search"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = {row[0] for row in bind.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "timeline_events" not in tables:
        return
    columns = {row[1] for row in bind.exec_driver_sql("PRAGMA table_info(timeline_events)").fetchall()}
    if "evidence_ids_json" not in columns:
        op.add_column("timeline_events", sa.Column("evidence_ids_json", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    columns = {row[1] for row in bind.exec_driver_sql("PRAGMA table_info(timeline_events)").fetchall()}
    if "evidence_ids_json" in columns:
        op.drop_column("timeline_events", "evidence_ids_json")
