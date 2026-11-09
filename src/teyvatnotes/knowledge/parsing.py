"""保持字幕时间、文章顺序与章节关系的离线解析和字符切块。"""

from uuid import UUID, uuid5

from teyvatnotes.domain import JsonValue
from teyvatnotes.domain.guides import (
    EvidenceBlock,
    GuideChunk,
    GuideImportError,
    GuideSnapshot,
    SourcePlatform,
)


def json_object(value: JsonValue) -> dict[str, JsonValue]:
    """校验 JSON 对象并缩窄类型，失败返回稳定检查点错误。"""
    if not isinstance(value, dict):
        raise GuideImportError("invalid_checkpoint")
    return value


def json_list(value: JsonValue) -> list[JsonValue]:
    """校验 JSON 数组并缩窄类型。"""
    if not isinstance(value, list):
        raise GuideImportError("invalid_checkpoint")
    return value


def json_text(value: JsonValue) -> str:
    """校验 JSON 字符串并缩窄类型。"""
    if not isinstance(value, str):
        raise GuideImportError("invalid_checkpoint")
    return value


class OfflineSnapshotParser:
    """只解析已校验快照，图片保留位置，不声称完成 OCR 或视觉理解。"""

    def parse(self, snapshot: GuideSnapshot) -> tuple[EvidenceBlock, ...]:
        blocks: list[EvidenceBlock] = []
        section = "正文"
        if snapshot.source.platform is SourcePlatform.BILIBILI:
            for ordinal, item in enumerate(json_list(snapshot.data["segments"])):
                segment = json_object(item)
                blocks.append(
                    EvidenceBlock(
                        ordinal,
                        "subtitle",
                        json_text(segment["text"]),
                        json_text(segment["section"]),
                        {
                            "part_id": snapshot.source.part_id,
                            "start": segment["start"],
                            "end": segment["end"],
                            "segment_index": ordinal,
                        },
                    )
                )
        else:
            paragraph_index = 0
            image_index = 0
            for ordinal, item in enumerate(json_list(snapshot.data["article_blocks"])):
                block = json_object(item)
                kind = json_text(block["kind"])
                text = json_text(block["text"])
                locator: dict[str, JsonValue] = {"block_index": ordinal}
                if kind == "heading":
                    section = text.strip()
                elif kind == "paragraph":
                    paragraph_index += 1
                    locator["paragraph_index"] = paragraph_index
                else:
                    image_index += 1
                    locator.update(
                        {
                            "image_index": image_index,
                            "image_url": block["image_url"],
                            "requires_visual": block["requires_visual"],
                        }
                    )
                blocks.append(EvidenceBlock(ordinal, kind, text, section, locator))
        return tuple(blocks)


def blocks_to_json(blocks: tuple[EvidenceBlock, ...]) -> list[JsonValue]:
    """将解析块显式转换为可保存到 JSONB 的阶段产物。"""
    return [
        {
            "ordinal": block.ordinal,
            "kind": block.kind,
            "text": block.text,
            "section": block.section,
            "locator": block.locator,
        }
        for block in blocks
    ]


def blocks_from_json(value: JsonValue) -> tuple[EvidenceBlock, ...]:
    """从持久化阶段产物恢复解析块，不重新读取或解析来源。"""
    result: list[EvidenceBlock] = []
    for item in json_list(value):
        block = json_object(item)
        ordinal = block["ordinal"]
        if isinstance(ordinal, bool) or not isinstance(ordinal, int):
            raise GuideImportError("invalid_checkpoint")
        result.append(
            EvidenceBlock(
                ordinal,
                json_text(block["kind"]),
                json_text(block["text"]),
                json_text(block["section"]),
                json_object(block["locator"]),
            )
        )
    return tuple(result)


def chunk_blocks(
    document_id: UUID, blocks: tuple[EvidenceBlock, ...], *, max_chars: int
) -> tuple[GuideChunk, ...]:
    """按正文块切分，保持章节和位置；字符上限不是模型 Token 上限。"""

    chunks: list[GuideChunk] = []
    for block in blocks:
        if block.kind == "heading" or not block.text.strip():
            continue
        for start in range(0, len(block.text), max_chars):
            end = min(start + max_chars, len(block.text))
            text = block.text[start:end].strip()
            if not text:
                continue
            ordinal = len(chunks)
            locator = {
                **block.locator,
                "block_index": block.ordinal,
                "start_char": start,
                "end_char": end,
            }
            chunks.append(
                GuideChunk(
                    uuid5(document_id, str(ordinal)),
                    document_id,
                    ordinal,
                    text,
                    block.section,
                    (locator,),
                )
            )
    return tuple(chunks)
