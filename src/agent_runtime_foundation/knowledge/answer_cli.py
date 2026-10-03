"""带引用问答 CLI；真实调用只在正式证据就绪后读取 OpenASI 密钥。"""

import argparse
import asyncio
import json
import os
from pathlib import Path

from agent_runtime_foundation.application.guide_answering import GuideAnswerService
from agent_runtime_foundation.application.knowledge_read_ports import KnowledgeReader
from agent_runtime_foundation.application.model_ports import (
    ModelError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from agent_runtime_foundation.application.question_preparation import QuestionPreparationService
from agent_runtime_foundation.database import Database, create_database
from agent_runtime_foundation.domain.guides import GuideImportError
from agent_runtime_foundation.domain.task import JsonValue
from agent_runtime_foundation.knowledge.answers import (
    JsonAnswerDecoder,
    answer_to_json,
    demo_answer_json,
)
from agent_runtime_foundation.knowledge.demo import create_demo_repository
from agent_runtime_foundation.knowledge.output import emit_json
from agent_runtime_foundation.knowledge.questions import load_question
from agent_runtime_foundation.models.openasi import configured_client
from agent_runtime_foundation.repositories.knowledge_postgres import PostgresKnowledgeRepository


class OfflineDemoClient:
    """无网络、无凭据的摘录式演示，不可当作真实模型质量评测。"""

    async def generate(self, request: ModelRequest) -> ModelResponse:
        content: object = json.loads(request.messages[-1].content)
        if not isinstance(content, dict):
            raise ModelError("invalid_model_request")
        player, evidence = content.get("player"), content.get("evidence")
        if not isinstance(player, dict) or not isinstance(evidence, list):
            raise ModelError("invalid_model_request")
        return ModelResponse(
            demo_answer_json(player, evidence), "offline-excerpt-demo", ModelUsage(None, None)
        )


async def run_command(arguments: argparse.Namespace) -> dict[str, JsonValue]:
    """默认只读业务库，绝不使用 TEST_DATABASE_URL；演示与正式来源不能混合。"""
    request = await asyncio.to_thread(load_question, Path(arguments.path))
    database: Database | None = None
    reader: KnowledgeReader
    if arguments.demo:
        reader = await create_demo_repository()
    else:
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise GuideImportError("database_not_configured")
        database = create_database(database_url)
        reader = PostgresKnowledgeRepository(database.session_factory)
    try:
        service = GuideAnswerService(
            QuestionPreparationService(reader, demo_only=arguments.demo),
            OfflineDemoClient if arguments.demo else configured_client,
            JsonAnswerDecoder(),
            demo_only=arguments.demo,
        )
        result = answer_to_json(
            await service.answer(request), request, simulated_model=arguments.demo
        )
        return {**result, "storage": "memory_demo" if arguments.demo else "postgres"}
    finally:
        if database is not None:
            await database.dispose()


def main(argv: list[str] | None = None) -> int:
    """所有失败只输出稳定代码；不打印内部异常、密钥或服务端错误正文。"""
    parser = argparse.ArgumentParser(description="有证据的单轮攻略问答")
    parser.add_argument("path", help="UTF-8 问题 JSON 文件")
    parser.add_argument("--demo", action="store_true", help="虚构资料 + 离线模拟模型，无 API 费用")
    arguments = parser.parse_args(argv)
    try:
        result = asyncio.run(run_command(arguments))
    except (GuideImportError, ModelError) as exc:
        result = {"status": "error", "error": exc.code}
    except Exception:
        result = {"status": "error", "error": "answer_unavailable"}
    emit_json(result)
    return 1 if result.get("status") == "error" else 0


if __name__ == "__main__":
    raise SystemExit(main())
