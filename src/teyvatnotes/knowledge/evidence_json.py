"""从库内来源和定位信息生成可追溯 JSON；不采信模型提供的链接。"""

import math
from urllib.parse import urlsplit, urlunsplit

from teyvatnotes.application.question_preparation import QuestionPreparation
from teyvatnotes.domain.guides import GuideChunk, SourcePlatform
from teyvatnotes.domain.json_types import JsonValue
from teyvatnotes.domain.knowledge import EvidenceHit, EvidenceWindow


def chunk_to_json(chunk: GuideChunk) -> dict[str, JsonValue]:
    """显式输出证据及定位，不暴露整个原始快照。"""
    return {
        "evidence_id": str(chunk.id),
        "ordinal": chunk.ordinal,
        "section": chunk.section,
        "text": chunk.text,
        "locators": list(chunk.locators),
    }


def hit_to_json(hit: EvidenceHit) -> dict[str, JsonValue]:
    """视频时间链接来自已保存位置；文章使用原文链接+段落/图片位置，不发明锚点。"""
    document, chunk = hit.document, hit.chunk
    link = document.source.canonical_url
    if document.source.platform is SourcePlatform.BILIBILI and chunk.locators:
        start = chunk.locators[0].get("start")
        if (
            isinstance(start, (float, int))
            and not isinstance(start, bool)
            and math.isfinite(start)
            and start >= 0
        ):
            parsed = urlsplit(link)
            link = urlunsplit(
                (
                    parsed.scheme,
                    parsed.netloc,
                    parsed.path,
                    f"p={document.source.part_id}&t={int(start)}",
                    "",
                )
            )
    return {
        **chunk_to_json(chunk),
        "document_id": str(document.id),
        "title": document.title,
        "author": document.author,
        "game_version": document.game_version,
        "guide_kind": document.guide_kind.value,
        "source_url": document.source.canonical_url,
        "citation_url": link,
        "conditions": document.snapshot.get("conditions"),
        "evidence_category": document.snapshot.get("evidence_category"),
        "content_nature": document.snapshot.get("content_nature"),
        "score": hit.score,
        "status": document.status.value,
        "is_fixture": document.snapshot.get("is_fixture"),
    }


def window_to_json(window: EvidenceWindow) -> dict[str, JsonValue]:
    """返回实际命中块与有序上下文，截断标志不随文字展示丢失。"""
    return {
        "hit": hit_to_json(window.hit),
        "context": [chunk_to_json(chunk) for chunk in window.chunks],
        "context_truncated": window.context_truncated,
    }


def preparation_to_json(preparation: QuestionPreparation) -> dict[str, JsonValue]:
    """没有 answer 字段；证据就绪不能被客户端误判为生成完成。"""
    warnings: list[JsonValue] = ["not_a_generated_answer", "not_fact_verified"]
    if preparation.demo_only:
        warnings.append("fictional_demo_only")
    return {
        "status": preparation.status,
        "questions": list(preparation.questions),
        "reason": preparation.reason,
        "evidence": [window_to_json(w) for w in preparation.evidence],
        "demo_only": preparation.demo_only,
        "model_called": False,
        "warnings": warnings,
    }
