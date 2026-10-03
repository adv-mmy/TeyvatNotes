"""问答准备只取证据，不调用模型；缺信息、截断、取消和引用定位回归。"""

import asyncio
from dataclasses import replace
from uuid import UUID

import pytest
from test_knowledge_retrieval import searchable_snapshot

from agent_runtime_foundation.application.guide_ingestion import GuideIngestionService
from agent_runtime_foundation.application.question_preparation import (
    QuestionPreparationService,
    QuestionRequest,
)
from agent_runtime_foundation.domain.guides import GuideImportError, GuideKind
from agent_runtime_foundation.domain.knowledge import (
    EvidenceHit,
    EvidenceScope,
    EvidenceWindow,
    KeywordQuery,
)
from agent_runtime_foundation.knowledge.demo import create_demo_repository
from agent_runtime_foundation.knowledge.evidence_json import hit_to_json, preparation_to_json
from agent_runtime_foundation.knowledge.questions import parse_question
from agent_runtime_foundation.knowledge.snapshots import parse_snapshot
from agent_runtime_foundation.repositories.knowledge_memory import InMemoryKnowledgeRepository
from scripts.guide_retrieval_demo import run_guide_retrieval_demo


def quest_request(**changes: object) -> QuestionRequest:
    return replace(
        QuestionRequest(
            "第二步的证据？",
            GuideKind.QUEST,
            "test-version",
            "示例任务",
            ("第二步",),
            location="测试地点",
            progress="第一步已完成",
        ),
        **changes,
    )


def test_missing_information_does_not_touch_reader() -> None:
    class UnusedReader:
        async def search_knowledge(self, query: KeywordQuery) -> tuple[EvidenceHit, ...]:
            raise AssertionError("缺字段时不能检索")

        async def read_evidence(
            self, chunk_id: UUID, scope: EvidenceScope, *, max_chunks: int = 20
        ) -> EvidenceWindow:
            raise AssertionError("缺字段时不能回读")

    async def scenario() -> None:
        result = await QuestionPreparationService(UnusedReader()).prepare(
            QuestionRequest("怎么解谜？", GuideKind.QUEST)
        )
        assert result.status == "needs_clarification" and len(result.questions) == 5
        assert not result.evidence
        character = await QuestionPreparationService(UnusedReader()).prepare(
            quest_request(guide_kind=GuideKind.CHARACTER, goal=None)
        )
        assert character.status == "needs_clarification" and len(character.questions) == 1

    asyncio.run(scenario())


def test_preparation_has_versioned_citations_and_preserves_quest_prefix() -> None:
    async def scenario() -> None:
        repository = InMemoryKnowledgeRepository()
        await GuideIngestionService(repository).import_snapshot(
            parse_snapshot(searchable_snapshot()), publish=True
        )
        result = await QuestionPreparationService(repository).prepare(quest_request())
        assert result.status == "evidence_ready"
        window = result.evidence[0]
        assert [chunk.ordinal for chunk in window.chunks] == [0, 1, 2]
        public = preparation_to_json(result)
        assert public["model_called"] is False and "answer" not in public
        citation = hit_to_json(window.hit)
        assert citation["game_version"] == "test-version"
        assert citation["citation_url"] == "https://www.miyoushe.com/ys/article/1000000000"
        assert citation["locators"][0]["paragraph_index"] == 3

    asyncio.run(scenario())


def test_no_match_and_truncated_prefix_are_not_evidence_ready() -> None:
    async def scenario() -> None:
        repository = InMemoryKnowledgeRepository()
        service = QuestionPreparationService(repository)
        empty = await service.prepare(quest_request())
        assert empty.status == "insufficient_evidence" and empty.reason == "no_matching_evidence"
        await GuideIngestionService(repository).import_snapshot(
            parse_snapshot(
                searchable_snapshot(
                    article_blocks=[
                        {"kind": "paragraph", "text": f"第{i}步操作。"} for i in range(25)
                    ]
                )
            ),
            publish=True,
        )
        truncated = await service.prepare(quest_request(keywords=("第24步",)))
        assert (
            truncated.status == "insufficient_evidence" and truncated.reason == "context_truncated"
        )
        assert truncated.evidence[0].context_truncated

    asyncio.run(scenario())


def test_demo_has_video_timestamp_and_never_claims_real_answer() -> None:
    async def scenario() -> None:
        report = await run_guide_retrieval_demo()
        assert report["model_called"] is False and report["external_requests"] == 0
        cases = report["cases"]
        assert [case["status"] for case in cases] == [
            "needs_clarification",
            "evidence_ready",
            "evidence_ready",
        ]
        character = cases[1]["evidence"][0]["hit"]
        assert character["citation_url"].endswith("?p=2&t=10")
        assert character["is_fixture"] is True and character["status"] == "draft"
        assert cases[1]["demo_only"] is True and "fictional_demo_only" in cases[1]["warnings"]
        assert len(cases[2]["evidence"][0]["context"]) == 4
        normal = await QuestionPreparationService(await create_demo_repository()).prepare(
            parse_question(
                {
                    "question": "装备？",
                    "guide_kind": "character",
                    "game_version": "DEMO-ONLY",
                    "entity": "示例角色甲",
                    "keywords": ["装备"],
                    "conditions": {"装备": "示例木剑"},
                    "goal": "装备",
                }
            )
        )
        assert normal.status == "insufficient_evidence"

    asyncio.run(scenario())


def test_cancellation_propagates_and_does_not_read_later_evidence() -> None:
    async def scenario() -> None:
        started = asyncio.Event()

        class WaitingRepository(InMemoryKnowledgeRepository):
            async def search_knowledge(self, query: KeywordQuery) -> tuple[EvidenceHit, ...]:
                started.set()
                await asyncio.Event().wait()
                raise AssertionError("取消后不能继续")

        task = asyncio.create_task(
            QuestionPreparationService(WaitingRepository()).prepare(quest_request())
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "value",
    [
        {"question": "x", "api_key": "SECRET"},
        {"question": " "},
        {"question": "x", "keywords": [""]},
        {"question": "x", "conditions": {"装备": 1}},
        {"question": "x", "guide_kind": "execute_python"},
    ],
)
def test_question_input_forbids_unknown_fields_and_invalid_types(value: object) -> None:
    with pytest.raises(GuideImportError, match="invalid_question"):
        parse_question(value)


@pytest.mark.parametrize(
    "changes",
    [{"goal": " "}, {"keywords": []}, {"conditions": []}, {"progress": 3}, {"guide_kind": "quest"}],
)
def test_direct_application_request_has_runtime_validation(changes: dict[str, object]) -> None:
    with pytest.raises(GuideImportError, match="invalid_question"):
        quest_request(**changes)
