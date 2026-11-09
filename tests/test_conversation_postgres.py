"""真实随机schema会话验证；保留历史，不调用付费模型，不接公开Task接口。"""

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_conversations import REQUEST, RecordingAnswerer
from test_guide_ingestion import article_snapshot
from test_guide_postgres import check_migrations, connect, isolated_database, migrate

from teyvatnotes.application.conversations import (
    ConversationError,
    ConversationService,
    GuideConversation,
    QuestionPatch,
    question_to_data,
)
from teyvatnotes.application.guide_ingestion import GuideIngestionService
from teyvatnotes.application.model_budget import GenerationLedger
from teyvatnotes.application.question_preparation import QuestionRequest
from teyvatnotes.database.conversations import GuideConversationRecord
from teyvatnotes.domain import JsonValue
from teyvatnotes.knowledge import conversation_cli as cli
from teyvatnotes.knowledge.snapshots import parse_snapshot
from teyvatnotes.repositories.conversation_postgres import (
    PostgresConversationRepository,
)
from teyvatnotes.repositories.knowledge_postgres import PostgresKnowledgeRepository

pytestmark = pytest.mark.postgres


def test_private_state_survives_new_engine_and_inventory_replacement() -> None:
    async def scenario() -> None:
        async with isolated_database() as (first, schema):
            responder, owner = RecordingAnswerer(), uuid4()
            service = ConversationService(
                PostgresConversationRepository(first.session_factory), owner, responder
            )
            created = await service.create(REQUEST)
            initial = await service.ask(
                created.id, QuestionPatch({"question": "first"}), expected_revision=0
            )
            second = connect(schema)
            try:
                restarted = ConversationService(
                    PostgresConversationRepository(second.session_factory), owner, responder
                )
                assert await restarted.get(created.id) == initial
                updated = await restarted.ask(
                    created.id,
                    QuestionPatch(
                        {
                            "question": "换装备",
                            "owned_equipment": ["新装备"],
                            "conditions": {},
                        }
                    ),
                    expected_revision=2,
                )
                assert updated.context.owned_equipment == ("新装备",)
                assert updated.context.conditions == () and updated.revision == 4
                assert updated.turns[0].request.owned_equipment == ("旧装备",)
                assert await service.get(created.id) == updated
            finally:
                await second.dispose()

    asyncio.run(scenario())


def test_model_claim_is_atomic_across_independent_connections() -> None:
    async def scenario() -> None:
        class Waiting(RecordingAnswerer):
            def __init__(self) -> None:
                super().__init__()
                self.entered, self.release = asyncio.Event(), asyncio.Event()

            async def answer(
                self,
                request: QuestionRequest,
                *,
                demo_only: bool,
                generation: GenerationLedger | None = None,
            ) -> dict[str, JsonValue]:
                self.requests.append(request)
                self.entered.set()
                await self.release.wait()
                return {"status": "answer"}

        async with isolated_database() as (first, schema):
            second = connect(schema)
            responder, owner = Waiting(), uuid4()
            services = [
                ConversationService(
                    PostgresConversationRepository(db.session_factory), owner, responder
                )
                for db in (first, second)
            ]
            initial = await services[0].create(REQUEST)
            tasks = [
                asyncio.create_task(
                    service.ask(
                        initial.id,
                        QuestionPatch({"question": "concurrent"}),
                        expected_revision=0,
                    )
                )
                for service in services
            ]
            try:
                async with asyncio.timeout(10):
                    await responder.entered.wait()
                    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    assert len(done) == len(pending) == 1
                    assert isinstance(next(iter(done)).exception(), ConversationError)
                    assert len(responder.requests) == 1
                    responder.release.set()
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    assert sum(isinstance(value, GuideConversation) for value in results) == 1
                    saved = await services[1].get(initial.id)
                    assert len(saved.turns) == 1 and saved.revision == 2
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await second.dispose()

    asyncio.run(scenario())


def test_owner_predicate_and_close_are_persisted_without_model_calls() -> None:
    async def scenario() -> None:
        async with isolated_database() as (db, _):
            repo, responder = (
                PostgresConversationRepository(db.session_factory),
                RecordingAnswerer(),
            )
            service = ConversationService(repo, uuid4(), responder)
            value = await service.create(REQUEST)
            other = ConversationService(repo, uuid4(), responder)
            with pytest.raises(ConversationError, match="conversation_not_found"):
                await other.get(value.id)
            with pytest.raises(ConversationError, match="conversation_not_found"):
                await other.ask(
                    value.id, QuestionPatch({"question": "other owner"}), expected_revision=0
                )
            assert (await service.close(value.id, expected_revision=0)).closed
            with pytest.raises(ConversationError, match="conversation_closed"):
                await service.ask(
                    value.id, QuestionPatch({"question": "closed"}), expected_revision=1
                )
            assert not responder.requests

    asyncio.run(scenario())


def test_upgrade_from_0004_preserves_guides_and_matches_metadata() -> None:
    async def scenario() -> None:
        async with isolated_database("20261004_0004") as (db, _):
            repo = PostgresKnowledgeRepository(db.session_factory)
            imported = await GuideIngestionService(repo).import_snapshot(
                parse_snapshot(article_snapshot())
            )
            async with db.engine.begin() as connection:
                await connection.run_sync(migrate)
                await connection.run_sync(check_migrations)
            assert (await repo.get_document(imported.document_id)) is not None
            value = await ConversationService(
                PostgresConversationRepository(db.session_factory),
                uuid4(),
                RecordingAnswerer(),
            ).create(REQUEST)
            assert value.revision == 0

    asyncio.run(scenario())


def test_interrupt_records_crashed_turn_without_replaying_late_model_result() -> None:
    async def scenario() -> None:
        class Waiting(RecordingAnswerer):
            def __init__(self) -> None:
                super().__init__()
                self.entered, self.release = asyncio.Event(), asyncio.Event()

            async def answer(
                self,
                request: QuestionRequest,
                *,
                demo_only: bool,
                generation: GenerationLedger | None = None,
            ) -> dict[str, JsonValue]:
                self.requests.append(request)
                self.entered.set()
                await self.release.wait()
                return {"status": "answer", "claims": [{"text": "late"}]}

        async with isolated_database() as (db, schema):
            second = connect(schema)
            responder, owner = Waiting(), uuid4()
            service = ConversationService(
                PostgresConversationRepository(db.session_factory), owner, responder
            )
            recovering = ConversationService(
                PostgresConversationRepository(second.session_factory), owner, responder
            )
            value = await service.create(REQUEST)
            task = asyncio.create_task(
                service.ask(value.id, QuestionPatch({"question": "问"}), expected_revision=0)
            )
            try:
                async with asyncio.timeout(10):
                    await responder.entered.wait()
                    pending = await recovering.get(value.id)
                    assert pending.revision == 1 and pending.latest_reply is None
                    recovered = await recovering.interrupt(value.id, expected_revision=1)
                    assert recovered.turns[0].status == "interrupted"
                    closed = await recovering.close(value.id, expected_revision=2)
                    responder.release.set()
                    with pytest.raises(ConversationError, match="conversation_conflict"):
                        await task
                    assert await service.get(value.id) == closed
                    assert len(responder.requests) == 1
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await second.dispose()

    asyncio.run(scenario())


def test_corrupt_private_payload_is_rejected_not_echoed() -> None:
    async def scenario() -> None:
        async with isolated_database() as (db, _):
            service = ConversationService(
                PostgresConversationRepository(db.session_factory), uuid4(), RecordingAnswerer()
            )
            value = await service.create(REQUEST)
            await service.ask(value.id, QuestionPatch({"question": "问"}), expected_revision=0)
            async with db.session_factory.begin() as session:
                row = await session.scalar(
                    select(GuideConversationRecord).where(GuideConversationRecord.id == value.id)
                )
                assert row is not None
                corrupted = deepcopy(row.payload)
                corrupted["turns"][0]["reply"]["reasoning"] = "PRIVATE"
                row.payload = corrupted
            with pytest.raises(ConversationError, match="^conversation_storage_corrupt$"):
                await service.get(value.id)

    asyncio.run(scenario())


def test_cli_create_show_ask_close_use_private_postgres_without_model_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        async with isolated_database() as (_, schema):
            responder = RecordingAnswerer()
            monkeypatch.setenv("GUIDE_SESSION_OWNER_ID", str(uuid4()))
            monkeypatch.setenv("DATABASE_URL", "placeholder-only")
            monkeypatch.setattr(cli, "create_database", lambda url: connect(schema))
            monkeypatch.setattr(cli, "GuideConversationAnswerer", lambda reader: responder)

            def forbidden() -> None:
                raise AssertionError("不能读取凭据或发请求")

            monkeypatch.setattr(cli, "configured_tool_client", forbidden)
            initial_path, patch_path = tmp_path / "question.json", tmp_path / "patch.json"
            initial_path.write_text(json.dumps(question_to_data(REQUEST)), encoding="utf-8")
            patch_path.write_text('{"question":"new","owned_equipment":["new"]}', encoding="utf-8")
            created = await cli.run_command(
                SimpleNamespace(command="create", path=str(initial_path))
            )
            identifier = created["conversation_id"]
            shown = await cli.run_command(
                SimpleNamespace(command="show", id=identifier, history=True)
            )
            assert shown["revision"] == 0 and shown["history"] == []
            asked = await cli.run_command(
                SimpleNamespace(command="ask", id=identifier, path=str(patch_path), revision=0)
            )
            assert asked["revision"] == 2 and len(responder.requests) == 1
            closed = await cli.run_command(
                SimpleNamespace(command="close", id=identifier, revision=2)
            )
            assert closed["closed"] is True and closed["revision"] == 3

    asyncio.run(scenario())


def test_pg_cas_cannot_rewrite_completed_history() -> None:
    async def scenario() -> None:
        async with isolated_database() as (db, _):
            repo = PostgresConversationRepository(db.session_factory)
            service = ConversationService(repo, uuid4(), RecordingAnswerer())
            created = await service.create(REQUEST)
            saved = await service.ask(
                created.id, QuestionPatch({"question": "问"}), expected_revision=0
            )
            altered = replace(
                saved,
                revision=3,
                closed=True,
                turns=(
                    replace(
                        saved.turns[0], reply={"status": "answer", "claims": [{"text": "changed"}]}
                    ),
                ),
            )
            with pytest.raises(ConversationError, match="invalid_conversation_transition"):
                await repo.compare_and_swap(altered, expected_revision=2)
            assert await service.get(saved.id) == saved

    asyncio.run(scenario())
