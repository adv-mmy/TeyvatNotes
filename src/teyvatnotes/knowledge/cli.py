"""离线快照 CLI：只读本地文件，显式选择内存或已迁移 PostgreSQL。"""

import argparse
import asyncio
import os
from pathlib import Path
from uuid import UUID

from teyvatnotes.application.guide_ingestion import GuideIngestionService
from teyvatnotes.application.knowledge_ports import KnowledgeRepository
from teyvatnotes.database import Database, create_database
from teyvatnotes.domain.guides import GuideImportError, ImportReport
from teyvatnotes.domain.task import JsonValue
from teyvatnotes.knowledge.output import emit_json
from teyvatnotes.knowledge.snapshots import load_snapshot
from teyvatnotes.knowledge.terminology import load_catalog
from teyvatnotes.repositories.knowledge_memory import InMemoryKnowledgeRepository
from teyvatnotes.repositories.knowledge_postgres import PostgresKnowledgeRepository


def report_to_json(report: ImportReport) -> dict[str, JsonValue]:
    return {
        "document_id": str(report.document_id),
        "status": report.status.value,
        "chunk_count": report.chunk_count,
        "blocking_issues": list(report.blocking_issues),
        "warnings": list(report.warnings),
        "reused_steps": [name.value for name in report.reused_steps],
    }


async def run_command(arguments: argparse.Namespace) -> dict[str, JsonValue]:
    database: Database | None = None
    repository: KnowledgeRepository
    if arguments.memory:
        repository = InMemoryKnowledgeRepository()
    else:
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise GuideImportError("database_not_configured")
        database = create_database(database_url)
        repository = PostgresKnowledgeRepository(database.session_factory)
    try:
        if arguments.command == "import-snapshot":
            # 文件读取可能阻塞；在线程中完成边界校验，事件循环只等待结果。
            snapshot = await asyncio.to_thread(load_snapshot, Path(arguments.path))
            catalog = await asyncio.to_thread(
                load_catalog, Path(arguments.terms) if arguments.terms else None
            )
            report = await GuideIngestionService(
                repository, terminology_catalog=catalog
            ).import_snapshot(snapshot, publish=arguments.publish)
            return {
                **report_to_json(report),
                "storage": "memory" if arguments.memory else "postgres",
            }
        try:
            document_id = UUID(arguments.document_id)
        except ValueError as exc:
            raise GuideImportError("invalid_document_id") from exc
        document = await repository.get_document(document_id)
        chunks = await repository.list_chunks(document_id)
        evidence: list[JsonValue] = [
            {
                "chunk_id": str(chunk.id),
                "ordinal": chunk.ordinal,
                "section": chunk.section,
                "text": chunk.text,
                "locators": list(chunk.locators),
            }
            for chunk in chunks
        ]
        return {
            "document_id": str(document.id),
            "status": document.status.value,
            "title": document.title,
            "game_version": document.game_version,
            "source_url": document.source.canonical_url,
            "chunks": evidence,
        }
    finally:
        if database is not None:
            await database.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="原神攻略资料离线导入；不抓取网站、不调用模型")
    commands = parser.add_subparsers(dest="command", required=True)
    importer = commands.add_parser("import-snapshot", help="导入人工整理的 JSON 快照")
    importer.add_argument("path", help="UTF-8 JSON 文件路径")
    importer.add_argument("--publish", action="store_true", help="显式请求发布，缺口会阻断")
    importer.add_argument("--terms", help="开发者UTF-8专有名词表路径；默认用包内词表")
    viewer = commands.add_parser("show", help="查看已保存版本与证据位置")
    viewer.add_argument("document_id")
    for command in (importer, viewer):
        command.add_argument("--memory", action="store_true", help="仅当前进程内演示，不持久化")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        result = asyncio.run(run_command(arguments))
    except GuideImportError as exc:
        result = {"error": exc.code}
        exit_code = 1
    except Exception:
        result = {"error": "import_unavailable"}
        exit_code = 1
    else:
        exit_code = 0
    emit_json(result)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
