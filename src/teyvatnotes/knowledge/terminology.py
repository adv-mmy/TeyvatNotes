"""有界读取开发者维护的 UTF-8 词表；数据错误不退回空词表。"""

import json
from importlib.resources import files
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from teyvatnotes.domain.guides import GuideImportError
from teyvatnotes.domain.terminology import (
    TermCategory,
    TermDefinition,
    TerminologyCatalog,
)

MAX_CATALOG_BYTES = 1_048_576
Name = Annotated[str, Field(strict=True, min_length=1, max_length=80)]


class TermSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: TermCategory
    name: Name
    aliases: list[Name] = Field(default_factory=list, max_length=8)
    confirmation: str = Field(strict=True, min_length=1, max_length=400)


class CatalogSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(strict=True, min_length=1, max_length=40)
    terms: list[TermSchema] = Field(max_length=2000)


def parse_catalog(value: object) -> TerminologyCatalog:
    try:
        schema = CatalogSchema.model_validate(value)
        return TerminologyCatalog(
            schema.revision,
            tuple(
                TermDefinition(row.category, row.name, tuple(row.aliases), row.confirmation)
                for row in schema.terms
            ),
        )
    except (ValidationError, ValueError, TypeError):
        raise GuideImportError("invalid_terminology_catalog") from None


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def load_catalog(path: Path | None = None) -> TerminologyCatalog:
    """默认随 Python 包携带；可显式传开发者词表路径，不访问网络或模型配置。"""
    target = (
        path
        if path is not None
        else files("teyvatnotes.knowledge").joinpath("data", "terminology.json")
    )
    try:
        with target.open("rb") as stream:
            raw = stream.read(MAX_CATALOG_BYTES + 1)
        if len(raw) > MAX_CATALOG_BYTES:
            raise GuideImportError("terminology_catalog_too_large")
        return parse_catalog(json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_keys))
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise GuideImportError("terminology_catalog_unreadable") from None
