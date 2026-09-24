"""决策连接冷却的退避阶梯：连续失败翻倍、同一次故障的并发失败不加码、成功或显式重试复位；不访问真实供应商。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.errors import (
    ProviderConfigurationError,
    ProviderQuotaExhaustedError,
    ProviderTransientError,
)
from agent_py_agent.agent.conversation import decision_model_call as calls
from agent_py_agent.agent.conversation import decision_policy as policy
from agent_py_agent.agent.conversation import decision_service as service
from agent_py_agent.tests.test_decision_service import decide, prepared, successful  # noqa: F401

KEY = ("owner", "profile", "connection")


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(policy, "time", SimpleNamespace(monotonic=lambda: now[0]))
    with policy._LOCK:
        policy._FAILURES.clear()
    yield now
    with policy._LOCK:
        policy._FAILURES.clear()


def _fail(error: Exception | None = None) -> str:
    return policy.record_failure(KEY, "policy-1", error or ProviderTransientError("temporary"))


def _state(revision: str = "policy-1") -> tuple[str, float]:
    return policy.cooldown_state(KEY, revision)


def test_failures_after_each_expiry_double_the_cooldown_up_to_the_cap(clock):
    waits = []
    for _ in range(6):
        assert _fail() == "cooldown"
        status, remaining = _state()
        assert status == "cooldown"
        waits.append(remaining)
        clock[0] += remaining + 1
        assert _state() == ("", 0.0), "冷却过期后放行一次尝试"
    assert waits == [30.0, 60.0, 120.0, 240.0, 300.0, 300.0]


def test_failures_returning_inside_one_cooldown_count_as_one_outage(clock):
    _fail()
    clock[0] += 5
    _fail()
    assert _state() == ("cooldown", 25.0), "冷却期内返回的并发失败不改截止时刻"
    clock[0] += 26
    _fail()
    assert _state() == ("cooldown", 60.0), "过期后再失败只加一级"


def test_success_or_explicit_retry_restarts_the_ladder(clock):
    _fail()
    clock[0] += 31
    _fail()
    assert _state() == ("cooldown", 60.0)
    policy.record_success(KEY)
    assert _state() == ("", 0.0)
    _fail()
    assert _state() == ("cooldown", 30.0)
    clock[0] += 31
    _fail()
    assert policy.cooldown_state(KEY, "policy-1", retry=True) == ("", 0.0)
    _fail()
    assert _state() == ("cooldown", 30.0)


def test_quota_keeps_300_seconds_and_extends_an_active_cooldown_without_escalating(clock):
    _fail()
    clock[0] += 10
    _fail(ProviderQuotaExhaustedError("quota"))
    assert _state() == ("cooldown", 300.0)
    clock[0] += 301
    _fail()
    assert _state() == ("cooldown", 60.0), "额度冷却期内的失败不加级，过期后再失败才加一级"


def test_configuration_errors_wait_for_a_new_revision_and_start_no_ladder(clock):
    assert _fail(ProviderConfigurationError("credential")) == "configuration_required"
    clock[0] += 10_000
    assert _state() == ("configuration_required", 0.0), "配置错误不随时间解除"
    assert _state("policy-2") == ("", 0.0)
    _fail()
    assert _state("policy-2") == ("cooldown", 30.0)


def test_service_backs_off_a_persistently_failing_connection_until_it_answers(prepared, monkeypatch):  # noqa: F811
    host, params, _ = prepared
    offset = [0.0]
    real = policy.time.monotonic
    monkeypatch.setattr(policy, "time", SimpleNamespace(monotonic=lambda: real() + offset[0]))
    attempts = []

    def failing(*_args, **_kwargs):
        attempts.append(1)
        raise ProviderTransientError("temporary")

    monkeypatch.setattr(calls, "invoke_decision_model_call", failing)
    stage = service.begin_decision_stage(host, params, operation_id="batch")
    waits = []
    for _ in range(3):
        assert decide(host, params, stage).status == "error"
        blocked = decide(host, params, stage)
        assert (blocked.status, blocked.reason) == ("cooldown", "connection_backoff")
        waits.append(round(blocked.retry_after_seconds))
        offset[0] += blocked.retry_after_seconds + 1
    assert waits == [30, 60, 120] and len(attempts) == 3, "冷却期内不发请求，过期后每次再失败冷却翻倍"
    monkeypatch.setattr(calls, "invoke_decision_model_call", successful)
    assert decide(host, params, stage).status == "success"
    monkeypatch.setattr(calls, "invoke_decision_model_call", failing)
    decide(host, params, stage)
    assert round(decide(host, params, stage).retry_after_seconds) == 30, "连接返回过一次响应即复位阶梯"
