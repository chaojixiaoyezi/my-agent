"""LLM: Tests for subagent dispatch: dry-run planning, apply with
runner/patch/acceptance, and transient-failure retry.

给人看的解释：
测试子代理调度：dry-run 规划、apply 执行 runner/patch/验收、临时失败重试。
"""

import json
import tempfile
import time
from pathlib import Path

from agent_py_agent.agent.agent_core.dispatch_params import DispatchParams
from agent_py_agent.agent.capabilities import CapabilityRouter
from agent_py_agent.agent.capability_config import CapabilityConfig
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.subagent import EvidencePacket, VerificationEvidence
from agent_py_agent.agent.subagents.parent_acceptance_followup_control import (
    ParentAcceptanceFollowUpControlOptions,
)

from .backends import AcceptedSubagentBackend, FlakyThenAcceptedSubagentBackend


def _make_router(agent):
    return CapabilityRouter(config=CapabilityConfig(), tool_specs=agent.tools.specs())


def _setup_review_task(agent, task, *, patch_status="applied", patch_summary="已应用"):
    """Helper to set up a task in AWAITING_ACCEPTANCE state with evidence and output JSON."""
    task.status = "AWAITING_ACCEPTANCE"
    task.verification_status = "NEEDS_ACCEPTANCE"
    task.channel_status = "OK"
    task.evidence.append(
        VerificationEvidence(kind="note", summary="有验收证据", ok=True, created_at=time.time())
    )
    task.evidence_packets.append(EvidencePacket(
        id="evpkt-dispatch",
        claim="调度任务已有验收证据",
        checked_scope="dispatch review fixture",
        evidence_refs=[task.acceptance_file],
        artifact_refs=[task.output_json],
        confidence=0.9,
        created_at=time.time(),
    ))
    agent.subagents.save(task)
    Path(task.output_json).write_text(
        json.dumps({
            "run_id": task.id,
            "tests": [{"name": "smoke", "validation_method": "file_check", "file_path": "README.md", "ok": True}],
            "patches": [{"path": "agent_py_agent/agent/demo.py", "status": patch_status, "summary": patch_summary}],
            "blockers": [],
        }, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return task


def _dispatch_runner_once(agent, router):
    return agent.dispatch_subagents(
        router,
        CapabilityConfig(),
        apply=True,
        execute_runners=True,
        max_runners=1,
        probe=False,
        reviewer="dispatch-test",
    )


# LLM: _acceptance_dispatch_record keeps dispatch test bodies below strict size limits.
# 函数用途: 从调度报告中取出指定 run 的 acceptance 记录，减少测试主体重复断言。
def _acceptance_dispatch_record(report, run_id: str):
    return next(item for item in report.records if item.step == "acceptance" and item.run_id == run_id)


# LLM: _write_output_patches keeps acceptance fixtures explicit while avoiding oversized tests.
# 函数用途: 覆盖子代理 output.json 里的 patches 列表，用于构造“没有待审核 patch”的验收场景。
def _write_output_patches(task, patches: list[dict[str, object]]) -> None:
    output_path = Path(task.output_json)
    output = json.loads(output_path.read_text(encoding="utf-8"))
    output["patches"] = patches
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")


# LLM: _assert_parent_acceptance_policy_dispatch verifies policy summaries stay refs-only and non-executing.
# 函数用途: 检查 dispatch record 的父级验收 auto-policy 摘要，确保 manual/preflight 字段没有变成执行许可。
def _assert_parent_acceptance_policy_dispatch(record, task) -> Path:
    assert record.parent_acceptance_policy_action == "run_tests"
    assert record.parent_acceptance_policy_would_execute is True
    assert record.parent_acceptance_policy_executed is False
    assert record.parent_acceptance_policy_execution_mode == "manual_only"
    assert record.parent_acceptance_policy_automatic_execution_allowed is False
    assert record.parent_acceptance_policy_recommended_command == f"subagents-tests {task.id} --re-run"
    assert record.parent_acceptance_policy_preflight_status == "manual_ready"
    assert record.parent_acceptance_policy_ready_for_automatic_execution is False
    assert record.parent_acceptance_policy_preflight_blockers == ["automatic_execution_disabled"]
    policy_ref = Path(task.reports_dir) / "parent_acceptance_auto_policy.json"
    assert record.parent_acceptance_policy_ref == str(policy_ref)
    execution_ref = Path(task.reports_dir) / "parent_acceptance_auto_execution.json"
    assert record.parent_acceptance_auto_execution_ref == str(execution_ref)
    assert record.parent_acceptance_auto_execution_status == "blocked"
    assert record.parent_acceptance_auto_execution_allowed is False
    assert record.parent_acceptance_auto_execution_executed is False
    assert record.parent_acceptance_auto_execution_guard_status == "blocked"
    assert record.parent_acceptance_auto_execution_blocked_by == [
        "automatic_execution_disabled",
        "auto_executor_dry_run_only",
    ]
    return policy_ref


# LLM: _assert_dispatch_policy_markdown verifies human dispatch output mirrors the safe machine summary.
# 函数用途: 检查 dispatch Markdown 展示 policy ref、manual-only 和 preflight 摘要，但不暗示自动执行。
def _assert_dispatch_policy_markdown(markdown: str, task, policy_ref: Path) -> None:
    assert "parent_acceptance_auto_policy" in markdown
    assert "execution_mode=manual_only" in markdown
    assert "automatic_execution_allowed=False" in markdown
    assert f"recommended_command=subagents-tests {task.id} --re-run" in markdown
    assert "preflight_status=manual_ready" in markdown
    assert "ready_for_automatic_execution=False" in markdown
    assert "preflight_blockers=automatic_execution_disabled" in markdown
    assert "parent_acceptance_auto_execution" in markdown
    assert "execution_status=blocked" in markdown
    assert "execution_allowed=False" in markdown
    assert "execution_blocked_by=automatic_execution_disabled,auto_executor_dry_run_only" in markdown
    assert str(policy_ref) in markdown


# LLM: _assert_manual_acceptance_test_execution verifies dispatch records show the post-test dry-run result.
# 函数用途: 检查显式执行父级 tests 后，dispatch acceptance 记录和 follow-up 字段都来自最新测试结果。
def _assert_manual_acceptance_test_execution(record, task, test_ref: Path, followup_ref: Path) -> None:
    assert record.action == "accept"
    assert record.ok is True
    assert record.message == "验收通过。"
    assert record.parent_acceptance_auto_execution_status == "tests_executed"
    assert record.parent_acceptance_auto_execution_allowed is True
    assert record.parent_acceptance_auto_execution_executed is True
    assert record.parent_acceptance_auto_execution_guard_status == "manual_confirmed"
    assert record.parent_acceptance_auto_execution_blocked_by == []
    assert record.parent_acceptance_auto_execution_test_ref == str(test_ref)
    assert record.parent_acceptance_auto_execution_test_failed == 0
    assert record.parent_acceptance_followup_ref == str(followup_ref)
    assert record.parent_acceptance_followup_status == "ready_for_manual_apply"
    assert record.parent_acceptance_followup_action == "apply_acceptance"
    assert record.parent_acceptance_followup_command == (
        f"subagents-acceptance-plan {task.id} --apply-followup"
    )


# LLM: _assert_acceptance_aggregate_refreshed verifies apply+tests reports use post-test facts.
# 函数用途: 检查全局 acceptance 报告和单 run 审计都显示测试后的 dry-run 结论，而不是测试前旧结论。
def _assert_acceptance_aggregate_refreshed(root: Path, task) -> None:
    report_path = root / "subs" / "subagent_acceptance_report.json"
    single_path = Path(task.reports_dir) / "acceptance_review.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    single = json.loads(single_path.read_text(encoding="utf-8"))
    assert report["dry_run"] is True
    assert report["summary"]["ACCEPT"] == 1
    assert report["summary"]["ok"] == 1
    assert report["summary"]["dry_run"] == 1
    assert report["records"][0]["run_id"] == task.id
    assert report["records"][0]["decision"] == "ACCEPT"
    assert report["records"][0]["applied"] is False
    assert report["records"][0]["message"] == "验收通过。"
    assert single["decision"] == "ACCEPT"
    assert single["applied"] is False
    assert single["message"] == "验收通过。"


# LLM: _assert_failed_acceptance_aggregate_refreshed guards the real-test failure truth source.
# 函数用途: 检查父级真实验收测试失败时，全局和单 run 审计不再保留“验收通过”的旧口径。
def _assert_failed_acceptance_aggregate_refreshed(root: Path, task) -> None:
    report_path = root / "subs" / "subagent_acceptance_report.json"
    single_path = Path(task.reports_dir) / "acceptance_review.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    single = json.loads(single_path.read_text(encoding="utf-8"))
    assert report["dry_run"] is True
    assert report["summary"]["REJECT"] == 1
    assert report["summary"]["failed"] == 1
    assert report["summary"]["dry_run"] == 1
    assert report["records"][0]["run_id"] == task.id
    assert report["records"][0]["decision"] == "REJECT"
    assert report["records"][0]["ok"] is False
    assert "父级真实验收测试失败" in report["records"][0]["message"]
    assert single["decision"] == "REJECT"
    assert single["ok"] is False
    assert "验收通过" not in single["message"]


def test_subagent_dispatch_dry_run_plans_runner_patch_and_acceptance():
    """LLM: Verifies dry-run dispatch plans runner, patch_review, and acceptance steps without mutating state."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        runner_task = agent.subagents.create_run(
            goal="调度 runner dry-run", thought="等待父代理调度。", plan=["执行 runner"],
        )
        review_task = _setup_review_task(
            agent,
            agent.subagents.create_run(
                goal="调度 patch 和验收 dry-run", thought="runner 已完成，等待审核。", plan=["审核 patch", "验收"],
            ),
            patch_status="applied", patch_summary="已应用但未审核",
        )

        report = agent.dispatch_subagents(_make_router(agent), CapabilityConfig(), apply=False, max_runners=1)
        output = json.loads(Path(review_task.output_json).read_text(encoding="utf-8"))

        assert report.dry_run
        assert any(item.step == "runner" and item.run_id == runner_task.id for item in report.records)
        assert any(item.step == "patch_review" and item.run_id == review_task.id for item in report.records)
        policy_ref = _assert_parent_acceptance_policy_dispatch(
            _acceptance_dispatch_record(report, review_task.id),
            review_task,
        )
        policy_payload = json.loads(policy_ref.read_text(encoding="utf-8"))
        assert policy_payload["policy"]["executed"] is False
        assert policy_payload["policy"]["mutates_task_state"] is False
        assert "review_status" not in output["patches"][0]
        assert agent.subagents.load(runner_task.id).status == "PLANNING"
        assert agent.subagents.load(review_task.id).status == "AWAITING_ACCEPTANCE"
        dispatch_markdown = (root / "subs" / "SUBAGENT_DISPATCH.md").read_text(encoding="utf-8")
        _assert_dispatch_policy_markdown(dispatch_markdown, review_task, policy_ref)
        assert (root / "subs" / "subagent_dispatch_report.json").exists()
        assert not (root / "subs" / "subagent_dispatch_log.jsonl").exists()


def test_subagent_dispatch_parent_scope_runs_direct_children_not_parent():
    """LLM: Verifies nested dispatch from a runner advances children instead of rerunning itself."""
    from agent_py_agent.agent.agent_core.dispatch_params import DispatchParams

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        parent = agent.subagents.create_run(
            goal="parent", thought="currently running", plan=["dispatch child"], role="coordinator",
        )
        child = agent.subagents.create_run(
            goal="child", thought="work", plan=["work"], parent_id=parent.id, root_id=parent.id, depth=1,
        )
        running_parent = agent.subagents.load(parent.id)
        running_parent.status = "RUNNING"
        agent.subagents.save(running_parent)

        report = agent.dispatch_subagents(
            _make_router(agent),
            CapabilityConfig(),
            params=DispatchParams(apply=False, max_runners=1, workflow_mode="off", parent_run_id=parent.id),
        )

        runner_records = [item for item in report.records if item.step == "runner"]
        assert [item.run_id for item in runner_records] == [child.id]
        assert parent.id not in [item.run_id for item in runner_records]


def test_subagent_dispatch_can_defer_acceptance_finalize():
    """LLM: Verifies nested dispatch can run children without immediately applying acceptance."""
    from agent_py_agent.agent.agent_core.dispatch_params import DispatchParams

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(
            goal="awaiting acceptance", thought="runner done", plan=["accept"], role="worker",
        )
        loaded = agent.subagents.load(task.id)
        loaded.status = "AWAITING_ACCEPTANCE"
        loaded.verification_status = "NEEDS_ACCEPTANCE"
        agent.subagents.save(loaded)
        Path(loaded.output_json).write_text(json.dumps({"patches": []}), encoding="utf-8")

        report = agent.dispatch_subagents(
            _make_router(agent),
            CapabilityConfig(),
            params=DispatchParams(apply=True, max_runners=0, workflow_mode="off", finalize_acceptance=False),
        )

        assert not [item for item in report.records if item.step == "acceptance"]
        assert agent.subagents.load(task.id).status == "AWAITING_ACCEPTANCE"


def test_subagent_dispatch_surfaces_leadership_recovery_plan_refs_only():
    """Dispatch should write a leadership recovery plan ref without applying hierarchy changes."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        coordinator = agent.subagents.create_run(
            goal="stale coordinator", thought="waiting", plan=["coordinate"], role="coordinator",
        )
        child = agent.subagents.create_run(
            goal="child", thought="work", plan=["work"], parent_id=coordinator.id, root_id=coordinator.id, depth=1,
        )
        stale = agent.subagents.load(coordinator.id)
        stale.status = "PLANNING"
        stale.runner_active_attempt_id = ""
        stale.heartbeat_at = time.time() - 120
        stale.updated_at = stale.heartbeat_at
        agent.subagents.save_hierarchy_links(stale)

        report = agent.dispatch_subagents(
            _make_router(agent),
            CapabilityConfig(subagent_heartbeat_timeout=1),
            apply=False,
            max_runners=0,
        )

        record = next(item for item in report.records if item.step == "leadership_recovery_plan")
        plan_ref = root / "subs" / "subagent_leadership_recovery_plan.json"
        plan_payload = json.loads(plan_ref.read_text(encoding="utf-8"))
        assert record.action == "inspect_refs"
        assert record.dry_run is True
        assert record.applied is False
        assert record.ok is True
        assert str(plan_ref) in record.evidence_paths
        assert plan_payload["summary"]["stale_coordinators"] == 1
        assert plan_payload["summary"]["unassigned_children"] == 1
        assert agent.subagents.load(child.id).parent_id == coordinator.id


def test_subagent_dispatch_manual_acceptance_test_execution_is_test_only():
    """LLM: Verifies dispatch can explicitly run parent acceptance tests without applying acceptance."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        (root / "README.md").write_text("dispatch manual acceptance test\n", encoding="utf-8")
        task = _setup_review_task(
            agent,
            agent.subagents.create_run(
                goal="调度只跑验收测试", thought="等待父代理跑 tests 但不 apply。", plan=["验收测试"],
            ),
            patch_status="none",
        )
        _write_output_patches(task, [])

        report = agent.dispatch_subagents(
            _make_router(agent),
            CapabilityConfig(),
            params=DispatchParams(max_runners=0, execute_acceptance_tests=True),
        )

        record = _acceptance_dispatch_record(report, task.id)
        test_ref = Path(task.reports_dir) / "test_execution.json"
        followup_ref = Path(task.reports_dir) / "parent_acceptance_auto_followup.json"
        payload = json.loads(test_ref.read_text(encoding="utf-8"))
        followup_payload = json.loads(followup_ref.read_text(encoding="utf-8"))
        loaded = agent.subagents.load(task.id)
        _assert_manual_acceptance_test_execution(record, task, test_ref, followup_ref)
        assert followup_payload["followup"]["status"] == "ready_for_manual_apply"
        assert payload["failed"] == 0
        assert loaded.status == "AWAITING_ACCEPTANCE"
        assert loaded.verification_status == "NEEDS_ACCEPTANCE"


def test_subagent_dispatch_apply_with_acceptance_tests_refreshes_aggregate_report():
    """LLM: Verifies apply+execute tests refreshes acceptance reports without applying task state."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        (root / "README.md").write_text("dispatch apply acceptance refresh\n", encoding="utf-8")
        task = _setup_review_task(
            agent,
            agent.subagents.create_run(
                goal="调度 apply 跑 tests 但等待 follow-up", thought="等待 tests。", plan=["tests"],
            ),
            patch_status="none",
        )
        _write_output_patches(task, [])

        report = agent.dispatch_subagents(
            _make_router(agent),
            CapabilityConfig(),
            params=DispatchParams(apply=True, max_runners=0, execute_acceptance_tests=True),
        )

        record = _acceptance_dispatch_record(report, task.id)
        loaded = agent.subagents.load(task.id)
        assert record.action == "accept"
        assert record.ok is True
        assert record.applied is False
        assert loaded.status == "AWAITING_ACCEPTANCE"
        assert loaded.verification_status == "NEEDS_ACCEPTANCE"
        _assert_acceptance_aggregate_refreshed(root, task)


# LLM: test_subagent_dispatch_failed_acceptance_tests_override_old_pass_message protects E2E handoff truth.
# 函数用途: 真实验收测试失败时，dispatch 给主代理看的 record 必须拒绝并指向 rescue，不能继续写“验收通过”。
def test_subagent_dispatch_failed_acceptance_tests_override_old_pass_message():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = _setup_review_task(
            agent,
            agent.subagents.create_run(
                goal="调度 apply 跑失败 tests 后要救援", thought="等待 tests。", plan=["tests"],
            ),
            patch_status="none",
        )
        _write_output_patches(task, [])

        report = agent.dispatch_subagents(
            _make_router(agent),
            CapabilityConfig(),
            params=DispatchParams(apply=True, max_runners=0, execute_acceptance_tests=True),
        )

        record = _acceptance_dispatch_record(report, task.id)
        test_ref = Path(task.reports_dir) / "test_execution.json"
        followup_ref = Path(task.reports_dir) / "parent_acceptance_auto_followup.json"
        payload = json.loads(test_ref.read_text(encoding="utf-8"))
        followup_payload = json.loads(followup_ref.read_text(encoding="utf-8"))
        loaded = agent.subagents.load(task.id)
        assert record.action == "reject"
        assert record.ok is False
        assert "父级真实验收测试失败" in record.message
        assert "验收通过" not in record.message
        assert record.parent_acceptance_auto_execution_test_failed == 1
        assert record.parent_acceptance_followup_status == "needs_manual_rescue"
        assert record.parent_acceptance_followup_action == "plan_rescue"
        assert followup_payload["followup"]["status"] == "needs_manual_rescue"
        assert followup_payload["followup"]["action"] == "plan_rescue"
        assert payload["failed"] == 1
        assert loaded.status == "AWAITING_ACCEPTANCE"
        assert loaded.verification_status == "NEEDS_ACCEPTANCE"
        _assert_failed_acceptance_aggregate_refreshed(root, task)


def test_subagent_dispatch_to_followup_apply_acceptance_chain():
    """LLM: Verifies dispatch tests can hand off to explicit follow-up apply."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        (root / "README.md").write_text("dispatch followup acceptance chain\n", encoding="utf-8")
        task = _setup_review_task(
            agent,
            agent.subagents.create_run(
                goal="调度后显式 follow-up apply", thought="等待测试和人工 apply。", plan=["tests", "apply"],
            ),
            patch_status="none",
        )
        _write_output_patches(task, [])

        agent.dispatch_subagents(
            _make_router(agent),
            CapabilityConfig(),
            params=DispatchParams(max_runners=0, execute_acceptance_tests=True),
        )
        result = agent.subagents.apply_parent_acceptance_followup(
            task.id,
            options=ParentAcceptanceFollowUpControlOptions(apply=True, reviewer="dispatch-followup"),
        )

        loaded = agent.subagents.load(task.id)
        assert result.status == "applied_acceptance"
        assert result.applied is True
        assert loaded.status == "DONE"
        assert loaded.verification_status == "VERIFIED"


def test_subagent_dispatch_apply_reviews_patch_then_accepts():
    """LLM: Verifies apply dispatch reviews patch and then accepts the run."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = _setup_review_task(
            agent,
            agent.subagents.create_run(
                goal="调度 patch 审核后验收", thought="runner 已完成，等待调度器收口。", plan=["审核 patch", "验收"],
            ),
        )
        Path(task.runner_result_json).write_text(
            json.dumps({
                "run_id": task.id, "structured_output_found": True,
                "structured_output_ok": True, "structured_parse_error": "",
            }, ensure_ascii=False, indent=2), encoding="utf-8",
        )

        report = agent.dispatch_subagents(
            _make_router(agent), CapabilityConfig(), apply=True, max_runners=0, reviewer="dispatch-test",
        )
        loaded = agent.subagents.load(task.id)
        output = json.loads(Path(task.output_json).read_text(encoding="utf-8"))

        assert not report.dry_run
        assert any(item.step == "patch_review" and item.ok for item in report.records)
        assert any(item.step == "acceptance" and item.ok for item in report.records)
        assert output["patches"][0]["review_status"] == "APPROVED"
        assert loaded.status == "DONE"
        assert loaded.verification_status == "VERIFIED"
        assert (root / "subs" / "DISPATCH_LOG.md").exists()


def test_subagent_dispatch_apply_executes_runner_and_accepts(monkeypatch):
    """LLM: Verifies apply dispatch executes a real runner and then accepts."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        backend = AcceptedSubagentBackend()
        monkeypatch.setattr("agent_py_agent.agent.core.get_backend", lambda _name, _config: backend)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(
            goal="调度器执行 runner",
            thought="等待 dispatch 调用真实 runner 路径。",
            plan=["执行", "验收"],
        )

        router = CapabilityRouter(config=CapabilityConfig(), tool_specs=agent.tools.specs())
        report = agent.dispatch_subagents(
            router,
            CapabilityConfig(),
            apply=True,
            execute_runners=True,
            max_runners=1,
            probe=False,
            reviewer="dispatch-test",
        )
        loaded = agent.subagents.load(task.id)

        assert any(item.step == "runner" and item.applied and item.ok for item in report.records)
        assert any(item.step == "acceptance" and item.applied and item.ok for item in report.records)
        assert loaded.status == "DONE"
        assert loaded.verification_status == "VERIFIED"
        assert loaded.evidence[0].summary == "调度器结构化执行证据"


def test_subagent_dispatch_retries_transient_runner_failure(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        backend = FlakyThenAcceptedSubagentBackend()
        monkeypatch.setattr("agent_py_agent.agent.core.get_backend", lambda _name, _config: backend)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(
            goal="调度器重试临时 runner 失败",
            thought="第一次模型调用失败后，下一轮 dispatch 应该自动重试。",
            plan=["第一次失败", "第二次重试", "验收"],
        )
        router = CapabilityRouter(config=CapabilityConfig(), tool_specs=agent.tools.specs())

        first = _dispatch_runner_once(agent, router)
        after_first = agent.subagents.load(task.id)

        assert any(item.step == "runner" and item.action == "execute_runner" and not item.ok for item in first.records)
        assert any(item.step == "runner" and item.action == "retry_runner" and item.ok for item in first.records)
        assert any(item.step == "acceptance" and item.applied and item.ok for item in first.records)
        assert backend.calls == 3
        assert after_first.status == "DONE"
        assert after_first.verification_status == "VERIFIED"
        assert after_first.runner_attempts == 2
        assert after_first.runner_last_error == ""
