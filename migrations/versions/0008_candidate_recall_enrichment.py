"""Add the durable candidate recall and enrichment layer.

This migration is additive.  Existing projects, announcements, snapshots and
evidence remain intact; the new tables are deliberately allowed to contain
incomplete or blocked candidates.
"""

from alembic import op
import sqlalchemy as sa


revision = "0008_candidate_recall_enrichment"
down_revision = "0007_stage5_product"
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


def _create_index_if_missing(bind, name: str, table_name: str, columns: list[str]) -> None:
    if name not in _indexes(bind, table_name):
        op.create_index(name, table_name, columns)


def upgrade() -> None:
    bind = op.get_bind()

    _add_columns(bind, "projects", {
        "tender_status": sa.Column("tender_status", sa.String(16), nullable=True),
        "relevance_class": sa.Column("relevance_class", sa.String(32), nullable=True),
        "verification_status": sa.Column("verification_status", sa.String(32), nullable=True),
        "enrichment_state": sa.Column("enrichment_state", sa.String(32), nullable=True),
        "blocker": sa.Column("blocker", sa.String(64), nullable=True),
        "next_action": sa.Column("next_action", sa.Text(), nullable=True),
        "identity_status": sa.Column("identity_status", sa.String(32), nullable=True),
        "identity_confidence": sa.Column("identity_confidence", sa.Float(), nullable=True),
        "relation_types_json": sa.Column("relation_types_json", sa.Text(), nullable=True),
        "matched_concepts_json": sa.Column("matched_concepts_json", sa.Text(), nullable=True),
        "missing_fields_json": sa.Column("missing_fields_json", sa.Text(), nullable=True),
        "project_location": sa.Column("project_location", sa.String(500), nullable=True),
        "tenderer_location": sa.Column("tenderer_location", sa.String(500), nullable=True),
        "agency_location": sa.Column("agency_location", sa.String(500), nullable=True),
        "source_location": sa.Column("source_location", sa.String(500), nullable=True),
        "rank_score": sa.Column("rank_score", sa.Float(), nullable=True),
    })
    _add_columns(bind, "evidence", {
        "candidate_id": sa.Column("candidate_id", sa.String(128), nullable=True),
    })
    _add_columns(bind, "search_sessions", {
        "search_mode": sa.Column("search_mode", sa.String(32), nullable=True),
        "result_mode": sa.Column("result_mode", sa.String(32), nullable=True),
        "candidate_pool_count": sa.Column("candidate_pool_count", sa.Integer(), nullable=True, server_default="0"),
        "new_candidate_count": sa.Column("new_candidate_count", sa.Integer(), nullable=True, server_default="0"),
        "updated_candidate_count": sa.Column("updated_candidate_count", sa.Integer(), nullable=True, server_default="0"),
        "reopened_candidate_count": sa.Column("reopened_candidate_count", sa.Integer(), nullable=True, server_default="0"),
        "enrichment_count": sa.Column("enrichment_count", sa.Integer(), nullable=True, server_default="0"),
        "coverage_manifest_json": sa.Column("coverage_manifest_json", sa.Text(), nullable=True),
        "quality_metrics_json": sa.Column("quality_metrics_json", sa.Text(), nullable=True),
    })
    _add_columns(bind, "search_session_projects", {
        "candidate_id": sa.Column("candidate_id", sa.String(128), nullable=True),
        "relevance_class": sa.Column("relevance_class", sa.String(32), nullable=True),
        "verification_status": sa.Column("verification_status", sa.String(32), nullable=True),
        "enrichment_state": sa.Column("enrichment_state", sa.String(32), nullable=True),
        "blocker": sa.Column("blocker", sa.String(64), nullable=True),
        "next_action": sa.Column("next_action", sa.Text(), nullable=True),
        "result_bucket": sa.Column("result_bucket", sa.String(8), nullable=True),
        "match_type": sa.Column("match_type", sa.String(32), nullable=True),
    })
    _add_columns(bind, "candidate_enrichment_queries", {
        "content_hash": sa.Column("content_hash", sa.String(128), nullable=True),
    })
    _add_columns(bind, "candidates", {
        "content_hash": sa.Column("content_hash", sa.String(128), nullable=True),
        "identity_confidence": sa.Column("identity_confidence", sa.Float(), nullable=True),
    })

    tables = _tables(bind)
    if "candidates" not in tables:
        op.create_table(
            "candidates",
            sa.Column("candidate_id", sa.String(128), primary_key=True),
            sa.Column("candidate_key", sa.String(256), nullable=False, unique=True),
            sa.Column("project_id", sa.String(128), sa.ForeignKey("projects.project_id"), nullable=True),
            sa.Column("announcement_id", sa.Integer(), sa.ForeignKey("announcements.id"), nullable=True),
            sa.Column("search_session_id", sa.String(64), sa.ForeignKey("search_sessions.session_id"), nullable=True),
            sa.Column("source_id", sa.String(128), sa.ForeignKey("sources.source_id"), nullable=True),
            sa.Column("title", sa.String(500), nullable=False),
            sa.Column("raw_title", sa.Text(), nullable=True),
            sa.Column("source_url", sa.Text(), nullable=True),
            sa.Column("original_url", sa.Text(), nullable=True),
            sa.Column("canonical_url", sa.Text(), nullable=True),
            sa.Column("snippet", sa.Text(), nullable=True),
            sa.Column("source_domain", sa.String(256), nullable=True),
            sa.Column("source_level", sa.String(8), nullable=True),
            sa.Column("content_hash", sa.String(128), nullable=True),
            sa.Column("relevance_class", sa.String(32), nullable=False, server_default="POSSIBLE"),
            sa.Column("verification_status", sa.String(32), nullable=False, server_default="DISCOVERY_LEAD"),
            sa.Column("tender_status", sa.String(16), nullable=False, server_default="UNKNOWN"),
            sa.Column("enrichment_state", sa.String(32), nullable=False, server_default="NEW"),
            sa.Column("identity_status", sa.String(32), nullable=False, server_default="UNRESOLVED"),
            sa.Column("identity_confidence", sa.Float(), nullable=False, server_default="0"),
            sa.Column("blocker", sa.String(64), nullable=True),
            sa.Column("next_action", sa.Text(), nullable=True),
            sa.Column("candidate_class", sa.String(64), nullable=True),
            sa.Column("relation_types_json", sa.Text(), nullable=True),
            sa.Column("matched_concepts_json", sa.Text(), nullable=True),
            sa.Column("missing_fields_json", sa.Text(), nullable=True),
            sa.Column("candidate_values_json", sa.Text(), nullable=True),
            sa.Column("evidence_ids_json", sa.Text(), nullable=True),
            sa.Column("source_ids_json", sa.Text(), nullable=True),
            sa.Column("official_found", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("rank_score", sa.Float(), nullable=False, server_default="0"),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_enriched_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_change_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("persisted_reason", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    tables = _tables(bind)
    if "candidate_sources" not in tables:
        op.create_table(
            "candidate_sources",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("candidate_id", sa.String(128), sa.ForeignKey("candidates.candidate_id"), nullable=False),
            sa.Column("source_id", sa.String(128), sa.ForeignKey("sources.source_id"), nullable=True),
            sa.Column("source_url", sa.Text(), nullable=False),
            sa.Column("original_url", sa.Text(), nullable=True),
            sa.Column("canonical_url", sa.Text(), nullable=False),
            sa.Column("source_domain", sa.String(256), nullable=True),
            sa.Column("source_name", sa.String(256), nullable=True),
            sa.Column("source_level", sa.String(8), nullable=True),
            sa.Column("source_type", sa.String(64), nullable=True),
            sa.Column("provider", sa.String(64), nullable=True),
            sa.Column("source_title", sa.String(500), nullable=True),
            sa.Column("snippet", sa.Text(), nullable=True),
            sa.Column("source_location", sa.String(500), nullable=True),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("content_hash", sa.String(128), nullable=True),
            sa.Column("is_official", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("is_secondary", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("access_status", sa.String(32), nullable=False, server_default="DISCOVERED"),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("candidate_id", "canonical_url", name="uq_candidate_source_url"),
        )
    tables = _tables(bind)
    if "candidate_enrichment_queries" not in tables:
        op.create_table(
            "candidate_enrichment_queries",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("candidate_id", sa.String(128), sa.ForeignKey("candidates.candidate_id"), nullable=False),
            sa.Column("search_session_id", sa.String(64), sa.ForeignKey("search_sessions.session_id"), nullable=True),
            sa.Column("parent_query_id", sa.Integer(), sa.ForeignKey("candidate_enrichment_queries.id"), nullable=True),
            sa.Column("query_text", sa.String(1000), nullable=False),
            sa.Column("strategy", sa.String(64), nullable=False),
            sa.Column("round_no", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("provider", sa.String(64), nullable=True),
            sa.Column("source_id", sa.String(128), sa.ForeignKey("sources.source_id"), nullable=True),
            sa.Column("status", sa.String(32), nullable=False, server_default="PENDING"),
            sa.Column("results_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("candidate_hits", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("new_fact_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("new_source_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("query_hash", sa.String(128), nullable=True),
            sa.Column("content_hash", sa.String(128), nullable=True),
            sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("error", sa.Text(), nullable=True),
        )
    tables = _tables(bind)
    if "candidate_enrichment_results" not in tables:
        op.create_table(
            "candidate_enrichment_results",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("query_id", sa.Integer(), sa.ForeignKey("candidate_enrichment_queries.id"), nullable=False),
            sa.Column("candidate_id", sa.String(128), sa.ForeignKey("candidates.candidate_id"), nullable=False),
            sa.Column("discovered_candidate_id", sa.String(128), sa.ForeignKey("candidates.candidate_id"), nullable=True),
            sa.Column("search_session_id", sa.String(64), sa.ForeignKey("search_sessions.session_id"), nullable=True),
            sa.Column("title", sa.String(500), nullable=False),
            sa.Column("source_url", sa.Text(), nullable=False),
            sa.Column("canonical_url", sa.Text(), nullable=False),
            sa.Column("snippet", sa.Text(), nullable=True),
            sa.Column("provider", sa.String(64), nullable=True),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("source_level", sa.String(8), nullable=True),
            sa.Column("content_hash", sa.String(128), nullable=True),
            sa.Column("identity_status", sa.String(32), nullable=True),
            sa.Column("relevance_class", sa.String(32), nullable=True),
            sa.Column("is_official", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("is_secondary", sa.Boolean(), nullable=False, server_default="0"),
            sa.Column("match_type", sa.String(32), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("query_id", "canonical_url", name="uq_candidate_enrichment_result"),
        )
    tables = _tables(bind)
    if "candidate_facts" not in tables:
        op.create_table(
            "candidate_facts",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("candidate_id", sa.String(128), sa.ForeignKey("candidates.candidate_id"), nullable=False),
            sa.Column("field_name", sa.String(128), nullable=False),
            sa.Column("value", sa.Text(), nullable=True),
            sa.Column("normalized_value", sa.Text(), nullable=True),
            sa.Column("raw_value", sa.Text(), nullable=True),
            sa.Column("evidence_id", sa.Integer(), sa.ForeignKey("evidence.id"), nullable=True),
            sa.Column("source_url", sa.Text(), nullable=True),
            sa.Column("source_level", sa.String(8), nullable=True),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
            sa.Column("is_current", sa.Boolean(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "source_pivots" not in tables:
        op.create_table(
            "source_pivots",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("candidate_id", sa.String(128), sa.ForeignKey("candidates.candidate_id"), nullable=False),
            sa.Column("entity_type", sa.String(32), nullable=False),
            sa.Column("entity_value", sa.String(500), nullable=False),
            sa.Column("source_id", sa.String(128), sa.ForeignKey("sources.source_id"), nullable=True),
            sa.Column("discovered_url", sa.Text(), nullable=True),
            sa.Column("domain", sa.String(256), nullable=True),
            sa.Column("strategy", sa.String(64), nullable=True),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(32), nullable=False, server_default="DISCOVERED"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )

    # The runtime initializer also creates these indexes for installations that
    # have not yet run Alembic.  Guarding them keeps migration idempotent.
    for name, table, columns in (
        ("ix_candidates_candidate_key", "candidates", ["candidate_key"]),
        ("ix_candidates_canonical_url", "candidates", ["canonical_url"]),
        ("ix_candidates_content_hash", "candidates", ["content_hash"]),
        ("ix_candidates_tender_status", "candidates", ["tender_status"]),
        ("ix_candidates_enrichment_state", "candidates", ["enrichment_state"]),
        ("ix_candidate_sources_source_id", "candidate_sources", ["source_id"]),
        ("ix_candidate_enrichment_queries_candidate", "candidate_enrichment_queries", ["candidate_id"]),
        ("ix_candidate_enrichment_queries_content_hash", "candidate_enrichment_queries", ["content_hash"]),
        ("ix_candidate_enrichment_results_query", "candidate_enrichment_results", ["query_id"]),
        ("ix_candidate_enrichment_results_candidate", "candidate_enrichment_results", ["candidate_id"]),
        ("ix_candidate_enrichment_results_canonical_url", "candidate_enrichment_results", ["canonical_url"]),
        ("ix_candidate_facts_field", "candidate_facts", ["field_name"]),
        ("ix_source_pivots_entity", "source_pivots", ["entity_type", "entity_value"]),
    ):
        _create_index_if_missing(bind, name, table, columns)


def downgrade() -> None:
    # This migration is intentionally conservative.  Removing recall tables
    # could discard user-reviewed candidate history, so downgrade only removes
    # objects when explicitly requested by a future operator.
    bind = op.get_bind()
    tables = _tables(bind)
    for table in ("source_pivots", "candidate_facts", "candidate_enrichment_results", "candidate_enrichment_queries", "candidate_sources", "candidates"):
        if table in tables:
            op.drop_table(table)
