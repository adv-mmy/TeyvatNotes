"""从环境变量加载且不会在 repr 中暴露密钥的应用配置。"""

from __future__ import annotations

import math
import os
import re
import socket
from collections.abc import Mapping
from dataclasses import dataclass, field
from ipaddress import ip_address
from typing import Literal, cast

from pydantic import SecretStr

from teyvatnotes.runtime import RuntimeSettings
from teyvatnotes.worker import RetryPolicy, WorkerSettings

type ExecutionMode = Literal["embedded", "api_only"]

_API_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._~+/=-]{32,256}$")


class ConfigurationError(ValueError):
    """环境变量缺失或格式无效。"""


@dataclass(frozen=True, slots=True)
class RateLimitSettings:
    """高成本写接口的进程内滑动窗口配置。"""

    requests: int = 30
    window_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.requests < 1:
            raise ValueError("requests 必须大于 0")
        if not math.isfinite(self.window_seconds) or self.window_seconds <= 0:
            raise ValueError("window_seconds 必须是大于 0 的有限数")


@dataclass(frozen=True, slots=True)
class AppSettings:
    """API、Runtime、存储和限流的集中配置快照。"""

    api_key: SecretStr = field(repr=False)
    database_url: SecretStr | None = field(default=None, repr=False)
    execution_mode: ExecutionMode = "embedded"
    runtime: RuntimeSettings = field(default_factory=RuntimeSettings)
    rate_limit: RateLimitSettings = field(default_factory=RateLimitSettings)

    def __post_init__(self) -> None:
        api_key = self.api_key.get_secret_value()
        if _API_KEY_PATTERN.fullmatch(api_key) is None:
            raise ConfigurationError("AGENT_RUNTIME_API_KEY 必须是 32～256 位 Bearer 安全字符")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> AppSettings:
        """读取环境变量并在进程启动时尽早拒绝无效配置。"""

        values = os.environ if environ is None else environ
        api_key = values.get("AGENT_RUNTIME_API_KEY")
        if api_key is None:
            raise ConfigurationError("缺少必需环境变量 AGENT_RUNTIME_API_KEY")

        execution_mode_value = values.get(
            "AGENT_RUNTIME_EXECUTION_MODE",
            "embedded",
        )
        if execution_mode_value not in {"embedded", "api_only"}:
            raise ConfigurationError("AGENT_RUNTIME_EXECUTION_MODE 必须是 embedded 或 api_only")
        execution_mode = cast(ExecutionMode, execution_mode_value)

        database_url_value = values.get("DATABASE_URL")
        database_url = (
            SecretStr(database_url_value)
            if database_url_value is not None and database_url_value.strip()
            else None
        )
        return cls(
            api_key=SecretStr(api_key),
            database_url=database_url,
            execution_mode=execution_mode,
            runtime=RuntimeSettings(
                worker_count=_read_int(values, "AGENT_RUNTIME_WORKER_COUNT", 4),
                max_concurrency=_read_int(
                    values,
                    "AGENT_RUNTIME_MAX_CONCURRENCY",
                    2,
                ),
                task_timeout_seconds=_read_float(
                    values,
                    "AGENT_RUNTIME_TASK_TIMEOUT_SECONDS",
                    30.0,
                ),
                shutdown_timeout_seconds=_read_float(
                    values,
                    "AGENT_RUNTIME_SHUTDOWN_TIMEOUT_SECONDS",
                    5.0,
                ),
            ),
            rate_limit=RateLimitSettings(
                requests=_read_int(
                    values,
                    "AGENT_RUNTIME_RATE_LIMIT_REQUESTS",
                    30,
                ),
                window_seconds=_read_float(
                    values,
                    "AGENT_RUNTIME_RATE_LIMIT_WINDOW_SECONDS",
                    60.0,
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class WorkerProcessSettings:
    """独立 PostgreSQL Worker 进程的数据库与执行策略配置。"""

    database_url: SecretStr = field(repr=False)
    worker: WorkerSettings
    monitor_host: str = "127.0.0.1"
    monitor_port: int = 9101

    def __post_init__(self) -> None:
        try:
            ip_address(self.monitor_host)
        except ValueError as exc:
            raise ConfigurationError("AGENT_RUNTIME_WORKER_MONITOR_HOST 必须是 IP 地址") from exc
        if not 1 <= self.monitor_port <= 65535:
            raise ConfigurationError("AGENT_RUNTIME_WORKER_MONITOR_PORT 必须在 1～65535 之间")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> WorkerProcessSettings:
        """读取 Worker 环境变量，并拒绝缺少数据库连接的启动。"""

        values = os.environ if environ is None else environ
        database_url = values.get("DATABASE_URL")
        if database_url is None or not database_url.strip():
            raise ConfigurationError("独立 Worker 缺少必需环境变量 DATABASE_URL")
        worker_id = values.get("AGENT_RUNTIME_WORKER_ID")
        if worker_id is None or not worker_id.strip():
            worker_id = f"{socket.gethostname()}-{os.getpid()}"
        return cls(
            database_url=SecretStr(database_url),
            monitor_host=values.get("AGENT_RUNTIME_WORKER_MONITOR_HOST", "127.0.0.1"),
            monitor_port=_read_int(values, "AGENT_RUNTIME_WORKER_MONITOR_PORT", 9101),
            worker=WorkerSettings(
                worker_id=worker_id,
                poll_interval_seconds=_read_float(
                    values,
                    "AGENT_RUNTIME_WORKER_POLL_INTERVAL_SECONDS",
                    0.2,
                ),
                lease_duration_seconds=_read_float(
                    values,
                    "AGENT_RUNTIME_WORKER_LEASE_DURATION_SECONDS",
                    45.0,
                ),
                task_timeout_seconds=_read_float(
                    values,
                    "AGENT_RUNTIME_TASK_TIMEOUT_SECONDS",
                    30.0,
                ),
                retry_policy=RetryPolicy(
                    max_attempts=_read_int(values, "AGENT_RUNTIME_MAX_ATTEMPTS", 3),
                    base_delay_seconds=_read_float(
                        values,
                        "AGENT_RUNTIME_RETRY_BASE_DELAY_SECONDS",
                        1.0,
                    ),
                    max_delay_seconds=_read_float(
                        values,
                        "AGENT_RUNTIME_RETRY_MAX_DELAY_SECONDS",
                        30.0,
                    ),
                    jitter_ratio=_read_float(
                        values,
                        "AGENT_RUNTIME_RETRY_JITTER_RATIO",
                        0.2,
                    ),
                ),
            ),
        )


def _read_int(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} 必须是整数") from exc


def _read_float(values: Mapping[str, str], name: str, default: float) -> float:
    raw = values.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} 必须是数字") from exc
    if not math.isfinite(value):
        raise ConfigurationError(f"{name} 必须是有限数")
    return value
