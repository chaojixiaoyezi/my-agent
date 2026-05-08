"""LLM: Tests for subagent acceptance review: dry-run/apply, missing-evidence
rejection, write-file evidence enforcement, and actual-tool evidence.

给人看的解释：
测试子代理验收审核流程：dry-run/apply、缺证据拒绝、
写文件证据强制、真实工具证据。
"""

import json
import tempfile
import time
from pathlib import Path

from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.subagent import (
    AcceptanceReviewOptions,
    EvidencePacket,
    RecordRunnerResultParams,
    VerificationEvidence,
    parse_subagent_runner_output,
)


def _rrr(run_id: str, **kwargs) -> RecordRunnerResultParams:
    """Helper to create RecordRunnerResultParams with run_id as positional arg."""
    return RecordRunnerResultParams(run_id=run_id, **kwargs)


def _setup_acceptance_task(agent, task):
    """Helper to set up a task in AWAITING_ACCEPTANCE state with evidence and output JSON."""
    task.status = "AWAITING_ACCEPTANCE"
    task.verification_status = "NEEDS_ACCEPTANCE"
    task.channel_status = "OK"
    Path(task.reports_dir).mkdir(parents=True, exist_ok=True)
    Path(task.reports_dir, "smoke.md").write_text("smoke ok\n", encoding="utf-8")
    task.evidence.append(VerificationEvidence(
        kind="command", summary="smoke test 通过", command="python smoke.py", ok=True, created_at=time.time(),
    ))
    task.evidence_packets.append(EvidencePacket(
        id="evpkt-smoke",
        claim="smoke test 通过",
        checked_scope="reports/smoke.md",
        evidence_refs=[str(Path(task.reports_dir, "smoke.md"))],
        artifact_refs=["reports/smoke.md"],
        confidence=0.9,
        created_at=time.time(),
    ))
    agent.subagents.save(task)
    Path(task.output_json).write_text(json.dumps({
        "run_id": task.id, "status": "AWAITING_ACCEPTANCE",
        "artifacts": [{"path": "reports/smoke.md", "kind": "report"}],
        "tests": [{"name": "smoke", "command": "python smoke.py", "ok": True}],
        "patches": [], "blockers": [],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(task.runner_result_json).write_text(json.dumps({
        "run_id": task.id, "structured_output_found": True,
        "structured_output_ok": True, "structured_parse_error": "",
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def _actual_tool_evidence_output() -> str:
    return (
        "[SUBAGENT_RESULT]\n"
        "{\n"
        '  "status": "AWAITING_ACCEPTANCE",\n'
        '  "summary": "已读取 README 并写入报告",\n'
        '  "used_tools": ["read_file", "write_file"],\n'
        '  "evidence": [\n'
        '    {"kind": "command", "summary": "读取 README.md 成功", "path": "README.md", "ok": true},\n'
        '    {"kind": "command", "summary": "写入报告成功", "path": "scenario_outputs/demo.md", "ok": true}\n'
        "  ],\n"
        '  "evidence_packets": [\n'
        '    {"id": "evpkt-tools", "claim": "README 已读取且报告已写入", "checked_scope": "README.md + scenario_outputs/demo.md", "evidence_refs": ["README.md", "scenario_outputs/demo.md"], "artifact_refs": ["scenario_outputs/demo.md"], "confidence": 0.9}\n'
        "  ],\n"
        '  "artifacts": [],\n'
        '  "tests": [],\n'
        '  "patches": []\n'
        "}\n"
        "[/SUBAGENT_RESULT]"
    )


def test_subagent_acceptance_dry_run_and_apply():
    """LLM: Verifies dry-run acceptance reports ACCEPT but does not change status; apply transitions to DONE."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(
            goal="验收 runner 结果", thought="runner 已完成，等待父代理验收。",
            plan=["检查 evidence", "检查 tests", "标记完成"], acceptance_checks=["有证据", "无 blocker"],
        )
        _setup_acceptance_task(agent, task)

        dry = agent.subagents.write_acceptance_review_report(run_ids=[task.id], apply=False, reviewer="tester")
        loaded = agent.subagents.load(task.id)
        assert dry.dry_run
        assert dry.records[0].ok
        assert dry.records[0].decision == "ACCEPT"
        assert dry.records[0].applied is False
        assert dry.records[0].worker_claims
        assert any("evidence_packets=1" in item for item in dry.records[0].evidence_facts)
        assert dry.records[0].parent_conclusions[0] == "decision=ACCEPT"
        assert any(item.name == "verifier_evidence_packets_traceable" and item.ok for item in dry.records[0].verifier_checks)
        assert loaded.status == "AWAITING_ACCEPTANCE"

        applied = agent.subagents.write_acceptance_review_report(run_ids=[task.id], apply=True, reviewer="tester")
        loaded = agent.subagents.load(task.id)
        assert not applied.dry_run
        assert applied.records[0].applied
        assert loaded.status == "DONE"
        assert loaded.verification_status == "VERIFIED"
        assert Path(loaded.task_dir, "ACCEPTANCE_REVIEW.md").exists()
        assert Path(loaded.reports_dir, "acceptance_review.json").exists()


def test_subagent_acceptance_rejects_missing_evidence_without_apply():
    """LLM: Verifies acceptance rejects a run with no evidence and does not apply."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(
            goal="缺证据验收",
            thought="故意没有 evidence。",
            plan=["等待验收"],
        )
        task.status = "AWAITING_ACCEPTANCE"
        task.verification_status = "NEEDS_ACCEPTANCE"
        agent.subagents.save(task)

        report = agent.subagents.write_acceptance_review_report(
            run_ids=[task.id],
            apply=False,
        )
        loaded = agent.subagents.load(task.id)
        assert report.records[0].decision == "REJECT"
        assert not report.records[0].ok
        assert any(item.name == "evidence_present" and not item.ok for item in report.records[0].findings)
        assert loaded.status == "AWAITING_ACCEPTANCE"


def test_subagent_acceptance_enforces_required_write_file_evidence():
    """LLM: Verifies acceptance rejects when acceptance_checks require write_file evidence but only read_file exists."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(
            goal="缺少写文件证据", thought="runner 只读了文件，但验收要求写文件。",
            plan=["读取", "写入", "等待验收"],
            acceptance_checks=["必须有 read_file 证据；必须有 write_file 证据"],
        )
        task.status = "AWAITING_ACCEPTANCE"
        task.verification_status = "NEEDS_ACCEPTANCE"
        task.channel_status = "OK"
        task.used_tools = ["read_file"]
        task.evidence.append(VerificationEvidence(
            kind="read_file", summary="成功读取 README.md", path="README.md", ok=True, created_at=time.time(),
        ))
        agent.subagents.save(task)
        Path(task.output_json).write_text(json.dumps({
            "run_id": task.id, "status": "AWAITING_ACCEPTANCE",
            "tests": [], "artifacts": [], "patches": [], "blockers": [],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        Path(task.runner_result_json).write_text(json.dumps({
            "run_id": task.id, "structured_output_found": True,
            "structured_output_ok": True, "structured_parse_error": "",
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        report = agent.subagents.write_acceptance_review_report(run_ids=[task.id], apply=True, reviewer="tester")
        loaded = agent.subagents.load(task.id)
        assert report.records[0].decision == "REJECT"
        assert not report.records[0].ok
        assert loaded.status == "BLOCKED"
        assert loaded.verification_status == "FAILED"
        assert any(item.name == "acceptance_requires_write_file" and not item.ok for item in report.records[0].findings)


def test_subagent_acceptance_uses_actual_tool_evidence_from_runner():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(
            goal="真实工具证据验收",
            thought="runner 的自然语言证据没有写工具名，但系统有 actual_tools。",
            plan=["读取", "写入", "等待验收"],
            allowed_tools=["read_file", "write_file"],
            acceptance_checks=["必须有 read_file 证据；必须有 write_file 证据"],
        )
        parsed = parse_subagent_runner_output(_actual_tool_evidence_output())
        agent.subagents.record_runner_result(
            _rrr(
                task.id,
                dry_run=False,
                ok=True,
                message="done",
                structured_output=parsed,
                actual_tools=["read_file", "write_file"],
                tool_rounds=2,
            )
        )

        report = agent.subagents.write_acceptance_review_report(
            run_ids=[task.id],
            apply=True,
            reviewer="tester",
        )
        loaded = agent.subagents.load(task.id)

        assert report.records[0].decision == "ACCEPT"
        assert loaded.status == "DONE"
        assert loaded.verification_status == "VERIFIED"
        assert any(item.command == "read_file" for item in loaded.evidence)
        assert any(item.command == "write_file" for item in loaded.evidence)


def test_subagent_acceptance_verifier_rejects_unresolved_evidence_risk():
    """LLM: Verifies verifier findings block acceptance when evidence packets keep unresolved risks."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(
            goal="验收未解决风险",
            thought="runner 留下 unresolved evidence risk。",
            plan=["检查风险"],
        )
        _setup_acceptance_task(agent, task)
        loaded = agent.subagents.load(task.id)
        loaded.evidence_packets[0].unresolved_risks = ["没有覆盖错误路径"]
        agent.subagents.save(loaded)

        report = agent.subagents.write_acceptance_review_report(run_ids=[task.id], apply=True, reviewer="tester")
        rejected = agent.subagents.load(task.id)

        assert report.records[0].decision == "REJECT"
        assert rejected.status == "BLOCKED"
        assert any(
            item.name == "verifier_no_unresolved_evidence_risks" and not item.ok
            for item in report.records[0].verifier_checks
        )


def test_subagent_acceptance_can_execute_real_tests_on_explicit_dry_run():
    """LLM: Verifies explicit acceptance test execution writes records and blocks false PASS claims."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(
            goal="真实测试执行验收",
            thought="runner 声称测试通过，但命令实际失败。",
            plan=["检查 evidence", "真实执行 tests"],
            acceptance_checks=["必须有真实测试记录"],
        )
        _setup_acceptance_task(agent, task)
        Path(task.output_json).write_text(json.dumps({
            "run_id": task.id,
            "status": "AWAITING_ACCEPTANCE",
            "tests": [{
                "name": "failing command",
                "validation_method": "command",
                "command": "python -c \"import sys; sys.exit(1)\"",
                "ok": True,
            }],
            "artifacts": [{"path": "reports/smoke.md", "kind": "report"}],
            "patches": [],
            "blockers": [],
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        report = agent.subagents.write_acceptance_review_report(
            run_ids=[task.id],
            options=AcceptanceReviewOptions(execute_tests=True, test_timeout_seconds=10),
            reviewer="tester",
        )
        loaded = agent.subagents.load(task.id)
        test_execution_json = Path(loaded.reports_dir) / "test_execution.json"

        assert report.records[0].decision == "REJECT"
        assert loaded.status == "AWAITING_ACCEPTANCE"
        assert test_execution_json.exists()
        assert Path(loaded.reports_dir, "test_execution.md").exists()
        assert any(item.name == "test_execution_passed" and not item.ok for item in report.records[0].findings)
        assert str(test_execution_json) in report.records[0].evidence_paths
