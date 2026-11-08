"""工具白名单、严格参数、固定范围和每次问答独立的证据回读能力。"""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from teyvatnotes.application.guide_answering import evidence_payload
from teyvatnotes.application.knowledge_read_ports import KnowledgeReader
from teyvatnotes.application.tool_model_ports import ToolCall
from teyvatnotes.domain.guides import GuideImportError
from teyvatnotes.domain.knowledge import (
    EvidenceHit,
    EvidenceScope,
    EvidenceWindow,
    KeywordQuery,
)
from teyvatnotes.knowledge.demo import create_demo_repository
from teyvatnotes.knowledge.questions import load_question
from teyvatnotes.knowledge.tools import KnowledgeToolSession

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def demo_scope() -> EvidenceScope:
    """读取长期问题样本，只转换已声明条件，不写临时文件或访问网络。"""
    request = load_question(PROJECT_ROOT / "samples" / "questions" / "quest.json")
    assert request.game_version and request.guide_kind and request.entity
    return EvidenceScope(
        request.game_version, request.guide_kind, request.entity, request.conditions, demo_only=True
    )


def search_call(keywords: tuple[str, ...] = ("第二步",), limit: int = 5) -> ToolCall:
    return ToolCall(
        "search-id", "search_knowledge", json.dumps({"keywords": keywords, "limit": limit})
    )


def read_call(identifier: UUID) -> ToolCall:
    return ToolCall("read-id", "read_evidence", json.dumps({"evidence_id": str(identifier)}))


class ControlledReader:
    """仅在内存替换结果/失败，观察工具实际传入的服务器范围与预算。"""

    def __init__(self, reader: KnowledgeReader) -> None:
        self.reader = reader
        self.queries: list[KeywordQuery] = []
        self.reads: list[tuple[UUID, EvidenceScope, int]] = []
        self.hits: tuple[EvidenceHit, ...] | None = None
        self.window: EvidenceWindow | None = None
        self.error: Exception | None = None
        self.block: asyncio.Event | None = None
        self.entered = asyncio.Event()

    async def search_knowledge(self, query: KeywordQuery) -> tuple[EvidenceHit, ...]:
        self.queries.append(query)
        self.entered.set()
        if self.error:
            raise self.error
        if self.block:
            await self.block.wait()
        return self.hits if self.hits is not None else await self.reader.search_knowledge(query)

    async def read_evidence(
        self, chunk_id: UUID, scope: EvidenceScope, *, max_chunks: int = 20
    ) -> EvidenceWindow:
        self.reads.append((chunk_id, scope, max_chunks))
        self.entered.set()
        if self.error:
            raise self.error
        if self.block:
            await self.block.wait()
        if self.window is not None:
            return self.window
        return await self.reader.read_evidence(chunk_id, scope, max_chunks=max_chunks)


def test_search_summary_is_not_evidence_and_read_preserves_full_prefix() -> None:
    async def scenario() -> None:
        reader = ControlledReader(await create_demo_repository())
        scope = demo_scope()
        session = KnowledgeToolSession(reader, scope)
        search = await session.execute(search_call())
        assert search.error is None and search.evidence == ()
        assert search.output["ok"] is True
        assert reader.queries == [KeywordQuery(scope, ("第二步",), 5)]
        hit = (await reader.reader.search_knowledge(reader.queries[0]))[0]
        expected = await reader.reader.read_evidence(hit.chunk.id, scope)
        read = await session.execute(read_call(hit.chunk.id))
        assert read.error is None and read.evidence == (expected,)
        assert read.output["evidence"] == evidence_payload((expected,))
        assert reader.reads == [(hit.chunk.id, scope, 20)]
        assert expected.chunks[0].section == "前置条件"
        assert "完成标志" in expected.chunks[-1].text

    asyncio.run(scenario())


def test_search_scope_conditions_version_entity_and_demo_cannot_be_overridden() -> None:
    async def scenario() -> None:
        repository = await create_demo_repository()
        for scope in (
            replace(demo_scope(), conditions=()),
            replace(demo_scope(), conditions=(("前置任务", "未完成"),)),
            replace(demo_scope(), game_version="OTHER"),
            replace(demo_scope(), entity="其他实体"),
            replace(demo_scope(), demo_only=False),
        ):
            result = await KnowledgeToolSession(repository, scope).execute(search_call())
            assert result.output["hits"] == [] and not result.evidence
        session = KnowledgeToolSession(repository, demo_scope())
        for field in ("scope", "conditions", "game_version", "entity", "demo_only", "url"):
            raw = {"keywords": ["第二步"], "limit": 5, field: "ignored?"}
            result = await session.execute(ToolCall("id", "search_knowledge", json.dumps(raw)))
            assert result.error == "invalid_tool_arguments" and result.parameters is None

    asyncio.run(scenario())


def test_only_returned_hit_ids_are_readable_and_sessions_do_not_share_capabilities() -> None:
    async def scenario() -> None:
        reader = ControlledReader(await create_demo_repository())
        scope = demo_scope()
        first = KnowledgeToolSession(reader, scope)
        second = KnowledgeToolSession(reader, scope)
        hit = (await reader.reader.search_knowledge(KeywordQuery(scope, ("第二步",))))[0]
        for identifier in (hit.chunk.id, uuid4()):
            denied = await first.execute(read_call(identifier))
            assert denied.error == "evidence_not_available" and not denied.evidence
        assert not reader.reads
        await first.execute(search_call())
        assert (await first.execute(read_call(hit.chunk.id))).error is None
        assert (await second.execute(read_call(hit.chunk.id))).error == "evidence_not_available"
        context_id = (await reader.reader.read_evidence(hit.chunk.id, scope)).chunks[0].id
        assert (await first.execute(read_call(context_id))).error == "evidence_not_available"
        assert len(reader.reads) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "raw",
    [
        "not JSON",
        "[]",
        "null",
        "{}",
        '{"keywords": ["x"]}',
        '{"limit": 1}',
        '{"keywords": [], "limit": 1}',
        '{"keywords": [" "], "limit": 1}',
        '{"keywords": "x", "limit": 1}',
        '{"keywords": [1], "limit": 1}',
        '{"keywords": ["x"], "limit": true}',
        '{"keywords": ["x"], "limit": "1"}',
        '{"keywords": ["x"], "limit": 1.0}',
        '{"keywords": ["x"], "limit": 0}',
        '{"keywords": ["x"], "limit": 6}',
        '{"keywords": ["x"], "limit": 1, "limit": 2}',
        '{"keywords": ["x"], "limit": NaN}',
        '{"keywords": ["x"], "limit": Infinity}',
        '{"keywords": ["x"], "limit": -Infinity}',
        json.dumps({"keywords": ["x"] * 13, "limit": 1}),
        json.dumps({"keywords": ["x" * 81], "limit": 1}),
        '{"keywords": ["x"], "limit": 1}' + " " * 4000,
        '{"keywords": ["x"], "limit": 1, "unknown": {"a": 1, "a": 2}}',
        "[" * 1500 + "0" + "]" * 1500,
    ],
)
def test_search_arguments_reject_coercion_ambiguity_and_unbounded_input(raw: str) -> None:
    async def scenario() -> None:
        reader = ControlledReader(await create_demo_repository())
        result = await KnowledgeToolSession(reader, demo_scope()).execute(
            ToolCall("id", "search_knowledge", raw)
        )
        assert result.error == "invalid_tool_arguments"
        assert result.parameters is None and not result.evidence
        assert not reader.queries and not reader.reads
        assert raw not in json.dumps(result.output)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "raw",
    [
        "{}",
        '{"evidence_id": 123}',
        '{"evidence_id": "https://example.com/"}',
        json.dumps({"evidence_id": str(uuid4()).upper()}),
        json.dumps({"evidence_id": uuid4().hex}),
        json.dumps({"evidence_id": "{" + str(uuid4()) + "}"}),
        json.dumps({"evidence_id": str(uuid4()), "url": "https://example.com/"}),
        '{"evidence_id": "00000000-0000-0000-0000-000000000000", "evidence_id": "x"}',
    ],
)
def test_read_requires_canonical_id_and_has_no_url_or_file_parameters(raw: str) -> None:
    async def scenario() -> None:
        reader = ControlledReader(await create_demo_repository())
        result = await KnowledgeToolSession(reader, demo_scope()).execute(
            ToolCall("id", "read_evidence", raw)
        )
        assert result.error == "invalid_tool_arguments" and result.parameters is None
        assert not reader.reads

    asyncio.run(scenario())


@pytest.mark.parametrize("name", ["eval", "exec", "__import__", "read_file", "fetch_url", ""])
def test_unknown_tool_names_never_dispatch_or_record_raw_parameters(name: str) -> None:
    async def scenario() -> None:
        reader = ControlledReader(await create_demo_repository())
        result = await KnowledgeToolSession(reader, demo_scope()).execute(
            ToolCall("id", name, '{"secret": "PRIVATE-SENTINEL"}')
        )
        assert result.error == "tool_not_allowed" and result.parameters is None
        assert "PRIVATE-SENTINEL" not in repr(result)
        assert not reader.queries and not reader.reads

    asyncio.run(scenario())


def test_search_trace_is_validated_actual_query_and_snippet_is_bounded() -> None:
    async def scenario() -> None:
        reader = ControlledReader(await create_demo_repository())
        hit = (await reader.reader.search_knowledge(KeywordQuery(demo_scope(), ("第二步",))))[0]
        reader.hits = (replace(hit, chunk=replace(hit.chunk, text="甲" * 1000)),)
        result = await KnowledgeToolSession(reader, demo_scope()).execute(
            search_call(("BUILD", "build"), 1)
        )
        assert result.parameters == {"keywords": ["build"], "limit": 1}
        hits = result.output["hits"]
        assert isinstance(hits, list) and isinstance(hits[0], dict)
        assert len(str(hits[0]["snippet"])) == 160
        assert not result.evidence

    asyncio.run(scenario())


def test_truncated_read_is_rejected_without_ledger_or_partial_text() -> None:
    async def scenario() -> None:
        reader = ControlledReader(await create_demo_repository())
        session = KnowledgeToolSession(reader, demo_scope())
        hit = (await reader.reader.search_knowledge(KeywordQuery(demo_scope(), ("第二步",))))[0]
        await session.execute(search_call())
        window = await reader.reader.read_evidence(hit.chunk.id, demo_scope())
        reader.window = replace(window, context_truncated=True)
        result = await session.execute(read_call(hit.chunk.id))
        assert result.error == "context_truncated" and not result.evidence
        assert "evidence" not in result.output and reader.reads[-1][2] == 20

    asyncio.run(scenario())


def test_large_read_rejects_whole_context_and_large_search_does_not_authorize_ids() -> None:
    async def scenario() -> None:
        reader = ControlledReader(await create_demo_repository())
        hit = (await reader.reader.search_knowledge(KeywordQuery(demo_scope(), ("第二步",))))[0]
        session = KnowledgeToolSession(reader, demo_scope())
        reader.hits = (replace(hit, chunk=replace(hit.chunk, section="大" * 20_000)),)
        result = await session.execute(search_call())
        assert result.error == "tool_output_too_large" and not result.evidence
        denied = await session.execute(read_call(hit.chunk.id))
        assert denied.error == "evidence_not_available" and not reader.reads
        reader.hits = None
        await session.execute(search_call())
        window = await reader.reader.read_evidence(hit.chunk.id, demo_scope())
        reader.window = replace(
            window, chunks=tuple(replace(chunk, text="前置" * 10_000) for chunk in window.chunks)
        )
        result = await session.execute(read_call(hit.chunk.id))
        assert result.error == "tool_output_too_large" and not result.evidence
        assert result.parameters == {"evidence_id": str(hit.chunk.id)}
        assert len(json.dumps(result.output, ensure_ascii=False)) < 100

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "error, code",
    [
        (GuideImportError("evidence_not_found"), "evidence_not_found"),
        (GuideImportError("invalid_knowledge_query"), "invalid_knowledge_query"),
        (GuideImportError("PRIVATE-SENTINEL"), "knowledge_tool_failed"),
        (RuntimeError("PRIVATE-SENTINEL"), "knowledge_tool_failed"),
    ],
)
@pytest.mark.parametrize("operation", ["search", "read"])
def test_reader_failures_have_stable_codes_and_validated_trace(
    error: Exception, code: str, operation: str
) -> None:
    async def scenario() -> None:
        reader = ControlledReader(await create_demo_repository())
        session = KnowledgeToolSession(reader, demo_scope())
        call = search_call()
        expected: dict[str, object] = {"keywords": ["第二步"], "limit": 5}
        if operation == "read":
            await session.execute(call)
            hit = (await reader.reader.search_knowledge(reader.queries[0]))[0]
            call = read_call(hit.chunk.id)
            expected = {"evidence_id": str(hit.chunk.id)}
        reader.error = error
        result = await session.execute(call)
        assert result.error == code and not result.evidence
        assert result.parameters == expected
        assert "PRIVATE-SENTINEL" not in repr(result)

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["search", "read"])
def test_cancellation_propagates_and_blocked_reader_has_no_surviving_task(operation: str) -> None:
    async def scenario() -> None:
        reader = ControlledReader(await create_demo_repository())
        session = KnowledgeToolSession(reader, demo_scope())
        call = search_call()
        if operation == "read":
            await session.execute(call)
            hit = (await reader.reader.search_knowledge(reader.queries[0]))[0]
            call = read_call(hit.chunk.id)
        reader.entered.clear()
        reader.block = asyncio.Event()
        task = asyncio.create_task(session.execute(call))
        await reader.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.done()

    asyncio.run(scenario())


def test_faulty_reader_cannot_return_evidence_outside_fixed_version() -> None:
    async def scenario() -> None:
        reader = ControlledReader(await create_demo_repository())
        scope = demo_scope()
        hit = (await reader.reader.search_knowledge(KeywordQuery(scope, ("第二步",))))[0]
        foreign = replace(hit, document=replace(hit.document, game_version="OTHER"))
        reader.hits = (foreign,)
        session = KnowledgeToolSession(reader, scope)
        assert (await session.execute(search_call())).error == "knowledge_tool_failed"
        assert (await session.execute(read_call(hit.chunk.id))).error == "evidence_not_available"
        reader.hits = None
        await session.execute(search_call())
        window = await reader.reader.read_evidence(hit.chunk.id, scope)
        reader.window = replace(window, hit=foreign)
        rejected = await session.execute(read_call(hit.chunk.id))
        assert rejected.error == "evidence_not_found" and not rejected.evidence

    asyncio.run(scenario())


def test_nonreturned_hits_remain_unreadable_and_reader_cannot_exceed_limit() -> None:
    async def scenario() -> None:
        reader = ControlledReader(await create_demo_repository())
        scope = demo_scope()
        all_hits = await reader.reader.search_knowledge(KeywordQuery(scope, ("示例",)))
        assert len(all_hits) > 1
        session = KnowledgeToolSession(reader, scope)
        assert (await session.execute(search_call(("示例",), 1))).error is None
        assert (await session.execute(read_call(all_hits[1].chunk.id))).error == (
            "evidence_not_available"
        )
        reader.hits = all_hits
        fresh = KnowledgeToolSession(reader, scope)
        assert (await fresh.execute(search_call(("示例",), 1))).error == "knowledge_tool_failed"
        assert (await fresh.execute(read_call(all_hits[0].chunk.id))).error == (
            "evidence_not_available"
        )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "invalid_context", ["empty", "foreign_document", "missing_hit", "over_limit"]
)
def test_faulty_reader_cannot_add_invalid_context_to_ledger(invalid_context: str) -> None:
    async def scenario() -> None:
        reader = ControlledReader(await create_demo_repository())
        session = KnowledgeToolSession(reader, demo_scope())
        await session.execute(search_call())
        hit = (await reader.reader.search_knowledge(reader.queries[0]))[0]
        window = await reader.reader.read_evidence(hit.chunk.id, demo_scope())
        match invalid_context:
            case "empty":
                chunks = ()
            case "foreign_document":
                chunks = (replace(window.hit.chunk, document_id=uuid4()),)
            case "missing_hit":
                chunks = tuple(chunk for chunk in window.chunks if chunk.id != hit.chunk.id)
            case _:
                chunks = (window.hit.chunk,) * 21
        reader.window = replace(window, chunks=chunks)
        result = await session.execute(read_call(hit.chunk.id))
        assert result.error == "evidence_not_found" and not result.evidence

    asyncio.run(scenario())


def test_tool_definitions_are_explicit_required_closed_and_not_shared() -> None:
    async def scenario() -> None:
        session = KnowledgeToolSession(await create_demo_repository(), demo_scope())
        definitions = session.definitions()
        assert tuple(item.name for item in definitions) == ("search_knowledge", "read_evidence")
        for item in definitions:
            assert item.parameters["additionalProperties"] is False
        assert definitions[0].parameters["required"] == ["keywords", "limit"]
        assert definitions[1].parameters["required"] == ["evidence_id"]
        definitions[0].parameters["additionalProperties"] = True
        assert session.definitions()[0].parameters["additionalProperties"] is False

    asyncio.run(scenario())
