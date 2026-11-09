"""开发者确认的专有名词及确定性校对；不猜测新角色或游戏机制。"""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import StrEnum
from types import MappingProxyType
from typing import Literal

from teyvatnotes.domain import JsonValue
from teyvatnotes.domain.guides import GuideImportError


class TermCategory(StrEnum):
    CHARACTER = "character"
    WEAPON = "weapon"
    ARTIFACT = "artifact"


def valid_name(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 80
        and value == value.strip()
        and not any(ord(char) < 32 for char in value)
    )


@dataclass(frozen=True, slots=True)
class TermDefinition:
    category: TermCategory
    name: str
    aliases: tuple[str, ...] = ()
    confirmation: str = ""

    def __post_init__(self) -> None:
        if (
            not isinstance(self.category, TermCategory)
            or not valid_name(self.name)
            or not isinstance(self.aliases, tuple)
            or len(self.aliases) > 8
            or any(not valid_name(alias) or len(alias) < 2 for alias in self.aliases)
            or len(set((self.name, *self.aliases))) != len(self.aliases) + 1
            or not isinstance(self.confirmation, str)
            or not 1 <= len(self.confirmation) <= 400
        ):
            raise GuideImportError("invalid_terminology_catalog")


@dataclass(frozen=True, slots=True)
class TermResolution:
    original: str
    name: str
    status: Literal["canonical", "corrected", "needs_review"]
    candidates: tuple[str, ...] = ()
    suggestions_limited: bool = False


@dataclass(frozen=True, slots=True)
class NameEdit:
    original: str
    replacement: str
    source_start: int
    source_end: int
    normalized_start: int
    normalized_end: int


@dataclass(frozen=True, slots=True)
class TextCorrection:
    text: str
    edits: tuple[NameEdit, ...]
    ambiguous_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TerminologyCatalog:
    """类别内名称唯一；正文跨类别有歧义时不替换，相似度仅供人工参考。"""

    revision: str
    terms: tuple[TermDefinition, ...]
    _forms: Mapping[str, tuple[TermDefinition, ...]] = field(init=False, repr=False, compare=False)
    _pattern: re.Pattern[str] | None = field(init=False, repr=False, compare=False)
    _content_hash: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.revision, str)
            or re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", self.revision) is None
            or not isinstance(self.terms, tuple)
            or len(self.terms) > 2000
            or any(not isinstance(term, TermDefinition) for term in self.terms)
        ):
            raise GuideImportError("invalid_terminology_catalog")
        identities: set[tuple[TermCategory, str]] = set()
        forms: dict[str, list[TermDefinition]] = {}
        for term in self.terms:
            for spelling in (term.name, *term.aliases):
                identity = (term.category, spelling)
                if identity in identities:
                    raise GuideImportError("ambiguous_terminology_catalog")
                identities.add(identity)
                forms.setdefault(spelling, []).append(term)
        if len(identities) > 5000:
            raise GuideImportError("terminology_catalog_too_large")
        object.__setattr__(
            self, "_forms", MappingProxyType({key: tuple(rows) for key, rows in forms.items()})
        )
        # 同时加入标准名称保护长专名；最长优先且替换只走一遍，不级联替换。
        alternatives = "|".join(
            r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])"
            if name.isascii()
            else re.escape(name)
            for name in sorted(forms, key=lambda s: (-len(s), s))
        )
        pattern = re.compile("(?:" + alternatives + ")")
        object.__setattr__(self, "_pattern", pattern if forms else None)
        encoded = json.dumps(
            self.to_json(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        object.__setattr__(
            self, "_content_hash", hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        )

    def to_json(self) -> dict[str, JsonValue]:
        """保存完整已确认词表，旧版本可独立解释；顺序不影响内容摘要。"""
        rows: list[JsonValue] = []
        for term in sorted(self.terms, key=lambda row: (row.category.value, row.name)):
            aliases: list[JsonValue] = [alias for alias in sorted(term.aliases)]
            rows.append(
                {
                    "category": term.category.value,
                    "name": term.name,
                    "aliases": aliases,
                    "confirmation": term.confirmation,
                }
            )
        return {"revision": self.revision, "terms": rows}

    @property
    def content_hash(self) -> str:
        return self._content_hash

    def resolve(self, name: str, category: TermCategory | None = None) -> TermResolution:
        """仅精确登记的别名可自动更正；未知项保留原名，最多给出三个近似候选。"""
        if not valid_name(name) or (
            category is not None and not isinstance(category, TermCategory)
        ):
            raise GuideImportError("invalid_terminology_name")
        matches = tuple(
            row for row in self._forms.get(name, ()) if category is None or row.category is category
        )
        canonical = tuple(sorted({row.name for row in matches}))
        if len(canonical) == 1:
            target = canonical[0]
            return TermResolution(name, target, "canonical" if name == target else "corrected")
        if canonical:
            return TermResolution(name, name, "needs_review", canonical[:3])
        forms = tuple(
            (form, term.name)
            for term in self.terms
            if category is None or term.category is category
            for form in (term.name, *term.aliases)
        )
        # 不截取词表前缀产生有偏候选；超预算时仍可用精确索引，未知名留给人工。
        if len(forms) > 256:
            return TermResolution(name, name, "needs_review", suggestions_limited=True)
        ranked = sorted(
            ((SequenceMatcher(None, name, form).ratio(), target) for form, target in forms),
            key=lambda item: (-item[0], item[1]),
        )
        candidates = tuple(dict.fromkeys(target for score, target in ranked if score >= 0.6))[:3]
        return TermResolution(name, name, "needs_review", candidates)

    def correct_text(self, text: str) -> TextCorrection:
        """正文只查登记的完整词形；这不是从任意中文句子抽取未知实体的解析器。"""
        if not isinstance(text, str) or len(text) > 20_000:
            raise GuideImportError("terminology_text_too_large")
        if self._pattern is None:
            return TextCorrection(text, (), ())
        parts: list[str] = []
        edits: list[NameEdit] = []
        ambiguous: list[str] = []
        source_cursor = normalized_cursor = 0
        for match in self._pattern.finditer(text):
            prefix = text[source_cursor : match.start()]
            parts.append(prefix)
            normalized_cursor += len(prefix)
            resolved = self.resolve(match.group())
            neighbours = (
                text[max(0, match.start() - 1) : match.start()]
                + text[match.end() : match.end() + 1]
            )
            # 中文没有天然词边界。无法证明这是完整名词时，不替换更长未知名称的一部分。
            if resolved.status == "corrected" and any(
                char.isalpha() and not char.isascii() for char in neighbours
            ):
                resolved = TermResolution(
                    resolved.original, resolved.original, "needs_review", (resolved.name,)
                )
            parts.append(resolved.name)
            if resolved.status == "corrected":
                edits.append(
                    NameEdit(
                        match.group(),
                        resolved.name,
                        match.start(),
                        match.end(),
                        normalized_cursor,
                        normalized_cursor + len(resolved.name),
                    )
                )
            elif resolved.status == "needs_review":
                ambiguous.append(match.group())
            normalized_cursor += len(resolved.name)
            source_cursor = match.end()
        parts.append(text[source_cursor:])
        return TextCorrection("".join(parts), tuple(edits), tuple(dict.fromkeys(ambiguous)))
