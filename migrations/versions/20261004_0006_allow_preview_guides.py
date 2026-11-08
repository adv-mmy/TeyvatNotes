"""体验服与正式服使用同一发布规则，保留来源标签及历史资料。"""

from alembic import op

revision = "20261004_0006"
down_revision = "20261004_0005"
branch_labels = None
depends_on = None

CONSTRAINT = "ck_guide_documents_published_metadata_valid"
PUBLISHED_METADATA = (
    "status != 'published' OR (game_version IS NOT NULL AND "
    "char_length(trim(game_version)) > 0 AND "
    "COALESCE(snapshot->>'is_fixture' = 'false', false) AND "
    "COALESCE(snapshot->>'complete' = 'true', false)"
)


def upgrade() -> None:
    """仅替换 CHECK，不更改文档状态、快照、检查点或其他表。"""
    op.drop_constraint(op.f(CONSTRAINT), "guide_documents", type_="check")
    op.create_check_constraint(op.f(CONSTRAINT), "guide_documents", PUBLISHED_METADATA + ")")


def downgrade() -> None:
    """已有 published preview 时旧 CHECK 拒绝回滚，事务保留全部数据。"""
    op.drop_constraint(op.f(CONSTRAINT), "guide_documents", type_="check")
    op.create_check_constraint(
        op.f(CONSTRAINT),
        "guide_documents",
        PUBLISHED_METADATA + " AND COALESCE(snapshot->>'content_nature' = 'released', false))",
    )
