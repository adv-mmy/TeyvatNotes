"""真实 PostgreSQL→证据准备→模拟答案链路，资料和模型均为测试构造。"""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_guide_answering import FakeClient
from test_guide_postgres import isolated_database

from teyvatnotes.application.guide_answering import GuideAnswerService
from teyvatnotes.application.guide_ingestion import GuideIngestionService
from teyvatnotes.application.question_preparation import QuestionPreparationService
from teyvatnotes.knowledge.answers import JsonAnswerDecoder, answer_to_json
from teyvatnotes.knowledge.questions import load_question
from teyvatnotes.knowledge.snapshots import parse_snapshot
from teyvatnotes.repositories.knowledge_postgres import PostgresKnowledgeRepository

pytestmark = pytest.mark.postgres
ROOT = Path(__file__).resolve().parents[1]


def test_published_postgres_evidence_builds_citations_without_real_model() -> None:
    async def scenario() -> None:
        async with isolated_database() as (database, _):
            repository = PostgresKnowledgeRepository(database.session_factory)
            # 仅在随机隔离测试 schema 模拟正式标记；不发布/冒充真实攻略资料。
            snapshot = json.loads(
                (ROOT / "samples/guides/retrieval_character.json").read_text(encoding="utf-8")
            )
            snapshot.update(is_fixture=False, game_version="synthetic-test-only")
            await GuideIngestionService(repository).import_snapshot(
                parse_snapshot(snapshot), publish=True
            )
            request = replace(
                load_question(ROOT / "samples/questions/character.json"),
                game_version="synthetic-test-only",
            )
            client = FakeClient()
            result = await GuideAnswerService(
                QuestionPreparationService(repository), lambda: client, JsonAnswerDecoder()
            ).answer(request)
            assert result.status == "answer" and client.calls == 1 and not result.demo_only
            public = answer_to_json(result, request)
            for claim in public["claims"]:
                assert all(
                    citation["status"] == "published" and citation["is_fixture"] is False
                    for citation in claim["citations"]
                )
            assert public["input_tokens"] == 50 and public["cost"] is None

    asyncio.run(scenario())
