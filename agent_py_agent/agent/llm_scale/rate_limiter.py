"""每租户令牌桶限流(Tier 3):平滑突发、租户间公平——一个租户刷爆不拖垮其他租户。

令牌桶经典算法,自建(无依赖):桶容量 burst,每秒补 rps 个令牌;来一个请求扣 1(或按预估
token 扣权重),够则放行。注入时钟 → 确定性测试(不靠真实 sleep)。线程安全(每桶一把锁)。

跨实例一致限流(N 个 worker 副本共享同一租户配额)需把桶状态放 Redis——研究确认是"没人做"的
空白点;本类是单实例权威,多副本时每副本独立桶(配额按副本数均分)或后续换 Redis 后端。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass


def _now_ms() -> int:
    """单调时钟(毫秒):不受系统时间回拨影响,适合限流计时。"""
    return time.monotonic_ns() // 1_000_000


@dataclass
class _BucketState:
    tokens: float
    last_refill_ms: int


class TokenBucket:
    """单令牌桶:容量 capacity,每秒补 refill_per_sec 个。try_consume(n) 够则扣返回 True。"""

    def __init__(self, capacity: float, refill_per_sec: float, *, clock: Callable[[], int] | None = None) -> None:
        self._cap = float(capacity)
        self._rate = float(refill_per_sec)
        self._clock = clock or _now_ms
        self._state = _BucketState(tokens=float(capacity), last_refill_ms=self._clock())
        self._lock = threading.Lock()

    def try_consume(self, tokens: float = 1.0) -> bool:
        with self._lock:
            self._refill_locked()
            if self._state.tokens >= tokens:
                self._state.tokens -= tokens
                return True
            return False

    def available(self) -> float:
        with self._lock:
            self._refill_locked()
            return self._state.tokens

    def _refill_locked(self) -> None:
        now = self._clock()
        elapsed_ms = now - self._state.last_refill_ms
        if elapsed_ms <= 0:
            return  # 时钟未前进(或同毫秒内多次调用)
        gained = (elapsed_ms / 1000.0) * self._rate
        self._state.tokens = min(self._cap, self._state.tokens + gained)
        self._state.last_refill_ms = now


class TenantRateLimiter:
    """每租户一个独立令牌桶:租户隔离限流。首见租户惰性建桶(共享 rps/burst 配置)。"""

    def __init__(self, rps: float, burst: float, *, clock: Callable[[], int] | None = None) -> None:
        self._rps = float(rps)
        self._burst = float(burst)
        self._clock = clock
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def allow(self, tenant: str, tokens: float = 1.0) -> bool:
        """租户 tenant 此刻是否放行(扣 tokens 个令牌)。超限返回 False。"""
        return self._bucket_for(tenant).try_consume(tokens)

    def available(self, tenant: str) -> float:
        return self._bucket_for(tenant).available()

    def tenant_count(self) -> int:
        with self._lock:
            return len(self._buckets)

    def _bucket_for(self, tenant: str) -> TokenBucket:
        with self._lock:
            bucket = self._buckets.get(tenant)
            if bucket is None:
                bucket = TokenBucket(self._burst, self._rps, clock=self._clock)
                self._buckets[tenant] = bucket
            return bucket
