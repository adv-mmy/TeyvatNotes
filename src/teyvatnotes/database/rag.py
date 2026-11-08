"""不可变块向量的 PostgreSQL 表；JSONB 小规模存储，不依赖 pgvector 扩展。"""

from uuid import UUID

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from teyvatnotes.database.models import Base


class GuideChunkVectorRecord(Base):
    """索引身份包含服务商/模型/算法/维度及演示标记，旧值不覆盖。"""

    __tablename__ = "guide_chunk_vectors"
    __table_args__ = (
        CheckConstraint("profile_id ~ '^[0-9a-f]{64}$'", name="profile_id_valid"),
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="content_hash_valid"),
        CheckConstraint("dimensions BETWEEN 1 AND 4096", name="dimensions_bounded"),
        CheckConstraint(
            "char_length(trim(provider)) > 0 AND char_length(trim(model)) > 0 AND "
            "char_length(trim(revision)) > 0",
            name="profile_names_non_empty",
        ),
        CheckConstraint("semantic OR demo_only", name="lexical_demo_only"),
        CheckConstraint(
            "CASE WHEN jsonb_typeof(\"values\") = 'array' THEN "
            'jsonb_array_length("values") = dimensions ELSE false END',
            name="vector_dimensions_match",
        ),
        CheckConstraint(
            'NOT jsonb_path_exists("values", \'$[*] ? (@.type() != "number")\')',
            name="vector_numbers_only",
        ),
        CheckConstraint("jsonb_path_exists(\"values\", '$[*] ? (@ != 0)')", name="vector_non_zero"),
        Index("ix_guide_chunk_vectors_chunk_id", "chunk_id"),
    )

    profile_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    chunk_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), ForeignKey("guide_chunks.id"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    model: Mapped[str] = mapped_column(String(80), nullable=False)
    revision: Mapped[str] = mapped_column(String(80), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    demo_only: Mapped[bool] = mapped_column(Boolean, nullable=False)
    semantic: Mapped[bool] = mapped_column(Boolean, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    values: Mapped[list[float]] = mapped_column(JSONB, nullable=False)
