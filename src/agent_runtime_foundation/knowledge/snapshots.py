"""校验人工准备的 UTF-8 JSON 快照，不把它当作网站抓取器。"""

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal, Self, cast
from urllib.parse import parse_qs, urlsplit
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from agent_runtime_foundation.domain.guides import (
    GuideImportError,
    GuideKind,
    GuideSnapshot,
    GuideSource,
    SourcePlatform,
)
from agent_runtime_foundation.domain.task import JsonValue

MAX_SNAPSHOT_BYTES = 1_048_576
NonBlank = Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")]


class SnapshotBase(BaseModel):
    """所有来源共用的人工声明及元数据，拒绝未知字段。"""

    model_config = ConfigDict(extra="forbid")

    native_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    url: str = Field(max_length=2048)
    title: NonBlank
    author: NonBlank
    guide_kind: GuideKind
    game_version: NonBlank | None = None
    published_at: datetime | None = None
    complete: bool = Field(default=False, strict=True)
    is_fixture: bool = Field(default=False, strict=True)
    entities: list[NonBlank] = Field(default_factory=list, max_length=30)
    conditions: dict[NonBlank, NonBlank] = Field(default_factory=dict, max_length=30)
    content_nature: Literal["released", "preview"] = "released"
    evidence_category: Literal["official", "community_test", "recommendation"] = "recommendation"

    @model_validator(mode="after")
    def reject_naive_publication_time(self) -> Self:
        if self.published_at is not None and self.published_at.utcoffset() is None:
            raise ValueError("published_at 必须包含时区")
        return self


class SubtitleSegment(BaseModel):
    """一个非空字幕及其已声明的章节、有限时间范围。"""

    model_config = ConfigDict(extra="forbid")

    start: float = Field(ge=0, allow_inf_nan=False, strict=True)
    end: float = Field(gt=0, allow_inf_nan=False, strict=True)
    text: NonBlank
    section: NonBlank = "字幕"

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.end <= self.start:
            raise ValueError("字幕结束时间必须晚于开始时间")
        return self


class BilibiliSnapshot(SnapshotBase):
    """B 站人工字幕快照，显式区分视频分 P。"""

    platform: Literal["bilibili"]
    part_id: int = Field(default=1, ge=1, le=1000, strict=True)
    segments: list[SubtitleSegment] = Field(default_factory=list, max_length=5000)
    requires_visual: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        for previous, current in zip(self.segments, self.segments[1:], strict=False):
            if current.start < previous.end:
                raise ValueError("字幕必须按时间排列且不能重叠")
        return self


class ArticleBlock(BaseModel):
    """文章中的标题、段落或只保存引用的图片块。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["heading", "paragraph", "image"]
    text: str = Field(default="", max_length=20_000)
    image_url: str | None = Field(default=None, max_length=2048)
    requires_visual: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def validate_content(self) -> Self:
        if self.kind == "image":
            if self.image_url is None:
                raise ValueError("图片块需要 image_url")
            image = urlsplit(self.image_url)
            if image.scheme != "https" or not image.hostname or image.username or image.password:
                raise ValueError("图片只允许无凭据的 HTTPS 引用")
        elif not self.text.strip() or self.image_url is not None or self.requires_visual:
            raise ValueError("文字块必须非空，且不能携带图片字段")
        return self


class MiyousheSnapshot(SnapshotBase):
    """按原文顺序整理的米游社正文快照，不是 HTML 抓取结果。"""

    platform: Literal["miyoushe"]
    article_blocks: list[ArticleBlock] = Field(default_factory=list, max_length=5000)


SnapshotSchema = Annotated[BilibiliSnapshot | MiyousheSnapshot, Field(discriminator="platform")]
_snapshot_adapter: TypeAdapter[BilibiliSnapshot | MiyousheSnapshot] = TypeAdapter(SnapshotSchema)


def _canonical_url(value: BilibiliSnapshot | MiyousheSnapshot) -> str:
    parsed = urlsplit(value.url)
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise ValueError("来源必须是无凭据的标准 HTTPS 链接")
    if isinstance(value, BilibiliSnapshot):
        if parsed.hostname not in {"www.bilibili.com", "bilibili.com"}:
            raise ValueError("B 站来源域名不匹配")
        path = f"/video/{value.native_id}"
        parts = parse_qs(parsed.query).get("p")
        if parts is not None and parts != [str(value.part_id)]:
            raise ValueError("链接分 P 与 part_id 不匹配")
        canonical = f"https://www.bilibili.com{path}?p={value.part_id}"
    else:
        if parsed.hostname not in {"www.miyoushe.com", "miyoushe.com"}:
            raise ValueError("米游社来源域名不匹配")
        path = f"/ys/article/{value.native_id}"
        canonical = f"https://www.miyoushe.com{path}"
    if parsed.path.rstrip("/") != path:
        raise ValueError("来源路径与 native_id 不匹配")
    return canonical


def parse_snapshot(value: object) -> GuideSnapshot:
    """转换边界输入；跟踪参数不进入存储，分 P 身份来自显式 part_id。"""

    try:
        schema = _snapshot_adapter.validate_python(value)
        canonical = _canonical_url(schema)
    except (ValidationError, ValueError) as exc:
        raise GuideImportError("invalid_snapshot") from exc
    platform = SourcePlatform(schema.platform)
    part_id = schema.part_id if isinstance(schema, BilibiliSnapshot) else 1
    identity = f"{platform.value}:{schema.native_id}:{part_id}"
    source = GuideSource(
        uuid5(NAMESPACE_URL, identity), platform, schema.native_id, part_id, canonical
    )
    data = cast(dict[str, JsonValue], schema.model_dump(mode="json"))
    data["url"] = canonical
    return GuideSnapshot(
        source, schema.title, schema.author, schema.guide_kind, schema.game_version, data
    )


def load_snapshot(path: Path) -> GuideSnapshot:
    """有界读取本地文件，不下载 URL，也不输出文件内容或路径到错误中。"""

    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_SNAPSHOT_BYTES + 1)
        if len(raw) > MAX_SNAPSHOT_BYTES:
            raise GuideImportError("snapshot_too_large")
        value: object = json.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise GuideImportError("snapshot_unreadable") from exc
    return parse_snapshot(value)
