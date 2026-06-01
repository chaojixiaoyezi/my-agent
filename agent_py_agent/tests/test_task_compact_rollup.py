from __future__ import annotations

import json
from pathlib import Path


def test_task_compact_rollup_exposes_status_counts_and_actionable_refs(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.task_compact_rollup import sync_task_compact_rollup

    task_root = tmp_path / "tasks" / "task-root"
    work = task_root / "work"
    (work / "agents" / "agent-a").mkdir(parents=True)
    (work / "agents" / "agent-b").mkdir(parents=True)
    (work / "agents" / "agent-c").mkdir(parents=True)
    (work / "state.json").write_text(
        json.dumps({"task_id": "task-root", "status": "RUNNING", "progress": 0.4}),
        encoding="utf-8",
    )
    _write_agent_state(work / "agents" / "agent-a" / "state.json", {"id": "agent-a", "status": "DONE", "artifact_refs": ["/tmp/out-a.md"]})
    _write_agent_state(work / "agents" / "agent-b" / "state.json", {"id": "agent-b", "status": "RUNNING"})
    _write_agent_state(work / "agents" / "agent-c" / "state.json", {"id": "agent-c", "status": "BLOCKED", "blockers": ["缺少输入文件"]})

    result = sync_task_compact_rollup(task_root)

    rollup = json.loads(result.rollup_json.read_text(encoding="utf-8"))
    continue_packet = json.loads((result.compact_package_dir / "continue_packet.json").read_text(encoding="utf-8"))
    assert result.compact_root == work / "compact"
    assert rollup["status_counts"] == {"blocked": 1, "done": 1, "running": 1}
    assert rollup["completed_run_ids"] == ["agent-a"]
    assert rollup["pending_run_ids"] == ["agent-b", "agent-c"]
    assert rollup["blocked_run_ids"] == ["agent-c"]
    assert rollup["artifact_refs"] == ["/tmp/out-a.md"]
    assert continue_packet["pending_work"] == ["agent-b: RUNNING", "agent-c: BLOCKED"]
    assert continue_packet["active_refs"][0] == str(result.rollup_json)


def test_task_compact_rollup_writes_owner_level_compact_indexes(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.task_compact_rollup import sync_task_compact_rollup

    owner_home = tmp_path / "home" / "owners" / "local" / "main"
    task_root = owner_home / "tasks" / "2026-05-13" / "task-root"
    work = task_root / "work"
    (work / "agents" / "agent-a").mkdir(parents=True)
    (work / "state.json").write_text(
        json.dumps({"task_id": "task-root", "status": "RUNNING", "primary_run_id": "run-root"}),
        encoding="utf-8",
    )
    _write_agent_state(
        work / "agents" / "agent-a" / "state.json",
        {"id": "agent-a", "task_id": "task-root", "status": "DONE", "artifact_refs": ["/tmp/out-a.md"]},
    )

    result = sync_task_compact_rollup(task_root)

    by_task = json.loads((owner_home / "compact" / "by_task" / "task-root.json").read_text(encoding="utf-8"))
    by_run = json.loads((owner_home / "compact" / "by_run" / "agent-a.json").read_text(encoding="utf-8"))
    by_agent = json.loads((owner_home / "compact" / "by_agent" / "agent-a.json").read_text(encoding="utf-8"))
    assert by_task["rollup_json"] == str(result.rollup_json)
    assert by_run["task_id"] == "task-root"
    assert by_agent["compact_package"] == str(result.compact_package_dir)


def _write_agent_state(path: Path, overrides: dict) -> None:
    run_id = str(overrides.get("id") or path.parent.name)
    payload = {
        "id": run_id,
        "status": "RUNNING",
        "progress": 0.2,
        "latest_summary": f"{run_id} summary",
        "artifact_refs": [],
        "blockers": [],
    }
    payload.update(overrides)
    if str(payload.get("status") or "").upper() == "DONE" and "progress" not in overrides:
        payload["progress"] = 1.0
    path.write_text(json.dumps(payload), encoding="utf-8")
