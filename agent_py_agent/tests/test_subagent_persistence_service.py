from __future__ import annotations

"""LLM: verifies SubAgentManager persistence now routes through a service.

给人看的解释：
这个测试确保 service 化没有改变子代理工单的创建、保存、读取和扫描行为。
"""

import json

from agent_py_agent.agent.subagents.manager import SubAgentManager


def test_subagent_persistence_service_round_trips_task(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)

    task = manager.create_run(
        goal="治理持久化边界",
        thought="把读写职责移到 service 后保持兼容。",
        plan=["创建", "保存", "读取"],
    )
    task.status = "DONE"
    manager.save(task)

    loaded = manager.load(task.id)
    runs = manager.list_runs()

    assert manager.persistence is not None
    assert loaded.id == task.id
    assert loaded.status == "DONE"
    assert loaded.goal == "治理持久化边界"
    assert any(item.id == task.id for item in runs)
    assert (tmp_path / task.id / "task.json").exists()
    assert (tmp_path / task.id / "thought.md").exists()
    assert loaded.latest_status_report.run_id == task.id
    assert loaded.latest_status_report.state == "DONE"
    assert (tmp_path / task.id / "reports" / "status_report.json").exists()
    assert (tmp_path / task.id / "reports" / "checkpoint.json").exists()
    assert (tmp_path / task.id / "reports" / "decision_ledger.json").exists()
    assert (tmp_path / task.id / "reports" / "progress.md").exists()
    assert (tmp_path / task.id / "reports" / "failing_tests.json").exists()
    assert (tmp_path / task.id / "reports" / "next_actions.json").exists()
    assert (tmp_path / task.id / "SKILL_SPARKS.md").exists()
    assert loaded.checkpoint_ref == loaded.checkpoint_json
    assert loaded.skill_sparks_file.endswith("SKILL_SPARKS.md")


def test_subagent_persistence_writes_checkpoint_recovery_artifacts(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)

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
        "next_action": "请求父级验收",
    }
    (tmp_path / task.id / "output.json").write_text(json.dumps(output_payload), encoding="utf-8")
    task.status = "BLOCKED"
    task.progress = 0.4
    task.current_step = "等待证据"
    task.latest_summary = "runner 已产出材料但证据不足。"
    task.blockers = ["父级未验收"]
    task.artifact_refs = ["output.json"]
    task.evidence_refs = ["logs/focused.txt"]

    manager.save(task)

    checkpoint = json.loads((tmp_path / task.id / "reports" / "checkpoint.json").read_text(encoding="utf-8"))
    failing_tests = json.loads((tmp_path / task.id / "reports" / "failing_tests.json").read_text(encoding="utf-8"))
    next_actions = json.loads((tmp_path / task.id / "reports" / "next_actions.json").read_text(encoding="utf-8"))
    progress_md = (tmp_path / task.id / "reports" / "progress.md").read_text(encoding="utf-8")

    assert checkpoint["run_id"] == task.id
    assert checkpoint["status"] == "BLOCKED"
    assert checkpoint["checkpoint_ref"].endswith("reports/checkpoint.json")
    assert checkpoint["status_report_ref"].endswith("reports/status_report.json")
    assert checkpoint["blockers"] == ["父级未验收", "缺少验证证据"]
    assert failing_tests["failing_tests"] == [
        {
            "name": "focused",
            "status": "failed",
            "evidence_ref": "logs/focused.txt",
            "message": "assertion failed",
        }
    ]
    assert next_actions["next_actions"][:2] == ["补证据链", "请求父级验收"]
    assert "runner 已产出材料但证据不足。" in progress_md
