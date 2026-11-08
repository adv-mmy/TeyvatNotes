"""PostgreSQL 知识仓储：版本幂等，切块与检查点原子提交，发布使用行锁。"""

from typing import cast
from uuid import UUID

from sqlalchemy import case, func, literal, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from teyvatnotes.database.knowledge import (
    GuideChunkRecord,
    GuideDocumentRecord,
    GuideSourceRecord,
    GuideStepRecord,
)
from teyvatnotes.domain.guides import (
    DocumentStatus,
    GuideChunk,
    GuideDocument,
    GuideImportError,
    GuideKind,
    GuideSource,
    IngestionCheckpoint,
    SourcePlatform,
    StepName,
    StepStatus,
    require_publishable,
    require_valid_checkpoint,
)
from teyvatnotes.domain.knowledge import (
    EvidenceHit,
    EvidenceScope,
    EvidenceWindow,
    KeywordQuery,
    context_range,
    document_in_scope,
    require_context_limit,
)
from teyvatnotes.domain.task import JsonValue


def _document(record: GuideDocumentRecord, source: GuideSourceRecord) -> GuideDocument:
    return GuideDocument(
        record.id,
        GuideSource(
            source.id,
            SourcePlatform(source.platform),
            source.native_id,
            source.part_id,
            source.canonical_url,
        ),
        record.content_hash,
        record.processing_version,
        record.title,
        record.author,
        GuideKind(record.guide_kind),
        record.game_version,
        cast(dict[str, JsonValue], record.snapshot),
        DocumentStatus(record.status),
        record.created_at,
    )


def _checkpoint(record: GuideStepRecord) -> IngestionCheckpoint:
    return IngestionCheckpoint(
        record.document_id,
        StepName(record.name),
        StepStatus(record.status),
        record.input_hash,
        cast(dict[str, JsonValue], record.output),
        record.error_code,
        record.updated_at,
    )


def _chunk(record: GuideChunkRecord) -> GuideChunk:
    """在只读 Session 生命周期内显式转换 ORM，调用方不会得到活 ORM 对象。"""
    return GuideChunk(
        record.id,
        record.document_id,
        record.ordinal,
        record.text,
        record.section,
        tuple(cast(dict[str, JsonValue], item) for item in record.locators),
    )


def _version_filters(scope: EvidenceScope) -> tuple[ColumnElement[bool], ...]:
    """SQL 与内存采用相同的可见版本政策；JSON 字面布尔通过 ->> 比较。"""
    status = DocumentStatus.DRAFT if scope.demo_only else DocumentStatus.PUBLISHED
    return (
        GuideDocumentRecord.status == status.value,
        GuideDocumentRecord.game_version == scope.game_version,
        GuideDocumentRecord.guide_kind == scope.guide_kind.value,
        GuideDocumentRecord.snapshot["is_fixture"].as_string()
        == ("true" if scope.demo_only else "false"),
        GuideDocumentRecord.snapshot["complete"].as_string() == "true",
    )


def _scope_filters(scope: EvidenceScope) -> tuple[ColumnElement[bool], ...]:
    """资料前提是玩家条件的子集；额外玩家条件不会排除通用资料。"""
    return (
        *_version_filters(scope),
        GuideDocumentRecord.snapshot["entities"].contains([scope.entity]),
        GuideDocumentRecord.snapshot["conditions"].contained_by(dict(scope.conditions)),
    )


class PostgresKnowledgeRepository:
    """每操作一个 Session，同版本写入用文档行锁串行化。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def _find(
        self, session: AsyncSession, document_id: UUID, *, lock: bool = False
    ) -> tuple[GuideDocumentRecord, GuideSourceRecord]:
        statement = select(GuideDocumentRecord).where(GuideDocumentRecord.id == document_id)
        if lock:
            statement = statement.with_for_update()
        record = await session.scalar(statement)
        if record is None:
            raise GuideImportError("document_not_found")
        source = await session.get(GuideSourceRecord, record.source_id)
        if source is None:
            raise GuideImportError("source_not_found")
        return record, source

    async def register(self, document: GuideDocument) -> GuideDocument:
        async with self._sessions() as session, session.begin():
            source = document.source
            await session.execute(
                insert(GuideSourceRecord)
                .values(
                    id=source.id,
                    platform=source.platform.value,
                    native_id=source.native_id,
                    part_id=source.part_id,
                    canonical_url=source.canonical_url,
                )
                .on_conflict_do_nothing(index_elements=["id"])
            )
            await session.execute(
                insert(GuideDocumentRecord)
                .values(
                    id=document.id,
                    source_id=source.id,
                    content_hash=document.content_hash,
                    processing_version=document.processing_version,
                    title=document.title,
                    author=document.author,
                    guide_kind=document.guide_kind.value,
                    game_version=document.game_version,
                    snapshot=document.snapshot,
                    status=DocumentStatus.DRAFT.value,
                    created_at=document.created_at,
                )
                .on_conflict_do_nothing(index_elements=["id"])
            )
            record, saved_source = await self._find(session, document.id)
            return _document(record, saved_source)

    async def get_document(self, document_id: UUID) -> GuideDocument:
        async with self._sessions() as session:
            record, source = await self._find(session, document_id)
            return _document(record, source)

    async def get_checkpoint(self, document_id: UUID, name: StepName) -> IngestionCheckpoint | None:
        async with self._sessions() as session:
            await self._find(session, document_id)
            record = await session.get(GuideStepRecord, (document_id, name.value))
            return None if record is None else _checkpoint(record)

    async def commit_checkpoint(
        self, checkpoint: IngestionCheckpoint, *, chunks: tuple[GuideChunk, ...] = ()
    ) -> IngestionCheckpoint:
        require_valid_checkpoint(checkpoint, chunks)
        async with self._sessions() as session, session.begin():
            await self._find(session, checkpoint.document_id, lock=True)
            previous = await session.get(
                GuideStepRecord, (checkpoint.document_id, checkpoint.name.value)
            )
            if previous is not None and previous.status == StepStatus.COMPLETED.value:
                return _checkpoint(previous)
            if chunks:
                for chunk in chunks:
                    session.add(
                        GuideChunkRecord(
                            id=chunk.id,
                            document_id=chunk.document_id,
                            ordinal=chunk.ordinal,
                            text=chunk.text,
                            section=chunk.section,
                            locators=list(chunk.locators),
                        )
                    )
            if previous is None:
                previous = GuideStepRecord(
                    document_id=checkpoint.document_id, name=checkpoint.name.value
                )
                session.add(previous)
            previous.status = checkpoint.status.value
            previous.input_hash = checkpoint.input_hash
            previous.output = checkpoint.output
            previous.error_code = checkpoint.error_code
            previous.updated_at = checkpoint.updated_at
            await session.flush()
            return _checkpoint(previous)

    async def list_chunks(self, document_id: UUID) -> tuple[GuideChunk, ...]:
        async with self._sessions() as session:
            await self._find(session, document_id)
            records = await session.scalars(
                select(GuideChunkRecord)
                .where(GuideChunkRecord.document_id == document_id)
                .order_by(GuideChunkRecord.ordinal)
            )
            return tuple(
                GuideChunk(
                    record.id,
                    record.document_id,
                    record.ordinal,
                    record.text,
                    record.section,
                    tuple(cast(dict[str, JsonValue], item) for item in record.locators),
                )
                for record in records
            )

    async def publish(self, document_id: UUID) -> GuideDocument:
        async with self._sessions() as session, session.begin():
            record, source = await self._find(session, document_id, lock=True)
            steps = await session.scalars(
                select(GuideStepRecord).where(GuideStepRecord.document_id == document_id)
            )
            count = await session.scalar(
                select(func.count())
                .select_from(GuideChunkRecord)
                .where(GuideChunkRecord.document_id == document_id)
            )
            require_publishable(
                _document(record, source),
                tuple(_checkpoint(step) for step in steps),
                int(count or 0),
            )
            record.status = DocumentStatus.PUBLISHED.value
            await session.flush()
            return _document(record, source)

    async def search_knowledge(self, query: KeywordQuery) -> tuple[EvidenceHit, ...]:
        """参数化 strpos 字面检索；数据库计算排名并限制结果，不把全库加载入 Python。"""
        latest = (
            select(
                GuideDocumentRecord.id,
                func.row_number()
                .over(
                    partition_by=GuideDocumentRecord.source_id,
                    order_by=(GuideDocumentRecord.created_at.desc(), GuideDocumentRecord.id.desc()),
                )
                .label("version_rank"),
            )
            .where(*_version_filters(query.scope))
            .subquery()
        )
        score: ColumnElement[int] = literal(0)
        for term in query.keywords:
            score = score + case(
                (func.strpos(func.lower(GuideChunkRecord.text), term) > 0, 4), else_=0
            )
            score = score + case(
                (func.strpos(func.lower(GuideChunkRecord.section), term) > 0, 2), else_=0
            )
        statement = (
            select(GuideChunkRecord, GuideDocumentRecord, GuideSourceRecord, score.label("score"))
            .join(GuideDocumentRecord, GuideChunkRecord.document_id == GuideDocumentRecord.id)
            .join(GuideSourceRecord, GuideDocumentRecord.source_id == GuideSourceRecord.id)
            .join(latest, latest.c.id == GuideDocumentRecord.id)
            .where(latest.c.version_rank == 1, *_scope_filters(query.scope), score > 0)
            .order_by(score.desc(), GuideDocumentRecord.id, GuideChunkRecord.ordinal)
            .limit(query.limit)
        )
        async with self._sessions() as session:
            rows = await session.execute(statement)
            return tuple(
                EvidenceHit(_document(doc, source), _chunk(chunk), points)
                for chunk, doc, source, points in rows
            )

    async def read_evidence(
        self, chunk_id: UUID, scope: EvidenceScope, *, max_chunks: int = 20
    ) -> EvidenceWindow:
        """先检查证据可见性，再取有界上下文；不存在和范围不符返回相同公开错误。"""
        require_context_limit(max_chunks)
        async with self._sessions() as session, session.begin():
            row = (
                await session.execute(
                    select(GuideChunkRecord, GuideDocumentRecord, GuideSourceRecord)
                    .join(
                        GuideDocumentRecord, GuideChunkRecord.document_id == GuideDocumentRecord.id
                    )
                    .join(GuideSourceRecord, GuideDocumentRecord.source_id == GuideSourceRecord.id)
                    .where(GuideChunkRecord.id == chunk_id, *_scope_filters(scope))
                )
            ).one_or_none()
            if row is None:
                raise GuideImportError("evidence_not_found")
            chunk, record, source = row
            document = _document(record, source)
            if not document_in_scope(document, scope):
                raise GuideImportError("evidence_not_found")
            hit = EvidenceHit(document, _chunk(chunk))
            start, stop = context_range(hit)
            records = await session.scalars(
                select(GuideChunkRecord)
                .where(
                    GuideChunkRecord.document_id == document.id,
                    GuideChunkRecord.ordinal >= start,
                    GuideChunkRecord.ordinal <= stop,
                )
                .order_by(GuideChunkRecord.ordinal)
                .limit(max_chunks + 1)
            )
            context = tuple(_chunk(item) for item in records)
            return EvidenceWindow(hit, context[:max_chunks], len(context) > max_chunks)
