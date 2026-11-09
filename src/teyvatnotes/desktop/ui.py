"""提瓦特手记的 Qt Widgets 三栏窗口。"""

from pathlib import Path
from uuid import UUID

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QCloseEvent, QDesktopServices, QKeyEvent, QResizeEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPlainTextEdit,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from teyvatnotes.application.conversations import (
    ConversationSummary,
    GuideConversation,
    QuestionPatch,
)
from teyvatnotes.desktop.service import Page, SubmitResult, Workbench
from teyvatnotes.desktop.style import WORKBENCH_STYLE
from teyvatnotes.desktop.widgets import (
    ConditionsForm,
    SettingsDialog,
    button,
    error_text,
    label,
)
from teyvatnotes.desktop.worker import AsyncWorker
from teyvatnotes.domain.json_types import JsonValue


def _clear(layout: QVBoxLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        if item is not None and (widget := item.widget()) is not None:
            widget.hide()
            widget.deleteLater()


def _panel(name: str, margin: int = 15) -> tuple[QWidget, QVBoxLayout]:
    widget = QWidget()
    widget.setObjectName(name)
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(margin, margin, margin, margin)
    layout.setSpacing(10)
    return widget, layout


def _texts(value: JsonValue) -> str:
    if isinstance(value, list):
        return "\n".join(item for item in value if isinstance(item, str))
    return ""


def _gap_text(reason: JsonValue) -> str:
    if reason == "source_disabled":
        return "没有匹配的本地攻略，联网资料源尚未启用，本轮未调用模型。"
    if reason == "no_matching_evidence":
        return "未找到匹配的本地攻略，本轮未调用模型。"
    return "当前范围内缺少可核对的证据。"


def _inventory(value: tuple[str, ...] | None) -> str:
    if value is None:
        return "待确认"
    return "、".join(value) if value else "已确认没有"


class Composer(QPlainTextEdit):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("questionInput")
        self.setAccessibleName("攻略问题")
        self.setPlaceholderText("问问角色配队、装备选择或任务进度…")
        self.setFixedHeight(75)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        # 使用 Ctrl+Enter 发送，保留回车换行与中文输入法的确认操作。
        if (
            event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            and event.modifiers() == Qt.KeyboardModifier.ControlModifier
        ):
            window = self.window()
            if isinstance(window, WorkbenchWindow):
                window.send_prompt()
            return
        super().keyPressEvent(event)


class WorkbenchWindow(QMainWindow):
    def __init__(self, directory: Path, workbench: Workbench | None = None) -> None:
        super().__init__()
        self.setWindowTitle("提瓦特手记 · 攻略工作台")
        self.setStyleSheet(WORKBENCH_STYLE)
        self.resize(1240, 760)
        self.setMinimumSize(730, 525)
        self._directory = directory
        self._config_path = directory / "client.toml"
        self._workbench = workbench or Workbench()
        self._worker = AsyncWorker(self._workbench)
        self._worker.ready.connect(self._ready)
        self._worker.delivered.connect(self._received)
        self._worker.failed.connect(self._failed)
        self._worker.finished.connect(self.close)
        self._ready_flag = False
        self._sending = False
        self._switching = False
        self._closing = False
        self._target: UUID | None = None
        self._send_target: UUID | None = None
        self._send_text = ""
        self._conversation: GuideConversation | None = None
        self._form: ConditionsForm | None = None
        self._items: list[ConversationSummary] = []
        self._next_offset = 0
        self._list_serial = 0
        self._manual_evidence: bool | None = None
        self._build()
        self._draw_history()
        self._draw_context()
        self._sync_actions()
        self._worker.start()

    def _build(self) -> None:
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(1)
        self.setCentralWidget(splitter)

        sidebar, nav = _panel("sidebar")
        sidebar.setMinimumWidth(174)
        sidebar.setMaximumWidth(330)
        brand = QHBoxLayout()
        mark = label("✦", "mark")
        mark.setFixedSize(40, 40)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand.addWidget(mark)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(1)
        brand_text.addWidget(label("提瓦特手记", "brand"))
        brand_text.addWidget(label("攻略工作台", "muted"))
        brand.addLayout(brand_text)
        brand.addStretch()
        nav.addLayout(brand)
        nav.addSpacing(7)
        self._new = button("＋  新建攻略", "primary")
        self._new.clicked.connect(self._new_session)
        nav.addWidget(self._new)
        nav.addSpacing(8)
        nav.addWidget(label("攻略会话", "eyebrow"))
        self._history_list = QListWidget()
        self._history_list.setObjectName("conversationList")
        self._history_list.itemClicked.connect(self._open_item)
        self._history_list.itemActivated.connect(self._open_item)
        nav.addWidget(self._history_list, 1)
        self._more = button("加载更多")
        self._more.clicked.connect(lambda: self._load_page(self._next_offset))
        self._more.hide()
        nav.addWidget(self._more)
        self._preferences = button("设置")
        self._preferences.clicked.connect(self._open_settings)
        nav.addWidget(self._preferences)
        splitter.addWidget(sidebar)

        central, conversation_layout = _panel("conversation", 0)
        central.setMinimumWidth(390)
        header = QWidget()
        header.setObjectName("toolbar")
        top = QHBoxLayout(header)
        top.setContentsMargins(18, 10, 18, 10)
        self._title = label("攻略问答", "sectionTitle")
        top.addWidget(self._title, 1)
        self._refresh = button("刷新")
        self._refresh.clicked.connect(self._refresh_view)
        top.addWidget(self._refresh)
        self._show_context = button("条件与证据")
        self._show_context.setCheckable(True)
        self._show_context.setChecked(True)
        self._show_context.clicked.connect(self._toggle_context)
        top.addWidget(self._show_context)
        conversation_layout.addWidget(header)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        messages = QWidget()
        self._messages = QVBoxLayout(messages)
        self._messages.setContentsMargins(24, 20, 24, 15)
        self._messages.setSpacing(12)
        self._messages.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._scroll.setWidget(messages)
        conversation_layout.addWidget(self._scroll, 1)

        composer_area = QFrame()
        composer_area.setObjectName("composer")
        composer_box = QVBoxLayout(composer_area)
        composer_box.setContentsMargins(11, 9, 11, 9)
        composer_box.setSpacing(6)
        self._input = Composer()
        self._input.textChanged.connect(self._sync_actions)
        composer_box.addWidget(self._input)
        actions = QHBoxLayout()
        self._note = label("本地会话 · 共享攻略服务  /  Ctrl+Enter 发送", "muted", wrap=True)
        actions.addWidget(self._note, 1)
        self._send = button("发送", "primary")
        self._send.clicked.connect(self.send_prompt)
        actions.addWidget(self._send)
        composer_box.addLayout(actions)
        bottom = QVBoxLayout()
        bottom.setContentsMargins(18, 0, 18, 12)
        self._notice = label("", "notice", wrap=True)
        self._notice.hide()
        bottom.addWidget(self._notice)
        bottom.addWidget(composer_area)
        conversation_layout.addLayout(bottom)
        splitter.addWidget(central)

        self._evidence, evidence_layout = _panel("evidence")
        self._evidence.setMinimumWidth(230)
        self._evidence.setMaximumWidth(390)
        evidence_scroll = QScrollArea()
        evidence_scroll.setWidgetResizable(True)
        body = QWidget()
        self._context = QVBoxLayout(body)
        self._context.setContentsMargins(0, 0, 0, 0)
        self._context.setSpacing(11)
        self._context.setAlignment(Qt.AlignmentFlag.AlignTop)
        evidence_scroll.setWidget(body)
        evidence_layout.addWidget(evidence_scroll)
        splitter.addWidget(self._evidence)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([230, 710, 300])

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_evidence") and self._manual_evidence is None:
            self._set_evidence_visible(event.size().width() >= 1030)

    def _set_evidence_visible(self, visible: bool) -> None:
        self._evidence.setVisible(visible)
        self._show_context.setChecked(visible)

    def _toggle_context(self, checked: bool) -> None:
        self._manual_evidence = checked
        self._set_evidence_visible(checked)

    def _ready(self) -> None:
        self._switching = True
        self._feedback("正在打开本地会话…")
        self._worker.dispatch("connect", self._workbench.connect(self._directory))
        self._sync_actions()

    def _sync_actions(self) -> None:
        busy = self._sending or self._switching or not self._ready_flag
        open_pending = self._target is not None and self._conversation is None
        closed = bool(
            self._conversation
            and (
                self._conversation.closed
                or self._conversation.active_turn
                or self._conversation.demo_only
            )
        )
        self._send.setEnabled(
            not busy and not open_pending and not closed and bool(self._input.toPlainText().strip())
        )
        self._new.setEnabled(not busy)
        self._refresh.setEnabled(not self._switching and self._worker.isRunning())
        if self._form is not None:
            self._form.setEnabled(not busy)

    def _feedback(self, text: str = "") -> None:
        self._notice.setText(text)
        self._notice.setVisible(bool(text))

    def _load_page(self, offset: int = 0) -> None:
        if not self._ready_flag:
            return
        self._list_serial += 1
        self._more.setEnabled(False)
        self._worker.dispatch(f"page:{self._list_serial}", self._workbench.page(offset))

    def _render_page(self, page: Page) -> None:
        if page.offset == 0:
            self._items = []
        seen = {item.id for item in self._items}
        self._items.extend(item for item in page.items if item.id not in seen)
        self._next_offset = page.offset + len(page.items)
        self._more.setVisible(page.has_more)
        self._more.setEnabled(page.has_more)
        self._history_list.clear()
        for item in self._items:
            title = item.title + ("  ·  " + item.game_version if item.game_version else "")
            row = QListWidgetItem(title)
            row.setData(Qt.ItemDataRole.UserRole, str(item.id))
            row.setToolTip(title)
            self._history_list.addItem(row)
            if self._target == item.id:
                self._history_list.setCurrentItem(row)

    def _open_item(self, item: QListWidgetItem) -> None:
        if self._switching:
            return
        self._target = UUID(str(item.data(Qt.ItemDataRole.UserRole)))
        self._conversation = None
        self._draw_history()
        self._draw_context()
        self._feedback("正在读取会话…")
        self._worker.dispatch(f"open:{self._target}", self._workbench.get(self._target))
        self._sync_actions()

    def _refresh_view(self) -> None:
        if not self._ready_flag:
            self._ready()
            return
        self._load_page()
        if self._target is not None:
            self._worker.dispatch(f"open:{self._target}", self._workbench.get(self._target))

    def _new_session(self) -> None:
        self._target = None
        self._conversation = None
        self._history_list.clearSelection()
        self._input.clear()
        self._feedback()
        self._draw_history()
        self._draw_context()
        self._sync_actions()
        self._input.setFocus()

    def _open_settings(self) -> None:
        SettingsDialog(self, self._config_path).exec()

    def send_prompt(self) -> None:
        if self._send.isEnabled():
            self._send_value(self._input.toPlainText().strip())

    def _send_value(self, question: str | QuestionPatch) -> None:
        if (
            not self._ready_flag
            or self._sending
            or self._switching
            or (self._target is not None and self._conversation is None)
            or (
                self._conversation
                and (
                    self._conversation.closed
                    or self._conversation.active_turn
                    or self._conversation.demo_only
                )
            )
        ):
            return
        self._sending = True
        self._send_target = self._target
        self._send_text = question if isinstance(question, str) else ""
        self._feedback("正在查找攻略依据…")
        self._sync_actions()
        self._worker.dispatch("submit", self._workbench.submit(self._conversation, question))

    def _interrupt(self) -> None:
        if self._conversation is None or self._sending:
            return
        self._sending = True
        self._send_target = self._target
        self._send_text = ""
        self._sync_actions()
        self._worker.dispatch("submit", self._workbench.interrupt(self._conversation))

    def _received(self, operation: str, result: object) -> None:
        if operation == "connect" and isinstance(result, Page):
            self._switching = False
            self._ready_flag = True
            self._feedback()
            self._render_page(result)
        elif (
            not self._switching
            and operation == f"page:{self._list_serial}"
            and isinstance(result, Page)
        ):
            self._render_page(result)
        elif operation == f"open:{self._target}" and isinstance(result, GuideConversation):
            self._feedback()
            self._show_conversation(result)
        elif operation == "submit" and isinstance(result, SubmitResult):
            self._sending = False
            if self._target == self._send_target:
                if result.saved and self._input.toPlainText().strip() == self._send_text:
                    self._input.clear()
                self._show_conversation(result.conversation)
                self._feedback(result.notice)
            self._load_page()
        self._sync_actions()

    def _failed(self, operation: str, code: str) -> None:
        if operation == "submit":
            self._sending = False
            if code.startswith("conversation_") and self._target is not None:
                self._refresh_view()
        elif operation == "connect":
            self._switching = False
        elif operation == f"page:{self._list_serial}":
            self._more.setEnabled(True)
        self._feedback(error_text(code))
        self._sync_actions()

    def _show_conversation(self, result: GuideConversation) -> None:
        current = self._conversation
        if current is not None and current.id == result.id and current.revision > result.revision:
            return
        self._target = result.id
        self._conversation = result
        self._draw_history()
        self._draw_context()
        self._scroll_bottom()

    def _scroll_bottom(self) -> None:
        scroll = self._scroll.verticalScrollBar()
        QTimer.singleShot(0, lambda: scroll.setValue(scroll.maximum()))

    def _hero(self) -> None:
        hero = QFrame()
        hero.setObjectName("hero")
        layout = QVBoxLayout(hero)
        layout.setContentsMargins(20, 17, 20, 20)
        layout.setSpacing(9)
        layout.addWidget(label("✦  旅行者的案头", "eyebrow"))
        layout.addWidget(label("今天想查什么攻略？", "heroTitle"))
        layout.addWidget(
            label("从版本和持有条件出发，查看有出处的角色与任务建议。", "muted", wrap=True)
        )
        self._messages.addWidget(hero)

    def _draw_history(self) -> None:
        _clear(self._messages)
        self._form = None
        conversation = self._conversation
        self._title.setText((conversation.context.entity if conversation else None) or "攻略问答")
        if conversation is None:
            if self._target is None:
                self._hero()
            return
        if conversation.demo_only:
            self._messages.addWidget(label("旧版虚构演示记录 · 仅供回顾", "notice", wrap=True))
        for turn in conversation.turns:
            question = label(turn.request.question, "userMessage", wrap=True)
            question.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self._messages.addWidget(question)
            self._messages.addWidget(label("✦  手记助手", "eyebrow"))
            if turn.reply is not None:
                self._reply(turn.reply)
            elif turn.status == "pending":
                self._messages.addWidget(
                    label("本轮待处理。可以刷新查看最新状态。", "notice", wrap=True)
                )
        if conversation.active_turn and not self._sending:
            interrupt = button("标记待处理轮次中断")
            interrupt.clicked.connect(self._interrupt)
            self._messages.addWidget(interrupt, alignment=Qt.AlignmentFlag.AlignLeft)
        elif not conversation.closed and not conversation.demo_only:
            edit = button(
                "补充条件"
                if (conversation.latest_reply or {}).get("status") == "needs_clarification"
                else "修改玩家条件"
            )
            edit.clicked.connect(self._open_conditions)
            self._messages.addWidget(edit, alignment=Qt.AlignmentFlag.AlignLeft)
        if conversation.closed:
            self._messages.addWidget(label("会话已关闭，历史仍可查看。", "muted"))

    def _reply(self, reply: dict[str, JsonValue]) -> None:
        status = reply.get("status")
        if status == "needs_clarification":
            self._messages.addWidget(
                label("还需要确认几个条件\n" + _texts(reply.get("questions")), "notice", wrap=True)
            )
        elif status == "insufficient_evidence":
            self._messages.addWidget(
                label(
                    "现有资料不足\n"
                    + (_texts(reply.get("gaps")) or _gap_text(reply.get("reason"))),
                    "notice",
                    wrap=True,
                )
            )
        elif status == "error":
            self._messages.addWidget(
                label(error_text(str(reply.get("reason") or "")), "notice", wrap=True)
            )
        elif isinstance(claims := reply.get("claims"), list):
            model = reply.get("model")
            if isinstance(model, str) and model:
                self._messages.addWidget(label("模型 · " + model, "muted"))
            numbers: dict[str, int] = {}
            for claim in claims:
                if not isinstance(claim, dict):
                    continue
                card = QFrame()
                card.setObjectName("claim")
                layout = QVBoxLayout(card)
                layout.setContentsMargins(13, 11, 13, 11)
                section = claim.get("section")
                if isinstance(section, str) and section:
                    layout.addWidget(label(section, "sectionTitle", wrap=True))
                claim_text = label(str(claim.get("text") or ""), wrap=True)
                claim_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                layout.addWidget(claim_text)
                links = QHBoxLayout()
                citations = claim.get("citations")
                if isinstance(citations, list):
                    for evidence in citations:
                        if not isinstance(evidence, dict):
                            continue
                        key = str(evidence.get("evidence_id") or len(numbers) + 1)
                        index = numbers.setdefault(key, len(numbers) + 1)
                        cite = button(f"引用 {index}", "reference")
                        cite.clicked.connect(
                            lambda _checked=False, value=evidence, n=index: self._show_evidence(
                                value, n
                            )
                        )
                        links.addWidget(cite)
                links.addStretch()
                layout.addLayout(links)
                self._messages.addWidget(card)

    def _open_conditions(self) -> None:
        if self._conversation is None or self._form is not None or self._sending:
            return
        form = ConditionsForm(self._conversation.context)
        self._form = form
        form.submitted.connect(self._send_value)
        form.cancelled.connect(self._close_conditions)
        self._messages.addWidget(form)
        QTimer.singleShot(0, lambda: self._scroll.ensureWidgetVisible(form))

    def _close_conditions(self) -> None:
        if self._form is not None:
            self._form.hide()
            self._form.deleteLater()
            self._form = None

    def _draw_context(self) -> None:
        _clear(self._context)
        self._context.addWidget(label("已确认条件", "heading"))
        self._context.addWidget(label("本轮攻略采用的范围与持有条件", "muted", wrap=True))
        if self._conversation is None:
            self._context.addWidget(label("开始会话后，版本、角色和装备会显示在这里。", wrap=True))
        else:
            context = self._conversation.context
            for name, value in (
                ("游戏版本", context.game_version or "待确认"),
                ("攻略对象", context.entity or "待确认"),
                ("持有角色", _inventory(context.owned_characters)),
                ("持有装备", _inventory(context.owned_equipment)),
            ):
                self._context.addWidget(label(name, "eyebrow"))
                self._context.addWidget(label(value, wrap=True))
        self._context.addWidget(label("点击回答旁的引用，可核对攻略出处。", "muted", wrap=True))

    def _show_evidence(self, evidence: dict[str, JsonValue], number: int) -> None:
        _clear(self._context)
        self._set_evidence_visible(True)
        self._context.addWidget(label(f"证据摘录 · 引用 {number}", "heading", wrap=True))
        self._context.addWidget(
            label(str(evidence.get("title") or "攻略来源"), "sectionTitle", wrap=True)
        )
        for key in ("author", "game_version", "section"):
            value = evidence.get(key)
            if isinstance(value, str) and value:
                self._context.addWidget(label(value, "muted", wrap=True))
        excerpt = label(str(evidence.get("text") or ""), "excerpt", wrap=True)
        excerpt.setObjectName("excerpt")
        excerpt.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._context.addWidget(excerpt)
        locators = evidence.get("locators")
        if isinstance(locators, list):
            names = {
                "paragraph": "段落",
                "start": "起始秒",
                "end": "结束秒",
                "timestamp": "时间",
                "image_index": "图片",
            }
            for locator in locators:
                if isinstance(locator, dict):
                    location = " · ".join(
                        f"{names.get(key, key)} {value}"
                        for key, value in locator.items()
                        if isinstance(value, (int, float, str))
                    )
                    if location:
                        self._context.addWidget(label(location, "muted", wrap=True))
        url = QUrl(str(evidence.get("citation_url") or evidence.get("source_url") or ""))
        if url.isValid() and url.scheme() in {"http", "https"} and url.host():
            source = button("打开原文")
            source.clicked.connect(lambda: QDesktopServices.openUrl(url))
            self._context.addWidget(source)
        back = button("返回已确认条件")
        back.clicked.connect(self._draw_context)
        self._context.addWidget(back)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._worker.isRunning():
            event.ignore()
            if not self._closing:
                self._closing = True
                self.setEnabled(False)
                self._worker.shutdown()
            return
        super().closeEvent(event)
