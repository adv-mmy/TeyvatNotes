"""小规模 RAG 的有界候选目录与不可变向量存储，不修改知识导入/回读契约。"""

from copy import deepcopy
from dataclasses import replace
from uuid import UUID

from teyvatnotes.domain.guides import (
    DocumentStatus,
    GuideChunk,
    GuideDocument,
    GuideImportError,
)
from teyvatnotes.domain.knowledge import (
    EvidenceHit,
    EvidenceScope,
    document_in_scope,
    version_in_scope,
)
from teyvatnotes.domain.rag import (
    EmbeddingProfile,
    VectorEntry,
    content_digest,
    embedding_text,
    require_budget,
)
from teyvatnotes.repositories.knowledge_memory import InMemoryKnowledgeRepository


def require_vector_request(profile: EmbeddingProfile, chunk_ids: tuple[UUID, ...]) -> None:
    """空读取合法；最多 500 个不同的 UUID，不接受隐式转换或重复参数。"""
    if (
        not isinstance(profile, EmbeddingProfile)
        or not isinstance(chunk_ids, tuple)
        or len(chunk_ids) > 500
        or any(not isinstance(identifier, UUID) for identifier in chunk_ids)
        or len(set(chunk_ids)) != len(chunk_ids)
    ):
        raise GuideImportError("invalid_vector_request")
    try:
        replace(profile)
    except (GuideImportError, TypeError, ValueError, OverflowError):
        raise GuideImportError("invalid_vector_request") from None


def require_vector_batch(entries: tuple[VectorEntry, ...]) -> None:
    """批次整体先校验；同一索引身份/块重复出现是含糊输入，不猜测该保留哪项。"""
    if (
        not isinstance(entries, tuple)
        or len(entries) > 500
        or any(not isinstance(entry, VectorEntry) for entry in entries)
    ):
        raise GuideImportError("invalid_vector_batch")
    try:
        # 冻结对象也可能被 object.__setattr__ 绕过构造器，存储边界重新校验字段。
        for entry in entries:
            replace(entry, profile=replace(entry.profile))
    except (GuideImportError, TypeError, ValueError, OverflowError):
        raise GuideImportError("invalid_vector_batch") from None
    if len({(entry.profile.identity, entry.chunk_id) for entry in entries}) != len(entries):
        raise GuideImportError("invalid_vector_batch")


def require_indexable_entry(entry: VectorEntry, document: GuideDocument, chunk: GuideChunk) -> None:
    """两个实现共用索引写入规则；向量摘要必须来自确切章节及完整原文。"""
    if chunk.document_id != document.id or entry.chunk_id != chunk.id:
        raise GuideImportError("evidence_not_found")
    if document.snapshot.get("is_fixture") is not entry.profile.demo_only:
        raise GuideImportError("embedding_scope_mismatch")
    expected_status = DocumentStatus.DRAFT if entry.profile.demo_only else DocumentStatus.PUBLISHED
    if (
        document.status is not expected_status
        or document.snapshot.get("complete") is not True
        or not document.game_version
        or document.game_version.strip().lower() in {"unknown", "未知"}
    ):
        raise GuideImportError("index_document_not_publishable")
    if entry.content_hash != content_digest(embedding_text(chunk)):
        raise GuideImportError("vector_content_mismatch")


class InMemoryRagRepository:
    """与知识仓储共享锁取得一致快照；向量复制隔离且整批校验后才原子写入。"""

    def __init__(self, knowledge: InMemoryKnowledgeRepository) -> None:
        self._knowledge = knowledge
        self._vectors: dict[tuple[str, UUID], VectorEntry] = {}

    async def list_candidates(
        self, scope: EvidenceScope, *, max_chunks: int = 500
    ) -> tuple[EvidenceHit, ...]:
        """先选每来源最新可见版本，再过滤实体与条件，超预算不返回偏置的前缀。"""
        if not isinstance(scope, EvidenceScope):
            raise GuideImportError("invalid_knowledge_query")
        require_budget(max_chunks)
        # 受控复用既有存储锁/只读字段，避免为 RAG 改动旧导入仓储的公开协议。
        with self._knowledge._lock:
            latest: dict[UUID, GuideDocument] = {}
            for document in self._knowledge._documents.values():
                if not version_in_scope(document, scope):
                    continue
                previous = latest.get(document.source.id)
                if previous is None or (document.created_at, str(document.id)) > (
                    previous.created_at,
                    str(previous.id),
                ):
                    latest[document.source.id] = document
            candidates: list[EvidenceHit] = []
            for document in latest.values():
                if not document_in_scope(document, scope):
                    continue
                for chunk in self._knowledge._chunks.get(document.id, ()):
                    candidates.append(EvidenceHit(document, chunk))
                    if len(candidates) > max_chunks:
                        raise GuideImportError("rag_candidate_budget_exceeded")
            candidates.sort(key=lambda hit: (str(hit.document.id), hit.chunk.ordinal))
            return deepcopy(tuple(candidates))

    async def get_vectors(
        self, profile: EmbeddingProfile, chunk_ids: tuple[UUID, ...]
    ) -> tuple[VectorEntry, ...]:
        """只返回请求索引身份的已存条目，按请求顺序排列；缺失项不伪造向量。"""
        require_vector_request(profile, chunk_ids)
        with self._knowledge._lock:
            return deepcopy(
                tuple(
                    self._vectors[(profile.identity, identifier)]
                    for identifier in chunk_ids
                    if (profile.identity, identifier) in self._vectors
                )
            )

    async def save_vectors(self, entries: tuple[VectorEntry, ...]) -> None:
        """验证全部原文与冲突后一次写入；不能用重建索引覆盖旧原始向量。"""
        require_vector_batch(entries)
        if not entries:
            return
        with self._knowledge._lock:
            targets = {entry.chunk_id for entry in entries}
            chunks = {
                chunk.id: chunk
                for group in self._knowledge._chunks.values()
                for chunk in group
                if chunk.id in targets
            }
            for entry in entries:
                chunk = chunks.get(entry.chunk_id)
                if chunk is None:
                    raise GuideImportError("evidence_not_found")
                document = self._knowledge._documents.get(chunk.document_id)
                if document is None:
                    raise GuideImportError("evidence_not_found")
                require_indexable_entry(entry, document, chunk)
                previous = self._vectors.get((entry.profile.identity, entry.chunk_id))
                if previous is not None and previous != entry:
                    raise GuideImportError("vector_conflict")
            self._vectors.update(
                ((entry.profile.identity, entry.chunk_id), deepcopy(entry)) for entry in entries
            )
