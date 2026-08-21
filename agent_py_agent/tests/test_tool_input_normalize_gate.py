"""S-C1 回归测试：模型把数组/对象参数序列化成字符串时，normalize+validate 容错。

真机实锤（SUB-C/SUB-D/todo-verify）：MiniMax-M2.7 对 create_subagents.items、
dispatch_subagents.run_ids、send_guidance.target、task_progress.items 等数组/对象
参数全部序列化成 JSON 字符串，`_schema_decision` 只 validate 不 normalize，
导致 TOOL_PARAMETER_TYPE_INVALID 整批拒绝（批量创建子代理不可用、todo 面板
永远无数据渲染）。修复：schema 门先 normalize（无歧义字符串→原生类型纠正）
再 validate，纠正结果写回 call.arguments 供后续 effect/path/审批/幂等/执行
统一使用；concurrency 调度投影用同一份语义。
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.tooling import BaseTool, ToolHandlerOutcome
from agent_py_agent.agent.tooling.action_policy import (
    ActionDecision,
    ActionPolicy,
    ActionPolicyRequest,
)
from agent_py_agent.agent.tooling.concurrency import describe_tool_concurrency
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_test_call,
    execute_canonical_test_call,
    make_test_model_spec,
    make_test_runtime_policy,
    runtime_snapshot_for_tools,
)

ARRAY_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "items": {"type": "object"}},
        "content": {"type": "string"},
        "target": {
            "type": "object",
            "properties": {"kind": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    "required": ["items"],
    "additionalProperties": False,
}

# 模型（MiniMax-M2.7）实际发出的参数形态：数组/对象被序列化成字符串。
STRING_ITEMS = '[{"id": "1", "content": "first"}, {"id": "2", "content": "second"}]'
STRING_TARGET = '{"kind": "todo"}'


class _ArrayTool(BaseTool):
    def __init__(self, handler=None):
        self.model_spec = make_test_model_spec("array_tool", input_schema=ARRAY_SCHEMA)
        self.runtime_policy = make_test_runtime_policy(
            effect="read_only", concurrency_mode="parallel_safe"
        )
        self._handler = handler

    def execute(self, params: dict) -> ToolHandlerOutcome:
        if self._handler is not None:
            return self._handler(params)
        return ToolHandlerOutcome(self.model_spec.name, True, "ok")


def _policy_decision(arguments: dict[str, object]) -> tuple[ActionDecision, ToolCall]:
    tools = {"array_tool": _ArrayTool()}
    snapshot = runtime_snapshot_for_tools(tools, run_id="run-normalize")
    call = canonical_test_call(snapshot, "array_tool", arguments)
    root = Path("/tmp/my-agent-workspace")
    decision = ActionPolicy().decide(
        ActionPolicyRequest(
            call=call,
            runtime_snapshot=snapshot,
            workspace_root=root,
            workspace_roots=(root,),
        )
    )
    return decision, call


def test_schema_decision_coerces_string_items_to_array() -> None:
    decision, call = _policy_decision(
        {"items": STRING_ITEMS, "content": "数据整理任务清单"}
    )
    assert decision.allowed, decision.reason_codes
    assert isinstance(call.arguments["items"], list)
    assert call.arguments["items"][0] == {"id": "1", "content": "first"}
    # 非数组字段不受影响
    assert call.arguments["content"] == "数据整理任务清单"


def test_schema_decision_coerces_string_target_object() -> None:
    decision, call = _policy_decision(
        {"items": [{"id": "1", "content": "first"}], "target": STRING_TARGET}
    )
    assert decision.allowed, decision.reason_codes
    assert call.arguments["target"] == {"kind": "todo"}


def test_schema_decision_still_rejects_invalid_string_array() -> None:
    # 不是合法 JSON 的字符串数组保持拒绝（fail-closed，不做有损猜测）
    decision, _call = _policy_decision({"items": "not-a-json-array"})
    assert not decision.allowed
    assert "TOOL_PARAMETER_TYPE_INVALID" in decision.reason_codes


def test_normalize_is_deterministic_for_args_hash() -> None:
    # 同一 string 输入 → 同一 corrected arguments → args_hash 稳定（幂等/审批身份不变）
    _d1, call1 = _policy_decision({"items": STRING_ITEMS})
    _d2, call2 = _policy_decision({"items": STRING_ITEMS})
    assert call1.args_hash == call2.args_hash
    # 与直接传数组的 hash 一致（纠正后参数等价）
    _d3, call3 = _policy_decision({"items": json.loads(STRING_ITEMS)})
    assert call1.args_hash == call3.args_hash


def test_executor_full_path_runs_handler_with_normalized_arguments(tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def handler(params: dict[str, object]) -> ToolHandlerOutcome:
        seen.update(params)
        return ToolHandlerOutcome("array_tool", True, "handler-ran")

    tools = {"array_tool": _ArrayTool(handler)}
    execution = execute_canonical_test_call(
        tmp_path,
        tools=tools,
        tool_name="array_tool",
        arguments={"items": STRING_ITEMS, "content": "create"},
    )
    assert execution.decision.allowed, execution.decision.reason_codes
    assert execution.result.ok
    assert seen["items"] == json.loads(STRING_ITEMS)


def test_concurrency_projection_accepts_string_array_after_normalize() -> None:
    tools = {"array_tool": _ArrayTool()}
    snapshot = runtime_snapshot_for_tools(tools, run_id="run-concurrency")
    call = canonical_test_call(snapshot, "array_tool", {"items": STRING_ITEMS})
    descriptor = describe_tool_concurrency(snapshot, call, workspace_root=Path("/tmp"))
    assert descriptor.barrier_reason != "invalid_arguments", descriptor
