"""多租户:协作 store 是否真按 owner 物理隔离(反查 Phase-2 owner 作用域池有没有漏掉 collaboration)。

最高优先:collaboration_workspace 走 owner 作用域 runtime_root,理论上每 owner 独立 store 文件。
但这取决于 owner 作用域池(OwnerScopedAgentPool)有没有把 collaboration_workspace 也 owner 化——
如果漏了(沿用了 base "main" 路径),就是真实跨租户泄露 bug。这条测试就是来揭真相的。
"""

from __future__ import annotations

from agent_py_agent.agent.collaboration import AgentCapability
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.request_worker import _resolve_request_agent
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


def test_collaboration_store_path_is_per_owner(tmp_path) -> None:
    base = _gateway_agent(tmp_path, scoping=True)
    alice = _resolve_request_agent(base, _req("alice"))
    bob = _resolve_request_agent(base, _req("bob"))
    # ⭐ 两 owner 的协作 store 根路径必须不同(否则跨租户共享 = 隔离泄露)
    assert alice.collaboration_store.root != bob.collaboration_store.root, (
        f"协作 store 未按 owner 隔离!alice={alice.collaboration_store.root} bob={bob.collaboration_store.root}"
    )


def test_owner_a_case_and_requests_invisible_to_owner_b(tmp_path) -> None:
    """功能级隔离:owner A 开的 case + 请求,owner B 完全查不到(跨租户协作零泄露)。"""
    base = _gateway_agent(tmp_path, scoping=True)
    alice = _resolve_request_agent(base, _req("alice"))
    bob = _resolve_request_agent(base, _req("bob"))

    alice.collaboration_store.register_agent(AgentCapability(agent_id="a-src", capabilities=("query",)))
    case = alice.collaboration_store.open_case(
        {"thread_id": "t", "task_id": "task-a", "title": "公司A机密协作", "created_by": "a-src", "now": 1.0}
    )
    alice.collaboration_store.request_collaboration(
        {"case_id": case.case_id, "requester_agent_id": "a-src", "target_agent_ids": ("a-helper",),
         "question": "机密问题", "now": 2.0}
    )

    assert alice.collaboration_store.list_cases()  # alice 自己看得到
    assert bob.collaboration_store.list_cases() == []  # ⭐ bob 物理隔离,啥也看不到
    assert bob.collaboration_store.case_requests(case.case_id) == []  # 也查不到 alice case 的请求


def test_scoping_off_shares_one_collaboration_store(tmp_path) -> None:
    """开关关 → 共享单 agent,协作 store 自然也是同一个(现状不变)。"""
    base = _gateway_agent(tmp_path, scoping=False)
    a = _resolve_request_agent(base, _req("alice"))
    b = _resolve_request_agent(base, _req("bob"))
    assert a is b is base
    assert a.collaboration_store.root == b.collaboration_store.root
