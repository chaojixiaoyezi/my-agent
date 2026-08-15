"""Tier 3 限流 + 预算测试:令牌桶扣/补、租户隔离、预算封顶/窗口重置/真实token校正。

注入假时钟 → 确定性(不靠真实 sleep)。
"""

from __future__ import annotations

from agent_py_agent.agent.llm_scale.rate_limiter import TenantRateLimiter, TokenBucket
from agent_py_agent.agent.llm_scale.token_budget import TokenBudget


class _Clock:
    """可手动推进的假时钟(返回毫秒)。"""

    def __init__(self) -> None:
        self.ms = 0

    def __call__(self) -> int:
        return self.ms

    def advance(self, ms: int) -> None:
        self.ms += ms


def test_token_bucket_consumes_until_empty() -> None:
    clock = _Clock()
    bucket = TokenBucket(capacity=3, refill_per_sec=0, clock=clock)  # 不补
    assert bucket.try_consume() is True
    assert bucket.try_consume() is True
    assert bucket.try_consume() is True
    assert bucket.try_consume() is False  # 桶空,第 4 个被挡


def test_token_bucket_refills_over_time() -> None:
    clock = _Clock()
    bucket = TokenBucket(capacity=2, refill_per_sec=10, clock=clock)  # 每秒补 10
    assert bucket.try_consume(2) is True
    assert bucket.try_consume(1) is False  # 空了
    clock.advance(200)  # 0.2s → 补 2 个
    assert bucket.try_consume(1) is True
    assert bucket.available() >= 0.9  # 还剩 ~1


def test_tenant_isolation_one_tenant_cannot_starve_another() -> None:
    clock = _Clock()
    limiter = TenantRateLimiter(rps=0, burst=2, clock=clock)  # 每租户桶容量 2、不补
    assert limiter.allow("tenant-A") is True
    assert limiter.allow("tenant-A") is True
    assert limiter.allow("tenant-A") is False  # A 刷爆
    assert limiter.allow("tenant-B") is True  # B 不受影响
    assert limiter.tenant_count() == 2


def test_token_budget_caps_cumulative_spend() -> None:
    budget = TokenBudget(limit_tokens=1000, window_seconds=60, clock=_Clock())
    assert budget.try_charge("t", 600) is True
    assert budget.try_charge("t", 600) is False  # 会到 1200 > 1000,挡下、不扣
    assert budget.try_charge("t", 400) is True  # 600+400=1000 正好
    assert budget.remaining("t") == 0


def test_budget_window_rolls_and_resets() -> None:
    clock = _Clock()
    budget = TokenBudget(limit_tokens=1000, window_seconds=60, clock=clock)
    assert budget.try_charge("t", 1000) is True
    assert budget.try_charge("t", 1) is False  # 本窗口满
    clock.advance(60_000)  # 跨到新窗口
    assert budget.try_charge("t", 1000) is True  # 重置后又满额可用
    assert budget.spent("t") == 1000


def test_budget_settle_corrects_estimate() -> None:
    budget = TokenBudget(limit_tokens=10_000, window_seconds=60, clock=_Clock())
    assert budget.try_charge("t", 500) is True  # 预扣 500
    budget.settle("t", estimated=500, actual=700)  # 真实 700
    assert budget.spent("t") == 700
    assert budget.remaining("t") == 9_300
