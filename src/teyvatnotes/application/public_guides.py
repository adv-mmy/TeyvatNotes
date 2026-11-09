"""公开攻略问答：库内优先，仅在明确未命中时调用获授权资料端口。"""

import asyncio
import re
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from time import monotonic
from typing import Literal, Protocol

from teyvatnotes.application.guide_answering import (
    AnswerDecoder,
    AnswerResult,
    GuideAnswerService,
    missing_player_conditions,
)
from teyvatnotes.application.guide_ingestion import GuideIngestionService
from teyvatnotes.application.knowledge_read_ports import KnowledgeReader
from teyvatnotes.application.model_ports import ModelClient
from teyvatnotes.application.question_preparation import (
    QuestionPreparation,
    QuestionPreparationService,
    QuestionRequest,
)
from teyvatnotes.domain.guides import DocumentStatus, GuideImportError, GuideKind, GuideSnapshot
from teyvatnotes.domain.knowledge import bounded_text, conditions_are_valid
from teyvatnotes.domain.terminology import TermCategory, TerminologyCatalog
from teyvatnotes.knowledge.snapshots import parse_snapshot
from teyvatnotes.knowledge.terminology import load_catalog

SOURCE_TIMEOUT_SECONDS = 15
SOURCE_MAX_BYTES = 128 * 1024
MAX_CANDIDATES = 3
MAX_CONCURRENT_LOOKUPS = 2
LOOKUP_COOLDOWN_SECONDS = 30
MAX_RECENT_LOOKUPS = 128
_VERSION = re.compile(r"[0-9]{1,3}\.[0-9]{1,3}(?:\.[0-9]{1,3})?")
_ARTICLE = re.compile(r"https://www\.miyoushe\.com/ys/article/([1-9][0-9]{0,19})")


@dataclass(frozen=True, slots=True)
class SupplementScope:
    """授权资料源只能收到规范角色、明确版本和固定的配队目标。"""

    entity: str
    game_version: str
    goal: Literal["配队"] = "配队"


@dataclass(frozen=True, slots=True)
class SourceCandidate:
    """发现结果仅供选择正文，不是可用于回答的证据。"""

    url: str


@dataclass(frozen=True, slots=True)
class SourceArticle:
    """授权适配器取得的正文；关键前提或冲突核对不明确时拒绝发布。"""

    title: str
    author: str
    entity: str
    game_version: str
    version_evidence: str
    paragraphs: tuple[str, ...]
    conditions: tuple[tuple[str, str], ...] = ()
    complete: bool = False
    conditions_confirmed: bool = False
    conflicts_resolved: bool = False
    content_nature: Literal["released", "preview"] = "released"


class AuthorizedGuideSource(Protocol):
    """受信部署者显式注入；不提供公开配置入口或默认联网实现。"""

    async def search(self, scope: SupplementScope) -> tuple[SourceCandidate, ...]:
        """单次发现，返回有界的候选；失败时抛出 GuideSourceUnavailable。"""

    async def read(self, candidate: SourceCandidate) -> SourceArticle | None:
        """只读取这个规范来源，不重定向；受上游授权和正文大小约束。"""


class GuideSourceUnavailable(Exception):
    """资料源传输或协议故障；原始异常内容不得向客户端转发。"""


def _article_id(candidate: SourceCandidate) -> str | None:
    if not isinstance(candidate, SourceCandidate) or not isinstance(candidate.url, str):
        return None
    match = _ARTICLE.fullmatch(candidate.url) if len(candidate.url) <= 80 else None
    return match[1] if match else None


def _snapshot(
    scope: SupplementScope, candidate: SourceCandidate, article: SourceArticle
) -> GuideSnapshot | None:
    """做确定性准入筛选，不把适配器声明描述成语义或游戏事实验证。"""

    article_id = _article_id(candidate)
    if (
        article_id is None
        or not bounded_text(article.title, 300)
        or not bounded_text(article.author, 80)
        or article.entity != scope.entity
        or article.game_version != scope.game_version
        or article.complete is not True
        or article.conditions_confirmed is not True
        or article.conflicts_resolved is not True
        or not conditions_are_valid(article.conditions)
        or not bounded_text(article.version_evidence, 300)
        or scope.game_version
        not in re.findall(
            r"(?<![0-9.])[0-9]+\.[0-9]+(?:\.[0-9]+)?(?![0-9.])", article.version_evidence
        )
        or not isinstance(article.paragraphs, tuple)
        or not 1 <= len(article.paragraphs) <= 100
        or any(not bounded_text(text, 2_000) for text in article.paragraphs)
    ):
        return None
    try:
        if sum(len(text.encode("utf-8")) for text in article.paragraphs) > SOURCE_MAX_BYTES:
            return None
    except UnicodeError:
        return None
    body = "\n".join(article.paragraphs)
    if (
        scope.entity not in body
        or len(body) < 24
        or not any(word in body for word in ("配队", "阵容"))
    ):
        return None
    try:
        return parse_snapshot(
            {
                "platform": "miyoushe",
                "native_id": article_id,
                "url": candidate.url,
                "title": article.title,
                "author": article.author,
                "guide_kind": "character",
                "game_version": scope.game_version,
                "entities": [scope.entity],
                "conditions": dict(article.conditions),
                "content_nature": article.content_nature,
                "complete": True,
                "recognized_terms": [{"category": "character", "name": scope.entity}],
                "article_blocks": [
                    {"kind": "paragraph", "text": text} for text in article.paragraphs
                ],
            }
        )
    except GuideImportError:
        return None


_Outcome = Literal["published", "no_eligible_source", "source_conflict"]


@dataclass(slots=True)
class _Flight:
    task: asyncio.Task[_Outcome]
    waiters: int = 0


class PublicGuideService:
    """单实例攻略编排；每轮复用已有检索、单次模型调用和引用校验。"""

    def __init__(
        self,
        reader: KnowledgeReader,
        ingestion_service: GuideIngestionService,
        *,
        decoder: AnswerDecoder,
        source: AuthorizedGuideSource | None = None,
        catalog: TerminologyCatalog | None = None,
    ) -> None:
        self._local = QuestionPreparationService(reader)
        self._ingestion, self._decoder = ingestion_service, decoder
        self._source, self._catalog = source, catalog
        self._flights: dict[SupplementScope, _Flight] = {}
        self._tasks: set[asyncio.Task[_Outcome]] = set()
        self._recent: OrderedDict[SupplementScope, tuple[float, _Outcome]] = OrderedDict()

    async def answer(
        self, request: QuestionRequest, model_factory: Callable[[], ModelClient]
    ) -> AnswerResult:
        """由调用方提供按需创建的受预算控制模型；前置失败不创建模型。"""

        preparation = _PublicPreparation(self)
        service = GuideAnswerService(preparation, model_factory, self._decoder)
        result = await service.answer(request)
        return replace(result, lookup_path=preparation.lookup_path)

    async def _scope(self, request: QuestionRequest) -> SupplementScope | None:
        if (
            request.guide_kind is not GuideKind.CHARACTER
            or request.goal != "配队"
            or request.entity is None
            or request.game_version is None
            or _VERSION.fullmatch(request.game_version) is None
        ):
            return None
        catalog = self._catalog
        if catalog is None:
            catalog = await asyncio.to_thread(load_catalog)
            self._catalog = catalog
        try:
            term = catalog.resolve(request.entity, TermCategory.CHARACTER)
        except GuideImportError:
            return None
        if term.status != "canonical":
            return None
        return SupplementScope(request.entity, request.game_version)

    async def _lookup(self, scope: SupplementScope) -> _Outcome:
        source = self._source
        if source is None:
            raise GuideImportError("source_disabled")
        try:
            async with asyncio.timeout(SOURCE_TIMEOUT_SECONDS):
                try:
                    candidates = await source.search(scope)
                    snapshots = []
                    seen: set[str] = set()
                    for candidate in candidates[:MAX_CANDIDATES]:
                        article_id = _article_id(candidate)
                        if article_id is None or article_id in seen:
                            continue
                        seen.add(article_id)
                        article = await source.read(candidate)
                        if not isinstance(article, SourceArticle):
                            continue
                        if (
                            article.entity == scope.entity
                            and article.game_version == scope.game_version
                            and article.conflicts_resolved is not True
                        ):
                            # 先检查全部有限候选，再发布；已知冲突不被首条成功掩盖。
                            return "source_conflict"
                        snapshot = _snapshot(scope, candidate, article)
                        if snapshot is not None:
                            snapshots.append(snapshot)
                except (GuideSourceUnavailable, OSError):
                    raise GuideImportError("source_unavailable") from None
                for snapshot in snapshots:
                    try:
                        result = await self._ingestion.import_snapshot(snapshot, publish=True)
                    except GuideImportError as exc:
                        if exc.code == "publication_blocked":
                            continue
                        raise
                    if result.status is DocumentStatus.PUBLISHED:
                        # 首版一轮至多自动发布一篇，避免把未核对的多文结论混为一体。
                        return "published"
                return "no_eligible_source"
        except TimeoutError:
            raise GuideImportError("source_timeout") from None

    def _settle(self, scope: SupplementScope, task: asyncio.Task[_Outcome]) -> None:
        self._tasks.discard(task)
        flight = self._flights.get(scope)
        if flight is not None and flight.task is task:
            del self._flights[scope]
        if task.cancelled() or task.exception() is not None:
            return
        self._recent[scope] = monotonic() + LOOKUP_COOLDOWN_SECONDS, task.result()
        self._recent.move_to_end(scope)
        while len(self._recent) > MAX_RECENT_LOOKUPS:
            self._recent.popitem(last=False)

    async def _supplement(self, scope: SupplementScope) -> _Outcome:
        cached = self._recent.get(scope)
        if cached is not None and cached[0] > monotonic():
            return cached[1]
        flight = self._flights.get(scope)
        if flight is None:
            if len(self._tasks) >= MAX_CONCURRENT_LOOKUPS:
                raise GuideImportError("source_busy")
            task = asyncio.create_task(self._lookup(scope))
            flight = _Flight(task)
            self._tasks.add(task)
            self._flights[scope] = flight
            task.add_done_callback(lambda done: self._settle(scope, done))
        flight.waiters += 1
        try:
            # 一位客户端断开不影响其他等待者；最后一位离开时停止补查。
            return await asyncio.shield(flight.task)
        finally:
            flight.waiters -= 1
            if not flight.waiters and not flight.task.done():
                if self._flights.get(scope) is flight:
                    del self._flights[scope]
                flight.task.cancel()

    async def aclose(self) -> None:
        """退出时取消并等待仍未完成的外部工作，不遗留后台补查。"""

        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._recent.clear()


@dataclass(slots=True)
class _PublicPreparation:
    """每轮独立记录检索路径，避免不同请求共享证据、条件或模型。"""

    service: PublicGuideService
    lookup_path: Literal["local", "supplement", "miss"] | None = None

    async def prepare(self, request: QuestionRequest) -> QuestionPreparation:
        missing = missing_player_conditions(request)
        if missing:
            return QuestionPreparation("needs_clarification", missing)
        result = await self.service._local.prepare(request)
        if result.status == "evidence_ready":
            self.lookup_path = "local"
        if result.reason != "no_matching_evidence":
            return result
        self.lookup_path = "miss"
        scope = await self.service._scope(request)
        if scope is None:
            return result
        if self.service._source is None:
            return replace(result, reason="source_disabled")
        self.lookup_path = "supplement"
        outcome = await self.service._supplement(scope)
        if outcome != "published":
            return replace(result, reason=outcome)
        # 对每位等待者以自己的已确认条件重新查询，不分享另一个用户的匹配结果。
        return await self.service._local.prepare(request)
