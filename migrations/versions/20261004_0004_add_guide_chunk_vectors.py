"""增加不可变攻略块向量；不更改/重建知识资料和任务表，不依赖数据库扩展。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261004_0004"
down_revision = "20261003_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "guide_chunk_vectors",
        sa.Column("profile_id", sa.String(64), primary_key=True),
        sa.Column(
            "chunk_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("guide_chunks.id"),
            primary_key=True,
        ),
        sa.Column("provider", sa.String(80), nullable=False),
        sa.Column("model", sa.String(80), nullable=False),
        sa.Column("revision", sa.String(80), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("demo_only", sa.Boolean(), nullable=False),
        sa.Column("semantic", sa.Boolean(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("values", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint(
            "profile_id ~ '^[0-9a-f]{64}$'", name=op.f("ck_guide_chunk_vectors_profile_id_valid")
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_guide_chunk_vectors_content_hash_valid"),
        ),
        sa.CheckConstraint(
            "dimensions BETWEEN 1 AND 4096", name=op.f("ck_guide_chunk_vectors_dimensions_bounded")
        ),
        sa.CheckConstraint(
            "char_length(trim(provider)) > 0 AND char_length(trim(model)) > 0 AND "
            "char_length(trim(revision)) > 0",
            name=op.f("ck_guide_chunk_vectors_profile_names_non_empty"),
        ),
        sa.CheckConstraint(
            "semantic OR demo_only", name=op.f("ck_guide_chunk_vectors_lexical_demo_only")
        ),
        sa.CheckConstraint(
            "CASE WHEN jsonb_typeof(\"values\") = 'array' THEN "
            'jsonb_array_length("values") = dimensions ELSE false END',
            name=op.f("ck_guide_chunk_vectors_vector_dimensions_match"),
        ),
        sa.CheckConstraint(
            'NOT jsonb_path_exists("values", \'$[*] ? (@.type() != "number")\')',
            name=op.f("ck_guide_chunk_vectors_vector_numbers_only"),
        ),
        sa.CheckConstraint(
            "jsonb_path_exists(\"values\", '$[*] ? (@ != 0)')",
            name=op.f("ck_guide_chunk_vectors_vector_non_zero"),
        ),
    )
    op.create_index("ix_guide_chunk_vectors_chunk_id", "guide_chunk_vectors", ["chunk_id"])


def downgrade() -> None:
    """仅定义显式回滚；开发/测试不得自动执行删除向量表。"""
    op.drop_index("ix_guide_chunk_vectors_chunk_id", table_name="guide_chunk_vectors")
    op.drop_table("guide_chunk_vectors")
