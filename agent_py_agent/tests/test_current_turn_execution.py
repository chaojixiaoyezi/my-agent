from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.current_turn_execution import (
    render_current_turn_execution_facts,
)
from agent_py_agent.agent.prompting_parts import PromptBuilder, ToolSections
from agent_py_agent.agent.settings import AgentConfig


def _agent() -> SimpleNamespace:
    return SimpleNamespace(
        tools=SimpleNamespace(
            tools={
                "remember": SimpleNamespace(spec=SimpleNamespace(effect="mutating")),
                "read_file": SimpleNamespace(spec=SimpleNamespace(effect="read_only")),
            }
        )
    )


def _payload(rendered: str) -> dict[str, object]:
    body = rendered.split("```json\n", 1)[1].split("\n```", 1)[0]
    return json.loads(body)


def test_empty_current_turn_has_no_authorized_mutating_claims() -> None:
    rendered = render_current_turn_execution_facts(_agent(), [])
    payload = _payload(rendered)

    assert payload["call_count"] == 0
    assert payload["successful_mutating_calls"] == []
    assert "空列表表示本轮尚无这类成功事实" in rendered


def test_current_turn_projects_typed_success_failure_and_refs() -> None:
    rendered = render_current_turn_execution_facts(
        _agent(),
        [
            {
                "tool": "remember",
                "call_id": "call-ok",
                "ok": True,
                "status": "ok",
                "handler_executed": True,
                "effect_outcome": "applied",
                "effect_source_ref": "memory-entry-1",
            },
            {
                "tool": "remember",
                "call_id": "call-no",
                "ok": False,
                "status": "error",
                "error_code": "MEMORY_TRANSIENT_CREDENTIAL",
                "failure_stage": "handler",
                "handler_executed": True,
            },
            {
                "tool": "read_file",
                "call_id": "call-read",
                "ok": True,
                "status": "ok",
                "handler_executed": True,
            },
        ],
    )
    payload = _payload(rendered)

    assert payload["call_count"] == 3
    assert [item["call_id"] for item in payload["successful_mutating_calls"]] == ["call-ok"]
    assert payload["successful_mutating_calls"][0]["refs"] == ["memory-entry-1"]
    assert [item["call_id"] for item in payload["unsuccessful_mutating_calls"]] == ["call-no"]
    assert payload["unsuccessful_mutating_calls"][0]["error_code"] == (
        "MEMORY_TRANSIENT_CREDENTIAL"
    )
    assert payload["recent_calls"][-1]["effect"] == "read_only"


def test_execution_facts_are_after_active_user_task(tmp_path) -> None:
    facts = render_current_turn_execution_facts(_agent(), [])
    rendered = PromptBuilder(AgentConfig(prompt_files=[]), tmp_path).build(
        user_prompt="请保存这个偏好",
        memories=[],
        tools=ToolSections(
            tool_catalog_section="# Tools\n- remember",
            execution_facts_section=facts,
        ),
    )

    assert rendered.index("# User Task\n请保存这个偏好") < rendered.index(
        "# Current Turn Execution Facts"
    )
