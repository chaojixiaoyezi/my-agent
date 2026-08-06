from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.prompting_parts import PromptBuilder, ToolSections
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling import BaseTool, ToolHandlerOutcome
from agent_py_agent.agent.tooling.operation_verification import (
    build_operation_verification,
    incomplete_final_mutation_facts,
    public_operation_verification,
    redact_executed_operation_labels,
    render_current_turn_execution_facts,
)
from agent_py_agent.tests._tool_runtime_harness import (
    make_test_model_spec,
    make_test_runtime_policy,
    runtime_snapshot_for_tools,
)


class _ProjectionTool(BaseTool):
    def __init__(self, *, name: str, mutating: bool) -> None:
        properties = {}
        required = []
        effect_by_parameter = ()
        if name == "remember":
            properties = {
                "action": {
                    "type": "string",
                    "enum": ["add", "list", "replace", "remove"],
                }
            }
            required = ["action"]
            effect_by_parameter = (("action", (("list", "read_only"),)),)
        self.model_spec = make_test_model_spec(
            name,
            input_schema={
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        )
        self.runtime_policy = make_test_runtime_policy(
            "mutating" if mutating else "read_only",
            effect_by_parameter=effect_by_parameter,
        )

    def execute(self, params):
        return ToolHandlerOutcome(self.model_spec.name, True, str(params))


def _agent() -> SimpleNamespace:
    tools = {
        "remember": _ProjectionTool(name="remember", mutating=True),
        "read_file": _ProjectionTool(name="read_file", mutating=False),
    }
    snapshot = runtime_snapshot_for_tools(tools)
    return SimpleNamespace(
        tools=SimpleNamespace(runtime_snapshot=lambda: snapshot),
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


def test_final_operation_verification_stays_structured_and_hides_protocol_labels() -> None:
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
    rendered = redact_executed_operation_labels(
        "remember/list 已完成，remember/remove 没有执行。",
        verification,
    )

    assert verification["status"] == "failed"
    assert verification["counts"]["succeeded"] == 0
    assert verification["counts"]["not_started"] == 1
    assert "remember/list" in rendered
    assert "remember/remove" not in rendered
    assert rendered == "remember/list 已完成，相关操作 没有执行。"


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
    assert verification["operation_count"] == 1
    assert verification["operations"][0]["attempt_count"] == 2
    assert verification["operations"][0]["replayed"] is True


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
    assert verification["operation_count"] == 70
    assert verification["omitted_operation_count"] == 6
    assert len(verification["operations"]) == 64


def test_plain_chat_without_operations_is_unchanged() -> None:
    verification = build_operation_verification(_agent(), [])

    assert redact_executed_operation_labels("只是聊聊天。", verification) == "只是聊聊天。"


def test_only_an_unsuccessful_terminal_mutation_requires_reply_rewrite() -> None:
    failed = {
        "tool": "remember",
        "call_id": "call-failed",
        "operation_id": "operation-failed",
        "ok": False,
        "handler_executed": True,
        "tool_operation_status": "failed",
        "effect_outcome": "failed",
        "error_code": "MEMORY_WRITE_FAILED",
        "parameters": {"action": "add"},
    }
    facts = incomplete_final_mutation_facts(_agent(), [failed])

    assert facts["latest_mutating_operation"] == {
        "tool": "remember",
        "action": "add",
        "status": "failed",
        "handler_executed": True,
        "error_code": "MEMORY_WRITE_FAILED",
        "effect_outcome": "failed",
    }
    assert facts["operation_verification"]["status"] == "failed"

    succeeded = {
        **failed,
        "call_id": "call-succeeded",
        "operation_id": "operation-succeeded",
        "ok": True,
        "tool_operation_status": "succeeded",
        "effect_outcome": "succeeded",
        "error_code": "",
    }
    assert incomplete_final_mutation_facts(_agent(), [failed, succeeded]) == {}


class _AlwaysFailMutationTool(BaseTool):
    model_spec = make_test_model_spec(
        "always_fail_mutation",
        description="Fail one mutation for response-integrity testing.",
    )
    runtime_policy = make_test_runtime_policy("mutating")

    def execute(self, params):
        assert params == {}
        return ToolHandlerOutcome(
            self.model_spec.name,
            False,
            '{"ok":false}',
            error_code="TOOL_INVALID_ARGUMENTS",
            effect_outcome="not_started",
        )


class _FailedMutationFalseClaimBackend:
    name = "failed-mutation-false-claim"
    context_window_tokens = 200_000

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None, tools=None, messages=None) -> ModelResponse:
        del on_chunk, tools, messages
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text=(
                    '[TOOL_CALL]\n{"tool":"always_fail_mutation"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        if self.calls == 2:
            return ModelResponse(text="已经成功完成修改。", backend=self.name)
        assert "[natural-user-reply]" in prompt
        assert "operation_incomplete" in prompt
        return ModelResponse(text="这次修改实际没有成功，当前请求尚未完成。", backend=self.name)


def test_failed_final_mutation_discards_false_success_draft(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            tool_protocol="text",
            prompt_files=[],
            my_agent_home=str(tmp_path / "home"),
        ),
        tmp_path / "workspace",
    )
    agent.tools.register(_AlwaysFailMutationTool())
    backend = _FailedMutationFalseClaimBackend()
    agent.backend = backend

    result = agent.run(
        "执行一次测试修改。",
        save=False,
        request_id="request-failed-mutation",
        run_id="run-failed-mutation",
        task_id="task-failed-mutation",
    )

    assert backend.calls == 3
    assert "已经成功完成修改" not in result.response
    assert "实际没有成功" in result.response
    assert result.runtime_status == "unfinished"
    assert result.runtime_reason == "OPERATION_INCOMPLETE"
    assert result.operation_verification["status"] == "failed"


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


def test_agent_run_keeps_operation_proof_out_of_model_authored_prose(
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
    assert result.response == "我已经删除了记忆。"
    assert "操作核验" not in result.response
    assert result.operation_verification["status"] == "none"
    assert result.operation_verification["operations"] == []


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
