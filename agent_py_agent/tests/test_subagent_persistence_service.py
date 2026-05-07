from __future__ import annotations

"""LLM: verifies SubAgentManager persistence now routes through a service.

给人看的解释：
这个测试确保 service 化没有改变子代理工单的创建、保存、读取和扫描行为。
"""

import json
from pathlib import Path

from agent_py_agent.agent.subagents.manager import SubAgentManager


def test_subagent_persistence_service_round_trips_task(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)

    task = manager.create_run(
        goal="治理持久化边界",
        thought="把读写职责移到 service 后保持兼容。",
        plan=["创建", "保存", "读取"],
    )
    task.status = "DONE"
    manager.save(task)

    loaded = manager.load(task.id)
    runs = manager.list_runs()

    assert manager.persistence is not None
    assert loaded.id == task.id
    assert loaded.status == "DONE"
    assert loaded.goal == "治理持久化边界"
    assert any(item.id == task.id for item in runs)
    assert (tmp_path / task.id / "task.json").exists()
    assert (tmp_path / task.id / "thought.md").exists()
    assert loaded.latest_status_report.run_id == task.id
    assert loaded.latest_status_report.state == "DONE"
    assert (tmp_path / task.id / "reports" / "status_report.json").exists()
    assert (tmp_path / task.id / "reports" / "checkpoint.json").exists()
    assert (tmp_path / task.id / "reports" / "decision_ledger.json").exists()
    assert (tmp_path / task.id / "reports" / "progress.md").exists()
    assert (tmp_path / task.id / "reports" / "failing_tests.json").exists()
    assert (tmp_path / task.id / "reports" / "next_actions.json").exists()
    assert (tmp_path / task.id / "SKILL_SPARKS.md").exists()
    assert loaded.checkpoint_ref == loaded.checkpoint_json
    assert loaded.skill_sparks_file.endswith("SKILL_SPARKS.md")
    _assert_runtime_workspace_paths(loaded, tmp_path / "tasks" / task.root_id, task.id)


def test_subagent_persistence_creates_task_workspace_skeleton(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)

    task = manager.create_run(
        goal="建立任务级运行记忆空间",
        thought="旧工单目录不能移动，新空间只做 adapter。",
        plan=["建 task workspace", "写兼容指针"],
    )
    task.status = "RUNNING"
    task.progress = 0.5
    task.current_step = "同步状态"
    task.latest_summary = "已创建 task workspace 骨架。"
    manager.save(task)

    task_workspace = tmp_path / "tasks" / task.root_id
    state = json.loads((task_workspace / "state.json").read_text(encoding="utf-8"))
    legacy_ref = json.loads(
        (task_workspace / "agents" / task.id / "legacy_run_ref.json").read_text(encoding="utf-8"),
    )
    timeline_lines = (task_workspace / "timeline.jsonl").read_text(encoding="utf-8").splitlines()
    summary_md = (task_workspace / "summaries" / "current_summary.md").read_text(encoding="utf-8")

    assert (task_workspace / "task.yaml").exists()
    assert (task_workspace / "shared" / "blackboard.md").exists()
    assert (task_workspace / "shared" / "messages.jsonl").exists()
    assert (task_workspace / "shared" / "findings.jsonl").exists()
    assert (task_workspace / "artifacts" / "tool_outputs").is_dir()
    assert (task_workspace / "artifacts" / "log_samples").is_dir()
    assert (task_workspace / "artifacts" / "code_snapshots").is_dir()
    assert (task_workspace / "artifacts" / "reports").is_dir()
    assert state["task_id"] == task.root_id
    assert state["primary_run_id"] == task.id
    assert state["status"] == "RUNNING"
    assert state["progress"] == 0.5
    assert state["legacy"]["task_json"].endswith(f"{task.id}/task.json")
    assert legacy_ref["mode"] == "legacy_subagent_work_order_adapter"
    assert legacy_ref["legacy_task_dir"] == str(tmp_path / task.id)
    assert legacy_ref["agent_run_workspace_status"] == "phase_1_skeleton"
    assert any(json.loads(line)["event"] == "task_workspace_synced" for line in timeline_lines)
    assert "已创建 task workspace 骨架。" in summary_md


def _assert_runtime_workspace_paths(loaded, task_workspace, run_id: str) -> None:
    run_workspace = task_workspace / "agents" / run_id
    assert loaded.task_workspace_dir == str(task_workspace)
    assert loaded.task_workspace_task_yaml == str(task_workspace / "task.yaml")
    assert loaded.task_workspace_state_json == str(task_workspace / "state.json")
    assert loaded.task_workspace_timeline_jsonl == str(task_workspace / "timeline.jsonl")
    assert loaded.task_workspace_summary_file == str(task_workspace / "summaries" / "current_summary.md")
    assert loaded.task_workspace_shared_dir == str(task_workspace / "shared")
    assert loaded.task_workspace_artifacts_dir == str(task_workspace / "artifacts")
    assert loaded.task_workspace_agents_dir == str(task_workspace / "agents")
    assert loaded.agent_run_workspace_dir == str(run_workspace)
    assert loaded.agent_run_agent_yaml == str(run_workspace / "agent.yaml")
    assert loaded.agent_run_state_json == str(run_workspace / "state.json")
    assert loaded.agent_run_task_md == str(run_workspace / "task.md")
    assert loaded.agent_run_timeline_jsonl == str(run_workspace / "timeline.jsonl")
    assert loaded.agent_run_checkpoint_json == str(run_workspace / "checkpoint.json")
    assert loaded.agent_run_summary_md == str(run_workspace / "summary.md")
    assert loaded.agent_run_final_report_md == str(run_workspace / "final_report.md")
    assert loaded.agent_run_findings_jsonl == str(run_workspace / "findings.jsonl")
    assert loaded.agent_run_inbox_dir == str(run_workspace / "inbox")
    assert loaded.agent_run_outbox_dir == str(run_workspace / "outbox")
    assert loaded.agent_run_artifacts_dir == str(run_workspace / "artifacts")
    assert loaded.agent_run_compactions_dir == str(run_workspace / "compactions")
    assert loaded.legacy_run_ref_json == str(run_workspace / "legacy_run_ref.json")
    assert "/daily/" in loaded.daily_ledger_file
    assert loaded.daily_ledger_file.endswith("/events.jsonl")
    assert loaded.daily_ledger_last_event_id.startswith(f"evt-{run_id}-{run_id}-")
    assert loaded.task_artifact_manifest_jsonl == str(task_workspace / "artifacts" / "manifest.jsonl")
    assert loaded.agent_run_artifact_manifest_jsonl == str(run_workspace / "artifacts" / "manifest.jsonl")


def test_subagent_persistence_creates_agent_run_workspace_skeleton(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)

    task = manager.create_run(
        goal="建立子代理运行工作位",
        thought="在 task workspace 下生成 run workspace，但旧目录继续兼容。",
        plan=["写 agent.yaml", "写 run state", "写恢复入口"],
    )
    task.status = "BLOCKED"
    task.progress = 0.25
    task.current_step = "等待证据"
    task.latest_summary = "run workspace 已创建，等待证据。"
    task.blockers = ["缺少日志样本"]
    task.result = "暂未完成"
    manager.save(task)

    run_workspace = tmp_path / "tasks" / task.root_id / "agents" / task.id
    run_state = json.loads((run_workspace / "state.json").read_text(encoding="utf-8"))
    checkpoint = json.loads((run_workspace / "checkpoint.json").read_text(encoding="utf-8"))
    legacy_ref = json.loads((run_workspace / "legacy_run_ref.json").read_text(encoding="utf-8"))
    run_timeline = (run_workspace / "timeline.jsonl").read_text(encoding="utf-8").splitlines()

    assert (run_workspace / "agent.yaml").exists()
    assert (run_workspace / "task.md").exists()
    assert (run_workspace / "summary.md").exists()
    assert (run_workspace / "final_report.md").exists()
    assert (run_workspace / "findings.jsonl").exists()
    assert (run_workspace / "inbox").is_dir()
    assert (run_workspace / "outbox").is_dir()
    assert (run_workspace / "artifacts" / "tool_outputs").is_dir()
    assert (run_workspace / "artifacts" / "reports").is_dir()
    assert (run_workspace / "compactions").is_dir()
    assert run_state["task_id"] == task.root_id
    assert run_state["run_id"] == task.id
    assert run_state["status"] == "BLOCKED"
    assert run_state["blockers"] == ["缺少日志样本"]
    assert checkpoint["legacy_checkpoint_ref"].endswith(f"{task.id}/reports/checkpoint.json")
    assert legacy_ref["legacy_task_dir"] == str(tmp_path / task.id)
    assert legacy_ref["agent_run_workspace_status"] == "phase_1_skeleton"
    assert any(json.loads(line)["event"] == "agent_run_workspace_synced" for line in run_timeline)


def test_subagent_persistence_appends_daily_event_ledger(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)

    task = manager.create_run(
        goal="写每日事件账本",
        thought="事件只放摘要、状态和引用，不放完整上下文。",
        plan=["保存任务", "检查 daily ledger"],
    )
    task.status = "RUNNING"
    task.current_step = "记录事件"
    task.latest_summary = "daily ledger 已追加 task/run 引用。"
    task.artifact_refs = ["reports/demo.txt"]
    task.evidence_refs = ["logs/demo.log"]
    manager.save(task)

    loaded = manager.load(task.id)
    events = _read_jsonl(loaded.daily_ledger_file)
    latest = events[-1]

    assert loaded.daily_ledger_last_event_id == latest["event_id"]
    assert latest["event_type"] == "subagent_task_saved"
    assert latest["task_id"] == task.root_id
    assert latest["run_id"] == task.id
    assert latest["status"] == "RUNNING"
    assert latest["summary"] == "daily ledger 已追加 task/run 引用。"
    assert latest["artifact_refs"] == ["reports/demo.txt"]
    assert latest["evidence_refs"] == ["logs/demo.log"]
    assert latest["refs"]["task_workspace"] == str(tmp_path / "tasks" / task.root_id)
    assert latest["refs"]["agent_run_workspace"] == str(tmp_path / "tasks" / task.root_id / "agents" / task.id)
    assert latest["refs"]["task_artifact_manifest"] == str(tmp_path / "tasks" / task.root_id / "artifacts" / "manifest.jsonl")
    assert latest["refs"]["agent_artifact_manifest"] == str(
        tmp_path / "tasks" / task.root_id / "agents" / task.id / "artifacts" / "manifest.jsonl",
    )
    assert latest["refs"]["legacy_task_dir"] == str(tmp_path / task.id)
    assert "goal" not in latest


def test_subagent_persistence_writes_artifact_manifests(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)

    task = manager.create_run(
        goal="外置 artifact 引用",
        thought="manifest 只记录摘要、hash 和路径，不复制输出正文。",
        plan=["写 artifact", "保存 manifest"],
    )
    artifact_path = tmp_path / task.id / "reports" / "demo.txt"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text("artifact body\n", encoding="utf-8")
    task.artifact_refs = ["reports/demo.txt", "missing.log"]
    manager.save(task)

    loaded = manager.load(task.id)
    task_records = _read_jsonl(loaded.task_artifact_manifest_jsonl)
    run_records = _read_jsonl(loaded.agent_run_artifact_manifest_jsonl)
    existing = task_records[0]
    missing = task_records[1]

    assert run_records == task_records
    assert existing["ref"] == "reports/demo.txt"
    assert existing["path"] == str(artifact_path)
    assert existing["exists"] is True
    assert existing["size_bytes"] == len("artifact body\n")
    assert existing["sha256"]
    assert existing["content_externalized"] is True
    assert "artifact body" not in json.dumps(existing, ensure_ascii=False)
    assert missing["ref"] == "missing.log"
    assert missing["exists"] is False
    assert missing["sha256"] == ""


def test_subagent_persistence_writes_checkpoint_recovery_artifacts(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)

    task = manager.create_run(
        goal="恢复 compact 后的子代理事实",
        thought="只保存恢复需要的结构化事实。",
        plan=["写状态", "写失败测试", "写下一步"],
    )
    output_payload = {
        "run_id": task.id,
        "status": "BLOCKED",
        "blockers": ["缺少验证证据"],
        "tests": [
            {
                "name": "focused",
                "ok": False,
                "message": "assertion failed",
                "evidence_ref": "logs/focused.txt",
            },
            {"name": "lint", "ok": True},
        ],
        "next_actions": ["补证据链"],
        "next_action": "请求父级验收",
    }
    (tmp_path / task.id / "output.json").write_text(json.dumps(output_payload), encoding="utf-8")
    task.status = "BLOCKED"
    task.progress = 0.4
    task.current_step = "等待证据"
    task.latest_summary = "runner 已产出材料但证据不足。"
    task.blockers = ["父级未验收"]
    task.artifact_refs = ["output.json"]
    task.evidence_refs = ["logs/focused.txt"]

    manager.save(task)

    checkpoint = json.loads((tmp_path / task.id / "reports" / "checkpoint.json").read_text(encoding="utf-8"))
    failing_tests = json.loads((tmp_path / task.id / "reports" / "failing_tests.json").read_text(encoding="utf-8"))
    next_actions = json.loads((tmp_path / task.id / "reports" / "next_actions.json").read_text(encoding="utf-8"))
    progress_md = (tmp_path / task.id / "reports" / "progress.md").read_text(encoding="utf-8")

    assert checkpoint["run_id"] == task.id
    assert checkpoint["status"] == "BLOCKED"
    assert checkpoint["checkpoint_ref"].endswith("reports/checkpoint.json")
    assert checkpoint["status_report_ref"].endswith("reports/status_report.json")
    assert checkpoint["blockers"] == ["父级未验收", "缺少验证证据"]
    assert failing_tests["failing_tests"] == [
        {
            "name": "focused",
            "status": "failed",
            "evidence_ref": "logs/focused.txt",
            "message": "assertion failed",
        }
    ]
    assert next_actions["next_actions"][:2] == ["补证据链", "请求父级验收"]
    assert "runner 已产出材料但证据不足。" in progress_md


def _read_jsonl(path: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
