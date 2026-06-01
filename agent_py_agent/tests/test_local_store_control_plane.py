from __future__ import annotations

from agent_py_agent.agent.local_storage.control_plane_models import (
    AgentEventInput,
    AgentRunRecord,
    AgentRuntimeQueryContext,
)
from agent_py_agent.agent.local_store import LocalStore


def _parent_run_record() -> AgentRunRecord:
    return AgentRunRecord(
        run_id="run-parent",
        root_task_id="task-root",
        parent_run_id="",
        depth=0,
        role="coordinator",
        agent_name="parent",
        status="RUNNING",
        progress=0.5,
        current_step="协调子任务",
        latest_summary="父任务正在等待子代理结果。",
        workspace_path="tasks/task-root/agents/run-parent",
        checkpoint_ref="tasks/task-root/agents/run-parent/checkpoint.json",
        latest_compact_ref="",
        compact_count=0,
        heartbeat_at=100.0,
        created_at=90.0,
        updated_at=100.0,
    )


def _child_run_record() -> AgentRunRecord:
    return AgentRunRecord(
        run_id="run-child",
        root_task_id="task-root",
        parent_run_id="run-parent",
        depth=1,
        role="worker",
        agent_name="child",
        status="BLOCKED",
        progress=0.25,
        current_step="等待证据",
        latest_summary="子任务缺少 evidence packet。",
        workspace_path="tasks/task-root/agents/run-child",
        checkpoint_ref="tasks/task-root/agents/run-child/checkpoint.json",
        latest_compact_ref="",
        compact_count=0,
        heartbeat_at=101.0,
        created_at=91.0,
        updated_at=101.0,
    )


def _leaf_run_record() -> AgentRunRecord:
    return AgentRunRecord(
        run_id="run-leaf",
        root_task_id="task-root",
        parent_run_id="run-child",
        depth=2,
        role="worker",
        agent_name="leaf",
        status="RUNNING",
        progress=0.1,
        current_step="执行子步骤",
        latest_summary="叶子任务正在执行。",
        created_at=92.0,
        updated_at=103.0,
    )


def _blocked_sibling_record() -> AgentRunRecord:
    return AgentRunRecord(
        run_id="run-sibling",
        root_task_id="task-root",
        parent_run_id="run-parent",
        depth=1,
        role="worker",
        agent_name="sibling",
        status="BLOCKED",
        progress=0.2,
        current_step="等待接管",
        latest_summary="同级任务需要接管。",
        created_at=93.0,
        updated_at=104.0,
    )


# LLM: _timeout_leaf_record models a hung grandchild that should still be visible to takeover views.
# 函数用途: 构造 TIMEOUT 孙代理投影，验证接管候选不仅包含 BLOCKED。
def _timeout_leaf_record() -> AgentRunRecord:
    return AgentRunRecord(
        run_id="run-timeout",
        root_task_id="task-root",
        parent_run_id="run-child",
        depth=2,
        role="worker",
        agent_name="timeout-leaf",
        status="TIMEOUT",
        progress=0.15,
        current_step="执行超时",
        latest_summary="孙代理超时，需要接管。",
        created_at=95.0,
        updated_at=105.0,
    )


def _store_runtime_query_tree(store: LocalStore) -> None:
    for record in [_parent_run_record(), _child_run_record(), _leaf_run_record(), _blocked_sibling_record()]:
        store.upsert_agent_run(record)
    store.rebuild_task_rollup("task-root")


def test_local_store_control_plane_lists_agent_tree_and_rollup(tmp_path) -> None:
    store = LocalStore(tmp_path / "local.db")
    parent = _parent_run_record()
    child = _child_run_record()

    store.upsert_agent_run(parent)
    store.upsert_agent_run(child)
    event = store.record_agent_event(
        AgentEventInput(
            root_task_id="task-root",
            run_id="run-child",
            parent_run_id="run-parent",
            event_type="blocked",
            payload={"reason": "missing evidence"},
            created_at=102.0,
        ),
    )
    rollup = store.rebuild_task_rollup("task-root")
    tree = store.list_agent_tree("task-root")
    subtree = store.list_subtree("run-parent")
    blocked = store.list_blocked_runs("task-root")

    assert event.event_type == "blocked"
    assert rollup.task_id == "task-root"
    assert rollup.running_agents == 1
    assert rollup.blocked_agents == 1
    assert rollup.completed_agents == 0
    assert rollup.failed_agents == 0
    assert rollup.latest_summary == "子任务缺少 evidence packet。"
    assert [item.run_id for item in tree.runs] == ["run-parent", "run-child"]
    assert tree.rollup == rollup
    assert [item.run_id for item in subtree.runs] == ["run-parent", "run-child"]
    assert [item.run_id for item in blocked] == ["run-child"]


def test_runtime_query_scope_limits_middle_agent_to_own_subtree(tmp_path) -> None:
    store = LocalStore(tmp_path / "local.db")
    _store_runtime_query_tree(store)

    result = store.query_agent_runtime(
        AgentRuntimeQueryContext(
            requester_run_id="run-child",
            scope="own_subtree",
            purpose="progress_panel",
        ),
    )

    assert result.context.requester_run_id == "run-child"
    assert result.context.root_task_id == "task-root"
    assert result.context.visibility == "requester_subtree"
    assert [item.run_id for item in result.report.runs] == ["run-child", "run-leaf"]
    assert result.warnings == []


def test_runtime_query_scope_keeps_takeover_candidates_extensible(tmp_path) -> None:
    store = LocalStore(tmp_path / "local.db")
    _store_runtime_query_tree(store)
    store.upsert_agent_run(_timeout_leaf_record())
    store.rebuild_task_rollup("task-root")

    result = store.query_agent_runtime(
        AgentRuntimeQueryContext(
            requester_run_id="run-parent",
            root_task_id="task-root",
            scope="takeover_candidates",
            purpose="rescue",
            requester_role="coordinator",
            authorized_scope="root_task",
        ),
    )

    assert result.context.visibility == "takeover_candidates"
    assert result.context.authorized_scope == "root_task"
    assert result.report.task_id == "task-root"
    assert [item.run_id for item in result.report.runs] == ["run-child", "run-sibling", "run-timeout"]
    assert result.reserved["takeover_hint"] == "blocked_failed_timeout_runs"


def test_subagent_save_projects_status_into_control_plane(tmp_path) -> None:
    from agent_py_agent.agent.subagents.manager import SubAgentManager

    store = LocalStore(tmp_path / "local.db")
    manager = SubAgentManager(tmp_path / "subagents", local_store=store)
    parent = manager.create_run(
        goal="父任务控制面投影",
        thought="上级代理需要不用扫目录就能看到任务树。",
        plan=["创建父任务", "派发子任务"],
        role="coordinator",
    )
    child = manager.create_run(
        goal="子任务控制面投影",
        thought="子代理保存时要同步状态到 LocalStore。",
        plan=["写状态", "触发投影"],
        role="worker",
        parent_id=parent.id,
        root_id=parent.root_id,
        depth=1,
    )
    child.status = "BLOCKED"
    child.progress = 0.4
    child.current_step = "等待 evidence packet"
    child.latest_summary = "子代理已阻塞，等待证据。"
    child.blockers = ["缺少 evidence packet"]

    manager.save(child)

    _assert_child_control_projection(store.get_agent_run(child.id), parent, child)
    _assert_child_rollup_projection(store, parent.root_id, child.id)


def _assert_child_control_projection(projected, parent, child) -> None:
    assert projected is not None
    assert projected.run_id == child.id
    assert projected.parent_run_id == parent.id
    assert projected.root_task_id == parent.root_id
    assert projected.status == "BLOCKED"
    assert projected.progress == 0.4
    assert projected.current_step == "等待 evidence packet"
    assert projected.latest_summary == "子代理已阻塞，等待证据。"
    assert projected.metadata["system_tree"]["updated_by"] == "system"
    assert projected.metadata["system_tree"]["parent_id"] == parent.id
    assert projected.workspace_path.replace("\\", "/").endswith(f"tasks/{parent.root_id}/work/agents/{child.id}")
    assert projected.checkpoint_ref.endswith("checkpoint.json")


def _assert_child_rollup_projection(store: LocalStore, root_id: str, child_id: str) -> None:
    tree = store.list_agent_tree(root_id)
    rollup = store.get_task_rollup(root_id)
    assert rollup is not None
    assert rollup.blocked_agents == 1
    assert rollup.running_agents == 0
    assert child_id in [item.run_id for item in tree.runs]


# LLM: Regression for multi-level agent trees where stale parent snapshots are saved after child creation.
# 函数用途: 验证旧父/子对象再次保存时不会覆盖 add_child 已写入的 child_ids，避免并行子代理层级断链。
def test_subagent_save_preserves_child_links_from_stale_snapshots(tmp_path) -> None:
    from agent_py_agent.agent.subagents.manager import SubAgentManager

    manager = SubAgentManager(tmp_path)
    parent = manager.create_run(
        goal="父任务",
        thought="保存前已被调用方持有。",
        plan=["派发子任务"],
        role="coordinator",
    )
    child = manager.create_run(
        goal="子任务",
        thought="链接到父任务。",
        plan=["继续派发"],
        parent_id=parent.id,
        root_id=parent.root_id,
        depth=1,
    )
    leaf = manager.create_run(
        goal="孙任务",
        thought="链接到子任务。",
        plan=["执行叶子任务"],
        parent_id=child.id,
        root_id=parent.root_id,
        depth=2,
    )

    parent.status = "RUNNING"
    child.status = "BLOCKED"
    manager.save(parent)
    manager.save(child)

    reloaded_parent = manager.load(parent.id)
    reloaded_child = manager.load(child.id)

    assert child.id in reloaded_parent.child_ids
    assert leaf.id in reloaded_child.child_ids


# LLM: System-owned tree metadata must override model-provided hints.
# 函数用途: 防止子代理把 attributes.system_tree 当成事实源；树关系、状态和 refs 只能由保存链路从任务字段派生。
def test_subagent_save_overwrites_model_written_system_tree_snapshot(tmp_path) -> None:
    from agent_py_agent.agent.subagents.manager import SubAgentManager

    manager = SubAgentManager(tmp_path)
    parent = manager.create_run(
        goal="父任务",
        thought="系统维护任务树。",
        plan=["创建子任务"],
        role="coordinator",
    )
    child = manager.create_run(
        goal="子任务",
        thought="子代理只能写结果，不能改树。",
        plan=["写产物"],
        parent_id=parent.id,
        root_id=parent.root_id,
        depth=1,
    )
    child.status = "DONE"
    child.verification_status = "VERIFIED"
    child.artifact_refs = [str(tmp_path / "artifact.md")]
    child.evidence_refs = [str(tmp_path / "evidence.json")]
    registry_record = {
        "artifact_id": "artifact-child-1",
        "path": str(tmp_path / "artifact.md"),
        "status": "ready",
    }
    child.attributes["artifact_registry_refs"] = [registry_record]
    child.attributes["system_tree"] = {
        "updated_by": "model",
        "parent_id": "fake-parent",
        "root_id": "fake-root",
        "child_ids": ["fake-child"],
        "status": "FAKE",
    }

    manager.save(child)

    reloaded = manager.load(child.id)
    tree = reloaded.attributes["system_tree"]
    snapshot = manager.kernel_snapshot()
    child_node = next(row for row in snapshot.runs if row.run_id == child.id)

    assert tree["updated_by"] == "system"
    assert tree["parent_id"] == parent.id
    assert tree["root_id"] == parent.root_id
    assert tree["child_ids"] == []
    assert tree["status"] == "DONE"
    assert tree["verification_status"] == "VERIFIED"
    assert tree["artifact_refs"] == [str(tmp_path / "artifact.md")]
    assert tree["artifact_registry_refs"] == [registry_record]
    assert tree["evidence_refs"] == [str(tmp_path / "evidence.json")]
    assert child_node.parent_run_id == parent.id
    assert child_node.status == "DONE"
    assert child_node.artifact_registry_refs == [registry_record]
