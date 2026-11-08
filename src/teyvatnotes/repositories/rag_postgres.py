"""PostgreSQL 有界候选目录/JSONB 向量存储；不在数据库中伪造近似向量索引。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from teyvatnotes.database.knowledge import (
    GuideChunkRecord,
    GuideDocumentRecord,
    GuideSourceRecord,
)
from teyvatnotes.database.rag import GuideChunkVectorRecord
from teyvatnotes.domain.guides import GuideImportError
from teyvatnotes.domain.knowledge import EvidenceHit, EvidenceScope
from teyvatnotes.domain.rag import EmbeddingProfile, VectorEntry, require_budget
from teyvatnotes.repositories.knowledge_postgres import (
    _chunk,
    _document,
    _scope_filters,
    _version_filters,
)
from teyvatnotes.repositories.rag_memory import (
    require_indexable_entry,
    require_vector_batch,
    require_vector_request,
)


def _vector(record: GuideChunkVectorRecord) -> VectorEntry:
    """显式重建领域对象并核对指纹，不将 ORM/可变 JSON 数组暴露给应用。"""
    profile = EmbeddingProfile(
        record.provider,
        record.model,
        record.revision,
        record.dimensions,
        record.demo_only,
        record.semantic,
    )
    if profile.identity != record.profile_id:
        raise GuideImportError("vector_store_corrupt")
    return VectorEntry(record.chunk_id, profile, record.content_hash, tuple(record.values))


class PostgresRagRepository:
    """候选过滤在 SQL 完成，向量写入以块/文档行锁和复合唯一身份串行化。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[AsyncSession]:
        """存储故障只暴露稳定代码；事务会先回滚/关闭，合作取消不被转换。"""
        try:
            async with self._sessions() as session:
                yield session
        except GuideImportError:
            raise
        except Exception:
            raise GuideImportError("rag_storage_unavailable") from None

    async def list_candidates(
        self, scope: EvidenceScope, *, max_chunks: int = 500
    ) -> tuple[EvidenceHit, ...]:
        """只加载 max_chunks+1 项以检测超预算；不先截断再宣称完成全部候选。"""
        if not isinstance(scope, EvidenceScope):
            raise GuideImportError("invalid_knowledge_query")
        require_budget(max_chunks)
        # 受控复用旧 SQL 过滤器，让发布/fixture/最新版本规则不产生第二套政策。
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
            .where(*_version_filters(scope))
            .subquery()
        )
        statement = (
            select(GuideChunkRecord, GuideDocumentRecord, GuideSourceRecord)
            .join(GuideDocumentRecord, GuideChunkRecord.document_id == GuideDocumentRecord.id)
            .join(GuideSourceRecord, GuideDocumentRecord.source_id == GuideSourceRecord.id)
            .join(latest, latest.c.id == GuideDocumentRecord.id)
            .where(latest.c.version_rank == 1, *_scope_filters(scope))
            .order_by(GuideDocumentRecord.id, GuideChunkRecord.ordinal)
            .limit(max_chunks + 1)
        )
        async with self._session() as session:
            rows = (await session.execute(statement)).all()
            if len(rows) > max_chunks:
                raise GuideImportError("rag_candidate_budget_exceeded")
            return tuple(
                EvidenceHit(_document(document, source), _chunk(chunk))
                for chunk, document, source in rows
            )

    async def get_vectors(
        self, profile: EmbeddingProfile, chunk_ids: tuple[UUID, ...]
    ) -> tuple[VectorEntry, ...]:
        """按索引身份及已请求 ID 精确读取；空请求不访问数据库。"""
        require_vector_request(profile, chunk_ids)
        if not chunk_ids:
            return ()
        async with self._session() as session:
            records = await session.scalars(
                select(GuideChunkVectorRecord).where(
                    GuideChunkVectorRecord.profile_id == profile.identity,
                    GuideChunkVectorRecord.chunk_id.in_(chunk_ids),
                )
            )
            entries = {record.chunk_id: _vector(record) for record in records}
            return tuple(entries[identifier] for identifier in chunk_ids if identifier in entries)

    async def save_vectors(self, entries: tuple[VectorEntry, ...]) -> None:
        """整批事务：校验原文及范围，幂等插入，再核对任何并发冲突；从不覆盖旧值。"""
        require_vector_batch(entries)
        if not entries:
            return
        identifiers = tuple(dict.fromkeys(entry.chunk_id for entry in entries))
        async with self._session() as session, session.begin():
            rows = await session.execute(
                select(GuideChunkRecord, GuideDocumentRecord, GuideSourceRecord)
                .join(GuideDocumentRecord, GuideChunkRecord.document_id == GuideDocumentRecord.id)
                .join(GuideSourceRecord, GuideDocumentRecord.source_id == GuideSourceRecord.id)
                .where(GuideChunkRecord.id.in_(identifiers))
                .order_by(GuideDocumentRecord.id, GuideChunkRecord.id)
                .with_for_update(of=(GuideDocumentRecord, GuideChunkRecord))
            )
            chunks = {
                chunk.id: (_chunk(chunk), _document(document, source))
                for chunk, document, source in rows
            }
            for entry in entries:
                found = chunks.get(entry.chunk_id)
                if found is None:
                    raise GuideImportError("evidence_not_found")
                chunk, document = found
                require_indexable_entry(entry, document, chunk)
            keys = tuple((entry.profile.identity, entry.chunk_id) for entry in entries)
            old_records = await session.scalars(
                select(GuideChunkVectorRecord).where(
                    tuple_(GuideChunkVectorRecord.profile_id, GuideChunkVectorRecord.chunk_id).in_(
                        keys
                    )
                )
            )
            old = {(record.profile_id, record.chunk_id): _vector(record) for record in old_records}
            for entry in entries:
                previous = old.get((entry.profile.identity, entry.chunk_id))
                if previous is not None and previous != entry:
                    raise GuideImportError("vector_conflict")
            await session.execute(
                insert(GuideChunkVectorRecord)
                .values(
                    [
                        {
                            "profile_id": entry.profile.identity,
                            "chunk_id": entry.chunk_id,
                            "provider": entry.profile.provider,
                            "model": entry.profile.model,
                            "revision": entry.profile.revision,
                            "dimensions": entry.profile.dimensions,
                            "demo_only": entry.profile.demo_only,
                            "semantic": entry.profile.semantic,
                            "content_hash": entry.content_hash,
                            "values": list(entry.values),
                        }
                        for entry in entries
                    ]
                )
                .on_conflict_do_nothing(index_elements=["profile_id", "chunk_id"])
            )
            # 即使有绕过本仓储行锁的写入者，唯一约束也不能造成无声的值覆盖/冲突。
            saved_records = await session.scalars(
                select(GuideChunkVectorRecord).where(
                    tuple_(GuideChunkVectorRecord.profile_id, GuideChunkVectorRecord.chunk_id).in_(
                        keys
                    )
                )
            )
            saved = {
                (record.profile_id, record.chunk_id): _vector(record) for record in saved_records
            }
            if any(
                saved.get((entry.profile.identity, entry.chunk_id)) != entry for entry in entries
            ):
                raise GuideImportError("vector_conflict")
