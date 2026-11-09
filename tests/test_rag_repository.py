"""RAG 内存目录、最新版本范围和不可覆盖的整批向量写入契约。"""

import asyncio
from dataclasses import replace
from typing import cast
from uuid import UUID, uuid4

import pytest
from test_guide_ingestion import article_snapshot
from test_knowledge_retrieval import scope, searchable_snapshot

from teyvatnotes.application.guide_ingestion import GuideIngestionService
from teyvatnotes.domain.guides import GuideChunk, GuideImportError
from teyvatnotes.domain.rag import (
    EmbeddingProfile,
    VectorEntry,
    content_digest,
    embedding_text,
)
from teyvatnotes.knowledge.demo import create_demo_repository
from teyvatnotes.knowledge.snapshots import parse_snapshot
from teyvatnotes.repositories.knowledge_memory import InMemoryKnowledgeRepository
from teyvatnotes.repositories.rag_memory import InMemoryRagRepository

SEMANTIC = EmbeddingProfile("test-provider", "test-model", "rev-1", 2)
DEMO = EmbeddingProfile("local", "lexical-test", "rev-1", 2, demo_only=True, semantic=False)


def vector(chunk: GuideChunk, profile: EmbeddingProfile = SEMANTIC) -> VectorEntry:
    """原始值不是单位向量，用于证明仓储不会静默改写模型返回值。"""
    return VectorEntry(chunk.id, profile, content_digest(embedding_text(chunk)), (3.0, 4.0))


async def published_repository() -> tuple[InMemoryKnowledgeRepository, tuple[GuideChunk, ...]]:
    repository = InMemoryKnowledgeRepository()
    report = await GuideIngestionService(repository).import_snapshot(
        parse_snapshot(searchable_snapshot()), publish=True
    )
    return repository, await repository.list_chunks(report.document_id)


def test_candidates_are_latest_in_scope_complete_and_copy_isolated() -> None:
    async def scenario() -> None:
        knowledge, chunks = await published_repository()
        rag = InMemoryRagRepository(knowledge)
        actual = await rag.list_candidates(scope())
        assert tuple(hit.chunk for hit in actual) == chunks
        assert all(hit.score == 0 for hit in actual)
        actual[0].document.snapshot["conditions"] = {"private": "changed"}
        actual[0].chunk.locators[0]["block_index"] = -1
        again = await rag.list_candidates(scope())
        assert again[0].document.snapshot["conditions"] == {}
        assert again[0].chunk.locators[0]["block_index"] >= 0
        for other in (
            scope(game_version="other"),
            scope(entity="other"),
            scope(demo_only=True),
        ):
            assert await rag.list_candidates(other) == ()
        service = GuideIngestionService(knowledge)
        changed = searchable_snapshot(
            conditions={"新前置": "已完成"},
            article_blocks=[{"kind": "paragraph", "text": "新版操作。"}],
        )
        await service.import_snapshot(parse_snapshot(changed))
        assert tuple(hit.chunk for hit in await rag.list_candidates(scope())) == chunks
        await service.import_snapshot(parse_snapshot(changed), publish=True)
        # 最新可见版本前提不满足时不能回退到旧版通用资料。
        assert await rag.list_candidates(scope()) == ()
        assert len(await rag.list_candidates(scope(conditions=(("新前置", "已完成"),)))) == 1

    asyncio.run(scenario())


def test_all_conditions_must_match_and_extra_player_conditions_are_allowed() -> None:
    async def scenario() -> None:
        knowledge = InMemoryKnowledgeRepository()
        await GuideIngestionService(knowledge).import_snapshot(
            parse_snapshot(searchable_snapshot(conditions={"前置": "完成", "等级": "达到"})),
            publish=True,
        )
        rag = InMemoryRagRepository(knowledge)
        for conditions in ((), (("前置", "完成"),), (("前置", "未完成"), ("等级", "达到"))):
            assert await rag.list_candidates(scope(conditions=conditions)) == ()
        allowed = scope(conditions=(("前置", "完成"), ("等级", "达到"), ("额外", "允许")))
        assert await rag.list_candidates(allowed)

    asyncio.run(scenario())


@pytest.mark.parametrize("nature", ["released", "preview"])
def test_content_nature_does_not_exclude_candidates_or_vector_writes(nature: str) -> None:
    async def scenario() -> None:
        knowledge = InMemoryKnowledgeRepository()
        report = await GuideIngestionService(knowledge).import_snapshot(
            parse_snapshot(searchable_snapshot(content_nature=nature)), publish=True
        )
        rag = InMemoryRagRepository(knowledge)
        hits = await rag.list_candidates(scope())
        assert hits and all(hit.document.id == report.document_id for hit in hits)
        entries = tuple(vector(hit.chunk) for hit in hits)
        await rag.save_vectors(entries)
        assert await rag.get_vectors(SEMANTIC, tuple(hit.chunk.id for hit in hits)) == entries

    asyncio.run(scenario())


def test_demo_candidates_never_mix_with_formal_published_material() -> None:
    async def scenario() -> None:
        knowledge = await create_demo_repository()
        rag = InMemoryRagRepository(knowledge)
        demo = scope(
            game_version="DEMO-ONLY",
            entity="示例任务甲",
            conditions=(("前置任务", "已完成示例前置"),),
            demo_only=True,
        )
        hits = await rag.list_candidates(demo)
        assert hits and all(hit.document.snapshot["is_fixture"] is True for hit in hits)
        assert await rag.list_candidates(replace(demo, demo_only=False)) == ()

    asyncio.run(scenario())


@pytest.mark.parametrize("budget", [True, 0, 501, 1.5, "5"])
def test_candidate_budget_rejects_invalid_types_and_range(budget: int) -> None:
    async def scenario() -> None:
        knowledge, _ = await published_repository()
        with pytest.raises(GuideImportError, match="invalid_rag_budget"):
            await InMemoryRagRepository(knowledge).list_candidates(scope(), max_chunks=budget)

    asyncio.run(scenario())


def test_candidate_excess_fails_instead_of_silently_taking_a_prefix() -> None:
    async def scenario() -> None:
        knowledge, chunks = await published_repository()
        rag = InMemoryRagRepository(knowledge)
        with pytest.raises(GuideImportError, match="rag_candidate_budget_exceeded"):
            await rag.list_candidates(scope(), max_chunks=len(chunks) - 1)
        assert len(await rag.list_candidates(scope(), max_chunks=len(chunks))) == len(chunks)
        await GuideIngestionService(knowledge).import_snapshot(
            parse_snapshot(
                searchable_snapshot(
                    article_blocks=[
                        {"kind": "paragraph", "text": f"步骤 {i}。"} for i in range(501)
                    ]
                )
            ),
            publish=True,
        )
        with pytest.raises(GuideImportError, match="rag_candidate_budget_exceeded"):
            await rag.list_candidates(scope())

    asyncio.run(scenario())


def test_original_vectors_are_idempotent_profile_isolated_and_in_requested_order() -> None:
    async def scenario() -> None:
        knowledge, chunks = await published_repository()
        rag = InMemoryRagRepository(knowledge)
        entries = tuple(vector(chunk) for chunk in chunks)
        await rag.save_vectors(entries)
        await rag.save_vectors(entries)
        actual = await rag.get_vectors(SEMANTIC, tuple(chunk.id for chunk in reversed(chunks)))
        assert actual == tuple(reversed(entries))
        assert actual[0].values == (3.0, 4.0)
        for profile in (
            replace(SEMANTIC, revision="rev-2"),
            replace(SEMANTIC, model="another"),
            replace(SEMANTIC, dimensions=3),
        ):
            assert await rag.get_vectors(profile, (chunks[0].id,)) == ()
        assert await rag.get_vectors(SEMANTIC, ()) == ()
        assert await rag.get_vectors(SEMANTIC, (uuid4(), chunks[0].id)) == (entries[0],)
        await rag.save_vectors(())

    asyncio.run(scenario())


def test_vector_conflict_does_not_overwrite_or_partially_save_later_entries() -> None:
    async def scenario() -> None:
        knowledge, chunks = await published_repository()
        rag = InMemoryRagRepository(knowledge)
        original = vector(chunks[0])
        await rag.save_vectors((original,))
        with pytest.raises(GuideImportError, match="vector_conflict"):
            await rag.save_vectors((vector(chunks[1]), replace(original, values=(4.0, 3.0))))
        assert await rag.get_vectors(SEMANTIC, tuple(chunk.id for chunk in chunks)) == (original,)

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["wrong_hash", "missing_chunk", "fixture_scope"])
def test_invalid_batch_is_all_or_none(failure: str) -> None:
    async def scenario() -> None:
        knowledge, chunks = await published_repository()
        rag = InMemoryRagRepository(knowledge)
        invalid = vector(chunks[1])
        code = "vector_content_mismatch"
        match failure:
            case "wrong_hash":
                invalid = replace(invalid, content_hash="0" * 64)
            case "missing_chunk":
                invalid = replace(invalid, chunk_id=uuid4())
                code = "evidence_not_found"
            case _:
                invalid = replace(invalid, profile=DEMO)
                code = "embedding_scope_mismatch"
        with pytest.raises(GuideImportError, match=code):
            await rag.save_vectors((vector(chunks[0]), invalid))
        assert await rag.get_vectors(SEMANTIC, (chunks[0].id, chunks[1].id)) == ()

    asyncio.run(scenario())


def test_draft_formal_documents_cannot_be_indexed_but_demo_can() -> None:
    async def scenario() -> None:
        knowledge = InMemoryKnowledgeRepository()
        report = await GuideIngestionService(knowledge).import_snapshot(
            parse_snapshot(article_snapshot())
        )
        chunk = (await knowledge.list_chunks(report.document_id))[0]
        with pytest.raises(GuideImportError, match="index_document_not_publishable"):
            await InMemoryRagRepository(knowledge).save_vectors((vector(chunk),))
        demo = await create_demo_repository()
        candidates = await InMemoryRagRepository(demo).list_candidates(
            scope(
                game_version="DEMO-ONLY",
                entity="示例任务甲",
                conditions=(("前置任务", "已完成示例前置"),),
                demo_only=True,
            )
        )
        rag = InMemoryRagRepository(demo)
        entry = vector(candidates[0].chunk, DEMO)
        await rag.save_vectors((entry,))
        assert await rag.get_vectors(DEMO, (entry.chunk_id,)) == (entry,)
        with pytest.raises(GuideImportError, match="embedding_scope_mismatch"):
            await rag.save_vectors((vector(candidates[0].chunk),))

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", ["list", "too_many", "duplicate", "not_uuid"])
def test_vector_request_rejects_noncanonical_or_unbounded_parameter_sets(invalid: str) -> None:
    async def scenario() -> None:
        knowledge, _ = await published_repository()
        identities: object = (uuid4(),)
        match invalid:
            case "list":
                identities = [uuid4()]
            case "too_many":
                identities = tuple(uuid4() for _ in range(501))
            case "duplicate":
                identity = uuid4()
                identities = (identity, identity)
            case _:
                identities = ("not-a-uuid",)
        with pytest.raises(GuideImportError, match="invalid_vector_request"):
            await InMemoryRagRepository(knowledge).get_vectors(
                SEMANTIC, cast(tuple[UUID, ...], identities)
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", ["list", "too_many", "duplicate", "not_entry"])
def test_vector_batch_rejects_duplicate_or_unbounded_entries(invalid: str) -> None:
    async def scenario() -> None:
        knowledge, chunks = await published_repository()
        entry = vector(chunks[0])
        entries: object = (entry,)
        match invalid:
            case "list":
                entries = [entry]
            case "too_many":
                entries = (entry,) * 501
            case "duplicate":
                entries = (entry, entry)
            case _:
                entries = ("not-entry",)
        with pytest.raises(GuideImportError, match="invalid_vector_batch"):
            await InMemoryRagRepository(knowledge).save_vectors(
                cast(tuple[VectorEntry, ...], entries)
            )
        assert await InMemoryRagRepository(knowledge).get_vectors(SEMANTIC, (entry.chunk_id,)) == ()

    asyncio.run(scenario())


def test_concurrent_memory_vector_writes_have_one_immutable_winner() -> None:
    async def scenario() -> None:
        knowledge, chunks = await published_repository()
        rag = InMemoryRagRepository(knowledge)
        original = vector(chunks[0])
        alternative = replace(original, values=(1.0, 1.0))
        results = await asyncio.gather(
            rag.save_vectors((original,)), rag.save_vectors((alternative,)), return_exceptions=True
        )
        assert sum(result is None for result in results) == 1
        failed = next(result for result in results if isinstance(result, GuideImportError))
        assert failed.code == "vector_conflict"
        assert (await rag.get_vectors(SEMANTIC, (original.chunk_id,)))[0] in (original, alternative)

    asyncio.run(scenario())


@pytest.mark.parametrize("damage", ["nan", "dimensions", "invalid_profile", "non_tuple"])
def test_repository_revalidates_vectors_if_frozen_constructor_was_bypassed(damage: str) -> None:
    async def scenario() -> None:
        knowledge, chunks = await published_repository()
        rag = InMemoryRagRepository(knowledge)
        damaged = vector(chunks[1])
        match damage:
            case "nan":
                object.__setattr__(damaged, "values", (float("nan"), 1.0))
            case "dimensions":
                object.__setattr__(damaged, "values", (1.0,))
            case "invalid_profile":
                damaged_profile = replace(SEMANTIC)
                object.__setattr__(damaged_profile, "dimensions", True)
                object.__setattr__(damaged, "profile", damaged_profile)
            case _:
                object.__setattr__(damaged, "values", [1.0, 2.0])
        with pytest.raises(GuideImportError, match="invalid_vector_batch"):
            await rag.save_vectors((vector(chunks[0]), damaged))
        assert await rag.get_vectors(SEMANTIC, tuple(chunk.id for chunk in chunks)) == ()

    asyncio.run(scenario())
