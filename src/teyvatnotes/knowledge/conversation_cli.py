"""私有攻略会话CLI；显式玩家条件，独立持久化，不接公开Task接口。"""

import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

from teyvatnotes.application.conversations import (
    ConversationError,
    ConversationService,
    ConversationSubmission,
    GuideConversation,
    QuestionPatch,
    question_from_data,
    question_to_data,
)
from teyvatnotes.application.guide_agent import GuideAgentService
from teyvatnotes.application.guide_ingestion import GuideIngestionService
from teyvatnotes.application.knowledge_read_ports import KnowledgeReader
from teyvatnotes.application.model_budget import GenerationLedger
from teyvatnotes.application.model_ports import ModelError
from teyvatnotes.application.question_preparation import (
    QuestionPreparationService,
    QuestionRequest,
)
from teyvatnotes.application.question_understanding import QuestionUnderstandingService
from teyvatnotes.application.tool_model_ports import ToolModelClient
from teyvatnotes.database import create_database
from teyvatnotes.domain.guides import GuideImportError
from teyvatnotes.domain.task import JsonValue
from teyvatnotes.knowledge.agent_cli import (
    OfflineToolClient,
    agent_to_json,
    tool_session,
)
from teyvatnotes.knowledge.answers import JsonAnswerDecoder
from teyvatnotes.knowledge.demo import PROJECT_ROOT, create_demo_repository
from teyvatnotes.knowledge.output import emit_json
from teyvatnotes.knowledge.questions import load_question
from teyvatnotes.knowledge.snapshots import load_snapshot
from teyvatnotes.knowledge.terminology import load_catalog
from teyvatnotes.knowledge.understanding import JsonUnderstandingDecoder
from teyvatnotes.models.responses import configured_client
from teyvatnotes.models.responses_tools import configured_tool_client
from teyvatnotes.repositories.conversation_memory import InMemoryConversationRepository
from teyvatnotes.repositories.conversation_postgres import (
    PostgresConversationRepository,
)
from teyvatnotes.repositories.knowledge_postgres import PostgresKnowledgeRepository


def _unique_pairs(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError


def load_patch(path: Path) -> QuestionPatch:
    """16KiB有界读取；重复键、NaN、无效UTF-8不静默接受。"""
    try:
        with path.open("rb") as stream:
            raw = stream.read(16_385)
        if len(raw) > 16_384:
            raise ConversationError("conversation_input_too_large")
        value = json.loads(
            raw.decode("utf-8-sig"),
            object_pairs_hook=_unique_pairs,
            parse_constant=_reject_constant,
        )
        if not isinstance(value, dict):
            raise ConversationError("invalid_conversation_input")
        return QuestionPatch(value)
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise ConversationError("conversation_input_unreadable") from None


class GuideConversationAnswerer:
    """每轮新建Agent用例/工具账本；只把当前已确认条件交给模型。"""

    def __init__(
        self,
        reader: KnowledgeReader,
        tool_client_factory: Callable[[], ToolModelClient] | None = None,
    ) -> None:
        self._reader = reader
        self._tool_client_factory = tool_client_factory

    async def answer(
        self,
        request: QuestionRequest,
        *,
        demo_only: bool,
        generation: GenerationLedger | None = None,
    ) -> dict[str, JsonValue]:
        service = GuideAgentService(
            QuestionPreparationService(self._reader, demo_only=demo_only),
            OfflineToolClient if demo_only else self._tool_client_factory or configured_tool_client,
            lambda question: tool_session(self._reader, question, demo_only),
            JsonAnswerDecoder(),
            demo_only=demo_only,
        )
        return agent_to_json(await service.answer(request, generation=generation), request)


def conversation_view(value: GuideConversation, *, history: bool = False) -> dict[str, JsonValue]:
    """历史是显式私有视图；旧轮条件/答案不伪装成当前建议。"""
    result: dict[str, JsonValue] = {
        "status": "ok",
        "conversation_id": str(value.id),
        "revision": value.revision,
        "closed": value.closed,
        "demo_only": value.demo_only,
        "turn_count": len(value.turns),
        "context": question_to_data(value.context),
        "active_turn_id": str(value.active_turn.id) if value.active_turn else None,
        "latest_reply": value.latest_reply,
        "created_at": value.created_at.isoformat(),
        "updated_at": value.updated_at.isoformat(),
    }
    if history:
        result["history"] = [
            {
                "turn_id": str(turn.id),
                "number": turn.number,
                "status": turn.status,
                "context": question_to_data(turn.request),
                "reply": turn.reply,
                "started_at": turn.started_at.isoformat(),
                "finished_at": turn.finished_at.isoformat() if turn.finished_at else None,
            }
            for turn in value.turns
        ]
    return result


async def run_demo() -> dict[str, JsonValue]:
    """三轮虚构摘录演示：追问、补全、装备替换；不读取任何部署环境。"""
    reader = await create_demo_repository()
    extra = await asyncio.to_thread(
        load_snapshot,
        PROJECT_ROOT / "samples/guides/conversation_character_alternative.json",
    )
    await GuideIngestionService(reader).import_snapshot(extra)
    sample = await asyncio.to_thread(
        load_question, PROJECT_ROOT / "samples/questions/character.json"
    )
    service = ConversationService(
        InMemoryConversationRepository(),
        uuid4(),
        GuideConversationAnswerer(reader),
    )
    value = await service.create(replace(sample, owned_equipment=None), demo_only=True)
    assert value is not None
    steps: list[JsonValue] = []
    patches: tuple[dict[str, JsonValue], ...] = (
        {"question": "我这个示例角色该怎样培养？"},
        question_to_data(sample),
        {
            "question": "换成我已拥有的示例木弓（纯虚构）。",
            "owned_equipment": ["示例木弓"],
            "conditions": {"装备": "示例木弓"},
        },
    )
    for patch in patches:
        value = await service.ask(value.id, QuestionPatch(patch), expected_revision=value.revision)
        assert value is not None
        steps.append(conversation_view(value))
    return {
        "status": "ok",
        "storage": "memory_demo",
        "model_called": False,
        "demo_only": True,
        "steps": steps,
        "conversation": conversation_view(value, history=True),
        "warnings": ["fictional_demo_only", "simulated_model_no_network", "not_fact_verified"],
    }


def _identifier(raw: str, code: str) -> UUID:
    try:
        return UUID(raw)
    except (ValueError, TypeError, AttributeError):
        raise ConversationError(code) from None


async def run_command(
    arguments: argparse.Namespace, *, generation: GenerationLedger | None = None
) -> dict[str, JsonValue]:
    if arguments.command == "demo":
        return await run_demo()
    if arguments.command == "understand":
        context = (
            await asyncio.to_thread(load_question, Path(arguments.context))
            if arguments.context
            else None
        )
        ledger = (
            generation
            if generation is not None
            else GenerationLedger()
            if arguments.extract_model
            else None
        )
        understood = await _understanding_service(arguments.extract_model).understand(
            arguments.text,
            context,
            generation=ledger,
        )
        return {**understood.to_json(), **({"round_usage": ledger.to_json()} if ledger else {})}
    # 输入先校验；配置失败不创建连接，也不读取模型凭据。
    patch = (
        await asyncio.to_thread(load_patch, Path(arguments.path))
        if arguments.command in {"create", "ask"} and arguments.path is not None
        else None
    )
    owner = _identifier(
        os.getenv("GUIDE_SESSION_OWNER_ID", ""), "conversation_owner_not_configured"
    )
    identifier = (
        _identifier(arguments.id, "invalid_conversation_id")
        if arguments.command != "create"
        else None
    )
    url = os.getenv("DATABASE_URL")
    if not url:
        raise ConversationError("database_not_configured")
    database = create_database(url)
    if generation is None and getattr(arguments, "extract_model", False):
        generation = GenerationLedger()
    submission = ConversationSubmission(generation)
    text = getattr(arguments, "text", None)
    try:
        reader = PostgresKnowledgeRepository(database.session_factory)
        service = ConversationService(
            PostgresConversationRepository(database.session_factory),
            owner,
            GuideConversationAnswerer(reader),
            understanding_factory=lambda: _understanding_service(
                getattr(arguments, "extract_model", False)
            ),
        )
        if arguments.command == "create":
            request = (
                text
                if text is not None
                else question_from_data(patch.values)
                if patch is not None
                else None
            )
            if request is None:
                raise ConversationError("invalid_conversation_input")
            value = await service.create(request, submission=submission)
        elif identifier is None:
            raise ConversationError("invalid_conversation_id")
        elif arguments.command == "show":
            value = await service.get(identifier)
        elif arguments.command == "ask":
            patch = text if text is not None else patch
            if patch is None:
                raise ConversationError("invalid_conversation_input")
            value = await service.ask(
                identifier,
                patch,
                expected_revision=arguments.revision,
                submission=submission,
            )
        elif arguments.command == "close":
            value = await service.close(identifier, expected_revision=arguments.revision)
        else:
            value = await service.interrupt(identifier, expected_revision=arguments.revision)
        if value is None:
            # 未通过理解的提交没有领取轮次；状态与字段形状沿用原 CLI 合同。
            assert submission.understanding is not None
            output: dict[str, JsonValue] = {
                "status": submission.understanding.status,
                "storage": "postgres",
            }
        else:
            output = {
                **conversation_view(value, history=getattr(arguments, "history", False)),
                "storage": "postgres",
            }
        if submission.understanding is not None:
            output["understanding"] = submission.understanding.to_json()
        if generation is not None:
            output["round_usage"] = generation.to_json()
        return output
    except (ConversationError, GuideImportError, ModelError) as exc:
        if generation is None:
            raise
        code = exc.code if re.fullmatch(r"[a-z_]{1,64}", exc.code) else "conversation_unavailable"
        return {
            "status": "error",
            "error": code,
            "understanding": submission.understanding.to_json()
            if submission.understanding is not None
            else None,
            "round_usage": generation.to_json(),
        }
    except Exception:
        if generation is None:
            raise
        return {
            "status": "error",
            "error": "conversation_unavailable",
            "understanding": submission.understanding.to_json()
            if submission.understanding is not None
            else None,
            "round_usage": generation.to_json(),
        }
    finally:
        cancelling = isinstance(sys.exception(), asyncio.CancelledError)
        try:
            await database.dispose()
        except Exception:
            # 清理失败不能把提取/回答的合作取消改成普通异常。
            if not cancelling:
                raise


def _understanding_service(use_model: bool) -> QuestionUnderstandingService:
    """显式模式，无失败后规则回退；仅真正生成时读取通用模型配置。"""
    return QuestionUnderstandingService(
        load_catalog(),
        model_factory=configured_client if use_model else None,
        decoder=JsonUnderstandingDecoder() if use_model else None,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="私有攻略会话与显式玩家条件")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("demo", help="三轮虚构内存演示，无网络或付费请求")
    understand = commands.add_parser(
        "understand", help="理解一句话，不保存；默认离线，可显式开启模型提取"
    )
    understand.add_argument("text")
    understand.add_argument("--context", help="已确认条件JSON；不是任意历史答案")
    understand.add_argument(
        "--extract-model", action="store_true", help="单次模型候选提取，可能计费，无自动重试"
    )
    create = commands.add_parser("create", help="创建会话，尚不生成答案")
    inputs = create.add_mutually_exclusive_group(required=True)
    inputs.add_argument("path", nargs="?", help="初始问题JSON")
    inputs.add_argument("--text", help="自然语言初始问题，可缺条件以等待追问")
    create.add_argument(
        "--extract-model",
        action="store_true",
        help="配合--text调用一次提取模型，可能计费；不生成答案",
    )
    for name in ("show", "ask", "close", "interrupt"):
        command = commands.add_parser(name)
        command.add_argument("id", help="会话UUID")
        if name == "ask":
            inputs = command.add_mutually_exclusive_group(required=True)
            inputs.add_argument("path", nargs="?", help="本轮问题与显式条件补充JSON")
            inputs.add_argument("--text", help="自然语言问题或字段补充；条件齐全时可能调用付费模型")
            command.add_argument(
                "--extract-model",
                action="store_true",
                help="配合--text开启模型提取，与回答共用调用预算，可能计费",
            )
        if name != "show":
            command.add_argument("--revision", required=True, type=int, help="show返回的当前修订号")
        if name == "show":
            command.add_argument("--history", action="store_true", help="显示私有完整历史")
    arguments = parser.parse_args(argv)
    if (
        getattr(arguments, "extract_model", False)
        and arguments.command in {"create", "ask"}
        and arguments.text is None
    ):
        parser.error("--extract-model必须与--text一起使用")
    generation = GenerationLedger() if getattr(arguments, "extract_model", False) else None
    try:
        result = asyncio.run(run_command(arguments, generation=generation))
    except (ConversationError, GuideImportError, ModelError) as exc:
        result = {"status": "error", "error": exc.code}
    except Exception:
        result = {"status": "error", "error": "conversation_unavailable"}
    if generation is not None:
        # 包括数据库清理失败在内，最外层仍持有实际用量，不会退回零调用。
        result["round_usage"] = generation.to_json()
    emit_json(result)
    reply = result.get("latest_reply")
    return int(
        result.get("status") == "error"
        or (isinstance(reply, dict) and reply.get("status") == "error")
    )


if __name__ == "__main__":
    raise SystemExit(main())
