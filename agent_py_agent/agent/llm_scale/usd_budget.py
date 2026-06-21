"""每租户 USD 预算(审计 #19 成本闸,token 预算的金额孪生):窗口内累计 USD 封顶。

token 预算挡"烧光 token",USD 预算挡"烧光钱"——贵模型少量 token 也可能超支,只有 token 维度无法
对齐成本。默认 limit_usd<=0 = 不限额(永远放行,不改现有行为),配了上限才在 precheck 熔断。
预留-结算同 TokenBudget:预估成本预扣,真实成本 settle 校正。注入时钟确定性测试,线程安全。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from agent_py_agent.agent.llm_scale.rate_limiter import _now_ms


@dataclass
class _UsdWindow:
    spent_usd: float
    window_start_ms: int


class UsdBudget:
    """每租户 USD 预算:窗口 window_seconds 内累计 ≤ limit_usd。limit_usd<=0=不限额。滚动窗口重置。"""

    def __init__(self, limit_usd: float, window_seconds: float, *, clock: Callable[[], int] | None = None) -> None:
        self._limit = float(limit_usd)
        self._window_ms = int(window_seconds * 1000)
        self._clock = clock or _now_ms
        self._states: dict[str, _UsdWindow] = {}
        self._lock = threading.Lock()

    @property
    def unlimited(self) -> bool:
        return self._limit <= 0  # 未配上限:不限额(默认,不改现有行为)

    def try_charge(self, tenant: str, cost_usd: float) -> bool:
        """预扣 cost_usd。不限额或当前窗口累计仍 ≤ limit 则扣下返回 True;否则不扣返回 False。"""
        if self.unlimited:
            return True
        with self._lock:
            state = self._roll_locked(tenant)
            if state.spent_usd + cost_usd > self._limit:
                return False
            state.spent_usd += cost_usd
            return True

    def settle(self, tenant: str, estimated_usd: float, actual_usd: float) -> None:
        """调用后用真实成本校正预扣(估算与实际有出入时纠偏)。不限额则无操作。"""
        if self.unlimited:
            return
        with self._lock:
            state = self._roll_locked(tenant)
            state.spent_usd = max(0.0, state.spent_usd + (actual_usd - estimated_usd))

    def spent(self, tenant: str) -> float:
        with self._lock:
            return self._roll_locked(tenant).spent_usd

    def remaining(self, tenant: str) -> float:
        if self.unlimited:
            return float("inf")
        with self._lock:
            return max(0.0, self._limit - self._roll_locked(tenant).spent_usd)

    def _roll_locked(self, tenant: str) -> _UsdWindow:
        now = self._clock()
        state = self._states.get(tenant)
        if state is None or now - state.window_start_ms >= self._window_ms:
            state = _UsdWindow(spent_usd=0.0, window_start_ms=now)  # 新租户或窗口到期 → 重置
            self._states[tenant] = state
        return state
