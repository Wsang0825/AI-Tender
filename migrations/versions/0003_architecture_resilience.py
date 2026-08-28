"""可配置 Profile、快照、回放、健康度和 SQLite 韧性字段。"""

from alembic import op
import sqlalchemy as sa

from tender_ai.storage.models import Base


revision = "0003_architecture_resilience"
down_revision = "0002_real_crawl_runtime"
branch_labels = None
depends_on = None


def _add_columns(bind, additions):
    inspector = sa.inspect(bind)
    for table, columns in additions.items():
        if table not in inspector.get_table_names():
            continue
        existing = {column["name"] for column in inspector.get_columns(table)}
        for name, column_type, kwargs in columns:
            if name not in existing:
                op.add_column(table, sa.Column(name, column_type, nullable=True, **kwargs))


def upgrade() -> None:
    bind = op.get_bind()
    # 新表直接由声明式模型创建；旧表的增量字段在下面显式添加。
    Base.metadata.create_all(bind)
    _add_columns(
        bind,
        {
            "projects": [
                ("original_url", sa.Text(), {}), ("canonical_url", sa.Text(), {}), ("content_hash", sa.String(128), {}),
                ("status_reason", sa.String(128), {}), ("status_evaluated_at", sa.DateTime(timezone=True), {}),
                ("lifecycle_state", sa.String(16), {"server_default": "NEW"}), ("last_change_at", sa.DateTime(timezone=True), {}),
                ("favorite", sa.Boolean(), {"server_default": "0"}), ("ignored", sa.Boolean(), {"server_default": "0"}), ("ignore_reason", sa.Text(), {}),
            ],
            "announcements": [
                ("original_url", sa.Text(), {}), ("canonical_url", sa.Text(), {}), ("clean_text", sa.Text(), {}), ("snapshot_id", sa.String(64), {}),
            ],
            "sources": [
                ("adapter_level", sa.String(32), {"server_default": "CUSTOM_HTTP"}), ("adapter_config", sa.Text(), {}),
                ("crawl_interval", sa.Integer(), {"server_default": "86400"}), ("rate_limit", sa.Float(), {"server_default": "0.2"}),
                ("lookback_days", sa.Integer(), {"server_default": "30"}), ("browser_profile_path", sa.Text(), {}),
                ("health_reason", sa.String(64), {}), ("consecutive_failures", sa.Integer(), {"server_default": "0"}),
                ("average_items", sa.Float(), {"server_default": "0"}), ("latest_items", sa.Integer(), {"server_default": "0"}), ("last_health_at", sa.DateTime(timezone=True), {}),
            ],
            "project_sources": [("original_url", sa.Text(), {}), ("canonical_url", sa.Text(), {}), ("content_hash", sa.String(128), {})],
            "crawl_runs": [
                ("run_id", sa.String(64), {}), ("profile_id", sa.String(128), {}), ("checkpoint", sa.Text(), {}),
                ("items_seen", sa.Integer(), {"server_default": "0"}), ("items_new", sa.Integer(), {"server_default": "0"}),
                ("items_updated", sa.Integer(), {"server_default": "0"}), ("items_failed", sa.Integer(), {"server_default": "0"}),
            ],
            "discovered_sources": [("original_url", sa.Text(), {}), ("canonical_url", sa.Text(), {}), ("last_seen_at", sa.DateTime(timezone=True), {})],
            "search_queries": [
                ("profile_id", sa.String(128), {}), ("last_success_at", sa.DateTime(timezone=True), {}),
                ("new_project_count", sa.Integer(), {"server_default": "0"}), ("run_count", sa.Integer(), {"server_default": "0"}),
                ("last_error", sa.Text(), {}), ("cooldown_until", sa.DateTime(timezone=True), {}),
            ],
        },
    )


def downgrade() -> None:
    # 生产库不自动删除业务数据；回滚由备份或人工审查完成。
    pass
