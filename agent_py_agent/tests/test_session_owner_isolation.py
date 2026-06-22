"""多租户:会话 store 是否真按 owner 物理隔离(反查 Phase-2 owner 池有没有 owner 化 session_workspace)。

session_workspace 在 runtime_paths 走 owner 作用域(_owner_memory_and_session_paths → owner_sessions_dir)。
既有 session 测试覆盖了 user_id 过滤层(test_multiple_users_isolation)+ admin 访问控制,但没覆盖
owner 池层的物理隔离。这条来锁死:两 owner 的会话 store 不同、A 的会话 B 完全看不到。
"""

from __future__ import annotations

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.request_worker import _resolve_request_agent
from agent_py_agent.agent.session.manager import SessionManager
from agent_py_agent.agent.settings.config import AgentConfig


def _gateway_agent(tmp_path, *, scoping: bool) -> SimpleAgent:
    config = AgentConfig(
        model_backend="echo",
        my_agent_home=str(tmp_path / "home"),
        gateway_per_user_owner_scoping=scoping,
    )
    return SimpleAgent(config, tmp_path)


def _req(user_id: str) -> dict:
    return {"user_id": user_id, "metadata": {"user_id": user_id, "channel": "feishu"}}


def test_session_workspace_is_per_owner_and_invisible_across_owners(tmp_path) -> None:
    base = _gateway_agent(tmp_path, scoping=True)
    alice = _resolve_request_agent(base, _req("alice"))
    bob = _resolve_request_agent(base, _req("bob"))
    assert alice.config.session_workspace != bob.config.session_workspace  # ⭐ session_workspace per-owner

    a_mgr = SessionManager(alice.config)
    b_mgr = SessionManager(bob.config)
    sess = a_mgr.create_session(user_id="alice", channel="feishu")
    assert a_mgr.session_exists(sess.session_id)  # alice 自己有
    assert not b_mgr.session_exists(sess.session_id)  # ⭐ bob 物理隔离,看不到 alice 的会话
    assert b_mgr.list_sessions("alice") == []  # bob 的 store 里没有任何 alice 会话


def test_scoping_off_shares_one_session_workspace(tmp_path) -> None:
    base = _gateway_agent(tmp_path, scoping=False)
    a = _resolve_request_agent(base, _req("alice"))
    b = _resolve_request_agent(base, _req("bob"))
    assert a is b is base
    assert a.config.session_workspace == b.config.session_workspace  # 开关关 → 共享(现状不变)
