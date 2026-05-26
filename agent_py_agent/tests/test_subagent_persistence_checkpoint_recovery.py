from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.subagents.manager import SubAgentManager


def _path_text(path: str) -> str:
    return path.replace("\\", "/")


# LLM: _create_checkpoint_recovery_task builds a blocked run with enough facts for resume handoff.
# 函数用途: 构造 compact 后恢复需要的子代理状态、失败测试和下一步事实。
def _create_checkpoint_recovery_task(manager: SubAgentManager, tmp_path: Path):
    task = manager.create_run(
        goal="恢复 compact 后的子代理事实",
        thought="只保存恢复需要的结构化事实。",
        plan=["写状态", "写失败测试", "写下一步"],
    )
    output_payload = {
        "run_id": task.id,
        "status": "BLOCKED",
        "blockers": ["缺少验证证据"],
        "tests": [
            {
                "name": "focused",
                "ok": False,
                "message": "assertion failed",
                "evidence_ref": "logs/focused.txt",
            },
            {"name": "lint", "ok": True},
        ],
        "next_actions": ["补证据链"],
        "next_action": "请求最终收口",
    }
    (tmp_path / task.id / "output.json").write_text(json.dumps(output_payload), encoding="utf-8")
    task.status = "BLOCKED"
    task.progress = 0.4
    task.current_step = "等待证据"
    task.latest_summary = "runner 已产出材料但证据不足。"
    task.blockers = ["父级未收口"]
    task.artifact_refs = ["output.json"]
    task.evidence_refs = ["logs/focused.txt"]
    return task


# LLM: _assert_checkpoint_recovery_artifacts verifies durable recovery refs, not transient logs.
# 函数用途: 检查 checkpoint、失败测试、下一步和进度报告都写入 task reports。
def _assert_checkpoint_recovery_artifacts(tmp_path: Path, task) -> None:
    checkpoint = json.loads((tmp_path / task.id / "reports" / "checkpoint.json").read_text(encoding="utf-8"))
    failing_tests = json.loads((tmp_path / task.id / "reports" / "failing_tests.json").read_text(encoding="utf-8"))
    next_actions = json.loads((tmp_path / task.id / "reports" / "next_actions.json").read_text(encoding="utf-8"))
    progress_md = (tmp_path / task.id / "reports" / "progress.md").read_text(encoding="utf-8")

    assert checkpoint["run_id"] == task.id
    assert checkpoint["status"] == "BLOCKED"
    assert _path_text(checkpoint["checkpoint_ref"]).endswith("reports/checkpoint.json")
    assert _path_text(checkpoint["status_report_ref"]).endswith("reports/status_report.json")
    assert checkpoint["blockers"] == ["父级未收口", "缺少验证证据"]
    assert failing_tests["failing_tests"] == [
        {
            "name": "focused",
            "status": "failed",
            "evidence_ref": "logs/focused.txt",
            "message": "assertion failed",
        }
    ]
    assert next_actions["next_actions"][:2] == ["补证据链", "请求最终收口"]
    assert "runner 已产出材料但证据不足。" in progress_md


def test_subagent_persistence_writes_checkpoint_recovery_artifacts(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = _create_checkpoint_recovery_task(manager, tmp_path)
    manager.save(task)

    _assert_checkpoint_recovery_artifacts(tmp_path, task)
