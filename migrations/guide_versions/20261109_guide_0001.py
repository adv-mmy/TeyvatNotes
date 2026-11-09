"""独立攻略库基线：攻略版本、匿名访问和日额度。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261109_guide_0001"
down_revision = None
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
            "COALESCE(snapshot->>'complete' = 'true', false))",
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

    op.create_table(
        "guide_access_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "token_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_guide_access_sessions_token_hash_valid"),
        ),
    )
    op.create_index("ix_guide_access_sessions_expires_at", "guide_access_sessions", ["expires_at"])
    op.create_table(
        "guide_daily_quotas",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("bucket", sa.String(16), primary_key=True),
        sa.Column("subject", sa.String(64), primary_key=True),
        sa.Column("used", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.CheckConstraint("used >= 0", name=op.f("ck_guide_daily_quotas_used_non_negative")),
    )


def downgrade() -> None:
    """不提供会丢失证据、授权或配额的降级；部署回滚保留两个独立数据库。"""
    raise RuntimeError("攻略库基线不支持破坏性降级")
