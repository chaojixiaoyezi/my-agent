from __future__ import annotations

"""LLM: end-to-end checks for explicit memory-gate closeout."""

import json
from pathlib import Path

from agent_py_agent.agent.memory_archive.memory_gate.export import MemoryGateExportRequest
from agent_py_agent.agent.memory_archive.memory_gate.retention import MemoryGateRetentionRequest
from agent_py_agent.agent.memory_archive.memory_gate.review import MemoryGateReviewRequest
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.models import EvidencePacket, Finding


def test_subagent_memory_gate_explicit_export_and_retention_flow(tmp_path) -> None:
    manager = SubAgentManager(tmp_path)
    task = _create_task_with_memory_gate_candidates(manager, tmp_path)
    manager.save(task)
    loaded = manager.load(task.id)
    candidates = _read_jsonl(loaded.agent_run_memory_candidates_jsonl)
    skill_candidate = next(item for item in candidates if item["candidate_type"] == "skill_spark")
    memory_candidate = next(item for item in candidates if item["candidate_type"] == "memory_candidate")

    manager.review_memory_gate_candidate(
        task.id,
        MemoryGateReviewRequest(candidate_id=memory_candidate["candidate_id"], decision="approve_memory"),
    )
    manager.review_memory_gate_candidate(
        task.id,
        MemoryGateReviewRequest(candidate_id=skill_candidate["candidate_id"], decision="approve_skill"),
    )
    memory_result = manager.export_memory_gate_candidates_to_memory(
        task.id,
        memory_path=tmp_path / "main_memory.jsonl",
        request=MemoryGateExportRequest(candidate_id=memory_candidate["candidate_id"], reviewer="parent-test"),
    )
    skill_result = manager.export_memory_gate_candidates_to_skill_drafts(
        task.id,
        output_dir=None,
        request=MemoryGateExportRequest(candidate_id=skill_candidate["candidate_id"], reviewer="parent-test"),
    )
    retention = manager.run_memory_gate_retention(task.id, MemoryGateRetentionRequest(apply=True))
    verifier = manager.verify_memory_gate_boundary(task.id)
    after_export = _read_jsonl(loaded.agent_run_memory_candidates_jsonl)
    memory_lines = (tmp_path / "main_memory.jsonl").read_text(encoding="utf-8").splitlines()
    review_queue = _read_jsonl(loaded.agent_run_memory_review_queue_jsonl)

    assert memory_result.exported_count == 1
    assert skill_result.exported_count == 1
    assert json.loads(memory_lines[0])["kind"] == "subagent_memory_candidate"
    assert Path(skill_result.exported[0]["draft_path"]).exists()
    assert _candidate_by_id(after_export, memory_candidate["candidate_id"])["promotion_status"] == "promoted_to_memory"
    assert _candidate_by_id(after_export, skill_candidate["candidate_id"])["promotion_status"] == "skill_draft_created"
    assert retention.report["planned_action_count"] == 2
    assert review_queue == []
    assert verifier.ok is True
    assert Path(loaded.agent_run_memory_exports_jsonl).exists()


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


def _candidate_by_id(candidates, candidate_id: str) -> dict[str, object]:
    return next(item for item in candidates if item["candidate_id"] == candidate_id)


def _read_jsonl(path: str | Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
