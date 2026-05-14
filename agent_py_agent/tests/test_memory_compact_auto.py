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
from agent_py_agent.agent.memory_archive.compact_work_state_sources import (
    WorkStateFieldSourceRequest,
    build_work_state_field_sources,
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


def test_memory_compact_work_state_treats_scope_ids_as_literal_paths(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    leak_dir = root / "tasks" / "unrelated-task" / "agents" / "run-leak"
    leak_dir.mkdir(parents=True)
    (leak_dir / "ACCEPTANCE.md").write_text("- leaked acceptance should not be imported\n", encoding="utf-8")

    sources = build_work_state_field_sources(
        WorkStateFieldSourceRequest(
            plan={"workspace_root": str(root), "scope": {"request_id": "*"}},
            source_state={"task_refs": [], "content_paths": []},
        )
    )

    assert sources.acceptance["items"] == []
    assert sources.read_files == []


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
    assert resume["continue_packet"]["ready_to_continue"] is True
    assert resume["continue_packet"]["continue_mode"] == "automated_guarded"
    assert resume["continue_packet"]["guard"]["allowed_next_action"] == "continue_after_guard"
    assert cycle["status"] == "ready_after_action_guard"
    assert cycle["allowed_to_continue"] is True
    assert cycle["automatic_tool_execution"] == "none"
    assert cycle["next_action"] == "continue_after_guard"
    assert cycle["continue_packet"]["ready_to_continue"] is True
    assert cycle["apply_id"]


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
    assert owner["reserved_hooks"]["enabled"] is False
    assert owner["reserved_hooks"]["writes_main_memory"] is False
    assert owner["reserved_hooks"]["automatic_tool_execution"] == "none"
    assert Path(owner["refs"]["agent_run_workspace"]).parts[-4:] == (
        "tasks",
        "root-compact",
        "agents",
        "run-compact",
    )
    assert owner["refs"]["agent_checkpoint"].endswith("checkpoint.json")
    assert Path(owner["legacy_run_ref"]["legacy_task_dir"]).parts[-2:] == ("subagents", "run-compact")


# LLM: parent resume status needs to see whether a subagent has a task-local continue packet ready.
# 函数用途: 验证 compact resume 会把子代理 run workspace 的 latest_continue_packet 作为只读引用暴露给父级。
def test_memory_compact_resume_exposes_subagent_latest_continue_packet(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    _write_compact_fixture(root)
    _write_work_state_fact_sources(root)
    _write_subagent_run_workspace(root)
    packet = (
        root
        / "tasks"
        / "root-compact"
        / "agents"
        / "run-compact"
        / "compactions"
        / "session"
        / "latest_continue_packet.json"
    )
    packet.parent.mkdir(parents=True, exist_ok=True)
    packet.write_text(
        json.dumps({"ready_to_continue": True, "next_action": "resume subagent locally"}),
        encoding="utf-8",
    )

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
    assert owner["refs"]["latest_continue_packet"].endswith("latest_continue_packet.json")
    assert owner["reserved_hooks"]["enabled"] is True
    assert owner["reserved_hooks"]["continue_packet_ref"].endswith("latest_continue_packet.json")
    assert owner["reserved_hooks"]["writes_main_memory"] is False


# LLM: Real E2E stores legacy subagent work orders in the configured subagent workspace, not always root/subagents.
# 函数用途: 验证 compact resume 能通过配置的 subagent_workspace 找到真实子代理 run workspace 和继续包。
def test_memory_compact_resume_uses_configured_subagent_workspace_refs(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    configured_subagents = root / "_runtime" / "subagents"
    _write_compact_fixture(root)
    _write_work_state_fact_sources(root)
    _write_subagent_run_workspace_in_configured_root(configured_subagents, "run-configured")
    _write_configured_continue_packet(configured_subagents, "run-configured")
    resume = _configured_subagent_resume(root, configured_subagents)

    _assert_configured_subagent_owner_resume(resume, configured_subagents)


# LLM: _assert_configured_subagent_owner_resume keeps the E2E-shaped regression compact and readable.
# 函数用途: 校验 configured subagent workspace 被解析成 task-local owner refs 和推荐读取路径。
def _assert_configured_subagent_owner_resume(resume: dict, configured_subagents: Path) -> None:
    owner = resume["subagent_session_compact"]
    assert owner["status"] == "linked_run_workspace"
    assert owner["refs"]["legacy_task_dir"] == str(configured_subagents / "run-configured")
    assert owner["refs"]["agent_run_workspace"].endswith("tasks/root-configured/agents/run-configured")
    assert owner["reserved_hooks"]["continue_packet_ready"] is True
    subagent_packet = resume["continue_packet"]["subagent"]
    assert subagent_packet["recommended_read_paths"] == [
        owner["refs"]["latest_continue_packet"],
        owner["refs"]["agent_checkpoint"],
        owner["refs"]["agent_summary"],
        owner["refs"]["agent_task"],
        owner["refs"]["agent_timeline"],
        owner["refs"]["agent_findings"],
    ]
    assert all(str(configured_subagents) in path for path in subagent_packet["recommended_read_paths"])


# LLM: _configured_subagent_resume creates one compact apply then resumes it with an explicit subagent root.
# 函数用途: 复用标准 compact apply 流程，返回指定 subagent_run owner 的 resume payload。
def _configured_subagent_resume(root: Path, configured_subagents: Path) -> dict:
    result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(session_id="session-compact", request_id="request-compact"),
        ),
    )
    return build_memory_compact_resume(
        root,
        MemoryCompactResumeOptions(
            apply_ref=result["apply_id"],
            owner_type="subagent_run",
            owner_id="run-configured",
            resume_mode="auto",
            subagent_workspace=configured_subagents,
        ),
    )


# LLM: _write_configured_continue_packet simulates the task-local packet produced by a real subagent save.
# 函数用途: 在配置 subagent workspace 的 agent-run compactions 目录写 latest_continue_packet.json。
def _write_configured_continue_packet(configured_subagents: Path, run_id: str) -> None:
    packet = (
        configured_subagents
        / "tasks"
        / "root-configured"
        / "agents"
        / run_id
        / "compactions"
        / "session"
        / "latest_continue_packet.json"
    )
    packet.parent.mkdir(parents=True, exist_ok=True)
    packet.write_text(json.dumps({"ready_to_continue": True}), encoding="utf-8")


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


def _write_subagent_run_workspace_in_configured_root(subagents_root: Path, run_id: str) -> None:
    legacy_dir = subagents_root / run_id
    run_dir = subagents_root / "tasks" / "root-configured" / "agents" / run_id
    legacy_dir.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    for directory in ("compactions", "artifacts"):
        (run_dir / directory).mkdir(exist_ok=True)
    for path, payload in {
        run_dir / "state.json": {"run_id": run_id, "status": "running"},
        run_dir / "checkpoint.json": {"run_id": run_id, "current_step": "resume"},
        run_dir / "legacy_run_ref.json": {"legacy_task_dir": str(legacy_dir)},
    }.items():
        path.write_text(json.dumps(payload), encoding="utf-8")
    (run_dir / "summary.md").write_text("summary\n", encoding="utf-8")
    (run_dir / "task.md").write_text("task\n", encoding="utf-8")
    (run_dir / "timeline.jsonl").write_text("", encoding="utf-8")
    (run_dir / "findings.jsonl").write_text("", encoding="utf-8")
    (legacy_dir / "task.json").write_text(
        json.dumps(
            {
                "id": run_id,
                "agent_run_workspace_dir": str(run_dir),
                "agent_run_checkpoint_json": str(run_dir / "checkpoint.json"),
                "agent_run_compactions_dir": str(run_dir / "compactions"),
            }
        ),
        encoding="utf-8",
    )
