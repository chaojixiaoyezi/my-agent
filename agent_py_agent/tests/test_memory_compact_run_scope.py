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
    ApprovedRuntimeFactSourceRequest,
    RuntimeFactSourceRequest,
    write_approved_runtime_fact_source,
    write_runtime_fact_source,
)
from agent_py_agent.agent.task_progress import progress_path, write_task_progress
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


def test_memory_compact_work_state_carries_target_coverage(tmp_path: Path) -> None:
    """compact 后必须保留结构化目标覆盖合同，不能只靠 handoff 自然语言续接。"""
    root = tmp_path / "workspace"
    write_compact_fixture(root)
    write_runtime_fact_source(
        RuntimeFactSourceRequest(
            root=root,
            request_id="request-compact",
            user_prompt="完整读完 data/source.txt 后写报告。",
            run_id="run-compact",
            task_id="run-compact",
            delivery_contract={
                "artifacts": [
                    {
                        "artifact_id": "final_report",
                        "kind": "markdown",
                        "preferred_path": "outputs/final-report.md",
                    }
                ],
                "target_coverage_contract": {
                    "coverage_requirement": "full_source_read",
                    "enforcement": "required",
                    "target_items": [
                        {
                            "target_id": "data/source.txt",
                            "source_path": "data/source.txt",
                            "coverage_kind": "full_source_read",
                        }
                    ],
                },
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
    metadata = json.loads(Path(result["refs"]["metadata"]).read_text(encoding="utf-8"))

    coverage = work_state["target_coverage"]
    assert coverage["source_status"] == "recorded"
    assert coverage["payload"]["coverage_requirement"] == "full_source_read"
    assert coverage["payload"]["target_items"][0]["source_path"] == "data/source.txt"
    assert metadata["compaction_state"]["work"]["target_coverage"]["coverage_requirement"] == "full_source_read"
    assert "目标覆盖: full_source_read" in result["handoff_summary"]
    assert "data/source.txt" in result["handoff_summary"]


def test_approved_runtime_fact_preserves_delivery_and_coverage(tmp_path: Path) -> None:
    """人工补验收事实不能覆盖掉同一 run 原有的交付目标和覆盖账本。"""
    root = tmp_path / "workspace"
    write_compact_fixture(root)
    write_runtime_fact_source(
        RuntimeFactSourceRequest(
            root=root,
            request_id="request-compact",
            user_prompt="完整读完 data/source.txt 后写报告。",
            run_id="run-compact",
            task_id="task-compact",
            delivery_contract={
                "artifacts": [
                    {
                        "artifact_id": "final_report",
                        "kind": "markdown",
                        "preferred_path": "outputs/final-report.md",
                    }
                ],
                "target_coverage_contract": {
                    "coverage_requirement": "full_source_read",
                    "enforcement": "required",
                    "target_items": [
                        {
                            "target_id": "data/source.txt",
                            "source_path": "data/source.txt",
                            "coverage_kind": "full_source_read",
                        }
                    ],
                },
            },
        )
    )

    write_approved_runtime_fact_source(
        ApprovedRuntimeFactSourceRequest(
            root=root,
            fact_id="request-compact",
            goal="补充 compact 验收事实",
            next_actions=["继续读取 compact 交接材料"],
            acceptance=["final-report.md 包含源文件结论"],
            constraints=["不得猜测未读取内容"],
            latest_tests=["compact roundtrip passed"],
            source_apply_id="apply-1",
        )
    )

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )
    fact_payload = json.loads(
        (root / "memory_archive" / "runtime_facts" / "request-compact" / "task.json").read_text(encoding="utf-8")
    )
    work_state = json.loads(Path(result["refs"]["work_state_snapshot"]).read_text(encoding="utf-8"))

    assert fact_payload["source"] == "approved_runtime_fact_source"
    assert fact_payload["run_id"] == "run-compact"
    assert fact_payload["task_id"] == "task-compact"
    assert fact_payload["desired_outputs"][0]["target_path"] == "outputs/final-report.md"
    assert fact_payload["target_coverage"]["target_items"][0]["source_path"] == "data/source.txt"
    assert work_state["desired_outputs"]["source_status"] == "recorded"
    assert work_state["target_coverage"]["source_status"] == "recorded"
    assert work_state["target_coverage"]["payload"]["coverage_requirement"] == "full_source_read"


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


def test_memory_compact_work_state_reports_corrupt_run_intent_task_json(tmp_path: Path) -> None:
    """运行意图来源 task.json 坏了不能被 compact 当成没有路径意图。"""
    root = tmp_path / "workspace"
    write_compact_fixture(root)
    task_root = root / "tasks" / "run-compact"
    task_root.mkdir(parents=True, exist_ok=True)
    (task_root / "task.json").write_text("{not-json", encoding="utf-8")

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )

    work_state = json.loads(Path(result["refs"]["work_state_snapshot"]).read_text(encoding="utf-8"))

    assert work_state["run_intent"]["load_errors"][0]["context"] == "compact_work_state.run_intent.task_json"


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
                        "id": "sample_a-main",
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
    assert coverage["active_targets"][0]["id"] == "sample_a-main"
    assert coverage["active_targets"][0]["checks"]["分析模块"] == "pending"


def test_memory_compact_work_state_reports_corrupt_task_progress(tmp_path: Path) -> None:
    """进度账本坏了应进入 compact 恢复材料，而不是被当成没有进度。"""
    root = tmp_path / "workspace"
    write_compact_fixture(root)
    path = progress_path(root, "run-compact")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not-json", encoding="utf-8")

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )

    work_state = json.loads(Path(result["refs"]["work_state_snapshot"]).read_text(encoding="utf-8"))

    assert work_state["task_progress"]["load_error"]["context"] == "task_progress.read"
