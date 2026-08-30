from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.orchestration.background import dispatch as dispatch_module
from agent_py_agent.agent.agent_core.orchestration.background.dispatch import (
    _start_inprocess_dispatch,
    _use_inprocess_autostart,
)


def _agent(model_backend="minimax", provider="", owner_kind="main", owner_id="main"):
    return SimpleNamespace(
        config=SimpleNamespace(
            model_backend=model_backend,
            my_agent_owner_provider=provider,
            my_agent_owner_kind=owner_kind,
            my_agent_owner_id=owner_id,
        )
    )


def test_echo_backend_uses_inprocess():
    assert _use_inprocess_autostart(_agent(model_backend="echo")) is True


def test_scoped_feishu_owner_uses_inprocess():
    """飞书 scoped owner 走进程内派工(避开子进程 owner 丢失,子代理才被驱动)。"""
    assert _use_inprocess_autostart(_agent(provider="feishu")) is True


def test_base_local_owner_still_uses_subprocess():
    """base owner(local)行为不变,仍走 durable 子进程。"""
    assert _use_inprocess_autostart(_agent(provider="local")) is False


def test_scoped_local_user_uses_inprocess():
    """单 Gateway 的 local/user TUI 必须保留当前 owner，不能用 base config 起错子进程。"""
    assert (
        _use_inprocess_autostart(
            _agent(provider="local", owner_kind="user", owner_id="alice")
        )
        is True
    )


def test_scoped_local_group_uses_inprocess():
    """本机结构化群组与本机用户遵守同一 owner 传递合同。"""
    assert (
        _use_inprocess_autostart(
            _agent(provider="local", owner_kind="group", owner_id="team-a")
        )
        is True
    )


def test_no_provider_still_uses_subprocess():
    assert _use_inprocess_autostart(_agent(provider="")) is False


def test_scoped_owner_inprocess_dispatch_is_daemon_recovery_work(monkeypatch):
    created: dict[str, object] = {}

    class FakeThread:
        def __init__(self, **kwargs):
            created.update(kwargs)
            self.name = str(kwargs.get("name") or "")

        def start(self) -> None:
            created["started"] = True

    monkeypatch.setattr(dispatch_module.threading, "Thread", FakeThread)
    monkeypatch.setattr(dispatch_module, "_remember_background_dispatch", lambda *_args: None)
    request = SimpleNamespace(launch_id="launch-1", run_ids=["run-1"])

    result = _start_inprocess_dispatch(SimpleNamespace(), request)

    assert created["daemon"] is True
    assert created["started"] is True
    assert result["background_backend"] == "thread"
