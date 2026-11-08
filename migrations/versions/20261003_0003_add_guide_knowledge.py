"""增加离线攻略来源、版本、切块与导入检查点，不改变任务表。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261003_0003"
down_revision = "20260825_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "guide_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("native_id", sa.String(64), nullable=False),
        sa.Column("part_id", sa.Integer(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.UniqueConstraint("platform", "native_id", "part_id", name="uq_guide_sources_identity"),
        sa.CheckConstraint(
            "platform IN ('bilibili', 'miyoushe')", name=op.f("ck_guide_sources_platform_allowed")
        ),
        sa.CheckConstraint("part_id >= 1", name=op.f("ck_guide_sources_part_positive")),
    )
    op.create_table(
        "guide_documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("guide_sources.id"),
            nullable=False,
        ),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("processing_version", sa.String(64), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("author", sa.Text(), nullable=False),
        sa.Column("guide_kind", sa.String(32), nullable=False),
        sa.Column("game_version", sa.Text(), nullable=True),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "source_id", "content_hash", "processing_version", name="uq_guide_documents_version"
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'published')", name=op.f("ck_guide_documents_status_allowed")
        ),
        sa.CheckConstraint(
            "guide_kind IN ('character', 'quest')", name=op.f("ck_guide_documents_kind_allowed")
        ),
        sa.CheckConstraint(
            "char_length(content_hash) = 64", name=op.f("ck_guide_documents_hash_length")
        ),
        sa.CheckConstraint(
            "status != 'published' OR (game_version IS NOT NULL AND "
            "char_length(trim(game_version)) > 0 AND "
            "COALESCE(snapshot->>'is_fixture' = 'false', false) AND "
            "COALESCE(snapshot->>'complete' = 'true', false) AND "
            "COALESCE(snapshot->>'content_nature' = 'released', false))",
            name=op.f("ck_guide_documents_published_metadata_valid"),
        ),
    )
    op.create_index(
        "ix_guide_documents_status_kind_version",
        "guide_documents",
        ["status", "guide_kind", "game_version"],
    )
    op.create_table(
        "guide_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("guide_documents.id"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("section", sa.Text(), nullable=False),
        sa.Column("locators", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("document_id", "ordinal", name="uq_guide_chunks_document_ordinal"),
        sa.CheckConstraint("ordinal >= 0", name=op.f("ck_guide_chunks_ordinal_non_negative")),
        sa.CheckConstraint("char_length(text) > 0", name=op.f("ck_guide_chunks_text_non_empty")),
    )
    op.create_table(
        "guide_ingestion_steps",
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("guide_documents.id"),
            primary_key=True,
        ),
        sa.Column("name", sa.String(32), primary_key=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("output", postgresql.JSONB(), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "name IN ('parse', 'chunk', 'validate')",
            name=op.f("ck_guide_ingestion_steps_name_allowed"),
        ),
        sa.CheckConstraint(
            "status IN ('completed', 'failed')",
            name=op.f("ck_guide_ingestion_steps_status_allowed"),
        ),
        sa.CheckConstraint(
            "char_length(input_hash) = 64", name=op.f("ck_guide_ingestion_steps_hash_length")
        ),
        sa.CheckConstraint(
            "(status = 'completed' AND error_code IS NULL) OR "
            "(status = 'failed' AND error_code IS NOT NULL)",
            name=op.f("ck_guide_ingestion_steps_error_matches_status"),
        ),
    )


def downgrade() -> None:
    """仅定义回滚；不在演示或本机验证中自动执行删除。"""

    op.drop_table("guide_ingestion_steps")
    op.drop_table("guide_chunks")
    op.drop_index("ix_guide_documents_status_kind_version", table_name="guide_documents")
    op.drop_table("guide_documents")
    op.drop_table("guide_sources")
