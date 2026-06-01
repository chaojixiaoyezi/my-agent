from __future__ import annotations

from pathlib import Path


# LLM: compact package shape must stay the same at task, run, and agent levels.
# 函数用途: 验证 task/run/agent 三层 compact 包共享同一组基础文件，防止恢复链路分叉。
def test_compact_package_layout_is_shared_across_levels(tmp_path: Path):
    from agent_py_agent.agent.user_space.compact_layout import (
        CompactPackageRequest,
        ensure_compact_package,
    )

    task_pkg = ensure_compact_package(tmp_path / "tasks" / "task_1" / "compact", CompactPackageRequest(1, "task"))
    run_pkg = ensure_compact_package(tmp_path / "runs" / "run_1" / "compact", CompactPackageRequest(1, "run"))
    agent_pkg = ensure_compact_package(tmp_path / "agents" / "agent_1" / "compact", CompactPackageRequest(1, "agent"))

    expected = {
        "compact_context.md",
        "handoff_summary.md",
        "work_state_snapshot.json",
        "continue_packet.json",
        "refs.json",
        "metadata.json",
    }

    for pkg in (task_pkg, run_pkg, agent_pkg):
        assert {path.name for path in pkg.package_dir.iterdir() if path.is_file()} == expected
        assert pkg.latest_symlink.exists() or pkg.latest_pointer.exists()
        assert pkg.ledger_jsonl.exists()


# LLM: compact branches let recovery from an old compact continue without overwriting the main chain.
# 函数用途: 验证 compact 包会记录 branch/current_branch/parent_compact_id，供后续恢复区分主链和分叉。
def test_compact_package_records_branch_metadata(tmp_path: Path):
    import json

    from agent_py_agent.agent.user_space.compact_layout import (
        CompactPackageRequest,
        ensure_compact_package,
    )

    first = ensure_compact_package(tmp_path / "compact", CompactPackageRequest(1, "task"))
    branch = ensure_compact_package(
        tmp_path / "compact",
        CompactPackageRequest(2, "task", branch_id="recovery-a", parent_compact_id="compact_0001"),
    )

    metadata = json.loads(branch.metadata_json.read_text(encoding="utf-8"))
    branches = json.loads((tmp_path / "compact" / "branches.json").read_text(encoding="utf-8"))

    assert first.package_dir.name == "compact_0001"
    assert metadata["branch_id"] == "recovery-a"
    assert metadata["parent_compact_id"] == "compact_0001"
    assert (tmp_path / "compact" / "current_branch.txt").read_text(encoding="utf-8").strip() == "recovery-a"
    assert branches["branches"]["main"]["head"] == "compact_0001"
    assert branches["branches"]["recovery-a"]["head"] == "compact_0002"


# LLM: task rollups should keep a branch-specific summary next to the general task summary.
# 函数用途: 验证 task compact rollup 会生成 branch_main_rollup.json，父代理可先读当前分支摘要。
def test_task_compact_rollup_writes_branch_rollup(tmp_path: Path):
    import json

    from agent_py_agent.agent.user_space.task_compact_rollup import sync_task_compact_rollup

    task = tmp_path / "owners" / "local" / "main" / "tasks" / "2026-06-01" / "demo"
    agent_state = task / "work" / "agents" / "agent-1" / "state.json"
    state = task / "work" / "state.json"
    agent_state.parent.mkdir(parents=True, exist_ok=True)
    state.parent.mkdir(parents=True, exist_ok=True)
    agent_state.write_text('{"id":"agent-1","status":"DONE","progress":1.0}\n', encoding="utf-8")
    state.write_text('{"task_id":"demo","status":"RUNNING","progress":0.5}\n', encoding="utf-8")

    result = sync_task_compact_rollup(task)
    branch_rollup = result.compact_root / "rollups" / "branch_main_rollup.json"

    assert branch_rollup.exists()
    payload = json.loads(branch_rollup.read_text(encoding="utf-8"))
    assert payload["branch_id"] == "main"
    assert payload["task_id"] == "demo"
    assert payload["child_count"] == 1
