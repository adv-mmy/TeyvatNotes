"""桌面版配置、交互和真实会话接入的回归验收。"""

import asyncio
from codecs import BOM_UTF8
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest

from teyvatnotes.application.conversations import QuestionPatch, question_to_data
from teyvatnotes.desktop.config import DesktopError, load_config, save_model
from teyvatnotes.desktop.service import (
    DEMO_FAILURE,
    DEMO_GAP,
    DEMO_QUESTION,
    DEMO_REPLACEMENT,
    Workbench,
)

OLD_URL = "https://previous.example/v1"
NEW_URL = "https://test.example/v1"


def test_desktop_config_saves_only_model_fields_and_detects_conflicts(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    unrelated = '# 中文注释\r\nUNRELATED="保留 # 值"\r\n\r\n'.encode()
    path.write_bytes(
        BOM_UTF8
        + unrelated
        + f"LLM_BASE_URL={OLD_URL}\r\nLLM_MODEL=old\r\nLLM_API_KEY=old-test\r\n".encode()
    )
    initial = load_config(path)
    assert initial.has_key and initial.model == "old" and "old-test" not in repr(initial)
    with pytest.raises(DesktopError, match="config_key_required"):
        save_model(initial, NEW_URL, "test", "")
    same = save_model(initial, OLD_URL, "same-origin", "")
    assert b"LLM_API_KEY=old-test\r\n" in path.read_bytes()
    with pytest.raises(DesktopError, match="config_conflict"):
        save_model(initial, NEW_URL, "test", "test")
    with pytest.raises(DesktopError, match="config_invalid_model"):
        save_model(same, "http://test.example", "test", "test")
    updated = save_model(same, NEW_URL, "test", "test")
    raw = path.read_bytes()
    assert raw.startswith(BOM_UTF8 + unrelated)
    assert b"LLM_API_KEY='test'\r\n" in raw and b"old-test" not in raw
    assert updated.base_url == NEW_URL and load_config(path).model == "test"


def test_demo_runs_through_existing_conversation_state_machine() -> None:
    async def run() -> None:
        workbench = Workbench()
        first = await workbench.submit(None, DEMO_QUESTION)
        assert first.conversation.latest_reply["status"] == "needs_clarification"
        patch = QuestionPatch(
            {
                "question": DEMO_QUESTION,
                "game_version": "1.0",
                "owned_characters": ["风铃旅人", "苔羽", "星见"],
                "owned_equipment": [],
            }
        )
        answer = await workbench.submit(first.conversation, patch)
        claim = answer.conversation.latest_reply["claims"][0]
        assert "苔羽" in claim["text"] and claim["citations"][0]["locators"]
        next_turn = await workbench.submit(answer.conversation, DEMO_REPLACEMENT)
        assert "星见" in next_turn.conversation.latest_reply["claims"][0]["text"]
        assert next_turn.conversation.context.owned_equipment == ()
        conflict = await workbench.submit(first.conversation, patch)
        assert not conflict.saved and conflict.conversation.revision == 6
        gap = await workbench.submit(None, DEMO_GAP)
        assert gap.conversation.latest_reply["status"] == "insufficient_evidence"
        with pytest.raises(DesktopError, match="demo_failure"):
            await workbench.submit(None, DEMO_FAILURE)
        page = await workbench.page()
        assert len(page.items) == 2 and not page.has_more
        assert len((await workbench.page(1)).items) == 1
        await workbench.close()

    asyncio.run(run())


def test_qt_window_offline_flow_settings_and_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("PySide6")
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton

    from teyvatnotes.desktop.ui import WorkbenchWindow
    from teyvatnotes.desktop.widgets import SettingsDialog

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    window = WorkbenchWindow(tmp_path)
    window.show()

    def wait_for(check):
        for _ in range(300):
            app.processEvents()
            if check():
                return
            QTest.qWait(10)
        assert check()

    def click(text: str) -> None:
        def find():
            return next(
                (
                    item
                    for item in window.findChildren(QPushButton)
                    if item.text() == text and item.isVisible()
                ),
                None,
            )

        wait_for(find)
        find().click()

    try:
        wait_for(lambda: window._ready_flag)
        click(DEMO_QUESTION)
        click("补充条件")
        click("填入示例")
        click("确认并继续")
        click("引用 1")
        assert "优先使用" in window.findChild(QLabel, "excerpt").text()
        click(DEMO_REPLACEMENT)
        wait_for(lambda: window._conversation and window._conversation.revision == 6)
        assert "苔羽" not in window._conversation.context.owned_characters
        window.resize(820, 560)
        app.processEvents()
        assert not window._evidence.isVisible()
        dialog = SettingsDialog(window, tmp_path / ".env", tmp_path / ".env", allow_switch=True)
        dialog.findChild(QLineEdit, "baseUrl").setText(NEW_URL)
        dialog.findChild(QLineEdit, "modelName").setText("test")
        key = dialog.findChild(QLineEdit, "modelKey")
        key.setText("test")
        dialog.findChild(QPushButton, "saveModel").click()
        assert key.text() == "" and load_config(tmp_path / ".env").has_key
        dialog.close()
        started, cancelled = Event(), Event()

        async def pending() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        window._worker.dispatch("test-pending", pending())
        wait_for(started.is_set)
        window.close()
        wait_for(lambda: not window._worker.isRunning())
        assert cancelled.is_set()
    finally:
        window.close()
        wait_for(lambda: not window._worker.isRunning())
        app.processEvents()


@pytest.mark.postgres
def test_desktop_real_workspace_reuses_published_knowledge_and_reloads_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from test_guide_postgres import connect, isolated_database

    from teyvatnotes.application.guide_ingestion import GuideIngestionService
    from teyvatnotes.desktop import service
    from teyvatnotes.knowledge.agent_cli import OfflineToolClient
    from teyvatnotes.knowledge.demo import PROJECT_ROOT
    from teyvatnotes.knowledge.questions import load_question
    from teyvatnotes.knowledge.snapshots import parse_snapshot
    from teyvatnotes.repositories.knowledge_postgres import PostgresKnowledgeRepository

    class Model(OfflineToolClient):
        def __init__(self, settings):
            self.settings = settings
            selected.append(settings.model)

        async def generate_turn(self, request):
            result = await super().generate_turn(request)
            return replace(result, model=self.settings.model)

    selected: list[str] = []

    async def run() -> None:
        async with isolated_database() as (database, schema):
            monkeypatch.setattr(service, "create_database", lambda _url: connect(schema))
            repo = PostgresKnowledgeRepository(database.session_factory)
            raw = json.loads(
                (PROJECT_ROOT / "samples/guides/retrieval_character.json").read_text(
                    encoding="utf-8"
                )
            )
            version = "desktop-synthetic"
            raw.update(is_fixture=False, game_version=version)
            await GuideIngestionService(repo).import_snapshot(parse_snapshot(raw), publish=True)
            question = replace(
                load_question(PROJECT_ROOT / "samples/questions/character.json"),
                game_version=version,
            )
            path = tmp_path / ".env"
            path.write_text(
                "DATABASE_URL=postgresql+asyncpg://test@127.0.0.1/test\n"
                "GUIDE_SESSION_OWNER_ID=05bdc67b-e55c-4541-80a5-bbc490a90227\n"
                f"LLM_BASE_URL={NEW_URL}\nLLM_MODEL=first-test\nLLM_API_KEY=test\n",
                encoding="utf-8",
            )
            workbench = Workbench(Model)
            try:
                assert not (await workbench.connect(path)).items
                draft = await workbench.submit(None, "请核对这条测试问题")
                patch = QuestionPatch(question_to_data(question))
                result = await workbench.submit(draft.conversation, patch)
                reply = result.conversation.latest_reply
                assert reply["status"] == "answer" and reply["model"] == "first-test"
                assert reply["claims"][0]["citations"][0]["game_version"] == version
                save_model(load_config(path), NEW_URL, "second-test", "")
                updated = await workbench.submit(result.conversation, patch)
                assert updated.conversation.latest_reply["model"] == "second-test"
                conflict = await workbench.submit(result.conversation, patch)
                assert not conflict.saved and conflict.conversation.revision == (
                    updated.conversation.revision
                )
                assert selected == ["first-test", "second-test"]
            finally:
                await workbench.close()
            reopened = Workbench(Model)
            try:
                page = await reopened.connect(path)
                assert page.items[0].id == updated.conversation.id
                assert (await reopened.get(page.items[0].id)).turns == updated.conversation.turns
            finally:
                await reopened.close()

    asyncio.run(run())
