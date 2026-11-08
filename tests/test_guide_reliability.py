"""确定性故障演示，不用真实数据库、模型服务或固定sleep证明并发。"""

import asyncio
import json

import pytest

from teyvatnotes.knowledge import agent_cli, evaluation_cli
from teyvatnotes.knowledge.reliability import (
    cancellation_probe,
    checkpoint_probe,
    run_reliability,
)


def test_checkpoint_resume_reuses_parse_then_every_completed_step() -> None:
    result = asyncio.run(checkpoint_probe())
    assert result["passed"] is True and result["parse_calls"] == 1
    assert result["recovered_steps"] == ["parse"]
    assert result["third_import_reused"] == ["parse", "chunk", "validate"]


def test_budget_timeout_cancellation_and_event_probes_are_all_actual_assertions() -> None:
    report = asyncio.run(run_reliability())
    assert report["status"] == "ok" and len(report["checks"]) == 9
    rows = {row["name"]: row for row in report["checks"]}
    assert all(row["passed"] is True for row in rows.values())
    assert rows["unknown_usage_still_bounded"]["model_attempts"] == 4
    assert rows["unknown_usage_still_bounded"]["tool_attempts"] == 3
    assert rows["model_timeout"]["error"] == "agent_timeout"
    assert rows["cooperative_cancellation"]["provider_stop_or_zero_cost_proven"] is False
    assert len(rows["answer_metadata_events"]["events"]) == 12
    assert report["database_verified"] is False and report["cost"] is None
    assert "private-demo" not in json.dumps(report) and "encrypted_content" not in json.dumps(
        report
    )


def test_cancellation_probe_records_one_attempt_without_automatic_replay() -> None:
    result = asyncio.run(cancellation_probe())
    assert result["passed"] is True and result["attempts"] == 1
    assert result["events"][-1]["kind"] == "answer_cancelled"


def test_reliability_cli_is_offline_and_reports_zero_model_network(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DATABASE_URL", "PRIVATE")
    monkeypatch.setenv("LLM_API_KEY", "PRIVATE")

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("不能使用部署配置")

    monkeypatch.setattr(agent_cli, "create_database", forbidden)
    monkeypatch.setattr(agent_cli, "configured_tool_client", forbidden)
    assert evaluation_cli.main(["reliability"]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["model_called"] is False and "PRIVATE" not in output
