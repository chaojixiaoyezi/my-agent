"""Redis 跨副本 LLM 准入状态：原子限流、预算与带租约的全局并发槽。"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from agent_py_agent.agent.llm_scale.admission import LLMAdmission
from agent_py_agent.agent.llm_scale.concurrency import ConcurrencyTimeout

RedisClient = Any  # redis-py 的 eval 是动态 Lua 参数面；调用点仍逐项显式传参，不暴露服务 varargs 接口。


@dataclass(frozen=True)
class RedisAdmissionConfig:
    rps: float
    burst: float
    token_limit: int
    window_seconds: float
    max_in_flight: int
    usd_limit: float = 0
    lease_seconds: float = 900


_RATE_SCRIPT = """
local now = redis.call('TIME')
local now_ms = (tonumber(now[1]) * 1000) + math.floor(tonumber(now[2]) / 1000)
local cap = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local values = redis.call('HMGET', KEYS[1], 'tokens', 'updated_ms')
local tokens = tonumber(values[1]) or cap
local updated = tonumber(values[2]) or now_ms
tokens = math.min(cap, tokens + math.max(0, now_ms - updated) * rate / 1000)
local allowed = 0
if tokens >= cost then tokens = tokens - cost; allowed = 1 end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'updated_ms', now_ms)
local ttl = math.max(1000, math.ceil((cap / math.max(rate, 0.001)) * 2000))
redis.call('PEXPIRE', KEYS[1], ttl)
return {allowed, tostring(tokens)}
"""

_BUDGET_CHARGE_SCRIPT = """
local now = redis.call('TIME')
local now_ms = (tonumber(now[1]) * 1000) + math.floor(tonumber(now[2]) / 1000)
local limit = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local charge = tonumber(ARGV[3])
local values = redis.call('HMGET', KEYS[1], 'spent', 'window_start_ms')
local spent = tonumber(values[1]) or 0
local started = tonumber(values[2]) or now_ms
if now_ms - started >= window_ms then spent = 0; started = now_ms end
local allowed = 0
if limit <= 0 or spent + charge <= limit then spent = spent + charge; allowed = 1 end
redis.call('HSET', KEYS[1], 'spent', spent, 'window_start_ms', started)
redis.call('PEXPIRE', KEYS[1], math.max(1000, window_ms * 2))
local remaining = limit <= 0 and -1 or math.max(0, limit - spent)
return {allowed, tostring(remaining)}
"""

_BUDGET_SETTLE_SCRIPT = """
local now = redis.call('TIME')
local now_ms = (tonumber(now[1]) * 1000) + math.floor(tonumber(now[2]) / 1000)
local window_ms = tonumber(ARGV[1])
local delta = tonumber(ARGV[2])
local values = redis.call('HMGET', KEYS[1], 'spent', 'window_start_ms')
local spent = tonumber(values[1]) or 0
local started = tonumber(values[2]) or now_ms
if now_ms - started >= window_ms then spent = 0; started = now_ms end
spent = math.max(0, spent + delta)
redis.call('HSET', KEYS[1], 'spent', spent, 'window_start_ms', started)
redis.call('PEXPIRE', KEYS[1], math.max(1000, window_ms * 2))
return tostring(spent)
"""

_BUDGET_READ_SCRIPT = """
local now = redis.call('TIME')
local now_ms = (tonumber(now[1]) * 1000) + math.floor(tonumber(now[2]) / 1000)
local limit = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local values = redis.call('HMGET', KEYS[1], 'spent', 'window_start_ms')
local spent = tonumber(values[1]) or 0
local started = tonumber(values[2]) or now_ms
if now_ms - started >= window_ms then spent = 0 end
return tostring(limit <= 0 and -1 or math.max(0, limit - spent))
"""

_ACQUIRE_SCRIPT = """
local now = redis.call('TIME')
local now_ms = (tonumber(now[1]) * 1000) + math.floor(tonumber(now[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now_ms)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[1]) then return 0 end
redis.call('ZADD', KEYS[1], now_ms + tonumber(ARGV[2]), ARGV[3])
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[2]) * 2)
return 1
"""

_RENEW_SCRIPT = """
local now = redis.call('TIME')
local now_ms = (tonumber(now[1]) * 1000) + math.floor(tonumber(now[2]) / 1000)
if redis.call('ZSCORE', KEYS[1], ARGV[2]) == false then return 0 end
redis.call('ZADD', KEYS[1], now_ms + tonumber(ARGV[1]), ARGV[2])
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[1]) * 2)
return 1
"""

_RELEASE_SCRIPT = "return redis.call('ZREM', KEYS[1], ARGV[1])"


def _tenant_key(prefix: str, tenant: str) -> str:
    digest = hashlib.sha256(tenant.encode("utf-8", "replace")).hexdigest()
    return f"my-agent:{prefix}:{digest}"


def _number(value: Any) -> float:
    if isinstance(value, bytes):
        value = value.decode("ascii", "replace")
    return float(value)


class RedisTenantRateLimiter:
    def __init__(self, client: RedisClient, rps: float, burst: float) -> None:
        self._client = client
        self._rps = float(rps)
        self._burst = float(burst)

    def allow(self, tenant: str, tokens: float = 1.0) -> bool:
        result = self._client.eval(
            _RATE_SCRIPT,
            1,
            _tenant_key("rate", tenant),
            self._burst,
            self._rps,
            float(tokens),
        )
        return bool(int(result[0]))

    def available(self, tenant: str) -> float:
        result = self._client.eval(
            _RATE_SCRIPT,
            1,
            _tenant_key("rate", tenant),
            self._burst,
            self._rps,
            0,
        )
        return _number(result[1])


class RedisBudget:
    def __init__(self, client: RedisClient, limit: float, window_seconds: float, *, prefix: str) -> None:
        self._client = client
        self._limit = float(limit)
        self._window_ms = max(1000, int(window_seconds * 1000))
        self._prefix = prefix

    @property
    def unlimited(self) -> bool:
        return self._limit <= 0

    def try_charge(self, tenant: str, amount: float) -> bool:
        result = self._client.eval(
            _BUDGET_CHARGE_SCRIPT,
            1,
            _tenant_key(self._prefix, tenant),
            self._limit,
            self._window_ms,
            float(amount),
        )
        return bool(int(result[0]))

    def settle(self, tenant: str, estimated: float, actual: float) -> None:
        if self.unlimited:
            return
        self._client.eval(
            _BUDGET_SETTLE_SCRIPT,
            1,
            _tenant_key(self._prefix, tenant),
            self._window_ms,
            float(actual) - float(estimated),
        )

    def remaining(self, tenant: str) -> float:
        result = self._client.eval(
            _BUDGET_READ_SCRIPT,
            1,
            _tenant_key(self._prefix, tenant),
            self._limit,
            self._window_ms,
        )
        remaining = _number(result)
        return float("inf") if remaining < 0 else remaining

    def spent(self, tenant: str) -> float:
        remaining = self.remaining(tenant)
        return 0.0 if remaining == float("inf") else max(0.0, self._limit - remaining)


class RedisTokenBudget(RedisBudget):
    def __init__(self, client: RedisClient, limit_tokens: int, window_seconds: float) -> None:
        super().__init__(client, limit_tokens, window_seconds, prefix="token-budget")

    def remaining(self, tenant: str) -> int:
        return int(super().remaining(tenant))

    def spent(self, tenant: str) -> int:
        return int(super().spent(tenant))


class RedisUsdBudget(RedisBudget):
    def __init__(self, client: RedisClient, limit_usd: float, window_seconds: float) -> None:
        super().__init__(client, limit_usd, window_seconds, prefix="usd-budget")


class RedisConcurrencyLimiter:
    def __init__(
        self,
        client: RedisClient,
        max_in_flight: int,
        *,
        lease_seconds: float = 900,
        poll_seconds: float = 0.05,
    ) -> None:
        if max_in_flight < 1:
            raise ValueError("max_in_flight 必须 ≥ 1")
        self._client = client
        self._max = int(max_in_flight)
        self._lease_ms = max(3000, int(lease_seconds * 1000))
        self._poll = max(0.01, float(poll_seconds))
        self._key = "my-agent:llm-concurrency"

    @contextmanager
    def slot(self, timeout: float | None = None) -> Iterator[None]:
        token = uuid4().hex
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while not self._acquire(token):
            if deadline is not None and time.monotonic() >= deadline:
                raise ConcurrencyTimeout(f"等不到 Redis 全局并发槽(上限 {self._max})")
            time.sleep(self._poll)
        stop = threading.Event()
        renewer = threading.Thread(target=self._renew_loop, args=(token, stop), daemon=True)
        renewer.start()
        try:
            yield
        finally:
            stop.set()
            renewer.join(timeout=min(1.0, self._lease_ms / 3000))
            self._client.eval(_RELEASE_SCRIPT, 1, self._key, token)

    def _acquire(self, token: str) -> bool:
        return bool(
            int(self._client.eval(_ACQUIRE_SCRIPT, 1, self._key, self._max, self._lease_ms, token))
        )

    def _renew_loop(self, token: str, stop: threading.Event) -> None:
        interval = max(1.0, self._lease_ms / 3000)
        while not stop.wait(interval):
            if not self._renew_once(token):
                return

    def _renew_once(self, token: str) -> bool:
        try:
            alive = self._client.eval(_RENEW_SCRIPT, 1, self._key, self._lease_ms, token)
            return bool(int(alive))
        except Exception:
            # Redis 短抖动时租约仍在；下一拍重试。持续故障最终由租约自动释放，避免死槽。
            return True


def redis_client_from_url(url: str) -> RedisClient:
    try:
        import redis
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径由 scale 镜像验
        raise RuntimeError("scale Redis 共享态需 redis 包：pip install 'my-agent[scale]'") from exc
    client = redis.Redis.from_url(url, socket_connect_timeout=3, socket_timeout=3)
    client.ping()
    return client


def build_redis_admission(
    client: RedisClient,
    config: RedisAdmissionConfig,
) -> LLMAdmission:
    return LLMAdmission(
        RedisTenantRateLimiter(client, config.rps, config.burst),  # type: ignore[arg-type]
        RedisTokenBudget(client, config.token_limit, config.window_seconds),  # type: ignore[arg-type]
        RedisConcurrencyLimiter(
            client,
            config.max_in_flight,
            lease_seconds=config.lease_seconds,
        ),  # type: ignore[arg-type]
        usd_budget=RedisUsdBudget(client, config.usd_limit, config.window_seconds),  # type: ignore[arg-type]
    )


__all__ = [
    "RedisAdmissionConfig",
    "RedisConcurrencyLimiter",
    "RedisTenantRateLimiter",
    "RedisTokenBudget",
    "RedisUsdBudget",
    "build_redis_admission",
    "redis_client_from_url",
]
