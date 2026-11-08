"""真实隔离数据库和原生工具协议模拟链路；不调用供应商，不删除 schema。"""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from test_guide_postgres import isolated_database

from teyvatnotes.application.guide_agent import GuideAgentService
from teyvatnotes.application.guide_ingestion import GuideIngestionService
from teyvatnotes.application.question_preparation import QuestionPreparationService
from teyvatnotes.knowledge.agent_cli import (
    OfflineToolClient,
    agent_to_json,
    tool_session,
)
from teyvatnotes.knowledge.answers import JsonAnswerDecoder
from teyvatnotes.knowledge.questions import load_question
from teyvatnotes.knowledge.snapshots import parse_snapshot
from teyvatnotes.models.responses import ResponsesSettings
from teyvatnotes.models.responses_tools import ResponsesToolClient
from teyvatnotes.repositories.knowledge_postgres import PostgresKnowledgeRepository

BASE_URL = "https://llm.example.test/v1"
MODEL = "example-model-v1"
pytestmark = pytest.mark.postgres
ROOT = Path(__file__).resolve().parents[1]


def test_postgres_tool_loop_with_native_responses_mock() -> None:
    async def scenario() -> None:
        async with isolated_database() as (database, _):
            repository = PostgresKnowledgeRepository(database.session_factory)
            snapshot = json.loads(
                (ROOT / "samples/guides/retrieval_character.json").read_text(encoding="utf-8")
            )
            # 仅在随机测试 schema 构造发布标记，不把永久虚构样本发布到业务库。
            snapshot.update(is_fixture=False, game_version="synthetic-tool-test")
            await GuideIngestionService(repository).import_snapshot(
                parse_snapshot(snapshot), publish=True
            )
            request = replace(
                load_question(ROOT / "samples/questions/character.json"),
                game_version="synthetic-tool-test",
            )
            calls = []

            async def handle(http_request: httpx.Request) -> httpx.Response:
                payload = json.loads(http_request.content)
                calls.append(payload)
                assert payload["model"] == MODEL and payload["store"] is False
                assert payload["parallel_tool_calls"] is False
                items = payload["input"]
                from teyvatnotes.application.model_ports import ModelMessage
                from teyvatnotes.application.tool_model_ports import ToolTurnRequest

                simulated = await OfflineToolClient().generate_turn(
                    ToolTurnRequest(
                        tuple(ModelMessage(item["role"], item["content"]) for item in items[:2]),
                        (),
                        tuple(items[2:]),
                    )
                )
                output = (
                    list(simulated.continuation)
                    if simulated.calls
                    else [
                        {
                            "type": "message",
                            "role": "assistant",
                            "status": "completed",
                            "content": [{"type": "output_text", "text": simulated.text}],
                        }
                    ]
                )
                return httpx.Response(
                    200,
                    json={
                        "status": "completed",
                        "model": MODEL,
                        "output": output,
                        "usage": {"input_tokens": 12, "output_tokens": 6},
                    },
                )

            client = ResponsesToolClient(
                ResponsesSettings("fake-test-key", BASE_URL, MODEL),
                transport=httpx.MockTransport(handle),
            )
            result = await GuideAgentService(
                QuestionPreparationService(repository),
                lambda: client,
                lambda q: tool_session(repository, q, False),
                JsonAnswerDecoder(),
            ).answer(request)
            public = agent_to_json(result, request)
            assert result.answer.status == "answer" and len(calls) == 3
            assert public["input_tokens"] == 36 and public["output_tokens"] == 18
            assert public["model_called"] is True and public["cost"] is None
            for claim in public["claims"]:
                assert all(
                    citation["status"] == "published" and citation["is_fixture"] is False
                    for citation in claim["citations"]
                )

    asyncio.run(scenario())
