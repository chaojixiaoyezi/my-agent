
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ArtifactReadBudgetRequest:
    run_id: str
    requested_chars: int
    now: float | None = None


class ArtifactReadBudget:
    def __init__(self, *, window_seconds: int, max_chars: int) -> None:
        self.window_seconds = max(0, int(window_seconds or 0))
        self.max_chars = max(0, int(max_chars or 0))
        self._events_by_run: dict[str, list[tuple[float, int]]] = {}

    def preflight(self, request: ArtifactReadBudgetRequest) -> str:
        run_id = str(request.run_id or "").strip()
        if not run_id or self.window_seconds <= 0 or self.max_chars <= 0:
            return ""
        requested = max(0, int(request.requested_chars))
        if requested <= 0:
            return ""
        now = float(time.monotonic() if request.now is None else request.now)
        events = self._fresh_events(run_id, now)
        used = sum(chars for _, chars in events)
        if used + requested > self.max_chars:
            return _budget_error(run_id, self.max_chars, self.window_seconds, f"已用 {used} 字符，本次请求 {requested} 字符")
        return ""

    def commit(self, run_id: str, chars: int, *, now: float | None = None) -> None:
        scoped_run = str(run_id or "").strip()
        if not scoped_run or self.window_seconds <= 0 or self.max_chars <= 0:
            return
        timestamp = float(time.monotonic() if now is None else now)
        events = self._fresh_events(scoped_run, timestamp)
        events.append((timestamp, max(0, int(chars))))
        self._events_by_run[scoped_run] = events

    def _fresh_events(self, run_id: str, now: float) -> list[tuple[float, int]]:
        events = [
            item
            for item in self._events_by_run.get(run_id, [])
            if item[0] > now - self.window_seconds
        ]
        self._events_by_run[run_id] = events
        return events


def _budget_error(run_id: str, max_chars: int, window_seconds: int, detail: str) -> str:
    return (
        f"artifact 读取预算已达到：run_id={run_id} 最近 {window_seconds} 秒最多读取 {max_chars} 字符。"
        f"原因：{detail}。请改用 mode=search/head/tail 或更小 max_chars 读取窄片段；"
        "如果仍缺证据，请向父级上报需要继续读取或提高预算。"
    )
