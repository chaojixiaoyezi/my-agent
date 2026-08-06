from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.memory_push import (
    _build_memory_query,
    _structured_scope_attributes,
    push_relevant_memories,
)
from agent_py_agent.tests._memory_push_v2_harness import formal_memory_agent, promote_lesson


def test_query_contains_reason_failure_type_and_goal() -> None:
    query = _build_memory_query(
        "failure",
        {"failure_type": "timeout", "goal": "数据库迁移失败"},
    )

    assert query.split()[:3] == ["failure", "timeout", "数据库迁移失败"]


def test_query_does_not_embed_task_identity() -> None:
    query = _build_memory_query(
        "failure",
        {"task_id": "secret-task-id", "goal": "验证失败"},
    )

    assert "secret-task-id" not in query


def test_query_goal_is_bounded() -> None:
    query = _build_memory_query("planning", {"goal": "A" * 1000})

    assert len(query) < 150


def test_structured_scope_copies_only_typed_fields() -> None:
    attrs = _structured_scope_attributes(
        {
            "goal": "请在 company:acme 范围回答",
            "company_id": "company:acme",
            "task_attributes": {"project_id": "project:alpha", "unrelated": "kept"},
        }
    )

    assert attrs["company_id"] == "company:acme"
    assert attrs["project_id"] == "project:alpha"
    assert "goal" not in attrs


def test_company_scope_requires_typed_company_id(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    promote_lesson(
        agent,
        content="公司部署失败时核对环境变量。",
        subject_key="company.deploy.failure",
        scope_type="company",
        scope_key="company:acme",
    )

    absent = push_relevant_memories(
        agent,
        "failure",
        {"goal": "company:acme 部署失败"},
    )
    present = push_relevant_memories(
        agent,
        "failure",
        {"goal": "部署失败", "company_id": "company:acme"},
    )

    assert absent == []
    assert len(present) == 1


def test_session_scope_requires_typed_session_id(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    promote_lesson(
        agent,
        content="本会话失败时保留现场。",
        subject_key="session.failure.preserve",
        scope_type="session",
        scope_key="session:s1",
    )

    assert push_relevant_memories(agent, "failure", {"goal": "会话失败"}) == []
    assert push_relevant_memories(
        agent,
        "failure",
        {"goal": "会话失败", "session_id": "session:s1"},
    )


def test_project_task_id_accepts_exact_project_scope(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    promote_lesson(
        agent,
        content="项目失败时读取迁移清单。",
        subject_key="project.failure.migration",
        scope_type="project",
        scope_key="project:alpha",
    )

    assert push_relevant_memories(
        agent,
        "failure",
        {"task_id": "alpha", "goal": "项目失败"},
    )


def test_declared_memory_scope_is_accepted_as_typed_data(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    promote_lesson(
        agent,
        content="临时演练失败时不执行外部写入。",
        subject_key="temporary.drill.failure",
        scope_type="temporary",
        scope_key="temporary:drill-1",
    )

    records = push_relevant_memories(
        agent,
        "failure",
        {
            "goal": "演练失败",
            "memory_scope": {
                "scope_type": "temporary",
                "scope_key": "temporary:drill-1",
            },
        },
    )

    assert len(records) == 1


def test_invalid_declared_scope_cannot_expand_to_global(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    promote_lesson(
        agent,
        content="公司失败处理规则。",
        subject_key="company.failure.rule",
        scope_type="company",
        scope_key="company:acme",
    )

    records = push_relevant_memories(
        agent,
        "failure",
        {
            "goal": "公司失败",
            "memory_scope": {"scope_type": "global", "scope_key": "company:acme"},
        },
    )

    assert records == []
