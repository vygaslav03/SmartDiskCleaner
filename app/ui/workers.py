"""Фоновые задачи: QObject-worker в отдельном QThread.

Сигналы worker'а проходят через _Relay — объект, живущий в GUI-потоке, поэтому
колбэки (в т.ч. lambda) гарантированно выполняются в главном потоке
(queued connection), и интерфейс не блокируется.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Signal, Slot

from app.utils.logger import get_logger

log = get_logger("workers")


class _Worker(QObject):
    progress = Signal(object)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[..., Any], args: tuple, kwargs: dict, cancel: threading.Event) -> None:
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self._cancel = cancel

    @Slot()
    def run(self) -> None:
        try:
            result = self._fn(
                *self._args, progress_cb=self.progress.emit, cancel_event=self._cancel, **self._kwargs
            )
        except Exception as exc:  # noqa: BLE001 - любая ошибка задачи показывается пользователю
            log.exception("Ошибка фоновой задачи")
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.finished.emit(result)


class _Relay(QObject):
    progress = Signal(object)
    finished = Signal(object)
    failed = Signal(str)


class Task(QObject):
    """Дескриптор запущенной задачи. Храните ссылку на него, пока задача выполняется."""

    def __init__(
        self,
        parent: QObject,
        fn: Callable[..., Any],
        *args: Any,
        on_progress: Callable[[Any], None] | None = None,
        on_finished: Callable[[Any], None] | None = None,
        on_failed: Callable[[str], None] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(parent)
        self.cancel_event = threading.Event()
        self._running = True
        self._relay = _Relay(self)
        self._thread = QThread()
        self._worker = _Worker(fn, args, kwargs, self.cancel_event)
        self._worker.moveToThread(self._thread)

        # worker (фоновый поток) -> relay (GUI-поток): queued connection
        self._worker.progress.connect(self._relay.progress)
        self._worker.finished.connect(self._relay.finished)
        self._worker.failed.connect(self._relay.failed)
        if on_progress:
            self._relay.progress.connect(on_progress)
        self._relay.finished.connect(self._mark_done)
        self._relay.failed.connect(self._mark_done)
        if on_finished:
            self._relay.finished.connect(on_finished)
        if on_failed:
            self._relay.failed.connect(on_failed)

        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.start()

    def _mark_done(self, *_: Any) -> None:
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def cancel(self) -> None:
        self.cancel_event.set()

    def shutdown(self, timeout_ms: int = 5000) -> None:
        """Отмена и ожидание завершения потока (при закрытии окна)."""
        self.cancel_event.set()
        if self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(timeout_ms)
