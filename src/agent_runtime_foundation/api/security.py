"""Bearer API Key 鉴权与单进程滑动窗口限流。"""

from __future__ import annotations

import math
import secrets
from bisect import bisect_right, insort
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import Annotated, cast

from fastapi import Depends, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import SecretStr

from agent_runtime_foundation.config import RateLimitSettings

_bearer = HTTPBearer(auto_error=False)


class AuthenticationError(Exception):
    """请求没有携带有效的 Bearer API Key。"""


class RateLimitExceededError(Exception):
    """当前主体在滑动窗口内超过允许请求数。"""

    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__("rate limit exceeded")


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    """鉴权后的稳定主体；不保存也不返回原始 API Key。"""

    identifier: str = "configured-api-key"


class ApiKeyAuthenticator:
    """用恒定时间比较验证单个环境变量 API Key。"""

    def __init__(self, api_key: SecretStr) -> None:
        self._api_key = api_key

    def authenticate(self, presented_key: str | None) -> AuthenticatedPrincipal:
        """凭据缺失、过长或不匹配时返回同一种公开错误。"""

        if presented_key is None or not 1 <= len(presented_key) <= 256:
            raise AuthenticationError
        expected = self._api_key.get_secret_value()
        if not secrets.compare_digest(
            presented_key.encode("utf-8"),
            expected.encode("utf-8"),
        ):
            raise AuthenticationError
        return AuthenticatedPrincipal()


class SlidingWindowRateLimiter:
    """线程安全的进程内滑动窗口；不跨容器共享。"""

    def __init__(
        self,
        settings: RateLimitSettings,
        *,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._requests = settings.requests
        self._window_seconds = settings.window_seconds
        self._clock = clock
        self._timestamps: defaultdict[str, list[float]] = defaultdict(list)
        self._lock = Lock()

    def check(self, principal: AuthenticatedPrincipal) -> None:
        """原子清理过期时间戳、检查配额并记录本次请求。"""

        with self._lock:
            now = self._clock()
            if not math.isfinite(now):
                raise RuntimeError("限流时钟必须返回有限数")
            cutoff = now - self._window_seconds
            timestamps = self._timestamps[principal.identifier]
            expired = bisect_right(timestamps, cutoff)
            if expired:
                del timestamps[:expired]
            if len(timestamps) >= self._requests:
                retry_after = max(
                    1,
                    math.ceil(timestamps[0] + self._window_seconds - now),
                )
                raise RateLimitExceededError(retry_after)
            insort(timestamps, now)


def get_authenticator(request: Request) -> ApiKeyAuthenticator:
    """从应用状态读取鉴权器。"""

    return cast(ApiKeyAuthenticator, request.app.state.api_key_authenticator)


def get_rate_limiter(request: Request) -> SlidingWindowRateLimiter:
    """从应用状态读取高成本接口限流器。"""

    return cast(SlidingWindowRateLimiter, request.app.state.rate_limiter)


async def require_api_key(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Security(_bearer),
    ],
) -> AuthenticatedPrincipal:
    """FastAPI 依赖：要求 Authorization: Bearer <API Key>。"""

    presented_key = credentials.credentials if credentials is not None else None
    return get_authenticator(request).authenticate(presented_key)


AuthenticatedDependency = Annotated[
    AuthenticatedPrincipal,
    Depends(require_api_key),
]


async def enforce_cost_rate_limit(
    request: Request,
    principal: AuthenticatedDependency,
) -> AuthenticatedPrincipal:
    """FastAPI 依赖：鉴权成功后才消耗高成本接口配额。"""

    get_rate_limiter(request).check(principal)
    return principal


RateLimitedDependency = Annotated[
    AuthenticatedPrincipal,
    Depends(enforce_cost_rate_limit),
]
