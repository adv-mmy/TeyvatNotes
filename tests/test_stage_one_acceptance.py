"""第八周演示脚本和并发基线验收。"""

import asyncio
import json

import pytest

from scripts.concurrency_baseline import run_concurrency_baseline
from scripts.stage_one_demo import run_stage_one_demo


def test_concurrency_baseline_converges_without_a_flaky_speed_threshold() -> None:
    result = asyncio.run(
        run_concurrency_baseline(
            task_count=40,
            max_concurrency=8,
        )
    )

    assert result.task_count == 40
    assert result.max_concurrency == 8
    assert result.peak_concurrency == 8
    assert result.succeeded == 40
    assert result.duration_seconds > 0
    assert result.throughput_per_second > 0


@pytest.mark.parametrize(
    ("task_count", "max_concurrency", "message"),
    [
        (0, 1, "task_count"),
        (1, 0, "max_concurrency"),
    ],
)
def test_concurrency_baseline_rejects_invalid_scale(
    task_count: int,
    max_concurrency: int,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        asyncio.run(
            run_concurrency_baseline(
                task_count=task_count,
                max_concurrency=max_concurrency,
            )
        )


def test_stage_one_demo_covers_all_seven_scenarios() -> None:
    report = run_stage_one_demo(emit_logs=False)

    assert report.normal_status == "succeeded"
    assert report.normal_total == 6
    assert report.idempotent_same_task is True
    assert report.cancelled_status == "cancelled"
    assert report.timeout_attempt_outcomes == ("retry_scheduled", "succeeded")
    assert report.crash_recovery_attempt_outcomes == ("lease_expired", "succeeded")
    assert report.crash_recovery_worker_ids == (
        "worker-crashed",
        "worker-replacement",
    )
    assert report.crash_status_before_recovery == "running"
    assert report.crash_attempt_outcome_before_recovery == "running"
    assert report.crash_metrics_completed_before_recovery == 0
    assert report.normal_task_id
    assert report.normal_request_id
    assert report.sse_event_ids == tuple(sorted(report.sse_event_ids))
    assert set(report.sse_event_types) >= {
        "task_created",
        "status_changed",
        "task_progress",
    }
    assert report.sse_correlation_observed is True
    assert report.log_record_count > 0
    assert report.log_correlation_observed is True
    assert report.crash_abandoned_log_observed is True
    assert report.false_crash_completion_absent is True
    assert report.secret_absent_from_logs is True


def test_stage_one_demo_logs_are_json_correlated_and_secret_free(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    known_api_key = "stage-one-test-key-32-characters-long"
    monkeypatch.setattr(
        "scripts.stage_one_demo.secrets.token_urlsafe",
        lambda _: known_api_key,
    )

    report = run_stage_one_demo(emit_logs=True)
    log_text = capsys.readouterr().out
    records = [json.loads(line) for line in log_text.splitlines() if line.strip()]

    assert records
    assert all(isinstance(record, dict) for record in records)
    assert any(
        record.get("request_id") == report.normal_request_id
        and record.get("task_id") == report.normal_task_id
        and isinstance(record.get("attempt_id"), str)
        and isinstance(record.get("worker_id"), str)
        for record in records
    )
    assert any(
        record.get("worker_id") == "worker-crashed" and record.get("event") == "task_abandoned"
        for record in records
    )
    assert not any(
        record.get("worker_id") == "worker-crashed"
        and record.get("event") == "task_completed"
        and record.get("task_status") == "cancelled"
        for record in records
    )
    assert known_api_key not in log_text
    assert "Authorization" not in log_text
    assert "Bearer " not in log_text
