"""每租户 token 预算(Tier 3 成本闸):窗口内累计消耗封顶——直接挡住"一个租户烧光预算"。

资源预计结论:LLM API 成本是 10k-100k 并发的绑定约束(~$100k-$1M+/月)。所以预算闸是 Tier 3
最该有的东西:每租户每窗口(如每天/每月)给 limit_tokens,累计超了 try_charge 返回 False → 上层拒
绝或降级。窗口滚动重置(到点新窗口从 0 计)。注入时钟做确定性测试。线程安全。

预留-结算模型:调用前按"预估 token"try_charge(预扣),调用后 settle 用真实 token 校正差额——
防止预估不准导致预算漂移(LLM 实际 token 常与预估有出入)。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from agent_py_agent.agent.llm_scale.rate_limiter import _now_ms


@dataclass
class _WindowState:
    spent: int
    window_start_ms: int


class TokenBudget:
    """每租户 token 预算:窗口 window_seconds 内累计 ≤ limit_tokens。滚动窗口,到点重置。"""

    def __init__(self, limit_tokens: int, window_seconds: float, *, clock: Callable[[], int] | None = None) -> None:
        self._limit = int(limit_tokens)
        self._window_ms = int(window_seconds * 1000)
        self._clock = clock or _now_ms
        self._states: dict[str, _WindowState] = {}
        self._lock = threading.Lock()

    def try_charge(self, tenant: str, tokens: int) -> bool:
        """预扣 tokens 个。当前窗口累计仍 ≤ limit 则扣下并返回 True;否则不扣返回 False。"""
        with self._lock:
            state = self._roll_locked(tenant)
            if state.spent + tokens > self._limit:
                return False
            state.spent += tokens
            return True

    def settle(self, tenant: str, estimated: int, actual: int) -> None:
        """调用后用真实 token 校正:把预扣的 estimated 调成 actual(差额加到当前窗口)。"""
        delta = actual - estimated
        with self._lock:
            state = self._roll_locked(tenant)
            state.spent = max(0, state.spent + delta)

    def remaining(self, tenant: str) -> int:
        with self._lock:
            state = self._roll_locked(tenant)
            return max(0, self._limit - state.spent)

    def spent(self, tenant: str) -> int:
        with self._lock:
            return self._roll_locked(tenant).spent

    def _roll_locked(self, tenant: str) -> _WindowState:
        now = self._clock()
        state = self._states.get(tenant)
        if state is None or now - state.window_start_ms >= self._window_ms:
            state = _WindowState(spent=0, window_start_ms=now)  # 新租户或窗口到期 → 重置
            self._states[tenant] = state
        return state
