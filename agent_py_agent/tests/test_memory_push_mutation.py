from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.memory_push import push_relevant_memories
from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
from agent_py_agent.tests._memory_push_v2_harness import (
    formal_memory_agent,
    promote_hot,
    promote_lesson,
)


def _snapshot_tree(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_recall_is_read_only_for_entire_owner_memory_tree(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    lesson = promote_lesson(agent)
    promote_hot(agent, lesson)
    root = agent.home_paths.owner_home_dir
    before = _snapshot_tree(root)

    for _ in range(4):
        push_relevant_memories(agent, "failure", {"goal": "失败验证"})

    assert _snapshot_tree(root) == before


def test_recall_does_not_review_or_promote_pending_candidate(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    candidate = agent.memory_candidates.observe(
        CandidateObservation(
            candidate_type="lesson",
            content="模型猜测的失败教训。",
            subject_key="failure.model-guess",
            scope=MemoryScope("global", "global"),
            origin="model_inferred",
            promotion_target="lesson",
            observation_id="guess-1",
        )
    )

    assert push_relevant_memories(agent, "failure", {"goal": "失败猜测"}) == []
    assert agent.memory_candidates.get(candidate.candidate_id).status == "pending_review"


def test_recall_does_not_increase_candidate_occurrence_count(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    candidate = agent.memory_candidates.observe(
        CandidateObservation(
            candidate_type="lesson",
            content="一次观察。",
            subject_key="failure.once",
            scope=MemoryScope("global", "global"),
            origin="model_inferred",
            promotion_target="lesson",
            observation_id="once",
        )
    )

    for _ in range(5):
        push_relevant_memories(agent, "failure", {"goal": "一次失败"})

    assert agent.memory_candidates.get(candidate.candidate_id).occurrence_count == 1


def test_recall_does_not_create_daily_or_ops_files(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    promote_lesson(agent)
    memory_root = agent.home_paths.owner_home_dir / "memory"
    daily = memory_root / "daily"
    ops = memory_root / "ops.jsonl"

    push_relevant_memories(agent, "failure", {"goal": "失败验证"})

    assert not daily.exists()
    assert not ops.exists()


def test_legacy_long_term_record_is_not_mutated_or_migrated_on_read(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    legacy = agent.memory.add("system", "旧教训正文", kind="lesson_task")
    before = agent.memory.path.read_bytes()

    assert push_relevant_memories(agent, "failure", {"goal": "旧教训"}) == []

    assert agent.memory.path.read_bytes() == before
    assert any(record.entry_id == legacy.entry_id for record in agent.memory.all())


def test_missing_route_index_is_not_created_by_recall(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    index = agent.home_paths.owner_memory_routing_index_md
    assert not index.exists()

    assert push_relevant_memories(agent, "failure", {"goal": "失败"}) == []

    assert not index.exists()


def test_scope_mismatch_does_not_modify_formal_lesson(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    lesson = promote_lesson(
        agent,
        scope_type="project",
        scope_key="project:alpha",
    )
    path = agent.home_paths.owner_home_dir / lesson.path
    before = path.read_bytes()

    assert push_relevant_memories(
        agent,
        "failure",
        {"task_id": "beta", "goal": "失败验证"},
    ) == []
    assert path.read_bytes() == before


def test_prompt_projection_does_not_write_sanitized_copy(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    promote_lesson(agent, content="历史内容仅作为参考。")
    root = agent.home_paths.owner_home_dir
    before_paths = {path.relative_to(root).as_posix() for path in root.rglob("*")}

    push_relevant_memories(agent, "planning", {"goal": "历史内容"})

    after_paths = {path.relative_to(root).as_posix() for path in root.rglob("*")}
    assert after_paths == before_paths
