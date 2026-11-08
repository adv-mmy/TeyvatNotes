"""只读检索、证据回读和问答准备 CLI；当前不生成模型答案。"""

import argparse
import asyncio
import os
from pathlib import Path
from uuid import UUID

from teyvatnotes.application.knowledge_read_ports import KnowledgeReader
from teyvatnotes.application.question_preparation import QuestionPreparationService
from teyvatnotes.database import Database, create_database
from teyvatnotes.domain.guides import GuideImportError, GuideKind
from teyvatnotes.domain.knowledge import (
    EvidenceScope,
    KeywordQuery,
    require_context_limit,
)
from teyvatnotes.domain.task import JsonValue
from teyvatnotes.knowledge.demo import create_demo_repository
from teyvatnotes.knowledge.evidence_json import (
    hit_to_json,
    preparation_to_json,
    window_to_json,
)
from teyvatnotes.knowledge.output import emit_json
from teyvatnotes.knowledge.questions import load_question
from teyvatnotes.repositories.knowledge_postgres import PostgresKnowledgeRepository


def parse_conditions(values: list[str]) -> tuple[tuple[str, str], ...]:
    """解析 KEY=VALUE，首个等号之后保留原值；重复键拒绝而不悄然覆盖。"""
    result: dict[str, str] = {}
    for value in values:
        key, separator, condition = value.partition("=")
        if not separator or key in result:
            raise GuideImportError("invalid_knowledge_query")
        result[key] = condition
    return tuple(sorted(result.items()))


async def run_command(arguments: argparse.Namespace) -> dict[str, JsonValue]:
    """演示只读虚构草稿，正常模式只读已迁移 PostgreSQL 的正式发布资料。"""
    # 先校验用户输入；缺数据库配置不能掩盖问题，也不静默回退演示。
    request = None
    scope = None
    query = None
    chunk_id = None
    if arguments.command == "prepare-question":
        request = await asyncio.to_thread(load_question, Path(arguments.path))
    else:
        scope = EvidenceScope(
            arguments.version,
            GuideKind(arguments.kind),
            arguments.entity,
            parse_conditions(arguments.condition),
            arguments.demo,
        )
        if arguments.command == "search":
            query = KeywordQuery(scope, tuple(arguments.keyword), arguments.limit)
        else:
            require_context_limit(arguments.max_chunks)
            try:
                chunk_id = UUID(arguments.evidence_id)
            except ValueError as exc:
                raise GuideImportError("invalid_evidence_id") from exc
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
        if request is not None:
            result = preparation_to_json(
                await QuestionPreparationService(reader, demo_only=arguments.demo).prepare(request)
            )
        elif query is not None:
            hits = await reader.search_knowledge(query)
            result = {
                "hits": [hit_to_json(hit) for hit in hits],
                "count": len(hits),
                "keywords": list(query.keywords),
            }
        elif chunk_id is not None and scope is not None:
            result = window_to_json(
                await reader.read_evidence(chunk_id, scope, max_chunks=arguments.max_chunks)
            )
        else:
            raise GuideImportError("invalid_knowledge_query")
        return {
            **result,
            "storage": "memory_demo" if arguments.demo else "postgres",
            "demo_only": arguments.demo,
            "model_called": False,
        }
    finally:
        if database is not None:
            await database.dispose()


def build_parser() -> argparse.ArgumentParser:
    """限制命令和参数集合；没有任意工具名称、URL、SQL 或 Python 执行入口。"""
    parser = argparse.ArgumentParser(description="知识检索与问答证据准备；不调用模型")
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search", help="显式关键词字面检索")
    search.add_argument("--keyword", action="append", required=True)
    search.add_argument("--limit", type=int, default=5)
    reader = commands.add_parser("read-evidence", help="按库内证据 ID 读取上下文")
    reader.add_argument("evidence_id")
    reader.add_argument("--max-chunks", type=int, default=20)
    prepare = commands.add_parser("prepare-question", help="追问字段或准备证据，不生成答案")
    prepare.add_argument("path", help="UTF-8 问题 JSON 文件路径")
    for command in (search, reader):
        command.add_argument("--version", required=True)
        command.add_argument("--kind", choices=[kind.value for kind in GuideKind], required=True)
        command.add_argument("--entity", required=True)
        command.add_argument("--condition", action="append", default=[])
    for command in (search, reader, prepare):
        command.add_argument("--demo", action="store_true", help="仅内存中的纯虚构样本")
    return parser


def main(argv: list[str] | None = None) -> int:
    """只将稳定错误输出到 stdout，不打印原始异常、路径、数据库 URL 或玩家内容。"""
    arguments = build_parser().parse_args(argv)
    try:
        result = asyncio.run(run_command(arguments))
    except GuideImportError as exc:
        result = {"error": exc.code}
        exit_code = 1
    except Exception:
        result = {"error": "knowledge_unavailable"}
        exit_code = 1
    else:
        exit_code = 0
    emit_json(result)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
