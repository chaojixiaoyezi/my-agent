"""LLM: focused tests for refs-only multi-level subagent recovery packets.

函数/模块用途: 验证父代理能从 root run 恢复整棵子/孙任务树，并只拿 refs，不读取 artifact 正文。
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.hierarchy_recovery import HierarchyRecoveryRequest
from agent_py_agent.agent.subagents.services.hierarchy_scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)


# LLM: _make_tree creates a 1 parent / 2 child / 4 grandchild recovery fixture.
# 函数用途: 构造多层子代理树，并让一个孙代理失败、一个孙代理超时。
def _make_tree(manager: SubAgentManager):
    root = manager.create_run(goal="root", thought="orchestrate", plan=["split"])
    children = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=root.id,
            apply=True,
            child_specs=[
                HierarchyChildSpec(goal="child one", role="reporter", agent_name="child-1"),
                HierarchyChildSpec(goal="child two", role="reporter", agent_name="child-2"),
            ],
        )
    ).created_run_ids
    grandchildren: list[str] = []
    for child_id in children:
        grandchildren.extend(
            manager.schedule_child_runs(
                params=HierarchyScheduleRequest(
                    parent_run_id=child_id,
                    apply=True,
                    child_specs=[
                        HierarchyChildSpec(goal=f"{child_id} grand one", role="checker", agent_name="grand-1"),
                        HierarchyChildSpec(goal=f"{child_id} grand two", role="checker", agent_name="grand-2"),
                    ],
                )
            ).created_run_ids
        )
    blocked = manager.load(grandchildren[0])
    artifact_path = Path(blocked.reports_dir) / "large.txt"
    artifact_path.write_text("DO_NOT_READ_THIS_RECOVERY_ARTIFACT_BODY", encoding="utf-8")
    blocked.status = "BLOCKED"
    blocked.latest_summary = "blocked with large artifact ref"
    blocked.artifact_refs = [str(artifact_path)]
    blocked.blockers = ["needs takeover"]
    manager.save(blocked)

    timed_out = manager.load(grandchildren[-1])
    timed_out.status = "TIMEOUT"
    timed_out.failure_type = "runner_timeout"
    timed_out.blockers = ["runner timed out"]
    manager.save(timed_out)
    return root, children, grandchildren


# LLM: test_hierarchy_recovery_packet_collects_multilevel_candidates_refs_only covers tree recovery.
# 函数用途: 确认恢复包能列出多层节点、失败候选和接管入口，但不读取 artifact 正文。
def test_hierarchy_recovery_packet_collects_multilevel_candidates_refs_only(tmp_path):
    manager = SubAgentManager(tmp_path)
    root, _, grandchildren = _make_tree(manager)

    result = manager.build_hierarchy_recovery_packet(
        params=HierarchyRecoveryRequest(root_run_id=root.id, requested_by="parent")
    )
    payload = json.dumps(result.to_dict(), ensure_ascii=False)

    assert result.root_run_id == root.id
    assert result.node_count == 7
    assert result.recovery_candidate_count == 2
    assert {item.run_id for item in result.recovery_candidates} == {grandchildren[0], grandchildren[-1]}
    assert all(item.takeover_readiness_ref for item in result.recovery_candidates)
    assert all(item.recommended_command.startswith("my-agent subagents-acceptance-plan") for item in result.recovery_candidates)
    assert "DO_NOT_READ_THIS_RECOVERY_ARTIFACT_BODY" not in payload


# LLM: test_hierarchy_recovery_packet_can_hide_healthy_nodes keeps compact handoff small.
# 函数用途: include_healthy=False 时只返回 root 和需要恢复的节点，减少上下文压力。
def test_hierarchy_recovery_packet_can_hide_healthy_nodes(tmp_path):
    manager = SubAgentManager(tmp_path)
    root, _, grandchildren = _make_tree(manager)

    result = manager.build_hierarchy_recovery_packet(
        params=HierarchyRecoveryRequest(root_run_id=root.id, include_healthy=False)
    )

    assert [item.run_id for item in result.nodes] == [root.id, grandchildren[0], grandchildren[-1]]
    assert result.omitted_healthy_count == 4
