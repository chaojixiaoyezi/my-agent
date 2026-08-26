from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_support import _tool_runtime_evidence
from agent_py_agent.agent.agent_core.tool_model_generation import (
    _remember_model_turn_tool_choice,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.tool_protocol_adapter import (
    ProviderToolCallRequest,
    canonical_tool_calls_from_response,
)
from agent_py_agent.agent.concurrency.interrupt import (
    interrupt_by_name,
    register_interruptible,
)
from agent_py_agent.agent.contracts.effective_contract_snapshot import (
    build_effective_contract_snapshot,
)
from agent_py_agent.agent.contracts.required_actions import (
    RequiredAction,
    required_action_no_tool_decision,
    settle_required_action,
    tool_choice_for_required_actions,
)
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.tooling._filesystem_write import WriteFileTool
from agent_py_agent.agent.tooling.cancellation import (
    CancellationToken,
    current_cancellation_token,
)
from agent_py_agent.agent.tooling.controlled_exec import ControlledExecTool
from agent_py_agent.agent.tooling.executor import ToolExecutor, ToolExecutorRequest
from agent_py_agent.agent.tooling.models import (
    ApprovalPolicy,
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolInputPolicy,
    ToolModelSpec,
    ToolRuntime,
    ToolRuntimePolicy,
    ToolRuntimeSnapshot,
    TrustedParameterBinding,
    tool_effect_for_runtime_policy,
)
from agent_py_agent.agent.tooling.runtime_contracts import (
    ProviderToolCapability,
    ToolCall,
    ToolChoice,
    ToolProtocolSnapshot,
)
from agent_py_agent.agent.tooling.shell import ShellTool


class _CountingTool(BaseTool):
    def __init__(self, *, command: bool = False) -> None:
        properties = {"command": {"type": "string"}} if command else {"value": {"type": "string"}}
        required = ["command"] if command else ["value"]
        self.model_spec = ToolModelSpec(
            "command_tool" if command else "echo_tool",
            "Execute a fake command." if command else "Echo one value.",
            {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        )
        self.runtime_policy = ToolRuntimePolicy(
            effect_resolver=EffectResolverPolicy(
                default_effect="mutating" if command else "read_only",
                strategy="command" if command else "declared",
                command_parameter="command" if command else "",
            ),
            approval_policy=ApprovalPolicy("dangerous"),
            idempotency_policy=IdempotencyPolicy("operation" if command else ""),
        )
        self.executions = 0

    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        self.executions += 1
        body = str(params.get("command") or params.get("value") or "")
        return ToolHandlerOutcome(self.model_spec.name, True, body)


class _SchemaTool(BaseTool):
    def __init__(self) -> None:
        self.model_spec = ToolModelSpec(
            "schema_tool",
            "Validate a constrained object.",
            {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["brief", "full"]},
                    "limit": {"type": "integer", "minimum": 1},
                },
                "required": ["mode", "limit"],
                "additionalProperties": False,
            },
        )
        self.runtime_policy = ToolRuntimePolicy(EffectResolverPolicy("read_only"))
        self.executions = 0

    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        self.executions += 1
        return ToolHandlerOutcome(self.model_spec.name, True, json.dumps(params))


def _snapshot(tool: _CountingTool, *, run_id: str = "run-1") -> ToolRuntimeSnapshot:
    runtime = ToolRuntime(tool.model_spec, tool.runtime_policy, tool)
    return ToolRuntimeSnapshot(
        run_id=run_id,
        runtimes=(runtime,),
        available_tool_names=frozenset({tool.model_spec.name}),
        unavailable_tools=(),
        allowed_tools=None,
    )


def _call(tool: _CountingTool, arguments: dict[str, object]) -> ToolCall:
    return ToolCall(
        call_id="call-1",
        tool_name=tool.model_spec.name,
        arguments=arguments,
        source_protocol="native",
        schema_hash=tool.model_spec.schema_hash,
        run_id="run-1",
        turn_id="turn-1",
        attempt_id="attempt-1",
    )


def _execute(
    tmp_path: Path,
    tool: _CountingTool,
    call: ToolCall,
    *,
    write_boundary: dict[str, object] | None = None,
    cancellation_token: CancellationToken | None = None,
    output_archiver=None,
):
    return ToolExecutor().execute(
        ToolExecutorRequest(
            call=call,
            runtime_snapshot=_snapshot(tool),
            workspace_root=tmp_path,
            workspace_roots=(tmp_path,),
            write_boundary=write_boundary,
            operation_store_required=False,
            cancellation_token=cancellation_token,
            output_archiver=output_archiver,
        )
    )


def test_wrong_argument_type_is_rejected_before_handler(tmp_path: Path) -> None:
    tool = _CountingTool()
    execution = _execute(tmp_path, tool, _call(tool, {"value": 7}))

    assert execution.decision.status == "deny"
    assert execution.result.failure_stage == "validation"
    assert execution.result.handler_executed is False
    assert tool.executions == 0


def test_direct_executor_without_archiver_never_drops_large_output(tmp_path: Path) -> None:
    tool = _CountingTool()
    body = "x" * 20_000

    execution = _execute(tmp_path, tool, _call(tool, {"value": body}))

    assert execution.result.ok is True
    assert execution.result.output == body
    assert execution.result.refs == ()
    assert execution.result.metadata.get("projection_truncated") is not True


def test_output_archive_failure_still_returns_paired_persistence_result(
    tmp_path: Path,
) -> None:
    tool = _CountingTool()

    def broken_archiver(_call: ToolCall, _outcome: object):
        raise OSError("archive unavailable")

    execution = _execute(
        tmp_path,
        tool,
        _call(tool, {"value": "must not leak"}),
        output_archiver=broken_archiver,
    )

    assert execution.call.call_id == execution.result.call_id
    assert execution.result.status == "failed"
    assert execution.result.error_code == "TOOL_OUTPUT_ARCHIVE_FAILED"
    assert execution.result.failure_stage == "persistence"
    assert execution.result.handler_executed is True
    assert execution.result.refs == ()
    assert "must not leak" not in execution.result.output
    assert execution.result.metadata["projection_exception_type"] == "OSError"
    assert "persistence_failed" in execution.states
    assert tool.executions == 1


def test_required_enum_and_extra_fields_are_rejected_before_handler(
    tmp_path: Path,
) -> None:
    for arguments, expected_code in (
        ({"mode": "brief"}, "TOOL_PARAMETER_REQUIRED"),
        ({"mode": "unknown", "limit": 1}, "TOOL_INVALID_ARGUMENTS"),
        (
            {"mode": "brief", "limit": 1, "untrusted": True},
            "TOOL_INVALID_ARGUMENTS",
        ),
    ):
        tool = _SchemaTool()
        execution = _execute(tmp_path, tool, _call(tool, arguments))

        assert execution.decision.status == "deny"
        assert execution.result.error_code == expected_code
        assert execution.result.failure_stage == "validation"
        assert execution.result.handler_executed is False
        assert tool.executions == 0


def test_unknown_command_passes_without_approval(tmp_path: Path) -> None:
    # 2026-08-07 用户设计：unknown 命令不做人工审批（无审批通道，ask 只会卡死任务），
    # 决策层放行，额度制在 runtime guard 层按代理实例滚动管理。
    tool = _CountingTool(command=True)
    execution = _execute(tmp_path, tool, _call(tool, {"command": "mystery_cli --do-it"}))

    assert execution.decision.status == "allow"
    assert execution.result.handler_executed is True
    assert tool.executions == 1


def test_dangerous_command_needs_exact_approval_binding(tmp_path: Path) -> None:
    tool = _CountingTool(command=True)
    call = _call(tool, {"command": "git push origin main"})
    first = _execute(tmp_path, tool, call)
    assert first.decision.status == "ask"
    assert tool.executions == 0

    binding = {
        **dict(first.decision.approval_request or {}),
        "approval_id": "approval-1",
        "status": "APPROVED",
    }
    second = _execute(tmp_path, tool, call, write_boundary={"approved_actions": [binding]})
    assert second.result.ok is True
    assert second.result.handler_executed is True
    assert tool.executions == 1


# LLM: 这个夹具必须复用真实 ShellTool schema/policy，只用无副作用 handler 替换实际进程启动。
# 函数用途: 构造一次可安全穿过完整 ToolExecutor 的后台 HTTP 命令授权用例。
def _background_shell_request(
    tmp_path: Path,
) -> tuple[ToolExecutorRequest, list[dict[str, object]]]:
    tool = ShellTool(tmp_path)
    executed: list[dict[str, object]] = []

    def fake_execute(params: dict[str, object]) -> ToolHandlerOutcome:
        executed.append(dict(params))
        return ToolHandlerOutcome("run_command", True, "started")

    tool.execute = fake_execute  # type: ignore[method-assign]
    runtime = ToolRuntime(tool.model_spec, tool.runtime_policy, tool)
    snapshot = ToolRuntimeSnapshot(
        run_id="run-background-approval",
        runtimes=(runtime,),
        available_tool_names=frozenset({"run_command"}),
        unavailable_tools=(),
        allowed_tools=None,
    )
    call = ToolCall(
        call_id="call-background-server",
        tool_name="run_command",
        arguments={
            "command": "python3 -m http.server 8765 --bind 0.0.0.0",
            "run_in_background": True,
        },
        source_protocol="native",
        schema_hash=tool.model_spec.schema_hash,
        run_id=snapshot.run_id,
        turn_id="turn-background-server",
        attempt_id="attempt-background-server",
    )
    return (
        ToolExecutorRequest(
            call=call,
            runtime_snapshot=snapshot,
            workspace_root=tmp_path,
            workspace_roots=(tmp_path,),
            operation_store_required=False,
        ),
        executed,
    )


def test_managed_background_command_needs_exact_approval_binding(tmp_path: Path) -> None:
    request, executed = _background_shell_request(tmp_path)
    first = ToolExecutor().execute(request)

    assert first.decision.status == "ask"
    assert first.decision.reason_codes == ("APPROVAL_REQUIRED",)
    assert first.decision.resolved_effect == "dangerous"
    assert first.decision.approval_request is not None
    assert first.result.handler_executed is False
    assert executed == []

    binding = {
        **dict(first.decision.approval_request),
        "approval_id": "approval-background-server",
        "status": "APPROVED",
    }
    second = ToolExecutor().execute(
        replace(request, write_boundary={"approved_actions": [binding]})
    )

    assert second.decision.status == "allow"
    assert second.decision.resolved_effect == "dangerous"
    assert second.result.handler_executed is True
    assert len(executed) == 1
    assert executed[0]["command"] == "python3 -m http.server 8765 --bind 0.0.0.0"
    assert executed[0]["run_in_background"] is True
    assert executed[0]["timeout"] == 30


def test_write_boundary_denial_happens_before_operation_claim(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    allowed = workspace / "allowed"
    workspace.mkdir()
    allowed.mkdir()
    tool = WriteFileTool(workspace)
    snapshot = ToolRuntimeSnapshot(
        run_id="run-1",
        runtimes=(ToolRuntime(tool.model_spec, tool.runtime_policy, tool),),
        available_tool_names=frozenset({"write_file"}),
        unavailable_tools=(),
        allowed_tools=None,
    )
    call = ToolCall(
        call_id="call-write-denied",
        tool_name="write_file",
        arguments={"path": "outside.txt", "content": "must not write"},
        source_protocol="native",
        schema_hash=tool.model_spec.schema_hash,
        run_id="run-1",
        turn_id="turn-1",
        attempt_id="attempt-1",
    )
    store = LocalStore(tmp_path / "local.db", enable_fts=False)

    execution = ToolExecutor().execute(
        ToolExecutorRequest(
            call=call,
            runtime_snapshot=snapshot,
            workspace_root=workspace,
            workspace_roots=(workspace,),
            write_boundary={"allowed_write_roots": [str(allowed)]},
            operation_store=store,
            operation_store_required=True,
            operation_owner_id="owner-a",
        )
    )

    assert execution.result.error_code == "WRITE_FORBIDDEN"
    assert execution.result.handler_executed is False
    assert store.list_tool_operations(owner_id="owner-a", run_id="run-1") == []
    assert not (workspace / "outside.txt").exists()


def test_pytest_command_is_classified_and_executes_without_approval(tmp_path: Path) -> None:
    tool = _CountingTool(command=True)
    execution = _execute(tmp_path, tool, _call(tool, {"command": "pytest -q"}))

    assert execution.decision.status == "allow"
    assert execution.decision.resolved_effect == "read_only"
    assert execution.result.ok is True
    assert tool.executions == 1


def test_pre_cancelled_call_never_enters_handler(tmp_path: Path) -> None:
    tool = _CountingTool()
    token = CancellationToken()
    token.cancel("user_stop")
    execution = _execute(
        tmp_path,
        tool,
        _call(tool, {"value": "x"}),
        cancellation_token=token,
    )

    assert execution.result.status == "cancelled"
    assert execution.result.handler_executed is False
    assert tool.executions == 0


def test_cancellation_token_reaches_running_handler_and_returns_paired_result(
    tmp_path: Path,
) -> None:
    started = threading.Event()

    class BlockingTool(_CountingTool):
        def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
            del params
            self.executions += 1
            token = current_cancellation_token()
            assert token is not None
            started.set()
            assert token.wait(2)
            token.raise_if_cancelled()
            raise AssertionError("cancelled handler continued")

    tool = BlockingTool()
    token = CancellationToken()
    observed: dict[str, object] = {}

    def run() -> None:
        with register_interruptible("tool-token-bridge-test"):
            observed["execution"] = _execute(
                tmp_path,
                tool,
                _call(tool, {"value": "x"}),
                cancellation_token=token,
            )

    thread = threading.Thread(target=run)
    thread.start()
    assert started.wait(2)
    assert interrupt_by_name("tool-token-bridge-test") is True
    thread.join(2)

    assert not thread.is_alive()
    execution = observed["execution"]
    assert execution.result.status == "cancelled"
    assert execution.result.handler_executed is True
    assert execution.result.failure_stage == "execution"
    assert tool.executions == 1


def test_native_prose_tool_block_is_violation_and_never_a_call() -> None:
    tool = _CountingTool()
    snapshot = _snapshot(tool)
    capability = ProviderToolCapability(
        provider="fake",
        endpoint="local://fake",
        model="fake-model",
        stream=False,
        native_supported=True,
        evidence="test_probe",
    )
    result = canonical_tool_calls_from_response(
        ProviderToolCallRequest(
            response=ModelResponse(
                text='[TOOL_CALL]{"tool":"echo_tool","value":"x"}[/TOOL_CALL]',
                backend="fake",
            ),
            protocol=ToolProtocolSnapshot("run-1", "native", capability),
            runtime_snapshot=snapshot,
            turn_id="turn-1",
            attempt_id="attempt-1",
        )
    )

    assert result.calls == ()
    assert [item.code for item in result.violations] == ["PROTOCOL_VIOLATION"]
    assert tool.executions == 0


def test_run_evidence_projects_exact_snapshots_choices_and_completion() -> None:
    runtime_snapshot = _snapshot(_CountingTool())
    capability = ProviderToolCapability(
        provider="test-provider",
        endpoint="https://provider.invalid/v1",
        model="test-model",
        stream=True,
        native_supported=True,
        evidence="capability_probe",
    )
    protocol = ToolProtocolSnapshot("run-1", "native", capability)
    contract = build_effective_contract_snapshot(
        run_id="run-1",
        layers=(),
        required_action_assessment={
            "source": "model_structured",
            "error": "",
            "requires_action": False,
            "raw": {"untrusted": "not projected"},
        },
    )
    state: dict[str, object] = {
        "_current_model_turn_id": "turn-1",
        "protocol_violation_trace": [
            {
                "turn_id": "turn-0",
                "violations": [
                    {
                        "code": "TOOL_CHOICE_VIOLATION",
                        "detail": "tools disabled",
                        "source_protocol": "native",
                        "evidence_preview": "",
                    }
                ],
            }
        ],
    }
    _remember_model_turn_tool_choice(
        SimpleNamespace(live_archive_state=state),
        ToolChoice.none("semantic_assessment_informational"),
    )

    evidence = _tool_runtime_evidence(
        runtime_snapshot,
        protocol,
        contract,
        state,
        ModelResponse("解释内容", "test-provider"),
    )

    assert evidence["runtime_snapshot"]["snapshot_hash"] == runtime_snapshot.snapshot_hash
    assert evidence["protocol"] == {
        "source_protocol": "native",
        "provider": "test-provider",
        "endpoint": "https://provider.invalid/v1",
        "model": "test-model",
        "stream": True,
        "native_supported": True,
        "capability_evidence": "capability_probe",
    }
    assert evidence["required_action_assessment"] == {
        "source": "model_structured",
        "error": "",
        "requires_action": False,
    }
    assert evidence["tool_choices"] == [
        {
            "turn_id": "turn-1",
            "mode": "none",
            "tool_name": "",
            "reason": "semantic_assessment_informational",
        }
    ]
    assert evidence["protocol_violations"] == state["protocol_violation_trace"]
    assert evidence["completion_gate"]["completed"] is True
    assert "not projected" not in json.dumps(evidence)


def test_required_action_lets_model_choose_and_keeps_choosing_after_evidence(tmp_path: Path) -> None:
    tool = _CountingTool(command=True)
    action = RequiredAction(
        action_id="required-1",
        source_turn_id="turn-user-1",
        kind="execute",
        allowed_tools=("command_tool",),
        effect_ceiling="read_only",
    )
    contract = build_effective_contract_snapshot(
        run_id="run-1",
        layers=(),
        required_actions=(action,),
    )
    provider_tools = [{"name": "command_tool"}]
    # open action 不再强制 specific 工具:执行层 _required_action_decision 已硬约束
    # 工具名单与 effect 上限(真机铁证 2026-08-08: 强制唯一工具卡死"先读后改"链)。
    assert tool_choice_for_required_actions(contract, provider_tools).mode == "auto"

    call = replace(
        _call(tool, {"command": "pytest -q"}),
        required_action_id=action.action_id,
    )
    execution = ToolExecutor().execute(
        ToolExecutorRequest(
            call=call,
            runtime_snapshot=_snapshot(tool),
            workspace_root=tmp_path,
            workspace_roots=(tmp_path,),
            operation_store_required=False,
            required_action=action,
        )
    )
    settle_required_action(contract, execution.call, execution.result)

    assert action.status == "satisfied"
    assert action.evidence_call_ids == ["call-1"]
    # settled 后仍由模型自主:销账后常需继续验证(编译/测试),强制 none 切断
    # "修完-验证"链 → TOOL_CHOICE_VIOLATION break(真机铁证 2026-08-08: 模型
    # 5 轮修完 main.go 后想 go build 被连拦 3 轮)。settled=无待办约束。
    assert tool_choice_for_required_actions(contract, provider_tools).mode == "auto"


def test_required_action_without_call_repairs_once_then_is_unfinished() -> None:
    action = RequiredAction(
        action_id="required-1",
        source_turn_id="turn-user-1",
        kind="execute",
        allowed_tools=("command_tool",),
        effect_ceiling="read_only",
    )
    contract = build_effective_contract_snapshot(
        run_id="run-1",
        layers=(),
        required_actions=(action,),
    )

    assert required_action_no_tool_decision(contract) == "repair"
    assert required_action_no_tool_decision(contract) == "unfinished"
    assert action.status == "unfinished"
    assert action.evidence_call_ids == []


def test_required_action_id_reuse_falls_back_and_drains_in_order(tmp_path: Path) -> None:
    # G4-001 真机铁证(2026-08-11): 评估产出多个允许同工具的 open action 时,
    # 模型多次调用复用同一个 required_action_id → 精确匹配只销第一个,其余真实
    # 执行被漏销,收口门 REQUIRED_ACTION_HAS_NO_EVIDENCE 误杀已完成任务。
    # 修复: id 指向已销账的 action 时回退工具匹配,多候选按评估顺序先到先得。
    tool = _CountingTool(command=True)
    first = RequiredAction(
        action_id="required-1",
        source_turn_id="turn-user-1",
        kind="execute",
        allowed_tools=("command_tool",),
        effect_ceiling="mutating",
    )
    second = RequiredAction(
        action_id="required-2",
        source_turn_id="turn-user-1",
        kind="execute",
        allowed_tools=("command_tool",),
        effect_ceiling="mutating",
    )
    contract = build_effective_contract_snapshot(
        run_id="run-1",
        layers=(),
        required_actions=(first, second),
    )

    def run_call(call_id: str, action_id: str | None) -> None:
        call = replace(
            _call(tool, {"command": "true"}),
            call_id=call_id,
            required_action_id=action_id,
        )
        execution = ToolExecutor().execute(
            ToolExecutorRequest(
                call=call,
                runtime_snapshot=_snapshot(tool),
                workspace_root=tmp_path,
                workspace_roots=(tmp_path,),
                operation_store_required=False,
            )
        )
        settle_required_action(contract, execution.call, execution.result)

    # 第一次: 精确匹配销第一个
    run_call("call-1", first.action_id)
    assert first.status == "satisfied"
    assert second.status == "open"
    # 第二次复用同一 id(该 action 已销账): 回退工具匹配,按顺序销第二个
    run_call("call-2", first.action_id)
    assert first.status == "satisfied"
    assert second.status == "satisfied"
    assert first.evidence_call_ids == ["call-1"]
    assert second.evidence_call_ids == ["call-2"]


def test_required_action_unknown_id_falls_back_to_tool_match(tmp_path: Path) -> None:
    # 模型携带不存在的 action id(幻觉/拼写): 回退工具匹配,不因 id 偏差漏销
    # 真实执行(与复用 id 同根因,修复同源)。
    tool = _CountingTool(command=True)
    action = RequiredAction(
        action_id="required-1",
        source_turn_id="turn-user-1",
        kind="execute",
        allowed_tools=("command_tool",),
        effect_ceiling="mutating",
    )
    contract = build_effective_contract_snapshot(
        run_id="run-1",
        layers=(),
        required_actions=(action,),
    )
    call = replace(
        _call(tool, {"command": "true"}),
        call_id="call-1",
        required_action_id="required-nope",
    )
    execution = ToolExecutor().execute(
        ToolExecutorRequest(
            call=call,
            runtime_snapshot=_snapshot(tool),
            workspace_root=tmp_path,
            workspace_roots=(tmp_path,),
            operation_store_required=False,
        )
    )
    settle_required_action(contract, execution.call, execution.result)
    assert action.status == "satisfied"
    assert action.evidence_call_ids == ["call-1"]


def test_required_action_open_with_execution_evidence_completes() -> None:
    # G4-001 真机铁证(三轮 PASS/FAIL/FAIL): 评估拆解粒度与模型合并执行粒度
    # 不对齐(评估拆 3 个 action、模型 2 次调用合并完成)时,open action 没有
    # 自己的独立调用可销 → 旧逻辑 repair→unfinished 误杀已完成任务。修复:
    # 本 run 已有真实成功执行证据(executed_tools 非空) → 义务已有动作证据,
    # 收口门直接 complete;防假完成由产物/验收校验把关。
    first = RequiredAction(
        action_id="required-1",
        source_turn_id="turn-user-1",
        kind="execute",
        allowed_tools=("command_tool",),
        effect_ceiling="mutating",
    )
    second = RequiredAction(
        action_id="required-2",
        source_turn_id="turn-user-1",
        kind="execute",
        allowed_tools=("command_tool",),
        effect_ceiling="mutating",
    )
    contract = build_effective_contract_snapshot(
        run_id="run-1",
        layers=(),
        required_actions=(first, second),
    )
    assert (
        required_action_no_tool_decision(contract, has_succeeded_evidence=True)
        == "complete"
    )
    assert first.status == "satisfied"
    assert second.status == "satisfied"


def test_runtime_snapshot_rejects_policy_field_missing_from_public_schema() -> None:
    tool = _CountingTool()
    bad_effect_policy = replace(
        tool.runtime_policy,
        effect_resolver=EffectResolverPolicy(
            "read_only",
            by_parameter=(("missing", (("x", "dangerous"),)),),
        ),
    )
    bad_scope_policy = replace(
        tool.runtime_policy,
        resource_scopes=ResourceScopePolicy(parameter_names=("missing",)),
    )

    with pytest.raises(ValueError, match="effect resolver.*missing"):
        ToolRuntime(tool.model_spec, bad_effect_policy, tool)
    with pytest.raises(ValueError, match="resource scope.*missing"):
        ToolRuntime(tool.model_spec, bad_scope_policy, tool)


def test_runtime_snapshot_allows_only_explicit_internal_trusted_parameters() -> None:
    tool = _CountingTool()
    binding = TrustedParameterBinding(
        source_refs=("run_scope.run_id",),
        authority="host_authoritative",
    )
    accepted = replace(
        tool.runtime_policy,
        input_policy=ToolInputPolicy(
            internal_parameters=("run_id",),
            trusted_parameter_bindings=(("run_id", binding),),
        ),
    )
    undeclared = replace(
        tool.runtime_policy,
        input_policy=ToolInputPolicy(
            trusted_parameter_bindings=(("run_id", binding),),
        ),
    )

    assert ToolRuntime(tool.model_spec, accepted, tool).runtime_policy is accepted
    with pytest.raises(ValueError, match="trusted binding.*run_id"):
        ToolRuntime(tool.model_spec, undeclared, tool)


def test_policy_objects_combine_command_and_parameter_effects() -> None:
    policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy(
            "dangerous",
            strategy="command",
            command_parameter="command",
            by_parameter=(("run_in_background", (("true", "dangerous"),)),),
        )
    )

    assert (
        tool_effect_for_runtime_policy(
            policy,
            {"command": "ls -la", "run_in_background": False},
        )
        == "read_only"
    )
    assert (
        tool_effect_for_runtime_policy(
            policy,
            {"command": "python3 -m http.server", "run_in_background": False},
        )
        == "mutating"
    )
    assert (
        tool_effect_for_runtime_policy(
            policy,
            {"command": "ls -la", "run_in_background": True},
        )
        == "dangerous"
    )


def test_resource_scope_policy_rejects_mixed_strategies() -> None:
    with pytest.raises(ValueError, match="from_arguments.*static_scopes"):
        ResourceScopePolicy(
            mode="from_arguments",
            parameter_names=("path",),
            static_scopes=("fixed",),
        )


def test_controlled_exec_boolean_apply_has_exact_effect_mapping() -> None:
    policy = ControlledExecTool().runtime_policy

    assert tool_effect_for_runtime_policy(policy, {"apply": False}) == "read_only"
    assert tool_effect_for_runtime_policy(policy, {"apply": True}) == "dangerous"


def test_required_action_gate_still_blocks_open_actions_without_evidence() -> None:
    """护栏不因上一条修复失效: 有 open action 且无执行证据仍 block。"""
    contract = build_effective_contract_snapshot(
        run_id="run-open-action",
        layers=(),
        required_actions=[
            RequiredAction(
                action_id="a1",
                kind="execute",
                description="run the tests",
                success_criteria="tests pass",
                allowed_tools=("run_command",),
                effect_ceiling="mutating",
                acceptable_exits=("blocked",),
                source_turn_id="turn-1",
            )
        ],
        required_action_assessment={
            "source": "structured_contract",
            "requires_action": True,
        },
    )
    assert required_action_no_tool_decision(contract) in {"repair", "unfinished", "blocked"}
