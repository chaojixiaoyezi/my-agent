from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.memory_archive.compact import MemoryCompactPlanOptions
from agent_py_agent.agent.memory_archive.compact_apply import (
    MemoryCompactApplyOptions,
    apply_memory_compact,
)
from agent_py_agent.agent.memory_archive.compact_resume import (
    MemoryCompactResumeOptions,
    build_memory_compact_resume,
)
from agent_py_agent.agent.memory_archive.runtime_fact_source import (
    RuntimeFactSourceRequest,
    write_runtime_fact_source,
)
from agent_py_agent.agent.task_progress import write_task_progress
from agent_py_agent.tests.memory_compact_support import write_compact_fixture


def test_memory_compact_apply_uses_run_local_directory_and_monotonic_index(tmp_path: Path) -> None:
    """同一 run 多次 compact 应写入 run 专属目录，并按 run 递增 compact_index。"""
    root = tmp_path / "workspace"
    write_compact_fixture(root)

    first = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", run_id="run-compact"),
        ),
    )
    second = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", run_id="run-compact", limit=1),
        ),
    )

    assert "/memory_archive/runs/run-compact/compact_applies/" in first["refs"]["metadata"]
    assert first["compaction_state"]["compact_index"] == 1
    assert second["compaction_state"]["compact_index"] == 2
    assert second["lineage"]["previous_apply_id"] == first["apply_id"]
    assert (root / "memory_archive" / "compact_applies" / "ledger.jsonl").exists()
    resume = build_memory_compact_resume(root, MemoryCompactResumeOptions(apply_ref=second["apply_id"]))
    assert resume["ok"] is True
    assert resume["lineage"]["cycle_index"] == 2


def test_memory_compact_work_state_carries_desired_outputs(tmp_path: Path) -> None:
    """目标产物路径应进入运行状态，compact 后能继续提醒模型写回目标路径。"""
    root = tmp_path / "workspace"
    write_compact_fixture(root)
    write_runtime_fact_source(
        RuntimeFactSourceRequest(
            root=root,
            request_id="request-compact",
            user_prompt="写一份报告到指定位置。",
            run_id="run-compact",
            task_id="run-compact",
            delivery_contract={
                "artifacts": [
                    {
                        "artifact_id": "final_report",
                        "kind": "markdown",
                        "preferred_path": "outputs/final-report.md",
                    }
                ]
            },
        )
    )

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )
    work_state = json.loads(Path(result["refs"]["work_state_snapshot"]).read_text(encoding="utf-8"))

    assert any("target_path" in item and "outputs/final-report.md" in item for item in work_state["desired_outputs"]["items"])
    assert "outputs/final-report.md" in result["handoff_summary"]


def test_memory_compact_work_state_carries_run_intent_paths(tmp_path: Path) -> None:
    """compact 应携带显式参考目录和目标产物，帮助模型区分读资料和写产物。"""
    root = tmp_path / "workspace"
    reference = tmp_path / "reference-projects"
    reference.mkdir()
    write_compact_fixture(root)
    write_runtime_fact_source(
        RuntimeFactSourceRequest(
            root=root,
            request_id="request-compact",
            user_prompt=f"看看 {reference} 下面的项目，报告写到 outputs/final-report.md。",
            run_id="run-compact",
            task_id="run-compact",
            delivery_contract={
                "artifacts": [
                    {
                        "artifact_id": "final_report",
                        "kind": "markdown",
                        "preferred_path": "outputs/final-report.md",
                    }
                ]
            },
        )
    )

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )
    work_state = json.loads(Path(result["refs"]["work_state_snapshot"]).read_text(encoding="utf-8"))

    assert any(str(reference) in item for item in work_state["run_intent"]["reference_roots"]["items"])
    assert any("outputs/final-report.md" in item for item in work_state["run_intent"]["desired_outputs"]["items"])
    assert str(reference) in result["handoff_summary"]
    assert "outputs/final-report.md" in result["handoff_summary"]


def test_memory_compact_work_state_reads_task_coverage_ledger(tmp_path: Path) -> None:
    """compact 应携带覆盖账本摘要，避免长任务压缩后忘记哪些对象没覆盖。"""
    root = tmp_path / "workspace"
    write_compact_fixture(root)
    write_task_progress(
        root,
        "run-compact",
        {
            "summary": "正在覆盖多个项目。",
            "coverage": {
                "goal": "每个项目都要读 README、分析模块、写入报告。",
                "dimensions": ["读 README", "分析模块", "写入报告"],
                "targets": [
                    {
                        "id": "agentscope-main",
                        "checks": {"读 README": "done", "分析模块": "done", "写入报告": "done"},
                    },
                    {
                        "id": "codex-main",
                        "checks": {"读 README": "done", "分析模块": "pending", "写入报告": "pending"},
                    },
                ],
            },
        },
    )

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )

    work_state = json.loads(Path(result["refs"]["work_state_snapshot"]).read_text(encoding="utf-8"))
    coverage = work_state["task_progress"]["coverage"]

    assert coverage["goal"] == "每个项目都要读 README、分析模块、写入报告。"
    assert coverage["counts"]["targets_total"] == 2
    assert coverage["counts"]["targets_done"] == 1
    assert coverage["active_targets"][0]["id"] == "codex-main"
    assert coverage["active_targets"][0]["checks"]["分析模块"] == "pending"
