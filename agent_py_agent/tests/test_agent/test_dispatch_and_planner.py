"""LLM: Tests for subagent dispatch: dry-run planning, apply with
runner/patch/acceptance, and transient-failure retry.

给人看的解释：
测试子代理调度：dry-run 规划、apply 执行 runner/patch/验收、临时失败重试。
"""

from pathlib import Path
import json
import time
import tempfile

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


def test_subagent_dispatch_apply_executes_runner_and_accepts():
    """LLM: Verifies apply dispatch executes a real runner and then accepts."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        agent.backend = AcceptedSubagentBackend()
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


def test_subagent_dispatch_retries_transient_runner_failure():
    """LLM: Verifies dispatch retries a transient runner failure on the second call."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        backend = FlakyThenAcceptedSubagentBackend()
        agent.backend = backend
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
        assert backend.calls == 2
        assert loaded.status == "DONE"
        assert loaded.verification_status == "VERIFIED"
        assert loaded.runner_attempts == 2
        assert loaded.runner_last_error == ""
