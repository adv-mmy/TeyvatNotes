"""设置对话框、补条件表单与通用文本控件。"""

import re
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
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
    DesktopError,
    load_config,
    save_config,
)
from teyvatnotes.domain.guides import GuideKind
from teyvatnotes.domain.json_types import JsonValue

ERRORS = {
    "client_config_required": "请先在设置中填写本地攻略服务的 HTTPS 地址。",
    "client_config_invalid": "地址必须是 HTTPS 源，不带路径、参数或凭据。",
    "client_config_unavailable": "无法读取客户端配置，请检查本机文件权限。",
    "client_config_conflict": "配置已被外部修改，请重新打开设置。",
    "client_ca_invalid": "本地 HTTPS 证书文件无效。",
    "guide_service_unavailable": "无法连接攻略服务，请检查服务是否启动和证书是否受信。",
    "guide_response_invalid": "攻略服务返回了不符合约定的数据。",
    "authentication_required": "匿名会话已失效。重新提问时会获取新凭据，不会自动重发。",
    "quota_exceeded": "今日匿名额度已用完，请在 UTC 日期切换后重试。",
    "guide_busy": "攻略服务繁忙，请稍后再试。",
    "knowledge_unavailable": "攻略数据库不可用，本轮未继续查询。",
    "model_unavailable": "服务端模型不可用，请联系维护者。",
    "model_timeout": "服务端模型超时，没有自动重试。",
    "request_timeout": "等待攻略服务超时，没有自动重试。",
    "source_busy": "资料补查繁忙，请稍后再试。",
    "source_timeout": "资料补查超时，没有自动重试。",
    "source_unavailable": "授权资料源暂不可用。",
    "conversation_storage_unavailable": "无法打开本地会话库，请检查权限或备份。",
    "conversation_interrupted": "这轮在旧应用中未完成，迁移后不会自动重试。",
    "conversation_storage_version": "会话库版本不兼容，请使用独立迁移工具。",
    "conversation_not_found": "此会话不可用。请刷新列表。",
    "conversation_turn_budget_exceeded": "此会话已到轮次上限，可以新建攻略。",
    "conversation_scope_change": "已确认的角色、版本和攻略类型需通过新建会话更换。",
    "conversation_conflict": "会话已有更新，请刷新后核对。",
    "invalid_conversation_input": "请检查问题、版本或持有名单的格式。",
    "invalid_question": "请填写有效问题，长度最多 1000 字符。",
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
    """编辑服务地址；不再接受模型密钥或数据库配置。"""

    def __init__(self, parent: QWidget, path: Path) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置 · 攻略服务")
        self.setMinimumWidth(460)
        self._url = QLineEdit()
        self._url.setObjectName("serviceUrl")
        self._url.setMaxLength(2048)
        self._url.setPlaceholderText("https://127.0.0.1:8443")
        self._status = label("", "muted", wrap=True)
        layout = QVBoxLayout(self)
        layout.addWidget(label("服务地址", "sectionTitle"))
        layout.addWidget(self._url)
        layout.addWidget(self._status)
        controls = QHBoxLayout()
        close = button("关闭")
        close.clicked.connect(self.reject)
        save = button("保存", "primary")
        controls.addWidget(close)
        controls.addWidget(save)
        layout.addLayout(controls)
        try:
            config = load_config(path)
            self._url.setText(config.service_url)
        except DesktopError as error:
            save.setEnabled(False)
            self._status.setText(error_text(error.code))
        else:

            def update() -> None:
                try:
                    save_config(config, self._url.text().strip())
                except DesktopError as error:
                    self._status.setText(error_text(error.code))
                else:
                    self.accept()

            save.clicked.connect(update)


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

    def __init__(self, request: QuestionRequest) -> None:
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
