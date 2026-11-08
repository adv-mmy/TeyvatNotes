"""增加私有攻略会话，不改写任务、资料、向量或旧历史。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261004_0005"
down_revision = "20261004_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "guide_conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("closed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint(
            "revision >= 0", name=op.f("ck_guide_conversations_revision_non_negative")
        ),
        sa.CheckConstraint(
            "updated_at >= created_at", name=op.f("ck_guide_conversations_timestamps_ordered")
        ),
        sa.CheckConstraint(
            "CASE WHEN jsonb_typeof(payload) = 'object' AND "
            "jsonb_typeof(payload->'turns') = 'array' THEN "
            "jsonb_array_length(payload->'turns') <= 20 ELSE false END",
            name=op.f("ck_guide_conversations_payload_structure"),
        ),
        sa.CheckConstraint(
            "octet_length(payload::text) <= 2000000",
            name=op.f("ck_guide_conversations_history_bounded"),
        ),
    )
    op.create_index("ix_guide_conversations_owner_id", "guide_conversations", ["owner_id"])


def downgrade() -> None:
    """仅声明显式回滚；没有用户批准时不得执行删除私有历史。"""
    op.drop_index("ix_guide_conversations_owner_id", table_name="guide_conversations")
    op.drop_table("guide_conversations")
