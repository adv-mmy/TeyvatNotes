"""模型提取CLI衔接、预检与提取期间竞争；数据库和模型均为注入替身。"""

import argparse
import asyncio
import json

import pytest
from test_conversation_submissions import CONTEXT, TEXT
from test_conversation_submissions import submission_rig as submission_rig

from teyvatnotes.application.model_budget import GenerationLedger
from teyvatnotes.application.question_understanding import UnderstandingCancelled
from teyvatnotes.knowledge import conversation_cli as cli


@pytest.fixture
def rig(submission_rig, monkeypatch: pytest.MonkeyPatch):
    value = submission_rig
    value.dispose_error = False

    class Database:
        session_factory = object()

        async def dispose(self):
            if value.dispose_error:
                raise RuntimeError("PRIVATE_DATABASE_DETAILS")

    monkeypatch.setenv("GUIDE_SESSION_OWNER_ID", str(value.owner))
    monkeypatch.setenv("DATABASE_URL", "fake-only-no-network")
    monkeypatch.setattr(cli, "create_database", lambda url: Database())
    monkeypatch.setattr(cli, "PostgresConversationRepository", lambda factory: value.repo)
    monkeypatch.setattr(cli, "PostgresKnowledgeRepository", lambda factory: object())
    monkeypatch.setattr(cli, "GuideConversationAnswerer", lambda reader: value.answerer)
    monkeypatch.setattr(cli, "configured_client", value.model_factory)
    return value


def command(capsys, args):
    code = cli.main(args)
    return code, json.loads(capsys.readouterr().out)


def ask(capsys, state):
    return command(
        capsys,
        [
            "ask",
            str(state.id),
            "--revision",
            str(state.revision),
            "--text",
            TEXT,
            "--extract-model",
        ],
    )


def test_explicit_model_create_calls_only_extractor(rig, capsys):
    code, result = command(capsys, ["create", "--text", TEXT, "--extract-model"])
    assert code == 0 and result["revision"] == 0
    assert rig.model_calls == 1 and rig.answer_calls == 0
    assert result["understanding"]["status"] == "needs_clarification"
    assert result["round_usage"]["generation_attempts"] == 1


def test_ready_history_reports_total_and_stage_usage(rig, capsys):
    state = asyncio.run(rig.service.create(CONTEXT))
    code, result = ask(capsys, state)
    assert code == 0 and rig.model_calls == 1 and rig.answer_calls == 1
    assert result["latest_reply"]["generation_attempts"] == 2
    assert result["latest_reply"]["input_tokens"] == 19
    assert result["round_usage"]["answer"]["generation_attempts"] == 1
    assert result["latest_reply"]["round_usage"]["output_tokens"] == 7


def test_dispose_failure_does_not_erase_actual_calls(rig, capsys):
    state = asyncio.run(rig.service.create(CONTEXT))
    rig.dispose_error = True
    code, result = ask(capsys, state)
    assert code == 1 and result["error"] == "conversation_unavailable"
    assert result["round_usage"]["generation_attempts"] == 2
    assert result["round_usage"]["input_tokens"] == 19
    assert "PRIVATE" not in json.dumps(result)
    assert asyncio.run(rig.service.get(state.id)).latest_reply["status"] == "answer"


def test_json_input_cannot_enable_model_extraction(rig, capsys):
    with pytest.raises(SystemExit) as caught:
        command(capsys, ["create", "not-read.json", "--extract-model"])
    assert caught.value.code == 2 and rig.factory_calls == 0


def test_standalone_model_understand_needs_no_database_or_owner(rig, capsys, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("GUIDE_SESSION_OWNER_ID", raising=False)

    def forbidden(url):
        raise AssertionError("不能连接数据库")

    monkeypatch.setattr(cli, "create_database", forbidden)
    code, result = command(capsys, ["understand", TEXT, "--extract-model"])
    assert code == 0 and result["status"] == "needs_clarification"
    assert rig.model_calls == 1 and rig.answer_calls == 0
    assert result["round_usage"]["generation_attempts"] == 1


def test_dispose_error_during_extraction_cancel_does_not_mask_cancel(rig):
    async def scenario():
        state = await rig.service.create(CONTEXT)
        entered = asyncio.Event()

        async def block():
            entered.set()
            await asyncio.Event().wait()

        rig.hook, rig.dispose_error = block, True
        arguments = argparse.Namespace(
            command="ask", id=str(state.id), revision=0, path=None, text=TEXT, extract_model=True
        )
        ledger = GenerationLedger()
        task = asyncio.create_task(cli.run_command(arguments, generation=ledger))
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(UnderstandingCancelled) as caught:
            await task
        assert caught.value.result.generation_attempts == 1
        assert ledger.to_json()["generation_attempts"] == 1
        assert rig.answer_calls == 0
        assert (await rig.service.get(state.id)).revision == 0

    asyncio.run(scenario())
