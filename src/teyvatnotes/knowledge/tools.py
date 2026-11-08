"""两个显式只读知识工具：服务器控制范围，本次命中 ID 才具有回读能力。"""

import json
from typing import Annotated, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from teyvatnotes.application.guide_answering import evidence_payload
from teyvatnotes.application.knowledge_read_ports import KnowledgeReader
from teyvatnotes.application.tool_model_ports import (
    ToolCall,
    ToolDefinition,
    ToolExecution,
)
from teyvatnotes.domain.guides import GuideImportError
from teyvatnotes.domain.knowledge import (
    EvidenceScope,
    EvidenceWindow,
    KeywordQuery,
    document_in_scope,
)
from teyvatnotes.domain.task import JsonValue

MAX_ARGUMENT_CHARACTERS = 4000
MAX_OUTPUT_CHARACTERS = 20_000
Keyword = Annotated[str, Field(strict=True, min_length=1, max_length=80, pattern=r"\S")]


class SearchKnowledgeArguments(BaseModel):
    """模型只能选择字面关键词和命中数，不能改变玩家、版本或资料发布范围。"""

    model_config = ConfigDict(strict=True, extra="forbid")
    keywords: list[Keyword] = Field(
        min_length=1, max_length=12, description="1～12 个字面关键词，每项 1～80 字符。"
    )
    limit: int = Field(ge=1, le=5, description="返回 1～5 个命中；必须是整数，不能是布尔值。")


class ReadEvidenceArguments(BaseModel):
    """只接受规范 UUID 字符串；随后还要检查本次会话的命中授权。"""

    model_config = ConfigDict(strict=True, extra="forbid")
    evidence_id: str = Field(
        min_length=36, max_length=36, description="本次 search_knowledge 返回的规范证据 UUID。"
    )

    @model_validator(mode="after")
    def canonical_identifier(self) -> "ReadEvidenceArguments":
        """拒绝大写、无连字符和其他可被 UUID 自动归一化的写法。"""
        if str(UUID(self.evidence_id)) != self.evidence_id:
            raise ValueError("invalid_evidence_id")
        return self


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """JSON 默认允许重复键；工具边界必须拒绝含糊参数，且不输出键的原文。"""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    """拒绝 Python JSON 解码器默认接受的 NaN、Infinity 和 -Infinity。"""
    raise ValueError("non_json_constant")


def _arguments(raw: str) -> object:
    """先守住字符预算，再严格解码；不是任意函数/表达式执行入口。"""
    if not isinstance(raw, str) or len(raw) > MAX_ARGUMENT_CHARACTERS:
        raise ValueError("invalid_arguments")
    return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)


def _failure(code: str, parameters: dict[str, JsonValue] | None = None) -> ToolExecution:
    """错误只有有限稳定代码；校验前的原始参数、异常文本和正文均不进入 trace。"""
    return ToolExecution({"ok": False, "error": code}, parameters=parameters, error=code)


def _bounded_execution(
    output: dict[str, JsonValue],
    parameters: dict[str, JsonValue],
    evidence: tuple[EvidenceWindow, ...] = (),
) -> ToolExecution:
    """超出预算拒绝整份输出，不静默截断关键前置条件，也不放行引用证据。"""
    encoded = json.dumps(output, ensure_ascii=False, allow_nan=False)
    if len(encoded) > MAX_OUTPUT_CHARACTERS:
        return _failure("tool_output_too_large", parameters)
    return ToolExecution(output, evidence, parameters)


class KnowledgeToolSession:
    """每次问答实例独立；只在显式白名单内分派，固定范围永远不取自模型参数。"""

    def __init__(self, reader: KnowledgeReader, scope: EvidenceScope) -> None:
        if not isinstance(scope, EvidenceScope):
            raise GuideImportError("invalid_knowledge_query")
        self._reader = reader
        self._scope = scope
        self._readable: set[UUID] = set()

    def definitions(self) -> tuple[ToolDefinition, ...]:
        """每次生成独立 Schema，调用者改字典不能改变后续白名单或校验器。"""
        return (
            ToolDefinition(
                "search_knowledge",
                "在服务器固定的版本、实体和玩家条件内检索已存资料；命中摘要不能直接引用。",
                cast(dict[str, JsonValue], SearchKnowledgeArguments.model_json_schema()),
            ),
            ToolDefinition(
                "read_evidence",
                "回读本次检索返回的证据及有界原文上下文；完整回读后才能用于最终答案引用。",
                cast(dict[str, JsonValue], ReadEvidenceArguments.model_json_schema()),
            ),
        )

    async def execute(self, call: ToolCall) -> ToolExecution:
        """参数全部通过后才调用仓储；合作取消不转换成工具错误。"""
        if not isinstance(call.name, str) or call.name not in {"search_knowledge", "read_evidence"}:
            return _failure("tool_not_allowed")
        try:
            decoded = _arguments(call.arguments)
            operation: KeywordQuery | UUID
            if call.name == "search_knowledge":
                search = SearchKnowledgeArguments.model_validate(decoded)
                query = KeywordQuery(self._scope, tuple(search.keywords), search.limit)
                parameters: dict[str, JsonValue] = {
                    "keywords": list(query.keywords),
                    "limit": query.limit,
                }
                operation = query
            else:
                read = ReadEvidenceArguments.model_validate(decoded)
                parameters = {"evidence_id": read.evidence_id}
                operation = UUID(read.evidence_id)
        except (ValidationError, ValueError, TypeError, RecursionError):
            return _failure("invalid_tool_arguments")

        try:
            if isinstance(operation, KeywordQuery):
                return await self._search(operation, parameters)
            return await self._read(operation, parameters)
        except GuideImportError as exc:
            code = (
                exc.code
                if exc.code in {"evidence_not_found", "invalid_knowledge_query"}
                else "knowledge_tool_failed"
            )
            return _failure(code, parameters)
        except Exception:
            return _failure("knowledge_tool_failed", parameters)

    async def _search(self, query: KeywordQuery, parameters: dict[str, JsonValue]) -> ToolExecution:
        hits = await self._reader.search_knowledge(query)
        # 端口实现有缺陷时也不允许越过范围或突破模型指定的有界命中数。
        if len(hits) > query.limit or any(
            not document_in_scope(hit.document, self._scope)
            or hit.chunk.document_id != hit.document.id
            for hit in hits
        ):
            return _failure("knowledge_tool_failed", parameters)
        items: list[JsonValue] = [
            {
                "evidence_id": str(hit.chunk.id),
                "section": hit.chunk.section,
                "snippet": hit.chunk.text[:160],
                "game_version": hit.document.game_version,
                "score": hit.score,
            }
            for hit in hits
        ]
        result = _bounded_execution({"ok": True, "hits": items}, parameters)
        if result.error is None:
            self._readable.update(hit.chunk.id for hit in hits)
        return result

    async def _read(self, identifier: UUID, parameters: dict[str, JsonValue]) -> ToolExecution:
        if identifier not in self._readable:
            return _failure("evidence_not_available", parameters)
        window = await self._reader.read_evidence(identifier, self._scope, max_chunks=20)
        if (
            window.hit.chunk.id != identifier
            or not document_in_scope(window.hit.document, self._scope)
            or len(window.chunks) > 20
            or any(chunk.document_id != window.hit.document.id for chunk in window.chunks)
        ):
            return _failure("evidence_not_found", parameters)
        if window.context_truncated:
            return _failure("context_truncated", parameters)
        if not window.chunks or identifier not in {chunk.id for chunk in window.chunks}:
            return _failure("evidence_not_found", parameters)
        return _bounded_execution(
            {
                "ok": True,
                "evidence": evidence_payload((window,)),
                "context_truncated": False,
            },
            parameters,
            (window,),
        )
