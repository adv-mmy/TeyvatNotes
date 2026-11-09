"""显式注入 RAG reader 的离线问答衔接，不改变旧关键词预检默认行为。"""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from teyvatnotes.application.guide_answering import GuideAnswerService
from teyvatnotes.application.question_preparation import QuestionPreparationService
from teyvatnotes.application.rag_service import RagKnowledgeReader
from teyvatnotes.domain.rag import RetrievalMode
from teyvatnotes.knowledge.answer_cli import OfflineDemoClient
from teyvatnotes.knowledge.answers import JsonAnswerDecoder, answer_to_json
from teyvatnotes.knowledge.questions import load_question
from teyvatnotes.knowledge.rag_evaluation import create_indexed_demo

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", ["character", "quest"])
@pytest.mark.parametrize("mode", [RetrievalMode.VECTOR, RetrievalMode.HYBRID])
def test_explicit_rag_reader_can_prepare_and_cite_without_literal_keyword_match(
    name: str, mode: RetrievalMode
) -> None:
    async def scenario() -> None:
        knowledge, _, search = await create_indexed_demo()
        request = replace(
            load_question(ROOT / f"samples/questions/{name}.json"),
            keywords=("不存在的字面词",),
        )
        old = await QuestionPreparationService(knowledge, demo_only=True).prepare(request)
        assert old.status == "insufficient_evidence"
        reader = RagKnowledgeReader(search, knowledge, question_text=request.question, mode=mode)
        service = GuideAnswerService(
            QuestionPreparationService(reader, demo_only=True),
            OfflineDemoClient,
            JsonAnswerDecoder(),
            demo_only=True,
        )
        result = await service.answer(request)
        assert result.status == "answer" and result.claims and result.evidence
        public = answer_to_json(result, request, simulated_model=True)
        assert public["model_called"] is False and public["cost"] is None
        assert "citation_support_not_verified" in public["warnings"]
        allowed = {str(chunk.id) for window in result.evidence for chunk in window.chunks}
        assert all(set(claim.evidence_ids) <= allowed for claim in result.claims)
        if name == "quest":
            assert {claim.section for claim in result.claims} >= {
                "prerequisite",
                "step",
                "completion",
            }

    asyncio.run(scenario())


def test_adapter_does_not_bypass_missing_player_fields_or_required_keyword() -> None:
    async def scenario() -> None:
        knowledge, _, search = await create_indexed_demo()
        request = replace(load_question(ROOT / "samples/questions/character.json"), keywords=())
        reader = RagKnowledgeReader(search, knowledge, question_text=request.question)
        prepared = await QuestionPreparationService(reader, demo_only=True).prepare(request)
        assert prepared.status == "needs_clarification" and not prepared.evidence

    asyncio.run(scenario())
