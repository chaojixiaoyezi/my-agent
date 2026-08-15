from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.planner_service import (
    _planner_memory_goal,
    append_planner_memory_hint,
)
from agent_py_agent.agent.agent_core.runner.gate import _append_memory_hint
from agent_py_agent.agent.memory_push import format_memories_for_injection, push_failure_memories
from agent_py_agent.tests._memory_push_v2_harness import formal_memory_agent, promote_lesson

_STATE = {
    "due_issues": [{"run_id": "r1", "goal": "规划失败验证", "kind": "stalled"}],
    "active_tasks": [{"id": "r2", "goal": "备用目标"}],
}


def test_planner_prompt_gets_formal_memory_context(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    promote_lesson(agent)

    prompt = append_planner_memory_hint(agent, _STATE, "# Parent Planner Tick\n...")

    assert prompt.startswith("# Parent Planner Tick")
    assert prompt.count("<memory-context>") == 1
    assert "[相关记忆提示]" not in prompt


def test_planner_injection_is_soft_when_owner_memory_missing() -> None:
    prompt = "# Parent Planner Tick"

    assert append_planner_memory_hint(SimpleNamespace(), _STATE, prompt) == prompt


def test_planner_injection_survives_formal_recall_exception(monkeypatch) -> None:
    import agent_py_agent.agent.memory_push as memory_push

    monkeypatch.setattr(
        memory_push,
        "push_relevant_memories",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("index crashed")),
    )

    assert append_planner_memory_hint(SimpleNamespace(), _STATE, "P") == "P"


def test_planner_memory_goal_prefers_due_issue_goal() -> None:
    assert _planner_memory_goal(_STATE) == "规划失败验证"
    assert _planner_memory_goal({"active_tasks": [{"goal": "备用目标"}]}) == "备用目标"
    assert _planner_memory_goal({}) == ""
    assert _planner_memory_goal({"due_issues": ["garbage", {"goal": "g"}]}) == "g"


def test_planner_injection_is_idempotent(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    promote_lesson(agent)

    once = append_planner_memory_hint(agent, _STATE, "# Tick\n")
    twice = append_planner_memory_hint(agent, _STATE, once)

    assert twice == once
    assert twice.count("<memory-context>") == 1


def test_runner_failure_hint_uses_same_envelope(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    promote_lesson(agent)
    hint = format_memories_for_injection(
        push_failure_memories(agent, "task-x", "失败验证", "timeout")
    )

    instruction = _append_memory_hint("继续推进任务", hint)
    again = _append_memory_hint(instruction, hint)

    assert instruction.count("<memory-context>") == 1
    assert again == instruction


def test_failure_memory_hint_handles_empty_inputs() -> None:
    assert _append_memory_hint("指令", "") == "指令"
    assert _append_memory_hint("", "hint") == "hint"


def test_planner_does_not_inject_raw_routing_markdown(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    promote_lesson(agent)

    prompt = append_planner_memory_hint(agent, _STATE, "P")

    assert "authority_path:" not in prompt
    assert "trigger_keywords:" not in prompt


def test_planner_scope_is_not_inferred_from_goal_text(tmp_path: Path) -> None:
    agent = formal_memory_agent(tmp_path)
    promote_lesson(
        agent,
        scope_type="company",
        scope_key="company:acme",
        subject_key="planning.company.failure",
    )
    state = {"due_issues": [{"goal": "按 company:acme 规划失败处理"}]}

    assert append_planner_memory_hint(agent, state, "P") == "P"
