from __future__ import annotations

import json

from agent_py_agent.agent.subagents.manager import SubAgentManager


def test_subagent_save_does_not_overwrite_main_task_workspace_state(tmp_path) -> None:
    manager = SubAgentManager(tmp_path / "workspace")
    task_root = tmp_path / "home" / "owners" / "local" / "main" / "tasks" / "2026-06-03" / "分析-all-agent"
    work = task_root / "work"
    work.mkdir(parents=True)
    original_state = {
        "version": 1,
        "request_id": "req-main",
        "run_id": "run-main",
        "task_id": "分析 all-agent",
        "owner_id": "local/main",
        "updated_at": "2026-06-03T00:00:00+00:00",
    }
    (work / "state.json").write_text(json.dumps(original_state, ensure_ascii=False), encoding="utf-8")
    (work / "summaries").mkdir(parents=True)
    (work / "summaries" / "current_summary.md").write_text(
        "# Current Summary\n\n- task_id: 分析 all-agent\n- primary_run_id: run-main\n\n## Latest\n\n父任务摘要\n",
        encoding="utf-8",
    )

    child = manager.create_run(
        goal="分析子项目",
        thought="主任务目录来自父代理，子代理只能写 agents 子目录。",
        plan=["读源码", "写报告"],
        root_id="run-main",
        parent_id="run-main",
        depth=1,
    )
    child.attributes = {"run_workspace": {"task_root": str(task_root)}}
    child.status = "RUNNING"
    manager.save(child)

    state = json.loads((work / "state.json").read_text(encoding="utf-8"))
    run_state = json.loads((work / "agents" / child.id / "state.json").read_text(encoding="utf-8"))

    assert state["request_id"] == "req-main"
    assert state["run_id"] == "run-main"
    assert state["task_id"] == "分析 all-agent"
    assert state["child_run_ids"] == [child.id]
    assert "父任务摘要" in (work / "summaries" / "current_summary.md").read_text(encoding="utf-8")
    assert run_state["run_id"] == child.id
    assert run_state["task_id"] == "run-main"


def test_subagent_save_with_child_root_id_keeps_parent_workspace_identity(tmp_path) -> None:
    manager = SubAgentManager(tmp_path / "workspace")
    task_root = tmp_path / "home" / "owners" / "local" / "main" / "tasks" / "2026-06-03" / "分析-all-agent"
    work = task_root / "work"
    work.mkdir(parents=True)
    (work / "task.yaml").write_text(
        'task_id: "all-agent-架构分析"\nrun_id: "run-main"\n',
        encoding="utf-8",
    )
    (work / "state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "request_id": "req-main",
                "run_id": "run-main",
                "task_id": "all-agent-架构分析",
                "updated_at": "2026-06-03T00:00:00+00:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (work / "summaries").mkdir(parents=True)
    (work / "summaries" / "current_summary.md").write_text("父任务当前摘要\n", encoding="utf-8")

    child = manager.create_run(
        goal="分析 claude-code",
        thought="子代理 root_id 初始等于自己，但 run_workspace 指向父任务目录。",
        plan=["读源码", "写报告"],
        parent_id="run-main",
        depth=1,
    )
    child.attributes = {"run_workspace": {"task_root": str(task_root)}}
    child.status = "DONE"
    child.progress = 1.0
    child.latest_summary = "子代理完成摘要，不能覆盖父摘要。"
    manager.save(child)

    state = json.loads((work / "state.json").read_text(encoding="utf-8"))
    summary_md = (work / "summaries" / "current_summary.md").read_text(encoding="utf-8")
    run_state = json.loads((work / "agents" / child.id / "state.json").read_text(encoding="utf-8"))

    assert state["request_id"] == "req-main"
    assert state["run_id"] == "run-main"
    assert state["task_id"] == "all-agent-架构分析"
    assert state["child_run_ids"] == [child.id]
    assert "父任务当前摘要" in summary_md
    assert "子代理完成摘要" not in summary_md
    assert run_state["task_id"] == "run-main"
    assert run_state["run_id"] == child.id
