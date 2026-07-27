from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.prompting_parts import PromptBuilder, ToolSections
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.operation_verification import (
    append_operation_verification,
    build_operation_verification,
    public_operation_verification,
    render_current_turn_execution_facts,
)


def _agent() -> SimpleNamespace:
    return SimpleNamespace(
        tools=SimpleNamespace(
            tools={
                "remember": SimpleNamespace(
                    spec=SimpleNamespace(
                        effect="mutating",
                        parameters={"action": "memory action"},
                        parameter_schema={
                            "action": {
                                "type": "string",
                                "enum": ["add", "list", "replace", "remove"],
                            }
                        },
                    )
                ),
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
                "operation_id": "operation-ok",
                "tool_operation_status": "succeeded",
                "parameters": {"action": "remove"},
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
    assert payload["successful_mutating_calls"][0]["action"] == "remove"
    assert payload["successful_mutating_calls"][0]["refs"] == ["memory-entry-1"]
    assert [item["call_id"] for item in payload["unsuccessful_mutating_calls"]] == ["call-no"]
    assert payload["unsuccessful_mutating_calls"][0]["error_code"] == (
        "MEMORY_TRANSIENT_CREDENTIAL"
    )
    assert payload["recent_calls"][-1]["effect"] == "read_only"
    assert payload["effect_counts"] == {
        "dangerous": 0,
        "mutating": 2,
        "read_only": 1,
        "unknown": 0,
    }
    assert payload["verification_counts"]["succeeded"] == 2
    assert payload["verification_counts"]["unverified"] == 1


def test_current_turn_prompt_projection_is_bounded_but_keeps_complete_counts() -> None:
    records = [
        {
            "tool": "read_file",
            "call_id": f"read-{index}",
            "ok": True,
            "status": "ok",
            "handler_executed": True,
            "raw_archive_path": f"/owner/archive/round-{index // 10}.jsonl",
        }
        for index in range(80)
    ]
    records.extend(
        {
            "tool": "remember",
            "call_id": f"write-{index}",
            "operation_id": f"operation-{index}",
            "ok": True,
            "status": "ok",
            "handler_executed": True,
            "tool_operation_status": "succeeded",
            "parameters": {"action": "add"},
            "raw_archive_path": f"/owner/archive/write-{index // 10}.jsonl",
        }
        for index in range(20)
    )

    payload = _payload(render_current_turn_execution_facts(_agent(), records))

    assert payload["call_count"] == 100
    assert payload["effect_counts"]["read_only"] == 80
    assert payload["effect_counts"]["mutating"] == 20
    assert payload["verification_counts"]["succeeded"] == 100
    assert len(payload["recent_calls"]) == 6
    assert payload["omitted_call_count"] == 94
    assert len(payload["successful_mutating_calls"]) == 6
    assert payload["omitted_successful_mutating_call_count"] == 14
    assert payload["mutating_operation_groups"] == [
        {
            "action": "add",
            "count": 20,
            "label": "remember/add",
            "replayed": False,
            "status": "succeeded",
            "tool": "remember",
        }
    ]
    assert payload["raw_archive_refs"] == [
        "/owner/archive/round-6.jsonl",
        "/owner/archive/round-7.jsonl",
        "/owner/archive/write-0.jsonl",
        "/owner/archive/write-1.jsonl",
    ]


def test_mutating_ok_without_operation_terminal_is_not_verified_success() -> None:
    payload = _payload(
        render_current_turn_execution_facts(
            _agent(),
            [
                {
                    "tool": "remember",
                    "call_id": "call-no-ledger",
                    "ok": True,
                    "handler_executed": True,
                    "parameters": {"action": "remove"},
                }
            ],
        )
    )

    assert payload["successful_mutating_calls"] == []
    assert payload["unsuccessful_mutating_calls"][0]["verification_status"] == "unverified"


def test_final_operation_verification_uses_typed_action_and_terminal_status() -> None:
    verification = build_operation_verification(
        _agent(),
        [
            {
                "tool": "remember",
                "call_id": "call-list",
                "operation_id": "operation-list",
                "ok": True,
                "handler_executed": True,
                "tool_operation_status": "succeeded",
                "parameters": {"action": "list"},
            },
            {
                "tool": "remember",
                "call_id": "call-remove",
                "operation_id": "operation-remove",
                "ok": False,
                "handler_executed": False,
                "failure_stage": "runtime_gate",
                "effect_outcome": "not_started",
                "tool_operation_status": "failed",
                "parameters": {"action": "remove"},
            },
        ],
    )
    rendered = append_operation_verification("我已经删除了这条记忆。", verification)

    assert verification["status"] == "partial"
    assert verification["counts"]["succeeded"] == 1
    assert verification["counts"]["not_started"] == 1
    assert "- remember/list：成功" in rendered
    assert "- remember/remove：未执行" in rendered
    assert "我已经删除了这条记忆。" in rendered


def test_final_operation_verification_deduplicates_replayed_operation() -> None:
    records = [
        {
            "tool": "remember",
            "call_id": call_id,
            "operation_id": "same-operation",
            "ok": True,
            "handler_executed": handler_executed,
            "tool_operation_status": "succeeded",
            "tool_operation_replayed": replayed,
            "parameters": {"action": "add"},
        }
        for call_id, handler_executed, replayed in (
            ("call-original", True, False),
            ("call-replay", False, True),
        )
    ]

    verification = build_operation_verification(_agent(), records)
    rendered = append_operation_verification("完成。", verification)

    assert verification["operation_count"] == 1
    assert verification["operations"][0]["attempt_count"] == 2
    assert verification["operations"][0]["replayed"] is True
    assert "幂等重放，未重复执行" in rendered


def test_public_operation_verification_omits_internal_ids_and_refs() -> None:
    verification = build_operation_verification(
        _agent(),
        [
            {
                "tool": "remember",
                "call_id": "private-call",
                "operation_id": "private-operation",
                "ok": False,
                "handler_executed": True,
                "effect_outcome": "unknown",
                "effect_source_ref": "/private/owner/memory.jsonl",
                "tool_operation_status": "unknown",
                "parameters": {"action": "remove"},
            }
        ],
    )

    public = public_operation_verification(verification)
    serialized = json.dumps(public, ensure_ascii=False)

    assert public["status"] == "uncertain"
    assert public["groups"][0] == {
        "tool": "remember",
        "action": "remove",
        "label": "remember/remove",
        "status": "unknown",
        "count": 1,
        "replayed": False,
    }
    assert "private-call" not in serialized
    assert "private-operation" not in serialized
    assert "/private/owner" not in serialized


def test_public_operation_verification_is_idempotent() -> None:
    verification = build_operation_verification(
        _agent(),
        [
            {
                "tool": "remember",
                "call_id": "private-call",
                "operation_id": "private-operation",
                "ok": True,
                "handler_executed": True,
                "effect_outcome": "succeeded",
                "tool_operation_status": "succeeded",
                "parameters": {"action": "remove"},
            }
        ],
    )

    first = public_operation_verification(verification)
    second = public_operation_verification(first)

    assert second == first
    assert second["groups"][0]["label"] == "remember/remove"


def test_operation_verification_bounds_detail_without_losing_totals() -> None:
    records = [
        {
            "tool": "remember",
            "call_id": f"call-{index}",
            "operation_id": f"operation-{index}",
            "ok": True,
            "handler_executed": True,
            "effect_outcome": "succeeded",
            "tool_operation_status": "succeeded",
            "parameters": {"action": "add"},
        }
        for index in range(70)
    ]

    verification = build_operation_verification(_agent(), records)
    rendered = append_operation_verification("完成。", verification)

    assert verification["operation_count"] == 70
    assert verification["omitted_operation_count"] == 6
    assert len(verification["operations"]) == 64
    assert "另有 6 项较早操作" in rendered


def test_plain_chat_has_no_program_generated_operation_footer() -> None:
    verification = build_operation_verification(_agent(), [])

    assert append_operation_verification("只是聊聊天。", verification) == "只是聊聊天。"


class _RememberListThenFalseClaimBackend:
    name = "remember-list-then-false-claim"
    context_window_tokens = 200_000

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, _prompt: str, on_chunk=None, tools=None, messages=None) -> ModelResponse:
        del on_chunk, tools, messages
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text=(
                    '[TOOL_CALL]\n{"tool":"remember","action":"list"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        return ModelResponse(text="我已经删除了记忆。", backend=self.name)


def test_agent_run_appends_authoritative_action_without_parsing_false_prose(
    tmp_path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            tool_protocol="text",
            prompt_files=[],
            my_agent_home=str(tmp_path / "home"),
        ),
        tmp_path / "workspace",
    )
    backend = _RememberListThenFalseClaimBackend()
    agent.backend = backend

    result = agent.run(
        "只列出记忆，不要删除。",
        save=False,
        request_id="request-operation-verification",
        run_id="run-operation-verification",
        task_id="task-operation-verification",
    )

    assert backend.calls == 2
    assert "我已经删除了记忆。" in result.response
    assert "- remember/list：成功" in result.response
    assert "remember/remove" not in result.response
    assert result.operation_verification["status"] == "succeeded"


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
