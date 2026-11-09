"""专用共享攻略 API：匿名鉴权、证据编排及有界模型调用。"""

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect

from teyvatnotes.application.guide_ingestion import GuideIngestionService
from teyvatnotes.application.model_ports import (
    ModelClient,
    ModelNotCalledError,
    ModelRequest,
    ModelResponse,
)
from teyvatnotes.application.public_guides import PublicGuideService
from teyvatnotes.config import GuideSettings
from teyvatnotes.database.session import Database, create_database
from teyvatnotes.domain.guides import GuideImportError
from teyvatnotes.knowledge.answers import JsonAnswerDecoder, answer_to_json
from teyvatnotes.knowledge.questions import MAX_QUESTION_BYTES, parse_question
from teyvatnotes.models.responses import configured_client
from teyvatnotes.repositories.guide_access import GuideAccess, PostgresGuideAccess, QuotaExceeded
from teyvatnotes.repositories.knowledge_postgres import PostgresKnowledgeRepository

logger = logging.getLogger("teyvatnotes.guides")
_TOKEN = re.compile(r"Bearer ([A-Za-z0-9_-]{43})", re.IGNORECASE | re.ASCII)
_REPLY_FIELDS = frozenset(
    {
        "status",
        "claims",
        "questions",
        "gaps",
        "conflicts",
        "reason",
        "model_called",
        "generation_attempts",
        "model",
        "input_tokens",
        "output_tokens",
        "warnings",
    }
)
_MAX_REPLY_BYTES = 65_536


class ApiError(Exception):
    """只传稳定错误和可选重试时间，不传请求、凭据或上游原文。"""

    def __init__(self, status: int, code: str, retry_after: int | None = None) -> None:
        self.status, self.code, self.retry_after = status, code, retry_after


def _network_key(request: Request, secret: bytes) -> str:
    try:
        address = ipaddress.ip_address(request.client.host if request.client else "")
    except ValueError:
        raise ApiError(400, "invalid_client_address") from None
    if isinstance(address, ipaddress.IPv6Address):
        address = (
            address.ipv4_mapped
            or ipaddress.IPv6Network((address, 64), strict=False).network_address
        )
    # 只信 ASGI peer；可信反代范围由部署配置约束，不读取用户提供的转发头。
    return hmac.new(secret, address.packed, hashlib.sha256).hexdigest()


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError
        result[name] = value
    return result


def _reject_constant(_: str) -> None:
    raise ValueError


async def _body(request: Request, *, allow_empty: bool = False) -> object:
    body = bytearray()
    async for chunk in request.stream():
        if len(chunk) + len(body) > MAX_QUESTION_BYTES:
            raise ApiError(413, "request_too_large")
        body.extend(chunk)
    if allow_empty and not body:
        return {}
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/json"
    ):
        raise ApiError(415, "unsupported_media_type")
    try:
        return json.loads(
            body.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_reject_constant
        )
    except (UnicodeError, ValueError, RecursionError):
        raise ApiError(422, "validation_error") from None


class _MeteredModel:
    def __init__(
        self,
        client: ModelClient,
        access: GuideAccess,
        principal: str,
        ip_key: str,
        gate: asyncio.Semaphore,
        clock: Callable[[], datetime],
    ) -> None:
        self._client, self._access = client, access
        self._principal, self._ip, self._gate, self._clock = principal, ip_key, gate, clock
        self.retry_after: int | None = None

    async def generate(self, request: ModelRequest) -> ModelResponse:
        if self._gate.locked():
            raise ModelNotCalledError("guide_busy")
        async with self._gate:
            try:
                await self._access.reserve(self._principal, self._ip, self._clock())
            except QuotaExceeded as error:
                self.retry_after = error.retry_after
                raise ModelNotCalledError("quota_exceeded") from None
            except SQLAlchemyError:
                raise ModelNotCalledError("knowledge_unavailable") from None
            return await self._client.generate(request)


async def _ready(database: Database) -> bool:
    async with asyncio.timeout(2), database.session_factory() as session:
        for table in (
            "guide_sources",
            "guide_documents",
            "guide_chunks",
            "guide_ingestion_steps",
            "guide_access_sessions",
            "guide_daily_quotas",
        ):
            await session.execute(text(f"SELECT 1 FROM {table} LIMIT 0"))
    return True


async def _while_connected[T](request: Request, run: Awaitable[T]) -> T:
    """连接在回答期间断开时取消剩余工作；不自动重发或退还已扣额度。"""

    async def reply() -> T:
        return await run

    async def disconnect() -> None:
        while (await request.receive())["type"] != "http.disconnect":
            pass

    work = asyncio.create_task(reply())
    monitor = asyncio.create_task(disconnect())
    try:
        await asyncio.wait((work, monitor), return_when=asyncio.FIRST_COMPLETED)
        if work.done():
            return work.result()
        raise ClientDisconnect
    finally:
        for task in (work, monitor):
            if not task.done():
                task.cancel()
        await asyncio.gather(work, monitor, return_exceptions=True)


def create_app(
    settings: GuideSettings | None = None,
    *,
    database: Database | None = None,
    guides: PublicGuideService | None = None,
    access: GuideAccess | None = None,
    model_factory: Callable[[], ModelClient] = configured_client,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FastAPI:
    """生产路径只有 PostgreSQL；测试可显式注入两套隔离端口。"""
    resolved = settings or GuideSettings.from_env()
    owned_database = database is None and (guides is None or access is None)
    if owned_database:
        database = create_database(resolved.database_url)
    if guides is None:
        assert database is not None
        repository = PostgresKnowledgeRepository(database.session_factory)
        guides = PublicGuideService(
            repository, GuideIngestionService(repository), decoder=JsonAnswerDecoder()
        )
    if access is None:
        assert database is not None
        access = PostgresGuideAccess(database.session_factory)
    service, authorizer, db = guides, access, database
    model_gate, request_gate = asyncio.Semaphore(2), asyncio.Semaphore(16)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            try:
                await service.aclose()
            finally:
                if owned_database and db is not None:
                    await db.dispose()

    app = FastAPI(
        title="TeyvatNotes 攻略服务",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    def error_response(request: Request, error: ApiError) -> JSONResponse:
        request.state.error_code = error.code
        headers = {"Retry-After": str(error.retry_after)} if error.retry_after is not None else {}
        if error.status == 401:
            headers["WWW-Authenticate"] = "Bearer"
        return JSONResponse(
            {"code": error.code, "message": error.code, "request_id": request.state.request_id},
            status_code=error.status,
            headers=headers,
        )

    @app.middleware("http")
    async def boundary(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.request_id = uuid4().hex
        request.state.lookup_path = None
        request.state.error_code = None
        started = asyncio.get_running_loop().time()
        try:
            if request_gate.locked():
                raise ApiError(429, "guide_busy", 1)
            async with request_gate:
                async with asyncio.timeout(105):
                    response = await call_next(request)
        except ApiError as error:
            response = error_response(request, error)
        except ClientDisconnect:
            response = error_response(request, ApiError(499, "client_disconnected"))
        except SQLAlchemyError:
            response = error_response(request, ApiError(503, "knowledge_unavailable"))
        except TimeoutError:
            response = error_response(request, ApiError(504, "request_timeout"))
        except Exception:
            response = error_response(request, ApiError(500, "internal_error"))
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Cache-Control"] = "no-store"
        logger.info(
            "guide_http request_id=%s path=%s status=%d error_code=%s elapsed_ms=%d",
            request.state.request_id,
            request.state.lookup_path,
            response.status_code,
            request.state.error_code,
            round((asyncio.get_running_loop().time() - started) * 1000),
        )
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> Response:
        return error_response(request, ApiError(error.status_code, "http_error"))

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        try:
            if db is not None and await _ready(db):
                return {"status": "ok"}
        except (SQLAlchemyError, TimeoutError):
            pass
        raise ApiError(503, "not_ready")

    @app.post("/v1/guides/sessions", status_code=201)
    async def session(request: Request) -> JSONResponse:
        if await _body(request, allow_empty=True) != {}:
            raise ApiError(422, "validation_error")
        ip_key = _network_key(request, resolved.ip_hash_key)
        try:
            token = await authorizer.issue(ip_key, clock())
        except QuotaExceeded as error:
            raise ApiError(429, "quota_exceeded", error.retry_after) from None
        return JSONResponse(
            {"access_token": token.access_token, "expires_at": token.expires_at.isoformat()},
            status_code=201,
        )

    @app.post("/v1/guides/answer")
    async def answer(request: Request) -> JSONResponse:
        match = _TOKEN.fullmatch(request.headers.get("authorization", ""))
        if match is None or (principal := await authorizer.authenticate(match[1], clock())) is None:
            raise ApiError(401, "authentication_required")
        try:
            question = parse_question(await _body(request))
        except GuideImportError:
            raise ApiError(422, "validation_error") from None
        ip_key = _network_key(request, resolved.ip_hash_key)
        wrapper: _MeteredModel | None = None

        def metered() -> ModelClient:
            nonlocal wrapper
            wrapper = _MeteredModel(
                model_factory(), authorizer, principal, ip_key, model_gate, clock
            )
            return wrapper

        result = await _while_connected(request, service.answer(question, metered))
        request.state.lookup_path = result.lookup_path
        if result.status == "error":
            reason = result.reason
            if reason == "quota_exceeded":
                raise ApiError(429, reason, wrapper.retry_after if wrapper is not None else None)
            if reason in {"guide_busy", "source_busy"}:
                raise ApiError(429, reason, 1)
            if reason == "knowledge_unavailable":
                raise ApiError(503, reason)
            if reason in {"answer_timeout", "model_timeout"}:
                raise ApiError(504, "model_timeout")
            if reason == "source_timeout":
                raise ApiError(504, reason)
            if reason == "source_unavailable":
                raise ApiError(502, reason)
            if reason in {"invalid_model_configuration", "model_key_not_configured"}:
                raise ApiError(503, "model_unavailable")
            raise ApiError(502, "model_unavailable")
        raw = answer_to_json(result, question)
        payload = {key: raw[key] for key in _REPLY_FIELDS}
        response = JSONResponse(payload)
        if len(response.body) > _MAX_REPLY_BYTES:
            raise ApiError(502, "answer_too_large")
        return response

    return app
