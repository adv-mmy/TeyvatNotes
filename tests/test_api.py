"""任务 API 验收测试。"""

import asyncio
import logging
import time
from collections.abc import Coroutine
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_runtime_foundation.api import create_app
from agent_runtime_foundation.domain import Task, TaskStatus, TaskType
from agent_runtime_foundation.repositories import InMemoryTaskRepository

PATHS_FROM_QUEUED = {
    TaskStatus.QUEUED: (),
    TaskStatus.RUNNING: (TaskStatus.RUNNING,),
    TaskStatus.SUCCEEDED: (TaskStatus.RUNNING, TaskStatus.SUCCEEDED),
    TaskStatus.CANCEL_REQUESTED: (TaskStatus.CANCEL_REQUESTED,),
    TaskStatus.CANCELLED: (
        TaskStatus.CANCEL_REQUESTED,
        TaskStatus.CANCELLED,
    ),
    TaskStatus.FAILED: (TaskStatus.RUNNING, TaskStatus.FAILED),
}


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


def _task_in_status(status: TaskStatus) -> Task:
    task = Task(task_type=TaskType.SUM_NUMBERS, payload={"numbers": [1]})
    for target_status in PATHS_FROM_QUEUED[status]:
        task.transition_to(target_status)
    return task


def _wait_for_api_status(
    client: TestClient,
    path: str,
    expected_status: str,
    *,
    timeout: float = 1.0,
) -> httpx.Response:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(path)
        if response.json()["status"] == expected_status:
            return response
        time.sleep(0.001)
    raise AssertionError(f"任务未在 {timeout} 秒内进入 {expected_status}")


def test_create_task_returns_201(client: TestClient) -> None:
    response = client.post(
        "/v1/tasks",
        json={
            "task_type": "sum_numbers",
            "payload": {"numbers": [1, 2, 3]},
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert UUID(body["id"])
    assert body["task_type"] == "sum_numbers"
    assert body["payload"] == {"numbers": [1, 2, 3]}
    assert body["idempotency_key"] is None
    assert body["status"] == "queued"
    assert body["result"] is None
    assert body["version"] == 0
    assert response.headers["Location"] == f"/v1/tasks/{body['id']}"


def test_duplicate_idempotency_key_returns_same_task_without_duplicate_enqueue(
    client: TestClient,
) -> None:
    headers = {"Idempotency-Key": "create-sum-once"}
    request_body = {
        "task_type": "sum_numbers",
        "payload": {"numbers": [1, 2, 3]},
    }

    first = client.post("/v1/tasks", headers=headers, json=request_body)
    replay = client.post(
        "/v1/tasks",
        headers=headers,
        json={
            "task_type": "sum_numbers",
            "payload": {"numbers": [999]},
        },
    )

    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["payload"] == request_body["payload"]
    assert replay.json()["idempotency_key"] == "create-sum-once"
    listed = client.get("/v1/tasks")
    assert listed.json()["total"] == 1
    completed = _wait_for_api_status(client, first.headers["Location"], "succeeded")
    assert completed.json()["version"] == 2


def test_created_task_can_be_queried_across_requests(client: TestClient) -> None:
    created = client.post(
        "/v1/tasks",
        json={
            "task_type": "word_count",
            "payload": {"text": "hello world"},
        },
    )

    queried = _wait_for_api_status(
        client,
        created.headers["Location"],
        "succeeded",
    )

    assert queried.status_code == 200
    assert queried.json()["id"] == created.json()["id"]
    assert queried.json()["status"] == "succeeded"
    assert queried.json()["result"] == {
        "output": {"word_count": 2},
        "error": None,
    }


def test_get_does_not_change_task_state_or_timestamp(
    client: TestClient,
    repository: InMemoryTaskRepository,
) -> None:
    task = Task(payload={"numbers": [1]})
    _run(repository.add(task))
    before = _run(repository.get(task.id))
    assert before is not None

    response = client.get(f"/v1/tasks/{task.id}")

    after = _run(repository.get(task.id))
    assert response.status_code == 200
    assert after is not None
    assert after.status is before.status
    assert after.updated_at == before.updated_at


def test_application_instances_do_not_share_tasks() -> None:
    first_application = create_app(InMemoryTaskRepository())
    second_application = create_app(InMemoryTaskRepository())
    with (
        TestClient(first_application) as first_client,
        TestClient(second_application) as second_client,
    ):
        created = first_client.post(
            "/v1/tasks",
            json={
                "task_type": "sum_numbers",
                "payload": {"numbers": [1]},
            },
        )
        second_list = second_client.get("/v1/tasks")

    assert created.status_code == 201
    assert second_list.json()["total"] == 0


@pytest.mark.parametrize(
    "request_body",
    [
        {},
        {"task_type": "unknown", "payload": {}},
        {"task_type": "sum_numbers", "payload": {"numbers": []}},
        {"task_type": "sum_numbers", "payload": {"numbers": [True]}},
        {"task_type": "sum_numbers", "payload": {"numbers": [1_000_001]}},
        {"task_type": "sum_numbers", "payload": {"numbers": [1] * 101}},
        {
            "task_type": "sum_numbers",
            "payload": {"numbers": [1], "unexpected": True},
        },
        {
            "task_type": "word_count",
            "payload": {"text": "valid"},
            "status": "queued",
        },
        {"task_type": "word_count", "payload": {"text": "   "}},
        {"task_type": "word_count", "payload": {"text": "x" * 10_001}},
    ],
)
def test_create_rejects_invalid_payloads(
    client: TestClient,
    request_body: dict[str, object],
) -> None:
    response = client.post("/v1/tasks", json=request_body)

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert set(body["details"]) == {"errors"}
    assert body["request_id"] == response.headers["X-Request-ID"]


def test_get_unknown_task_returns_unified_404(client: TestClient) -> None:
    task_id = uuid4()

    response = client.get(f"/v1/tasks/{task_id}")

    assert response.status_code == 404
    body = response.json()
    assert body == {
        "code": "task_not_found",
        "message": "任务不存在",
        "details": {"task_id": str(task_id)},
        "request_id": response.headers["X-Request-ID"],
    }


def test_list_tasks_supports_status_filter_and_pagination(
    client: TestClient,
    repository: InMemoryTaskRepository,
) -> None:
    first_queued = Task(task_type=TaskType.WORD_COUNT, payload={"text": "first"})
    running = Task(task_type=TaskType.WORD_COUNT, payload={"text": "running"})
    running.transition_to(TaskStatus.RUNNING)
    second_queued = Task(task_type=TaskType.WORD_COUNT, payload={"text": "second"})
    for task in (first_queued, running, second_queued):
        _run(repository.add(task))

    response = client.get("/v1/tasks?status=queued&offset=1&limit=1")

    assert response.status_code == 200
    body = response.json()
    assert body["offset"] == 1
    assert body["limit"] == 1
    assert body["total"] == 2
    assert len(body["items"]) == 1
    assert body["items"][0]["id"] == str(second_queued.id)


def test_list_order_is_stable_and_offset_can_exceed_total(
    client: TestClient,
    repository: InMemoryTaskRepository,
) -> None:
    for number in range(3):
        _run(
            repository.add(
                Task(
                    task_type=TaskType.SUM_NUMBERS,
                    payload={"numbers": [number]},
                )
            )
        )

    first_ids = [item["id"] for item in client.get("/v1/tasks").json()["items"]]
    second_ids = [item["id"] for item in client.get("/v1/tasks").json()["items"]]
    beyond = client.get("/v1/tasks?offset=10&limit=1")

    assert first_ids == second_ids
    assert beyond.status_code == 200
    assert beyond.json()["items"] == []
    assert beyond.json()["total"] == 3


@pytest.mark.parametrize("current_status", [TaskStatus.QUEUED, TaskStatus.RUNNING])
def test_cancel_allowed_task(
    client: TestClient,
    repository: InMemoryTaskRepository,
    current_status: TaskStatus,
) -> None:
    task = _task_in_status(current_status)
    _run(repository.add(task))

    response = client.post(f"/v1/tasks/{task.id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "cancel_requested"
    stored = _run(repository.get(task.id))
    assert stored is not None
    assert stored.status is TaskStatus.CANCEL_REQUESTED


@pytest.mark.parametrize(
    "current_status",
    [
        TaskStatus.CANCEL_REQUESTED,
        TaskStatus.SUCCEEDED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    ],
)
def test_cancel_conflict_returns_unified_409(
    client: TestClient,
    repository: InMemoryTaskRepository,
    current_status: TaskStatus,
) -> None:
    task = _task_in_status(current_status)
    _run(repository.add(task))

    response = client.post(f"/v1/tasks/{task.id}/cancel")

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "invalid_task_transition"
    assert body["details"] == {
        "task_id": str(task.id),
        "from_status": current_status.value,
        "action": "cancel",
    }
    assert body["request_id"] == response.headers["X-Request-ID"]


def test_retry_failed_task(client: TestClient, repository: InMemoryTaskRepository) -> None:
    task = Task(task_type=TaskType.SUM_NUMBERS, payload={"numbers": [1]})
    task.transition_to(TaskStatus.RUNNING)
    task.transition_to(TaskStatus.FAILED)
    _run(repository.add(task))

    response = client.post(f"/v1/tasks/{task.id}/retry")

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    completed = _wait_for_api_status(
        client,
        f"/v1/tasks/{task.id}",
        "succeeded",
    )
    assert completed.json()["result"] == {
        "output": {"total": 1},
        "error": None,
    }


@pytest.mark.parametrize(
    "current_status",
    [
        TaskStatus.QUEUED,
        TaskStatus.RUNNING,
        TaskStatus.SUCCEEDED,
        TaskStatus.CANCEL_REQUESTED,
        TaskStatus.CANCELLED,
    ],
)
def test_retry_conflict_returns_409(
    client: TestClient,
    repository: InMemoryTaskRepository,
    current_status: TaskStatus,
) -> None:
    task = _task_in_status(current_status)
    _run(repository.add(task))

    response = client.post(f"/v1/tasks/{task.id}/retry")

    assert response.status_code == 409
    assert response.json()["code"] == "invalid_task_transition"


@pytest.mark.parametrize("action", ["cancel", "retry"])
def test_task_action_returns_404_for_unknown_task(
    client: TestClient,
    action: str,
) -> None:
    response = client.post(f"/v1/tasks/{uuid4()}/{action}")

    assert response.status_code == 404
    assert response.json()["code"] == "task_not_found"


def test_invalid_request_returns_unified_422(client: TestClient) -> None:
    response = client.post(
        "/v1/tasks",
        json={"task_type": "unknown", "payload": {}, "unexpected": True},
    )

    assert response.status_code == 422
    body = response.json()
    assert set(body) == {"code", "message", "details", "request_id"}
    assert body["code"] == "validation_error"
    assert body["message"] == "请求参数校验失败"
    assert body["details"]
    assert body["request_id"] == response.headers["X-Request-ID"]


def test_query_validation_error_returns_422(client: TestClient) -> None:
    response = client.get("/v1/tasks?offset=-1&limit=101")

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_invalid_status_filter_returns_422(client: TestClient) -> None:
    response = client.get("/v1/tasks?status=not-a-status")

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_request_id_is_generated_and_consistent(client: TestClient) -> None:
    first = client.get(f"/v1/tasks/{uuid4()}")
    second = client.get(f"/v1/tasks/{uuid4()}")

    assert first.json()["request_id"] == first.headers["X-Request-ID"]
    assert second.json()["request_id"] == second.headers["X-Request-ID"]
    assert first.headers["X-Request-ID"] != second.headers["X-Request-ID"]


def test_internal_error_does_not_leak_exception_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "database-password=do-not-leak"

    class FailingRepository(InMemoryTaskRepository):
        async def get(self, task_id: UUID) -> Task | None:
            raise RuntimeError(secret)

    application = create_app(FailingRepository())
    with (
        caplog.at_level(logging.ERROR),
        TestClient(application, raise_server_exceptions=False) as client,
    ):
        response = client.get(f"/v1/tasks/{uuid4()}")

    assert response.status_code == 500
    body_text = response.text
    assert secret not in body_text
    assert "Traceback" not in body_text
    assert response.json()["code"] == "internal_error"
    assert response.json()["details"] == {}
    assert response.json()["request_id"] == response.headers["X-Request-ID"]
    assert secret not in caplog.text
    assert "未处理异常" in caplog.text


def test_openapi_contains_v1_task_routes(client: TestClient) -> None:
    response = client.get("/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/v1/tasks" in paths
    assert "/v1/tasks/{task_id}" in paths
    assert "/v1/tasks/{task_id}/events" in paths
    assert "/v1/tasks/{task_id}/cancel" in paths
    assert "/v1/tasks/{task_id}/retry" in paths
    assert "/health/live" in paths
    assert "/health/ready" in paths
    assert "/metrics" in paths
    create_operation = paths["/v1/tasks"]["post"]
    assert "Location" in create_operation["responses"]["201"]["headers"]
    assert "422" in create_operation["responses"]
    assert "503" in create_operation["responses"]
    assert "500" in create_operation["responses"]
    header_parameters = create_operation["parameters"]
    assert any(parameter["name"] == "Idempotency-Key" for parameter in header_parameters)
    retry_operation = paths["/v1/tasks/{task_id}/retry"]["post"]
    assert "503" in retry_operation["responses"]


def test_docs_are_available(client: TestClient) -> None:
    response = client.get("/docs")

    assert response.status_code == 200
    assert "Agent Runtime Foundation" in response.text


def test_unknown_route_and_method_use_unified_error_format(client: TestClient) -> None:
    unknown_route = client.get("/v1/unknown")
    wrong_method = client.delete("/v1/tasks")

    assert unknown_route.status_code == 404
    assert wrong_method.status_code == 405
    assert unknown_route.json()["code"] == "http_error"
    assert wrong_method.json()["code"] == "http_error"
