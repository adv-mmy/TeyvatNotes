"""服务端配置、模拟 Responses 输出与桌面消费的完整契约；不访问模型平台。"""

import asyncio
import json
import logging
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from test_guide_postgres import isolated_database

from teyvatnotes.api import create_app
from teyvatnotes.application.conversations import ConversationError
from teyvatnotes.application.guide_ingestion import GuideIngestionService
from teyvatnotes.config import GuideSettings
from teyvatnotes.desktop.client import RemoteGuideAnswerer
from teyvatnotes.desktop.config import load_config, save_config
from teyvatnotes.guide_protocol import GUIDE_OUTPUT_ERRORS
from teyvatnotes.knowledge.answers import demo_answer_json
from teyvatnotes.knowledge.questions import load_question
from teyvatnotes.knowledge.snapshots import parse_snapshot
from teyvatnotes.models.responses import ResponsesClient, ResponsesSettings
from teyvatnotes.repositories.knowledge_postgres import PostgresKnowledgeRepository

ROOT = Path(__file__).resolve().parents[1]
ENV = {"GUIDE_DATABASE_URL": "postgresql://example@localhost/test", "GUIDE_IP_HASH_KEY": "x" * 32}
KEY = "GUIDE_ANSWER_MAX_OUTPUT_TOKENS"


def test_answer_budget_configuration() -> None:
    assert GuideSettings.from_env(ENV).answer_max_output_tokens == 8192
    for limit in (256, 4096, 8192, 16384):
        assert GuideSettings.from_env({**ENV, KEY: str(limit)}).answer_max_output_tokens == limit
    for invalid in ("", "true", "0", "255", "16385", "5.5", "invalid"):
        with pytest.raises(ValueError, match=KEY):
            GuideSettings.from_env({**ENV, KEY: invalid})


def test_deployment_mode_defaults_to_public_and_requires_a_known_value() -> None:
    assert GuideSettings.from_env(ENV).deployment_mode == "public"
    assert (
        GuideSettings.from_env({**ENV, "GUIDE_DEPLOYMENT_MODE": "local"}).deployment_mode == "local"
    )
    with pytest.raises(ValueError, match="GUIDE_DEPLOYMENT_MODE"):
        GuideSettings.from_env({**ENV, "GUIDE_DEPLOYMENT_MODE": "unlimited"})


@pytest.mark.postgres
@pytest.mark.parametrize(
    "limit,case,expected",
    [
        (8192, "limit", "answer_output_limit_reached"),
        (4096, "complete", "answer"),
        (12288, "other_incomplete", "model_incomplete"),
        (8192, "invalid_json", "invalid_answer_structure"),
        (8192, "missing", "invalid_answer_structure"),
        (8192, "wrong_type", "invalid_answer_structure"),
        (8192, "conflict", "invalid_answer_structure"),
        (8192, "unknown_key", "invalid_answer_structure"),
        (8192, "gap", "insufficient_evidence"),
        (8192, "conflicts_only", "insufficient_evidence"),
        (8192, "bad_citation", "invalid_answer_citation"),
        (8192, "timeout", "model_timeout"),
    ],
)
def test_responses_to_desktop_with_no_replay(
    tmp_path: Path,
    limit: int,
    case: str,
    expected: str,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sent_text: str | None = None
    logfile = tmp_path / "server" / "guide-errors.jsonl"
    private = "PRIVATE_UPSTREAM_CONTENT_DO_NOT_LOG"
    details = {
        "invalid_json": "json_invalid",
        "missing": "missing_field:claims[0].required_equipment",
        "wrong_type": "invalid_type:claims[0].required_equipment",
        "conflict": "status_conflict rule=answer_forbids_questions answer_status=answer "
        "claims_count=1 questions_count=1 gaps_count=0 conflicts_count=0",
        "unknown_key": "unexpected_field",
    }

    class ObservedAPI(httpx.ASGITransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            response = await super().handle_async_request(request)
            if request.url.path == "/v1/guides/answer":
                assert response.status_code == (
                    200
                    if expected in {"answer", "insufficient_evidence"}
                    else 504
                    if case == "timeout"
                    else 502
                )
                request_id = response.headers["x-request-id"]
                entries = [
                    r.getMessage()
                    for r in caplog.records
                    if r.name == "teyvatnotes.guides"
                    and f"request_id={request_id} " in r.getMessage()
                ]
                assert len(entries) == 1
                assert "path=local " in entries[0]
                assert entries[0].endswith("validation_detail=" + details.get(case, "None"))
                assert private not in caplog.text
                assert "示例木剑" not in caplog.text
                raw = await response.aread()
                assert b"validation_detail" not in raw
                lines = logfile.read_text(encoding="utf-8").splitlines()
                if response.status_code >= 400:
                    assert json.loads(raw) == {
                        "code": expected,
                        "message": expected,
                        "request_id": request_id,
                    }
                    assert len(lines) == 1
                    event = json.loads(lines[0])
                    assert datetime.fromisoformat(event.pop("time")).utcoffset() is not None
                    elapsed = event.pop("elapsed_ms")
                    assert f"elapsed_ms={elapsed} " in entries[0]
                    assert event == {
                        "request_id": request_id,
                        "status": response.status_code,
                        "path": "local",
                        "error_code": expected,
                        "validation_detail": details.get(case),
                        "model_output": sent_text if sent_text is not None else "未收到模型输出",
                    }
                    assert "SECRET_REASONING_MARKER" not in lines[0]
                else:
                    assert lines == []
            return response

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal calls, sent_text
        calls += 1
        if case == "timeout":
            raise httpx.ReadTimeout("模拟超时")
        body = json.loads(request.content)
        assert body["max_output_tokens"] == limit
        usage = {"input_tokens": 80, "output_tokens": limit if case == "limit" else 4096}
        if case in {"limit", "other_incomplete"}:
            reason = "max_output_tokens" if case == "limit" else "content_filter"
            return httpx.Response(
                200,
                json={
                    "status": "incomplete",
                    "incomplete_details": {"reason": reason},
                    "usage": usage,
                },
            )
        prompt = json.loads(body["input"][-1]["content"])
        answer = demo_answer_json(prompt["player"], prompt["evidence"])
        if case == "invalid_json":
            answer = "```json\r\n" + private + "这是没有闭合的中文正文" * 1000 + "\n```"
        if case in {"gap", "conflicts_only"}:
            answer = json.dumps(
                {
                    "status": "insufficient_evidence",
                    "claims": [],
                    "questions": [],
                    "gaps": ["缺少配队对照资料"] if case == "gap" else [],
                    "conflicts": ["已读资料互相矛盾"] if case == "conflicts_only" else [],
                }
            )
        if case in {"missing", "wrong_type", "conflict", "unknown_key"}:
            data = json.loads(answer)
            data["claims"][0]["text"] = private
            if case == "missing":
                del data["claims"][0]["required_equipment"]
            elif case == "wrong_type":
                data["claims"][0]["required_equipment"] = private
            elif case == "unknown_key":
                data["claims"][0][private + "\nFORGED_LOG"] = private
            else:
                data["claims"] = data["claims"][:1]
                data["questions"] = ["是否还需要这份攻略？\n第二行中文与引号：“示例”"]
            answer = "  \r\n" + json.dumps(data, ensure_ascii=False, indent=2) + "\n "
        if case == "bad_citation":
            decoded = json.loads(answer)
            decoded["claims"][0]["evidence_ids"] = ["00000000-0000-0000-0000-000000000000"]
            answer = json.dumps(decoded)
        sent_text = answer
        return httpx.Response(
            200,
            json={
                "model": "mock",
                "status": "completed",
                "usage": usage,
                "output": [
                    {"type": "reasoning", "summary": "SECRET_REASONING_MARKER"},
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": answer}],
                    },
                ],
            },
        )

    async def run() -> None:
        async with isolated_database() as (db, _):
            # 测试进程内执行的 Alembic fileConfig 会重置日志；实际部署是独立进程。
            logger = logging.getLogger("teyvatnotes.guides")
            monkeypatch.setattr(logger, "disabled", False)
            monkeypatch.setattr(logger, "handlers", [caplog.handler])
            monkeypatch.setattr(logger, "propagate", False)
            caplog.set_level(logging.INFO, logger=logger.name)
            snapshot = json.loads((ROOT / "samples/guides/retrieval_character.json").read_bytes())
            snapshot.update(is_fixture=False, game_version="synthetic-test-only")
            await GuideIngestionService(
                PostgresKnowledgeRepository(db.session_factory)
            ).import_snapshot(parse_snapshot(snapshot), publish=True)
            model = ResponsesClient(
                ResponsesSettings("test-only", "https://model.invalid", "mock"),
                transport=httpx.MockTransport(upstream),
            )
            app = create_app(
                GuideSettings.from_env({**ENV, KEY: str(limit)}),
                database=db,
                model_factory=lambda: model,
                diagnostic_dir=logfile.parent,
            )
            save_config(load_config(tmp_path / "client.toml"), "https://local.test")
            desktop = RemoteGuideAnswerer(tmp_path, ObservedAPI(app=app))
            question = replace(
                load_question(ROOT / "samples/questions/character.json"),
                game_version="synthetic-test-only",
            )
            async with app.router.lifespan_context(app):
                assert str(logfile.resolve()) in caplog.text
                if expected == "answer":
                    reply = await desktop.answer(question, demo_only=False)
                    assert reply["status"] == "answer" and reply["output_tokens"] == 4096
                    assert reply["claims"][0]["citations"][0]["source_url"]
                elif expected == "insufficient_evidence":
                    reply = await desktop.answer(question, demo_only=False)
                    assert reply["status"] == expected and not reply["claims"]
                    assert reply["gaps"] == (["缺少配队对照资料"] if case == "gap" else [])
                    assert reply["conflicts"] == (
                        ["已读资料互相矛盾"] if case == "conflicts_only" else []
                    )
                else:
                    with pytest.raises(ConversationError) as error:
                        await desktop.answer(question, demo_only=False)
                    assert error.value.code == expected
                    if case == "limit":
                        assert (
                            GUIDE_OUTPUT_ERRORS[expected] == "回答达到输出上限，未能生成完整攻略。"
                        )
                assert calls == 1

    asyncio.run(run())
