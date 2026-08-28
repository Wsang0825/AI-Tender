"""真实采集运行指标、发现来源和搜索统计。"""

from alembic import op
import sqlalchemy as sa


revision = "0002_real_crawl_runtime"
down_revision = "0001_initial_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    additions = {
        "sources": [
            ("last_success_at", sa.DateTime(timezone=True)),
            ("last_failure_at", sa.DateTime(timezone=True)),
            ("failure_count", sa.Integer(), {"server_default": "0"}),
            ("items_found", sa.Integer(), {"server_default": "0"}),
            ("last_http_status", sa.Integer()),
            ("runtime_status", sa.String(32), {"server_default": "ACTIVE"}),
            ("last_error", sa.Text()),
            ("crawl_enabled", sa.Boolean(), {"server_default": "0"}),
            ("max_pages", sa.Integer(), {"server_default": "2"}),
            ("request_delay_seconds", sa.Float(), {"server_default": "0.2"}),
        ],
        "crawl_runs": [
            ("new_item_count", sa.Integer(), {"server_default": "0"}),
            ("attachment_count", sa.Integer(), {"server_default": "0"}),
            ("new_domain_count", sa.Integer(), {"server_default": "0"}),
            ("wechat_candidate_count", sa.Integer(), {"server_default": "0"}),
        ],
        "discovered_sources": [
            ("domain", sa.String(256)),
            ("projects_found", sa.Integer(), {"server_default": "0"}),
            ("source_level_guess", sa.String(8)),
            ("confidence", sa.Float(), {"server_default": "0"}),
        ],
        "search_queries": [
            ("results_count", sa.Integer(), {"server_default": "0"}),
            ("new_results_count", sa.Integer(), {"server_default": "0"}),
            ("priority", sa.Integer(), {"server_default": "5"}),
        ],
    }
    for table, columns in additions.items():
        existing = {column["name"] for column in inspector.get_columns(table)}
        for entry in columns:
            name, column_type, *kwargs = entry
            if name not in existing:
                op.add_column(table, sa.Column(name, column_type, nullable=True, **(kwargs[0] if kwargs else {})))


def downgrade() -> None:
    for table, names in {
        "search_queries": ["priority", "new_results_count", "results_count"],
        "discovered_sources": ["confidence", "source_level_guess", "projects_found", "domain"],
        "crawl_runs": ["wechat_candidate_count", "new_domain_count", "attachment_count", "new_item_count"],
        "sources": ["request_delay_seconds", "max_pages", "crawl_enabled", "last_error", "runtime_status", "last_http_status", "items_found", "failure_count", "last_failure_at", "last_success_at"],
    }.items():
        for name in names:
            op.drop_column(table, name)
