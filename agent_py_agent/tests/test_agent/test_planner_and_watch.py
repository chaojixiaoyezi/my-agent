"""LLM: Tests for parent-planner dispatch and watch: planner parsing, gate
enforcement, zero-limit context, watch cycle, and lock prevention.

给人看的解释：
测试父代理 planner 调度和 watch：planner 解析与门控、零限制上下文、
watch 周期、锁防重入。
"""

import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

from agent_py_agent.agent.agent_core.dispatch_params import WatchParams
from agent_py_agent.agent.capabilities import CapabilityRouter
from agent_py_agent.agent.capability_config import CapabilityConfig
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.subagent import parse_parent_planner_output

from .backends import ParentPlannerBackend


def test_parent_planner_parser_reads_structured_result():
    """LLM: Verifies parse_parent_planner_output extracts decision, actions, risks, etc."""
    parsed = parse_parent_planner_output(
        "[PARENT_PLANNER_RESULT]\n"
        "{\n"
        '  "decision": "DISPATCH",\n'
        '  "summary": "需要继续推进。",\n'
        '  "should_dispatch": true,\n'
        '  "runner_instruction": "补充证据。",\n'
        '  "suggested_max_runners": 1,\n'
        '  "actions": [{"action": "execute_runner", "run_id": "r1"}],\n'
        '  "blockers": [],\n'
        '  "risks": ["api_budget"],\n'
        '  "notes": ["ok"]\n'
        "}\n"
        "[/PARENT_PLANNER_RESULT]"
    )

    assert parsed.found
    assert parsed.ok
    assert parsed.decision == "DISPATCH"
    assert parsed.runner_instruction == "补充证据。"
    assert parsed.actions[0]["action"] == "execute_runner"
    assert parsed.risks == ["api_budget"]


def test_subagent_dispatch_parent_planner_runs_when_gate_has_work():
    """LLM: Verifies parent planner runs dispatch when there are active tasks in the gate."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        backend = ParentPlannerBackend()
        agent.backend = backend
        agent.subagents.create_run(
            goal="planner 需要看到的 active task",
            thought="等待父代理 planner 判断。",
            plan=["planner", "dispatch"],
        )
        router = CapabilityRouter(config=CapabilityConfig(), tool_specs=agent.tools.specs())

        report = agent.dispatch_subagents(
            router,
            CapabilityConfig(),
            apply=True,
            planner=True,
            execute_runners=False,
            max_runners=1,
        )

        assert len(backend.prompts) == 1
        assert any(item.step == "parent_planner" and item.ok for item in report.records)
        assert any(item.step == "runner" and item.action == "runner_dry_run" for item in report.records)
        planner_record = next(item for item in report.records if item.step == "parent_planner")
        assert planner_record.action == "dispatch"
        assert (root / "subs" / "parent_planner_report.json").exists()
        assert (root / "subs" / "PARENT_PLANNER.md").exists()
        assert (root / "subs" / "PARENT_PLANNER_LOG.md").exists()


def test_subagent_dispatch_parent_planner_blocks_empty_heartbeat_ok_when_gate_has_work():
    """LLM: Verifies parent planner blocks HEARTBEAT_OK when there is active work."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        backend = ParentPlannerBackend(decision="HEARTBEAT_OK")
        agent.backend = backend
        agent.subagents.create_run(
            goal="不能空心 OK 的 active task",
            thought="需要父代理继续推进。",
            plan=["dispatch"],
        )
        router = CapabilityRouter(config=CapabilityConfig(), tool_specs=agent.tools.specs())

        report = agent.dispatch_subagents(
            router,
            CapabilityConfig(),
            apply=False,
            planner=True,
            max_runners=0,
        )

        planner_record = next(item for item in report.records if item.step == "parent_planner")
        assert len(backend.prompts) == 1
        assert not planner_record.ok
        assert planner_record.action == "heartbeat_ok"
        assert "禁止" in planner_record.message


def test_subagent_dispatch_parent_planner_zero_limit_means_unlimited_context():
    """LLM: Verifies limit=0 passes all active tasks to planner prompt (unlimited context)."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        backend = ParentPlannerBackend()
        agent.backend = backend
        agent.subagents.create_run(
            goal="zero limit active task should appear in planner prompt",
            thought="验证 limit=0 不会把 planner 上下文切空。",
            plan=["dispatch"],
        )
        router = CapabilityRouter(config=CapabilityConfig(), tool_specs=agent.tools.specs())

        report = agent.dispatch_subagents(
            router,
            CapabilityConfig(),
            apply=False,
            planner=True,
            max_runners=0,
            limit=0,
        )

        assert len(backend.prompts) == 1
        assert "zero limit active task should appear in planner prompt" in backend.prompts[0]
        assert any(item.step == "parent_planner" and item.ok for item in report.records)


def test_subagent_dispatch_watch_runs_one_cycle_and_releases_lock():
    """LLM: Verifies watch runs one dispatch cycle and releases the lock file."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        agent.subagents.create_run(
            goal="watch 调度 dry-run",
            thought="等待 watch 调度一轮。",
            plan=["dispatch"],
        )
        router = CapabilityRouter(config=CapabilityConfig(), tool_specs=agent.tools.specs())

        report = agent.watch_subagents(
            router,
            CapabilityConfig(),
            apply=False,
            max_cycles=1,
            interval=0,
            max_runners=1,
        )
        workspace = root / "subs"

        assert report.summary["total"] == 1
        assert report.records[0].ok
        assert report.records[0].dispatch_record_count >= 1
        assert (workspace / "subagent_dispatch_watch_report.json").exists()
        assert (workspace / "SUBAGENT_DISPATCH_WATCH.md").exists()
        assert (workspace / "subagent_dispatch_watch_heartbeat.json").exists()
        assert (workspace / "DISPATCH_WATCH_LOG.md").exists()
        assert not (workspace / "subagent_dispatch_watch.lock").exists()


# LLM: Builds a watch fixture that is ready for parent acceptance policy planning.
# 函数用途: 创建等待父级验收的子代理任务，并写入安全的 file_check 测试事实。
def _setup_watch_acceptance_policy_task(agent):
    task = agent.subagents.create_run(
        goal="watch parent acceptance policy",
        thought="worker finished and needs parent tests",
        plan=["wait for parent acceptance"],
    )
    task.status = "AWAITING_ACCEPTANCE"
    task.verification_status = "NEEDS_ACCEPTANCE"
    task.channel_status = "OK"
    agent.subagents.save(task)
    Path(task.output_json).write_text(
        json.dumps({
            "run_id": task.id,
            "status": "AWAITING_ACCEPTANCE",
            "tests": [{"name": "smoke", "validation_method": "file_check", "file_path": "README.md"}],
            "artifacts": [],
            "patches": [],
            "blockers": [],
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return task


# LLM: Reads the acceptance dispatch record from the watch-triggered dispatch report.
# 函数用途: 从 dispatch JSON 报告里取出 acceptance 记录，方便断言 policy 摘要字段。
def _watch_acceptance_dispatch_record(root: Path) -> dict:
    dispatch_payload = json.loads((root / "subs" / "subagent_dispatch_report.json").read_text(encoding="utf-8"))
    return next(item for item in dispatch_payload["records"] if item["step"] == "acceptance")


# LLM: _assert_watch_auto_execution_summary keeps watch tests below size guard limits.
# 函数用途: 检查 watch 透传 auto-execution hard guard 摘要，确认它不是自动执行许可。
def _assert_watch_auto_execution_summary(record: dict, task) -> None:
    execution_ref = Path(task.reports_dir) / "parent_acceptance_auto_execution.json"
    assert record["parent_acceptance_auto_execution_ref"] == str(execution_ref)
    assert record["parent_acceptance_auto_execution_status"] == "blocked"
    assert record["parent_acceptance_auto_execution_allowed"] is False
    assert record["parent_acceptance_auto_execution_executed"] is False
    assert record["parent_acceptance_auto_execution_guard_status"] == "blocked"
    assert record["parent_acceptance_auto_execution_blocked_by"] == [
        "automatic_execution_disabled",
        "auto_executor_dry_run_only",
    ]


# LLM: _assert_watch_auto_policy_summary verifies policy summaries remain advisory in watch reports.
# 函数用途: 检查 watch 透传 auto-policy 摘要，避免把 manual_ready 误当自动执行许可。
def _assert_watch_auto_policy_summary(record: dict, task, policy_ref: Path) -> None:
    assert record["parent_acceptance_policy_ref"] == str(policy_ref)
    assert record["parent_acceptance_policy_action"] == "run_tests"
    assert record["parent_acceptance_policy_would_execute"] is True
    assert record["parent_acceptance_policy_executed"] is False
    assert record["parent_acceptance_policy_execution_mode"] == "manual_only"
    assert record["parent_acceptance_policy_automatic_execution_allowed"] is False
    assert record["parent_acceptance_policy_recommended_command"] == f"subagents-tests {task.id} --re-run"
    assert record["parent_acceptance_policy_preflight_status"] == "manual_ready"
    assert record["parent_acceptance_policy_ready_for_automatic_execution"] is False
    assert record["parent_acceptance_policy_preflight_blockers"] == ["automatic_execution_disabled"]


def test_subagent_dispatch_watch_surfaces_parent_acceptance_auto_policy_refs():
    """LLM: Verifies watch sees parent acceptance auto-policy dry-run refs without executing them."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = _setup_watch_acceptance_policy_task(agent)
        router = CapabilityRouter(config=CapabilityConfig(), tool_specs=agent.tools.specs())

        report = agent.watch_subagents(
            router,
            CapabilityConfig(),
            apply=False,
            max_cycles=1,
            interval=0,
            max_runners=0,
        )

        watch_record = report.records[0]
        policy_ref = Path(task.reports_dir) / "parent_acceptance_auto_policy.json"
        payload = json.loads(policy_ref.read_text(encoding="utf-8"))
        reloaded = agent.subagents.load(task.id)
        assert watch_record.evidence_paths == [
            str(root / "subs" / "subagent_dispatch_report.json"),
            str(root / "subs" / "SUBAGENT_DISPATCH.md"),
        ]
        assert watch_record.dispatch_summary["acceptance"] >= 1
        acceptance_record = _watch_acceptance_dispatch_record(root)
        _assert_watch_auto_policy_summary(acceptance_record, task, policy_ref)
        _assert_watch_auto_execution_summary(acceptance_record, task)
        assert payload["policy"]["would_execute"] is True
        assert payload["policy"]["executed"] is False
        assert payload["reserved"]["mutates_task_state"] is False
        assert reloaded.status == "AWAITING_ACCEPTANCE"
        assert reloaded.verification_status == "NEEDS_ACCEPTANCE"


def test_subagent_dispatch_watch_executes_acceptance_tests_when_confirmed():
    """LLM: Verifies watch forwards explicit acceptance test execution without applying the task."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        (root / "README.md").write_text("watch manual acceptance test\n", encoding="utf-8")
        task = _setup_watch_acceptance_policy_task(agent)
        router = CapabilityRouter(config=CapabilityConfig(), tool_specs=agent.tools.specs())

        agent.watch_subagents(
            router,
            CapabilityConfig(),
            params=WatchParams(max_cycles=1, interval=0, max_runners=0, execute_acceptance_tests=True),
        )

        record = _watch_acceptance_dispatch_record(root)
        test_ref = Path(task.reports_dir) / "test_execution.json"
        followup_ref = Path(task.reports_dir) / "parent_acceptance_auto_followup.json"
        loaded = agent.subagents.load(task.id)
        assert record["parent_acceptance_auto_execution_status"] == "tests_executed"
        assert record["parent_acceptance_auto_execution_allowed"] is True
        assert record["parent_acceptance_auto_execution_executed"] is True
        assert record["parent_acceptance_auto_execution_test_ref"] == str(test_ref)
        assert record["parent_acceptance_followup_ref"] == str(followup_ref)
        assert record["parent_acceptance_followup_status"] == "ready_for_manual_apply"
        assert record["parent_acceptance_followup_action"] == "apply_acceptance"
        assert json.loads(test_ref.read_text(encoding="utf-8"))["failed"] == 0
        assert loaded.status == "AWAITING_ACCEPTANCE"
        assert loaded.verification_status == "NEEDS_ACCEPTANCE"


def test_subagent_dispatch_watch_lock_prevents_second_parent():
    """LLM: Verifies watch raises RuntimeError when a lock file already exists."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        router = CapabilityRouter(config=CapabilityConfig(), tool_specs=agent.tools.specs())
        workspace = root / "subs"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "subagent_dispatch_watch.lock").write_text(
            json.dumps({"token": "other", "pid": os.getpid(), "created_at": time.time()}),
            encoding="utf-8",
        )

        try:
            agent.watch_subagents(
                router,
                CapabilityConfig(),
                apply=False,
                max_cycles=1,
                interval=0,
            )
        except RuntimeError as exc:
            assert "dispatch watch lock already exists" in str(exc)
        else:
            raise AssertionError("watch lock should block a second parent")


def test_watch_dispatch_cycle_passes_dispatch_params_bundle():
    """watch service 内部调用 dispatch_subagents 时使用 DispatchParams。"""
    from agent_py_agent.agent.agent_core.dispatch_params import DispatchParams
    from agent_py_agent.agent.agent_core.services.watch_service import (
        RunSingleWatchCycleParams,
        _execute_watch_dispatch,
    )

    agent = MagicMock()
    agent.subagents.workspace = Path("/tmp/subs")
    report = MagicMock()
    report.records = []
    report.summary = {}
    agent.dispatch_subagents.return_value = report
    params = RunSingleWatchCycleParams(
        cycle=1,
        lock_path=Path("/tmp/lock"),
        stop_path=None,
        router=MagicMock(),
        cfg=CapabilityConfig(),
        dispatch_params=DispatchParams(apply=True, max_runners=2),
        active_interval=0,
        idle_interval=0,
        max_consecutive=20,
        last_dispatch_had_changes=False,
        max_cycles=1,
    )

    ok, _, record_count, _, _ = _execute_watch_dispatch(agent, params)

    assert ok is True
    assert record_count == 0
    call_kwargs = agent.dispatch_subagents.call_args.kwargs
    assert call_kwargs["params"] is params.dispatch_params
    assert "apply" not in call_kwargs
