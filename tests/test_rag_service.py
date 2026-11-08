"""RAG 用例预算、索引完整性、三路排序、引用回读及取消测试；全部离线。"""

import asyncio
import threading
from copy import deepcopy
from dataclasses import replace
from typing import cast
from uuid import UUID, uuid4

import pytest
from test_guide_ingestion import article_snapshot

from teyvatnotes.application import rag_service as rag_module
from teyvatnotes.application.guide_ingestion import GuideIngestionService
from teyvatnotes.application.rag_service import (
    RagIndexService,
    RagKnowledgeReader,
    RagSearchService,
    read_context,
)
from teyvatnotes.domain.guides import (
    GuideChunk,
    GuideDocument,
    GuideImportError,
    GuideKind,
    StepName,
)
from teyvatnotes.domain.knowledge import (
    EvidenceHit,
    EvidenceScope,
    EvidenceWindow,
    KeywordQuery,
)
from teyvatnotes.domain.rag import (
    EmbeddingProfile,
    RagQuery,
    RetrievalMode,
    VectorEntry,
    content_digest,
    embedding_text,
)
from teyvatnotes.knowledge.snapshots import parse_snapshot
from teyvatnotes.repositories.knowledge_memory import InMemoryKnowledgeRepository
from teyvatnotes.repositories.rag_memory import InMemoryRagRepository


def profile(*, demo: bool = False) -> EmbeddingProfile:
    # 这是测试桩身份，不代表任何服务商已提供语义模型或真实调用。
    return EmbeddingProfile("offline-test", "fixed-test-vectors", "v1", 2, demo, not demo)


def scope(*, demo: bool = False) -> EvidenceScope:
    return EvidenceScope("test-version", GuideKind.QUEST, "示例任务", demo_only=demo)


class FakeEmbedding:
    def __init__(self, selected: EmbeddingProfile | None = None) -> None:
        self.profile = selected or profile()
        self.calls: list[tuple[str, ...]] = []
        self.invalid: object | None = None
        self.fail_at: int | None = None
        self.switch = False
        self.pause = False
        self.entered = asyncio.Event()
        self.query_vector = (1.0, 0.0)

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        self.calls.append(texts)
        if self.pause:
            self.entered.set()
            await asyncio.Event().wait()
        if self.fail_at == len(self.calls):
            raise RuntimeError("PRIVATE-EMBEDDING-ERROR")
        if self.switch:
            self.profile = replace(self.profile, revision="v2")
        if self.invalid is not None:
            return cast(tuple[tuple[float, ...], ...], self.invalid)
        return tuple(self.query_vector for _ in texts)


class FakeRagRepository:
    def __init__(self, candidates: tuple[EvidenceHit, ...] = ()) -> None:
        self.candidates = candidates
        self.rows: dict[tuple[str, UUID], VectorEntry] = {}
        self.save_calls = 0
        self.get_calls = 0
        self.fail = False
        self.pause = False
        self.entered = asyncio.Event()
        self.override_entries: tuple[VectorEntry, ...] | None = None

    async def list_candidates(
        self, requested: EvidenceScope, *, max_chunks: int = 500
    ) -> tuple[EvidenceHit, ...]:
        if self.pause:
            self.entered.set()
            await asyncio.Event().wait()
        if self.fail:
            raise RuntimeError("PRIVATE-REPOSITORY-ERROR")
        # 范围/最新版本由真实仓储负责，本桩故意原样返回以测试应用防御。
        return deepcopy(self.candidates)

    async def get_vectors(
        self, selected: EmbeddingProfile, chunk_ids: tuple[UUID, ...]
    ) -> tuple[VectorEntry, ...]:
        self.get_calls += 1
        if self.override_entries is not None:
            return self.override_entries
        return tuple(
            self.rows[(selected.identity, identifier)]
            for identifier in chunk_ids
            if (selected.identity, identifier) in self.rows
        )

    async def save_vectors(self, entries: tuple[VectorEntry, ...]) -> None:
        self.save_calls += 1
        self.rows.update(
            ((entry.profile.identity, entry.chunk_id), deepcopy(entry)) for entry in entries
        )


async def dataset(
    *, demo: bool = False, publish: bool = True, count: int | None = None
) -> tuple[InMemoryKnowledgeRepository, FakeRagRepository, UUID]:
    knowledge = InMemoryKnowledgeRepository()
    changes: dict[str, object] = {"entities": ["示例任务"], "is_fixture": demo}
    if count is not None:
        changes["article_blocks"] = [
            {"kind": "paragraph", "text": f"第{i}步原文。"} for i in range(count)
        ]
    report = await GuideIngestionService(knowledge).import_snapshot(
        parse_snapshot(article_snapshot(**changes)), publish=publish and not demo
    )
    document = await knowledge.get_document(report.document_id)
    chunks = await knowledge.list_chunks(report.document_id)
    return (
        knowledge,
        FakeRagRepository(tuple(EvidenceHit(document, chunk) for chunk in chunks)),
        report.document_id,
    )


def query(mode: RetrievalMode = RetrievalMode.HYBRID, *, demo: bool = False) -> RagQuery:
    return RagQuery(scope(demo=demo), "如何操作？", ("第二步",), mode)


def fill_vectors(
    repository: FakeRagRepository,
    embedding: FakeEmbedding,
    *,
    vectors: tuple[tuple[float, ...], ...] | None = None,
) -> None:
    chosen = vectors or tuple((1.0, 0.0) for _ in repository.candidates)
    for hit, vector in zip(repository.candidates, chosen, strict=True):
        entry = VectorEntry(
            hit.chunk.id, embedding.profile, content_digest(embedding_text(hit.chunk)), vector
        )
        repository.rows[(embedding.profile.identity, hit.chunk.id)] = entry


def test_index_batches_once_then_reuses_every_valid_vector() -> None:
    async def scenario() -> None:
        knowledge, repository, identifier = await dataset()
        embedding = FakeEmbedding()
        service = RagIndexService(knowledge, repository, embedding, batch_size=2)
        first = await service.index_document(identifier)
        assert first.chunk_count == first.generated_count == 3 and first.reused_count == 0
        assert [len(batch) for batch in embedding.calls] == [2, 1]
        assert repository.save_calls == 1
        chunks = await knowledge.list_chunks(identifier)
        assert embedding.calls[0][0] == embedding_text(chunks[0])
        second = await service.index_document(identifier)
        assert second.generated_count == 0 and second.reused_count == 3
        assert len(embedding.calls) == 2 and repository.save_calls == 1
        del repository.rows[(embedding.profile.identity, chunks[1].id)]
        third = await service.index_document(identifier)
        assert third.generated_count == 1 and third.reused_count == 2
        assert embedding.calls[-1] == (embedding_text(chunks[1]),)

    asyncio.run(scenario())


@pytest.mark.parametrize("nature", ["released", "preview"])
def test_both_content_natures_can_be_indexed_through_real_memory_repository(nature: str) -> None:
    async def scenario() -> None:
        knowledge = InMemoryKnowledgeRepository()
        report = await GuideIngestionService(knowledge).import_snapshot(
            parse_snapshot(article_snapshot(entities=["示例任务"], content_nature=nature)),
            publish=True,
        )
        repository = InMemoryRagRepository(knowledge)
        embedding = FakeEmbedding()
        result = await RagIndexService(knowledge, repository, embedding).index_document(
            report.document_id
        )
        assert result.generated_count == 3
        assert len(await repository.list_candidates(scope())) == 3
        assert len(embedding.calls) == 1

    asyncio.run(scenario())


def test_late_batch_failure_does_not_partially_save_or_automatically_retry() -> None:
    async def scenario() -> None:
        knowledge, repository, identifier = await dataset()
        embedding = FakeEmbedding()
        embedding.fail_at = 2
        service = RagIndexService(knowledge, repository, embedding, batch_size=2)
        with pytest.raises(GuideImportError, match="^rag_operation_failed$") as caught:
            await service.index_document(identifier)
        assert "PRIVATE" not in str(caught.value) and caught.value.__cause__ is None
        assert len(embedding.calls) == 2 and repository.save_calls == 0 and not repository.rows

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "demo,embedding_demo,publish",
    [(False, False, False), (False, True, True), (True, False, False)],
)
def test_index_rejects_draft_real_or_cross_scope(
    demo: bool, embedding_demo: bool, publish: bool
) -> None:
    async def scenario() -> None:
        knowledge, repository, identifier = await dataset(demo=demo, publish=publish)
        embedding = FakeEmbedding(profile(demo=embedding_demo))
        with pytest.raises(GuideImportError, match="^embedding_scope_mismatch$"):
            await RagIndexService(knowledge, repository, embedding).index_document(identifier)
        assert not embedding.calls and repository.save_calls == 0

    asyncio.run(scenario())


def test_index_checks_chunk_count_and_whole_document_character_budget() -> None:
    async def scenario() -> None:
        knowledge, repository, identifier = await dataset(count=3)
        embedding = FakeEmbedding()
        with pytest.raises(GuideImportError, match="rag_index_budget_exceeded"):
            await RagIndexService(knowledge, repository, embedding, max_chunks=2).index_document(
                identifier
            )
        assert not embedding.calls
        huge = article_snapshot(
            entities=["示例任务"],
            article_blocks=[{"kind": "paragraph", "text": "长" * 2000} for _ in range(65)],
        )
        report = await GuideIngestionService(knowledge, max_chunk_chars=2000).import_snapshot(
            parse_snapshot(huge), publish=True
        )
        with pytest.raises(GuideImportError, match="rag_index_budget_exceeded"):
            await RagIndexService(knowledge, repository, embedding).index_document(
                report.document_id
            )
        assert not embedding.calls

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "invalid", [(), ((1.0,),), ((0.0, 0.0),), ((float("nan"), 1.0),), ((True, 1.0),), [(1.0, 0.0)]]
)
def test_bad_embedding_count_dimension_or_value_cannot_be_saved(invalid: object) -> None:
    async def scenario() -> None:
        knowledge, repository, identifier = await dataset(count=1)
        embedding = FakeEmbedding()
        embedding.invalid = invalid
        with pytest.raises(GuideImportError, match="^invalid_embedding_response$"):
            await RagIndexService(knowledge, repository, embedding).index_document(identifier)
        assert repository.save_calls == 0

    asyncio.run(scenario())


def test_embedding_profile_cannot_change_during_index_or_query() -> None:
    async def scenario() -> None:
        knowledge, repository, identifier = await dataset()
        embedding = FakeEmbedding()
        embedding.switch = True
        with pytest.raises(GuideImportError, match="embedding_profile_changed"):
            await RagIndexService(knowledge, repository, embedding).index_document(identifier)
        assert repository.save_calls == 0
        embedding = FakeEmbedding()
        fill_vectors(repository, embedding)
        embedding.switch = True
        with pytest.raises(GuideImportError, match="embedding_profile_changed"):
            await RagSearchService(repository, embedding).search(query())

    asyncio.run(scenario())


def test_existing_wrong_hash_or_profile_is_rejected_not_overwritten() -> None:
    async def scenario() -> None:
        knowledge, repository, identifier = await dataset()
        embedding = FakeEmbedding()
        fill_vectors(repository, embedding)
        first = next(iter(repository.rows.values()))
        for corrupt in (
            replace(first, content_hash="0" * 64),
            replace(first, profile=replace(first.profile, revision="v2")),
        ):
            repository.override_entries = (corrupt,)
            with pytest.raises(GuideImportError, match="rag_index_conflict"):
                await RagIndexService(knowledge, repository, embedding).index_document(identifier)
        assert not embedding.calls and repository.save_calls == 0

    asyncio.run(scenario())


def test_search_rejects_corrupt_hash_duplicate_or_foreign_vector_entries() -> None:
    async def scenario() -> None:
        _, repository, _ = await dataset()
        embedding = FakeEmbedding()
        fill_vectors(repository, embedding)
        entries = tuple(repository.rows.values())
        corrupt_values = deepcopy(entries[0])
        object.__setattr__(corrupt_values, "values", (0.0, 0.0))
        malformed_identifier = deepcopy(entries[0])
        object.__setattr__(malformed_identifier, "chunk_id", [])
        for corrupt in (
            (replace(entries[0], content_hash="0" * 64), *entries[1:]),
            (
                replace(
                    entries[0],
                    profile=replace(embedding.profile, dimensions=3),
                    values=(1.0, 0.0, 0.0),
                ),
                *entries[1:],
            ),
            (*entries, entries[0]),
            (replace(entries[0], chunk_id=uuid4()), *entries[1:]),
            (corrupt_values, *entries[1:]),
            (malformed_identifier, *entries[1:]),
        ):
            repository.override_entries = corrupt
            with pytest.raises(GuideImportError, match="^rag_index_incomplete$"):
                await RagSearchService(repository, embedding).search(query())
        assert not embedding.calls

    asyncio.run(scenario())


def test_missing_chunk_checkpoint_cannot_start_index() -> None:
    class MissingCheckpointRepository(InMemoryKnowledgeRepository):
        def __init__(self, existing: InMemoryKnowledgeRepository) -> None:
            super().__init__()
            self.existing = existing

        async def get_document(self, document_id: UUID) -> GuideDocument:
            return await self.existing.get_document(document_id)

        async def list_chunks(self, document_id: UUID) -> tuple[GuideChunk, ...]:
            return await self.existing.list_chunks(document_id)

        async def get_checkpoint(self, document_id: UUID, name: StepName) -> None:
            return None

    async def scenario() -> None:
        knowledge, repository, identifier = await dataset()
        embedding = FakeEmbedding()
        blocked = MissingCheckpointRepository(knowledge)
        with pytest.raises(GuideImportError, match="^rag_chunks_not_ready$"):
            await RagIndexService(blocked, repository, embedding).index_document(identifier)
        assert not embedding.calls and not repository.rows

    asyncio.run(scenario())


def test_keyword_needs_no_embedding_and_vector_ranking_differs() -> None:
    async def scenario() -> None:
        _, repository, _ = await dataset()
        embedding = FakeEmbedding()
        fill_vectors(repository, embedding, vectors=((1.0, 0.0), (0.0, 1.0), (0.8, 0.6)))
        service = RagSearchService(repository, embedding)
        keyword = await service.search(query(RetrievalMode.KEYWORD))
        assert len(keyword.hits) == 1 and keyword.hits[0].evidence.chunk.ordinal == 2
        assert keyword.hits[0].keyword_score == 4 and keyword.hits[0].vector_score is None
        assert keyword.profile is None and keyword.indexed_count == 0
        assert not embedding.calls and repository.get_calls == 0
        vector = await service.search(query(RetrievalMode.VECTOR))
        assert [item.evidence.chunk.ordinal for item in vector.hits] == [0, 2]
        assert vector.hits[0].vector_score == pytest.approx(1.0)
        assert vector.hits[0].evidence.score == 0  # 不把余弦浮点数硬塞旧关键词score。
        hybrid = await service.search(query())
        assert hybrid.hits[0].evidence.chunk.ordinal == 2
        assert hybrid.hits[0].fused_score == pytest.approx(1 / 61 + 1 / 62)
        assert hybrid.hits[0].keyword_rank == 1 and hybrid.hits[0].vector_rank == 2

    asyncio.run(scenario())


def test_equal_rank_is_stable_and_negative_cosine_is_not_recalled() -> None:
    async def scenario() -> None:
        _, repository, _ = await dataset()
        embedding = FakeEmbedding()
        fill_vectors(repository, embedding, vectors=((1.0, 0.0), (1.0, 0.0), (-1.0, 0.0)))
        repository.candidates = tuple(reversed(repository.candidates))
        service = RagSearchService(repository, embedding)
        result = await service.search(query(RetrievalMode.VECTOR))
        assert [item.evidence.chunk.ordinal for item in result.hits] == [0, 1]
        assert result.hits[0].vector_rank == 1 and result.hits[1].vector_rank == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", [RetrievalMode.VECTOR, RetrievalMode.HYBRID])
def test_missing_any_index_fails_before_query_embedding_without_keyword_fallback(
    mode: RetrievalMode,
) -> None:
    async def scenario() -> None:
        _, repository, _ = await dataset()
        embedding = FakeEmbedding()
        fill_vectors(repository, embedding)
        identifier = repository.candidates[0].chunk.id
        del repository.rows[(embedding.profile.identity, identifier)]
        with pytest.raises(GuideImportError, match="^rag_index_incomplete$"):
            await RagSearchService(repository, embedding).search(query(mode))
        assert not embedding.calls

    asyncio.run(scenario())


def test_unconfigured_embedding_wrong_scope_and_empty_candidates() -> None:
    async def scenario() -> None:
        _, repository, _ = await dataset()
        with pytest.raises(GuideImportError, match="^embedding_not_configured$"):
            await RagSearchService(repository).search(query())
        with pytest.raises(GuideImportError, match="^embedding_scope_mismatch$"):
            await RagSearchService(repository, FakeEmbedding(profile(demo=True))).search(query())
        empty = await RagSearchService(FakeRagRepository()).search(query())
        assert empty.hits == () and empty.candidate_count == empty.indexed_count == 0

    asyncio.run(scenario())


def test_demo_lexical_vectors_are_explicitly_not_semantic() -> None:
    async def scenario() -> None:
        knowledge, repository, identifier = await dataset(demo=True)
        embedding = FakeEmbedding(profile(demo=True))
        await RagIndexService(knowledge, repository, embedding).index_document(identifier)
        result = await RagSearchService(repository, embedding).search(query(demo=True))
        assert result.profile is not None and not result.profile.semantic
        assert result.warnings == ("lexical_vectors_not_semantic",)

    asyncio.run(scenario())


def test_out_of_scope_duplicate_oversized_or_cross_version_candidates_fail() -> None:
    async def scenario() -> None:
        _, repository, _ = await dataset()
        hit = repository.candidates[0]
        cases = [
            (
                (replace(hit, document=replace(hit.document, game_version="wrong")),),
                "invalid_rag_candidates",
            ),
            (
                (replace(hit, chunk=replace(hit.chunk, document_id=uuid4())),),
                "invalid_rag_candidates",
            ),
            ((hit, hit), "invalid_rag_candidates"),
            (
                (
                    hit,
                    replace(
                        hit,
                        document=replace(hit.document, id=uuid4()),
                        chunk=replace(hit.chunk, id=uuid4(), document_id=uuid4()),
                    ),
                ),
                "invalid_rag_candidates",
            ),
        ]
        for candidates, code in cases:
            repository.candidates = candidates
            with pytest.raises(GuideImportError, match=code):
                await RagSearchService(repository).search(query(RetrievalMode.KEYWORD))
        _, repository, _ = await dataset()
        with pytest.raises(GuideImportError, match="rag_candidate_budget_exceeded"):
            await RagSearchService(repository, max_candidates=2).search(
                query(RetrievalMode.KEYWORD)
            )

    asyncio.run(scenario())


def test_new_unindexed_version_does_not_use_old_vectors() -> None:
    async def scenario() -> None:
        knowledge, repository, old_identifier = await dataset()
        embedding = FakeEmbedding()
        await RagIndexService(knowledge, repository, embedding).index_document(old_identifier)
        newer = await GuideIngestionService(knowledge).import_snapshot(
            parse_snapshot(
                article_snapshot(
                    entities=["示例任务"],
                    article_blocks=[{"kind": "paragraph", "text": "新的第二步说明。"}],
                )
            ),
            publish=True,
        )
        document = await knowledge.get_document(newer.document_id)
        chunks = await knowledge.list_chunks(newer.document_id)
        repository.candidates = tuple(EvidenceHit(document, chunk) for chunk in chunks)
        with pytest.raises(GuideImportError, match="rag_index_incomplete"):
            await RagSearchService(repository, embedding).search(query())
        assert len(embedding.calls) == 1  # 只有旧索引批次，未用旧向量生成查询向量。

    asyncio.run(scenario())


def test_read_context_preserves_quest_prefix_and_explicit_truncation() -> None:
    async def scenario() -> None:
        knowledge, repository, _ = await dataset(count=25)
        result = await RagSearchService(repository).search(
            RagQuery(scope(), "第24步", ("第24步",), RetrievalMode.KEYWORD)
        )
        window = await read_context(knowledge, result.hits[0], scope())
        assert [chunk.ordinal for chunk in window.chunks] == list(range(20))
        assert window.context_truncated and window.hit.score == result.hits[0].keyword_score

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fault", ["wrong_document", "wrong_text", "gap", "wrong_start", "wrong_scope", "too_many"]
)
def test_context_rejects_identity_drift_and_fake_complete_prefix(fault: str) -> None:
    knowledge = InMemoryKnowledgeRepository()

    class BadReader(InMemoryKnowledgeRepository):
        async def read_evidence(
            self, chunk_id: UUID, requested: EvidenceScope, *, max_chunks: int = 20
        ) -> EvidenceWindow:
            original = await knowledge.read_evidence(chunk_id, requested, max_chunks=max_chunks)
            if fault == "wrong_document":
                return replace(
                    original,
                    hit=replace(original.hit, document=replace(original.hit.document, id=uuid4())),
                )
            if fault == "wrong_text":
                return replace(
                    original,
                    hit=replace(original.hit, chunk=replace(original.hit.chunk, text="变更正文")),
                )
            if fault == "gap":
                return replace(original, chunks=(original.chunks[0], original.chunks[2]))
            if fault == "wrong_start":
                return replace(original, chunks=original.chunks[1:])
            if fault == "wrong_scope":
                return replace(
                    original,
                    hit=replace(
                        original.hit, document=replace(original.hit.document, game_version="wrong")
                    ),
                )
            return replace(original, chunks=original.chunks * 8)

    async def scenario() -> None:
        nonlocal knowledge
        knowledge, repository, _ = await dataset()
        result = await RagSearchService(repository).search(query(RetrievalMode.KEYWORD))
        with pytest.raises(GuideImportError, match="evidence_not_found"):
            await read_context(BadReader(), result.hits[0], scope())

    asyncio.run(scenario())


def test_adapter_requires_search_grant_and_same_scope_without_replacing_keyword_score() -> None:
    async def scenario() -> None:
        knowledge, repository, _ = await dataset()
        embedding = FakeEmbedding()
        fill_vectors(repository, embedding)
        adapter = RagKnowledgeReader(
            RagSearchService(repository, embedding), knowledge, question_text="如何操作？"
        )
        identifier = repository.candidates[0].chunk.id
        with pytest.raises(GuideImportError, match="evidence_not_found"):
            await adapter.read_evidence(identifier, scope())
        hits = await adapter.search_knowledge(KeywordQuery(scope(), ("第二步",)))
        assert hits and all(isinstance(hit.score, int) for hit in hits)
        window = await adapter.read_evidence(hits[0].chunk.id, scope())
        assert window.hit.score == hits[0].score and not window.context_truncated
        with pytest.raises(GuideImportError, match="evidence_not_found"):
            await adapter.read_evidence(hits[0].chunk.id, replace(scope(), entity="其他任务"))
        another = RagKnowledgeReader(
            RagSearchService(repository),
            knowledge,
            question_text="另一个问题",
            mode=RetrievalMode.KEYWORD,
        )
        with pytest.raises(GuideImportError, match="evidence_not_found"):
            await another.read_evidence(hits[0].chunk.id, scope())

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["index", "search"])
def test_cancellation_during_embedding_propagates_without_save(operation: str) -> None:
    async def scenario() -> None:
        knowledge, repository, identifier = await dataset()
        embedding = FakeEmbedding()
        embedding.pause = True

        async def execute() -> object:
            if operation == "index":
                return await RagIndexService(knowledge, repository, embedding).index_document(
                    identifier
                )
            fill_vectors(repository, embedding)
            return await RagSearchService(repository, embedding).search(query())

        running = asyncio.create_task(execute())
        await embedding.entered.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert repository.save_calls == 0 and len(embedding.calls) == 1

    asyncio.run(scenario())


def test_timeout_covers_candidate_and_embedding_wait() -> None:
    async def scenario() -> None:
        knowledge, repository, identifier = await dataset()
        repository.pause = True
        with pytest.raises(GuideImportError, match="rag_timeout"):
            await RagSearchService(repository, timeout_seconds=0.01).search(
                query(RetrievalMode.KEYWORD)
            )
        repository.pause = False
        embedding = FakeEmbedding()
        embedding.pause = True
        with pytest.raises(GuideImportError, match="rag_timeout"):
            await RagIndexService(
                knowledge, repository, embedding, timeout_seconds=0.01
            ).index_document(identifier)
        assert repository.save_calls == 0

    asyncio.run(scenario())


def test_read_timeout_and_cancellation_preserve_errors_without_next_call() -> None:
    class WaitingReader(InMemoryKnowledgeRepository):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.calls = 0

        async def read_evidence(
            self, chunk_id: UUID, requested: EvidenceScope, *, max_chunks: int = 20
        ) -> EvidenceWindow:
            self.calls += 1
            self.entered.set()
            await asyncio.Event().wait()
            raise AssertionError("取消后不能继续回读")

    async def scenario() -> None:
        _, repository, _ = await dataset()
        ranked = (await RagSearchService(repository).search(query(RetrievalMode.KEYWORD))).hits[0]
        reader = WaitingReader()
        with pytest.raises(GuideImportError, match="^rag_timeout$"):
            await read_context(reader, ranked, scope(), timeout_seconds=0.01)
        running = asyncio.create_task(read_context(reader, ranked, scope()))
        reader.entered.clear()
        await reader.entered.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert reader.calls == 2

    asyncio.run(scenario())


def test_cpu_scoring_runs_off_event_loop_and_cancel_stops_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        _, repository, _ = await dataset()
        embedding = FakeEmbedding()
        fill_vectors(repository, embedding)
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()
        original = rag_module._cosine_scores

        def paused(
            values: tuple[float, ...], indexed: dict[UUID, VectorEntry]
        ) -> dict[UUID, float]:
            loop.call_soon_threadsafe(entered.set)
            release.wait(timeout=5)
            return original(values, indexed)

        monkeypatch.setattr(rag_module, "_cosine_scores", paused)
        running = asyncio.create_task(RagSearchService(repository, embedding).search(query()))
        try:
            await entered.wait()
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
            assert not release.is_set()
        finally:
            release.set()  # 线程有限工作收敛；取消不假装能强杀正在计算的线程。

    asyncio.run(scenario())


def test_batch_configuration_cannot_exceed_sixteen() -> None:
    with pytest.raises(GuideImportError, match="invalid_rag_budget"):
        RagIndexService(
            InMemoryKnowledgeRepository(), FakeRagRepository(), FakeEmbedding(), batch_size=17
        )


@pytest.mark.parametrize("value", [True, 0, -1, 501, 1.5])
def test_invalid_budgets_are_rejected(value: int) -> None:
    with pytest.raises(GuideImportError, match="invalid_rag_budget"):
        RagSearchService(FakeRagRepository(), max_candidates=value)


@pytest.mark.parametrize("value", [True, 0, -1, float("nan"), float("inf"), 121])
def test_timeout_configuration_cannot_be_nan_or_unbounded(value: float) -> None:
    with pytest.raises(GuideImportError, match="invalid_rag_budget"):
        RagSearchService(FakeRagRepository(), timeout_seconds=value)


def test_unexpected_repository_error_is_sanitized() -> None:
    async def scenario() -> None:
        repository = FakeRagRepository()
        repository.fail = True
        with pytest.raises(GuideImportError, match="^rag_operation_failed$") as caught:
            await RagSearchService(repository).search(query(RetrievalMode.KEYWORD))
        assert "PRIVATE" not in str(caught.value) and caught.value.__cause__ is None

    asyncio.run(scenario())
