from __future__ import annotations

from agent_py_agent.agent.local_storage.control_plane_models import AgentRuntimeQueryContext
from agent_py_agent.agent.local_store import LocalStore
from agent_py_agent.agent.subagents.manager import SubAgentManager


def test_shared_progress_panel_combines_runtime_query_rollup_and_inheritance_refs(tmp_path) -> None:
    store = LocalStore(tmp_path / "local.db")
    manager = SubAgentManager(tmp_path / "subagents", local_store=store)
    parent = manager.create_run(
        goal="共享进度父任务",
        thought="上级代理需要看到任务树状态和继承线索。",
        plan=["派发子任务"],
        allowed_tools=["read_file", "shell"],
    )
    child = manager.create_run(
        goal="共享进度子任务",
        thought="子代理阻塞时要能被接管视图发现。",
        plan=["写状态"],
        parent_id=parent.id,
        root_id=parent.root_id,
        depth=1,
        allowed_tools=["read_file"],
    )
    child.status = "BLOCKED"
    child.progress = 0.3
    child.current_step = "等待证据"
    child.latest_summary = "子代理等待 evidence packet。"
    child.blockers = ["缺少 evidence packet"]
    manager.save(child)

    panel = store.query_shared_progress_panel(
        AgentRuntimeQueryContext(
            requester_run_id=parent.id,
            root_task_id=parent.root_id,
            scope="root_tree",
            purpose="progress_panel",
        ),
    )

    assert panel.context.requester_run_id == parent.id
    assert panel.rollup is not None
    assert panel.rollup.blocked_agents == 1
    assert [item.run_id for item in panel.runs] == [parent.id, child.id]
    assert [item.run_id for item in panel.blocked_runs] == [child.id]
    assert panel.inheritance_manifest_refs == [child.inheritance_manifest_json]
    assert panel.takeover_readiness_refs == [child.takeover_readiness_json]
    assert panel.reserved["fact_source"] == "task_run_workspace"
