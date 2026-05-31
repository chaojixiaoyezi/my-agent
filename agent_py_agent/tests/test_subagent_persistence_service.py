from __future__ import annotations

"""LLM: verifies SubAgentManager persistence now routes through a service.

给人看的解释：
这个测试确保 service 化没有改变子代理工单的创建、保存、读取和扫描行为。
"""

import json
from pathlib import Path

from agent_py_agent.agent.memory_archive.memory_gate_review import MemoryGateReviewRequest
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.models import EvidencePacket, Finding


def _path_text(path: str) -> str:
    return path.replace("\\", "/")


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
    _assert_runtime_workspace_paths(loaded, tmp_path / "tasks" / task.root_id, task.id)


def test_subagent_task_has_session_and_thread_identity(tmp_path) -> None:
    """子代理 run 上方要有稳定 session/thread 身份，供接管和 compact 续跑复用。"""
    manager = SubAgentManager(tmp_path)
    root = manager.create_run(goal="root", thought="coordinate", plan=["split"])
    child = manager.create_run(
        goal="child",
        thought="work",
        plan=["do"],
        parent_id=root.id,
        root_id=root.id,
        depth=1,
    )

    loaded_child = manager.load(child.id)

    assert root.subagent_session_id.startswith("session-")
    assert root.agent_thread_id.startswith("thread-")
    assert loaded_child.subagent_session_id.startswith("session-")
    assert loaded_child.agent_thread_id.startswith("thread-")
    assert loaded_child.parent_subagent_session_id == root.subagent_session_id
    assert loaded_child.root_subagent_session_id == root.root_subagent_session_id
    assert loaded_child.subagent_session_id != root.subagent_session_id

    context = manager.build_execution_context(child.id)
    assert context.subagent_session_id == loaded_child.subagent_session_id
    assert context.parent_subagent_session_id == root.subagent_session_id


def test_subagent_persistence_creates_task_workspace_skeleton(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)

    task = manager.create_run(
        goal="建立任务级运行记忆空间",
        thought="旧工单目录不能移动，新空间只做 adapter。",
        plan=["建 task workspace", "写兼容指针"],
    )
    task.status = "RUNNING"
    task.progress = 0.5
    task.current_step = "同步状态"
    task.latest_summary = "已创建 task workspace 骨架。"
    manager.save(task)

    task_workspace = tmp_path / "tasks" / task.root_id
    state = json.loads((task_workspace / "state.json").read_text(encoding="utf-8"))
    legacy_ref = json.loads(
        (task_workspace / "agents" / task.id / "legacy_run_ref.json").read_text(encoding="utf-8"),
    )
    timeline_lines = (task_workspace / "timeline.jsonl").read_text(encoding="utf-8").splitlines()
    summary_md = (task_workspace / "summaries" / "current_summary.md").read_text(encoding="utf-8")

    assert (task_workspace / "task.yaml").exists()
    assert (task_workspace / "shared" / "blackboard.md").exists()
    assert (task_workspace / "shared" / "messages.jsonl").exists()
    assert (task_workspace / "shared" / "findings.jsonl").exists()
    assert (task_workspace / "artifacts" / "tool_outputs").is_dir()
    assert (task_workspace / "artifacts" / "log_samples").is_dir()
    assert (task_workspace / "artifacts" / "code_snapshots").is_dir()
    assert (task_workspace / "artifacts" / "reports").is_dir()
    assert (task_workspace / "compact" / "task_rollup.json").exists()
    rollup = json.loads((task_workspace / "compact" / "task_rollup.json").read_text(encoding="utf-8"))
    assert rollup["child_count"] == 1
    assert rollup["child_runs"][0]["run_id"] == task.id
    assert state["task_id"] == task.root_id
    assert state["primary_run_id"] == task.id
    assert state["status"] == "RUNNING"
    assert state["progress"] == 0.5
    assert _path_text(state["legacy"]["task_json"]).endswith(f"{task.id}/task.json")
    assert legacy_ref["mode"] == "legacy_subagent_work_order_adapter"
    assert legacy_ref["legacy_task_dir"] == str(tmp_path / task.id)
    assert legacy_ref["agent_run_workspace_status"] == "phase_1_skeleton"
    assert any(json.loads(line)["event"] == "task_workspace_synced" for line in timeline_lines)
    assert "已创建 task workspace 骨架。" in summary_md


def _assert_runtime_workspace_paths(loaded, task_workspace, run_id: str) -> None:
    run_workspace = task_workspace / "agents" / run_id
    assert loaded.task_workspace_dir == str(task_workspace)
    assert loaded.task_workspace_task_yaml == str(task_workspace / "task.yaml")
    assert loaded.task_workspace_state_json == str(task_workspace / "state.json")
    assert loaded.task_workspace_timeline_jsonl == str(task_workspace / "timeline.jsonl")
    assert loaded.task_workspace_summary_file == str(task_workspace / "summaries" / "current_summary.md")
    assert loaded.task_workspace_shared_dir == str(task_workspace / "shared")
    assert loaded.task_workspace_shared_blackboard == str(task_workspace / "shared" / "blackboard.md")
    assert loaded.task_workspace_shared_messages_jsonl == str(task_workspace / "shared" / "messages.jsonl")
    assert loaded.task_workspace_shared_findings_jsonl == str(task_workspace / "shared" / "findings.jsonl")
    assert loaded.task_workspace_shared_evidence_packets_dir == str(task_workspace / "shared" / "evidence_packets")
    assert loaded.task_workspace_shared_evidence_index_jsonl == str(
        task_workspace / "shared" / "evidence_packets" / "index.jsonl",
    )
    assert loaded.task_workspace_artifacts_dir == str(task_workspace / "artifacts")
    assert loaded.task_workspace_agents_dir == str(task_workspace / "agents")
    assert loaded.agent_run_workspace_dir == str(run_workspace)
    assert loaded.agent_run_agent_yaml == str(run_workspace / "agent.yaml")
    assert loaded.agent_run_state_json == str(run_workspace / "state.json")
    assert loaded.agent_run_task_md == str(run_workspace / "task.md")
    assert loaded.agent_run_timeline_jsonl == str(run_workspace / "timeline.jsonl")
    assert loaded.agent_run_checkpoint_json == str(run_workspace / "checkpoint.json")
    assert loaded.agent_run_summary_md == str(run_workspace / "summary.md")
    assert loaded.agent_run_final_report_md == str(run_workspace / "final_report.md")
    assert loaded.agent_run_findings_jsonl == str(run_workspace / "findings.jsonl")
    assert loaded.agent_run_inbox_dir == str(run_workspace / "inbox")
    assert loaded.agent_run_outbox_dir == str(run_workspace / "outbox")
    assert loaded.agent_run_artifacts_dir == str(run_workspace / "artifacts")
    assert loaded.agent_run_compactions_dir == str(run_workspace / "compactions")
    assert loaded.agent_run_compaction_ledger_jsonl == str(run_workspace / "compactions" / "compaction_ledger.jsonl")
    assert loaded.agent_run_latest_compaction_summary_md == str(run_workspace / "compactions" / "latest_summary.md")
    assert loaded.agent_run_latest_compaction_metadata_json == str(run_workspace / "compactions" / "latest_metadata.json")
    assert loaded.agent_run_memory_gate_dir == str(run_workspace / "memory_gate")
    assert loaded.agent_run_memory_candidates_jsonl == str(run_workspace / "memory_gate" / "candidates.jsonl")
    assert loaded.agent_run_memory_review_queue_jsonl == str(run_workspace / "memory_gate" / "review_queue.jsonl")
    assert loaded.agent_run_memory_decisions_jsonl == str(run_workspace / "memory_gate" / "decisions.jsonl")
    assert loaded.agent_run_memory_exports_jsonl == str(run_workspace / "memory_gate" / "exports.jsonl")
    assert loaded.agent_run_skill_spark_gate_json == str(run_workspace / "memory_gate" / "skill_spark_gate.json")
    assert loaded.legacy_run_ref_json == str(run_workspace / "legacy_run_ref.json")
    assert "/daily/" in _path_text(loaded.daily_ledger_file)
    assert _path_text(loaded.daily_ledger_file).endswith("/events.jsonl")
    assert loaded.daily_ledger_last_event_id.startswith(f"evt-{run_id}-{run_id}-")
    assert loaded.task_artifact_manifest_jsonl == str(task_workspace / "artifacts" / "manifest.jsonl")
    assert loaded.agent_run_artifact_manifest_jsonl == str(run_workspace / "artifacts" / "manifest.jsonl")


def test_subagent_persistence_writes_memory_gate_candidates(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = _create_task_with_memory_gate_candidates(manager, tmp_path)
    manager.save(task)

    loaded = manager.load(task.id)
    candidates = _read_jsonl(loaded.agent_run_memory_candidates_jsonl)
    review_queue = _read_jsonl(loaded.agent_run_memory_review_queue_jsonl)
    gate = json.loads(Path(loaded.agent_run_skill_spark_gate_json).read_text(encoding="utf-8"))
    checkpoint = json.loads(Path(loaded.agent_run_checkpoint_json).read_text(encoding="utf-8"))

    _assert_memory_gate_candidates(
        candidates,
        review_queue,
        {"gate": gate, "checkpoint": checkpoint, "loaded": loaded},
    )


def _create_task_with_memory_gate_candidates(manager: SubAgentManager, tmp_path: Path):
    task = manager.create_run(
        goal="沉淀可复用经验但先经过门禁",
        thought="经验候选必须留在 task/run 空间等待 review。",
        plan=["写 lesson", "同步 gate"],
    )
    output_payload = {
        "run_id": task.id,
        "status": "DONE",
        "lessons": ["先核验证据链，再把经验作为候选提交 review"],
    }
    (tmp_path / task.id / "output.json").write_text(json.dumps(output_payload), encoding="utf-8")
    task.status = "DONE"
    task.current_step = "等待 review"
    task.latest_summary = "已产出经验候选。"
    task.evidence_refs = ["reports/status_report.json"]
    task.evidence_packets = [
        EvidencePacket(
            id="evpkt-gate-1",
            claim="经验候选有可核验证据",
            checked_scope="runner result",
            evidence_refs=["reports/status_report.json"],
            artifact_refs=["output.json"],
            confidence=0.8,
        ),
    ]
    task.findings = [
        Finding(
            id="finding-gate-1",
            claim="经验只能作为候选，不可自动提升",
            evidence_packet_ids=["evpkt-gate-1"],
            evidence_refs=["reports/status_report.json"],
            confidence=0.7,
        ),
    ]
    return task


def _assert_memory_gate_candidates(candidates, review_queue, refs) -> None:
    gate = refs["gate"]
    checkpoint = refs["checkpoint"]
    loaded = refs["loaded"]
    skill_candidate = next(item for item in candidates if item["candidate_type"] == "skill_spark")
    memory_candidate = next(item for item in candidates if item["candidate_type"] == "memory_candidate")
    assert skill_candidate["content"] == "先核验证据链，再把经验作为候选提交 review"
    assert skill_candidate["promotion_status"] == "not_promoted"
    assert skill_candidate["review_required"] is True
    assert skill_candidate["evidence_refs"] == ["reports/status_report.json", "evpkt-gate-1"]
    assert skill_candidate["artifact_refs"] == ["output.json"]
    assert "limits_or_counterexamples" in skill_candidate["missing_requirements"]
    assert memory_candidate["content"] == "经验只能作为候选，不可自动提升"
    assert review_queue[0]["promotion_status"] == "not_promoted"
    assert gate["promotion_policy"] == "never_auto_promote"
    assert gate["promoted_count"] == 0
    assert checkpoint["memory_gate"]["auto_promote"] is False
    assert checkpoint["memory_gate"]["candidates_ref"] == loaded.agent_run_memory_candidates_jsonl
    assert checkpoint["memory_gate"]["exports_ref"] == loaded.agent_run_memory_exports_jsonl


def test_subagent_memory_gate_review_preserves_decision_across_save(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = _create_task_with_memory_gate_candidates(manager, tmp_path)
    manager.save(task)
    loaded = manager.load(task.id)
    candidate_id = _read_jsonl(loaded.agent_run_memory_candidates_jsonl)[0]["candidate_id"]

    result = manager.review_memory_gate_candidate(
        task.id,
        MemoryGateReviewRequest(
            candidate_id=candidate_id,
            decision="approve_memory",
            reviewer="parent-test",
            note="证据链已核验，允许后续显式导出 memory。",
        ),
    )
    reviewed = result.candidate
    manager.save(manager.load(task.id))
    after_save = _candidate_by_id(_read_jsonl(loaded.agent_run_memory_candidates_jsonl), candidate_id)
    decisions = _read_jsonl(loaded.agent_run_memory_decisions_jsonl)

    assert reviewed["review_status"] == "approved"
    assert reviewed["promotion_status"] == "approved_for_memory_export"
    assert reviewed["review_required"] is False
    assert after_save["reviewer"] == "parent-test"
    assert after_save["promotion_status"] == "approved_for_memory_export"
    assert decisions[-1]["candidate_id"] == candidate_id
    assert decisions[-1]["auto_promote"] is False


def _candidate_by_id(candidates, candidate_id: str) -> dict[str, object]:
    return next(item for item in candidates if item["candidate_id"] == candidate_id)


def test_subagent_persistence_creates_agent_run_workspace_skeleton(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)

    task = manager.create_run(
        goal="建立子代理运行工作位",
        thought="在 task workspace 下生成 run workspace，但旧目录继续兼容。",
        plan=["写 agent.yaml", "写 run state", "写恢复入口"],
    )
    task.status = "BLOCKED"
    task.progress = 0.25
    task.current_step = "等待证据"
    task.latest_summary = "run workspace 已创建，等待证据。"
    task.blockers = ["缺少日志样本"]
    task.result = "暂未完成"
    manager.save(task)

    run_workspace = tmp_path / "tasks" / task.root_id / "agents" / task.id
    run_state = json.loads((run_workspace / "state.json").read_text(encoding="utf-8"))
    checkpoint = json.loads((run_workspace / "checkpoint.json").read_text(encoding="utf-8"))
    legacy_ref = json.loads((run_workspace / "legacy_run_ref.json").read_text(encoding="utf-8"))
    run_timeline = (run_workspace / "timeline.jsonl").read_text(encoding="utf-8").splitlines()

    assert (run_workspace / "agent.yaml").exists()
    assert (run_workspace / "task.md").exists()
    assert (run_workspace / "summary.md").exists()
    assert (run_workspace / "final_report.md").exists()
    assert (run_workspace / "findings.jsonl").exists()
    assert (run_workspace / "inbox").is_dir()
    assert (run_workspace / "outbox").is_dir()
    assert (run_workspace / "artifacts" / "tool_outputs").is_dir()
    assert (run_workspace / "artifacts" / "reports").is_dir()
    assert (run_workspace / "compactions").is_dir()
    assert run_state["task_id"] == task.root_id
    assert run_state["run_id"] == task.id
    assert run_state["status"] == "BLOCKED"
    assert run_state["blockers"] == ["缺少日志样本"]
    assert _path_text(checkpoint["legacy_checkpoint_ref"]).endswith(f"{task.id}/reports/checkpoint.json")
    assert legacy_ref["legacy_task_dir"] == str(tmp_path / task.id)
    assert legacy_ref["agent_run_workspace_status"] == "phase_1_skeleton"
    assert any(json.loads(line)["event"] == "agent_run_workspace_synced" for line in run_timeline)


def test_subagent_persistence_appends_daily_event_ledger(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)

    task = manager.create_run(
        goal="写每日事件账本",
        thought="事件只放摘要、状态和引用，不放完整上下文。",
        plan=["保存任务", "检查 daily ledger"],
    )
    task.status = "RUNNING"
    task.current_step = "记录事件"
    task.latest_summary = "daily ledger 已追加 task/run 引用。"
    task.artifact_refs = ["reports/demo.txt"]
    task.evidence_refs = ["logs/demo.log"]
    manager.save(task)

    loaded = manager.load(task.id)
    events = _read_jsonl(loaded.daily_ledger_file)
    latest = events[-1]

    assert loaded.daily_ledger_last_event_id == latest["event_id"]
    assert latest["event_type"] == "subagent_task_saved"
    assert latest["task_id"] == task.root_id
    assert latest["run_id"] == task.id
    assert latest["status"] == "RUNNING"
    assert latest["summary"] == "daily ledger 已追加 task/run 引用。"
    assert latest["artifact_refs"] == ["reports/demo.txt"]
    assert latest["evidence_refs"] == ["logs/demo.log"]
    assert latest["refs"]["task_workspace"] == str(tmp_path / "tasks" / task.root_id)
    assert latest["refs"]["agent_run_workspace"] == str(tmp_path / "tasks" / task.root_id / "agents" / task.id)
    assert latest["refs"]["task_artifact_manifest"] == str(tmp_path / "tasks" / task.root_id / "artifacts" / "manifest.jsonl")
    assert latest["refs"]["agent_artifact_manifest"] == str(
        tmp_path / "tasks" / task.root_id / "agents" / task.id / "artifacts" / "manifest.jsonl",
    )
    assert latest["refs"]["agent_compaction_ledger"] == str(
        tmp_path / "tasks" / task.root_id / "agents" / task.id / "compactions" / "compaction_ledger.jsonl",
    )
    assert latest["refs"]["legacy_task_dir"] == str(tmp_path / task.id)
    assert "goal" not in latest


def test_subagent_persistence_writes_artifact_manifests(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)

    task = manager.create_run(
        goal="外置 artifact 引用",
        thought="manifest 只记录摘要、hash 和路径，不复制输出正文。",
        plan=["写 artifact", "保存 manifest"],
    )
    artifact_path = tmp_path / task.id / "reports" / "demo.txt"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text("artifact body\n", encoding="utf-8")
    task.artifact_refs = ["reports/demo.txt", "missing.log"]
    manager.save(task)

    loaded = manager.load(task.id)
    task_records = _read_jsonl(loaded.task_artifact_manifest_jsonl)
    run_records = _read_jsonl(loaded.agent_run_artifact_manifest_jsonl)
    existing = task_records[0]
    missing = task_records[1]

    assert run_records == task_records
    assert existing["ref"] == "reports/demo.txt"
    assert existing["path"] == str(artifact_path)
    assert existing["exists"] is True
    assert existing["size_bytes"] == artifact_path.stat().st_size
    assert existing["sha256"]
    assert existing["content_externalized"] is True
    assert "artifact body" not in json.dumps(existing, ensure_ascii=False)
    assert missing["ref"] == "missing.log"
    assert missing["exists"] is False
    assert missing["sha256"] == ""
    assert missing["resolution_status"] == "missing"


def test_subagent_persistence_writes_compact_checkpoint_chain(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)

    task = manager.create_run(
        goal="建立 checkpoint compact 链",
        thought="compact 链只保存恢复摘要和元数据，不删除 timeline 或 artifact。",
        plan=["写 checkpoint", "追加 compact ledger"],
    )
    output_path = tmp_path / task.id / "output.json"
    output_path.write_text(json.dumps({"next_actions": ["继续验收"]}), encoding="utf-8")
    task.status = "RUNNING"
    task.progress = 0.6
    task.current_step = "生成 compact checkpoint"
    task.latest_summary = "已经写入 checkpoint snapshot。"
    task.artifact_refs = ["output.json"]
    task.blockers = ["等待父级确认"]
    manager.save(task)

    loaded = manager.load(task.id)
    ledger = _read_jsonl(loaded.agent_run_compaction_ledger_jsonl)
    latest = ledger[-1]
    metadata = json.loads(Path(loaded.agent_run_latest_compaction_metadata_json).read_text(encoding="utf-8"))
    checkpoint = json.loads(Path(loaded.agent_run_checkpoint_json).read_text(encoding="utf-8"))
    summary = Path(loaded.agent_run_latest_compaction_summary_md).read_text(encoding="utf-8")

    assert len(ledger) >= 2
    assert latest["event_type"] == "checkpoint_snapshot"
    assert latest["compact_status"] == "checkpoint_only"
    assert latest["previous_event_id"] == ledger[-2]["event_id"]
    assert latest["refs"]["checkpoint"] == loaded.agent_run_checkpoint_json
    assert latest["refs"]["artifact_manifest"] == loaded.agent_run_artifact_manifest_jsonl
    assert metadata["event_id"] == latest["event_id"]
    assert checkpoint["compact_chain"]["last_event_id"] == latest["event_id"]
    assert checkpoint["compact_chain"]["content_preserved"] is True
    assert "已经写入 checkpoint snapshot。" in summary
    assert Path(loaded.agent_run_timeline_jsonl).exists()
    assert output_path.exists()


# LLM: checkpoint compact writes should be material-change driven, not every save call.
# 函数用途: 相同任务状态重复保存时不追加新的 checkpoint compact 事件，避免长 runner 目录被噪音撑大。
def test_subagent_persistence_deduplicates_unchanged_compact_checkpoint_chain(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)

    task = manager.create_run(
        goal="去重 checkpoint compact 链",
        thought="重复 save 不应制造重复 compact 事件。",
        plan=["保存一次", "重复保存"],
    )
    loaded = manager.load(task.id)
    before = _read_jsonl(loaded.agent_run_compaction_ledger_jsonl)

    manager.save(loaded)
    after = _read_jsonl(manager.load(task.id).agent_run_compaction_ledger_jsonl)

    assert len(after) == len(before)


def test_subagent_persistence_syncs_shared_workspace_facts(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = _create_task_with_shared_facts(manager)
    manager.save(task)

    loaded = manager.load(task.id)
    _assert_shared_workspace_facts(loaded, task)


def _create_task_with_shared_facts(manager: SubAgentManager):
    task = manager.create_run(
        goal="共享子代理任务局部事实",
        thought="shared 目录只放结构化 facts，不写主 memory。",
        plan=["写 evidence packet", "写 finding", "同步 shared"],
    )
    task.status = "RUNNING"
    task.current_step = "同步 shared workspace"
    task.latest_summary = "已产出可共享证据和发现。"
    task.blockers = ["等待 sibling 复核"]
    task.evidence_packets = [
        EvidencePacket(
            id="evpkt-shared-1",
            claim="复核输入已经准备好",
            checked_scope="shared workspace",
            evidence_refs=["reports/status_report.json"],
            artifact_refs=["output.json"],
            confidence=0.82,
        ),
    ]
    task.findings = [
        Finding(
            id="finding-shared-1",
            claim="需要 sibling 复核证据链",
            status="OPEN",
            severity="P2",
            evidence_packet_ids=["evpkt-shared-1"],
            evidence_refs=["reports/status_report.json"],
            confidence=0.74,
        ),
    ]
    return task


def _assert_shared_workspace_facts(loaded, task) -> None:
    messages = _read_jsonl(loaded.task_workspace_shared_messages_jsonl)
    findings = _read_jsonl(loaded.task_workspace_shared_findings_jsonl)
    evidence_index = _read_jsonl(loaded.task_workspace_shared_evidence_index_jsonl)
    packet = json.loads(
        (Path(loaded.task_workspace_shared_evidence_packets_dir) / "evpkt-shared-1.json").read_text(encoding="utf-8"),
    )
    blackboard = Path(loaded.task_workspace_shared_blackboard).read_text(encoding="utf-8")

    assert messages[-1]["message_type"] == "status_update"
    assert messages[-1]["summary"] == "已产出可共享证据和发现。"
    assert messages[-1]["evidence_packet_count"] == 1
    assert messages[-1]["finding_count"] == 1
    assert findings[0]["id"] == "finding-shared-1"
    assert findings[0]["run_id"] == task.id
    assert evidence_index[0]["id"] == "evpkt-shared-1"
    assert _path_text(evidence_index[0]["path"]).endswith("shared/evidence_packets/evpkt-shared-1.json")
    assert packet["claim"] == "复核输入已经准备好"
    assert "需要 sibling 复核证据链" in blackboard
    assert "等待 sibling 复核" in blackboard


def _read_jsonl(path: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
