"""用SQLite固定协议码验证重试/取消叶，兼容Python3.10；不创建Agent或审批权限。"""

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.concurrency import interrupt
from agent_py_agent.agent.gateway_parts import private_approval_control as private
from agent_py_agent.agent.gateway_parts import private_interaction_service as interactions


def test_real_interrupt_wait_stops_before_second_validation(monkeypatch):
    calls = []
    error = sqlite3.OperationalError("controlled busy")
    error.sqlite_errorcode = 5  # SQLite固定协议主码BUSY。

    def read(*args, **kwargs):
        calls.append(True)
        interrupt.set_interrupt(True)
        raise error

    monkeypatch.setattr(interactions, "validated_private_interaction_from_config", read)
    agent = SimpleNamespace(home_paths=SimpleNamespace(root=Path(".")), config=None)
    receipt = SimpleNamespace(channel="synthetic", user_id="synthetic")
    try:
        with pytest.raises(InterruptedError):
            private._validated_approval_input(agent, receipt, {}, retry_read_busy=True)
        assert len(calls) == 1
    finally:
        interrupt.set_interrupt(False)


def test_extended_busy_primary_code_revalidates_same_inputs(monkeypatch):
    calls = []
    waits = []
    body = {}
    config = object()
    error = sqlite3.OperationalError("controlled extended busy")
    error.sqlite_errorcode = 261  # SQLite固定扩展码BUSY_RECOVERY，其低8位仍是BUSY=5。

    def read(root, cfg, value, **kwargs):
        assert root == Path(".") and cfg is config and value is body
        assert kwargs == {"channel": "synthetic", "actor_id": "synthetic"}
        calls.append(True)
        if len(calls) < 3:
            raise error
        return {"same": "original"}, None

    monkeypatch.setattr(interactions, "validated_private_interaction_from_config", read)
    monkeypatch.setattr(interrupt, "wait_interruptibly", waits.append)
    agent = SimpleNamespace(home_paths=SimpleNamespace(root=Path(".")), config=config)
    receipt = SimpleNamespace(channel="synthetic", user_id="synthetic")
    assert private._validated_approval_input(agent, receipt, body, retry_read_busy=True) == (
        {"same": "original"},
        None,
    )
    assert len(calls) == 3 and waits == [0.005, 0.010]
