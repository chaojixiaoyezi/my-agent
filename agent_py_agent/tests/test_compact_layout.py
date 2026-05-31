from __future__ import annotations

from pathlib import Path


# LLM: compact package shape must stay the same at task, run, and agent levels.
# 函数用途: 验证 task/run/agent 三层 compact 包共享同一组基础文件，防止恢复链路分叉。
def test_compact_package_layout_is_shared_across_levels(tmp_path: Path):
    from agent_py_agent.agent.user_space.compact_layout import ensure_compact_package

    task_pkg = ensure_compact_package(tmp_path / "tasks" / "task_1" / "compact", compact_index=1, scope="task")
    run_pkg = ensure_compact_package(tmp_path / "runs" / "run_1" / "compact", compact_index=1, scope="run")
    agent_pkg = ensure_compact_package(tmp_path / "agents" / "agent_1" / "compact", compact_index=1, scope="agent")

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
