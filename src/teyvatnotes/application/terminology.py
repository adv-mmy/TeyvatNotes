"""入库名称校对与审计；原正文保留，未知词不伪装成已经验证。"""

from copy import deepcopy
from dataclasses import replace

from teyvatnotes.domain.guides import EvidenceBlock, GuideKind, GuideSnapshot
from teyvatnotes.domain.json_types import JsonValue
from teyvatnotes.domain.terminology import (
    TermCategory,
    TerminologyCatalog,
    TermResolution,
    valid_name,
)
from teyvatnotes.knowledge.parsing import json_list, json_object, json_text


def resolution_to_json(resolution: TermResolution) -> dict[str, JsonValue]:
    return {
        "original": resolution.original,
        "name": resolution.name,
        "status": resolution.status,
        "candidates": list(resolution.candidates),
        "suggestions_limited": resolution.suggestions_limited,
    }


def prepare_terminology(snapshot: GuideSnapshot, catalog: TerminologyCatalog) -> GuideSnapshot:
    """在身份计算前附完整词表和实体审计；不改写原文章、URL、版本或玩家条件。"""
    data = deepcopy(snapshot.data)
    original_entities = json_list(data.get("entities", []))
    entity_resolutions: list[JsonValue] = []
    entities: list[JsonValue] = []
    for entity in original_entities:
        name = json_text(entity)
        if snapshot.guide_kind is GuideKind.CHARACTER:
            resolution = (
                catalog.resolve(name, TermCategory.CHARACTER)
                if valid_name(name)
                else TermResolution(name, name, "needs_review")
            )
            entity_resolutions.append(resolution_to_json(resolution))
            name = resolution.name
        if name not in entities:
            entities.append(name)
    declared: list[JsonValue] = []
    for item in json_list(data.get("recognized_terms", [])):
        row = json_object(item)
        category = TermCategory(json_text(row["category"]))
        declared.append(
            {
                "category": category.value,
                **resolution_to_json(catalog.resolve(json_text(row["name"]), category)),
            }
        )
    data["entities"] = entities
    data["_terminology"] = {
        "policy": "confirmed-exact-v1",
        "catalog": catalog.to_json(),
        "catalog_hash": catalog.content_hash,
        "raw_entities": original_entities,
        "entity_resolutions": entity_resolutions,
        "recognized_term_resolutions": declared,
    }
    return replace(snapshot, data=data)


def correct_blocks(
    blocks: tuple[EvidenceBlock, ...], catalog: TerminologyCatalog
) -> tuple[tuple[EvidenceBlock, ...], tuple[str, ...]]:
    """校对已解析块；替换变长时用normalized_text坐标，不冒充原文字符偏移。"""
    corrected: list[EvidenceBlock] = []
    reviews: list[str] = []
    for block in blocks:
        result = catalog.correct_text(block.text)
        section = catalog.correct_text(block.section)
        reviews.extend((*result.ambiguous_names, *section.ambiguous_names))
        locator = block.locator.copy()
        if result.edits or section.edits:
            locator["terminology"] = {
                "catalog_hash": catalog.content_hash,
                "source_text": block.text,
                "source_section": block.section,
                "offset_basis": "normalized_text",
                "edits": [
                    {
                        "original": edit.original,
                        "replacement": edit.replacement,
                        "source_start": edit.source_start,
                        "source_end": edit.source_end,
                        "normalized_start": edit.normalized_start,
                        "normalized_end": edit.normalized_end,
                    }
                    for edit in result.edits
                ],
            }
        corrected.append(replace(block, text=result.text, section=section.text, locator=locator))
    return tuple(corrected), tuple(dict.fromkeys(reviews))
