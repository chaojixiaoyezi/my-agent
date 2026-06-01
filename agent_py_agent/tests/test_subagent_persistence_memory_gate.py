from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.memory_archive.memory_gate_review import MemoryGateReviewRequest
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.models import EvidencePacket, Finding


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


def _candidate_by_id(candidates, candidate_id: str) -> dict[str, object]:
    return next(item for item in candidates if item["candidate_id"] == candidate_id)


def _read_jsonl(path: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]

