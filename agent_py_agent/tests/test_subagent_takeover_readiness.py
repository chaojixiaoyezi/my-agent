from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.reports import DueCheckIssue
from agent_py_agent.agent.subagents.services.rescue_policy import rescue_fields_for_issue
from agent_py_agent.agent.subagents.services.takeover.readiness import (
    build_takeover_readiness_packet,
    render_takeover_readiness_markdown,
)


def test_takeover_readiness_packet_collects_refs_without_reading_artifact_body(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="接管黑盒大输出任务",
        thought="接管者需要先读 refs，不要直接读完整正文。",
        plan=["写 artifact", "失败交接", "等待接管"],
    )
    artifact_path = tmp_path / task.id / "reports" / "blackbox.txt"
    artifact_path.write_text("VERY_LARGE_ARTIFACT_BODY_SHOULD_NOT_APPEAR_IN_PACKET\n", encoding="utf-8")
    task.status = "FAILED"
    task.failure_type = "tool_output_context_overflow"
    task.latest_summary = "黑盒输出过大，已停止直接展开。"
    task.current_step = "等待接管"
    task.blockers = ["工具输出过大"]
    task.artifact_refs = ["reports/blackbox.txt"]
    task.evidence_refs = ["reports/status_report.json"]
    manager.write_execution_context(task.id)
    manager.save(task)

    loaded = manager.load(task.id)
    packet = build_takeover_readiness_packet(loaded)
    encoded = json.dumps(packet, ensure_ascii=False)
    markdown = render_takeover_readiness_markdown(packet)

    assert packet["schema_name"] == "subagent_takeover_readiness_packet"
    assert packet["schema_version"] == 1
    assert packet["run"]["run_id"] == task.id
    assert packet["run"]["root_id"] == task.root_id
    assert packet["failure_handoff_ref"] == loaded.failure_handoff_json
    assert packet["context_bundle_refs"]["legacy_context_bundle"] == str(Path(loaded.task_dir) / "context_bundle.json")
    assert packet["context_bundle_refs"]["agent_run_context_bundle"] == (
        str(Path(loaded.agent_run_workspace_dir) / "context_bundle.json")
    )
    assert packet["checkpoint_refs"]["legacy_checkpoint"] == loaded.checkpoint_json
    assert packet["checkpoint_refs"]["agent_run_checkpoint"] == loaded.agent_run_checkpoint_json
    assert packet["artifact_refs"] == ["reports/blackbox.txt"]
    assert packet["artifact_manifest_ref"] == loaded.agent_run_artifact_manifest_jsonl
    assert packet["artifact_manifest_records"][0]["ref"] == "reports/blackbox.txt"
    assert packet["artifact_manifest_records"][0]["exists"] is True
    assert packet["recommended_read_order"][0] == loaded.failure_handoff_json
    assert packet["context_bundle_refs"]["agent_run_context_bundle"] in packet["recommended_read_order"]
    assert "summary_is_not_verified_fact" in packet["boundary_notes"]
    assert "VERY_LARGE_ARTIFACT_BODY_SHOULD_NOT_APPEAR_IN_PACKET" not in encoded
    assert "VERY_LARGE_ARTIFACT_BODY_SHOULD_NOT_APPEAR_IN_PACKET" not in markdown
    assert "## 建议读取顺序" in markdown
    assert "## Context Bundle Refs" in markdown


def test_subagent_save_writes_takeover_readiness_packet_files(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="保存接管入口",
        thought="失败任务保存时要留下接管前必读包。",
        plan=["失败", "写接管包"],
    )
    task.status = "BLOCKED"
    task.latest_summary = "等待父级接管。"
    task.blockers = ["缺少权限"]
    task.artifact_refs = ["output.json"]
    manager.write_execution_context(task.id)
    manager.save(task)

    loaded = manager.load(task.id)
    payload = json.loads(Path(loaded.takeover_readiness_json).read_text(encoding="utf-8"))
    markdown = Path(loaded.takeover_readiness_md).read_text(encoding="utf-8")

    assert payload["run"]["run_id"] == task.id
    assert payload["failure_handoff_ref"] == loaded.failure_handoff_json
    assert payload["context_bundle_refs"]["agent_run_context_bundle"] in payload["recommended_read_order"]
    assert payload["reserved"]["reads_artifact_bodies"] is False
    assert "## 建议读取顺序" in markdown
    assert loaded.takeover_readiness_json in payload["recommended_read_order"]


def test_rescue_context_refs_use_takeover_readiness_read_order_without_artifact_body(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="rescue action reads readiness refs",
        thought="rescue should inspect refs, not large artifact bodies",
        plan=["write artifact", "block", "handoff"],
    )
    artifact_path = Path(task.task_dir) / "reports" / "blackbox.txt"
    artifact_path.write_text("DO_NOT_PULL_ARTIFACT_BODY_INTO_RESCUE_CONTEXT\n", encoding="utf-8")
    task.status = "BLOCKED"
    task.failure_type = "tool_output_context_overflow"
    task.current_step = "handoff"
    task.blockers = ["large tool output"]
    task.artifact_refs = [str(artifact_path)]
    manager.save(task)

    loaded = manager.load(task.id)
    packet = json.loads(Path(loaded.takeover_readiness_json).read_text(encoding="utf-8"))
    issue = DueCheckIssue(
        run_id=task.id,
        severity="P1",
        kind="status_blocked",
        message="blocked",
        suggested_action="takeover_or_reassign",
        status="BLOCKED",
        task_dir=loaded.task_dir,
    )

    fields = rescue_fields_for_issue(issue, "takeover_or_reassign")
    refs = fields["rescue_context_refs"]
    encoded = json.dumps(refs, ensure_ascii=False)
    expected_order = list(dict.fromkeys([loaded.takeover_readiness_json, *packet["recommended_read_order"]]))

    assert refs[0] == loaded.task_dir
    assert refs[1] == loaded.takeover_readiness_json
    assert loaded.failure_handoff_json in refs
    assert loaded.agent_run_checkpoint_json in refs
    assert loaded.agent_run_artifact_manifest_jsonl in refs
    assert refs[1 : 1 + len(expected_order)] == expected_order
    assert "DO_NOT_PULL_ARTIFACT_BODY_INTO_RESCUE_CONTEXT" not in encoded


def test_rescue_packet_records_refs_only_policy_and_escalation_without_artifact_body(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = manager.create_run(
        goal="build rescue packet",
        thought="rescue packet should stay metadata-only",
        plan=["write artifact", "block"],
    )
    artifact_path = Path(task.task_dir) / "reports" / "blackbox.txt"
    artifact_path.write_text("DO_NOT_PULL_ARTIFACT_BODY_INTO_RESCUE_PACKET\n", encoding="utf-8")
    task.status = "FAILED"
    task.failure_type = "tool_output_context_overflow"
    task.artifact_refs = [str(artifact_path)]
    manager.save(task)

    loaded = manager.load(task.id)
    issue = DueCheckIssue(
        run_id=task.id,
        severity="P1",
        kind="status_failed",
        message="failed",
        suggested_action="inspect_failure",
        status="FAILED",
        task_dir=loaded.task_dir,
    )

    fields = rescue_fields_for_issue(issue, "inspect_failure")
    packet = fields["rescue_packet"]
    encoded = json.dumps(packet, ensure_ascii=False)

    assert packet["schema_name"] == "subagent_rescue_packet"
    assert packet["run_id"] == task.id
    assert packet["dedupe_key"] == f"{task.id}:inspect_failure"
    assert packet["issue_kinds"] == ["status_failed"]
    assert packet["repeat_count"] == 1
    assert packet["retry_policy"]["max_attempts"] == 1
    assert packet["retry_policy"]["auto_retry"] is False
    assert packet["escalation"]["target"] == "parent"
    assert packet["manual_confirmation"]["required"] is True
    assert packet["recovery_entrypoints"][0] == loaded.takeover_readiness_json
    assert packet["reserved"]["reads_artifact_bodies"] is False
    assert "DO_NOT_PULL_ARTIFACT_BODY_INTO_RESCUE_PACKET" not in encoded
