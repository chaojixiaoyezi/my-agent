from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.orchestration.background.dispatch import (
    _use_inprocess_autostart,
)


def _agent(model_backend="minimax", provider=""):
    return SimpleNamespace(
        config=SimpleNamespace(model_backend=model_backend, my_agent_owner_provider=provider)
    )


def test_echo_backend_uses_inprocess():
    assert _use_inprocess_autostart(_agent(model_backend="echo")) is True


def test_scoped_feishu_owner_uses_inprocess():
    """飞书 scoped owner 走进程内派工(避开子进程 owner 丢失,子代理才被驱动)。"""
    assert _use_inprocess_autostart(_agent(provider="feishu")) is True


def test_base_local_owner_still_uses_subprocess():
    """base owner(local)行为不变,仍走 durable 子进程。"""
    assert _use_inprocess_autostart(_agent(provider="local")) is False


def test_no_provider_still_uses_subprocess():
    assert _use_inprocess_autostart(_agent(provider="")) is False
