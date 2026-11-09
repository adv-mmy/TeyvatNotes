"""仅连接共享攻略 HTTPS 服务；无数据库、模型 SDK 或重试。"""

import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx

from teyvatnotes.application.conversations import ConversationError, question_to_data, safe_reply
from teyvatnotes.application.model_budget import GenerationLedger
from teyvatnotes.application.question_preparation import QuestionRequest
from teyvatnotes.desktop.config import (
    DesktopError,
    atomic_write,
    load_config,
    read_regular,
    tls_context,
)
from teyvatnotes.domain.json_types import JsonValue

_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}", re.ASCII)
_REMOTE_ERRORS = {
    "authentication_required",
    "quota_exceeded",
    "guide_busy",
    "knowledge_unavailable",
    "model_unavailable",
    "model_timeout",
    "request_timeout",
    "validation_error",
    "request_too_large",
    "answer_too_large",
    "source_busy",
    "source_timeout",
    "source_unavailable",
}


class RemoteGuideAnswerer:
    """连接和令牌都绑定到配置中的 HTTPS 源；首次实问才访问服务器。"""

    def __init__(self, directory: Path, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._directory, self._transport = directory, transport
        self._token_path = directory / "client" / "anonymous-session.json"
        self._lock = asyncio.Lock()

    @staticmethod
    async def _post(
        client: httpx.AsyncClient,
        path: str,
        body: dict[str, JsonValue],
        *,
        status: int,
        token: str | None = None,
    ) -> dict[str, JsonValue]:
        headers = {"Authorization": "Bearer " + token} if token is not None else {}
        async with client.stream("POST", path, json=body, headers=headers) as response:
            data = bytearray()
            async for chunk in response.aiter_bytes():
                if len(chunk) + len(data) > 65_536:
                    raise ConversationError("guide_response_invalid")
                data.extend(chunk)
            try:
                parsed = json.loads(data.decode("utf-8"))
            except (ValueError, RecursionError):
                raise ConversationError("guide_response_invalid") from None
            if not isinstance(parsed, dict):
                raise ConversationError("guide_response_invalid")
            if response.status_code != status:
                code = parsed.get("code")
                if response.status_code == 401:
                    raise ConversationError("authentication_required")
                raise ConversationError(
                    code
                    if isinstance(code, str) and code in _REMOTE_ERRORS
                    else "guide_service_unavailable"
                )
            return cast(dict[str, JsonValue], parsed)

    @staticmethod
    def _valid_token(raw: object, origin: str) -> str | None:
        if not isinstance(raw, dict) or raw.get("origin") != origin:
            return None
        token, expires = raw.get("access_token"), raw.get("expires_at")
        if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
            return None
        try:
            if isinstance(expires, str):
                time = datetime.fromisoformat(expires)
                if time.tzinfo is not None and time > datetime.now(UTC):
                    return token
        except ValueError:
            pass
        return None

    async def _token(self, client: httpx.AsyncClient, origin: str) -> str:
        async with self._lock:
            raw = await asyncio.to_thread(read_regular, self._token_path, 4096)
            try:
                cached = json.loads(raw) if raw else None
            except (ValueError, RecursionError):
                cached = None
            token = self._valid_token(cached, origin)
            if token is None:
                value = await self._post(client, "/v1/guides/sessions", {}, status=201)
                saved = {"origin": origin, **value}
                token = self._valid_token(saved, origin)
                if token is None or set(value) != {"access_token", "expires_at"}:
                    raise ConversationError("guide_response_invalid")
                await asyncio.to_thread(
                    atomic_write, self._token_path, json.dumps(saved).encode("utf-8")
                )
            return token

    async def answer(
        self,
        request: QuestionRequest,
        *,
        demo_only: bool,
        generation: GenerationLedger | None = None,
    ) -> dict[str, JsonValue]:
        if demo_only:
            raise ConversationError("guide_response_invalid")
        try:
            config = await asyncio.to_thread(load_config, self._directory / "client.toml")
            if not config.service_url:
                raise ConversationError("client_config_required")
            verify = await asyncio.to_thread(tls_context, self._directory, config.service_url)
            async with (
                asyncio.timeout(110),
                httpx.AsyncClient(
                    base_url=config.service_url,
                    verify=verify,
                    transport=self._transport,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=httpx.Timeout(105, connect=5),
                ) as client,
            ):
                token = await self._token(client, config.service_url)
                try:
                    payload = await self._post(
                        client,
                        "/v1/guides/answer",
                        question_to_data(request),
                        token=token,
                        status=200,
                    )
                except ConversationError as error:
                    if error.code == "authentication_required":
                        # 只为下一次手工提交失效，不重放本轮。
                        await asyncio.to_thread(atomic_write, self._token_path, b"{}")
                    raise
            if payload.get("status") not in (
                "answer",
                "needs_clarification",
                "insufficient_evidence",
            ):
                raise ConversationError("guide_response_invalid")
            return safe_reply(payload)
        except DesktopError as error:
            raise ConversationError(error.code) from None
        except (httpx.TimeoutException, TimeoutError):
            raise ConversationError("request_timeout") from None
        except (httpx.HTTPError, OSError):
            raise ConversationError("guide_service_unavailable") from None
