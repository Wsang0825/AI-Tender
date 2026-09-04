"""Close the candidate enrichment attachment/document loop.

This migration is additive.  It does not rewrite or remove any project,
announcement, snapshot, attachment, or evidence row.
"""

from alembic import op
import sqlalchemy as sa


revision = "0009_candidate_enrichment_closure"
down_revision = "0008_candidate_recall_enrichment"
branch_labels = None
depends_on = None


def _tables(bind) -> set[str]:
    return {row[0] for row in bind.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _columns(bind, table_name: str) -> set[str]:
    return {row[1] for row in bind.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()}


def _indexes(bind, table_name: str) -> set[str]:
    return {row[1] for row in bind.exec_driver_sql(f"PRAGMA index_list({table_name})").fetchall()}


def _add_columns(bind, table_name: str, additions: dict[str, sa.Column]) -> None:
    if table_name not in _tables(bind):
        return
    existing = _columns(bind, table_name)
    for name, column in additions.items():
        if name not in existing:
            op.add_column(table_name, column)


def _index(bind, name: str, table_name: str, columns: list[str]) -> None:
    if name not in _indexes(bind, table_name):
        op.create_index(name, table_name, columns)


def upgrade() -> None:
    bind = op.get_bind()
    _add_columns(bind, "candidates", {
        "completeness_score": sa.Column("completeness_score", sa.Float(), nullable=True, server_default="0"),
        "enrichment_stop_reason": sa.Column("enrichment_stop_reason", sa.String(64), nullable=True),
        "review_priority": sa.Column("review_priority", sa.Integer(), nullable=True, server_default="5"),
    })
    _add_columns(bind, "snapshots", {
        "candidate_id": sa.Column("candidate_id", sa.String(128), nullable=True),
    })
    _add_columns(bind, "document_parses", {
        "candidate_id": sa.Column("candidate_id", sa.String(128), nullable=True),
    })

    if "candidate_attachments" not in _tables(bind):
        op.create_table(
            "candidate_attachments",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("candidate_id", sa.String(128), sa.ForeignKey("candidates.candidate_id"), nullable=False),
            sa.Column("enrichment_result_id", sa.Integer(), sa.ForeignKey("candidate_enrichment_results.id"), nullable=True),
            sa.Column("source_url", sa.Text(), nullable=False),
            sa.Column("canonical_url", sa.Text(), nullable=False),
            sa.Column("file_name", sa.String(500), nullable=True),
            sa.Column("local_path", sa.Text(), nullable=True),
            sa.Column("mime_type", sa.String(128), nullable=True),
            sa.Column("content_hash", sa.String(128), nullable=True),
            sa.Column("snapshot_id", sa.String(64), sa.ForeignKey("snapshots.snapshot_id"), nullable=True),
            sa.Column("document_id", sa.String(64), nullable=True),
            sa.Column("download_status", sa.String(32), nullable=False, server_default="DISCOVERED"),
            sa.Column("parse_status", sa.String(32), nullable=False, server_default="PENDING"),
            sa.Column("parse_error", sa.Text(), nullable=True),
            sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("downloaded_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("metadata_json", sa.Text(), nullable=True),
            sa.UniqueConstraint("candidate_id", "canonical_url", name="uq_candidate_attachment_url"),
        )
    for name, table, columns in (
        ("ix_candidates_completeness_score", "candidates", ["completeness_score"]),
        ("ix_candidates_enrichment_stop_reason", "candidates", ["enrichment_stop_reason"]),
        ("ix_candidates_review_priority", "candidates", ["review_priority"]),
        ("ix_snapshots_candidate_id", "snapshots", ["candidate_id"]),
        ("ix_document_parses_candidate_id", "document_parses", ["candidate_id"]),
        ("ix_candidate_attachments_candidate_id", "candidate_attachments", ["candidate_id"]),
        ("ix_candidate_attachments_content_hash", "candidate_attachments", ["content_hash"]),
        ("ix_candidate_attachments_snapshot_id", "candidate_attachments", ["snapshot_id"]),
    ):
        if table in _tables(bind):
            _index(bind, name, table, columns)


def downgrade() -> None:
    # Deliberately conservative: candidate attachment history is user-auditable
    # state and is not removed by a routine downgrade.
    return
