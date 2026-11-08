"""Qt 主线程与后台 asyncio 事件循环之间的桥接。"""

import asyncio
from collections.abc import Coroutine
from concurrent.futures import Future
from threading import Lock
from typing import Any

from PySide6.QtCore import QThread, Signal

from teyvatnotes.application.conversations import ConversationError
from teyvatnotes.desktop.config import DesktopError
from teyvatnotes.desktop.service import Workbench
from teyvatnotes.domain.guides import GuideImportError


class AsyncWorker(QThread):
    ready = Signal()
    delivered = Signal(str, object)
    failed = Signal(str, str)

    def __init__(self, workbench: Workbench) -> None:
        super().__init__()
        self.workbench = workbench
        self._loop: asyncio.AbstractEventLoop | None = None
        self._state_lock = Lock()
        self._closing = False

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with self._state_lock:
            self._loop = loop
            closing = self._closing
            if closing:
                loop.call_soon(loop.stop)
        if not closing:
            self.ready.emit()
        loop.run_forever()
        with self._state_lock:
            self._loop = None
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()

    def dispatch(self, key: str, operation: Coroutine[Any, Any, object]) -> None:
        with self._state_lock:
            loop = self._loop
            if loop is None or self._closing:
                operation.close()
                return
            future = asyncio.run_coroutine_threadsafe(operation, loop)
        future.add_done_callback(lambda done: self._deliver(key, done))

    def _deliver(self, key: str, future: Future[object]) -> None:
        if self._closing or future.cancelled():
            return
        try:
            result = future.result()
        except (ConversationError, DesktopError, GuideImportError) as error:
            self.failed.emit(key, error.code)
        except Exception:
            self.failed.emit(key, "request_failed")
        else:
            self.delivered.emit(key, result)

    def shutdown(self) -> None:
        with self._state_lock:
            if self._closing:
                return
            self._closing = True
            loop = self._loop
            if loop is not None:
                asyncio.run_coroutine_threadsafe(self._drain(loop), loop)

    async def _drain(self, loop: asyncio.AbstractEventLoop) -> None:
        pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        try:
            await asyncio.gather(*pending, return_exceptions=True)
            await self.workbench.close()
        finally:
            loop.call_soon(loop.stop)
