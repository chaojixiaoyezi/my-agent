from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.memory_push import (
    format_memories_for_injection,
    push_failure_memories,
    push_planning_memories,
    push_relevant_memories,
    push_relevant_memories_report,
    push_timeout_memories,
)
from agent_py_agent.agent.memory_store import MemoryRecord
from agent_py_agent.tests._memory_push_v2_harness import (
    formal_memory_agent,
    promote_hot,
    promote_lesson,
)


def test_push_reads_only_formal_routed_lesson(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    lesson = promote_lesson(agent)

    records = push_relevant_memories(
        agent,
        "failure",
        {"task_id": "task-x", "goal": "失败后如何验证"},
    )

    assert [record.entry_id for record in records] == [lesson.lesson_id]
    assert all(record.kind == "lesson" for record in records)


def test_legacy_long_term_lesson_kind_is_not_a_recall_source(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    agent.memory.add(
        "system",
        "旧 long-term lesson 不得复活。",
        kind="lesson_general",
    )

    records = push_relevant_memories(agent, "failure", {"goal": "旧教训"})

    assert records == []


def test_candidate_body_is_not_recalled_before_formal_promotion(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    candidate_text = "未审核候选绝不能进入 Prompt。"
    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope

    agent.memory_candidates.observe(
        CandidateObservation(
            candidate_type="lesson",
            content=candidate_text,
            subject_key="failure.unreviewed",
            scope=MemoryScope("global", "global"),
            origin="model_inferred",
            promotion_target="lesson",
            observation_id="unreviewed",
        )
    )

    rendered = format_memories_for_injection(
        push_relevant_memories(agent, "failure", {"goal": "未审核"})
    )

    assert candidate_text not in rendered


def test_hot_and_lesson_share_formal_scope_and_stable_order(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    lesson = promote_lesson(agent)
    hot = promote_hot(agent, lesson)

    records = push_relevant_memories(agent, "failure", {"goal": "验证失败"}, limit=3)

    assert [record.entry_id for record in records] == [hot.hot_id, lesson.lesson_id]
    assert [record.kind for record in records] == ["hot", "lesson"]


def test_scope_mismatch_excludes_project_lesson(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    promote_lesson(
        agent,
        scope_type="project",
        scope_key="project:alpha",
    )

    wrong = push_relevant_memories(
        agent,
        "failure",
        {"task_id": "beta", "goal": "验证失败"},
    )
    right = push_relevant_memories(
        agent,
        "failure",
        {"task_id": "alpha", "goal": "验证失败"},
    )

    assert wrong == []
    assert len(right) == 1


def test_natural_language_cannot_expand_company_scope(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    promote_lesson(
        agent,
        content="公司部署失败时先核对发布环境。",
        subject_key="company.failure.deploy",
        scope_type="company",
        scope_key="company:acme",
    )

    inferred = push_relevant_memories(
        agent,
        "failure",
        {"goal": "请按 company:acme 的公司部署规则处理"},
    )
    typed = push_relevant_memories(
        agent,
        "failure",
        {"goal": "公司部署失败", "company_id": "company:acme"},
    )

    assert inferred == []
    assert len(typed) == 1


def test_limit_is_enforced_after_formal_dedupe(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    promote_lesson(agent, content="失败验证规则甲。", subject_key="failure.rule.alpha")
    promote_lesson(agent, content="失败验证规则乙。", subject_key="failure.rule.beta")

    records = push_relevant_memories(agent, "failure", {"goal": "失败规则"}, limit=1)

    assert len(records) == 1


def test_format_uses_exactly_one_non_authoritative_envelope() -> None:
    record = MemoryRecord(
        role="system",
        content="历史教训只作参考。",
        kind="lesson",
        entry_id="lesson-1",
        attributes={"origin": "reviewed"},
    )

    rendered = format_memories_for_injection([record])

    assert rendered.count("<memory-context>") == 1
    assert rendered.count("</memory-context>") == 1
    assert '"authority":"non_authoritative"' in rendered
    assert "[相关记忆提示]" not in rendered


def test_format_empty_records_does_not_add_empty_envelope() -> None:
    assert format_memories_for_injection([]) == ""


def test_wrapper_functions_share_the_formal_service(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    promote_lesson(agent)

    assert push_timeout_memories(agent, "task-x", "planning failure validation")
    assert push_failure_memories(
        agent,
        "task-x",
        "planning failure validation",
        "timeout",
    )
    assert push_planning_memories(agent, "task-x", "planning failure validation")


def test_missing_owner_authority_fails_soft_with_structured_error() -> None:
    memories, errors = push_relevant_memories_report(
        SimpleNamespace(),
        "failure",
        {"goal": "失败验证"},
    )

    assert memories == []
    assert errors
    assert errors[0]["context"] == "memory_push.formal_recall"


def test_old_direct_lesson_writer_api_is_absent() -> None:
    import agent_py_agent.agent.memory_push as module

    assert not hasattr(module, "MemoryType")
    assert not hasattr(module, "MemoryWriteContext")
    assert not hasattr(module, "write_memory_with_type")
