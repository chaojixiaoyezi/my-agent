"""LLM: Tests for subagent patch review: approving applied patches, blocking
planned patches, and rejecting invalid patch statuses.

给人看的解释：
测试子代理 patch 审核：审批已应用 patch、阻止未应用 patch、
拒绝非法 patch 状态。
"""

import json
import tempfile
import time
from pathlib import Path

from agent_py_agent.__main__ import build_parser
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.subagent import EvidencePacket, VerificationEvidence


def _setup_patch_task(agent, task, *, patch_status="applied", patch_summary="测试 patch 已应用"):
    """Helper to set up a task in AWAITING_ACCEPTANCE state with patch output JSON."""
    task.status = "AWAITING_ACCEPTANCE"
    task.verification_status = "NEEDS_ACCEPTANCE"
    task.channel_status = "OK"
    task.evidence.append(VerificationEvidence(
        kind="command", summary="patch smoke test 通过", command="python smoke.py", ok=True, created_at=time.time(),
    ))
    task.evidence_packets.append(EvidencePacket(
        id="evpkt-patch",
        claim="patch smoke test 通过",
        checked_scope="patch output",
        evidence_refs=[task.output_json],
        artifact_refs=["agent_py_agent/agent/demo.py"],
        confidence=0.9,
        created_at=time.time(),
    ))
    agent.subagents.save(task)
    Path(task.output_json).write_text(json.dumps({
        "run_id": task.id,
        "tests": [{"name": "smoke", "command": "python smoke.py", "ok": True}],
        "patches": [{"path": "agent_py_agent/agent/demo.py", "status": patch_status, "summary": patch_summary}],
        "blockers": [],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(task.runner_result_json).write_text(json.dumps({
        "run_id": task.id, "structured_output_found": True,
        "structured_output_ok": True, "structured_parse_error": "",
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_workspace_patch_output(task) -> None:
    Path(task.output_json).write_text(
        json.dumps(
            {
                "run_id": task.id,
                "patches": [
                    {
                        "path": "workspace.txt",
                        "tool": "write_file",
                        "status": "planned",
                        "summary": "把文件内容改成 after",
                        "content": "after\n",
                    }
                ],
                "tests": [],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def test_subagent_patch_review_approves_applied_patch_before_acceptance():
    """LLM: Verifies patch review approves an applied patch and then acceptance can pass."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(
            goal="审核已应用 patch", thought="runner 已应用 patch，等待父代理审核。", plan=["审核 patch", "验收"],
        )
        _setup_patch_task(agent, task)

        blocked = agent.subagents.write_acceptance_review_report(run_ids=[task.id], apply=False)
        assert blocked.records[0].decision == "REJECT"
        assert any(item.name == "patches_reviewed" and not item.ok for item in blocked.records[0].findings)

        patch_report = agent.subagents.write_patch_review_report(
            run_ids=[task.id], apply=True, reviewer="tester", note="patch 已人工确认",
        )
        assert patch_report.records[0].ok and patch_report.records[0].decision == "APPROVE"
        output = json.loads(Path(task.output_json).read_text(encoding="utf-8"))
        assert output["patches"][0]["review_status"] == "APPROVED"

        accepted = agent.subagents.write_acceptance_review_report(run_ids=[task.id], apply=True, reviewer="tester")
        loaded = agent.subagents.load(task.id)
        assert accepted.records[0].ok and loaded.status == "DONE" and loaded.verification_status == "VERIFIED"
        assert Path(loaded.task_dir, "PATCH_REVIEW.md").exists()
        assert Path(loaded.reports_dir, "patch_review.json").exists()


def test_subagent_patch_review_blocks_planned_patch():
    """LLM: Verifies patch review rejects a planned (not applied) patch and acceptance also rejects."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(goal="阻止未应用 patch", thought="runner 只计划了 patch。", plan=["等待 patch"])
        _setup_patch_task(agent, task, patch_status="planned", patch_summary="只计划，未应用")

        patch_report = agent.subagents.write_patch_review_report(run_ids=[task.id], apply=True, reviewer="tester")
        assert not patch_report.records[0].ok and patch_report.records[0].decision == "REJECT"
        output = json.loads(Path(task.output_json).read_text(encoding="utf-8"))
        assert output["patches"][0]["review_status"] == "NEEDS_ACTION"

        acceptance = agent.subagents.write_acceptance_review_report(run_ids=[task.id], apply=False)
        assert acceptance.records[0].decision == "REJECT"
        assert any(item.name == "no_unresolved_patches" and not item.ok for item in acceptance.records[0].findings)
        assert agent.subagents.load(task.id).status == "AWAITING_ACCEPTANCE"


def test_subagent_patch_review_rejects_invalid_patch_status():
    """LLM: Verifies patch review rejects an unknown patch status and acceptance also rejects."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(goal="阻止未知 patch 状态", thought="runner 输出了不符合协议的 patch 状态。", plan=["审核 patch"])
        _setup_patch_task(agent, task, patch_status="unknown", patch_summary="协议外状态")

        patch_report = agent.subagents.write_patch_review_report(run_ids=[task.id], apply=True, reviewer="tester")
        assert not patch_report.records[0].ok and patch_report.records[0].decision == "REJECT"
        output = json.loads(Path(task.output_json).read_text(encoding="utf-8"))
        assert output["patches"][0]["review_status"] == "NEEDS_ACTION"

        acceptance = agent.subagents.write_acceptance_review_report(run_ids=[task.id], apply=False)
        assert acceptance.records[0].decision == "REJECT"
        assert any(item.name == "patch_status_valid" and not item.ok for item in acceptance.records[0].findings)


def test_subagent_patch_apply_dry_run_shows_diff_for_write_file_patch():
    """LLM: Verifies patch apply dry-run renders the diff without mutating files."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        target = root / "workspace.txt"
        target.write_text("before\n", encoding="utf-8")
        task = agent.subagents.create_run(
            goal="预览 patch apply diff",
            thought="先只看 diff。",
            plan=["dry-run apply"],
            extra_write_roots=[str(target)],
            acceptance_checks=["command: python -c \"print('ok')\""],
        )
        Path(task.output_json).write_text(
            json.dumps(
                {
                    "run_id": task.id,
                    "patches": [
                        {
                            "path": "workspace.txt",
                            "tool": "write_file",
                            "status": "planned",
                            "summary": "把文件内容改成 after",
                            "content": "after\n",
                        }
                    ],
                    "tests": [],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        report = agent.subagents.write_patch_apply_report(
            run_ids=[task.id],
            apply=False,
            applier="tester",
        )

        assert report.records[0].ok
        assert report.records[0].decision == "WOULD_APPLY"
        assert "a/workspace.txt" in report.records[0].patches[0]["diff_preview"]
        assert target.read_text(encoding="utf-8") == "before\n"


def test_subagent_patch_apply_writes_file_and_marks_patch_reviewed():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        target = root / "workspace.txt"
        target.write_text("before\n", encoding="utf-8")
        expected_content = repr("after\n")
        task = agent.subagents.create_run(
            goal="真正 apply patch",
            thought="把 planned patch 落地。",
            plan=["apply"],
            extra_write_roots=[str(target)],
            acceptance_checks=[
                f"command: python3 -c \"from pathlib import Path; assert Path('workspace.txt').read_text() == {expected_content}\""
            ],
        )
        _write_workspace_patch_output(task)

        report = agent.subagents.write_patch_apply_report(
            run_ids=[task.id],
            apply=True,
            applier="tester",
        )
        output = json.loads(Path(task.output_json).read_text(encoding="utf-8"))

        assert report.records[0].ok
        assert report.records[0].decision == "APPLIED"
        assert target.read_text(encoding="utf-8") == "after\n"
        assert output["patches"][0]["status"] == "applied"
        assert output["patches"][0]["review_status"] == "APPROVED"
        assert Path(task.task_dir, "PATCH_APPLY.md").exists()
        assert Path(task.reports_dir, "patch_apply.json").exists()


def test_subagent_patch_apply_rejects_outside_allowed_write_roots():
    """LLM: Verifies patch apply blocks writes outside allowed roots and leaves files untouched."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        outside = root / "outside.txt"
        outside.write_text("before\n", encoding="utf-8")
        task = agent.subagents.create_run(
            goal="阻止越界 patch",
            thought="不允许写出边界。",
            plan=["apply"],
        )
        Path(task.output_json).write_text(
            json.dumps(
                {
                    "run_id": task.id,
                    "patches": [
                        {
                            "path": "outside.txt",
                            "tool": "write_file",
                            "status": "planned",
                            "summary": "试图越界写文件",
                            "content": "after\n",
                        }
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        report = agent.subagents.write_patch_apply_report(
            run_ids=[task.id],
            apply=True,
            applier="tester",
        )

        assert not report.records[0].ok
        assert report.records[0].decision == "REJECT"
        assert "allowed_write_roots" in report.records[0].patches[0]["message"]
        assert outside.read_text(encoding="utf-8") == "before\n"


def test_subagent_patch_apply_rolls_back_when_post_apply_test_fails():
    """LLM: Verifies patch apply rolls back file changes when allowlisted tests fail."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(model_backend="echo", subagent_workspace="subs")
        agent = SimpleAgent(cfg, root)
        target = root / "workspace.txt"
        target.write_text("before\n", encoding="utf-8")
        task = agent.subagents.create_run(
            goal="apply 失败后回滚",
            thought="测试失败时必须恢复原状。",
            plan=["apply", "test", "rollback"],
            extra_write_roots=[str(target)],
            attributes={"patch_test_commands": ["python3 -c \"import sys; sys.exit(1)\""]},
        )
        Path(task.output_json).write_text(
            json.dumps(
                {
                    "run_id": task.id,
                    "patches": [
                        {
                            "path": "workspace.txt",
                            "tool": "write_file",
                            "status": "planned",
                            "summary": "把文件内容改成 after",
                            "content": "after\n",
                        }
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        report = agent.subagents.write_patch_apply_report(
            run_ids=[task.id],
            apply=True,
            applier="tester",
        )

        assert not report.records[0].ok
        assert report.records[0].decision == "ROLLBACK"
        assert report.records[0].rollback_performed is True
        assert target.read_text(encoding="utf-8") == "before\n"


def test_subagents_patches_cli_apply_dry_run_writes_patch_apply_report(tmp_path, capsys):
    config_path = tmp_path / "agent_config.yaml"
    config_path.write_text(
        'workspace_root: "."\n'
        'model_backend: "echo"\n'
        'subagent_workspace: "subs"\n',
        encoding="utf-8",
    )
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    target = tmp_path / "workspace.txt"
    target.write_text("before\n", encoding="utf-8")
    task = agent.subagents.create_run(
        goal="CLI 预览 patch apply",
        thought="命令行 dry-run apply。",
        plan=["apply-dry-run"],
        extra_write_roots=[str(target)],
    )
    _write_workspace_patch_output(task)

    parser = build_parser()
    args = parser.parse_args(
        [
            "--config",
            str(config_path),
            "subagents-patches",
            "--apply-dry-run",
        ]
    )
    code = args.func(args)
    output = capsys.readouterr().out

    assert code == 0
    assert "SUBAGENT PATCH APPLY" in output
    assert "mode=apply-dry-run" in output
    assert (tmp_path / "subs" / "subagent_patch_apply_report.json").exists()
