from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.subagents.manager import SubAgentManager


def test_subagent_save_writes_failure_handoff_for_failed_run(tmp_path) -> None:
    store = LocalStore(tmp_path / "local.db")
    manager = SubAgentManager(tmp_path / "subagents", local_store=store)
    task = manager.create_run(
        goal="处理黑盒大输出",
        thought="失败前要留下恢复线索。",
        plan=["调用黑盒", "保存避坑信息"],
    )
    task.status = "FAILED"
    task.failure_type = "tool_output_context_overflow"
    task.latest_summary = "黑盒输出可能撑爆上下文，已停止继续展开。"
    task.current_step = "保存失败交接"
    task.blockers = ["工具输出过大"]
    task.artifact_refs = ["artifacts/tool_outputs/blackbox.txt"]
    task.evidence_refs = ["reports/status_report.json"]
    manager.save(task)

    loaded = manager.load(task.id)
    payload = json.loads(Path(loaded.failure_handoff_json).read_text(encoding="utf-8"))
    projected = store.get_agent_run(task.id)

    assert loaded.failure_handoff.failure_type == "tool_output_context_overflow"
    assert loaded.failure_handoff.risk_level == "high"
    assert loaded.failure_handoff.last_safe_checkpoint_ref == loaded.checkpoint_json
    assert loaded.failure_handoff.artifact_refs == ["artifacts/tool_outputs/blackbox.txt"]
    assert loaded.failure_handoff.avoid_next_time
    assert loaded.failure_handoff.recommended_next_action == "先外置黑盒输出，再让接管代理读取摘要和 checkpoint。"
    assert payload["run_id"] == task.id
    assert payload["warning"] == "黑盒输出可能撑爆上下文，已停止继续展开。"
    assert projected is not None
    assert projected.metadata["failure_handoff_ref"] == loaded.failure_handoff_json


def test_subagent_save_removes_failure_handoff_after_successful_retry(tmp_path) -> None:
    manager = SubAgentManager(tmp_path / "subagents")
    task = manager.create_run(
        goal="先失败后恢复",
        thought="保留恢复线索，成功后清理当前失败态。",
        plan=["fail", "retry"],
    )
    task.status = "BLOCKED"
    task.failure_type = "capability_request"
    task.blockers = ["缺少 write_file"]
    manager.save(task)
    handoff_path = Path(manager.load(task.id).failure_handoff_json)
    assert handoff_path.exists()

    task = manager.load(task.id)
    task.status = "DONE"
    task.verification_status = "VERIFIED"
    task.failure_type = ""
    task.blockers = []
    manager.save(task)

    loaded = manager.load(task.id)
    assert loaded.failure_handoff.run_id == ""
    assert not handoff_path.exists()
