"""同一桌面会话的补条件回归：真实 HTTP 边界和隔离数据库，模型完全模拟。"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from test_guide_ingestion import article_snapshot
from test_guide_postgres import isolated_database

from teyvatnotes.api import create_app
from teyvatnotes.application.conversations import QuestionPatch
from teyvatnotes.application.guide_answering import AnswerStructureError
from teyvatnotes.application.guide_ingestion import GuideIngestionService
from teyvatnotes.application.model_ports import ModelRequest, ModelResponse, ModelUsage
from teyvatnotes.config import GuideSettings
from teyvatnotes.desktop.config import load_config, save_config
from teyvatnotes.desktop.service import Workbench
from teyvatnotes.knowledge.answers import JsonAnswerDecoder
from teyvatnotes.knowledge.snapshots import parse_snapshot
from teyvatnotes.repositories.knowledge_postgres import PostgresKnowledgeRepository


def _claim(evidence_id: str) -> dict:
    return {
        "text": "隔离样本中的配队推荐",
        "kind": "recommendation",
        "section": "summary",
        "evidence_ids": [evidence_id],
        "required_equipment": ["测试武器"],
        "required_characters": ["薇斯纳", "测试队友"],
    }


@pytest.mark.parametrize(
    "status,c,q,g,x,rule",
    [
        ("answer", 0, 0, 0, 0, "answer_requires_claims"),
        ("answer", 1, 2, 1, 0, "answer_forbids_questions"),
        ("answer", 1, 0, 0, 1, "answer_forbids_conflicts"),
        ("needs_clarification", 1, 1, 0, 0, "non_answer_forbids_claims"),
        ("insufficient_evidence", 1, 0, 1, 0, "non_answer_forbids_claims"),
        ("needs_clarification", 0, 0, 1, 0, "clarification_requires_questions"),
        ("insufficient_evidence", 0, 1, 0, 0, "insufficient_requires_gaps_or_conflicts"),
        ("answer", 1, 0, 1, 0, None),
        ("needs_clarification", 0, 1, 1, 0, None),
        ("insufficient_evidence", 0, 0, 0, 1, None),
    ],
)
def test_coherent_status(status, c, q, g, x, rule) -> None:
    value = {
        "status": status,
        "claims": [_claim("0" * 8 + "-0000-0000-0000-" + "0" * 12)] * c,
        "questions": ["敏感问题"] * q,
        "gaps": ["资料缺口"] * g,
        "conflicts": ["冲突"] * x,
    }
    raw = json.dumps(value, ensure_ascii=False)
    decoder = JsonAnswerDecoder()
    if rule is None:
        assert decoder.decode(raw).status == status
        return
    with pytest.raises(AnswerStructureError) as error:
        decoder.decode(raw)
    assert error.value.code == "invalid_answer_structure"
    assert error.value.detail == (
        f"status_conflict rule={rule} answer_status={status} claims_count={c} "
        f"questions_count={q} gaps_count={g} conflicts_count={x}"
    )


@pytest.mark.postgres
@pytest.mark.parametrize(
    "characters,equipment,conflict",
    [
        (None, None, False),
        (None, [], False),
        ([], None, False),
        ([], [], False),
        ([], [], True),
    ],
)
def test_same_session_confirms_team_inventory(
    tmp_path: Path, characters, equipment, conflict
) -> None:
    wire = []
    model_inputs = []
    source_url = "https://www.miyoushe.com/ys/article/1000000000"
    confirmed = {"owned_characters": ["薇斯纳", "测试队友"], "owned_equipment": ["测试武器"]}
    clarification = "还需要确认是否只看示例阵容？"

    class Observe(httpx.ASGITransport):
        async def handle_async_request(self, request):
            if request.url.path.endswith("/answer"):
                wire.append(json.loads(request.content))
            return await super().handle_async_request(request)

    class Model:
        async def generate(self, request: ModelRequest) -> ModelResponse:
            payload = json.loads(request.messages[-1].content)
            player = payload["player"]
            model_inputs.append(player)
            scoped = player["owned_characters"] == confirmed["owned_characters"]
            value = {
                "status": "answer" if scoped else "needs_clarification",
                "questions": [] if scoped and not conflict else [clarification],
                "claims": [_claim(payload["evidence"][0]["evidence_id"])] if scoped else [],
                "gaps": [],
                "conflicts": [],
            }
            return ModelResponse(json.dumps(value, ensure_ascii=False), "模拟", ModelUsage(2, 3))

    async def run() -> None:
        async with isolated_database() as (db, _):
            snapshot = article_snapshot(
                guide_kind="character",
                entities=["薇斯纳"],
                game_version="7.1",
                article_blocks=[
                    {
                        "kind": "paragraph",
                        "text": "合成测试证据：薇斯纳配队可在示例中使用测试队友和测试武器。",
                    }
                ],
            )
            await GuideIngestionService(
                PostgresKnowledgeRepository(db.session_factory)
            ).import_snapshot(parse_snapshot(snapshot), publish=True)
            app = create_app(
                GuideSettings("postgresql://example@localhost/test", b"x" * 32),
                database=db,
                model_factory=Model,
                diagnostic_dir=tmp_path / "server",
            )
            save_config(load_config(tmp_path / "client.toml"), "https://local.test")
            workbench = Workbench(Observe(app=app))
            async with app.router.lifespan_context(app):
                await workbench.connect(tmp_path)
                # 先由桌面理解角色和目标；随后两次提交与“补充条件”表单相同的补丁。
                start = (await workbench.submit(None, "薇斯纳怎么配队？")).conversation
                assert not wire and start.context.goal == "配队"
                question = start.context.question
                first = (
                    await workbench.submit(
                        start,
                        QuestionPatch(
                            {
                                "question": question,
                                "game_version": "7.1",
                                "owned_characters": characters,
                                "owned_equipment": equipment,
                            }
                        ),
                    )
                ).conversation
                assert first.latest_reply["status"] == "needs_clarification"
                assert first.latest_reply["questions"]
                initially_called = characters is not None and equipment is not None
                if initially_called:
                    assert first.latest_reply["questions"] == [clarification]
                assert len(model_inputs) == int(initially_called)
                assert wire[0]["owned_characters"] == characters
                assert wire[0]["owned_equipment"] == equipment
                second = (
                    await workbench.submit(
                        first,
                        QuestionPatch(
                            {
                                "question": question,
                                **confirmed,
                            }
                        ),
                    )
                ).conversation
                assert len(wire) == 2 and len(model_inputs) == 1 + int(initially_called)
                assert second.id == first.id and second.turns[:-1] == first.turns
                for item in [*wire, *model_inputs]:
                    assert (
                        item["entity"],
                        item["game_version"],
                        item["goal"],
                        item["guide_kind"],
                    ) == ("薇斯纳", "7.1", "配队", "character")
                    assert item["question"] == question and "history" not in item
                assert wire[0]["keywords"] == wire[1]["keywords"] == ["配队"]
                for name, names in confirmed.items():
                    assert wire[1][name] == model_inputs[-1][name] == names
                    if initially_called:
                        assert model_inputs[0][name] == []
                reply = second.latest_reply
                if conflict:
                    assert (
                        reply["status"] == "error" and reply["reason"] == "invalid_answer_structure"
                    )
                    assert "claims" not in reply
                else:
                    assert reply["status"] == "answer"
                    cite = reply["claims"][0]["citations"][0]
                    assert cite["source_url"] == source_url and cite["game_version"] == "7.1"
                    assert "薇斯纳配队" in cite["text"]
                assert (await workbench.get(second.id)).latest_reply == reply
                await workbench.close()

    asyncio.run(run())
