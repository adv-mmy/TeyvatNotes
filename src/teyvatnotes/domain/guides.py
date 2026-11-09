"""攻略资料的来源、版本、证据位置与导入检查点。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from teyvatnotes.domain.json_types import JsonValue


class SourcePlatform(StrEnum):
    """当前支持的人工快照来源平台。"""

    BILIBILI = "bilibili"
    MIYOUSHE = "miyoushe"


class GuideKind(StrEnum):
    """资料面向的两种攻略用途。"""

    CHARACTER = "character"
    QUEST = "quest"


class DocumentStatus(StrEnum):
    """导入默认草稿，发布需经过独立校验。"""

    DRAFT = "draft"
    PUBLISHED = "published"


class StepName(StrEnum):
    """本周三个确定性的导入阶段，顺序也是恢复顺序。"""

    PARSE = "parse"
    CHUNK = "chunk"
    VALIDATE = "validate"


class StepStatus(StrEnum):
    """已持久化的阶段结果；没有记录表示尚未完成。"""

    COMPLETED = "completed"
    FAILED = "failed"


class GuideImportError(Exception):
    """只暴露稳定错误代码，不携带原始正文、路径或外部异常。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class GuideSource:
    """平台、原生标识和分 P 组成的来源身份。"""

    id: UUID
    platform: SourcePlatform
    native_id: str
    part_id: int
    canonical_url: str


@dataclass(frozen=True, slots=True)
class GuideSnapshot:
    """经过输入校验的本地快照；不代表已验证网站内容的真实性。"""

    source: GuideSource
    title: str
    author: str
    guide_kind: GuideKind
    game_version: str | None
    data: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class GuideDocument:
    """内容及处理配置共同标识一个不可变版本，状态允许显式发布。"""

    id: UUID
    source: GuideSource
    content_hash: str
    processing_version: str
    title: str
    author: str
    guide_kind: GuideKind
    game_version: str | None
    snapshot: dict[str, JsonValue]
    status: DocumentStatus
    created_at: datetime


@dataclass(frozen=True, slots=True)
class EvidenceBlock:
    """保持原文顺序、章节和位置的解析块。"""

    ordinal: int
    kind: str
    text: str
    section: str
    locator: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class GuideChunk:
    """具有稳定标识、原文位置与字符偏移的文本片段。"""

    id: UUID
    document_id: UUID
    ordinal: int
    text: str
    section: str
    locators: tuple[dict[str, JsonValue], ...]


@dataclass(frozen=True, slots=True)
class IngestionCheckpoint:
    """输入哈希、阶段结果及稳定失败代码的持久化快照。"""

    document_id: UUID
    name: StepName
    status: StepStatus
    input_hash: str
    output: dict[str, JsonValue]
    error_code: str | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ImportReport:
    """CLI 展示的导入摘要，不包含原始正文或异常信息。"""

    document_id: UUID
    status: DocumentStatus
    chunk_count: int
    blocking_issues: tuple[str, ...]
    warnings: tuple[str, ...]
    reused_steps: tuple[StepName, ...]


def require_publishable(
    document: GuideDocument, checkpoints: tuple[IngestionCheckpoint, ...], chunk_count: int
) -> None:
    """仓储共享的发布不变量，结构通过不等于游戏事实已验证。"""

    indexed = {checkpoint.name: checkpoint for checkpoint in checkpoints}
    if (
        not document.game_version
        or document.snapshot.get("is_fixture") is not False
        or document.snapshot.get("complete") is not True
        or chunk_count < 1
    ):
        raise GuideImportError("publication_blocked")
    if any(
        name not in indexed or indexed[name].status is not StepStatus.COMPLETED for name in StepName
    ):
        raise GuideImportError("publication_blocked")
    if indexed[StepName.VALIDATE].output.get("blocking_issues") != []:
        raise GuideImportError("publication_blocked")


def require_valid_checkpoint(
    checkpoint: IngestionCheckpoint, chunks: tuple[GuideChunk, ...]
) -> None:
    """保持内存/SQL 一致，切块和其完成记录必须描述同一组数据。"""

    if checkpoint.status is StepStatus.FAILED:
        if not checkpoint.error_code or chunks:
            raise GuideImportError("invalid_checkpoint")
        return
    if checkpoint.error_code is not None:
        raise GuideImportError("invalid_checkpoint")
    if checkpoint.name is not StepName.CHUNK:
        if chunks:
            raise GuideImportError("invalid_checkpoint")
        return
    count = checkpoint.output.get("chunk_count")
    if isinstance(count, bool) or not isinstance(count, int) or count != len(chunks):
        raise GuideImportError("invalid_checkpoint")
    if len({chunk.id for chunk in chunks}) != len(chunks):
        raise GuideImportError("invalid_checkpoint")
    if any(
        chunk.document_id != checkpoint.document_id
        or chunk.ordinal != ordinal
        or not chunk.text.strip()
        or not chunk.locators
        for ordinal, chunk in enumerate(chunks)
    ):
        raise GuideImportError("invalid_checkpoint")
