"""alerts: add annotations JSON column

Revision ID: 3a1f6c2d8e91
Revises: 26f9bb55689e
Create Date: 2026-05-26 14:30:00.000000

新增 alerts.annotations JSONB/JSON 列，用于持久化 Alertmanager 的 annotations
（如 description / runbook_url / summary）以及内部派生字段（如 __generator_url）。
卡片渲染（services.cards.render_firing）依赖这些字段来出"详细描述 / 处理手册 /
查看监控"等行。

默认值为 '{}'，老数据 backfill 不需要应用层介入；NOT NULL 是为了让 SQLAlchemy
端类型与 DB 一致（dict 而非 dict | None），减少调用方分支。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "3a1f6c2d8e91"
down_revision: Union[str, Sequence[str], None] = "26f9bb55689e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "alerts",
        sa.Column(
            "annotations",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("alerts", "annotations")
