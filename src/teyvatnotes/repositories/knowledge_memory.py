"""知识资料的线程安全、复制隔离内存仓储，仅用于教学与测试。"""

from copy import deepcopy
from dataclasses import replace
from threading import RLock
from uuid import UUID

from teyvatnotes.domain.guides import (
    DocumentStatus,
    GuideChunk,
    GuideDocument,
    GuideImportError,
    IngestionCheckpoint,
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
    keyword_score,
    require_context_limit,
    version_in_scope,
)


class InMemoryKnowledgeRepository:
    """用同步锁保证原子写入，用深复制隔离嵌套字典。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self._documents: dict[UUID, GuideDocument] = {}
        self._checkpoints: dict[tuple[UUID, StepName], IngestionCheckpoint] = {}
        self._chunks: dict[UUID, tuple[GuideChunk, ...]] = {}

    def _get(self, document_id: UUID) -> GuideDocument:
        try:
            return self._documents[document_id]
        except KeyError as exc:
            raise GuideImportError("document_not_found") from exc

    async def register(self, document: GuideDocument) -> GuideDocument:
        with self._lock:
            draft = replace(document, status=DocumentStatus.DRAFT)
            saved = self._documents.setdefault(document.id, deepcopy(draft))
            return deepcopy(saved)

    async def get_document(self, document_id: UUID) -> GuideDocument:
        with self._lock:
            return deepcopy(self._get(document_id))

    async def get_checkpoint(self, document_id: UUID, name: StepName) -> IngestionCheckpoint | None:
        with self._lock:
            self._get(document_id)
            return deepcopy(self._checkpoints.get((document_id, name)))

    async def commit_checkpoint(
        self, checkpoint: IngestionCheckpoint, *, chunks: tuple[GuideChunk, ...] = ()
    ) -> IngestionCheckpoint:
        require_valid_checkpoint(checkpoint, chunks)
        with self._lock:
            self._get(checkpoint.document_id)
            key = (checkpoint.document_id, checkpoint.name)
            previous = self._checkpoints.get(key)
            if previous is not None and previous.status is StepStatus.COMPLETED:
                return deepcopy(previous)
            if chunks:
                self._chunks[checkpoint.document_id] = deepcopy(chunks)
            self._checkpoints[key] = deepcopy(checkpoint)
            return deepcopy(checkpoint)

    async def list_chunks(self, document_id: UUID) -> tuple[GuideChunk, ...]:
        with self._lock:
            self._get(document_id)
            return deepcopy(self._chunks.get(document_id, ()))

    async def publish(self, document_id: UUID) -> GuideDocument:
        with self._lock:
            document = self._get(document_id)
            checkpoints = tuple(
                item for (identity, _), item in self._checkpoints.items() if identity == document_id
            )
            require_publishable(document, checkpoints, len(self._chunks.get(document_id, ())))
            published = replace(document, status=DocumentStatus.PUBLISHED)
            self._documents[document_id] = published
            return deepcopy(published)

    async def search_knowledge(self, query: KeywordQuery) -> tuple[EvidenceHit, ...]:
        """小规模线性扫描；按同来源最新版本选择后再做字面过滤。"""
        with self._lock:
            latest: dict[UUID, GuideDocument] = {}
            for document in self._documents.values():
                if not version_in_scope(document, query.scope):
                    continue
                previous = latest.get(document.source.id)
                if previous is None or (document.created_at, str(document.id)) > (
                    previous.created_at,
                    str(previous.id),
                ):
                    latest[document.source.id] = document
            hits: list[EvidenceHit] = []
            for document in latest.values():
                if not document_in_scope(document, query.scope):
                    continue
                for chunk in self._chunks.get(document.id, ()):
                    score = keyword_score(chunk, query.keywords)
                    if score:
                        hits.append(EvidenceHit(document, chunk, score))
            hits.sort(key=lambda hit: (-hit.score, str(hit.document.id), hit.chunk.ordinal))
            return deepcopy(tuple(hits[: query.limit]))

    async def read_evidence(
        self, chunk_id: UUID, scope: EvidenceScope, *, max_chunks: int = 20
    ) -> EvidenceWindow:
        """在同一临界区回读精确证据版本，不将用户传入的 ID 当成引用内容。"""
        require_context_limit(max_chunks)
        with self._lock:
            for document_id, chunks in self._chunks.items():
                document = self._documents[document_id]
                if not document_in_scope(document, scope):
                    continue
                for chunk in chunks:
                    if chunk.id != chunk_id:
                        continue
                    hit = EvidenceHit(document, chunk)
                    start, stop = context_range(hit)
                    context = tuple(item for item in chunks if start <= item.ordinal <= stop)
                    return deepcopy(
                        EvidenceWindow(hit, context[:max_chunks], len(context) > max_chunks)
                    )
            raise GuideImportError("evidence_not_found")
