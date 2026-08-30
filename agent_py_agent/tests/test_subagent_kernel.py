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
    assert snapshot.runs[1].workspace_refs["agent_work_dir"].endswith(f"agents/{worker.id}")
    assert snapshot.runs[1].recovery_refs["checkpoint"].endswith("checkpoint.json")
    assert {"read_file", "write_file", "controlled_exec"}.issubset(
        snapshot.runs[1].tool_contract["allowed_tools"]
    )
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


def test_kernel_snapshot_reports_corrupt_task_record_instead_of_hiding_it(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    bad_dir = tmp_path / "run-bad"
    bad_dir.mkdir()
    (bad_dir / "task.json").write_text("{not-json", encoding="utf-8")

    snapshot = manager.kernel_snapshot(SubagentKernelQuery())

    assert snapshot.runs == []
    assert any(item.startswith("subagent_load_error:run-bad") for item in snapshot.warnings)
    errors = snapshot.load_errors
    assert errors[0]["run_id"] == "run-bad"
    assert errors[0]["category"] == "data_parse"
    assert "不要把它当成子代理没产物" in errors[0]["model_message"]


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
    # 真实 ToolResult 使用 tool_name；这个钉子防止测试桩的旧 tool 字段掩盖线上空状态。
    result = SimpleNamespace(tool_name="write_file", output="ok", ok=True)
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


def test_runtime_tool_progress_reports_task_load_error() -> None:
    from types import SimpleNamespace

    from agent_py_agent.agent.subagents.services.session_progress import (
        record_runtime_subagent_tool_progress,
    )

    class Manager:
        def load(self, run_id: str):
            raise FileNotFoundError(run_id)

    progress = record_runtime_subagent_tool_progress(
        SimpleNamespace(subagents=Manager()),
        SimpleNamespace(
            params=SimpleNamespace(context_scope="task_local", run_id="missing-run"),
            result=SimpleNamespace(tool="write_file", output="ok", ok=True),
            payload={"path": "out.md", "content": "done"},
            tool_rounds=1,
            idx=1,
        ),
    )

    assert progress["load_error"]["context"] == "subagent_tool_progress.subagents.load"
    assert progress["run_id"] == "missing-run"


def test_failed_runtime_tool_trace_does_not_claim_success(tmp_path) -> None:
    from types import SimpleNamespace

    from agent_py_agent.agent.subagents.services.session_progress import (
        record_runtime_subagent_tool_progress,
    )

    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="test", thought="", plan=["test"])
    task.agent_run_workspace_dir = str(tmp_path / "agents" / task.id)
    manager.save(task)
    record_runtime_subagent_tool_progress(
        SimpleNamespace(subagents=manager),
        SimpleNamespace(
            params=SimpleNamespace(context_scope="task_local", run_id=task.id),
            result=SimpleNamespace(tool_name="run_command", output="failed", ok=False),
            payload={"command": "cargo test"},
            tool_rounds=1,
            idx=1,
        ),
    )

    loaded = manager.load(task.id)
    trace = loaded.attributes["recent_tool_trace"]
    assert trace[-1]["ok"] is False
    assert trace[-1]["summary"] == "最近失败调用工具: run_command"


def test_runtime_tool_progress_reports_status_save_error(tmp_path) -> None:
    from types import SimpleNamespace

    from agent_py_agent.agent.subagents.services.session_progress import (
        record_runtime_subagent_tool_progress,
    )

    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="write", thought="", plan=["write"])
    task.agent_run_workspace_dir = str(tmp_path / "agents" / task.id)

    class Manager:
        def load(self, run_id: str):
            return task

        def save(self, item):
            raise OSError("state locked")

    progress = record_runtime_subagent_tool_progress(
        SimpleNamespace(subagents=Manager()),
        SimpleNamespace(
            params=SimpleNamespace(context_scope="task_local", run_id=task.id),
            result=SimpleNamespace(tool="write_file", output="ok", ok=True),
            payload={"path": str(tmp_path / "out.md"), "content": "# Summary\ndone"},
            tool_rounds=1,
            idx=1,
        ),
    )

    assert progress["status_save_error"]["context"] == "subagent_tool_progress.subagents.save"
    assert progress["status_save_error"]["message"] == "state locked"


def test_tree_snapshot_carries_capreq_guidance_when_open(tmp_path):
    """P4-1 引导前移钉子:存在 OPEN capreq 时,树快照 tool_contract 直接带
    recommended_tool 与可传参的 request id;无 OPEN 时不带(零噪声)。"""
    from agent_py_agent.agent.subagents.manager import SubAgentManager
    from agent_py_agent.agent.subagents.services.base import CreateRunParams
    from agent_py_agent.agent.subagents.services.lifecycle import RecordCapabilityRequestParams

    manager = SubAgentManager(workspace=tmp_path / "ws")
    task = manager.create_run(
        params=CreateRunParams(goal="引导前移钉子", thought="t", plan=["p"], role="worker")
    )
    snapshot_before = manager.kernel_snapshot(SubagentKernelQuery(root_id=task.id))
    contract_before = snapshot_before.runs[0].tool_contract
    assert "recommended_tool" not in contract_before

    request = manager.lifecycle.record_capability_request(
        task.id,
        RecordCapabilityRequestParams(
            problem="读不到材料",
            needed_capability="read_source_tree",
            capability_type="filesystem",
        ),
    )
    snapshot = manager.kernel_snapshot(SubagentKernelQuery(root_id=task.id))
    contract = snapshot.runs[0].tool_contract
    assert contract["recommended_tool"] == "resolve_capability_requests"
    assert request.id in contract["open_capability_request_ids"]
