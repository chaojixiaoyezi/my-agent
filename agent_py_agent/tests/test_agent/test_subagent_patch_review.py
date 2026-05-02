"""LLM: Tests for subagent patch review: approving applied patches, blocking
planned patches, and rejecting invalid patch statuses.

给人看的解释：
测试子代理 patch 审核：审批已应用 patch、阻止未应用 patch、
拒绝非法 patch 状态。
"""

from pathlib import Path
import json
import time
import tempfile

from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.subagent import VerificationEvidence


def test_subagent_patch_review_approves_applied_patch_before_acceptance():
    """LLM: Verifies patch review approves an applied patch and then acceptance can pass."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(
            goal="审核已应用 patch",
            thought="runner 已应用 patch，等待父代理审核。",
            plan=["审核 patch", "验收"],
        )
        task.status = "AWAITING_ACCEPTANCE"
        task.verification_status = "NEEDS_ACCEPTANCE"
        task.channel_status = "OK"
        task.evidence.append(
            VerificationEvidence(
                kind="command",
                summary="patch smoke test 通过",
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
                    "tests": [{"name": "smoke", "command": "python smoke.py", "ok": True}],
                    "patches": [
                        {
                            "path": "agent_py_agent/agent/demo.py",
                            "status": "applied",
                            "summary": "测试 patch 已应用",
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

        blocked = agent.subagents.write_acceptance_review_report(
            run_ids=[task.id],
            apply=False,
        )
        assert blocked.records[0].decision == "REJECT"
        assert any(item.name == "patches_reviewed" and not item.ok for item in blocked.records[0].findings)

        patch_report = agent.subagents.write_patch_review_report(
            run_ids=[task.id],
            apply=True,
            reviewer="tester",
            note="patch 已人工确认",
        )
        assert patch_report.records[0].ok
        assert patch_report.records[0].decision == "APPROVE"
        output = json.loads(Path(task.output_json).read_text(encoding="utf-8"))
        assert output["patches"][0]["review_status"] == "APPROVED"

        accepted = agent.subagents.write_acceptance_review_report(
            run_ids=[task.id],
            apply=True,
            reviewer="tester",
        )
        loaded = agent.subagents.load(task.id)
        assert accepted.records[0].ok
        assert loaded.status == "DONE"
        assert loaded.verification_status == "VERIFIED"
        assert Path(loaded.task_dir, "PATCH_REVIEW.md").exists()
        assert Path(loaded.reports_dir, "patch_review.json").exists()


def test_subagent_patch_review_blocks_planned_patch():
    """LLM: Verifies patch review rejects a planned (not applied) patch and acceptance also rejects."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(
            goal="阻止未应用 patch",
            thought="runner 只计划了 patch。",
            plan=["等待 patch"],
        )
        task.status = "AWAITING_ACCEPTANCE"
        task.verification_status = "NEEDS_ACCEPTANCE"
        task.channel_status = "OK"
        task.evidence.append(
            VerificationEvidence(
                kind="note",
                summary="已有说明，但 patch 未应用",
                ok=True,
                created_at=time.time(),
            )
        )
        agent.subagents.save(task)
        Path(task.output_json).write_text(
            json.dumps(
                {
                    "run_id": task.id,
                    "tests": [{"name": "static", "command": "", "ok": True}],
                    "patches": [
                        {
                            "path": "agent_py_agent/agent/demo.py",
                            "status": "planned",
                            "summary": "只计划，未应用",
                        }
                    ],
                    "blockers": [],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        patch_report = agent.subagents.write_patch_review_report(
            run_ids=[task.id],
            apply=True,
            reviewer="tester",
        )
        assert not patch_report.records[0].ok
        assert patch_report.records[0].decision == "REJECT"
        output = json.loads(Path(task.output_json).read_text(encoding="utf-8"))
        assert output["patches"][0]["review_status"] == "NEEDS_ACTION"

        acceptance = agent.subagents.write_acceptance_review_report(
            run_ids=[task.id],
            apply=False,
        )
        assert acceptance.records[0].decision == "REJECT"
        assert any(item.name == "no_unresolved_patches" and not item.ok for item in acceptance.records[0].findings)
        loaded = agent.subagents.load(task.id)
        assert loaded.status == "AWAITING_ACCEPTANCE"


def test_subagent_patch_review_rejects_invalid_patch_status():
    """LLM: Verifies patch review rejects an unknown patch status and acceptance also rejects."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(
            goal="阻止未知 patch 状态",
            thought="runner 输出了不符合协议的 patch 状态。",
            plan=["审核 patch"],
        )
        task.status = "AWAITING_ACCEPTANCE"
        task.verification_status = "NEEDS_ACCEPTANCE"
        task.channel_status = "OK"
        task.evidence.append(
            VerificationEvidence(
                kind="note",
                summary="已有证据，但 patch 状态非法",
                ok=True,
                created_at=time.time(),
            )
        )
        agent.subagents.save(task)
        Path(task.output_json).write_text(
            json.dumps(
                {
                    "run_id": task.id,
                    "tests": [{"name": "static", "command": "", "ok": True}],
                    "patches": [
                        {
                            "path": "agent_py_agent/agent/demo.py",
                            "status": "unknown",
                            "summary": "协议外状态",
                        }
                    ],
                    "blockers": [],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        patch_report = agent.subagents.write_patch_review_report(
            run_ids=[task.id],
            apply=True,
            reviewer="tester",
        )
        assert not patch_report.records[0].ok
        assert patch_report.records[0].decision == "REJECT"
        output = json.loads(Path(task.output_json).read_text(encoding="utf-8"))
        assert output["patches"][0]["review_status"] == "NEEDS_ACTION"

        acceptance = agent.subagents.write_acceptance_review_report(
            run_ids=[task.id],
            apply=False,
        )
        assert acceptance.records[0].decision == "REJECT"
        assert any(item.name == "patch_status_valid" and not item.ok for item in acceptance.records[0].findings)
