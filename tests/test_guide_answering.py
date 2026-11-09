"""单轮回答的先检索后计费、严格引用、资源限制、失败用量与离线 CLI。"""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from teyvatnotes.application.guide_answering import AnswerResult, GuideAnswerService
from teyvatnotes.application.model_ports import (
    ModelClient,
    ModelError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from teyvatnotes.application.question_preparation import (
    QuestionPreparationService,
    QuestionRequest,
)
from teyvatnotes.domain.guides import GuideImportError
from teyvatnotes.knowledge.answer_cli import OfflineDemoClient, main
from teyvatnotes.knowledge.answers import JsonAnswerDecoder, answer_to_json
from teyvatnotes.knowledge.demo import create_demo_repository
from teyvatnotes.knowledge.questions import load_question, parse_question

ROOT = Path(__file__).resolve().parents[1]


class FakeClient:
    """先取得合法摘录，再注入单一错误观察程序是否拒绝；不访问外网。"""

    def __init__(self, problem: str | None = None) -> None:
        self.calls = 0
        self.problem = problem

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        response = await OfflineDemoClient().generate(request)
        body = json.loads(response.text)
        claim = body["claims"][0]
        if self.problem == "citation":
            claim["evidence_ids"] = [str(uuid4())]
        elif self.problem == "equipment":
            claim["required_equipment"] = ["未拥有的装备"]
        elif self.problem == "character":
            claim["required_characters"] = ["未拥有的角色"]
        elif self.problem == "url":
            claim["url"] = "https://evil.test"
        elif self.problem == "completion":
            body["claims"] = [item for item in body["claims"] if item["section"] != "completion"]
        elif self.problem == "order":
            body["claims"].reverse()
        elif self.problem == "conflict":
            body["conflicts"] = ["资料冲突"]
        elif self.problem == "extra":
            body["shell_command"] = "不允许"
        text = json.dumps(body, ensure_ascii=False)
        if self.problem == "fence":
            text = "```json\n" + text + "\n```"
        return ModelResponse(text, "fake-test", ModelUsage(50, 25))


async def answer_with(
    client: FakeClient, name: str = "character", **changes: object
) -> tuple[AnswerResult, QuestionRequest]:
    reader = await create_demo_repository()
    request = replace(load_question(ROOT / f"samples/questions/{name}.json"), **changes)
    service = GuideAnswerService(
        QuestionPreparationService(reader, demo_only=True),
        lambda: client,
        JsonAnswerDecoder(),
        demo_only=True,
    )
    return await service.answer(request), request


def test_cited_answer_uses_stored_links_and_known_token_usage() -> None:
    async def scenario() -> None:
        client = FakeClient()
        result, request = await answer_with(client)
        public = answer_to_json(result, request)
        assert result.status == "answer" and client.calls == 1
        assert public["input_tokens"] == 50 and public["cost"] is None
        links = [c["citation_url"] for claim in public["claims"] for c in claim["citations"]]
        assert all(
            link.startswith("https://www.bilibili.com/video/BV1DEMO00002?p=2&t=") for link in links
        )
        assert "citation_support_not_verified" in public["warnings"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "changes,status",
    [
        ({"game_version": None}, "needs_clarification"),
        ({"keywords": ("没有这种词",)}, "insufficient_evidence"),
        ({"owned_equipment": None}, "needs_clarification"),
    ],
)
def test_no_model_or_credential_factory_when_not_ready(
    changes: dict[str, object], status: str
) -> None:
    async def scenario() -> None:
        reader = await create_demo_repository()
        request = replace(load_question(ROOT / "samples/questions/character.json"), **changes)

        def forbidden() -> ModelClient:
            raise AssertionError("不能读取密钥或创建模型")

        result = await GuideAnswerService(
            QuestionPreparationService(reader, demo_only=True), forbidden, JsonAnswerDecoder()
        ).answer(request)
        assert result.status == status and not result.model_called

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "problem,code",
    [
        ("citation", "invalid_answer_citation"),
        ("equipment", "answer_equipment_not_owned"),
        ("character", "answer_character_not_owned"),
        ("url", "invalid_answer_structure"),
        ("fence", "invalid_answer_structure"),
        ("extra", "invalid_answer_structure"),
        ("conflict", "invalid_answer_structure"),
    ],
)
def test_invalid_answer_is_not_returned_or_retried(problem: str, code: str) -> None:
    async def scenario() -> None:
        client = FakeClient(problem)
        result, request = await answer_with(client)
        public = answer_to_json(result, request)
        assert result.status == "error" and result.reason == code
        assert not result.claims and client.calls == 1 and result.model_called
        assert public["input_tokens"] == 50 and public["output_tokens"] == 25

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "problem,code",
    [
        ("completion", "incomplete_quest_answer"),
        ("order", "invalid_quest_order"),
    ],
)
def test_quest_structure_and_order_checked(problem: str, code: str) -> None:
    async def scenario() -> None:
        result, _ = await answer_with(FakeClient(problem), "quest")
        assert result.status == "error" and result.reason == code

    asyncio.run(scenario())


def test_answer_cancellation_reaches_model_and_does_not_retry() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()

        class WaitingClient:
            async def generate(self, request: ModelRequest) -> ModelResponse:
                entered.set()
                await asyncio.Event().wait()
                raise AssertionError("取消后不应继续")

        reader = await create_demo_repository()
        service = GuideAnswerService(
            QuestionPreparationService(reader, demo_only=True), WaitingClient, JsonAnswerDecoder()
        )
        task = asyncio.create_task(
            service.answer(load_question(ROOT / "samples/questions/quest.json"))
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "sample,status",
    [
        ("character", "answer"),
        ("quest", "answer"),
        ("clarification", "needs_clarification"),
    ],
)
def test_demo_cli_is_explicitly_simulated_and_never_reads_credentials(
    sample: str,
    status: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def forbidden() -> ModelClient:
        raise AssertionError("演示不得读取服务商配置")

    monkeypatch.setattr("teyvatnotes.knowledge.answer_cli.configured_client", forbidden)
    assert main([str(ROOT / f"samples/questions/{sample}.json"), "--demo"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == status and result["model_called"] is False
    assert result["provider"] == "offline_demo" and result["demo_only"] is True
    assert "simulated_model_no_network" in result["warnings"]


def test_normal_cli_no_test_database_or_demo_fallback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("GUIDE_DATABASE_URL", raising=False)
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+asyncpg://PRIVATE")
    assert main([str(ROOT / "samples/questions/quest.json")]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "error": "database_not_configured",
    }


@pytest.mark.parametrize("inventory", [[1], ["x", "x"], [" "], "x"])
def test_inventory_is_strict_and_bounded(inventory: object) -> None:
    with pytest.raises(GuideImportError, match="invalid_question"):
        parse_question({"question": "x", "owned_equipment": inventory})


def test_insufficient_evidence_json_does_not_contain_claims() -> None:
    result = JsonAnswerDecoder().decode(
        json.dumps(
            {
                "status": "insufficient_evidence",
                "claims": [],
                "questions": [],
                "gaps": ["没有足够证据"],
                "conflicts": [],
            }
        )
    )
    assert not result.claims and result.gaps


def test_direct_inventory_unhashable_value_is_rejected() -> None:
    with pytest.raises(GuideImportError, match="invalid_question"):
        replace(load_question(ROOT / "samples/questions/character.json"), owned_equipment=([],))


def test_model_failure_keeps_call_attempt_and_unknown_usage() -> None:
    async def scenario() -> None:
        class FailedClient:
            async def generate(self, request: ModelRequest) -> ModelResponse:
                raise ModelError("model_unavailable")

        reader = await create_demo_repository()
        request = load_question(ROOT / "samples/questions/quest.json")
        service = GuideAnswerService(
            QuestionPreparationService(reader, demo_only=True), FailedClient, JsonAnswerDecoder()
        )
        result = await service.answer(request)
        assert result.status == "error" and result.reason == "model_unavailable"
        assert result.model_called and result.response is None
        assert answer_to_json(result, request)["input_tokens"] is None

    asyncio.run(scenario())


def test_use_case_total_timeout_includes_retrieval(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        class WaitingPreparation:
            async def prepare(self, request: QuestionRequest):
                await asyncio.Event().wait()

        reader = await create_demo_repository()
        preparation = QuestionPreparationService(reader, demo_only=True)
        monkeypatch.setattr(preparation, "prepare", WaitingPreparation().prepare)
        monkeypatch.setattr("teyvatnotes.application.guide_answering.ANSWER_TIMEOUT_SECONDS", 0.01)
        client = FakeClient()
        result = await GuideAnswerService(preparation, lambda: client, JsonAnswerDecoder()).answer(
            load_question(ROOT / "samples/questions/quest.json")
        )
        assert result.status == "error" and result.reason == "answer_timeout"
        assert not result.model_called and client.calls == 0

    asyncio.run(scenario())
