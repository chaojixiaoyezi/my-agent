from __future__ import annotations

from agent_py_agent.agent.subagents.kernel import SubagentKernelQuery
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.models import CapabilityGap, CapabilityGrant, CapabilityRequest


def test_kernel_snapshot_returns_root_tree_with_status_buckets(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    root, worker = _create_kernel_root_worker(manager, tmp_path)

    snapshot = manager.kernel_snapshot(SubagentKernelQuery(root_id=root.id))

    assert snapshot.schema_version == "subagent_kernel_snapshot.v1"
    assert snapshot.root_id == root.id
    assert [item.run_id for item in snapshot.runs] == [root.id, worker.id]
    assert snapshot.running_run_ids == [root.id]
    assert snapshot.completed_run_ids == [worker.id]
    assert snapshot.runs[1].parent_id == root.id
    assert snapshot.runs[1].workspace_refs["agent_run_workspace"].endswith(f"agents/{worker.id}")
    assert snapshot.runs[1].recovery_refs["checkpoint"].endswith("checkpoint.json")
    assert snapshot.runs[1].tool_contract["allowed_tools"] == ["read_file", "write_file", "controlled_exec"]
    assert snapshot.runs[1].tool_contract["used_tools"] == ["write_file"]
    assert snapshot.runs[1].task_id == worker.id
    assert snapshot.runs[1].run_id == worker.id
    assert snapshot.runs[1].parent_task_id == root.id
    assert snapshot.runs[1].parent_run_id == root.id
    assert snapshot.runs[1].root_run_id == root.id
    assert snapshot.runs[1].agent_kind == "child_agent"
    assert snapshot.runs[1].heartbeat_at == worker.heartbeat_at
    assert snapshot.runs[1].current_tool == "write_file"
    assert snapshot.runs[1].last_progress_at == 1234.0
    assert snapshot.runs[1].last_progress_summary == "写出 HTML 产物"
    assert snapshot.runs[1].tool_contract["open_request_count"] == 1
    assert snapshot.runs[1].tool_contract["grant_count"] == 1
    assert snapshot.runs[1].tool_contract["gap_count"] == 1
    assert snapshot.runs[1].tool_contract["controlled_exec_grant_ids"] == ["grant-shell"]
    assert snapshot.runs[1].artifact_refs == ["artifact:index.html"]


# LLM: _create_kernel_root_worker keeps the kernel bucket test below the strict function-size line.
# 函数用途: 构造 root + worker fixture，包含工具合同、恢复引用和产物引用。
def _create_kernel_root_worker(manager: SubAgentManager, tmp_path):
    root = manager.create_run(
        goal="做一个家具品牌首页",
        thought="root 只负责派工和验收。",
        plan=["派 worker", "收 refs", "验收"],
        role="coordinator",
        agent_name="root",
    )
    worker = manager.create_run(
        goal="写 index.html",
        thought="交付单文件 HTML。",
        plan=["写文件"],
        role="worker",
        agent_name="小傻妞-html",
        parent_id=root.id,
        root_id=root.id,
        depth=1,
    )
    _populate_worker_kernel_fields(worker, tmp_path, root.id)
    manager.save(worker)
    root.child_ids = [worker.id]
    root.status = "RUNNING"
    manager.save(root)
    return root, worker


# LLM: _populate_worker_kernel_fields isolates tool and recovery refs from the core assertion path.
# 函数用途: 给 worker fixture 补齐工具申请、授权、缺口、artifact 和 checkpoint refs。
def _populate_worker_kernel_fields(worker, tmp_path, root_id: str) -> None:
    worker.status = "DONE"
    worker.progress = 1.0
    worker.current_tool = "write_file"
    worker.last_progress_at = 1234.0
    worker.last_progress_summary = "写出 HTML 产物"
    worker.latest_summary = "index.html 已完成"
    worker.allowed_tools = ["read_file", "write_file", "controlled_exec"]
    worker.used_tools = ["write_file"]
    worker.capability_requests = [
        CapabilityRequest(id="req-shell", from_run_id=worker.id, problem="需要跑本地测试", needed_capability="shell")
    ]
    worker.capability_grants = [
        CapabilityGrant(id="grant-shell", request_id="req-shell", grant_to_run_id=worker.id, tools=["controlled_exec"])
    ]
    worker.capability_gaps = [
        CapabilityGap(id="gap-api", run_id=worker.id, missing_capability="api", source_task="worker", why_failed="无凭据")
    ]
    worker.artifact_refs = ["artifact:index.html"]
    worker.agent_run_workspace_dir = str(tmp_path / "tasks" / root_id / "agents" / worker.id)
    worker.agent_run_checkpoint_json = str(tmp_path / "checkpoint.json")


def test_kernel_snapshot_can_scope_to_own_subtree(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    root = manager.create_run(goal="root", thought="", plan=["split"], role="coordinator")
    child = manager.create_run(
        goal="child",
        thought="",
        plan=["delegate"],
        parent_id=root.id,
        root_id=root.id,
        depth=1,
        role="coordinator",
    )
    leaf = manager.create_run(
        goal="leaf",
        thought="",
        plan=["write"],
        parent_id=child.id,
        root_id=root.id,
        depth=2,
        role="worker",
    )
    sibling = manager.create_run(
        goal="sibling",
        thought="",
        plan=["write"],
        parent_id=root.id,
        root_id=root.id,
        depth=1,
        role="worker",
    )
    child.child_ids = [leaf.id]
    root.child_ids = [child.id, sibling.id]
    for task in [root, child, leaf, sibling]:
        manager.save(task)

    snapshot = manager.kernel_snapshot(SubagentKernelQuery(run_id=child.id, scope="own_subtree"))

    assert snapshot.root_id == root.id
    assert [item.run_id for item in snapshot.runs] == [child.id, leaf.id]
    assert sibling.id not in [item.run_id for item in snapshot.runs]


def test_kernel_snapshot_marks_takeover_candidates_without_new_guards(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    root = manager.create_run(goal="root", thought="", plan=["run"], role="coordinator")
    failed = manager.create_run(
        goal="failed worker",
        thought="",
        plan=["write"],
        parent_id=root.id,
        root_id=root.id,
        depth=1,
        role="worker",
    )
    failed.status = "TIMEOUT"
    failed.failure_type = "model_timeout"
    failed.failure_handoff.warning = "模型调用超时"
    manager.save(failed)
    root.child_ids = [failed.id]
    manager.save(root)

    snapshot = manager.kernel_snapshot(SubagentKernelQuery(root_id=root.id))

    assert snapshot.failed_run_ids == [failed.id]
    assert snapshot.takeover_candidate_run_ids == [failed.id]
    assert snapshot.runs[1].failure_type == "model_timeout"
    assert "failure_warning" in snapshot.runs[1].recovery_refs


def test_runtime_tool_progress_updates_agent_status_fields(tmp_path) -> None:
    from types import SimpleNamespace

    from agent_py_agent.agent.subagents.services.session_progress import (
        record_runtime_subagent_tool_progress,
    )

    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="write", thought="", plan=["write"])
    task.agent_run_workspace_dir = str(tmp_path / "agents" / task.id)
    manager.save(task)
    agent = SimpleNamespace(subagents=manager)
    params = SimpleNamespace(context_scope="task_local", run_id=task.id)
    result = SimpleNamespace(tool="write_file", output="ok", ok=True)
    record = SimpleNamespace(
        params=params,
        result=result,
        payload={"path": str(tmp_path / "out.md"), "content": "# Summary\ndone"},
        tool_rounds=7,
        idx=2,
    )

    record_runtime_subagent_tool_progress(agent, record)

    loaded = manager.load(task.id)
    assert loaded.current_tool == "write_file"
    assert loaded.progress > 0.0
    assert loaded.last_progress_at > 0
    assert loaded.last_progress_summary
    assert loaded.heartbeat_at == loaded.last_progress_at
