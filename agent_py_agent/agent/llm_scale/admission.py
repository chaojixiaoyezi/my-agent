"""LLM 调用准入控制(Tier 3 合一闸):每次 LLM 调用前过三关——租户限流 + token 预算 + 全局并发。

用法::

    adm = LLMAdmission(rate_limiter, budget, concurrency)
    verdict = adm.precheck(tenant, estimated_tokens=2000)   # 限流 + 预算预扣
    if not verdict.admitted:
        return reject(verdict.reason)        # rate_limited / budget_exceeded
    with adm.slot(timeout=5):                # 占并发槽(满则等;超时抛 ConcurrencyTimeout)
        actual = call_llm(...)
    adm.settle(tenant, estimated_tokens, actual)   # 真实 token 校正预算

顺序:先限流(扣令牌,廉价、会回补)后预算(预扣 token,真实成本)——限流挡掉的请求不白扣预算。
预算挡掉时已扣的那一个令牌可忽略(会回补)。并发槽在真正调用时占,不在 precheck 里(precheck 不该阻塞)。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from agent_py_agent.agent.llm_scale.concurrency import ConcurrencyLimiter
from agent_py_agent.agent.llm_scale.rate_limiter import TenantRateLimiter
from agent_py_agent.agent.llm_scale.token_budget import TokenBudget


@dataclass(frozen=True)
class AdmissionResult:
    """准入裁决:admitted=放行;否则 reason 说明被哪关挡(rate_limited/budget_exceeded)。"""

    admitted: bool
    reason: str
    remaining_budget: int = 0


class LLMAdmission:
    """三关合一准入:限流 + 预算(precheck)+ 并发(slot)。稳定性 + 成本双闸。"""

    def __init__(self, rate_limiter: TenantRateLimiter, budget: TokenBudget, concurrency: ConcurrencyLimiter) -> None:
        self._rate = rate_limiter
        self._budget = budget
        self._conc = concurrency

    def precheck(self, tenant: str, estimated_tokens: int = 1) -> AdmissionResult:
        """限流 + 预算预扣。任一关不过返回 admitted=False 与原因(不占并发槽)。"""
        if not self._rate.allow(tenant):
            return AdmissionResult(False, "rate_limited", self._budget.remaining(tenant))
        if not self._budget.try_charge(tenant, estimated_tokens):
            return AdmissionResult(False, "budget_exceeded", self._budget.remaining(tenant))
        return AdmissionResult(True, "ok", self._budget.remaining(tenant))

    @contextmanager
    def slot(self, timeout: float | None = None) -> Iterator[None]:
        """占一个全局并发槽(防打爆 provider)。透传 ConcurrencyLimiter.slot。"""
        with self._conc.slot(timeout=timeout):
            yield

    def settle(self, tenant: str, estimated: int, actual: int) -> None:
        """调用后用真实 token 校正预算预扣(估算与实际有出入时纠偏)。"""
        self._budget.settle(tenant, estimated, actual)
