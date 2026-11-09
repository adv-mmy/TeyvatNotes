"""有界 RAG 检索及固定对比 CLI；不调用回答模型或付费 Embedding。"""

import argparse
import asyncio
import os

from teyvatnotes.application.knowledge_read_ports import KnowledgeReader
from teyvatnotes.application.rag_service import (
    RagIndexService,
    RagSearchService,
    read_context,
)
from teyvatnotes.database import Database, create_database
from teyvatnotes.domain import JsonValue
from teyvatnotes.domain.guides import GuideImportError, GuideKind
from teyvatnotes.domain.knowledge import EvidenceScope, require_context_limit
from teyvatnotes.domain.rag import RagQuery, RetrievalMode, require_budget
from teyvatnotes.knowledge.demo import create_demo_repository
from teyvatnotes.knowledge.evidence_json import hit_to_json, window_to_json
from teyvatnotes.knowledge.lexical_embedding import DemoLexicalEmbedding
from teyvatnotes.knowledge.output import emit_json
from teyvatnotes.knowledge.query_cli import parse_conditions
from teyvatnotes.knowledge.rag_evaluation import (
    CHARACTER_CONDITIONS,
    QUEST_CONDITIONS,
    compare_retrieval,
)
from teyvatnotes.repositories.knowledge_memory import InMemoryKnowledgeRepository
from teyvatnotes.repositories.knowledge_postgres import PostgresKnowledgeRepository
from teyvatnotes.repositories.rag_memory import InMemoryRagRepository
from teyvatnotes.repositories.rag_postgres import PostgresRagRepository


async def create_indexed_demo() -> tuple[
    InMemoryKnowledgeRepository, InMemoryRagRepository, RagSearchService
]:
    """索引两份长期教学快照；每次新进程内存独立，不写 PostgreSQL。"""
    knowledge = await create_demo_repository()
    repository = InMemoryRagRepository(knowledge)
    embedding = DemoLexicalEmbedding()
    indexer = RagIndexService(knowledge, repository, embedding)
    for kind, entity, conditions in (
        (GuideKind.CHARACTER, "示例角色甲", CHARACTER_CONDITIONS),
        (GuideKind.QUEST, "示例任务甲", QUEST_CONDITIONS),
    ):
        candidates = await repository.list_candidates(
            EvidenceScope("DEMO-ONLY", kind, entity, conditions, True)
        )
        for document_id in dict.fromkeys(hit.document.id for hit in candidates):
            await indexer.index_document(document_id)
    return knowledge, repository, RagSearchService(repository, embedding)


async def run_command(arguments: argparse.Namespace) -> dict[str, JsonValue]:
    database: Database | None = None
    require_budget(arguments.limit, 10)
    if arguments.command == "compare":
        if not arguments.demo:
            raise GuideImportError("retrieval_comparison_demo_required")
        knowledge, repository, service = await create_indexed_demo()
        return await compare_retrieval(service, repository, knowledge, limit=arguments.limit)
    require_context_limit(arguments.max_chunks)
    scope = EvidenceScope(
        arguments.version,
        GuideKind(arguments.kind),
        arguments.entity,
        parse_conditions(arguments.condition),
        arguments.demo,
    )
    query = RagQuery(
        scope,
        arguments.text,
        tuple(arguments.keyword),
        RetrievalMode(arguments.mode),
        arguments.limit,
    )
    # 当前第三方回答模型未声明 Embeddings 支持；正式向量模式不探测平台或发付费请求。
    if not arguments.demo and query.mode is not RetrievalMode.KEYWORD:
        raise GuideImportError("embedding_not_configured")
    reader: KnowledgeReader
    try:
        if arguments.demo:
            reader, _, service = await create_indexed_demo()
        else:
            url = os.getenv("DATABASE_URL")
            if not url:
                raise GuideImportError("database_not_configured")
            database = create_database(url)
            reader = PostgresKnowledgeRepository(database.session_factory)
            service = RagSearchService(PostgresRagRepository(database.session_factory), None)
        result = await service.search(query)
        hits: list[JsonValue] = []
        for hit in result.hits:
            window = await read_context(reader, hit, scope, max_chunks=arguments.max_chunks)
            hits.append(
                {
                    "evidence": hit_to_json(hit.evidence),
                    "context": window_to_json(window),
                    "keyword_score": hit.keyword_score,
                    "vector_score": hit.vector_score,
                    "keyword_rank": hit.keyword_rank,
                    "vector_rank": hit.vector_rank,
                    "fused_score": hit.fused_score,
                }
            )
        return {
            "mode": result.mode.value,
            "hits": hits,
            "candidate_count": result.candidate_count,
            "indexed_count": result.indexed_count,
            "profile_id": result.profile.identity if result.profile else None,
            "semantic_embedding": result.profile.semantic if result.profile else False,
            "storage": "memory_demo" if arguments.demo else "postgres",
            "demo_only": arguments.demo,
            "answer_model_called": False,
            "paid_embedding_called": False,
            "cost": None,
            "warnings": [*result.warnings, "not_a_generated_answer", "not_fact_verified"],
        }
    finally:
        if database is not None:
            await database.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RAG 离线验证：词面、向量及混合检索；不生成答案")
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search", help="有界检索，并显示引用及有序上下文")
    search.add_argument("--text", required=True)
    search.add_argument("--keyword", action="append", default=[])
    search.add_argument("--mode", choices=[mode.value for mode in RetrievalMode], default="hybrid")
    search.add_argument("--version", required=True)
    search.add_argument("--kind", choices=[kind.value for kind in GuideKind], required=True)
    search.add_argument("--entity", required=True)
    search.add_argument("--condition", action="append", default=[])
    search.add_argument("--max-chunks", type=int, default=20)
    compare = commands.add_parser("compare", help="12 个固定虚构问题；不是正式答案评测")
    for command in (search, compare):
        command.add_argument("--demo", action="store_true")
        command.add_argument("--limit", type=int, default=3)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        result = asyncio.run(run_command(arguments))
    except GuideImportError as exc:
        result, exit_code = {"error": exc.code}, 1
    except Exception:
        result, exit_code = {"error": "rag_unavailable"}, 1
    else:
        exit_code = 0
    emit_json(result)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
