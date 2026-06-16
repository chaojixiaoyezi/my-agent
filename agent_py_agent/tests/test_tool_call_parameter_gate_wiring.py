"""Step0b 防回归：把 tool_call_policy 参数校验器接到工具执行热路径。

覆盖：
- ToolCallPolicy builder 从 ToolSpec 现场构造 required + 顶层 type。
- evaluate_tool_call_parameter_gate 门级行为（缺参/类型错/合法/无 policy）。
- 真实 registry 热路径：缺 required 拦截、顶层 type 错拦截、合法放行。
- text 协议入口与 native(scoped envelope) 入口同样生效。
- 内部 __ 前缀注入键不会被误判为缺参/多参。
- TOOL_PARAMETER_REQUIRED / TOOL_PARAMETER_TYPE_INVALID 已在 error_taxonomy 注册为 retryable。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from agent_py_agent.agent.action_protocol import RunScope
from agent_py_agent.agent.action_protocol import ToolCallEnvelope as ActionToolCallEnvelope
from agent_py_agent.agent.contracts.error_taxonomy import error_contract
from agent_py_agent.agent.contracts.gates.adapters import evaluate_tool_call_parameter_gate
from agent_py_agent.agent.tooling.models import BaseTool, ToolExecutionResult, ToolSpec
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.agent.tooling.registry_gate_policy import (
    tool_call_policy_for_spec,
)


def _registry(root: Path) -> ToolRegistry:
    return ToolRegistry(
        ToolRegistryParams(
            workspace_root=root,
            max_chars=6000,
            max_entries=200,
            max_matches=50,
            web_max_chars=12000,
            http_timeout=30,
            catalog_limit=20,
            retrieval_limit=3,
            vector_search_enabled=False,
            shell_tool_timeout=30,
            shell_tool_output_max_chars=200,
        )
    )


class _StubTool(BaseTool):
    def __init__(self, spec: ToolSpec) -> None:
        self.spec = spec

    def execute(self, params: dict) -> ToolExecutionResult:  # pragma: no cover - not invoked
        return ToolExecutionResult(self.spec.name, True, "ok")


def _spec(**overrides) -> ToolSpec:
    base = {
        "name": "query_logs",
        "category": "security",
        "description": "query logs",
        "use_cases": [],
        "avoid_when": [],
        "keywords": [],
        "parameters": {"src_ip": "源 IP", "start_time": "起始时间", "limit": "条数"},
    }
    base.update(overrides)
    return ToolSpec(**base)


# ---- ToolCallPolicy builder ----


def test_tool_call_policy_builder_flattens_required_and_top_level_types() -> None:
    spec = _spec(
        required_parameters=["src_ip", "start_time"],
        parameter_schema={
            "src_ip": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1},
            "domains": {"type": "array", "items": {"type": "string"}},
        },
    )
    policy = tool_call_policy_for_spec(_StubTool(spec))

    assert policy is not None
    assert policy.required_parameters == {"query_logs": ("src_ip", "start_time")}
    # 顶层 type 拍平，items/minimum 被丢弃（policy 只认顶层 type 字符串）。
    assert policy.parameter_types == {
        "query_logs": {"src_ip": "string", "limit": "integer", "domains": "array"}
    }


def test_tool_call_policy_builder_returns_none_without_declarations() -> None:
    # 既无 required 又无 parameter_schema -> None（gate 直接放行，零开销）。
    assert tool_call_policy_for_spec(_StubTool(_spec())) is None
    assert tool_call_policy_for_spec(None) is None


def test_tool_call_policy_builder_skips_schema_entries_without_type() -> None:
    spec = _spec(
        parameter_schema={
            "src_ip": {"type": "string"},
            "weird": {"description": "no type here"},
            "nested": {"type": ["string", "null"]},  # 非 str type -> 跳过
        }
    )
    policy = tool_call_policy_for_spec(_StubTool(spec))

    assert policy is not None
    assert policy.parameter_types == {"query_logs": {"src_ip": "string"}}


# ---- gate-level behavior ----


def test_parameter_gate_blocks_missing_required_with_precise_code() -> None:
    policy = tool_call_policy_for_spec(
        _StubTool(_spec(required_parameters=["src_ip", "start_time"]))
    )
    decision = evaluate_tool_call_parameter_gate(
        {"tool": "query_logs", "src_ip": "1.1.1.1"}, policy
    )

    assert decision.allowed is False
    assert decision.finding_codes == ("TOOL_PARAMETER_REQUIRED",)
    assert "start_time" in decision.findings[0].evidence["fields"]


def test_parameter_gate_blocks_wrong_top_level_type_with_precise_code() -> None:
    policy = tool_call_policy_for_spec(
        _StubTool(_spec(parameter_schema={"limit": {"type": "integer"}}))
    )
    decision = evaluate_tool_call_parameter_gate(
        {"tool": "query_logs", "limit": "ten"}, policy
    )

    assert decision.allowed is False
    assert decision.finding_codes == ("TOOL_PARAMETER_TYPE_INVALID",)
    assert "limit:integer" in decision.findings[0].evidence["fields"]


def test_parameter_gate_allows_valid_call_and_missing_policy() -> None:
    policy = tool_call_policy_for_spec(
        _StubTool(
            _spec(
                required_parameters=["src_ip"],
                parameter_schema={"limit": {"type": "integer"}},
            )
        )
    )
    valid = evaluate_tool_call_parameter_gate(
        {"tool": "query_logs", "src_ip": "1.1.1.1", "limit": 10}, policy
    )
    no_policy = evaluate_tool_call_parameter_gate({"tool": "query_logs"}, None)

    assert valid.allowed is True
    assert no_policy.allowed is True


def test_parameter_gate_only_acts_on_required_and_type_not_enum() -> None:
    # 灰度：只接 required + 顶层 type。enum/minimum 不进 policy，故不拦。
    policy = tool_call_policy_for_spec(
        _StubTool(
            _spec(
                parameter_schema={"mode": {"type": "string", "enum": ["a", "b"]}}
            )
        )
    )
    # mode 是合法 string，即便不在 enum 里也放行（不比 native input_schema 更严）。
    decision = evaluate_tool_call_parameter_gate(
        {"tool": "query_logs", "mode": "not_in_enum"}, policy
    )

    assert decision.allowed is True


# ---- real registry hot path (native dict entry) ----


def test_hot_path_blocks_missing_required_parameter() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        registry = _registry(Path(tmp))
        result = registry.execute_call({"tool": "read_file"}, allowed_tools=["read_file"])

    assert result.ok is False
    assert result.error_code == "TOOL_PARAMETER_REQUIRED"
    assert result.retryable is True
    assert result.recommended_action == "repair_tool_arguments"


def test_hot_path_blocks_wrong_top_level_type() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "input.txt").write_text("hello\n", encoding="utf-8")
        registry = _registry(root)
        result = registry.execute_call(
            {"tool": "read_file", "path": "input.txt", "offset": "not-an-int"},
            allowed_tools=["read_file"],
        )

    assert result.ok is False
    assert result.error_code == "TOOL_PARAMETER_TYPE_INVALID"
    assert result.retryable is True


def test_hot_path_allows_valid_call() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "input.txt").write_text("hello\n", encoding="utf-8")
        registry = _registry(root)
        result = registry.execute_call(
            {"tool": "read_file", "path": "input.txt"},
            allowed_tools=["read_file"],
        )

    assert result.ok is True
    assert result.error_code == ""


# ---- text protocol entry ----


def test_text_protocol_path_blocks_missing_required_parameter() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        registry = _registry(Path(tmp))
        parsed = registry.parse_tool_calls('[TOOL_CALL]{"tool": "read_file"}[/TOOL_CALL]')
        result = registry.execute_call(parsed[0], allowed_tools=["read_file"])

    assert result.ok is False
    assert result.error_code == "TOOL_PARAMETER_REQUIRED"


def test_text_protocol_path_allows_valid_call() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "input.txt").write_text("hello\n", encoding="utf-8")
        registry = _registry(root)
        parsed = registry.parse_tool_calls(
            '[TOOL_CALL]{"tool": "read_file", "path": "input.txt"}[/TOOL_CALL]'
        )
        result = registry.execute_call(parsed[0], allowed_tools=["read_file"])

    assert result.ok is True


# ---- scoped envelope: internal __ injection must not false-reject ----


def test_scoped_envelope_internal_injection_does_not_false_reject() -> None:
    # __run_scope/__tool_call_id 在 gate 之后注入；合法 scoped 调用必须执行成功。
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "input.txt").write_text("hello\n", encoding="utf-8")
        registry = _registry(root)
        scope = RunScope(run_id="run-1", task_id="task-1", request_id="req-1")
        envelope = ActionToolCallEnvelope(
            call_id="call-1",
            source="native",
            tool_name="read_file",
            input={"path": "input.txt"},
            scope=scope,
        )
        result = registry.execute_call(envelope, allowed_tools=["read_file"])

    assert result.ok is True
    assert result.error_code == ""


def test_scoped_envelope_missing_required_still_blocks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        registry = _registry(Path(tmp))
        scope = RunScope(run_id="run-1", task_id="task-1", request_id="req-1")
        envelope = ActionToolCallEnvelope(
            call_id="call-2",
            source="native",
            tool_name="read_file",
            input={},
            scope=scope,
        )
        result = registry.execute_call(envelope, allowed_tools=["read_file"])

    assert result.ok is False
    assert result.error_code == "TOOL_PARAMETER_REQUIRED"


# ---- error taxonomy registration ----


def test_parameter_error_codes_are_registered_retryable() -> None:
    for code in ("TOOL_PARAMETER_REQUIRED", "TOOL_PARAMETER_TYPE_INVALID"):
        contract = error_contract(code)
        assert contract.code == code
        assert contract.retryable is True
        assert contract.recommended_action == "repair_tool_arguments"
