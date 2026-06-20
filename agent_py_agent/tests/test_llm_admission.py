"""Tier 3 并发限制 + 准入控制测试:信号量满则超时抛错(真线程)、三关合一裁决。"""

from __future__ import annotations

import threading

import pytest

from agent_py_agent.agent.llm_scale.admission import LLMAdmission
from agent_py_agent.agent.llm_scale.concurrency import ConcurrencyLimiter, ConcurrencyTimeout
from agent_py_agent.agent.llm_scale.rate_limiter import TenantRateLimiter
from agent_py_agent.agent.llm_scale.token_budget import TokenBudget


class _Clock:
    def __init__(self) -> None:
        self.ms = 0

    def __call__(self) -> int:
        return self.ms


def test_concurrency_limiter_blocks_and_times_out_when_full() -> None:
    limiter = ConcurrencyLimiter(max_in_flight=1)
    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with limiter.slot():
            held.set()
            release.wait(2.0)

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert held.wait(1.0)  # 第一个占住唯一的槽
    assert limiter.in_flight() == 1
    with pytest.raises(ConcurrencyTimeout):  # 第二个拿不到 → 超时抛
        with limiter.slot(timeout=0.1):
            pass
    release.set()
    t.join(2.0)
    assert limiter.in_flight() == 0  # 还槽后归零


def test_concurrency_rejects_bad_max() -> None:
    with pytest.raises(ValueError):
        ConcurrencyLimiter(max_in_flight=0)


def _admission(rps: float, burst: float, limit: int) -> LLMAdmission:
    clock = _Clock()
    return LLMAdmission(
        TenantRateLimiter(rps=rps, burst=burst, clock=clock),
        TokenBudget(limit_tokens=limit, window_seconds=60, clock=clock),
        ConcurrencyLimiter(max_in_flight=2),
    )


def test_precheck_rate_limited() -> None:
    adm = _admission(rps=0, burst=1, limit=10_000)
    assert adm.precheck("t", estimated_tokens=10).admitted is True
    verdict = adm.precheck("t", estimated_tokens=10)  # 桶空(burst=1、不补)
    assert verdict.admitted is False
    assert verdict.reason == "rate_limited"


def test_precheck_budget_exceeded_after_rate_ok() -> None:
    adm = _admission(rps=0, burst=100, limit=1000)  # 限流够松,卡在预算
    assert adm.precheck("t", estimated_tokens=800).admitted is True
    verdict = adm.precheck("t", estimated_tokens=800)  # 会到 1600 > 1000
    assert verdict.admitted is False
    assert verdict.reason == "budget_exceeded"


def test_happy_path_precheck_slot_settle() -> None:
    adm = _admission(rps=0, burst=10, limit=10_000)
    verdict = adm.precheck("t", estimated_tokens=2000)
    assert verdict.admitted is True
    assert verdict.remaining_budget == 8000  # 预扣 2000
    with adm.slot(timeout=1.0):  # 占并发槽真正调用
        pass
    adm.settle("t", estimated=2000, actual=2500)  # 真实多用了 500
    # 二次 precheck 反映真实消耗:剩 10000-2500-? ;先看预算余额
    assert adm._budget.remaining("t") == 7500
