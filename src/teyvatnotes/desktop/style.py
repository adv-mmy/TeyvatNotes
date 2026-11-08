"""使用 Windows 默认字体，只调整字号、颜色和控件间距。"""

WORKBENCH_STYLE = """
QWidget { color: #233b31; }
QMainWindow, QDialog, QWidget#conversation { background: #f7f5ed; }
QWidget#sidebar { background: #eef0e5; border-right: 1px solid #dddfd0; }
QWidget#evidence { background: #fbfaf3; border-left: 1px solid #dddfd0; }
QWidget#toolbar { background: #fffdf8; border-bottom: 1px solid #dddfd0; }
QWidget#paper, QFrame#claim, QFrame#conditionForm, QFrame#composer {
    background: #fffdf8; border: 1px solid #dddfd0; border-radius: 10px;
}
QFrame#hero { background: #e6edde; border: 1px solid #d9e2d2; border-radius: 13px; }
QLabel#brand, QLabel#heading { font-size: 21px; font-weight: bold; color: #214534; }
QLabel#heroTitle { font-size: 27px; font-weight: bold; color: #214534; }
QLabel#sectionTitle { font-size: 15px; font-weight: bold; color: #284b3a; }
QLabel#muted { color: #596a5f; }
QLabel#eyebrow { color: #56705d; font-weight: bold; }
QLabel#mark { color: #fffdf7; background: #24523f; border-radius: 10px; font-size: 22px; }
QLabel#userMessage { background: #e5eddf; padding: 10px 13px; border-radius: 10px; }
QLabel#notice { background: #f6ebd4; color: #75471a; padding: 11px; border-radius: 8px; }
QLabel#excerpt { background: #eef2e8; border-left: 3px solid #467656; padding: 11px; }
QPushButton {
    background: #fffdf8; border: 1px solid #d8dbcf; border-radius: 7px; padding: 7px 11px;
}
QPushButton:hover { background: #e9eee3; }
QPushButton:focus, QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus {
    border: 1px solid #44815c;
}
QPushButton#primary, QPushButton#saveModel {
    background: #24523f; color: #fffdf7; border-color: #24523f; font-weight: bold;
}
QPushButton#primary:hover, QPushButton#saveModel:hover { background: #316c52; }
QPushButton#reference { background: #e5eee1; color: #275940; padding: 3px 8px; font-weight: bold; }
QPushButton#demoMode { background: #f5e9cf; color: #75471a; border-color: #e5d3ad; }
QPushButton:disabled { color: #69756a; background: #e9ebe3; border-color: #dbded2; }
QLineEdit, QPlainTextEdit, QComboBox {
    background: #fffdf8; border: 1px solid #d8dbcf; border-radius: 6px; padding: 6px;
}
QLineEdit[readOnly="true"] { background: #edf1e7; }
QListWidget { background: transparent; border: none; outline: none; }
QListWidget::item { padding: 8px; border-radius: 7px; }
QListWidget::item:selected { background: #dce9db; color: #244c37; }
QListWidget::item:hover:!selected { background: #e5ece0; }
QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QSplitter::handle { background: #e3e4d8; }
"""
