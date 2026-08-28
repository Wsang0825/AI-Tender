"""创建核心领域表。"""

from alembic import op

from tender_ai.storage.models import Base


revision = "0001_initial_core"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 初始迁移直接使用声明式模型，后续变更通过 Alembic 自动生成并人工审核。
    Base.metadata.create_all(op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(op.get_bind())
