"""失败自省教训已从 legacy trigger_conditions 长期记录收敛为统一 Candidate。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.models import FailureType

pytestmark = pytest.mark.integration


def _agent(tmp_path: Path) -> SimpleAgent:
    return SimpleAgent(
        AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")),
        tmp_path,
    )


def _task(agent: SimpleAgent):
    task = agent.subagents.create_run(goal="一个会超时的大任务", thought="t", plan=["p"])
    task.status = "TIMEOUT"
    task.failure_type = FailureType.RUNNER_TIMEOUT.value
    task.runner_attempts = 1
    task.attributes["dynamic_timeout_seconds"] = 60.0
    agent.subagents.save(task)
    return task


def test_introspection_records_model_inferred_lesson_candidate(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    task = _task(agent)
    runner_result = SimpleNamespace(
        ok=False,
        status="TIMEOUT",
        message="runner timed out",
        failure_type=FailureType.RUNNER_TIMEOUT.value,
    )

    agent._handle_failure_introspection(task.id, task, runner_result)

    candidates = [item for item in agent.memory_candidates.list() if item.candidate_type == "lesson"]
    assert candidates
    candidate = candidates[0]
    assert candidate.origin == "model_inferred"
    assert candidate.promotion_target == "lesson"
    assert candidate.status == "pending_review"
    assert task.id in candidate.source_run_ids
    assert agent.memory.all() == []


def test_introspection_candidate_replay_is_idempotent(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    task = _task(agent)
    adapter = agent.subagents.memory_candidates
    kwargs = {
        "suggested_params": {"new_timeout_seconds": 120},
        "failure_type": FailureType.RUNNER_TIMEOUT.value,
        "attempts": 1,
        "confidence": 0.8,
    }

    first = adapter.record_introspection_lesson(task, **kwargs)
    second = adapter.record_introspection_lesson(task, **kwargs)

    assert first.candidate_id == second.candidate_id
    assert second.occurrence_count == 1
    assert len(agent.memory_candidates.list()) == 1


# 候选只按独立来源计票：同一 run 的重试只是同一来源的别名，同一根任务下另一个 run 的同类失败才增计。
def test_independent_attempts_increment_same_candidate(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    task = _task(agent)
    sibling = agent.subagents.create_run(goal=task.goal, thought="t", plan=["p"], root_id=task.root_id or task.id)
    adapter = agent.subagents.memory_candidates
    kwargs = {
        "suggested_params": {"new_timeout_seconds": 120},
        "failure_type": FailureType.RUNNER_TIMEOUT.value,
        "confidence": 0.8,
    }

    first = adapter.record_introspection_lesson(task, attempts=1, **kwargs)
    retry = adapter.record_introspection_lesson(task, attempts=2, **kwargs)
    second = adapter.record_introspection_lesson(sibling, attempts=1, **kwargs)

    assert first.candidate_id == retry.candidate_id == second.candidate_id
    assert retry.occurrence_count == 1
    assert second.occurrence_count == 2


def test_confidence_does_not_promote_introspection_candidate(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    task = _task(agent)

    candidate = agent.subagents.memory_candidates.record_introspection_lesson(
        task,
        suggested_params={"new_timeout_seconds": 120},
        failure_type=FailureType.RUNNER_TIMEOUT.value,
        attempts=1,
        confidence=1.0,
    )

    assert candidate.status == "pending_review"
    assert not list(agent.memory_lessons.lessons_dir.glob("*.md"))
    assert agent.memory.all() == []


def test_introspection_reference_contains_typed_failure_state_only(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    task = _task(agent)

    candidate = agent.subagents.memory_candidates.record_introspection_lesson(
        task,
        suggested_params={"new_timeout_seconds": 120},
        failure_type=FailureType.RUNNER_TIMEOUT.value,
        attempts=3,
        confidence=0.5,
    )

    evidence = candidate.evidence_refs[0]
    assert evidence["failure_type"] == FailureType.RUNNER_TIMEOUT.value
    assert evidence["attempts"] == 3
    assert "content" not in evidence
    assert "trigger_conditions" not in evidence
