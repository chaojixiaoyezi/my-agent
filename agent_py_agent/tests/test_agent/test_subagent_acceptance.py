"""LLM: Tests for subagent acceptance review: dry-run/apply, missing-evidence
rejection, write-file evidence enforcement, and actual-tool evidence.

给人看的解释：
测试子代理验收审核流程：dry-run/apply、缺证据拒绝、
写文件证据强制、真实工具证据。
"""

from pathlib import Path
import json
import time
import tempfile

from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.subagent import (
    VerificationEvidence,
    parse_subagent_runner_output,
)


def test_subagent_acceptance_dry_run_and_apply():
    """LLM: Verifies dry-run acceptance reports ACCEPT but does not change status; apply transitions to DONE."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(
            goal="验收 runner 结果",
            thought="runner 已完成，等待父代理验收。",
            plan=["检查 evidence", "检查 tests", "标记完成"],
            acceptance_checks=["有证据", "无 blocker"],
        )
        task.status = "AWAITING_ACCEPTANCE"
        task.verification_status = "NEEDS_ACCEPTANCE"
        task.channel_status = "OK"
        Path(task.reports_dir).mkdir(parents=True, exist_ok=True)
        Path(task.reports_dir, "smoke.md").write_text("smoke ok\n", encoding="utf-8")
        task.evidence.append(
            VerificationEvidence(
                kind="command",
                summary="smoke test 通过",
                command="python smoke.py",
                ok=True,
                created_at=time.time(),
            )
        )
        agent.subagents.save(task)
        Path(task.output_json).write_text(
            json.dumps(
                {
                    "run_id": task.id,
                    "status": "AWAITING_ACCEPTANCE",
                    "artifacts": [{"path": "reports/smoke.md", "kind": "report"}],
                    "tests": [{"name": "smoke", "command": "python smoke.py", "ok": True}],
                    "patches": [],
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

        dry = agent.subagents.write_acceptance_review_report(
            run_ids=[task.id],
            apply=False,
            reviewer="tester",
        )
        loaded = agent.subagents.load(task.id)
        assert dry.dry_run
        assert dry.records[0].ok
        assert dry.records[0].decision == "ACCEPT"
        assert dry.records[0].applied is False
        assert loaded.status == "AWAITING_ACCEPTANCE"

        applied = agent.subagents.write_acceptance_review_report(
            run_ids=[task.id],
            apply=True,
            reviewer="tester",
        )
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
            goal="缺少写文件证据",
            thought="runner 只读了文件，但验收要求写文件。",
            plan=["读取", "写入", "等待验收"],
            acceptance_checks=["必须有 read_file 证据；必须有 write_file 证据"],
        )
        task.status = "AWAITING_ACCEPTANCE"
        task.verification_status = "NEEDS_ACCEPTANCE"
        task.channel_status = "OK"
        task.used_tools = ["read_file"]
        task.evidence.append(
            VerificationEvidence(
                kind="read_file",
                summary="成功读取 README.md",
                path="README.md",
                ok=True,
                created_at=time.time(),
            )
        )
        agent.subagents.save(task)
        Path(task.output_json).write_text(
            json.dumps(
                {
                    "run_id": task.id,
                    "status": "AWAITING_ACCEPTANCE",
                    "tests": [],
                    "artifacts": [],
                    "patches": [],
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

        report = agent.subagents.write_acceptance_review_report(
            run_ids=[task.id],
            apply=True,
            reviewer="tester",
        )
        loaded = agent.subagents.load(task.id)

        assert report.records[0].decision == "REJECT"
        assert not report.records[0].ok
        assert loaded.status == "BLOCKED"
        assert loaded.verification_status == "FAILED"
        assert any(
            item.name == "acceptance_requires_write_file" and not item.ok
            for item in report.records[0].findings
        )


def test_subagent_acceptance_uses_actual_tool_evidence_from_runner():
    """LLM: Verifies acceptance uses actual_tools from runner result to match evidence requirements."""
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
        parsed = parse_subagent_runner_output(
            "[SUBAGENT_RESULT]\n"
            "{\n"
            '  "status": "AWAITING_ACCEPTANCE",\n'
            '  "summary": "已读取 README 并写入报告",\n'
            '  "used_tools": ["read_file", "write_file"],\n'
            '  "evidence": [\n'
            '    {"kind": "command", "summary": "读取 README.md 成功", "path": "README.md", "ok": true},\n'
            '    {"kind": "command", "summary": "写入报告成功", "path": "scenario_outputs/demo.md", "ok": true}\n'
            "  ],\n"
            '  "artifacts": [],\n'
            '  "tests": [],\n'
            '  "patches": []\n'
            "}\n"
            "[/SUBAGENT_RESULT]"
        )
        agent.subagents.record_runner_result(
            task.id,
            dry_run=False,
            ok=True,
            message="done",
            structured_output=parsed,
            actual_tools=["read_file", "write_file"],
            tool_rounds=2,
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
