"""审计 #19(成本闭环,medium/稳定)真测:USD 预算熔断 + 成本台账累加 + 准入/worker 端到端。

成本闭环 = 单价(model_pricing)→ 算成本(cost_usd)→ 累加(CostLedger)+ 预算熔断(UsdBudget)。
默认不限额 = 不改现有行为;配了 USD 上限才在 precheck 熔断。真造预算窗口/注入时钟/真跑 worker
handle 断言:超 USD 预算挡下不调下游、真实成本累加到 run/tenant。学 长期助手 持久化 cost、SDK total_cost_usd。
"""

from __future__ import annotations

import pytest

from agent_py_agent.agent.llm_scale import (
    ConcurrencyLimiter,
    CostLedger,
    LLMAdmission,
    TenantRateLimiter,
    TokenBudget,
    UsdBudget,
)


def _clock0():
    return 0


# ---------- UsdBudget ----------

def test_usd_budget_enforced_when_configured() -> None:
    clock = [0]
    budget = UsdBudget(1.0, 3600, clock=lambda: clock[0])
    assert budget.try_charge("t", 0.6)  # 0.6 ≤ 1.0
    assert budget.try_charge("t", 0.3)  # 0.9
    assert not budget.try_charge("t", 0.2)  # 1.1 > 1.0 → 熔断
    assert budget.spent("t") == pytest.approx(0.9)


def test_usd_budget_unlimited_is_default_no_op() -> None:
    budget = UsdBudget(0, 3600)  # ≤0 = 不限额
    assert budget.unlimited
    assert budget.try_charge("t", 1_000_000.0)  # 永远放行(不改现有行为)
    assert budget.remaining("t") == float("inf")


def test_usd_budget_window_resets() -> None:
    clock = [0]
    budget = UsdBudget(1.0, 1.0, clock=lambda: clock[0])  # 1s 窗口
    assert budget.try_charge("t", 0.9)
    assert not budget.try_charge("t", 0.2)
    clock[0] = 2000  # 2s 后新窗口
    assert budget.try_charge("t", 0.9)  # 重置,可再扣


def test_usd_budget_settle_corrects_estimate() -> None:
    clock = [0]
    budget = UsdBudget(10.0, 3600, clock=lambda: clock[0])
    budget.try_charge("t", 2.0)  # 预扣 2.0
    budget.settle("t", 2.0, 3.5)  # 真实 3.5 → 校正 +1.5
    assert budget.spent("t") == pytest.approx(3.5)


# ---------- CostLedger ----------

def test_cost_ledger_accumulates_tenant_and_run() -> None:
    ledger = CostLedger()
    ledger.record(0.5, tenant="acme", run_id="r1")
    ledger.record(0.3, tenant="acme", run_id="r1")
    ledger.record(0.2, tenant="other", run_id="r2")
    assert ledger.tenant_cost("acme") == pytest.approx(0.8)
    assert ledger.run_cost("r1") == pytest.approx(0.8)
    assert round(ledger.total_cost(), 6) == 1.0


def test_cost_ledger_ignores_nonpositive() -> None:
    ledger = CostLedger()
    ledger.record(-1.0, tenant="t")
    ledger.record(0.0, tenant="t")
    assert ledger.tenant_cost("t") == 0.0


def test_cost_ledger_run_dimension_bounded() -> None:
    ledger = CostLedger(max_runs=3)
    for i in range(5):
        ledger.record(1.0, run_id=f"r{i}")
    assert ledger.run_cost("r0") == 0.0 and ledger.run_cost("r1") == 0.0  # FIFO 逐出最旧
    assert ledger.run_cost("r4") == 1.0  # 最近的在


# ---------- LLMAdmission USD 闸 ----------

def _admission(*, usd_limit: float = 0.0) -> LLMAdmission:
    return LLMAdmission(
        TenantRateLimiter(1000, 1000, clock=_clock0),
        TokenBudget(10_000_000, 3600, clock=_clock0),
        ConcurrencyLimiter(8),
        usd_budget=UsdBudget(usd_limit, 3600, clock=_clock0),
    )


def test_admission_rejects_over_usd_budget() -> None:
    adm = _admission(usd_limit=1.0)
    assert adm.precheck("t", estimated_tokens=10, estimated_cost_usd=0.6).admitted
    verdict = adm.precheck("t", estimated_tokens=10, estimated_cost_usd=0.6)  # 累计 1.2 > 1.0
    assert not verdict.admitted and verdict.reason == "usd_budget_exceeded"


def test_admission_without_usd_budget_unchanged() -> None:
    adm = LLMAdmission(TenantRateLimiter(1000, 1000), TokenBudget(10_000_000, 3600), ConcurrencyLimiter(8))
    # 未配 usd_budget:巨额 est_cost 也放行(现有行为完全不变)
    assert adm.precheck("t", estimated_tokens=10, estimated_cost_usd=999999.0).admitted


# ---------- worker_handler 端到端 ----------

def test_worker_handler_usd_budget_blocks_expensive_call() -> None:
    from agent_py_agent.agent import worker_handler

    worker_handler.reset_for_test(_admission(usd_limit=0.01))
    called: list[int] = []
    worker_handler.set_downstream(lambda p, c: (called.append(1), 1000)[1])
    try:
        # opus 15/Mtok in:1000 token 预估成本 0.015 > 0.01 上限 → 挡下
        worker_handler.handle({"tenant": "acme", "model": "claude-opus-4-8", "estimated_tokens": 1000})
        assert called == []  # 超 USD 预算 → 不调下游(成本闸生效)
        assert ("acme", "usd_budget_exceeded") in worker_handler._REJECTED
    finally:
        worker_handler.set_downstream(None)
        worker_handler.reset_for_test()


def test_worker_handler_accumulates_real_cost() -> None:
    from agent_py_agent.agent import worker_handler

    worker_handler.reset_for_test(_admission(usd_limit=0))  # 不限额,只观测成本
    worker_handler.set_downstream(lambda p, c: 2000)  # 真实 2000 token
    try:
        worker_handler.handle(
            {"tenant": "acme", "model": "claude-sonnet-4-6", "estimated_tokens": 1000, "run_id": "run-1"}
        )
        # sonnet 3/Mtok in:2000 token 真实成本 0.006,累加到 tenant + run
        assert worker_handler._COST_LEDGER.tenant_cost("acme") == pytest.approx(2000 / 1_000_000 * 3)
        assert worker_handler._COST_LEDGER.run_cost("run-1") > 0
    finally:
        worker_handler.set_downstream(None)
        worker_handler.reset_for_test()
