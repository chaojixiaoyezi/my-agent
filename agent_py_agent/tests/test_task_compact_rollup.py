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


def test_task_compact_rollup_keeps_completed_alias_pending(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.task_compact_rollup import sync_task_compact_rollup

    task_root = tmp_path / "tasks" / "task-root"
    work = task_root / "work"
    (work / "agents" / "agent-old").mkdir(parents=True)
    (work / "state.json").write_text(
        json.dumps({"task_id": "task-root", "status": "RUNNING", "progress": 0.4}),
        encoding="utf-8",
    )
    _write_agent_state(work / "agents" / "agent-old" / "state.json", {"id": "agent-old", "status": "COMPLETED"})

    result = sync_task_compact_rollup(task_root)

    rollup = json.loads(result.rollup_json.read_text(encoding="utf-8"))
    continue_packet = json.loads((result.compact_package_dir / "continue_packet.json").read_text(encoding="utf-8"))
    assert rollup["status_counts"] == {"completed": 1}
    assert rollup["completed_run_ids"] == []
    assert rollup["pending_run_ids"] == ["agent-old"]
    assert continue_packet["pending_work"] == ["agent-old: COMPLETED"]


def test_task_compact_rollup_reports_corrupt_child_state(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.task_compact_rollup import sync_task_compact_rollup

    task_root = tmp_path / "tasks" / "task-root"
    work = task_root / "work"
    (work / "agents" / "agent-good").mkdir(parents=True)
    (work / "agents" / "agent-bad").mkdir(parents=True)
    (work / "state.json").write_text(
        json.dumps({"task_id": "task-root", "status": "RUNNING", "progress": 0.4}),
        encoding="utf-8",
    )
    _write_agent_state(work / "agents" / "agent-good" / "state.json", {"id": "agent-good", "status": "DONE"})
    (work / "agents" / "agent-bad" / "state.json").write_text("{bad-json}\n", encoding="utf-8")

    result = sync_task_compact_rollup(task_root)

    rollup = json.loads(result.rollup_json.read_text(encoding="utf-8"))
    bad_row = next(row for row in rollup["child_runs"] if row["run_id"] == "agent-bad")
    assert rollup["load_errors"]
    assert bad_row["state_load_error"]["context"] == "task_compact_rollup.child_state"
    assert rollup["status_counts"]["unknown"] == 1


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


def test_task_compact_rollup_reuses_current_package_for_status_sync(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.task_compact_rollup import sync_task_compact_rollup

    task_root = tmp_path / "tasks" / "task-root"
    work = task_root / "work"
    (work / "agents" / "agent-a").mkdir(parents=True)
    (work / "state.json").write_text(
        json.dumps({"task_id": "task-root", "status": "RUNNING", "progress": 0.4}),
        encoding="utf-8",
    )
    _write_agent_state(work / "agents" / "agent-a" / "state.json", {"id": "agent-a", "status": "RUNNING"})

    first = sync_task_compact_rollup(task_root)
    _write_agent_state(work / "agents" / "agent-a" / "state.json", {"id": "agent-a", "status": "DONE"})
    second = sync_task_compact_rollup(task_root)

    compact_ledger_lines = (first.compact_root / "compact_ledger.jsonl").read_text(encoding="utf-8").splitlines()
    rollup_ledger_lines = (first.compact_root / "rollup_ledger.jsonl").read_text(encoding="utf-8").splitlines()
    rollup = json.loads(second.rollup_json.read_text(encoding="utf-8"))
    assert first.compact_package_dir == second.compact_package_dir
    assert [path.name for path in first.compact_root.glob("compact_*") if path.is_dir()] == ["compact_0001"]
    assert len([line for line in compact_ledger_lines if line.strip()]) == 1
    assert len([line for line in rollup_ledger_lines if line.strip()]) == 2
    assert rollup["status_counts"] == {"done": 1}


def test_task_compact_rollup_skips_duplicate_rollup_events(tmp_path: Path) -> None:
    from agent_py_agent.agent.user_space.task_compact_rollup import sync_task_compact_rollup

    task_root = tmp_path / "tasks" / "task-root"
    work = task_root / "work"
    (work / "agents" / "agent-a").mkdir(parents=True)
    (work / "state.json").write_text(
        json.dumps({"task_id": "task-root", "status": "RUNNING", "progress": 0.4}),
        encoding="utf-8",
    )
    _write_agent_state(work / "agents" / "agent-a" / "state.json", {"id": "agent-a", "status": "RUNNING"})

    first = sync_task_compact_rollup(task_root)
    sync_task_compact_rollup(task_root)

    rollup_ledger_lines = (first.compact_root / "rollup_ledger.jsonl").read_text(encoding="utf-8").splitlines()
    assert len([line for line in rollup_ledger_lines if line.strip()]) == 1


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
