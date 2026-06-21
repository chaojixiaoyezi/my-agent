"""审计 #24(env 部分)修复真测:入口 env 整数/浮点安全解析,误配非法值用默认而非崩进程。"""
from __future__ import annotations

from agent_py_agent.agent.asgi_entry import env_float, env_int


def test_env_int_safe(monkeypatch) -> None:
    monkeypatch.setenv("X_T", "abc")
    assert env_int("X_T", 7) == 7  # 非法 → 默认(不抛 ValueError)
    monkeypatch.setenv("X_T", "42")
    assert env_int("X_T", 7) == 42
    monkeypatch.delenv("X_T", raising=False)
    assert env_int("X_T", 7) == 7  # 缺失 → 默认


def test_env_float_safe(monkeypatch) -> None:
    monkeypatch.setenv("Y_T", "  ")
    assert env_float("Y_T", 1.5) == 1.5  # 空格非法 → 默认
    monkeypatch.setenv("Y_T", "2.5")
    assert env_float("Y_T", 1.5) == 2.5


def test_build_admission_survives_bad_env(monkeypatch) -> None:
    monkeypatch.setenv("LLM_MAX_INFLIGHT", "not-a-number")
    monkeypatch.setenv("LLM_TENANT_RPS", "garbage")
    from agent_py_agent.agent.worker_handler import _build_admission

    assert _build_admission() is not None  # 误配非法 env 不崩,用默认
