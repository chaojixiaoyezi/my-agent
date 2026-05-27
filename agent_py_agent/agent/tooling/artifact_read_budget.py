# LLM: Artifact body reads need a per-run character budget separate from ordinary tool-call counts.
# 模块用途: 控制 read_artifact 在单个 run 内的滚动读取字符预算，避免反复展开大 artifact。

from __future__ import annotations

import time
from dataclasses import dataclass


# LLM: ArtifactReadBudgetRequest bundles the current read estimate and run scope for budget checks.
# 类用途: 保存 read_artifact 的 run_id、预计读取字符数、窗口和时间戳，供预算器判断是否放行。
@dataclass(frozen=True)
class ArtifactReadBudgetRequest:
    run_id: str
    requested_chars: int
    now: float | None = None


# LLM: ArtifactReadBudget tracks lightweight in-memory read counters per subagent run.
# 类用途: 按 run_id 记录 artifact 正文读取字符数；没有 run_id 或配置为 0 时不限制。
class ArtifactReadBudget:
    # LLM: ArtifactReadBudget.__init__ stores the rolling-window policy and starts with no events.
    # 函数用途: 初始化 artifact 读取预算器；window/max_chars 任一为 0 表示关闭。
    def __init__(self, *, window_seconds: int, max_chars: int) -> None:
        self.window_seconds = max(0, int(window_seconds or 0))
        self.max_chars = max(0, int(max_chars or 0))
        self._events_by_run: dict[str, list[tuple[float, int]]] = {}

    # LLM: preflight rejects over-budget bounded artifact body reads before the file body is loaded.
    # 函数用途: 在读取有界 artifact 正文前估算是否会超过 run 预算；max_chars=0 兼容放行但成功后仍计费。
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

    # LLM: commit records actual returned characters after a successful bounded artifact read.
    # 函数用途: 读取成功后登记实际返回字符数，供后续 read_artifact 预算判断。
    def commit(self, run_id: str, chars: int, *, now: float | None = None) -> None:
        scoped_run = str(run_id or "").strip()
        if not scoped_run or self.window_seconds <= 0 or self.max_chars <= 0:
            return
        timestamp = float(time.monotonic() if now is None else now)
        events = self._fresh_events(scoped_run, timestamp)
        events.append((timestamp, max(0, int(chars))))
        self._events_by_run[scoped_run] = events

    # LLM: _fresh_events prunes old budget entries without touching sibling run state.
    # 函数用途: 返回当前窗口内的读取事件，并把过期事件从对应 run 中清理掉。
    def _fresh_events(self, run_id: str, now: float) -> list[tuple[float, int]]:
        events = [
            item
            for item in self._events_by_run.get(run_id, [])
            if item[0] > now - self.window_seconds
        ]
        self._events_by_run[run_id] = events
        return events


# LLM: _budget_error gives the model a recoverable instruction rather than a silent hard stop.
# 函数用途: 生成 artifact 读取预算命中的中文错误，提示改用 search/head/tail 或向父级上报。
def _budget_error(run_id: str, max_chars: int, window_seconds: int, detail: str) -> str:
    return (
        f"artifact 读取预算已达到：run_id={run_id} 最近 {window_seconds} 秒最多读取 {max_chars} 字符。"
        f"原因：{detail}。请改用 mode=search/head/tail 或更小 max_chars 读取窄片段；"
        "如果仍缺证据，请向父级上报需要继续读取或提高预算。"
    )
