"""攻略知识表；来源身份、不可变文档版本、证据块与检查点分开保存。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from teyvatnotes.database.models import Base


class GuideSourceRecord(Base):
    __tablename__ = "guide_sources"
    __table_args__ = (
        UniqueConstraint("platform", "native_id", "part_id", name="uq_guide_sources_identity"),
        CheckConstraint("platform IN ('bilibili', 'miyoushe')", name="platform_allowed"),
        CheckConstraint("part_id >= 1", name="part_positive"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    native_id: Mapped[str] = mapped_column(String(64), nullable=False)
    part_id: Mapped[int] = mapped_column(Integer, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)


class GuideDocumentRecord(Base):
    __tablename__ = "guide_documents"
    __table_args__ = (
        UniqueConstraint(
            "source_id", "content_hash", "processing_version", name="uq_guide_documents_version"
        ),
        CheckConstraint("status IN ('draft', 'published')", name="status_allowed"),
        CheckConstraint("guide_kind IN ('character', 'quest')", name="kind_allowed"),
        CheckConstraint("char_length(content_hash) = 64", name="hash_length"),
        CheckConstraint(
            "status != 'published' OR (game_version IS NOT NULL AND "
            "char_length(trim(game_version)) > 0 AND "
            "COALESCE(snapshot->>'is_fixture' = 'false', false) AND "
            "COALESCE(snapshot->>'complete' = 'true', false))",
            name="published_metadata_valid",
        ),
        Index("ix_guide_documents_status_kind_version", "status", "guide_kind", "game_version"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    source_id: Mapped[UUID] = mapped_column(ForeignKey("guide_sources.id"), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    processing_version: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str] = mapped_column(Text, nullable=False)
    guide_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    game_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GuideChunkRecord(Base):
    __tablename__ = "guide_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "ordinal", name="uq_guide_chunks_document_ordinal"),
        CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
        CheckConstraint("char_length(text) > 0", name="text_non_empty"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("guide_documents.id"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    section: Mapped[str] = mapped_column(Text, nullable=False)
    locators: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)


class GuideStepRecord(Base):
    __tablename__ = "guide_ingestion_steps"
    __table_args__ = (
        CheckConstraint("name IN ('parse', 'chunk', 'validate')", name="name_allowed"),
        CheckConstraint("status IN ('completed', 'failed')", name="status_allowed"),
        CheckConstraint("char_length(input_hash) = 64", name="hash_length"),
        CheckConstraint(
            "(status = 'completed' AND error_code IS NULL) OR "
            "(status = 'failed' AND error_code IS NOT NULL)",
            name="error_matches_status",
        ),
    )

    document_id: Mapped[UUID] = mapped_column(ForeignKey("guide_documents.id"), primary_key=True)
    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
