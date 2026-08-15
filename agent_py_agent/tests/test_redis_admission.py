from __future__ import annotations

from agent_py_agent.agent.llm_scale.redis_admission import (
    RedisConcurrencyLimiter,
    RedisTenantRateLimiter,
    RedisTokenBudget,
)


class ScriptedRedis:
    def __init__(self, replies: list[object]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, int, tuple[object, ...]]] = []

    def eval(self, script: str, numkeys: int, *args: object):
        self.calls.append((script, numkeys, args))
        return self.replies.pop(0)

    def ping(self) -> bool:
        return True


def test_rate_limit_uses_hashed_tenant_key_and_atomic_script() -> None:
    client = ScriptedRedis([[1, b"3.5"]])
    limiter = RedisTenantRateLimiter(client, rps=5, burst=10)
    assert limiter.allow("secret-customer") is True
    key = str(client.calls[0][2][0])
    assert key.startswith("my-agent:rate:")
    assert "secret-customer" not in key


def test_token_budget_charge_remaining_and_settle_share_redis_state() -> None:
    client = ScriptedRedis([[1, b"800"], b"800", b"250"])
    budget = RedisTokenBudget(client, 1000, 60)
    assert budget.try_charge("acme", 200) is True
    assert budget.remaining("acme") == 800
    budget.settle("acme", 200, 250)
    assert all(call[1] == 1 for call in client.calls)


def test_concurrency_slot_releases_token() -> None:
    # acquire=1; renewer 在极短用例结束前不会跑; release=1
    client = ScriptedRedis([1, 1])
    limiter = RedisConcurrencyLimiter(client, 2, lease_seconds=3)
    with limiter.slot(timeout=0.1):
        pass
    assert len(client.calls) == 2
    acquire_token = client.calls[0][2][-1]
    release_token = client.calls[1][2][-1]
    assert acquire_token == release_token
