"""W0 runtime contracts: effect recovery, validator diagnostics, alias migration, action ceilings, native recommendation dedup, and O(1) snapshot index."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.action_protocol import RunScope
from agent_py_agent.agent.agent_core._tool_loop_service import (
    _dedup_native_recommendations_section,
    _mark_unknown_outcome_halt,
)
from agent_py_agent.agent.agent_core.orchestration.tool_grants import (
    CODING_SUBAGENT_TOOLS,
    READ_ONLY_SUBAGENT_TOOLS,
    subagent_allowed_tools,
)
from agent_py_agent.agent.agent_core.parameters import ONE_SHOT_TOOL_NAMES
from agent_py_agent.agent.agent_core.planner_service import PARENT_PLANNER_READ_TOOLS
from agent_py_agent.agent.contracts.error_taxonomy import error_contract as get_error_contract
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.local_storage.tool_operations import (
    TOOL_OPERATION_RUNNING,
    TOOL_OPERATION_SUCCEEDED,
    TOOL_OPERATION_UNKNOWN,
    ToolOperationClaimRequest,
    ToolOperationCompletionRequest,
    new_tool_operation_holder,
)
from agent_py_agent.agent.prompting_parts.builder import PromptBuildRequest, ToolSections
from agent_py_agent.agent.subagents.capability_scope import (
    DIRECT_PARENT_TOOL_AUTHORITY_ATTR,
    creation_tool_authority_snapshot,
    direct_parent_tool_authority,
)
from agent_py_agent.agent.subagents.model_task import SubAgentTask
from agent_py_agent.agent.subagents.role_contracts import apply_role_contract_to_create_params
from agent_py_agent.agent.subagents.role_templates import COORDINATOR_TOOLS
from agent_py_agent.agent.subagents.services.hierarchy.tool_policy import (
    ToolPolicyRequest,
    scheduled_child_tools,
)
from agent_py_agent.agent.tooling import registry as registry_module
from agent_py_agent.agent.tooling.executor import (
    ToolExecutor,
    ToolExecutorRequest,
    _effect_from_operation_status,
)
from agent_py_agent.agent.tooling.models import (
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ToolAvailability,
    ToolHandlerOutcome,
    ToolInputPolicy,
    ToolInvocationContext,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntime,
    ToolRuntimePolicy,
    ToolRuntimeSnapshot,
    TrustedParameterBinding,
)
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.agent.tooling.registry_auth import (
    action_ceilings,
    creation_snapshot_tool_grants,
    intersect_tool_grants,
    normalize_tool_grants,
    snapshot_tool_grants,
)
from agent_py_agent.agent.tooling.runtime_contracts import (
    ProviderToolCapability,
    ToolCall,
    ToolProtocolSnapshot,
    ToolResult,
)
from agent_py_agent.agent.tooling.versioned_input_decoder import (
    business_domain_actions,
    decode_retired_tool_call,
    retired_tool_aliases,
)
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_test_call,
    make_test_model_spec,
    make_test_runtime_policy,
)


def _make_registry(
    tmp_path: Path,
    *,
    store: LocalStore | None = None,
    disabled_tools: list[str] | None = None,
    deferred_categories: list[str] | None = None,
) -> ToolRegistry:
    return ToolRegistry(
        ToolRegistryParams(
            workspace_root=tmp_path,
            max_chars=10000,
            max_entries=100,
            max_matches=100,
            web_max_chars=10000,
            http_timeout=1,
            catalog_limit=100,
            retrieval_limit=10,
            vector_search_enabled=False,
            disabled_tools=disabled_tools or [],
            catalog_deferred_categories=deferred_categories or [],
            operation_store=store,
            operation_store_required=store is not None,
            operation_owner_id="owner-w0",
        )
    )


class _MutatingProbeTool(BaseTool):
    def __init__(
        self,
        name: str = "mutating_probe",
        *,
        outcome: ToolHandlerOutcome | Exception | None = None,
        validator: object | None = None,
        internal_parameters: tuple[str, ...] = (),
    ) -> None:
        self.calls = 0
        self._outcome = outcome
        self._validator = validator
        props: dict[str, object] = {
            "target_id": {"type": "string"},
            "run_id": {"type": "string"},
        }
        if "_internal_token" not in internal_parameters:
            props["_internal_token"] = {"type": "string"}
        self.model_spec = make_test_model_spec(
            name,
            description="执行受控写入探针并返回结构化回执。",
            input_schema={
                "type": "object",
                "properties": props,
                "required": ["target_id"],
                "additionalProperties": False,
            },
        )
        base_policy = make_test_runtime_policy("mutating")
        self.runtime_policy = replace(
            base_policy,
            idempotency_policy=IdempotencyPolicy(scope="operation"),
            input_policy=ToolInputPolicy(
                internal_parameters=internal_parameters,
                trusted_parameter_bindings=(
                    (
                        "run_id",
                        TrustedParameterBinding(
                            source_refs=("run_scope.run_id",),
                            authority="must_match",
                        ),
                    ),
                ),
            ),
        )

    def validate_invocation(
        self,
        params: dict[str, object],
        context: ToolInvocationContext,
    ) -> ToolHandlerOutcome | None:
        if callable(self._validator):
            return self._validator(params, context)
        return None

    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        self.calls += 1
        if isinstance(self._outcome, Exception):
            raise self._outcome
        if self._outcome is not None:
            return self._outcome
        return ToolHandlerOutcome(
            self.model_spec.name,
            True,
            json.dumps({"saved": params["target_id"]}),
        )


class _TransientReadProbeTool(BaseTool):
    runtime_policy = make_test_runtime_policy("read_only")

    def __init__(self, name: str, codes: list[str]) -> None:
        self.calls = 0
        self._codes = list(codes)
        self.model_spec = make_test_model_spec(
            name,
            description="读取受控快照状态。",
            input_schema={
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
                "additionalProperties": False,
            },
        )

    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        idx = min(self.calls, len(self._codes) - 1)
        self.calls += 1
        code = self._codes[idx]
        if not code:
            return ToolHandlerOutcome(self.model_spec.name, True, f"ok:{params['key']}")
        effect = "unknown" if code == "TOOL_OPERATION_OUTCOME_UNKNOWN" else "not_started"
        return ToolHandlerOutcome(
            self.model_spec.name,
            False,
            f"simulated read error: {code}",
            error_code=code,
            effect_outcome=effect,
            retryable=True,
        )


class _DomainMockTool(BaseTool):
    def __init__(self, domain_name: str = "business_execution", *, inspect_hook=None) -> None:
        self.calls: list[dict[str, object]] = []
        self.narrow_calls: list[tuple[str, ...]] = []
        self._inspect_hook = inspect_hook
        actions = list(business_domain_actions()[domain_name])
        self._full_actions = tuple(actions)
        one_of = [
            {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "const": act},
                    "payload": {"$ref": f"#/$defs/def_{act.replace('.', '_')}"},
                },
                "required": ["action"],
            }
            for act in actions
        ]
        defs = {
            f"def_{act.replace('.', '_')}": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "self_link": {"$ref": f"#/$defs/def_{act.replace('.', '_')}"},
                },
            }
            for act in actions
        }
        defs["def_unused"] = {"type": "object", "properties": {"orphan": {"type": "string"}}}
        self.model_spec = ToolModelSpec(
            name=domain_name,
            description="统一业务执行域工具。",
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": actions},
                    "title": {"type": "string"},
                    "task_id": {"type": "string"},
                    "payload": {"type": "object"},
                },
                "required": ["action"],
                "oneOf": one_of,
                "$defs": defs,
                "additionalProperties": True,
            },
            hints=ToolModelHints(
                category="business",
                keywords=("业务任务", "项目里程碑"),
                examples=(
                    json.dumps({"action": actions[0], "title": "示例A"}, ensure_ascii=False),
                    json.dumps({"action": actions[1], "task_id": "t-1"}, ensure_ascii=False),
                ),
            ),
        )
        self.runtime_policy = ToolRuntimePolicy(
            effect_resolver=EffectResolverPolicy(
                default_effect="mutating",
                by_parameter=(
                    (
                        "action",
                        tuple(
                            (act, "read_only" if act.endswith((".list", ".get")) else "mutating")
                            for act in actions
                        ),
                    ),
                ),
            ),
            idempotency_policy=IdempotencyPolicy(scope="operation"),
        )
        if inspect_hook is not None:
            self.inspect_operation_receipt = inspect_hook

    def operation_identity_for(self, arguments: dict[str, object]) -> str:
        act = str(arguments.get("action") or "").strip()
        for alias_name, (target, mapped_act) in retired_tool_aliases().items():
            if target == self.model_spec.name and mapped_act == act:
                return alias_name
        return self.model_spec.name

    def model_spec_for_actions(self, actions: frozenset[str]) -> ToolModelSpec:
        ordered = [act for act in self._full_actions if act in actions]
        self.narrow_calls.append(tuple(ordered))
        updated_schema = json.loads(json.dumps(self.model_spec.input_schema))
        updated_schema["properties"]["action"]["enum"] = ordered
        return replace(self.model_spec, schema_hash="", input_schema=updated_schema)

    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        self.calls.append(dict(params))
        act = str(params.get("action") or "")
        if params.get("title") == "simulate_unknown":
            return ToolHandlerOutcome(
                self.model_spec.name,
                False,
                "receipt pending",
                error_code="TOOL_OPERATION_OUTCOME_UNKNOWN",
                reported_error_code="STORE_BUSY",
                effect_outcome="unknown",
                retryable=True,
            )
        return ToolHandlerOutcome(
            self.model_spec.name,
            True,
            json.dumps({"ok": True, "action": act, "title": params.get("title", "")}),
        )


def test_unknown_effect_blocks_retry_every_projection(tmp_path: Path) -> None:
    outcome = ToolHandlerOutcome(
        "mutating_probe",
        False,
        "state uncertain after simulated I/O error",
        error_code="TOOL_OPERATION_OUTCOME_UNKNOWN",
        reported_error_code="TOOL_TIMEOUT",
        effect_outcome="unknown",
        retryable=True,
    )
    assert outcome.retryable is False

    store = LocalStore(tmp_path / "ops.db", enable_fts=False)
    probe = _MutatingProbeTool(outcome=outcome)
    registry = _make_registry(tmp_path, store=store)
    registry.register(probe)
    try:
        snapshot = registry.runtime_snapshot(run_id="run-unk")
        call = canonical_test_call(
            snapshot,
            "mutating_probe",
            {"target_id": "item-1"},
            call_id="call-unk-1",
            attempt_id="att-unk",
            operation_id="op-unk-1",
            idempotency_key="idem-unk-1",
        )
        direct_result = ToolResult(
            call_id=call.call_id,
            tool_name=call.tool_name,
            status="failed",
            error_code="TOOL_TIMEOUT",
            effect_outcome="unknown",
            retryable=True,
        )
        assert direct_result.retryable is False
        assert direct_result.to_dict()["retryable"] is False

        first = registry.execute_tool(call, write_boundary=None, runtime_snapshot=snapshot).result
        assert first.effect_outcome == "unknown"
        assert first.retryable is False
        assert first.to_dict()["retryable"] is False
        assert probe.calls == 1

        # Tamper saved SQLite JSON with retryable=True to verify rehydration enforces non-retryable.
        with sqlite3.connect(tmp_path / "ops.db") as conn:
            row = conn.execute(
                "SELECT result_json FROM tool_operations WHERE owner_id=? AND run_id=? AND operation_id=?",
                ("owner-w0", "run-unk", "op-unk-1"),
            ).fetchone()
            saved = json.loads(row[0])
            saved["retryable"] = True
            conn.execute(
                "UPDATE tool_operations SET result_json=? WHERE owner_id=? AND run_id=? AND operation_id=?",
                (json.dumps(saved), "owner-w0", "run-unk", "op-unk-1"),
            )
            conn.commit()

        replayed = registry.execute_tool(call, write_boundary=None, runtime_snapshot=snapshot).result
        assert replayed.retryable is False
        assert probe.calls == 1

        # Read-only retry requires host-typed transient condition; PATH_NOT_FOUND & unknown never auto-retry.
        transient_read = _TransientReadProbeTool("transient_read", ["TOOL_TIMEOUT", ""])
        non_transient_read = _TransientReadProbeTool("non_transient_read", ["PATH_NOT_FOUND", ""])
        unknown_read = _TransientReadProbeTool("unknown_read", ["TOOL_OPERATION_OUTCOME_UNKNOWN", ""])
        for item in (transient_read, non_transient_read, unknown_read):
            registry.register(item)
        snap2 = registry.runtime_snapshot(run_id="run-read-retry")
        r_ok = registry.execute_tool(
            canonical_test_call(snap2, "transient_read", {"key": "a"}),
            write_boundary=None,
            runtime_snapshot=snap2,
        ).result
        assert r_ok.ok is True and transient_read.calls == 2

        r_path = registry.execute_tool(
            canonical_test_call(snap2, "non_transient_read", {"key": "b"}),
            write_boundary=None,
            runtime_snapshot=snap2,
        ).result
        assert r_path.ok is False and non_transient_read.calls == 1

        r_unk = registry.execute_tool(
            canonical_test_call(snap2, "unknown_read", {"key": "c"}),
            write_boundary=None,
            runtime_snapshot=snap2,
        ).result
        assert r_unk.ok is False and r_unk.retryable is False and unknown_read.calls == 1
    finally:
        registry.close_mcp_clients()


def test_validator_specific_codes_and_details(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "val.db", enable_fts=False)
    returned_fail = _MutatingProbeTool(
        "val_returned_fail",
        validator=lambda params, _ctx: ToolHandlerOutcome(
            "val_returned_fail",
            False,
            f"invalid target_id={params.get('target_id')}",
            error_code="",
            next_action="Supply a non-empty canonical target_id.",
        ),
    )
    raised_fail = _MutatingProbeTool(
        "val_raised_fail",
        validator=lambda _params, _ctx: (_ for _ in ()).throw(
            ValueError("target_id format invalid; token=sk-secret1234567890abcdef")
        ),
    )
    broken_contract = _MutatingProbeTool(
        "val_broken_contract",
        validator=lambda _params, _ctx: "not-a-tool-handler-outcome",
    )
    guarded_inputs = _MutatingProbeTool(
        "val_guarded_inputs",
        internal_parameters=("_internal_token",),
    )
    registry = _make_registry(tmp_path, store=store)
    for item in (returned_fail, raised_fail, broken_contract, guarded_inputs):
        registry.register(item)
    try:
        snapshot = registry.runtime_snapshot(
            allowed_tools=["val_returned_fail", "val_raised_fail", "val_broken_contract", "val_guarded_inputs"],
            run_id="run-val",
        )

        res_ret = registry.execute_tool(
            canonical_test_call(snapshot, "val_returned_fail", {"target_id": "bad"}, operation_id="op-v1"),
            write_boundary=None,
            runtime_snapshot=snapshot,
        ).result
        assert res_ret.error_code == "TOOL_INVOCATION_VALIDATOR_FAILED"
        assert res_ret.retryable is False
        assert res_ret.effect_outcome == "not_started"
        assert returned_fail.calls == 0
        assert res_ret.metadata["next_action"] == "Supply a non-empty canonical target_id."

        res_exc = registry.execute_tool(
            canonical_test_call(snapshot, "val_raised_fail", {"target_id": "bad"}, operation_id="op-v2"),
            write_boundary=None,
            runtime_snapshot=snapshot,
        ).result
        assert res_exc.error_code == "TOOL_INVOCATION_VALIDATOR_FAILED"
        assert res_exc.retryable is False
        assert raised_fail.calls == 0
        assert res_exc.metadata["error_details"]["exception_type"] == "ValueError"
        assert "sk-secret1234567890abcdef" not in json.dumps(res_exc.to_dict(), ensure_ascii=False)

        res_broken = registry.execute_tool(
            canonical_test_call(snapshot, "val_broken_contract", {"target_id": "bad"}, operation_id="op-v3"),
            write_boundary=None,
            runtime_snapshot=snapshot,
        ).result
        assert res_broken.error_code == "TOOL_INVOCATION_VALIDATOR_CONTRACT_BROKEN"
        assert res_broken.retryable is False
        assert broken_contract.calls == 0

        # Forbidden internal parameter & trusted context conflict carry exact field lists.
        res_internal = registry.execute_tool(
            canonical_test_call(
                snapshot,
                "val_guarded_inputs",
                {"target_id": "ok", "_internal_token": "internal"},
            ),
            write_boundary=None,
            runtime_snapshot=snapshot,
        ).result
        assert res_internal.error_code == "TOOL_INTERNAL_PARAMETER_FORBIDDEN"
        assert res_internal.metadata["error_details"]["forbidden_fields"] == ["_internal_token"]

        res_conflict = registry.execute_tool(
            canonical_test_call(
                snapshot,
                "val_guarded_inputs",
                {"target_id": "ok", "run_id": "conflicting-run"},
            ),
            write_boundary=None,
            runtime_snapshot=snapshot,
        ).result
        assert res_conflict.error_code == "TOOL_TRUSTED_PARAMETER_CONFLICT"
        assert res_conflict.metadata["error_details"]["conflict_fields"] == ["run_id"]

        # Decoder error preserved; typo suggests only authorized snapshot tools.
        base_call = canonical_test_call(snapshot, "val_guarded_inputs", {"target_id": "ok"})
        bad_decode_call = replace(base_call, call_id="c-dec")
        res_dec = (
            ToolExecutor()
            .execute(
                ToolExecutorRequest(
                    call=bad_decode_call,
                    runtime_snapshot=snapshot,
                    workspace_root=tmp_path,
                    trusted_run_context={"tool_input_version": 99},
                )
            )
            .result
        )
        assert res_dec.error_code == "TOOL_INVALID_ARGUMENTS"
        assert res_dec.metadata["error_details"]["decoder_error"]["exception_type"] == "ValueError"

        typo_call = replace(
            base_call,
            call_id="c-typo",
            tool_name="val_guarded_input",
            arguments={"target_id": "ok"},
        )
        res_typo = registry.execute_tool(typo_call, write_boundary=None, runtime_snapshot=snapshot).result
        assert res_typo.error_code == "TOOL_NOT_IN_RUNTIME_SNAPSHOT"
        suggested = res_typo.metadata["error_details"]["suggestions"]
        assert "val_guarded_inputs" in suggested
        assert set(suggested) <= set(snapshot.available_tool_names)
    finally:
        registry.close_mcp_clients()


def test_failure_status_does_not_prove_zero_effect(tmp_path: Path) -> None:
    for code in (
        "SERVICE_UNAVAILABLE",
        "CHANNEL_MESSAGES_UNAVAILABLE",
        "TOOL_POST_COMMIT_READBACK_FAILED",
        "TOOL_PARTIAL_COMMIT",
    ):
        contract = get_error_contract(code)
        assert contract.code == code
        assert contract.retryable is False

    assert _effect_from_operation_status({"status": "failed"}) == "unknown"
    assert _effect_from_operation_status({"status": "failed", "replayed": True}, handler_executed=False) == "unknown"
    assert (
        _effect_from_operation_status(
            {"status": "failed", "original_tool_execution": {"handler_executed": False}}
        )
        == "not_started"
    )
    assert _effect_from_operation_status({"status": "failed"}, handler_executed=False) == "not_started"

    store = LocalStore(tmp_path / "fail.db", enable_fts=False)
    raising_mutator = _MutatingProbeTool(
        "raising_mutator",
        outcome=OSError(5, "simulated disk write interruption"),
    )
    partial_mutator = _MutatingProbeTool(
        "partial_mutator",
        outcome=ToolHandlerOutcome(
            "partial_mutator",
            False,
            "primary row written; index update failed",
            error_code="TOOL_PARTIAL_COMMIT",
            reported_error_code="SQLITE_IOERR",
            effect_outcome="failed",
        ),
    )
    registry = _make_registry(tmp_path, store=store)
    registry.register(raising_mutator)
    registry.register(partial_mutator)
    try:
        snapshot = registry.runtime_snapshot(run_id="run-fail")
        res_exc = registry.execute_tool(
            canonical_test_call(
                snapshot,
                "raising_mutator",
                {"target_id": "t-1"},
                operation_id="op-raise-1",
                idempotency_key="idem-raise-1",
            ),
            write_boundary=None,
            runtime_snapshot=snapshot,
        ).result
        assert res_exc.effect_outcome == "unknown"
        assert res_exc.retryable is False
        assert res_exc.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
        assert res_exc.metadata["reported_error_code"] == "TOOL_RUNTIME_ERROR"
        saved = store.get_tool_operation(owner_id="owner-w0", run_id="run-fail", operation_id="op-raise-1")
        assert saved is not None and saved.status == TOOL_OPERATION_UNKNOWN

        res_partial = registry.execute_tool(
            canonical_test_call(
                snapshot,
                "partial_mutator",
                {"target_id": "t-2"},
                operation_id="op-part-1",
                idempotency_key="idem-part-1",
            ),
            write_boundary=None,
            runtime_snapshot=snapshot,
        ).result
        assert res_partial.effect_outcome == "failed"
        assert res_partial.error_code == "TOOL_PARTIAL_COMMIT"
        assert res_partial.metadata["reported_error_code"] == "SQLITE_IOERR"
        assert res_partial.retryable is False
    finally:
        registry.close_mcp_clients()


def test_next_action_survives_durable_replay(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "replay.db", enable_fts=False)
    ok_tool = _MutatingProbeTool(
        "advice_ok_mutator",
        outcome=ToolHandlerOutcome(
            "advice_ok_mutator",
            True,
            json.dumps({"created_id": "item-42"}),
            next_action="Call inspect_item with id=item-42; token=sk-1234567890abcdefghijklmnop " + ("x" * 2400),
        ),
    )
    fail_tool = _MutatingProbeTool(
        "advice_fail_mutator",
        outcome=ToolHandlerOutcome(
            "advice_fail_mutator",
            False,
            "parent container does not exist",
            error_code="PATH_NOT_FOUND",
            effect_outcome="not_started",
            retryable=True,
            next_action="Create the parent directory first before re-issuing with a new operation.",
        ),
    )
    registry = _make_registry(tmp_path, store=store)
    registry.register(ok_tool)
    registry.register(fail_tool)
    try:
        snapshot = registry.runtime_snapshot(run_id="run-adv")
        ok_call = canonical_test_call(
            snapshot,
            "advice_ok_mutator",
            {"target_id": "item-42"},
            operation_id="op-adv-ok",
            idempotency_key="idem-adv-ok",
        )
        first_ok = registry.execute_tool(ok_call, write_boundary=None, runtime_snapshot=snapshot).result
        replayed_ok = registry.execute_tool(ok_call, write_boundary=None, runtime_snapshot=snapshot).result
        assert ok_tool.calls == 1
        advice = first_ok.metadata["next_action"]
        assert len(advice) <= 2048
        assert "sk-1234567890abcdefghijklmnop" not in advice
        assert replayed_ok.metadata["next_action"] == advice
        assert "next_action" not in (first_ok.metadata.get("error_details") or {})
        rendered_ok = replayed_ok.render_for_prompt()
        assert rendered_ok.count("[tool-next-action]") == 1

        fail_call = canonical_test_call(
            snapshot,
            "advice_fail_mutator",
            {"target_id": "missing"},
            operation_id="op-adv-fail",
            idempotency_key="idem-adv-fail",
        )
        first_fail = registry.execute_tool(fail_call, write_boundary=None, runtime_snapshot=snapshot).result
        replayed_fail = registry.execute_tool(fail_call, write_boundary=None, runtime_snapshot=snapshot).result
        assert fail_tool.calls == 1
        assert replayed_fail.metadata["next_action"] == first_fail.metadata["next_action"]
        assert replayed_fail.retryable is False
        assert replayed_fail.metadata.get("replayed_terminal_mutation") is True
        rendered_fail = replayed_fail.render_for_prompt()
        assert rendered_fail.count("[tool-next-action]") == 1
    finally:
        registry.close_mcp_clients()


def test_alias_decode_and_operation_identity(tmp_path: Path) -> None:
    aliases = retired_tool_aliases()
    domains = business_domain_actions()
    assert len(aliases) == 104
    assert len(domains) == 4
    assert sum(len(acts) for acts in domains.values()) == 105
    # When legacy leaf name is itself in available, decoder returns None so pre-W1 registrations run directly.
    assert decode_retired_tool_call("business_tasks_submit", {"title": "x"}, available=frozenset({"business_tasks_submit"})) is None

    store = LocalStore(tmp_path / "alias.db", enable_fts=False)
    inspected: list[dict[str, object]] = []

    def _inspect_receipt(params: dict[str, object]):
        inspected.append(dict(params))
        return {"operation_id": "op-unk-legacy", "state": "completed", "target_refs": ["task-99"], "error": None}

    domain_tool = _DomainMockTool("business_execution", inspect_hook=_inspect_receipt)
    registry = _make_registry(tmp_path, store=store)
    registry.register(domain_tool)
    try:
        snapshot = registry.runtime_snapshot(run_id="run-alias")

        base_alias_call = canonical_test_call(
            snapshot,
            "business_execution",
            {"action": "tasks.list"},
            attempt_id="att-alias",
        )

        # 1) Dead business_spaces nonexecuting redirect with effect_outcome == not_started.
        for spaces_id, spaces_args, expected_detail_key in (
            ("c-spaces-known", {"action": "spaces.create", "params": {"name": "demo"}}, "target_tool"),
            ("c-spaces-fallback", {"action": "create", "name": "demo"}, "target_tools"),
        ):
            spaces_call = replace(
                base_alias_call,
                call_id=spaces_id,
                tool_name="business_spaces",
                arguments=spaces_args,
            )
            spaces_res = registry.execute_tool(spaces_call, write_boundary=None, runtime_snapshot=snapshot).result
            assert spaces_res.ok is False
            assert spaces_res.error_code == "TOOL_NOT_IN_RUNTIME_SNAPSHOT"
            assert spaces_res.metadata["reported_error_code"] == "TOOL_RETIRED_REDIRECT"
            assert spaces_res.metadata["error_details"]["retired_tool"] == "business_spaces"
            assert expected_detail_key in spaces_res.metadata["error_details"]
            assert spaces_res.effect_outcome == "not_started"
            assert spaces_res.retryable is False
            assert "next_action" in spaces_res.metadata
        assert len(domain_tool.calls) == 0

        # 2) Conflicting action on retired leaf alias rejects before claim.
        conflict_call = replace(
            base_alias_call,
            call_id="c-conflict",
            tool_name="business_tasks_submit",
            arguments={"action": "tasks.cancel", "title": "bad"},
            operation_id="op-conflict-none",
            idempotency_key="idem-conflict-none",
        )
        conflict_res = registry.execute_tool(conflict_call, write_boundary=None, runtime_snapshot=snapshot).result
        assert conflict_res.ok is False
        assert conflict_res.error_code == "TOOL_INVALID_ARGUMENTS"
        assert store.get_tool_operation(owner_id="owner-w0", run_id="run-alias", operation_id="op-conflict-none") is None

        # 3) Retired leaf alias under v1 and v2 preserves call_id, operation_id, prior_migration, and operation identity.
        for version in (1, 2):
            op_id = f"op-v{version}"
            idem_key = f"idem-v{version}"
            alias_call = replace(
                base_alias_call,
                call_id=f"call-v{version}",
                tool_name="business_tasks_submit",
                arguments={"title": f"task-v{version}"},
                operation_id=op_id,
                idempotency_key=idem_key,
                input_migration={"prior_step": f"v{version}"},
            )
            exec_res = ToolExecutor().execute(
                ToolExecutorRequest(
                    call=alias_call,
                    runtime_snapshot=snapshot,
                    workspace_root=tmp_path,
                    operation_store=store,
                    operation_store_required=True,
                    operation_owner_id="owner-w0",
                    trusted_run_context={"tool_input_version": version, "run_scope": {"run_id": "run-alias"}},
                )
            )
            assert exec_res.result.ok is True
            assert exec_res.call.call_id == f"call-v{version}"
            assert exec_res.call.operation_id == op_id
            assert exec_res.call.input_migration["prior_migration"] == {"prior_step": f"v{version}"}
            saved_rec = store.get_tool_operation(owner_id="owner-w0", run_id="run-alias", operation_id=op_id)
            assert saved_rec is not None
            assert saved_rec.tool == "business_tasks_submit"
            assert saved_rec.idempotency_namespace == "business_tasks_submit"

        # 4) Legacy completed / in-flight / unknown records written under leaf name reconcile with canonical domain tool.
        canon_call = canonical_test_call(
            snapshot,
            "business_execution",
            {"action": "tasks.submit", "title": "legacy-done"},
            call_id="c-leg-done",
            attempt_id="att-alias",
            operation_id="op-leg-done",
            idempotency_key="idem-leg-done",
        )
        claim_done = store.claim_tool_operation(
            ToolOperationClaimRequest(
                owner_id="owner-w0",
                run_id="run-alias",
                task_id="run-alias",
                operation_id="op-leg-done",
                tool="business_tasks_submit",
                args_hash=canon_call.args_hash,
                idempotency_key="idem-leg-done",
                idempotency_scope="operation",
                idempotency_namespace="business_tasks_submit",
                holder=new_tool_operation_holder(),
                lease_expires_at=9999999999.0,
            )
        )
        store.finish_tool_operation(
            ToolOperationCompletionRequest(
                owner_id="owner-w0",
                run_id="run-alias",
                operation_id="op-leg-done",
                holder_id=claim_done.record.holder_id,
                generation=claim_done.record.generation,
                status=TOOL_OPERATION_SUCCEEDED,
                result={
                    "schema_version": "tool_execution_result.v1",
                    "tool": "business_tasks_submit",
                    "ok": True,
                    "output": '{"ok": true, "replayed": "legacy"}',
                    "handler_executed": True,
                    "effect_outcome": "committed",
                },
            )
        )
        before_calls = len(domain_tool.calls)
        replayed_legacy = registry.execute_tool(canon_call, write_boundary=None, runtime_snapshot=snapshot).result
        assert replayed_legacy.ok is True
        assert "legacy" in replayed_legacy.output
        assert len(domain_tool.calls) == before_calls

        # In-flight legacy record blocks concurrent canonical call without re-executing handler.
        inflight_call = canonical_test_call(
            snapshot,
            "business_execution",
            {"action": "tasks.submit", "title": "legacy-inflight"},
            call_id="c-leg-fly",
            attempt_id="att-alias",
            operation_id="op-leg-fly",
            idempotency_key="idem-leg-fly",
        )
        store.claim_tool_operation(
            ToolOperationClaimRequest(
                owner_id="owner-w0",
                run_id="run-alias",
                task_id="run-alias",
                operation_id="op-leg-fly",
                tool="business_tasks_submit",
                args_hash=inflight_call.args_hash,
                idempotency_key="idem-leg-fly",
                idempotency_scope="operation",
                idempotency_namespace="business_tasks_submit",
                holder=new_tool_operation_holder(),
                lease_expires_at=9999999999.0,
            )
        )
        inflight_res = registry.execute_tool(inflight_call, write_boundary=None, runtime_snapshot=snapshot).result
        assert inflight_res.ok is False
        assert store.get_tool_operation(owner_id="owner-w0", run_id="run-alias", operation_id="op-leg-fly").status == TOOL_OPERATION_RUNNING
        assert len(domain_tool.calls) == before_calls

        # Unknown outcome + _mark_unknown_outcome_halt receipt inspection matches operation_identity_for.
        unk_call = canonical_test_call(
            snapshot,
            "business_execution",
            {"action": "tasks.submit", "title": "simulate_unknown"},
            call_id="c-unk-rec",
            attempt_id="att-alias",
            operation_id="op-unk-legacy",
            idempotency_key="idem-unk-legacy",
        )
        unk_res = (
            ToolExecutor()
            .execute(
                ToolExecutorRequest(
                    call=unk_call,
                    runtime_snapshot=snapshot,
                    workspace_root=tmp_path,
                    operation_store=store,
                    operation_store_required=True,
                    operation_owner_id="owner-w0",
                    trusted_run_context={"run_scope": {"run_id": "run-alias", "task_id": "run-alias"}},
                )
            )
            .result
        )
        assert unk_res.effect_outcome == "unknown"
        scope = RunScope(
            request_id="req-alias",
            run_id="run-alias",
            task_id="run-alias",
            attempt_id="att-alias",
            owner_id="owner-w0",
        )
        loop_params = SimpleNamespace(
            run_scope=scope,
            request_id="req-alias",
            run_id="run-alias",
            task_id="run-alias",
            attempt_id="att-alias",
            context_scope="session",
            task_attributes={},
            tool_runtime_snapshot=snapshot,
            repeated_failure_halt=None,
            unknown_outcome_halt=None,
            tool_context=[],
        )
        host = SimpleNamespace(home_paths=SimpleNamespace(owner_id="owner-w0"), tools=registry)
        _mark_unknown_outcome_halt(
            host,
            SimpleNamespace(params=loop_params, call=unk_call, result=unk_res),
        )
        assert loop_params.unknown_outcome_halt is not None
        inspection_entry = next(
            item for item in loop_params.tool_context if item.startswith("[host-original-operation-inspection]\n")
        )
        inspection_facts = json.loads(inspection_entry.split("\n", 1)[1])
        assert inspection_facts["lookup"] == "owner_bound_tool_operation_store.get_tool_operation"
        assert inspection_facts["operation_id"] == "op-unk-legacy"
        assert inspection_facts["canonical_status"] == "unknown"

        # 5) tool_search reports renamed_aliases & redirect_only without widening unauthorized action ceilings.
        narrow_search_snap = registry.runtime_snapshot(
            allowed_tools=["tool_search", "business_tasks_list"],
            run_id="run-alias-search",
        )
        search_call = canonical_test_call(
            narrow_search_snap,
            "tool_search",
            {"action": "search", "names": ["business_spaces", "business_tasks_list", "business_tasks_submit"]},
        )
        search_res = registry.execute_tool(search_call, write_boundary=None, runtime_snapshot=narrow_search_snap).result
        assert search_res.ok is True
        search_payload = json.loads(search_res.output)
        assert search_payload["loaded_for_next_model_call"] == ["business_execution"]
        by_req = {item["requested"]: item for item in search_payload["renamed_aliases"]}
        assert by_req["business_spaces"]["redirect_only"] is True
        assert by_req["business_tasks_list"]["authorized"] is True
        assert by_req["business_tasks_submit"]["authorized"] is False
    finally:
        registry.close_mcp_clients()


def test_grant_intersection_never_widens(tmp_path: Path) -> None:
    assert [
        len(CODING_SUBAGENT_TOOLS),
        len(READ_ONLY_SUBAGENT_TOOLS),
        len(COORDINATOR_TOOLS),
        len(PARENT_PLANNER_READ_TOOLS),
        len(ONE_SHOT_TOOL_NAMES),
    ] == [25, 15, 32, 3, 1]

    # Token-only input grants nothing.
    assert normalize_tool_grants(["business_execution:tasks.list"]) == []
    assert action_ceilings(["business_execution:tasks.list"]) == {}

    # Parent ceiling cannot be widened by child bare domain grant, preset union, role normalization, or write narrowing.
    parent_narrow = normalize_tool_grants(["read_file", "business_tasks_list", "business_tasks_get"])
    assert set(parent_narrow) == {
        "read_file",
        "business_execution",
        "business_execution:tasks.list",
        "business_execution:tasks.get",
    }
    child_wide = ["read_file", "business_execution"]
    inter = intersect_tool_grants(child_wide, parent_narrow)
    assert action_ceilings(inter)["business_execution"] == frozenset({"tasks.list", "tasks.get"})

    merged_with_preset = subagent_allowed_tools(
        {"allowed_tools": ["business_tasks_list", "list_tools"], "tool_preset": "read_only"}
    )
    assert action_ceilings(merged_with_preset)["business_execution"] == frozenset({"tasks.list"})
    assert action_ceilings(merged_with_preset)["tool_search"] == frozenset({"catalog"})

    scheduled_leaf = scheduled_child_tools(
        ToolPolicyRequest(
            parent_tools=parent_narrow,
            spec=SimpleNamespace(role="worker", allowed_tools=["read_file", "business_execution"], attributes={}),
            extra_write_roots=[],
            goal="只读核对项目任务列表",
            role="worker",
        )
    )
    assert action_ceilings(scheduled_leaf)["business_execution"] == frozenset({"tasks.list", "tasks.get"})

    domain_tool = _DomainMockTool("business_execution")
    registry = _make_registry(tmp_path, disabled_tools=["business_tasks_cancel"])
    registry.register(domain_tool)
    try:
        # Creation snapshot persists ceilings; role normalization intersects against it.
        narrow_snap = registry.runtime_snapshot(
            allowed_tools=["read_file", "business_tasks_list"],
            run_id="parent-narrow",
        )
        saved_grants = snapshot_tool_grants(narrow_snap)
        assert "business_execution:tasks.list" in saved_grants
        auth_snap = creation_tool_authority_snapshot(narrow_snap)
        rehydrated = creation_snapshot_tool_grants(auth_snap)
        assert action_ceilings(rehydrated)["business_execution"] == frozenset({"tasks.list"})

        @pytest.mark.filterwarnings("ignore")
        def _build_create_params():
            from dataclasses import dataclass

            @dataclass(frozen=True)
            class _FakeCreateParams:
                role: str = "worker"
                allowed_tools: list[str] | None = None
                acceptance_checks: list[object] | None = None
                quality_contract: object = None
                attributes: dict[str, object] | None = None
                normalize_role: bool = True

            return _FakeCreateParams(
                role="worker",
                allowed_tools=["read_file", "business_execution"],
                attributes={DIRECT_PARENT_TOOL_AUTHORITY_ATTR: auth_snap},
            )

        role_res = apply_role_contract_to_create_params(_build_create_params())
        assert action_ceilings(role_res.allowed_tools)["business_execution"] == frozenset({"tasks.list"})

        # direct_parent_tool_authority rejects widening to full business_execution while allowing matching ceiling.
        parent_task = SubAgentTask(
            id="parent-1",
            goal="parent",
            thought="t",
            plan=["p"],
            role="coordinator",
            allowed_tools=["read_file", "business_execution", "business_execution:tasks.list"],
        )
        child_task = SubAgentTask(
            id="child-1",
            parent_id="parent-1",
            goal="child",
            thought="t",
            plan=["p"],
            role="worker",
        )
        fake_mgr = SimpleNamespace(
            load=lambda tid: parent_task if tid == "parent-1" else None,
            runner_context=SimpleNamespace(
                build_execution_context=lambda _tid: SimpleNamespace(allowed_tools=list(parent_task.allowed_tools))
            ),
        )
        widen_attempt = direct_parent_tool_authority(fake_mgr, child_task, ["business_execution"])
        assert widen_attempt.unavailable_tools == ("business_execution",)
        exact_attempt = direct_parent_tool_authority(
            fake_mgr,
            child_task,
            ["business_execution", "business_execution:tasks.list"],
        )
        assert exact_attempt.unavailable_tools == ()

        # Token-only snapshot grants nothing; unknown action ceiling fails closed with specific diagnostic.
        token_only_snap = registry.runtime_snapshot(allowed_tools=["business_execution:tasks.list"])
        assert "business_execution" not in token_only_snap.available_tool_names

        unknown_act_snap = registry.runtime_snapshot(
            allowed_tools=["business_execution", "business_execution:unknown_op"]
        )
        assert "business_execution" not in unknown_act_snap.available_tool_names
        assert any(
            name == "business_execution" and code == "TOOL_ACTION_NOT_PERMITTED"
            for name, code, _reason in unknown_act_snap.unavailable_tools
        )

        # Disabled leaf subtracts only tasks.cancel; schema narrowing prunes enum, oneOf, $defs, and examples.
        full_with_one_disabled = registry.runtime_snapshot(allowed_tools=["business_execution"])
        rt_disabled_leaf = full_with_one_disabled.runtime("business_execution")
        assert rt_disabled_leaf is not None
        enum_after_leaf_disable = rt_disabled_leaf.model_spec.input_schema["properties"]["action"]["enum"]
        assert "tasks.cancel" not in enum_after_leaf_disable
        assert "tasks.list" in enum_after_leaf_disable

        single_action_spec = narrow_snap.runtime("business_execution").model_spec
        first_act = business_domain_actions()["business_execution"][0]
        single_snap = registry.runtime_snapshot(allowed_tools=[f"business_{first_act.replace('.', '_')}"])
        narrowed_first = single_snap.runtime("business_execution").model_spec
        assert narrowed_first.input_schema["properties"]["action"]["enum"] == [first_act]
        assert len(narrowed_first.input_schema["oneOf"]) == 1
        assert set(narrowed_first.input_schema["$defs"]) == {f"def_{first_act.replace('.', '_')}"}
        assert len(narrowed_first.hints.examples) == 1
        assert single_action_spec.schema_hash != domain_tool.model_spec.schema_hash

        # Disabled family denies entire tool.
        fam_disabled_reg = _make_registry(tmp_path, disabled_tools=["business_execution"])
        fam_disabled_reg.register(_DomainMockTool("business_execution"))
        try:
            assert fam_disabled_reg.runtime_snapshot(allowed_tools=["business_tasks_list"]).runtime("business_execution") is None
        finally:
            fam_disabled_reg.close_mcp_clients()

        # Prototype cache stays bounded <= 128 across 160 distinct spec revisions.
        for idx in range(160):
            temp_spec = ToolModelSpec(
                name="bounded_cache_probe",
                description=f"probe-{idx}",
                input_schema={
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["a", "b"]},
                        f"field_{idx}": {"type": "string"},
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
            )
            registry_module._narrow_model_spec_for_actions(temp_spec, frozenset({"a"}))
        assert len(registry_module._NARROWED_MODEL_SPEC_CACHE) <= registry_module._NARROWED_MODEL_SPEC_MAX_ENTRIES
    finally:
        registry.close_mcp_clients()


def test_native_recommendations_exclude_active_schemas(tmp_path: Path) -> None:
    class _KeywordTool(BaseTool):
        runtime_policy = make_test_runtime_policy("read_only")

        def __init__(self, name: str, *, category: str) -> None:
            self.model_spec = make_test_model_spec(
                name,
                category=category,
                description="星河日志归档检索专用工具。",
                keywords=("星河日志", "归档检索", name),
            )

        def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
            return ToolHandlerOutcome(self.model_spec.name, True, "ok")

    registry = _make_registry(tmp_path, deferred_categories=["deferred_ops"])
    active_tool = _KeywordTool("active_galaxy_probe", category="general")
    deferred_tool = _KeywordTool("deferred_galaxy_probe", category="deferred_ops")
    registry.register(active_tool)
    registry.register(deferred_tool)
    try:
        snapshot = registry.runtime_snapshot(run_id="run-rec")
        query = "请使用星河日志归档检索查看记录"

        native_initial = registry.render_recommended_tools_section(
            query,
            tool_protocol="native",
            runtime_snapshot=snapshot,
        )
        assert "- deferred_galaxy_probe：" in native_initial
        assert "active_galaxy_probe" not in native_initial

        native_after_load = registry.render_recommended_tools_section(
            query,
            loaded_tool_names=("deferred_galaxy_probe",),
            tool_protocol="native",
            runtime_snapshot=snapshot,
        )
        assert "deferred_galaxy_probe" not in native_after_load
        assert "active_galaxy_probe" not in native_after_load

        text_mode = registry.render_recommended_tools_section(
            query,
            tool_protocol="text",
            runtime_snapshot=snapshot,
        )
        assert "active_galaxy_probe" in text_mode

        # Verify active-turn loop dedup drops newly discovered tools without mutating/replacing params.
        protocol = ToolProtocolSnapshot(
            "run-rec",
            "native",
            ProviderToolCapability(
                provider="echo",
                endpoint="local://echo",
                model="echo",
                stream=False,
                native_supported=True,
                evidence="local-contract-test",
            ),
        )
        fake_agent = SimpleNamespace(tools=registry)
        loop_params = SimpleNamespace(
            tool_protocol_snapshot=protocol,
            tool_runtime_snapshot=snapshot,
            allowed_tools=None,
            loaded_tool_names={"deferred_galaxy_probe"},
            live_archive_state={"loaded_tool_order": ["deferred_galaxy_probe"]},
            turn_trigger=None,
            tool_recommendations_section=native_initial + "\n\n# Capability Packages\n- keep-package",
        )
        req = PromptBuildRequest(
            user_prompt=query,
            memories=[],
            tools=ToolSections(
                tool_recommendations_section=loop_params.tool_recommendations_section,
                native_tool_use=True,
            ),
        )
        deduped = _dedup_native_recommendations_section(fake_agent, loop_params, req)
        assert "deferred_galaxy_probe" not in deduped.tools.tool_recommendations_section
        assert "# Capability Packages\n- keep-package" in deduped.tools.tool_recommendations_section
    finally:
        registry.close_mcp_clients()


def test_snapshot_name_lookup_reuses_index(tmp_path: Path) -> None:
    armed = False
    unrelated_touches = 0

    class _CountingRuntimeProxy:
        def __init__(self, inner: ToolRuntime, *, is_target: bool) -> None:
            self._inner = inner
            self._is_target = is_target

        @property
        def model_spec(self) -> ToolModelSpec:
            nonlocal unrelated_touches
            if armed and not self._is_target:
                unrelated_touches += 1
            return self._inner.model_spec

        def __getattr__(self, item: str) -> object:
            return getattr(self._inner, item)

    registry = _make_registry(tmp_path)
    try:
        base_snap = registry.runtime_snapshot(run_id="run-idx")
        all_runtimes = list(base_snap.runtimes)
        assert len(all_runtimes) >= 5
        target_name = all_runtimes[-1].model_spec.name
        wrapped = tuple(
            _CountingRuntimeProxy(rt, is_target=(rt.model_spec.name == target_name))
            for rt in all_runtimes
        )
        indexed_snap = ToolRuntimeSnapshot(
            run_id="run-idx-wrapped",
            runtimes=wrapped,
            available_tool_names=frozenset(rt.model_spec.name for rt in all_runtimes),
            unavailable_tools=(),
            allowed_tools=None,
        )
        armed = True
        for _ in range(50):
            found = indexed_snap.runtime(target_name)
            assert found is not None and found.model_spec.name == target_name
            assert indexed_snap.runtime("definitely_not_registered") is None
        assert unrelated_touches == 0

        projected = indexed_snap.with_presentation(
            presentation_shortlist_names=frozenset({"tool_search", target_name})
        )
        assert projected.runtimes is indexed_snap.runtimes
        assert projected._runtime_by_name is indexed_snap._runtime_by_name
        assert projected.runtime(target_name) is indexed_snap.runtime(target_name)
        assert unrelated_touches == 0
    finally:
        registry.close_mcp_clients()


def test_mcp_remote_error_contract_and_narrowed_spec_catalog_entry() -> None:
    from agent_py_agent.agent.settings.constants_catalog import load_catalog

    contract = get_error_contract("MCP_REMOTE_ERROR")
    assert contract.code == "MCP_REMOTE_ERROR"
    assert contract.category == "tool"
    assert contract.retryable is False
    assert contract.recommended_action == "change_strategy"
    assert "远端错误文本" in contract.recovery_hint
    assert "核实目标当前状态" in contract.recovery_hint
    assert "禁止原样重复提交" in contract.recovery_hint

    assert registry_module._NARROWED_MODEL_SPEC_MAX_ENTRIES == 128
    matches = [
        entry
        for entry in load_catalog()
        if entry["name"] == "_NARROWED_MODEL_SPEC_MAX_ENTRIES"
    ]
    assert len(matches) == 1
    assert matches[0]["file"] == "agent_py_agent/agent/tooling/registry.py"
    assert matches[0]["value"] == 128
    assert matches[0]["unit"] == "条"
    assert matches[0]["description"]
