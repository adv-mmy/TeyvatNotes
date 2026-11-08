"""有界 RAG 索引、排序及显式回读；不改变旧问答入口或调用生成模型。"""

import asyncio
import math
from copy import deepcopy
from dataclasses import replace
from uuid import UUID

from teyvatnotes.application.knowledge_ports import KnowledgeRepository
from teyvatnotes.application.knowledge_read_ports import KnowledgeReader
from teyvatnotes.application.rag_ports import EmbeddingClient, RagRepository
from teyvatnotes.domain.guides import (
    DocumentStatus,
    GuideChunk,
    GuideImportError,
    StepName,
    StepStatus,
    require_valid_checkpoint,
)
from teyvatnotes.domain.knowledge import (
    EvidenceHit,
    EvidenceScope,
    EvidenceWindow,
    KeywordQuery,
    bounded_text,
    context_range,
    document_in_scope,
    keyword_score,
    require_context_limit,
)
from teyvatnotes.domain.rag import (
    EmbeddingProfile,
    IndexResult,
    RagQuery,
    RankedEvidence,
    RetrievalMode,
    RetrievalResult,
    VectorEntry,
    content_digest,
    cosine_similarity,
    embedding_text,
    require_budget,
    unit_vector,
)

MAX_INDEX_CHARACTERS = 128_000


def _timeout(value: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 < value <= 120
    ):
        raise GuideImportError("invalid_rag_budget")


def _profile(client: EmbeddingClient) -> EmbeddingProfile:
    profile = client.profile
    if not isinstance(profile, EmbeddingProfile):
        raise GuideImportError("invalid_embedding_profile")
    # 不依赖外部对象的构造器曾经执行：端口测试/实现也可能绕过 dataclass 校验。
    return replace(profile)


def _unchanged(client: EmbeddingClient, profile: EmbeddingProfile) -> None:
    if _profile(client) != profile:
        raise GuideImportError("embedding_profile_changed")


def _vector_map(
    entries: tuple[VectorEntry, ...],
    profile: EmbeddingProfile,
    chunks: tuple[GuideChunk, ...],
    *,
    complete: bool,
) -> dict[UUID, VectorEntry]:
    """索引内容必须与现在的章节+原文相同，不能将旧哈希或别的模型作为缓存。"""
    code = "rag_index_incomplete" if complete else "rag_index_conflict"
    expected = {chunk.id: content_digest(embedding_text(chunk)) for chunk in chunks}
    if not isinstance(entries, tuple):
        raise GuideImportError(code)
    indexed: dict[UUID, VectorEntry] = {}
    for entry in entries:
        if (
            not isinstance(entry, VectorEntry)
            or not isinstance(entry.chunk_id, UUID)
            or entry.chunk_id in indexed
            or entry.chunk_id not in expected
            or entry.profile != profile
            or entry.content_hash != expected[entry.chunk_id]
        ):
            raise GuideImportError(code)
        try:
            unit_vector(entry.values, profile.dimensions)
        except GuideImportError:
            raise GuideImportError(code) from None
        indexed[entry.chunk_id] = entry
    if complete and set(indexed) != set(expected):
        raise GuideImportError(code)
    return indexed


def _embedding_values(
    values: tuple[tuple[float, ...], ...], count: int, profile: EmbeddingProfile
) -> tuple[tuple[float, ...], ...]:
    if not isinstance(values, tuple) or len(values) != count:
        raise GuideImportError("invalid_embedding_response")
    try:
        for vector in values:
            unit_vector(vector, profile.dimensions)
    except GuideImportError:
        raise GuideImportError("invalid_embedding_response") from None
    return values


class RagIndexService:
    """先验证并生成整份版本的索引，再一次原子保存；失败不隐式付费重试。"""

    def __init__(
        self,
        knowledge: KnowledgeRepository,
        vectors: RagRepository,
        embedding: EmbeddingClient,
        *,
        max_chunks: int = 500,
        batch_size: int = 16,
        timeout_seconds: float = 30,
    ) -> None:
        require_budget(max_chunks)
        require_budget(batch_size, 16)
        _timeout(timeout_seconds)
        self._knowledge = knowledge
        self._vectors = vectors
        self._embedding = embedding
        self._max_chunks = max_chunks
        self._batch_size = batch_size
        self._timeout_seconds = timeout_seconds

    async def index_document(self, document_id: UUID) -> IndexResult:
        """复用正确的已有向量，所有批次完成前不写入任何新向量。"""
        if not isinstance(document_id, UUID):
            raise GuideImportError("invalid_rag_query")
        try:
            async with asyncio.timeout(self._timeout_seconds):
                return await self._index_document(document_id)
        except TimeoutError:
            raise GuideImportError("rag_timeout") from None
        except GuideImportError:
            raise
        except Exception:
            raise GuideImportError("rag_operation_failed") from None

    async def _index_document(self, document_id: UUID) -> IndexResult:
        profile = _profile(self._embedding)
        document = await self._knowledge.get_document(document_id)
        expected_status = DocumentStatus.DRAFT if profile.demo_only else DocumentStatus.PUBLISHED
        if (
            document.id != document_id
            or document.status is not expected_status
            or document.snapshot.get("is_fixture") is not profile.demo_only
            or document.snapshot.get("complete") is not True
            or not isinstance(document.game_version, str)
            or not bounded_text(document.game_version, 80)
            or document.game_version.strip().lower() in {"unknown", "未知"}
        ):
            raise GuideImportError("embedding_scope_mismatch")
        checkpoint = await self._knowledge.get_checkpoint(document_id, StepName.CHUNK)
        chunks = await self._knowledge.list_chunks(document_id)
        if (
            not isinstance(chunks, tuple)
            or not chunks
            or checkpoint is None
            or checkpoint.document_id != document_id
            or checkpoint.name is not StepName.CHUNK
            or checkpoint.status is not StepStatus.COMPLETED
        ):
            raise GuideImportError("rag_chunks_not_ready")
        require_valid_checkpoint(checkpoint, chunks)
        if (
            len(chunks) > self._max_chunks
            or sum(len(embedding_text(chunk)) for chunk in chunks) > MAX_INDEX_CHARACTERS
        ):
            raise GuideImportError("rag_index_budget_exceeded")
        existing = await self._vectors.get_vectors(profile, tuple(chunk.id for chunk in chunks))
        indexed = await asyncio.to_thread(_vector_map, existing, profile, chunks, complete=False)
        missing = tuple(chunk for chunk in chunks if chunk.id not in indexed)
        created: list[VectorEntry] = []
        for start in range(0, len(missing), self._batch_size):
            batch = missing[start : start + self._batch_size]
            _unchanged(self._embedding, profile)
            values = await self._embedding.embed(tuple(embedding_text(chunk) for chunk in batch))
            _unchanged(self._embedding, profile)
            values = _embedding_values(values, len(batch), profile)
            created.extend(
                VectorEntry(chunk.id, profile, content_digest(embedding_text(chunk)), vector)
                for chunk, vector in zip(batch, values, strict=True)
            )
        _unchanged(self._embedding, profile)
        if created:
            await self._vectors.save_vectors(tuple(created))
        return IndexResult(document_id, profile, len(chunks), len(created), len(indexed))


def _stable(hit: EvidenceHit) -> tuple[str, int, str]:
    return str(hit.document.id), hit.chunk.ordinal, str(hit.chunk.id)


def _cosine_scores(
    query_vector: tuple[float, ...], indexed: dict[UUID, VectorEntry]
) -> dict[UUID, float]:
    """有界 CPU 工作在线程计算；取消停止等待，线程最多处理既定候选预算。"""
    return {
        chunk_id: cosine_similarity(query_vector, entry.values)
        for chunk_id, entry in indexed.items()
    }


class RagSearchService:
    """关键词、余弦或倒数排名融合；只对仓储已选定的最新可见候选排序。"""

    def __init__(
        self,
        repository: RagRepository,
        embedding: EmbeddingClient | None = None,
        *,
        max_candidates: int = 500,
        timeout_seconds: float = 30,
    ) -> None:
        require_budget(max_candidates)
        _timeout(timeout_seconds)
        self._repository = repository
        self._embedding = embedding
        self._max_candidates = max_candidates
        self._timeout_seconds = timeout_seconds

    async def search(self, query: RagQuery) -> RetrievalResult:
        """缺失索引整次失败，不因新版未索引而回退旧版或悄悄只做关键词。"""
        if not isinstance(query, RagQuery):
            raise GuideImportError("invalid_rag_query")
        try:
            async with asyncio.timeout(self._timeout_seconds):
                return await self._search(query)
        except TimeoutError:
            raise GuideImportError("rag_timeout") from None
        except GuideImportError:
            raise
        except Exception:
            raise GuideImportError("rag_operation_failed") from None

    async def _search(self, query: RagQuery) -> RetrievalResult:
        candidates = await self._repository.list_candidates(
            query.scope, max_chunks=self._max_candidates
        )
        if not isinstance(candidates, tuple) or any(
            not isinstance(hit, EvidenceHit)
            or not document_in_scope(hit.document, query.scope)
            or hit.chunk.document_id != hit.document.id
            for hit in candidates
        ):
            raise GuideImportError("invalid_rag_candidates")
        if len(candidates) > self._max_candidates:
            raise GuideImportError("rag_candidate_budget_exceeded")
        if len({hit.chunk.id for hit in candidates}) != len(candidates):
            raise GuideImportError("invalid_rag_candidates")
        # 防御有缺陷的端口在单次返回里混合一个来源的多个版本；完整 latest 规则由仓储守护。
        versions: dict[UUID, UUID] = {}
        for hit in candidates:
            previous = versions.setdefault(hit.document.source.id, hit.document.id)
            if previous != hit.document.id:
                raise GuideImportError("invalid_rag_candidates")
        if not candidates:
            return RetrievalResult((), query.mode, 0, 0, None)
        scores = {hit.chunk.id: keyword_score(hit.chunk, query.keywords) for hit in candidates}
        lexical = sorted(
            (hit for hit in candidates if scores[hit.chunk.id] > 0),
            key=lambda hit: (-scores[hit.chunk.id], _stable(hit)),
        )
        keyword_ranks = {hit.chunk.id: rank for rank, hit in enumerate(lexical, 1)}
        vector_scores: dict[UUID, float] = {}
        vector_ranks: dict[UUID, int] = {}
        profile: EmbeddingProfile | None = None
        if query.mode is not RetrievalMode.KEYWORD:
            if self._embedding is None:
                raise GuideImportError("embedding_not_configured")
            profile = _profile(self._embedding)
            if profile.demo_only is not query.scope.demo_only:
                raise GuideImportError("embedding_scope_mismatch")
            chunks = tuple(hit.chunk for hit in candidates)
            entries = await self._repository.get_vectors(
                profile, tuple(chunk.id for chunk in chunks)
            )
            indexed = await asyncio.to_thread(_vector_map, entries, profile, chunks, complete=True)
            _unchanged(self._embedding, profile)
            values = await self._embedding.embed((query.text,))
            _unchanged(self._embedding, profile)
            query_vector = _embedding_values(values, 1, profile)[0]
            vector_scores = await asyncio.to_thread(_cosine_scores, query_vector, indexed)
            _unchanged(self._embedding, profile)
            semantic = sorted(
                (hit for hit in candidates if vector_scores[hit.chunk.id] > 0),
                key=lambda hit: (-vector_scores[hit.chunk.id], _stable(hit)),
            )
            vector_ranks = {hit.chunk.id: rank for rank, hit in enumerate(semantic, 1)}
        hits: list[RankedEvidence] = []
        for hit in candidates:
            keyword_rank, vector_rank = (
                keyword_ranks.get(hit.chunk.id),
                vector_ranks.get(hit.chunk.id),
            )
            if query.mode is RetrievalMode.KEYWORD:
                fused = float(scores[hit.chunk.id]) if keyword_rank is not None else 0.0
            elif query.mode is RetrievalMode.VECTOR:
                fused = vector_scores[hit.chunk.id] if vector_rank is not None else 0.0
            else:
                fused = sum(
                    1 / (60 + rank) for rank in (keyword_rank, vector_rank) if rank is not None
                )
            if fused > 0:
                hits.append(
                    RankedEvidence(
                        replace(hit, score=scores[hit.chunk.id]),
                        scores[hit.chunk.id],
                        vector_scores.get(hit.chunk.id),
                        keyword_rank,
                        vector_rank,
                        fused,
                    )
                )
        hits.sort(key=lambda item: (-item.fused_score, _stable(item.evidence)))
        warnings = (
            ("lexical_vectors_not_semantic",)
            if profile is not None and not profile.semantic
            else ()
        )
        return RetrievalResult(
            tuple(hits[: query.limit]),
            query.mode,
            len(candidates),
            len(candidates) if profile else 0,
            profile,
            warnings,
        )


async def read_context(
    reader: KnowledgeReader,
    ranked: RankedEvidence,
    scope: EvidenceScope,
    *,
    max_chunks: int = 20,
    timeout_seconds: float = 30,
) -> EvidenceWindow:
    """复用既有前置/相邻回读；身份漂移即失败，截断原样保留给回答前置检查。"""
    require_context_limit(max_chunks)
    _timeout(timeout_seconds)
    if not isinstance(ranked, RankedEvidence) or not isinstance(scope, EvidenceScope):
        raise GuideImportError("invalid_rag_query")
    hit = ranked.evidence
    if not document_in_scope(hit.document, scope) or hit.chunk.document_id != hit.document.id:
        raise GuideImportError("evidence_not_found")
    try:
        async with asyncio.timeout(timeout_seconds):
            window = await reader.read_evidence(hit.chunk.id, scope, max_chunks=max_chunks)
            start, stop = context_range(hit)
            if (
                not isinstance(window, EvidenceWindow)
                or window.hit.document != hit.document
                or window.hit.chunk != hit.chunk
                or not document_in_scope(window.hit.document, scope)
                or not isinstance(window.chunks, tuple)
                or not window.chunks
                or len(window.chunks) > max_chunks
                or not isinstance(window.context_truncated, bool)
                or any(chunk.document_id != hit.document.id for chunk in window.chunks)
                or len({chunk.id for chunk in window.chunks}) != len(window.chunks)
                or tuple(chunk.ordinal for chunk in window.chunks)
                != tuple(range(start, start + len(window.chunks)))
                or any(chunk.ordinal > stop for chunk in window.chunks)
                or (not window.context_truncated and hit.chunk not in window.chunks)
                or any(chunk.id == hit.chunk.id and chunk != hit.chunk for chunk in window.chunks)
            ):
                raise GuideImportError("evidence_not_found")
            return replace(window, hit=replace(window.hit, score=ranked.keyword_score))
    except TimeoutError:
        raise GuideImportError("rag_timeout") from None
    except GuideImportError:
        raise
    except Exception:
        raise GuideImportError("rag_operation_failed") from None


class RagKnowledgeReader:
    """显式注入旧问答接口的每请求适配器；只有本实例已搜索的证据才能回读。"""

    def __init__(
        self,
        service: RagSearchService,
        reader: KnowledgeReader,
        *,
        question_text: str,
        mode: RetrievalMode = RetrievalMode.HYBRID,
    ) -> None:
        if not bounded_text(question_text, 1000) or not isinstance(mode, RetrievalMode):
            raise GuideImportError("invalid_rag_query")
        self._service = service
        self._reader = reader
        self._text = question_text
        self._mode = mode
        self._authorized: dict[UUID, tuple[EvidenceScope, RankedEvidence]] = {}

    async def search_knowledge(self, query: KeywordQuery) -> tuple[EvidenceHit, ...]:
        result = await self._service.search(
            RagQuery(query.scope, self._text, query.keywords, self._mode, query.limit)
        )
        for ranked in result.hits:
            self._authorized[ranked.evidence.chunk.id] = query.scope, deepcopy(ranked)
        return tuple(item.evidence for item in result.hits)

    async def read_evidence(
        self, chunk_id: UUID, scope: EvidenceScope, *, max_chunks: int = 20
    ) -> EvidenceWindow:
        permitted = self._authorized.get(chunk_id)
        if permitted is None or scope != permitted[0]:
            raise GuideImportError("evidence_not_found")
        return await read_context(self._reader, permitted[1], scope, max_chunks=max_chunks)
