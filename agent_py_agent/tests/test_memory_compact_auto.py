from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.memory_archive.compact import MemoryCompactPlanOptions
from agent_py_agent.agent.memory_archive.compact_apply import (
    MemoryCompactApplyOptions,
    apply_memory_compact,
)
from agent_py_agent.agent.memory_archive.compact_auto import (
    MemoryCompactAutoCycleOptions,
    run_memory_compact_auto_cycle,
)
from agent_py_agent.agent.memory_archive.compact_resume import (
    MemoryCompactResumeOptions,
    build_memory_compact_resume,
)
from agent_py_agent.tests.test_memory_compact import (
    _assert_apply_preserved_sources,
    _assert_schema_v2,
    _write_compact_fixture,
)


def test_memory_compact_auto_cycle_defaults_to_plan_only(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write_compact_fixture(root)

    result = run_memory_compact_auto_cycle(root, _auto_cycle_options())

    _assert_schema_v2(result, "compact_auto_cycle")
    assert result["ok"] is True
    assert result["status"] == "needs_user_confirmation"
    assert result["allow_apply"] is False
    assert result["automatic_tool_execution"] == "none"
    assert result["next_action"] == "ask_user_before_apply"
    assert result["apply_result"] == {}
    assert result["resume_result"] == {}
    assert not (root / "memory_archive" / "compact_applies").exists()


def test_memory_compact_auto_cycle_apply_stops_at_action_guard(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write_compact_fixture(root)

    result = run_memory_compact_auto_cycle(root, _auto_cycle_options(allow_apply=True))

    _assert_schema_v2(result, "compact_auto_cycle")
    assert result["ok"] is False
    assert result["status"] == "blocked_after_action_guard"
    assert result["automatic_tool_execution"] == "none"
    assert result["apply_result"]["mode"] == "apply"
    assert result["resume_result"]["action_guard"]["status"] == "blocked_missing_work_state_fields"
    assert result["allowed_to_continue"] is False
    assert result["next_action"] == "stop_and_request_review"
    assert (root / "memory_archive" / "compact_applies").exists()
    _assert_apply_preserved_sources(root)


def test_memory_compact_work_state_reads_task_fact_sources(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write_compact_fixture(root)
    _write_work_state_fact_sources(root)

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )

    work_state = json.loads(Path(result["refs"]["work_state_snapshot"]).read_text(encoding="utf-8"))
    assert work_state["missing_fields"] == []
    assert work_state["acceptance"]["items"] == ["focused compact resume tests pass"]
    assert work_state["constraints"]["items"] == ["do not touch agent_py_agent/config/agent_config.yaml"]
    assert work_state["latest_tests"]["items"] == ["python3 -m pytest -q agent_py_agent/tests/test_memory_compact.py"]
    assert work_state["source_quality"]["status"] == "complete"
    resume = build_memory_compact_resume(root, MemoryCompactResumeOptions(apply_ref=result["apply_id"]))
    _assert_schema_v2(resume["handoff"], "compact_resume_handoff")
    assert resume["handoff"]["acceptance"]["items"] == work_state["acceptance"]["items"]
    assert resume["handoff"]["constraints"]["items"] == work_state["constraints"]["items"]
    assert resume["handoff"]["latest_tests"]["items"] == work_state["latest_tests"]["items"]
    assert resume["handoff"]["action_guard"]["status"] == "requires_user_confirmation"
    assert "## Acceptance" in resume["context_block"]
    assert "focused compact resume tests pass" in resume["context_block"]


def test_memory_compact_auto_guard_allows_complete_work_state_without_running_tools(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write_compact_fixture(root)
    _write_work_state_fact_sources(root)

    resume = _auto_resume_after_apply(root)
    cycle = run_memory_compact_auto_cycle(root, _auto_cycle_options(allow_apply=True))

    assert resume["action_guard"]["ok"] is True
    assert resume["action_guard"]["status"] == "allow_automated_continue"
    assert resume["action_guard"]["allowed_to_continue"] is True
    assert resume["action_guard"]["allowed_next_action"] == "continue_after_guard"
    assert resume["action_guard"]["automatic_tool_execution"] == "none"
    assert resume["action_guard"]["missing_fields"] == []
    assert cycle["status"] == "ready_after_action_guard"
    assert cycle["allowed_to_continue"] is True
    assert cycle["automatic_tool_execution"] == "none"
    assert cycle["next_action"] == "continue_after_guard"


def test_memory_compact_resume_links_subagent_run_workspace_refs(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write_compact_fixture(root)
    _write_work_state_fact_sources(root)
    _write_subagent_run_workspace(root)

    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )
    resume = build_memory_compact_resume(
        root,
        MemoryCompactResumeOptions(
            apply_ref=result["apply_id"],
            owner_type="subagent_run",
            owner_id="run-compact",
            resume_mode="auto",
        ),
    )

    owner = resume["subagent_session_compact"]
    assert owner["status"] == "linked_run_workspace"
    assert owner["owner"] == {"owner_type": "subagent_run", "owner_id": "run-compact"}
    assert owner["memory_scope"] == "task_local"
    assert owner["writes_main_memory"] is False
    assert owner["automatic_tool_execution"] == "none"
    assert owner["refs"]["agent_run_workspace"].endswith("tasks/root-compact/agents/run-compact")
    assert owner["refs"]["agent_checkpoint"].endswith("checkpoint.json")
    assert owner["legacy_run_ref"]["legacy_task_dir"].endswith("subagents/run-compact")


def _auto_cycle_options(*, allow_apply: bool = False) -> MemoryCompactAutoCycleOptions:
    return MemoryCompactAutoCycleOptions(
        current_tokens=8000,
        max_context_tokens=10000,
        plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        allow_apply=allow_apply,
    )


def _auto_resume_after_apply(root: Path) -> dict:
    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )
    return build_memory_compact_resume(
        root,
        MemoryCompactResumeOptions(apply_ref=result["apply_id"], resume_mode="auto"),
    )


def _write_work_state_fact_sources(root: Path) -> None:
    run_dir = root / "subagents" / "run-compact"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "ACCEPTANCE.md").write_text("- focused compact resume tests pass\n", encoding="utf-8")
    (run_dir / "CONSTRAINTS.md").write_text(
        "- do not touch agent_py_agent/config/agent_config.yaml\n",
        encoding="utf-8",
    )
    (run_dir / "TEST_CHECKLIST.md").write_text(
        "- [x] python3 -m pytest -q agent_py_agent/tests/test_memory_compact.py\n",
        encoding="utf-8",
    )


def _write_subagent_run_workspace(root: Path) -> None:
    run_dir = root / "tasks" / "root-compact" / "agents" / "run-compact"
    run_dir.mkdir(parents=True, exist_ok=True)
    for directory in ("compactions", "artifacts"):
        (run_dir / directory).mkdir(exist_ok=True)
    for path, payload in {
        run_dir / "state.json": {"run_id": "run-compact", "status": "running"},
        run_dir / "checkpoint.json": {"run_id": "run-compact", "current_step": "resume"},
        run_dir / "legacy_run_ref.json": {"legacy_task_dir": str(root / "subagents" / "run-compact")},
    }.items():
        path.write_text(json.dumps(payload), encoding="utf-8")
    (run_dir / "summary.md").write_text("summary\n", encoding="utf-8")
    (run_dir / "task.md").write_text("task\n", encoding="utf-8")
    (run_dir / "timeline.jsonl").write_text("", encoding="utf-8")
    (run_dir / "findings.jsonl").write_text("", encoding="utf-8")
