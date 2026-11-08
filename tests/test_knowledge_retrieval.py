"""检索、版本隔离、前提条件、原文回读及确定性问答预检。"""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from test_guide_ingestion import article_snapshot

from teyvatnotes.application.guide_answering import evidence_payload
from teyvatnotes.application.guide_ingestion import GuideIngestionService
from teyvatnotes.domain.guides import GuideImportError, GuideKind
from teyvatnotes.domain.knowledge import (
    EvidenceScope,
    KeywordQuery,
    require_context_limit,
)
from teyvatnotes.knowledge.evidence_json import hit_to_json
from teyvatnotes.knowledge.snapshots import parse_snapshot
from teyvatnotes.repositories.knowledge_memory import InMemoryKnowledgeRepository


def scope(**changes: object) -> EvidenceScope:
    values: dict[str, object] = {
        "game_version": "test-version",
        "guide_kind": GuideKind.QUEST,
        "entity": "示例任务",
    }
    values.update(changes)
    return replace(EvidenceScope("test-version", GuideKind.QUEST, "示例任务"), **values)


def searchable_snapshot(**changes: object) -> dict[str, object]:
    return article_snapshot(entities=["示例任务"], **changes)


def test_only_matching_published_documents_are_searchable() -> None:
    async def scenario() -> None:
        repository = InMemoryKnowledgeRepository()
        service = GuideIngestionService(repository)
        data = searchable_snapshot()
        await service.import_snapshot(parse_snapshot(data))
        query = KeywordQuery(scope(), ("第二步",))
        assert await repository.search_knowledge(query) == ()
        await service.import_snapshot(parse_snapshot(data), publish=True)
        hits = await repository.search_knowledge(query)
        assert len(hits) == 1 and hits[0].chunk.ordinal == 2 and hits[0].score == 4
        for other in (
            scope(game_version="other"),
            scope(entity="其他任务"),
            scope(guide_kind=GuideKind.CHARACTER),
        ):
            assert await repository.search_knowledge(KeywordQuery(other, ("第二步",))) == ()
        assert (
            await repository.search_knowledge(KeywordQuery(scope(demo_only=True), ("第二步",)))
            == ()
        )

    asyncio.run(scenario())


def test_all_source_conditions_must_be_confirmed_and_results_are_isolated() -> None:
    async def scenario() -> None:
        repository = InMemoryKnowledgeRepository()
        await GuideIngestionService(repository).import_snapshot(
            parse_snapshot(searchable_snapshot(conditions={"前置任务": "已完成"})), publish=True
        )
        for conditions in ((), (("前置任务", "未完成"),)):
            assert (
                await repository.search_knowledge(
                    KeywordQuery(scope(conditions=conditions), ("步骤",))
                )
                == ()
            )
        query = KeywordQuery(
            scope(conditions=(("前置任务", "已完成"), ("额外条件", "允许"))), ("步骤",)
        )
        first = await repository.search_knowledge(query)
        assert first
        first[0].document.snapshot["conditions"] = {}
        first[0].chunk.locators[0]["paragraph_index"] = -1
        second = await repository.search_knowledge(query)
        assert second[0].document.snapshot["conditions"] == {"前置任务": "已完成"}
        assert second[0].chunk.locators[0]["paragraph_index"] > 0

    asyncio.run(scenario())


@pytest.mark.parametrize("nature", ["released", "preview"])
def test_both_content_natures_are_searchable_and_readable_with_same_scope(nature: str) -> None:
    async def scenario() -> None:
        repository = InMemoryKnowledgeRepository()
        report = await GuideIngestionService(repository).import_snapshot(
            parse_snapshot(searchable_snapshot(content_nature=nature)), publish=True
        )
        hit = (await repository.search_knowledge(KeywordQuery(scope(), ("第二步",))))[0]
        assert hit.document.id == report.document_id
        assert hit.document.snapshot["content_nature"] == nature
        assert (await repository.read_evidence(hit.chunk.id, scope())).hit.chunk == hit.chunk
        assert hit_to_json(hit)["content_nature"] == nature
        payload = evidence_payload((await repository.read_evidence(hit.chunk.id, scope()),))
        assert payload and all(
            isinstance(item, dict) and item["content_nature"] == nature for item in payload
        )
        assert (
            await repository.search_knowledge(KeywordQuery(scope(demo_only=True), ("第二步",)))
            == ()
        )
        assert (
            await repository.search_knowledge(
                KeywordQuery(scope(game_version="other"), ("第二步",))
            )
            == ()
        )

    asyncio.run(scenario())


def test_latest_published_source_version_selected_before_keywords() -> None:
    async def scenario() -> None:
        repository = InMemoryKnowledgeRepository()
        service = GuideIngestionService(repository)
        old = await service.import_snapshot(parse_snapshot(searchable_snapshot()), publish=True)
        old_hit = (await repository.search_knowledge(KeywordQuery(scope(), ("第二步",))))[0]
        changed = searchable_snapshot(
            article_blocks=[{"kind": "paragraph", "text": "新的操作说明。"}]
        )
        draft = await service.import_snapshot(parse_snapshot(changed))
        assert old.document_id != draft.document_id
        assert (await repository.search_knowledge(KeywordQuery(scope(), ("第二步",))))[0] == old_hit
        await service.import_snapshot(parse_snapshot(changed), publish=True)
        assert await repository.search_knowledge(KeywordQuery(scope(), ("第二步",))) == ()
        # 精确旧证据仍可回读，但不会再自动检索到。
        window = await repository.read_evidence(old_hit.chunk.id, scope())
        assert window.hit.document.id == old.document_id

    asyncio.run(scenario())


def test_literal_keywords_deduplicate_case_and_ignore_sql_wildcards() -> None:
    async def scenario() -> None:
        repository = InMemoryKnowledgeRepository()
        await GuideIngestionService(repository).import_snapshot(
            parse_snapshot(
                searchable_snapshot(
                    article_blocks=[
                        {"kind": "paragraph", "text": "Build 使用 50%_ 成本。"},
                        {"kind": "paragraph", "text": "普通步骤。"},
                    ]
                )
            ),
            publish=True,
        )
        query = KeywordQuery(scope(), ("BUILD", "build", "%_"))
        assert query.keywords == ("build", "%_")
        hits = await repository.search_knowledge(query)
        assert len(hits) == 1 and hits[0].score == 8

    asyncio.run(scenario())


def test_quest_prefix_is_ordered_and_truncation_is_explicit() -> None:
    async def scenario() -> None:
        repository = InMemoryKnowledgeRepository()
        data = searchable_snapshot(
            article_blocks=[{"kind": "paragraph", "text": f"第{i}步操作。"} for i in range(25)]
        )
        await GuideIngestionService(repository).import_snapshot(parse_snapshot(data), publish=True)
        hit = (await repository.search_knowledge(KeywordQuery(scope(), ("第24步",))))[0]
        window = await repository.read_evidence(hit.chunk.id, scope())
        assert window.hit.chunk.ordinal == 24
        assert [chunk.ordinal for chunk in window.chunks] == list(range(20))
        assert window.context_truncated
        with pytest.raises(GuideImportError, match="evidence_not_found"):
            await repository.read_evidence(hit.chunk.id, scope(game_version="wrong"))
        with pytest.raises(GuideImportError, match="evidence_not_found"):
            await repository.read_evidence(uuid4(), scope())

    asyncio.run(scenario())


def test_character_reads_only_neighbours_and_demo_never_sees_real_documents() -> None:
    async def scenario() -> None:
        repository = InMemoryKnowledgeRepository()
        service = GuideIngestionService(repository)
        data = searchable_snapshot(guide_kind="character", is_fixture=True)
        report = await service.import_snapshot(parse_snapshot(data))
        normal = scope(guide_kind=GuideKind.CHARACTER)
        demo = replace(normal, demo_only=True)
        assert await repository.search_knowledge(KeywordQuery(normal, ("第一步",))) == ()
        hit = (await repository.search_knowledge(KeywordQuery(demo, ("第一步",))))[0]
        assert hit.document.id == report.document_id
        window = await repository.read_evidence(hit.chunk.id, demo)
        assert [chunk.ordinal for chunk in window.chunks] == [0, 1, 2]
        assert not window.context_truncated
        with pytest.raises(GuideImportError, match="evidence_not_found"):
            await repository.read_evidence(hit.chunk.id, normal)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "changes",
    [
        {"game_version": ""},
        {"game_version": "未知"},
        {"entity": " "},
        {"conditions": (("x", "1"), ("x", "2"))},
    ],
)
def test_scope_requires_explicit_bounded_filters(changes: dict[str, object]) -> None:
    with pytest.raises(GuideImportError, match="invalid_knowledge_query"):
        scope(**changes)


@pytest.mark.parametrize(
    "keywords, limit", [((), 5), ((" ",), 5), (("x",), True), (("x",), 11), (("x",), 1.5)]
)
def test_keyword_query_validates_work_budget(keywords: tuple[str, ...], limit: int) -> None:
    with pytest.raises(GuideImportError, match="invalid_knowledge_query"):
        KeywordQuery(scope(), keywords, limit)


@pytest.mark.parametrize("limit", [True, 1.5, 0, 21, "1"])
def test_context_budget_rejects_coercion(limit: int) -> None:
    with pytest.raises(GuideImportError, match="invalid_knowledge_query"):
        require_context_limit(limit)
