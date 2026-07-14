"""Phase 2 真测:网关按请求 owner 跑作用域 agent(多用户飞书 per-用户隔离)。

显式关闭时走基础 agent；默认开启后，飞书用户 A/B 各跑在自己 owner 作用域的 agent 上
(home/记忆/数据隔离)。远程身份缺失或 owner 建立失败 fail-closed，不回退共享 agent。真建 SimpleAgent。
"""

from __future__ import annotations

import pytest

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.request_worker import (
    OwnerScopeUnavailableError,
    _owner_from_request,
    _resolve_request_agent,
)
from agent_py_agent.agent.settings.config import AgentConfig


def _agent(tmp_path, *, scoping: bool) -> SimpleAgent:
    config = AgentConfig(
        model_backend="echo", my_agent_home=str(tmp_path / "home"), gateway_per_user_owner_scoping=scoping
    )
    return SimpleAgent(config, tmp_path)


def _req(user_id: str, channel: str) -> dict:
    return {"user_id": user_id, "metadata": {"user_id": user_id, "channel": channel}}


def test_scoping_off_returns_base_agent(tmp_path) -> None:
    agent = _agent(tmp_path, scoping=False)
    assert _resolve_request_agent(agent, _req("u1", "feishu")) is agent  # 默认关 → 基础 agent(现状不变)


def test_scoping_on_returns_scoped_agent(tmp_path) -> None:
    agent = _agent(tmp_path, scoping=True)
    scoped = _resolve_request_agent(agent, _req("alice", "feishu"))
    assert scoped is not agent  # 跑在该用户作用域 agent
    assert "alice" in scoped.home_paths.owner_id and "feishu" in scoped.home_paths.owner_id


def test_two_feishu_users_isolated_at_gateway(tmp_path) -> None:
    agent = _agent(tmp_path, scoping=True)
    a = _resolve_request_agent(agent, _req("alice", "feishu"))
    b = _resolve_request_agent(agent, _req("bob", "feishu"))
    assert a is not b
    assert str(a.local_store.db_path) != str(b.local_store.db_path)  # 数据库隔离 = 不串户
    # 同一用户再来 → 命中同一作用域 agent
    assert _resolve_request_agent(agent, _req("alice", "feishu")) is a


def test_remote_anonymous_fails_closed_but_local_request_uses_base(tmp_path) -> None:
    agent = _agent(tmp_path, scoping=True)
    with pytest.raises(OwnerScopeUnavailableError):
        _resolve_request_agent(agent, _req("anonymous", "feishu"))
    assert _resolve_request_agent(agent, {"user_id": "u1", "metadata": {}}) is agent  # 无 channel 回退


def test_owner_pool_failure_does_not_fall_back_to_shared_agent(tmp_path, monkeypatch) -> None:
    agent = _agent(tmp_path, scoping=True)

    class BrokenPool:
        def get(self, _owner):
            raise OSError("owner storage unavailable")

    monkeypatch.setattr(
        "agent_py_agent.agent.gateway_parts.request_worker._owner_pool", lambda _agent: BrokenPool()
    )
    with pytest.raises(OwnerScopeUnavailableError):
        _resolve_request_agent(agent, _req("alice", "feishu"))


def test_owner_from_request_resolves_and_rejects(tmp_path) -> None:
    agent = _agent(tmp_path, scoping=True)
    owner = _owner_from_request(agent, _req("alice", "feishu"))
    assert owner is not None and "alice" in owner.owner_id
    assert _owner_from_request(agent, {"user_id": "anonymous", "metadata": {"channel": "feishu"}}) is None
    assert _owner_from_request(agent, {"user_id": "u1", "metadata": {}}) is None  # 无 channel


def test_owner_from_group_request_uses_chat_id_not_sender(tmp_path) -> None:
    agent = _agent(tmp_path, scoping=True)
    owner = _owner_from_request(
        agent,
        {
            "user_id": "sender-a",
            "metadata": {
                "user_id": "sender-a",
                "channel": "feishu",
                "channel_chat_type": "group",
                "channel_chat_id": "oc_group_1",
            },
        },
    )

    assert owner is not None
    assert owner.owner_kind == "group"
    assert "oc_group_1" in owner.owner_id


def test_pool_is_lazily_cached_on_agent(tmp_path) -> None:
    agent = _agent(tmp_path, scoping=True)
    _resolve_request_agent(agent, _req("alice", "feishu"))
    pool = agent._owner_pool
    _resolve_request_agent(agent, _req("bob", "feishu"))
    assert agent._owner_pool is pool  # 懒建一次,复用同一个池
