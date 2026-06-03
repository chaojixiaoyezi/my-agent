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
from agent_py_agent.agent.memory_archive.compact_work_state import (
    WorkStateFieldSourceRequest,
    build_work_state_field_sources,
)
from agent_py_agent.tests.memory_compact_support import write_compact_fixture


def test_memory_compact_work_state_reads_runtime_handoff(tmp_path: Path) -> None:
    """compact 应携带通用运行交接：最近引导和可见下级状态，而不是只恢复任务目标。"""
    root = tmp_path / "workspace"
    write_compact_fixture(root)
    _write_runtime_handoff_sources(root)

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )

    work_state = json.loads(Path(result["refs"]["work_state_snapshot"]).read_text(encoding="utf-8"))
    handoff = work_state["runtime_handoff"]
    resume = build_memory_compact_resume(root, MemoryCompactResumeOptions(apply_ref=result["apply_id"]))

    assert handoff["recent_guidance"][0]["message"] == "写报告时逐个对象收口，不要最后一次性打勾。"
    assert handoff["agent_tree"]["counts"]["total"] == 2
    assert handoff["agent_tree"]["active_agents"][0]["run_id"] == "run-child-a"
    assert handoff["next_suggestion"] == "继续推进当前任务；先查看进度账本和下级状态，再决定是否补充引导或接手。"
    assert resume["handoff"]["runtime_handoff"]["recent_guidance_count"] == 1
    assert "写报告时逐个对象收口" in resume["context_block"]


def test_memory_compact_runtime_guidance_overrides_stale_progress_next_step(tmp_path: Path) -> None:
    """compact 后续接应优先最近运行中提示，避免旧进度 next_step 把任务带回旧方向。"""
    from agent_py_agent.agent.task_progress import write_task_progress

    root = tmp_path / "workspace"
    write_compact_fixture(root)
    _write_runtime_handoff_sources(root)
    write_task_progress(
        root,
        "request-compact",
        {
            "summary": "旧任务摘要",
            "next_action": "继续读取已经过期的旧目录。",
            "items": [{"id": "old", "title": "旧方向", "status": "in_progress"}],
        },
    )

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )

    work_state = json.loads(Path(result["refs"]["work_state_snapshot"]).read_text(encoding="utf-8"))

    assert work_state["next_step"].startswith("按最近运行中提示继续：")
    assert "写报告时逐个对象收口" in work_state["next_step"]
    assert "旧目录" not in work_state["next_step"]


def test_runtime_handoff_treats_scope_ids_as_literal_paths(tmp_path: Path) -> None:
    """run_id 里的 * 等字符必须按字面值处理，不能扩大扫描其它任务的状态。"""
    root = tmp_path / "workspace"
    leak_dir = root / "tasks" / "2026-06-01" / "unrelated-task" / "work" / "agents" / "run-leak"
    leak_dir.mkdir(parents=True)
    (leak_dir / "ACCEPTANCE.md").write_text("- leaked acceptance should not be imported\n", encoding="utf-8")
    (leak_dir / "canonical_state.json").write_text(
        json.dumps({"run_id": "run-leak", "status": "running"}),
        encoding="utf-8",
    )

    sources = build_work_state_field_sources(
        WorkStateFieldSourceRequest(
            plan={"workspace_root": str(root), "scope": {"request_id": "*"}},
            source_state={"task_refs": [], "content_paths": []},
        )
    )

    assert sources.acceptance["items"] == []
    assert sources.read_files == []
    assert sources.runtime_handoff == {}


def test_runtime_fact_preserves_task_output_root_from_workspace_prompt(tmp_path: Path) -> None:
    from agent_py_agent.agent.memory_archive.runtime_fact_source import (
        RuntimeFactSourceRequest,
        write_runtime_fact_source,
    )

    output_dir = tmp_path / "owners" / "local" / "main" / "tasks" / "2026-06-01" / "demo" / "output"
    work_dir = output_dir.parent / "work"
    injection = "\n".join(
        [
            "# Current Task Workspace",
            f"- output_dir: {output_dir}",
            f"- work_dir: {work_dir}",
        ]
    )

    fact_dir = write_runtime_fact_source(
        RuntimeFactSourceRequest(
            root=tmp_path,
            request_id="request-1",
            user_prompt="分析几个项目并写报告。",
            runtime_injections=(injection,),
        )
    )
    payload = json.loads((Path(fact_dir) / "task.json").read_text(encoding="utf-8"))

    assert payload["desired_outputs"][0]["artifact_id"] == "task_output_dir"
    assert payload["desired_outputs"][0]["target_path"] == str(output_dir)
    assert payload["run_intent"]["desired_outputs"]["items"] == [str(output_dir)]


def _write_runtime_handoff_sources(root: Path) -> None:
    guidance_dir = root / "workspace" / "runtime" / "workspaces" / "project-1" / "conversations" / "guidance"
    guidance_dir.mkdir(parents=True, exist_ok=True)
    (guidance_dir / "thread.session-compact.jsonl").write_text(
        json.dumps(
            {
                "guidance_id": "guidance-1",
                "target_type": "thread",
                "target_id": "session-compact",
                "message": "写报告时逐个对象收口，不要最后一次性打勾。",
                "sender": "user",
                "priority": "normal",
                "created_at": 1.0,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    for run_id, status in (("run-child-a", "running"), ("run-child-b", "completed")):
        run_dir = root / "tasks" / "2026-06-01" / "run-compact" / "work" / "agents" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "canonical_state.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "parent_run_id": "run-compact",
                    "status": status,
                    "current_tool": "read_file" if status == "running" else "",
                    "last_progress_summary": "正在读源码结构" if status == "running" else "已写完摘要",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
