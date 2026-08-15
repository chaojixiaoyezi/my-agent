from __future__ import annotations

from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.models import Finding, SubAgentTask


def _task(run_id: str) -> SubAgentTask:
    return SubAgentTask(
        id=run_id,
        root_id="project-root",
        goal="修复项目记忆链路",
        thought="",
        plan=[],
        output_json=f"/artifacts/{run_id}/output.json",
        updated_at=100.0,
    )


def test_subagent_lessons_and_findings_use_owner_candidate_service(tmp_path):
    candidates = CandidateService(tmp_path / "owner" / "memory" / "candidates.jsonl")
    manager = SubAgentManager(tmp_path / "subagents", candidate_service=candidates)
    task = _task("run-1")
    finding = Finding(
        id="finding-1",
        claim="同一长期事实必须经过统一晋升服务。",
        confidence=0.8,
        evidence_refs=["evidence-1"],
        created_at=100.0,
    )

    recorded = manager.memory_candidates.record_result_candidates(
        task,
        lessons=["不要让 task-local 候选成为第二权威。"],
        findings=[finding],
    )

    assert len(recorded) == 2
    current = candidates.list()
    assert {item.origin for item in current} == {"subagent_lesson", "subagent_finding"}
    assert {item.status for item in current} == {"pending_review"}
    # 新持久化一律 project:<id>（单一权威；task:<id> 仅旧账本召回别名，不新写）
    assert {item.scope["scope_key"] for item in current} == {"project:project-root"}
    assert not (tmp_path / "subagents" / "data" / "learning_drafts").exists()


def test_subagent_candidate_replay_is_idempotent_but_new_run_is_independent(tmp_path):
    candidates = CandidateService(tmp_path / "owner" / "memory" / "candidates.jsonl")
    manager = SubAgentManager(tmp_path / "subagents", candidate_service=candidates)
    lesson = "候选必须保留来源 run。"

    first = manager.memory_candidates.record_result_candidates(
        _task("run-1"), lessons=[lesson], findings=[]
    )[0]
    replay = manager.memory_candidates.record_result_candidates(
        _task("run-1"), lessons=[lesson], findings=[]
    )[0]
    second_run = manager.memory_candidates.record_result_candidates(
        _task("run-2"), lessons=[lesson], findings=[]
    )[0]

    assert replay.candidate_id == first.candidate_id == second_run.candidate_id
    assert replay.occurrence_count == 1
    assert second_run.occurrence_count == 2
    assert set(second_run.source_run_ids) == {"run-1", "run-2"}
