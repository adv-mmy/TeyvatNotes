"""真实 PostgreSQL 上的公开攻略契约；全部外部能力为可控替身。"""

import asyncio
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import httpx
import pytest
from sqlalchemy import func, select
from test_guide_answering import FakeClient
from test_guide_postgres import isolated_database

from teyvatnotes.api import create_app
from teyvatnotes.application.guide_ingestion import GuideIngestionService
from teyvatnotes.application.public_guides import (
    PublicGuideService,
    SourceArticle,
    SourceCandidate,
    SupplementScope,
)
from teyvatnotes.config import GuideSettings
from teyvatnotes.database.access import GuideQuotaRecord, GuideSessionRecord
from teyvatnotes.database.knowledge import GuideDocumentRecord
from teyvatnotes.knowledge.answers import JsonAnswerDecoder
from teyvatnotes.repositories.guide_access import PostgresGuideAccess, QuotaExceeded
from teyvatnotes.repositories.knowledge_postgres import PostgresKnowledgeRepository

pytestmark = pytest.mark.postgres
SETTINGS = GuideSettings("postgresql+asyncpg://example@localhost/test", b"z" * 32)
QUESTION = {
    "question": "钟离怎么配队？",
    "entity": "钟离",
    "guide_kind": "character",
    "game_version": "7.1",
    "goal": "配队",
    "keywords": ["配队"],
    "owned_characters": ["钟离"],
    "owned_equipment": [],
}


class Source:
    """仅隔离测试用的人工合成资料，不联网。"""

    def __init__(self, *, version: str = "7.1", complete: bool = True) -> None:
        self.searches = 0
        self.reads = 0
        self.version, self.complete = version, complete

    async def search(self, scope: SupplementScope) -> tuple[SourceCandidate, ...]:
        assert scope == SupplementScope("钟离", "7.1")
        self.searches += 1
        return (
            SourceCandidate("https://evil.example/ys/article/123"),
            SourceCandidate("https://www.miyoushe.com/ys/article/123"),
        )

    async def read(self, candidate: SourceCandidate) -> SourceArticle:
        assert candidate.url == "https://www.miyoushe.com/ys/article/123"
        self.reads += 1
        return SourceArticle(
            "隔离验收的合成配队资料",
            "测试作者",
            "钟离",
            self.version,
            self.version,
            ("仅在隔离测试中验证钟离配队引用；此合成内容不是可分发的实际游戏结论。",),
            complete=self.complete,
            conditions_confirmed=True,
            conflicts_resolved=True,
        )


async def _bearer(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.post("/v1/guides/sessions")
    assert response.status_code == 201
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def test_two_clients_share_published_evidence_and_no_legacy_routes() -> None:
    async def run() -> None:
        async with isolated_database() as (db, _):
            repo = PostgresKnowledgeRepository(db.session_factory)
            source, model = Source(), FakeClient()
            guides = PublicGuideService(
                repo, GuideIngestionService(repo), decoder=JsonAnswerDecoder(), source=source
            )
            app = create_app(SETTINGS, database=db, guides=guides, model_factory=lambda: model)
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://test") as a,
                httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://test") as b,
            ):
                assert (await a.get("/health/ready")).status_code == 200
                assert (await a.get("/v1/tasks")).status_code == 404
                first, second = await _bearer(a), await _bearer(b)
                assert (await b.post("/v1/guides/answer", json=QUESTION)).status_code == 401
                oversized = await a.post(
                    "/v1/guides/answer",
                    content=b" " * 16_385,
                    headers={**first, "Content-Type": "application/json"},
                )
                assert oversized.status_code == 413
                clarification = await a.post(
                    "/v1/guides/answer", json={**QUESTION, "owned_equipment": None}, headers=first
                )
                assert clarification.json()["status"] == "needs_clarification"
                assert source.searches == model.calls == 0
                outputs = await asyncio.gather(
                    a.post("/v1/guides/answer", json=QUESTION, headers=first),
                    b.post("/v1/guides/answer", json=QUESTION, headers=second),
                )
                for response in outputs:
                    assert response.status_code == 200, response.json()
                    result = response.json()
                    assert result["status"] == "answer"
                    assert result["generation_attempts"] == 1
                    assert result["claims"][0]["citations"][0]["source_url"].endswith("/123")
                    assert "used_player_conditions" not in result
                assert source.searches == source.reads == 1 and model.calls == 2
                repeated = await b.post("/v1/guides/answer", json=QUESTION, headers=second)
                assert repeated.json()["status"] == "answer" and source.searches == 1
                for _ in range(4):
                    next_answer = await a.post("/v1/guides/answer", json=QUESTION, headers=first)
                    assert next_answer.status_code == 200
                rejected = await a.post("/v1/guides/answer", json=QUESTION, headers=first)
                assert rejected.status_code == 429 and rejected.json()["code"] == "quota_exceeded"
                assert model.calls == 7 and source.searches == 1
                async with db.session_factory() as session:
                    assert (
                        await session.scalar(select(func.count()).select_from(GuideDocumentRecord))
                        == 1
                    )

    asyncio.run(run())


@pytest.mark.parametrize("case", ["disabled", "wrong_version", "no_body"])
def test_no_model_or_published_guide_on_ineligible_source(case: str) -> None:
    async def run() -> None:
        async with isolated_database() as (db, _):
            repo = PostgresKnowledgeRepository(db.session_factory)
            source = (
                None
                if case == "disabled"
                else Source(
                    version="7.2" if case == "wrong_version" else "7.1",
                    complete=case != "no_body",
                )
            )
            guides = PublicGuideService(
                repo, GuideIngestionService(repo), decoder=JsonAnswerDecoder(), source=source
            )
            model = FakeClient()
            app = create_app(SETTINGS, database=db, guides=guides, model_factory=lambda: model)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="https://test"
            ) as client:
                token = await _bearer(client)
                response = await client.post("/v1/guides/answer", json=QUESTION, headers=token)
                assert response.status_code == 200
                result = response.json()
                assert result["status"] == "insufficient_evidence"
                assert result["reason"] == (
                    "source_disabled" if source is None else "no_eligible_source"
                )
                assert not result["model_called"] and model.calls == 0
                invalid = await client.post(
                    "/v1/guides/answer", json={**QUESTION, "history": []}, headers=token
                )
                assert invalid.status_code == 422
            async with db.session_factory() as session:
                assert (
                    await session.scalar(select(func.count()).select_from(GuideDocumentRecord)) == 0
                )

    asyncio.run(run())


def test_daily_quota_is_atomic_and_persistent() -> None:
    async def run() -> None:
        async with isolated_database() as (db, _):
            first = PostgresGuideAccess(db.session_factory)
            second = PostgresGuideAccess(db.session_factory)
            ip = "c" * 64
            now = datetime(2026, 11, 9, 2, tzinfo=UTC)
            issued = await asyncio.gather(
                *(first.issue(ip, now) for _ in range(5)), return_exceptions=True
            )
            allowed = [value for value in issued if not isinstance(value, BaseException)]
            assert len(allowed) == 3
            assert sum(isinstance(value, QuotaExceeded) for value in issued) == 2
            token = allowed[0]
            key = await second.authenticate(token.access_token, now)
            assert key == sha256(token.access_token.encode()).hexdigest()
            assert await second.authenticate(token.access_token, now + timedelta(days=7)) is None
            results = await asyncio.gather(
                *(second.reserve(key, ip, now) for _ in range(8)), return_exceptions=True
            )
            assert results.count(None) == 5
            assert sum(isinstance(value, QuotaExceeded) for value in results) == 3
            async with db.session_factory() as session:
                values = dict(
                    (
                        await session.execute(
                            select(GuideQuotaRecord.bucket, GuideQuotaRecord.used)
                        )
                    ).all()
                )
                assert values == {
                    "issue_global": 3,
                    "issue_ip": 3,
                    "model_global": 5,
                    "model_ip": 5,
                    "model_token": 5,
                }
                assert await session.get(GuideSessionRecord, token.access_token) is None
            await first.reserve(key, ip, now + timedelta(days=1))

    asyncio.run(run())


def test_ip_and_global_quota_buckets_do_not_depend_on_token_rotation() -> None:
    async def run() -> None:
        async with isolated_database() as (db, _):
            access = PostgresGuideAccess(db.session_factory)
            now = datetime(2026, 11, 9, tzinfo=UTC)
            # 5 个 IP 各 20 次；每次使用不同令牌身份来刻意绕开令牌桶。
            for index in range(100):
                principal, address = f"{index + 1:064x}", f"{index // 20 + 1:064x}"
                await access.reserve(principal, address, now)
                if index == 19:
                    with pytest.raises(QuotaExceeded):
                        await access.reserve("a" * 64, address, now)
            with pytest.raises(QuotaExceeded):
                await access.reserve("b" * 64, "b" * 64, now)
            async with db.session_factory() as session:
                assert (
                    await session.scalar(
                        select(GuideQuotaRecord.used).where(
                            GuideQuotaRecord.bucket == "model_global"
                        )
                    )
                    == 100
                )
            await access.reserve("b" * 64, "b" * 64, now + timedelta(days=1))

    asyncio.run(run())
