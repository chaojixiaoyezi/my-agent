from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.prompting_parts import PromptBuilder, ToolSections
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling import BaseTool, ToolHandlerOutcome
from agent_py_agent.agent.tooling.operation_verification import (
    build_operation_verification,
    incomplete_final_mutation_facts,
    post_failure_workspace_mutation_followup_facts,
    post_failure_workspace_mutation_followup_signature,
    prior_unresolved_mutation_facts,
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
    def __init__(
        self,
        *,
        name: str,
        mutating: bool,
        mutates_workspace: bool = False,
    ) -> None:
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
        self.runtime_policy = replace(
            make_test_runtime_policy(
                "mutating" if mutating else "read_only",
                effect_by_parameter=effect_by_parameter,
            ),
            mutates_workspace=mutates_workspace,
        )

    def execute(self, params):
        return ToolHandlerOutcome(self.model_spec.name, True, str(params))


def _agent() -> SimpleNamespace:
    tools = {
        "remember": _ProjectionTool(name="remember", mutating=True),
        "read_file": _ProjectionTool(name="read_file", mutating=False),
        "write_file": _ProjectionTool(
            name="write_file",
            mutating=True,
            mutates_workspace=True,
        ),
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


def test_not_started_tail_keeps_prior_succeeded_effect_authoritative() -> None:
    succeeded = {
        "tool": "remember",
        "call_id": "call-succeeded",
        "operation_id": "operation-succeeded",
        "ok": True,
        "handler_executed": True,
        "tool_operation_status": "succeeded",
        "effect_outcome": "succeeded",
        "parameters": {"action": "add"},
    }
    blocked = {
        "tool": "remember",
        "call_id": "call-blocked",
        "operation_id": "operation-blocked",
        "ok": False,
        "handler_executed": False,
        "failure_stage": "authorization",
        "error_code": "ACTION_BLOCKED",
        "effect_outcome": "not_started",
        "parameters": {"action": "add"},
    }

    verification = build_operation_verification(_agent(), [succeeded, blocked])

    assert verification["status"] == "partial"
    assert verification["counts"]["succeeded"] == 1
    assert verification["counts"]["not_started"] == 1
    assert incomplete_final_mutation_facts(_agent(), [succeeded, blocked]) == {}
    assert incomplete_final_mutation_facts(_agent(), [blocked])[
        "latest_mutating_operation"
    ]["status"] == "not_started"


def test_unknown_tail_still_overrides_prior_succeeded_effect() -> None:
    succeeded = {
        "tool": "remember",
        "call_id": "call-succeeded",
        "operation_id": "operation-succeeded",
        "ok": True,
        "handler_executed": True,
        "tool_operation_status": "succeeded",
        "effect_outcome": "succeeded",
        "parameters": {"action": "add"},
    }
    unknown = {
        "tool": "remember",
        "call_id": "call-unknown",
        "operation_id": "operation-unknown",
        "ok": False,
        "handler_executed": True,
        "tool_operation_status": "unknown",
        "effect_outcome": "unknown",
        "parameters": {"action": "add"},
    }

    facts = incomplete_final_mutation_facts(_agent(), [succeeded, unknown])

    assert facts["latest_mutating_operation"]["status"] == "unknown"


def test_prior_unknown_effect_survives_a_later_different_success() -> None:
    unknown = {
        "tool": "remember",
        "call_id": "call-unknown",
        "operation_id": "operation-unknown",
        "ok": False,
        "handler_executed": True,
        "tool_operation_status": "unknown",
        "effect_outcome": "unknown",
        "error_code": "TOOL_OPERATION_OUTCOME_UNKNOWN",
        "parameters": {"action": "add"},
    }
    succeeded = {
        "tool": "remember",
        "call_id": "call-succeeded",
        "operation_id": "operation-succeeded",
        "ok": True,
        "handler_executed": True,
        "tool_operation_status": "succeeded",
        "effect_outcome": "succeeded",
        "parameters": {"action": "replace"},
    }

    facts = prior_unresolved_mutation_facts(_agent(), [unknown, succeeded])

    assert facts["schema"] == "prior_unresolved_mutation.v1"
    assert facts["unresolved_operation_count"] == 1
    assert facts["later_succeeded_operation_count"] == 1
    assert facts["counts"]["unknown"] == 1
    assert facts["unresolved_operations"][0]["call_id"] == "call-unknown"
    assert facts["operation_verification"]["status"] == "uncertain"
    assert prior_unresolved_mutation_facts(_agent(), [succeeded, unknown]) == {}


def test_failed_call_then_final_workspace_write_requests_one_soft_followup() -> None:
    records = [
        {
            "tool": "read_file",
            "call_id": "call-failed-check",
            "ok": False,
            "handler_executed": True,
            "error_code": "CHECK_FAILED",
            "parameters": {},
        },
        {
            "tool": "write_file",
            "call_id": "call-fix",
            "operation_id": "operation-fix",
            "ok": True,
            "handler_executed": True,
            "tool_operation_status": "succeeded",
            "parameters": {},
        },
    ]

    facts = post_failure_workspace_mutation_followup_facts(_agent(), records)

    assert facts == {
        "schema": "post_failure_workspace_mutation_followup.v1",
        "latest_workspace_mutation": {"tool": "write_file", "status": "succeeded"},
        "prior_failed_call_count": 1,
        "latest_prior_failure": {
            "tool": "read_file",
            "status": "failed",
            "error_code": "CHECK_FAILED",
        },
        "tool_record_after_latest_mutation": False,
    }


def test_simple_workspace_write_and_post_write_check_do_not_request_followup() -> None:
    simple_write = {
        "tool": "write_file",
        "call_id": "call-write",
        "operation_id": "operation-write",
        "ok": True,
        "handler_executed": True,
        "tool_operation_status": "succeeded",
        "parameters": {},
    }
    failed = {
        "tool": "read_file",
        "call_id": "call-failed",
        "ok": False,
        "handler_executed": True,
        "parameters": {},
    }
    check = {
        "tool": "read_file",
        "call_id": "call-check",
        "ok": True,
        "handler_executed": True,
        "parameters": {},
    }

    assert post_failure_workspace_mutation_followup_facts(_agent(), [simple_write]) == {}
    assert (
        post_failure_workspace_mutation_followup_facts(
            _agent(),
            [failed, simple_write, check],
        )
        == {}
    )


def test_stale_verification_survives_reads_until_a_later_real_check() -> None:
    root = "/tmp/project"
    failed_check = {
        "tool": "run_command",
        "call_id": "call-test-failed",
        "ok": False,
        "handler_executed": True,
        "error_code": "COMMAND_FAILED",
        "parameters": {"command": "go test -v ."},
        "tool_result_envelope": {
            "verification_evidence": {
                "id": 41,
                "root": root,
                "status": "failed",
            }
        },
    }
    fixed = {
        "tool": "write_file",
        "call_id": "call-fix",
        "operation_id": "operation-fix",
        "ok": True,
        "handler_executed": True,
        "tool_operation_status": "succeeded",
        "parameters": {"path": f"{root}/example.go"},
        "tool_result_envelope": {
            "verification_state": [
                {
                    "root": root,
                    "status": "stale",
                    "last_verification_id": 41,
                    "last_verification_status": "failed",
                    "changed_paths": [f"{root}/example.go"],
                }
            ]
        },
    }
    read_after_fix = {
        "tool": "search_text",
        "call_id": "call-read",
        "ok": True,
        "handler_executed": True,
        "parameters": {"query": "func"},
    }

    facts = post_failure_workspace_mutation_followup_facts(
        _agent(),
        [failed_check, fixed, read_after_fix],
    )

    assert facts["schema"] == "post_failure_workspace_mutation_followup.v2"
    assert facts["tool_record_count_after_latest_mutation"] == 1
    assert facts["stale_workspace_verification"] == [
        {
            "root": root,
            "status": "stale",
            "last_verification_id": 41,
            "last_verification_status": "failed",
            "changed_path_count": 1,
        }
    ]
    assert post_failure_workspace_mutation_followup_signature(facts).startswith(
        "verification-cycle:"
    )

    passed_check = {
        "tool": "run_command",
        "call_id": "call-test-passed",
        "ok": True,
        "handler_executed": True,
        "parameters": {"command": "go test -v ."},
        "tool_result_envelope": {
            "verification_evidence": {
                "id": 42,
                "root": root,
                "status": "passed",
            }
        },
    }
    assert (
        post_failure_workspace_mutation_followup_facts(
            _agent(),
            [failed_check, fixed, read_after_fix, passed_check],
        )
        == {}
    )


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
            error_code="COMMAND_FAILED",
            effect_outcome="failed",
        )


# LLM: This fake mutating tool proves that a completion-conflict continuation
# still exposes the original tool surface and can establish a new succeeded
# terminal effect in the same active turn.
# 函数用途: 测试模型收到收口冲突后能继续调用工具，并用新的成功终态完成返工。
class _RepairMutationTool(BaseTool):
    model_spec = make_test_model_spec(
        "repair_mutation",
        description="Repair one failed mutation for completion-conflict testing.",
    )
    runtime_policy = make_test_runtime_policy("mutating")

    def execute(self, params):
        assert params == {}
        return ToolHandlerOutcome(self.model_spec.name, True, '{"ok":true}')


# LLM: Provider-facing assertions must inspect both the prompt and native
# message projection because runtime guidance follows the active protocol.
# 函数用途: 汇总 fake backend 本轮真正可见的提示，兼容文本和原生工具协议测试。
def _model_visible_text(prompt: str, messages: object) -> str:
    return prompt + "\n" + json.dumps(messages or [], ensure_ascii=False, sort_keys=True)


class _FailedMutationFalseClaimBackend:
    name = "failed-mutation-false-claim"
    context_window_tokens = 200_000

    def probe_tool_capability(self):
        from agent_py_agent.agent.backends.base import ProviderToolCapability, _utc_now_iso

        return ProviderToolCapability(
            provider=str(self.name or "test"), endpoint="local://test-backend",
            model="", stream=False, native_supported=True,
            evidence="test_backend_declares_native_tools",
            observed_at=_utc_now_iso(),
        )

    def __init__(self) -> None:
        self.calls = 0
        self.repair_turn_tool_counts: list[int] = []

    def generate(self, prompt: str, on_chunk=None, tools=None, messages=None, **kwargs) -> ModelResponse:
        del on_chunk
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text="",
                backend=self.name,
                tool_use_blocks=[{
                    "id": "call-mutation-1",
                    "name": "always_fail_mutation",
                    "input": {},
                }],
            )
        if self.calls in {2, 3, 4}:
            if self.calls >= 3:
                visible = _model_visible_text(prompt, messages)
                assert "completion_conflict.v1" in visible
                assert f'\\"repair_attempt\\":{self.calls - 2}' in visible
                assert "已经成功完成修改" in visible
                self.repair_turn_tool_counts.append(len(list(tools or [])))
            return ModelResponse(text="已经成功完成修改。", backend=self.name)
        assert "[natural-user-reply]" in prompt
        assert "operation_incomplete" in prompt
        return ModelResponse(text="这次修改实际没有成功，当前请求尚未完成。", backend=self.name)


def test_failed_final_mutation_gets_bounded_repair_before_fallback(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
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

    assert backend.calls == 5
    assert all(count > 0 for count in backend.repair_turn_tool_counts)
    assert "已经成功完成修改" not in result.response
    assert "实际没有成功" in result.response
    assert result.runtime_status == "unfinished"
    assert result.runtime_reason == "OPERATION_INCOMPLETE"
    assert result.operation_verification["status"] == "failed"


class _FailedMutationRepairBackend(_FailedMutationFalseClaimBackend):
    name = "failed-mutation-repair"

    def generate(self, prompt: str, on_chunk=None, tools=None, messages=None, **kwargs) -> ModelResponse:
        del on_chunk
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text="",
                backend=self.name,
                tool_use_blocks=[{
                    "id": "call-mutation-failed",
                    "name": "always_fail_mutation",
                    "input": {},
                }],
            )
        if self.calls == 2:
            return ModelResponse(text="已经成功完成修改。", backend=self.name)
        if self.calls == 3:
            visible = _model_visible_text(prompt, messages)
            assert "completion_conflict.v1" in visible
            assert '\\"repair_attempt\\":1' in visible
            assert "已经成功完成修改" in visible
            available = {
                str(item.get("name") or item.get("function", {}).get("name") or "")
                for item in list(tools or [])
                if isinstance(item, dict)
            }
            assert "repair_mutation" in available
            return ModelResponse(
                text="",
                backend=self.name,
                tool_use_blocks=[{
                    "id": "call-mutation-repair",
                    "name": "repair_mutation",
                    "input": {},
                }],
            )
        if self.calls == 4:
            return ModelResponse(text="修复和复验已经完成。", backend=self.name)
        assert self.calls == 5
        visible = _model_visible_text(prompt, messages)
        assert "prior_unresolved_reconciliation.v1" in visible
        assert "修复和复验已经完成" in visible
        return ModelResponse(text="修复和复验已经完成。", backend=self.name)


def test_failed_final_mutation_can_repair_with_tools_in_same_active_turn(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            prompt_files=[],
            my_agent_home=str(tmp_path / "home"),
        ),
        tmp_path / "workspace",
    )
    agent.tools.register(_AlwaysFailMutationTool())
    agent.tools.register(_RepairMutationTool())
    backend = _FailedMutationRepairBackend()
    agent.backend = backend

    result = agent.run(
        "执行一次测试修改，失败后自行修复。",
        save=False,
        request_id="request-failed-mutation-repair",
        run_id="run-failed-mutation-repair",
        task_id="task-failed-mutation-repair",
    )

    assert backend.calls == 5
    assert result.response == "修复和复验已经完成。"
    assert result.runtime_status == "ok"
    assert result.operation_verification["status"] == "partial"
    assert result.operation_verification["counts"]["succeeded"] == 1
    assert result.operation_verification["counts"]["failed"] == 1


class _RememberListThenFalseClaimBackend:
    name = "remember-list-then-false-claim"
    context_window_tokens = 200_000

    def probe_tool_capability(self):
        from agent_py_agent.agent.backends.base import ProviderToolCapability, _utc_now_iso

        return ProviderToolCapability(
            provider=str(self.name or "test"), endpoint="local://test-backend",
            model="", stream=False, native_supported=True,
            evidence="test_backend_declares_native_tools",
            observed_at=_utc_now_iso(),
        )

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, _prompt: str, on_chunk=None, tools=None, messages=None, **kwargs) -> ModelResponse:
        del on_chunk, tools, messages
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text="",
                tool_use_blocks=[{
                    "id": "call-remember-list-1",
                    "name": "remember",
                    "input": {"action": "list"},
                }],
                backend=self.name,
            )
        return ModelResponse(text="我已经删除了记忆。", backend=self.name)


def test_agent_run_keeps_operation_proof_out_of_model_authored_prose(
    tmp_path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
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
