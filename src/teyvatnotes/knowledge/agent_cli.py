"""只读工具问答入口；离线模拟走同一循环，不接入公开任务接口。"""

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import cast

from teyvatnotes.application.guide_agent import AgentResult, GuideAgentService
from teyvatnotes.application.knowledge_read_ports import KnowledgeReader
from teyvatnotes.application.model_ports import ModelError, ModelUsage
from teyvatnotes.application.question_preparation import (
    QuestionPreparationService,
    QuestionRequest,
)
from teyvatnotes.application.tool_model_ports import (
    ToolCall,
    ToolTurnRequest,
    ToolTurnResponse,
)
from teyvatnotes.database import Database, create_database
from teyvatnotes.domain.guides import GuideImportError
from teyvatnotes.domain.json_types import JsonValue
from teyvatnotes.domain.knowledge import EvidenceScope
from teyvatnotes.knowledge.answers import (
    JsonAnswerDecoder,
    answer_to_json,
    demo_answer_json,
)
from teyvatnotes.knowledge.demo import create_demo_repository
from teyvatnotes.knowledge.output import emit_json
from teyvatnotes.knowledge.questions import load_question
from teyvatnotes.knowledge.tools import KnowledgeToolSession
from teyvatnotes.models.responses_tools import configured_tool_client
from teyvatnotes.repositories.knowledge_postgres import PostgresKnowledgeRepository


class OfflineToolClient:
    """固定的 search → read → 摘录演示；不是模型自主选择能力的证明。"""

    async def generate_turn(self, request: ToolTurnRequest) -> ToolTurnResponse:
        content = json.loads(request.messages[-1].content)
        if not isinstance(content, dict) or not isinstance(content.get("player"), dict):
            raise ModelError("invalid_model_request")
        player = cast(dict[str, JsonValue], content["player"])
        count = sum(item.get("type") == "function_call_output" for item in request.history)
        if count == 0:
            return self._call(
                "demo_search", "search_knowledge", {"keywords": content.get("keywords"), "limit": 5}
            )
        output = json.loads(str(request.history[-1].get("output")))
        if count == 1 and output.get("ok") and output.get("hits"):
            return self._call(
                "demo_read", "read_evidence", {"evidence_id": output["hits"][0]["evidence_id"]}
            )
        if count == 2 and output.get("ok") and output.get("evidence"):
            text = demo_answer_json(player, output["evidence"])
        else:
            text = json.dumps(
                {
                    "status": "insufficient_evidence",
                    "claims": [],
                    "questions": [],
                    "gaps": ["离线演示未取得完整证据。"],
                    "conflicts": [],
                },
                ensure_ascii=False,
            )
        return ToolTurnResponse(text, (), (), "offline-tool-demo", ModelUsage(None, None))

    def _call(
        self, identifier: str, name: str, parameters: dict[str, JsonValue]
    ) -> ToolTurnResponse:
        arguments = json.dumps(parameters, ensure_ascii=False, allow_nan=False)
        call = ToolCall(identifier, name, arguments)
        return ToolTurnResponse(
            None,
            (call,),
            (
                {
                    "type": "function_call",
                    "call_id": identifier,
                    "name": name,
                    "arguments": arguments,
                },
            ),
            "offline-tool-demo",
            ModelUsage(None, None),
        )


def agent_to_json(result: AgentResult, request: QuestionRequest) -> dict[str, JsonValue]:
    """私有 CLI trace 包含已验证工具参数；不输出正文/opaque reasoning/凭据。"""
    demo = result.answer.demo_only
    public = answer_to_json(result.answer, request, simulated_model=demo)
    turns = result.model_turns
    input_counts = [turn.usage.input_tokens for turn in turns]
    output_counts = [turn.usage.output_tokens for turn in turns]
    public.update(
        {
            "generation_attempts": len(turns),
            "model_called": bool(turns) and not demo,
            "model": turns[-1].model if turns else None,
            "input_tokens": sum(value for value in input_counts if value is not None)
            if turns and all(value is not None for value in input_counts)
            else None,
            "output_tokens": sum(value for value in output_counts if value is not None)
            if turns and all(value is not None for value in output_counts)
            else None,
            "tool_attempts": len(result.tool_calls),
            "model_turns": [
                {
                    "index": turn.index,
                    "model": turn.model,
                    "input_tokens": turn.usage.input_tokens,
                    "output_tokens": turn.usage.output_tokens,
                    "cost": None,
                    "elapsed_ms": turn.elapsed_ms,
                    "error": turn.error,
                }
                for turn in turns
            ],
            "tool_calls": [
                {
                    "index": call.index,
                    "name": call.name,
                    "parameters": call.parameters,
                    "elapsed_ms": call.elapsed_ms,
                    "error": call.error,
                }
                for call in result.tool_calls
            ],
        }
    )
    return public


def tool_session(
    reader: KnowledgeReader, request: QuestionRequest, demo_only: bool
) -> KnowledgeToolSession:
    """范围从已校验玩家字段构建，模型参数没有权限修改范围。"""
    if request.game_version is None or request.guide_kind is None or request.entity is None:
        raise GuideImportError("invalid_question")
    return KnowledgeToolSession(
        reader,
        EvidenceScope(
            request.game_version, request.guide_kind, request.entity, request.conditions, demo_only
        ),
    )


async def run_command(arguments: argparse.Namespace) -> dict[str, JsonValue]:
    request = await asyncio.to_thread(load_question, Path(arguments.path))
    database: Database | None = None
    reader: KnowledgeReader
    if arguments.demo:
        reader = await create_demo_repository()
    else:
        database_url = os.getenv("GUIDE_DATABASE_URL")
        if not database_url:
            raise GuideImportError("database_not_configured")
        database = create_database(database_url)
        reader = PostgresKnowledgeRepository(database.session_factory)
    try:
        service = GuideAgentService(
            QuestionPreparationService(reader, demo_only=arguments.demo),
            OfflineToolClient if arguments.demo else configured_tool_client,
            lambda question: tool_session(reader, question, arguments.demo),
            JsonAnswerDecoder(),
            demo_only=arguments.demo,
        )
        result = agent_to_json(await service.answer(request), request)
        return {**result, "storage": "memory_demo" if arguments.demo else "postgres"}
    finally:
        if database is not None:
            await database.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读知识工具循环攻略问答")
    parser.add_argument("path", help="UTF-8 问题 JSON 文件")
    parser.add_argument(
        "--demo", action="store_true", help="虚构资料 + 固定离线工具模拟，无模型费用"
    )
    arguments = parser.parse_args(argv)
    try:
        result = asyncio.run(run_command(arguments))
    except (GuideImportError, ModelError) as exc:
        result = {"status": "error", "error": exc.code}
    except Exception:
        result = {"status": "error", "error": "agent_unavailable"}
    emit_json(result)
    return 1 if result.get("status") == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
