"""Tier 5 worker handler 测试:各层咬合——准入挡成本、并发槽、追踪关联、真实 token 结算。"""

from __future__ import annotations

from agent_py_agent.agent import worker_handler
from agent_py_agent.agent.llm_scale import (
    ConcurrencyLimiter,
    LLMAdmission,
    TenantRateLimiter,
    TokenBudget,
)
from agent_py_agent.agent.observability.tracing import inject, new_trace


def _fixed_clock() -> int:
    return 0


def _admission(*, burst: float = 100, budget: int = 10_000) -> LLMAdmission:
    return LLMAdmission(
        TenantRateLimiter(0, burst, clock=_fixed_clock),
        TokenBudget(budget, 3600, clock=_fixed_clock),
        ConcurrencyLimiter(8),
    )


def test_handle_happy_path_calls_downstream_and_settles() -> None:
    worker_handler.reset_for_test(_admission(budget=10_000))
    seen: list[dict] = []
    worker_handler.set_downstream(lambda payload, ctx: (seen.append(payload), 2500)[1])
    try:
        worker_handler.handle({"tenant": "acme", "estimated_tokens": 2000, "event": "x"})
        assert len(seen) == 1  # 准入放行 → 下游被调
        assert worker_handler._ADMISSION._budget.spent("acme") == 2500  # 预扣2000→settle到真实2500
    finally:
        worker_handler.set_downstream(None)
        worker_handler.reset_for_test()


def test_handle_budget_exceeded_skips_downstream() -> None:
    worker_handler.reset_for_test(_admission(budget=1000))  # 预算小
    called: list[int] = []
    worker_handler.set_downstream(lambda p, c: (called.append(1), 0)[1])
    try:
        worker_handler.handle({"tenant": "t", "estimated_tokens": 5000})  # 5000 > 1000
        assert called == []  # 超预算 → 不调 LLM(成本闸生效)
        assert ("t", "budget_exceeded") in worker_handler._REJECTED
    finally:
        worker_handler.set_downstream(None)
        worker_handler.reset_for_test()


def test_handle_rate_limited_skips_downstream() -> None:
    worker_handler.reset_for_test(_admission(burst=1, budget=10_000))  # 桶容量1、不补
    called: list[int] = []
    worker_handler.set_downstream(lambda p, c: (called.append(1), 10)[1])
    try:
        worker_handler.handle({"tenant": "t", "estimated_tokens": 10})  # 第1条:放行
        worker_handler.handle({"tenant": "t", "estimated_tokens": 10})  # 第2条:限流挡
        assert called == [1]  # 只放行了第一条
        assert ("t", "rate_limited") in worker_handler._REJECTED
    finally:
        worker_handler.set_downstream(None)
        worker_handler.reset_for_test()


def test_handle_propagates_trace_to_downstream() -> None:
    worker_handler.reset_for_test(_admission())
    root = new_trace()
    payload: dict = {"tenant": "t", "estimated_tokens": 10}
    inject(root, payload)  # ingress 注入 trace
    got: dict = {}
    worker_handler.set_downstream(lambda p, ctx: (got.update(trace_id=ctx.trace_id), 10)[1])
    try:
        worker_handler.handle(payload)
        assert got["trace_id"] == root.trace_id  # 下游拿到同一 trace(全链路关联)
    finally:
        worker_handler.set_downstream(None)
        worker_handler.reset_for_test()
