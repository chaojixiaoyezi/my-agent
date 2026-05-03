"""LLM: Tests for subagent dispatch: dry-run planning, apply with
runner/patch/acceptance, and transient-failure retry.

给人看的解释：
测试子代理调度：dry-run 规划、apply 执行 runner/patch/验收、临时失败重试。
"""

import json
import tempfile
import time
from pathlib import Path

from agent_py_agent.agent.capabilities import CapabilityRouter
from agent_py_agent.agent.capability_config import CapabilityConfig
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.subagent import VerificationEvidence

from .backends import (
    AcceptedSubagentBackend,
    FlakyThenAcceptedSubagentBackend,
)


def test_subagent_dispatch_dry_run_plans_runner_patch_and_acceptance():
    """LLM: Verifies dry-run dispatch plans runner, patch_review, and acceptance steps without mutating state."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        runner_task = agent.subagents.create_run(
            goal="调度 runner dry-run",
            thought="等待父代理调度。",
            plan=["执行 runner"],
        )
        review_task = agent.subagents.create_run(
            goal="调度 patch 和验收 dry-run",
            thought="runner 已完成，等待审核。",
            plan=["审核 patch", "验收"],
        )
        review_task.status = "AWAITING_ACCEPTANCE"
        review_task.verification_status = "NEEDS_ACCEPTANCE"
        review_task.channel_status = "OK"
        review_task.evidence.append(
            VerificationEvidence(
                kind="note",
                summary="有验收证据",
                ok=True,
                created_at=time.time(),
            )
        )
        agent.subagents.save(review_task)
        Path(review_task.output_json).write_text(
            json.dumps(
                {
                    "run_id": review_task.id,
                    "tests": [{"name": "smoke", "command": "", "ok": True}],
                    "patches": [
                        {
                            "path": "agent_py_agent/agent/demo.py",
                            "status": "applied",
                            "summary": "已应用但未审核",
                        }
                    ],
                    "blockers": [],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        router = CapabilityRouter(config=CapabilityConfig(), tool_specs=agent.tools.specs())
        report = agent.dispatch_subagents(
            router,
            CapabilityConfig(),
            apply=False,
            max_runners=1,
        )
        output = json.loads(Path(review_task.output_json).read_text(encoding="utf-8"))

        assert report.dry_run
        assert any(item.step == "runner" and item.run_id == runner_task.id for item in report.records)
        assert any(item.step == "patch_review" and item.run_id == review_task.id for item in report.records)
        assert any(item.step == "acceptance" and item.run_id == review_task.id for item in report.records)
        assert "review_status" not in output["patches"][0]
        assert agent.subagents.load(runner_task.id).status == "PLANNING"
        assert (root / "subs" / "subagent_dispatch_report.json").exists()
        assert not (root / "subs" / "subagent_dispatch_log.jsonl").exists()


def test_subagent_dispatch_apply_reviews_patch_then_accepts():
    """LLM: Verifies apply dispatch reviews patch and then accepts the run."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(
            goal="调度 patch 审核后验收",
            thought="runner 已完成，等待调度器收口。",
            plan=["审核 patch", "验收"],
        )
        task.status = "AWAITING_ACCEPTANCE"
        task.verification_status = "NEEDS_ACCEPTANCE"
        task.channel_status = "OK"
        task.evidence.append(
            VerificationEvidence(
                kind="note",
                summary="有验收证据",
                ok=True,
                created_at=time.time(),
            )
        )
        agent.subagents.save(task)
        Path(task.output_json).write_text(
            json.dumps(
                {
                    "run_id": task.id,
                    "tests": [{"name": "smoke", "command": "", "ok": True}],
                    "patches": [
                        {
                            "path": "agent_py_agent/agent/demo.py",
                            "status": "applied",
                            "summary": "已应用",
                        }
                    ],
                    "blockers": [],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        Path(task.runner_result_json).write_text(
            json.dumps(
                {
                    "run_id": task.id,
                    "structured_output_found": True,
                    "structured_output_ok": True,
                    "structured_parse_error": "",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        router = CapabilityRouter(config=CapabilityConfig(), tool_specs=agent.tools.specs())
        report = agent.dispatch_subagents(
            router,
            CapabilityConfig(),
            apply=True,
            max_runners=0,
            reviewer="dispatch-test",
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
    """LLM: Verifies dispatch retries a transient runner failure on the second call."""
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

        first = agent.dispatch_subagents(
            router,
            CapabilityConfig(),
            apply=True,
            execute_runners=True,
            max_runners=1,
            probe=False,
            reviewer="dispatch-test",
        )
        after_first = agent.subagents.load(task.id)

        assert any(item.step == "runner" and item.action == "execute_runner" and not item.ok for item in first.records)
        assert after_first.status == "BLOCKED"
        assert after_first.failure_type == "runner_error"
        assert after_first.runner_attempts == 1
        assert "temporary runner backend outage" in after_first.runner_last_error

        second = agent.dispatch_subagents(
            router,
            CapabilityConfig(),
            apply=True,
            execute_runners=True,
            max_runners=1,
            probe=False,
            reviewer="dispatch-test",
        )
        loaded = agent.subagents.load(task.id)

        assert any(item.step == "runner" and item.action == "retry_runner" and item.ok for item in second.records)
        assert any(item.step == "acceptance" and item.applied and item.ok for item in second.records)
        # Backend called 3 times: (1) first runner attempt fails, (2) failure introspection, (3) retry succeeds
        assert backend.calls == 3
        assert loaded.status == "DONE"
        assert loaded.verification_status == "VERIFIED"
        assert loaded.runner_attempts == 2
        assert loaded.runner_last_error == ""


def test_subagent_dispatch_workflow_plan_mode_persists_plan_only():
    """LLM: Verifies dispatch can backfill workflow plans onto existing parent runs without spawning children."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        parent = agent.subagents.create_run(
            goal="Fix API bug and add regression tests",
            thought="先建父工单，再由 dispatch 补做 workflow 规划。",
            plan=["等待规划"],
        )

        router = CapabilityRouter(config=CapabilityConfig(), tool_specs=agent.tools.specs())
        report = agent.dispatch_subagents(
            router,
            CapabilityConfig(),
            apply=True,
            workflow_mode="plan",
            max_runners=0,
        )
        loaded = agent.subagents.load(parent.id)

        assert any(item.step == "workflow" and item.action == "plan_workflow" and item.ok for item in report.records)
        assert loaded.workflow_mode == "plan"
        assert loaded.workflow_template_id == "code_feature_split"
        assert loaded.workflow_plan["ok"] is True
        assert loaded.workflow_child_run_ids == []
        assert loaded.child_ids == []


def test_subagent_dispatch_workflow_auto_mode_spawns_worker_children():
    """LLM: Verifies dispatch auto mode turns a parent workflow plan into worker child runs."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        parent = agent.subagents.create_run(
            goal="Fix API bug and add regression tests",
            thought="让 workflow 自动派出 implementation/tests worker。",
            plan=["等待自动派工"],
        )

        router = CapabilityRouter(config=CapabilityConfig(), tool_specs=agent.tools.specs())
        report = agent.dispatch_subagents(
            router,
            CapabilityConfig(),
            apply=True,
            workflow_mode="auto",
            max_runners=0,
        )
        loaded = agent.subagents.load(parent.id)
        children = [agent.subagents.load(run_id) for run_id in loaded.workflow_child_run_ids]

        assert any(item.step == "workflow" and item.action == "spawn_workflow_workers" for item in report.records)
        assert loaded.workflow_mode == "auto"
        assert loaded.workflow_plan["ok"] is True
        assert len(loaded.workflow_child_run_ids) == 3
        assert len(children) == 3
        assert {child.workflow_phase_id for child in children} == {"design_contract", "implementation", "tests"}
        assert all(child.parent_id == loaded.id for child in children)
        assert any(child.workflow_depends_on == ["design_contract"] for child in children if child.workflow_phase_id in {"implementation", "tests"})
