"""第七周环境配置、Bearer 鉴权、限流和输入白名单测试。"""

import logging
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from conftest import AUTH_HEADERS, TEST_API_KEY
from fastapi.testclient import TestClient
from pydantic import SecretStr

from agent_runtime_foundation.api import create_app
from agent_runtime_foundation.api.security import (
    ApiKeyAuthenticator,
    AuthenticatedPrincipal,
    AuthenticationError,
    RateLimitExceededError,
    SlidingWindowRateLimiter,
)
from agent_runtime_foundation.config import (
    AppSettings,
    ConfigurationError,
    RateLimitSettings,
)
from agent_runtime_foundation.repositories import InMemoryTaskRepository


def _settings(
    *,
    requests: int = 30,
    window_seconds: float = 60.0,
) -> AppSettings:
    return AppSettings(
        api_key=SecretStr(TEST_API_KEY),
        rate_limit=RateLimitSettings(
            requests=requests,
            window_seconds=window_seconds,
        ),
    )


def _sum_payload(number: int = 1) -> dict[str, object]:
    return {
        "task_type": "sum_numbers",
        "payload": {"numbers": [number]},
    }


def test_settings_require_api_key_and_mask_all_secrets() -> None:
    with pytest.raises(ConfigurationError, match="AGENT_RUNTIME_API_KEY"):
        AppSettings.from_env({})

    database_url = "postgresql+asyncpg://user:database-secret@db/runtime"
    settings = AppSettings.from_env(
        {
            "AGENT_RUNTIME_API_KEY": TEST_API_KEY,
            "DATABASE_URL": database_url,
        }
    )

    rendered = repr(settings)
    assert TEST_API_KEY not in rendered
    assert "database-secret" not in rendered
    assert "api_key" not in rendered
    assert "database_url" not in rendered


@pytest.mark.parametrize(
    ("overrides", "expected_message"),
    [
        ({"AGENT_RUNTIME_API_KEY": "short"}, "32～256"),
        ({"AGENT_RUNTIME_EXECUTION_MODE": "worker"}, "embedded 或 api_only"),
        ({"AGENT_RUNTIME_WORKER_COUNT": "many"}, "必须是整数"),
        ({"AGENT_RUNTIME_TASK_TIMEOUT_SECONDS": "nan"}, "必须是有限数"),
        ({"AGENT_RUNTIME_RATE_LIMIT_REQUESTS": "0"}, "requests 必须大于 0"),
    ],
)
def test_settings_fail_fast_for_invalid_environment(
    overrides: Mapping[str, str],
    expected_message: str,
) -> None:
    environ = {"AGENT_RUNTIME_API_KEY": TEST_API_KEY, **overrides}
    with pytest.raises((ConfigurationError, ValueError), match=expected_message):
        AppSettings.from_env(environ)


def test_create_and_cancel_require_valid_bearer_api_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    application = create_app(
        InMemoryTaskRepository(),
        settings=_settings(),
        execution_mode="api_only",
    )
    with caplog.at_level(logging.INFO), TestClient(application) as client:
        missing = client.post("/v1/tasks", json=_sum_payload())
        invalid = client.post(
            "/v1/tasks",
            headers={"Authorization": "Bearer wrong-key-that-is-never-logged"},
            json=_sum_payload(),
        )
        created = client.post(
            "/v1/tasks",
            headers=AUTH_HEADERS,
            json=_sum_payload(),
        )
        task_id = created.json()["id"]
        cancel_without_key = client.post(f"/v1/tasks/{task_id}/cancel")
        public_read = client.get(f"/v1/tasks/{task_id}")

    assert missing.status_code == 401
    assert missing.json()["code"] == "authentication_required"
    assert missing.headers["WWW-Authenticate"] == "Bearer"
    assert invalid.status_code == 401
    assert "wrong-key" not in invalid.text
    assert "wrong-key" not in caplog.text
    assert created.status_code == 201
    assert cancel_without_key.status_code == 401
    assert public_read.status_code == 200
    assert public_read.json()["status"] == "queued"


def test_authenticator_rejects_non_ascii_credentials_as_unauthorized() -> None:
    authenticator = ApiKeyAuthenticator(SecretStr(TEST_API_KEY))

    with pytest.raises(AuthenticationError):
        authenticator.authenticate("凭据" * 16)


def test_high_cost_endpoints_share_a_bounded_rate_limit() -> None:
    application = create_app(
        InMemoryTaskRepository(),
        settings=_settings(requests=2),
        execution_mode="api_only",
    )
    with TestClient(application, headers=AUTH_HEADERS) as client:
        first = client.post("/v1/tasks", json=_sum_payload(1))
        second = client.post("/v1/tasks", json=_sum_payload(2))
        rejected = client.post("/v1/tasks", json=_sum_payload(3))
        rejected_retry = client.post(f"/v1/tasks/{uuid4()}/retry")
        listed = client.get("/v1/tasks")

    assert first.status_code == 201
    assert second.status_code == 201
    assert rejected.status_code == 429
    assert rejected.json()["code"] == "rate_limit_exceeded"
    assert rejected.json()["details"] == {"retry_after_seconds": 60}
    assert rejected.headers["Retry-After"] == "60"
    assert rejected_retry.status_code == 429
    assert rejected_retry.json()["code"] == "rate_limit_exceeded"
    assert listed.json()["total"] == 2


def test_rate_limit_recovers_after_the_sliding_window() -> None:
    now = 100.0
    limiter = SlidingWindowRateLimiter(
        RateLimitSettings(requests=1, window_seconds=10),
        clock=lambda: now,
    )
    principal = AuthenticatedPrincipal()

    limiter.check(principal)
    with pytest.raises(RateLimitExceededError) as captured:
        limiter.check(principal)
    assert captured.value.retry_after_seconds == 10

    now = 110.0
    limiter.check(principal)


def test_rate_limit_keeps_timestamps_ordered_when_clock_reads_arrive_out_of_order() -> None:
    readings = iter((101.0, 100.0, 110.5))
    limiter = SlidingWindowRateLimiter(
        RateLimitSettings(requests=2, window_seconds=10),
        clock=lambda: next(readings),
    )
    principal = AuthenticatedPrincipal()

    limiter.check(principal)
    limiter.check(principal)
    limiter.check(principal)


def test_rate_limit_concurrent_requests_cannot_exceed_quota() -> None:
    quota = 20
    contenders = 60
    limiter = SlidingWindowRateLimiter(
        RateLimitSettings(requests=quota, window_seconds=60),
        clock=lambda: 100.0,
    )
    principal = AuthenticatedPrincipal()
    barrier = Barrier(contenders)

    def compete(_: int) -> bool:
        barrier.wait()
        try:
            limiter.check(principal)
        except RateLimitExceededError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=contenders) as pool:
        accepted = list(pool.map(compete, range(contenders)))

    assert sum(accepted) == quota


def test_task_input_cannot_select_python_functions_or_system_commands() -> None:
    application = create_app(InMemoryTaskRepository(), settings=_settings())
    with TestClient(application, headers=AUTH_HEADERS) as client:
        response = client.post(
            "/v1/tasks",
            json={
                "task_type": "run_python",
                "payload": {
                    "function": "os.system",
                    "command": "whoami",
                },
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


def test_openapi_marks_only_mutating_routes_as_bearer_protected() -> None:
    application = create_app(InMemoryTaskRepository(), settings=_settings())
    with TestClient(application) as client:
        paths = client.get("/openapi.json").json()["paths"]

    assert paths["/v1/tasks"]["post"]["security"] == [{"HTTPBearer": []}]
    assert "security" not in paths["/v1/tasks"]["get"]
    assert paths["/v1/tasks/{task_id}/cancel"]["post"]["security"] == [{"HTTPBearer": []}]
    assert paths["/v1/tasks/{task_id}/retry"]["post"]["security"] == [{"HTTPBearer": []}]
    assert "security" not in paths["/v1/tasks/{task_id}"]["get"]
