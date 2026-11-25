"""Progress reporting and cancellation shared by every long-running operation.

Core functions take an optional ``progress`` callback and ``cancel`` token so the
TUI, CLI and tests can all drive them the same way.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Optional


class Cancelled(Exception):
    """Raised inside an operation when the user cancelled it."""


class CancelToken:
    def __init__(self) -> None:
        self._ev = threading.Event()

    def cancel(self) -> None:
        self._ev.set()

    @property
    def cancelled(self) -> bool:
        return self._ev.is_set()

    def check(self) -> None:
        if self._ev.is_set():
            raise Cancelled()


@dataclass
class ProgressEvent:
    done: int
    total: int
    message: str = ""

    @property
    def fraction(self) -> float:
        return 0.0 if self.total <= 0 else min(1.0, self.done / self.total)


ProgressFn = Callable[[ProgressEvent], None]


class Reporter:
    """Small helper that wraps an optional callback + cancel token."""

    def __init__(self, progress: Optional[ProgressFn] = None,
                 cancel: Optional[CancelToken] = None, total: int = 0, message: str = "") -> None:
        self.fn = progress
        self.cancel = cancel
        self.total = total
        self.done = 0
        self.message = message

    def check(self) -> None:
        if self.cancel is not None:
            self.cancel.check()

    def set_total(self, total: int, message: str | None = None) -> None:
        self.total = total
        self.done = 0
        if message is not None:
            self.message = message
        self._emit()

    def advance(self, n: int, message: str | None = None) -> None:
        self.done += n
        if message is not None:
            self.message = message
        self.check()
        self._emit()

    def update(self, done: int, message: str | None = None) -> None:
        self.done = done
        if message is not None:
            self.message = message
        self.check()
        self._emit()

    def note(self, message: str) -> None:
        self.message = message
        self._emit()

    def _emit(self) -> None:
        if self.fn is not None:
            self.fn(ProgressEvent(self.done, self.total, self.message))
