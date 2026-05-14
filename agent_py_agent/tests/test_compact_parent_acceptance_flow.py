from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.memory_archive import (
    CompressionSnapshot,
    RawMemoryEvent,
    append_raw_event,
    append_snapshot,
    write_compression_snapshot_file,
)
from agent_py_agent.agent.memory_archive.compact import MemoryCompactPlanOptions
from agent_py_agent.agent.memory_archive.compact_apply import (
    MemoryCompactApplyOptions,
    apply_memory_compact,
)
from agent_py_agent.agent.memory_archive.compact_resume import (
    MemoryCompactResumeOptions,
    build_memory_compact_resume,
)
from agent_py_agent.agent.subagent import (
    AcceptanceReviewOptions,
    EvidencePacket,
    VerificationEvidence,
)


# LLM: test_compact_resume_and_parent_acceptance_auto_policy_stay_refs_only protects the combined flow boundary.
# 函数用途: 验证 compact resume、子代理 owner refs 和父级验收 auto policy 可以串联，但不会自动执行或改任务状态。
def test_compact_resume_and_parent_acceptance_auto_policy_stay_refs_only(tmp_path: Path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subagents"), tmp_path)
    task = _awaiting_acceptance_task(agent)
    _write_compact_state_files(task)
    _write_compact_archives(tmp_path, task.id)

    apply_result = apply_memory_compact(
        tmp_path,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(
                session_id="session-compact-acceptance",
                request_id="request-compact-acceptance",
                run_id=task.id,
                task_id=task.id,
            ),
        ),
    )
    resume = build_memory_compact_resume(
        tmp_path,
        MemoryCompactResumeOptions(
            apply_ref=apply_result["apply_id"],
            owner_type="subagent_run",
            owner_id=task.id,
            resume_mode="auto",
        ),
    )
    _write_parent_acceptance_output(task)
    agent.subagents.apply_parent_acceptance_decision(task.id, reviewer="parent")
    policy = agent.subagents.plan_parent_acceptance_auto_policy(task.id)

    reloaded = agent.subagents.load(task.id)
    assert resume["continue_packet"]["ready_to_continue"] is True
    assert resume["continue_packet"]["subagent"]["writes_main_memory"] is False
    assert resume["continue_packet"]["subagent"]["reserved_hooks"]["enabled"] is True
    assert resume["continue_packet"]["subagent"]["reserved_hooks"]["writes_main_memory"] is False
    assert resume["continue_packet"]["automatic_tool_execution"] == "none"
    assert policy.decision == "allow"
    assert policy.action == "run_tests"
    assert policy.would_execute is True
    assert policy.executed is False
    assert policy.mutates_task_state is False
    assert reloaded.status == "AWAITING_ACCEPTANCE"
    assert reloaded.verification_status == "NEEDS_ACCEPTANCE"


# LLM: test_compact_resume_parent_acceptance_executes_real_tests_then_applies covers the business acceptance handoff.
# 函数用途: 串联 compact auto resume、父级 dry-run、显式真实测试执行、inspect-only apply，确保流程不自动越权。
def test_compact_resume_parent_acceptance_executes_real_tests_then_applies(tmp_path: Path) -> None:
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subagents"), tmp_path)
    task = _awaiting_acceptance_task(agent)
    _write_compact_state_files(task)
    _write_compact_archives(tmp_path, task.id)
    _write_parent_acceptance_evidence(agent, task)
    (tmp_path / "business-output.txt").write_text("ready\n", encoding="utf-8")

    resume = _auto_resume_compact_acceptance(tmp_path, task.id)
    _write_parent_acceptance_output(
        task,
        tests=[{
            "name": "business artifact exists",
            "validation_method": "file_check",
            "file_path": "business-output.txt",
        }],
    )

    result = _run_parent_acceptance_real_test_cycle(agent, task.id)

    reloaded = agent.subagents.load(task.id)
    assert resume["continue_packet"]["ready_to_continue"] is True
    assert result["first_decision"].decision == "execute_tests"
    assert result["blocked_apply"].applied is False
    assert result["report"].records[0].decision == "ACCEPT"
    assert any(item.name == "test_execution_passed" and item.ok for item in result["report"].records[0].findings)
    assert result["second_decision"].decision == "inspect_only"
    assert result["applied"].applied is True
    assert result["applied"].acceptance_decision == "ACCEPT"
    assert reloaded.status == "DONE"
    assert reloaded.verification_status == "VERIFIED"
    exports_path = Path(reloaded.agent_run_memory_exports_jsonl)
    assert not exports_path.exists() or exports_path.read_text(encoding="utf-8") == ""


# LLM: _awaiting_acceptance_task creates a real saved task so workspace adapters and parent acceptance share refs.
# 函数用途: 建立等待父级验收的子代理任务，并触发 task/run workspace 兼容写入。
def _awaiting_acceptance_task(agent: SimpleAgent):
    task = agent.subagents.create_run(
        goal="compact resume parent acceptance flow",
        thought="worker finished and waits for parent tests",
        plan=["write output", "request parent acceptance"],
        acceptance_checks=["unit tests must be explicitly reviewed"],
    )
    task.status = "AWAITING_ACCEPTANCE"
    task.verification_status = "NEEDS_ACCEPTANCE"
    task.channel_status = "OK"
    agent.subagents.save(task)
    return task


# LLM: _auto_resume_compact_acceptance keeps the integration test below code-size guard while preserving scope.
# 函数用途: 对同一 run scope 执行非破坏性 compact apply/resume，并返回 auto continue packet。
def _auto_resume_compact_acceptance(root: Path, run_id: str) -> dict[str, object]:
    apply_result = apply_memory_compact(
        root,
        MemoryCompactApplyOptions(
            plan_options=MemoryCompactPlanOptions(
                session_id="session-compact-acceptance",
                request_id="request-compact-acceptance",
                run_id=run_id,
                task_id=run_id,
            ),
        ),
    )
    return build_memory_compact_resume(
        root,
        MemoryCompactResumeOptions(apply_ref=apply_result["apply_id"], resume_mode="auto"),
    )


# LLM: _run_parent_acceptance_real_test_cycle models explicit parent actions after compact resume.
# 函数用途: 依次执行父级 dry-run、拦截 apply、显式真实测试、inspect-only apply，并返回各阶段结果。
def _run_parent_acceptance_real_test_cycle(agent: SimpleAgent, run_id: str) -> dict[str, object]:
    first_decision = agent.subagents.plan_parent_acceptance(run_id)
    blocked_apply = agent.subagents.apply_parent_acceptance_decision(run_id, reviewer="parent")
    report = agent.subagents.write_acceptance_review_report(
        run_ids=[run_id],
        options=AcceptanceReviewOptions(execute_tests=True, test_timeout_seconds=10),
        reviewer="parent-tests",
    )
    second_decision = agent.subagents.plan_parent_acceptance(run_id)
    applied = agent.subagents.apply_parent_acceptance_decision(run_id, reviewer="parent")
    return {
        "first_decision": first_decision,
        "blocked_apply": blocked_apply,
        "report": report,
        "second_decision": second_decision,
        "applied": applied,
    }


# LLM: _write_parent_acceptance_evidence gives normal acceptance enough verifier evidence after real tests pass.
# 函数用途: 写入一条可追踪 evidence packet，避免父级验收只依赖 worker output 自述。
def _write_parent_acceptance_evidence(agent: SimpleAgent, task) -> None:
    evidence_path = Path(task.reports_dir) / "business-evidence.md"
    evidence_path.write_text("business output is present\n", encoding="utf-8")
    task.evidence.append(
        VerificationEvidence(
            kind="file_check",
            summary="business output exists",
            path="business-output.txt",
            ok=True,
        )
    )
    task.evidence_packets.append(
        EvidencePacket(
            id="packet-business-output",
            claim="business output exists",
            checked_scope="business-output.txt",
            evidence_refs=[str(evidence_path)],
            artifact_refs=["business-output.txt"],
            confidence=0.9,
        )
    )
    agent.subagents.save(task)


# LLM: _write_compact_state_files supplies explicit work-state facts for action guard auto mode.
# 函数用途: 写入验收、约束和测试事实源，让 compact apply 不需要猜测工作状态。
def _write_compact_state_files(task) -> None:
    task_dir = Path(task.task_dir)
    (task_dir / "CONSTRAINTS.md").write_text("- do not run acceptance automatically\n", encoding="utf-8")
    (task_dir / "TEST_CHECKLIST.md").write_text("- [x] parent acceptance focused tests planned\n", encoding="utf-8")


# LLM: _write_compact_archives creates the minimal raw/snapshot refs needed by non-destructive compact apply.
# 函数用途: 写入一组和子代理 run 绑定的 archive/snapshot，让 resume 能回到 task fact source。
def _write_compact_archives(root: Path, run_id: str) -> None:
    append_raw_event(
        root,
        RawMemoryEvent(
            event_id="raw-compact-acceptance",
            session_id="session-compact-acceptance",
            request_id="request-compact-acceptance",
            run_id=run_id,
            task_id=run_id,
            speaker="user",
            target="assistant",
            action="message",
            status="ok",
            content_preview="继续 compact/subagent acceptance flow",
            source="subagent_run",
            archive_level=2,
            created_at="2026-05-08T12:00:00+00:00",
        ),
    )
    snapshot = CompressionSnapshot(
        snapshot_id="snapshot-compact-acceptance",
        session_id="session-compact-acceptance",
        compression_id="compression-compact-acceptance",
        turn_range={"start": 1, "end": 1, "request_id": "request-compact-acceptance", "run_id": run_id, "task_id": run_id},
        user_intents=["继续 compact/subagent acceptance flow"],
        assistant_actions=["准备恢复后交给父级验收策略 dry-run。"],
        dispatch_events=[{"source": "subagent_run", "request_id": "request-compact-acceptance", "run_id": run_id, "task_id": run_id, "status": "ok"}],
        task_refs=[run_id],
        next_actions=["run parent acceptance auto-policy dry-run"],
        archive_level=2,
        created_at="2026-05-08T12:01:00+00:00",
    )
    append_snapshot(root, snapshot)
    write_compression_snapshot_file(root, snapshot)


# LLM: _write_parent_acceptance_output makes parent policy recommend run_tests without executing them.
# 函数用途: 写入 worker output 和 runner parse record，供父级验收 controller 产生 run_tests 建议。
def _write_parent_acceptance_output(task, *, tests: list[dict[str, object]] | None = None) -> None:
    output = {
        "run_id": task.id,
        "status": "AWAITING_ACCEPTANCE",
        "summary": "worker says tests are ready",
        "tests": tests or [{"name": "unit", "validation_method": "command", "command": "python -m pytest -q"}],
        "artifacts": [],
        "patches": [],
        "blockers": [],
    }
    runner = {
        "run_id": task.id,
        "structured_output_found": True,
        "structured_output_ok": True,
        "structured_parse_error": "",
    }
    Path(task.output_json).write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(task.runner_result_json).write_text(json.dumps(runner, ensure_ascii=False, indent=2), encoding="utf-8")
