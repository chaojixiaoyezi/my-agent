"""LLM: focused tests for scoped subagent messaging.

函数/模块用途: 验证上层能通知自己子树、平级能点对点讨论，同时不能越权广播或定向到别的分支。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.subagent_message_tool import SubagentMessageTool
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.hierarchy_scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)


# LLM: _manager_agent gives the tool only the facade it needs.
# 函数用途: 用真实 SubAgentManager 组装轻量 agent，避免测试依赖完整 SimpleAgent。
def _manager_agent(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    return SimpleNamespace(subagents=manager), manager


# LLM: _schedule_child creates real hierarchy edges through the production scheduler.
# 函数用途: 创建 parent 的直接 child，并返回落盘后的任务对象。
def _schedule_child(manager: SubAgentManager, parent_id: str, *, role: str = "worker", name: str = "worker"):
    return _schedule_children(manager, parent_id, [(role, name)])[0]


# LLM: _schedule_children batches sibling creation the same way real root planning often does.
# 函数用途: 一次创建多个同父级 child，避免测试 helper 触发连续单 child 调度防线。
def _schedule_children(manager: SubAgentManager, parent_id: str, specs: list[tuple[str, str]]):
    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent_id,
            child_specs=[
                HierarchyChildSpec(goal=f"负责 {name} 分支", role=role, agent_name=name)
                for role, name in specs
            ],
            apply=True,
        )
    )
    assert result.blocked is False, result.reason
    return [manager.load(run_id) for run_id in result.created_run_ids]


# LLM: test_direct_message_allows_ancestor_to_descendant locks upper-to-lower correction messages.
# 函数用途: root/coordinator 可以给自己子孙的 inbox 发定向纠偏消息，并在 sender outbox 留痕。
def test_direct_message_allows_ancestor_to_descendant(tmp_path):
    agent, manager = _manager_agent(tmp_path)
    root = manager.create_run(goal="root", thought="coordinate", plan=["split"], role="coordinator")
    child = _schedule_child(manager, root.id, role="child_coordinator", name="child-lead")
    grandchild = _schedule_child(manager, child.id, role="worker", name="leaf-worker")

    result = SubagentMessageTool(agent).execute(
        {
            "sender_run_id": root.id,
            "mode": "direct",
            "target_run_ids": [grandchild.id],
            "topic": "path_correction",
            "body": "产物必须写到 build/，不要写 sibling 目录。",
        }
    )

    assert result.ok is True
    payload = json.loads(result.output)
    inbox_file = Path(payload["refs"]["target_inboxes"][0])
    assert inbox_file.exists()
    message = json.loads(inbox_file.read_text(encoding="utf-8"))
    assert message["recipient_scope"] == "descendants"
    assert message["scope_root_run_id"] == root.id


# LLM: test_direct_message_allows_peers_under_same_parent reserves the future team discussion lane.
# 函数用途: 同父级 sibling 可以点对点讨论，但不是广播到全树。
def test_direct_message_allows_peers_under_same_parent(tmp_path):
    agent, manager = _manager_agent(tmp_path)
    root = manager.create_run(goal="root", thought="coordinate", plan=["split"], role="coordinator")
    peer_a, peer_b = _schedule_children(manager, root.id, [("tester", "qa-a"), ("bug_finder", "qa-b")])

    result = SubagentMessageTool(agent).execute(
        {
            "sender_run_id": peer_a.id,
            "mode": "direct",
            "scope": "peers",
            "target_run_ids": [peer_b.id],
            "topic": "team_discussion",
            "body": "我发现 app.js 可能缺少路由函数，请一起核对。",
        }
    )

    assert result.ok is True
    message = json.loads(Path(json.loads(result.output)["refs"]["target_inboxes"][0]).read_text(encoding="utf-8"))
    assert message["recipient_scope"] == "peers"
    assert message["scope_parent_run_id"] == root.id


# LLM: test_broadcast_scopes_to_sender_descendants prevents child broadcasts from claiming sibling subtrees.
# 函数用途: 子代理广播只标记自己的子树范围，不能伪装成 root 全局广播。
def test_broadcast_scopes_to_sender_descendants(tmp_path):
    agent, manager = _manager_agent(tmp_path)
    root = manager.create_run(goal="root", thought="coordinate", plan=["split"], role="coordinator")
    child_a, child_b = _schedule_children(
        manager,
        root.id,
        [("child_coordinator", "a-lead"), ("child_coordinator", "b-lead")],
    )

    result = SubagentMessageTool(agent).execute(
        {
            "sender_run_id": child_a.id,
            "mode": "broadcast",
            "scope": "descendants",
            "topic": "requirement_change",
            "body": "继续前先检查 shared board，按钮不能失效。",
        }
    )

    assert result.ok is True
    refs = json.loads(result.output)["refs"]
    rows = Path(refs["shared_messages"]).read_text(encoding="utf-8").strip().splitlines()
    message = json.loads(rows[-1])
    assert message["recipient_scope"] == "descendants"
    assert message["scope_root_run_id"] == child_a.id
    assert child_b.id not in message["target_run_ids"]


# LLM: test_message_blocks_cross_branch_descendant_targets locks the no-overreach rule.
# 函数用途: 一个 child 不能用 descendants 范围给兄弟分支的孙代理发命令或广播。
def test_message_blocks_cross_branch_descendant_targets(tmp_path):
    agent, manager = _manager_agent(tmp_path)
    root = manager.create_run(goal="root", thought="coordinate", plan=["split"], role="coordinator")
    child_a, child_b = _schedule_children(
        manager,
        root.id,
        [("child_coordinator", "a-lead"), ("child_coordinator", "b-lead")],
    )
    b_leaf = _schedule_child(manager, child_b.id, role="worker", name="b-leaf")

    result = SubagentMessageTool(agent).execute(
        {
            "sender_run_id": child_a.id,
            "mode": "direct",
            "scope": "descendants",
            "target_run_ids": [b_leaf.id],
            "topic": "bad_scope",
            "body": "这条不应该能越权发到兄弟分支孙节点。",
        }
    )

    assert result.ok is False
    assert "不属于 sender 下级" in result.output
