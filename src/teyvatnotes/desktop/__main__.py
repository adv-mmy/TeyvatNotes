"""提瓦特手记的可执行入口。"""

import sys

from PySide6.QtWidgets import QApplication

from teyvatnotes.desktop.config import data_directory
from teyvatnotes.desktop.ui import WorkbenchWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("TeyvatNotes")
    directory = data_directory()
    window = WorkbenchWindow(directory)
    window.show()
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
