from __future__ import annotations

"""One cancellation token propagated through every cancellable tool boundary."""

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

_CURRENT_TOKEN: ContextVar[CancellationToken | None] = ContextVar(
    "my_agent_tool_cancellation_token",
    default=None,
)


@dataclass
class CancellationToken:
    _event: threading.Event = field(default_factory=threading.Event, repr=False)
    _external_check: Callable[[], bool] | None = field(default=None, repr=False)
    _callback_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _callbacks: dict[int, Callable[[], object]] = field(default_factory=dict, repr=False)
    reason: str = ""

    def cancel(self, reason: str = "cancelled") -> None:
        self.reason = str(reason or "cancelled").strip() or "cancelled"
        self._event.set()
        with self._callback_lock:
            callbacks = tuple(self._callbacks.values())
        for callback in callbacks:
            try:
                callback()
            except Exception:
                continue

    @property
    def cancelled(self) -> bool:
        if self._event.is_set():
            return True
        check = self._external_check
        if check is None:
            return False
        try:
            return bool(check())
        except Exception:
            return True

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise ToolCancelled(self.reason or "cancelled")

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for cancellation without exposing the token's event object."""

        return self._event.wait(timeout)

    @contextmanager
    def register_callback(self, callback: Callable[[], object]) -> Iterator[None]:
        """Install an idempotent abort hook for one blocking operation."""

        key = id(callback)
        run_now = False
        with self._callback_lock:
            if self.cancelled:
                run_now = True
            else:
                self._callbacks[key] = callback
        if run_now:
            try:
                callback()
            except Exception:
                pass
        try:
            yield
        finally:
            with self._callback_lock:
                self._callbacks.pop(key, None)


class ToolCancelled(RuntimeError):
    pass


@contextmanager
def bind_cancellation_token(token: object) -> Iterator[None]:
    """Bind the host token only for the current handler thread/context."""

    value = token if isinstance(token, CancellationToken) else None
    context_token = _CURRENT_TOKEN.set(value)
    try:
        yield
    finally:
        _CURRENT_TOKEN.reset(context_token)


def current_cancellation_token() -> CancellationToken | None:
    return _CURRENT_TOKEN.get()


def cancellation_requested() -> bool:
    token = current_cancellation_token()
    return bool(token and token.cancelled)


def raise_if_cancelled() -> None:
    token = current_cancellation_token()
    if token is not None:
        token.raise_if_cancelled()


@contextmanager
def register_cancellation_callback(
    callback: Callable[[], object],
) -> Iterator[None]:
    token = current_cancellation_token()
    if token is None:
        yield
        return
    with token.register_callback(callback):
        yield


__all__ = [
    "CancellationToken",
    "ToolCancelled",
    "bind_cancellation_token",
    "cancellation_requested",
    "current_cancellation_token",
    "raise_if_cancelled",
    "register_cancellation_callback",
]
