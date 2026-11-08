"""设置对话框、补条件表单与通用文本控件。"""

import re
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from teyvatnotes.application.conversations import ConversationError, QuestionPatch
from teyvatnotes.application.question_preparation import QuestionRequest
from teyvatnotes.desktop.config import (
    ConfigSnapshot,
    DesktopError,
    load_config,
    save_model,
)
from teyvatnotes.domain.guides import GuideKind
from teyvatnotes.domain.task import JsonValue

ERRORS = {
    "config_conflict": "配置文件已被修改。请重新读取后再保存。",
    "config_invalid_model": "请检查 HTTPS 地址、Responses 协议、型号和 API Key。",
    "config_key_required": "请填写与此接口地址对应的 API Key。",
    "config_unavailable": "无法读取或写入配置文件，请检查格式或文件权限。",
    "conversation_not_found": "此会话不可用。请刷新列表。",
    "conversation_storage_unavailable": "会话库暂不可用，请核对连接并重试。",
    "conversation_turn_budget_exceeded": "此会话已到轮次上限，可以新建攻略。",
    "conversation_scope_change": "已确认的角色、版本和攻略类型需通过新建会话更换。",
    "conversation_conflict": "会话已有更新，请刷新后核对。",
    "database_config_required": "配置文件需要 PostgreSQL 的 DATABASE_URL 和会话所有者 UUID。",
    "database_unavailable": "无法读取攻略数据库，请检查连接和既有迁移。",
    "demo_failure": "演示请求失败。你可以修改输入后再次发送。",
    "invalid_conversation_input": "请检查问题、版本或持有名单的格式。",
    "invalid_question": "请填写有效问题，长度最多 1000 字符。",
    "model_key_not_configured": "请在设置中填写模型 API Key。",
    "invalid_model_configuration": "模型配置有误，请检查设置。",
    "conversation_timeout": "本轮等待超时，请核对会话后再继续。",
}


def error_text(code: str) -> str:
    return ERRORS.get(code, "本轮操作未完成，请核对条件和服务状态后再试。")


def label(text: str, style: str = "", *, wrap: bool = False) -> QLabel:
    control = QLabel(text)
    control.setTextFormat(Qt.TextFormat.PlainText)
    control.setWordWrap(wrap)
    if style:
        control.setObjectName(style)
    return control


def button(text: str, style: str = "") -> QPushButton:
    control = QPushButton(text)
    if style:
        control.setObjectName(style)
    control.setCursor(Qt.CursorShape.PointingHandCursor)
    return control


class SettingsDialog(QDialog):
    def __init__(
        self, parent: QWidget, current_path: Path, default_path: Path, *, allow_switch: bool
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置 · 模型 API")
        self.setMinimumWidth(530)
        self.selected = current_path
        self._default = default_path
        self._allow_switch = allow_switch
        self._snapshot: ConfigSnapshot | None = None
        self._path_label = label("", "muted", wrap=True)
        self._status = label("", "muted", wrap=True)
        self._url = QLineEdit()
        self._url.setObjectName("baseUrl")
        self._url.setPlaceholderText("https://example.com/v1")
        self._url.setMaxLength(2048)
        self._model = QLineEdit()
        self._model.setObjectName("modelName")
        self._model.setMaxLength(160)
        self._key = QLineEdit()
        self._key.setObjectName("modelKey")
        self._key.setEchoMode(QLineEdit.EchoMode.Password)
        self._key.setMaxLength(512)
        self._save = button("保存到 .env", "primary")
        self._save.setObjectName("saveModel")
        self._save.clicked.connect(self._write)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)
        layout.addWidget(label("默认模型 API", "heading"))
        layout.addWidget(label("配置保存后，从下一轮真实问答起使用。", "muted"))
        layout.addWidget(self._path_label)
        paths = QHBoxLayout()
        choose = button("选用配置文件")
        choose.setEnabled(self._allow_switch)
        choose.clicked.connect(self._choose)
        default = button("使用默认位置")
        default.setEnabled(self._allow_switch)
        default.clicked.connect(lambda: self._load(self._default))
        refresh = button("重新读取")
        refresh.clicked.connect(lambda: self._load(self.selected))
        paths.addWidget(choose)
        paths.addWidget(default)
        paths.addWidget(refresh)
        paths.addStretch()
        layout.addLayout(paths)
        fields = QFormLayout()
        fields.setSpacing(9)
        fields.addRow("HTTPS Base URL", self._url)
        fields.addRow("模型型号", self._model)
        fields.addRow("API Key", self._key)
        layout.addLayout(fields)
        layout.addWidget(
            label("同一地址可留空保留密钥；更换地址需填写对应密钥。", "muted", wrap=True)
        )
        layout.addWidget(self._status)
        actions = QHBoxLayout()
        actions.addStretch()
        done = button("完成")
        done.clicked.connect(self.accept)
        actions.addWidget(done)
        actions.addWidget(self._save)
        layout.addLayout(actions)
        self._load(self.selected)

    def _choose(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选用配置文件", str(self.selected.parent), "环境配置 (*.env .env);;所有文件 (*)"
        )
        if path:
            self._load(Path(path))

    def _load(self, path: Path) -> None:
        self._key.clear()
        try:
            snapshot = load_config(path)
        except DesktopError as error:
            self._status.setText(error_text(error.code))
            if path == self.selected:
                self._snapshot = None
                self._save.setEnabled(False)
            return
        self._accept(snapshot)

    def _accept(self, snapshot: ConfigSnapshot) -> None:
        self.selected = snapshot.path
        self._snapshot = snapshot
        self._path_label.setText(str(snapshot.path))
        self._url.setText(snapshot.base_url)
        self._model.setText(snapshot.model)
        self._key.clear()
        self._key.setPlaceholderText(
            "已配置 · 留空保留" if snapshot.has_key else "输入模型 API Key"
        )
        self._save.setEnabled(True)
        self._status.setText("")

    def _write(self) -> None:
        if self._snapshot is None:
            return
        try:
            snapshot = save_model(
                self._snapshot,
                self._url.text().strip(),
                self._model.text().strip(),
                self._key.text().strip(),
            )
        except DesktopError as error:
            self._status.setText(error_text(error.code))
            if error.code == "config_conflict":
                self._save.setEnabled(False)
            return
        self._accept(snapshot)
        self._status.setText("已保存配置。")


def _tokens(text: str) -> list[JsonValue]:
    return list(
        dict.fromkeys(value.strip() for value in re.split(r"[，、,\n]", text) if value.strip())
    )


class Inventory(QWidget):
    def __init__(self, names: tuple[str, ...] | None) -> None:
        super().__init__()
        self.mode = QComboBox()
        self.mode.addItems(["尚未确认", "填写持有名单", "确认没有"])
        self.names = QLineEdit()
        self.names.setPlaceholderText("用顿号或逗号分隔")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.mode)
        layout.addWidget(self.names)
        self.mode.currentIndexChanged.connect(lambda index: self.names.setVisible(index == 1))
        self.set_items(names)

    def set_items(self, names: tuple[str, ...] | None) -> None:
        self.mode.setCurrentIndex(0 if names is None else 1 if names else 2)
        self.names.setText("、".join(names or ()))
        self.names.setVisible(bool(names))

    def value(self) -> list[JsonValue] | None:
        index = self.mode.currentIndex()
        if index == 0:
            return None
        if index == 2:
            return []
        names = _tokens(self.names.text())
        if not names:
            raise DesktopError("invalid_conversation_input")
        return names


class ConditionsForm(QFrame):
    submitted = Signal(object)
    cancelled = Signal()

    def __init__(self, request: QuestionRequest, *, demo: bool) -> None:
        super().__init__()
        self.setObjectName("conditionForm")
        self._request = request
        self._version = QLineEdit(request.game_version or "")
        locked_version = bool(
            request.game_version and request.game_version.lower() not in {"未知", "unknown"}
        )
        self._version.setReadOnly(locked_version)
        self._entity = QLineEdit(request.entity or "")
        self._entity.setReadOnly(bool(request.entity))
        self._kind = QComboBox()
        self._kind.addItems(["角色攻略", "任务攻略"])
        self._kind.setCurrentIndex(int(request.guide_kind is GuideKind.QUEST))
        self._kind.setEnabled(request.guide_kind is None)
        self._keywords = QLineEdit("、".join(request.keywords))
        self._location = QLineEdit(request.location or "")
        self._progress = QLineEdit(request.progress or "")
        self._characters = Inventory(request.owned_characters)
        self._equipment = Inventory(request.owned_equipment)
        self._status = label("", "muted", wrap=True)
        self._save = button("确认并继续", "primary")
        self._save.clicked.connect(self._submit)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 13, 15, 13)
        layout.setSpacing(8)
        title = QHBoxLayout()
        title.addWidget(label("确认玩家条件", "sectionTitle"))
        title.addStretch()
        if demo:
            fill = button("填入示例")
            fill.clicked.connect(self._fill_demo)
            title.addWidget(fill)
        layout.addLayout(title)
        fields = QGridLayout()
        fields.setHorizontalSpacing(12)
        for row, pair in enumerate(
            (
                (("游戏版本", self._version), ("攻略类型", self._kind)),
                (("角色或任务名称", self._entity), ("检索词", self._keywords)),
                (("持有角色", self._characters), ("持有装备", self._equipment)),
            )
        ):
            for column, (text, control) in enumerate(pair):
                fields.addWidget(label(text, "muted"), row * 2, column)
                fields.addWidget(control, row * 2 + 1, column)
        quest = QWidget()
        quest_fields = QFormLayout(quest)
        quest_fields.setContentsMargins(0, 0, 0, 0)
        quest_fields.addRow("地点", self._location)
        quest_fields.addRow("当前进度", self._progress)
        quest.setVisible(request.guide_kind is GuideKind.QUEST)
        self._kind.currentIndexChanged.connect(lambda index: quest.setVisible(index == 1))
        layout.addLayout(fields)
        layout.addWidget(quest)
        layout.addWidget(self._status)
        actions = QHBoxLayout()
        actions.addStretch()
        cancel = button("收起")
        cancel.clicked.connect(self.cancelled.emit)
        actions.addWidget(cancel)
        actions.addWidget(self._save)
        layout.addLayout(actions)

    def _fill_demo(self) -> None:
        if not self._version.isReadOnly():
            self._version.setText("1.0")
        self._characters.set_items(("风铃旅人", "苔羽", "星见"))
        self._equipment.set_items(("旅途木剑",))

    def _submit(self) -> None:
        if not self._version.text().strip() or not self._entity.text().strip():
            self._status.setText("请填写游戏版本和角色或任务名称。")
            return
        fields: dict[str, JsonValue] = {
            "question": self._request.question,
            "game_version": self._version.text().strip(),
            "guide_kind": "quest" if self._kind.currentIndex() == 1 else "character",
            "entity": self._entity.text().strip(),
            "keywords": _tokens(self._keywords.text()),
        }
        if self._kind.currentIndex() == 1:
            fields.update(
                location=self._location.text().strip() or None,
                progress=self._progress.text().strip() or None,
            )
        try:
            fields["owned_characters"] = self._characters.value()
            fields["owned_equipment"] = self._equipment.value()
            patch = QuestionPatch(fields)
        except (ConversationError, DesktopError) as error:
            self._status.setText(error_text(error.code))
            return
        self.submitted.emit(patch)
