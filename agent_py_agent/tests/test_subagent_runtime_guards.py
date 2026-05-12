"""LLM: Runtime guard tests for stale runner attempts and factual closeout reports.

函数/模块用途: 验证超时旧线程不能继续调用工具，顶层工具上限收口必须基于 task.json 真实状态。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.subagent_attempt_guard import stale_subagent_attempt_result
from agent_py_agent.agent.agent_core.subagent_dispatch_closeout import (
    subagent_dispatch_limit_response,
)
from agent_py_agent.agent.subagents.manager import SubAgentManager


# LLM: test_stale_attempt_guard_blocks_abandoned_runner_tools covers timeout-thread leakage.
# 函数用途: 模拟 runner attempt 被 timeout abandon 后，旧线程后续 write_file 调用应被工具入口拒绝。
def test_stale_attempt_guard_blocks_abandoned_runner_tools(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    task = manager.create_run(
        goal="写一个文件",
        thought="模拟超时旧线程",
        plan=["write"],
        allowed_tools=["write_file"],
    )
    prepared = manager.prepare_runner_attempt(task.id)
    manager.abandon_runner_attempt(task.id, prepared.runner_active_attempt_id, reason="timeout")
    agent = SimpleNamespace(
        subagents=manager,
        _current_subagent_run_id=task.id,
        _current_subagent_attempt_id=prepared.runner_active_attempt_id,
    )

    result = stale_subagent_attempt_result(agent, {"tool": "write_file", "path": "out.txt"})

    assert result is not None
    assert result.ok is False
    assert result.tool == "write_file"
    assert "已被废弃或超时" in result.output


# LLM: test_dispatch_limit_response_uses_persisted_task_state prevents false-positive final summaries.
# 函数用途: 顶层收口报告必须显示真实 TIMEOUT/BLOCKED 节点，不能把产物存在误说成全部完成。
def test_dispatch_limit_response_uses_persisted_task_state(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    root = manager.create_run(
        goal="root coordinator",
        thought="coordinate",
        plan=["split"],
        agent_name="stage7-root",
        role="coordinator",
    )
    root.status = "TIMEOUT"
    root.verification_status = "UNVERIFIED"
    manager.save(root)
    child = manager.create_run(
        goal="style worker",
        thought="write",
        plan=["write"],
        parent_id=root.id,
        root_id=root.id,
        depth=1,
        agent_name="小傻妞-style",
        role="leaf_worker",
    )
    child.status = "BLOCKED"
    child.verification_status = "FAILED"
    manager.save(child)
    agent = SimpleNamespace(subagents=manager, _current_subagent_run_id="")

    response = subagent_dispatch_limit_response(agent, backend="test")

    assert response is not None
    assert "尚未完整通过" in response.text
    assert root.id in response.text
    assert child.id in response.text
    assert "TIMEOUT/UNVERIFIED" in response.text
    assert "BLOCKED/FAILED" in response.text
    assert "不要把本轮说成完成" in response.text
