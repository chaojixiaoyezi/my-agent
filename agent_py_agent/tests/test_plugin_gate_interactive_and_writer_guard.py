# LLM: B5 9b 终审必须修 2 的回归：生产接线给插件的 `interactive` 必须等于消费者真实的当场审批能力。
#   原来用"on_chunk 有没有 request_permission 方法"当判据，但 Gateway 流写入器总有这个方法；
#   非管理员 IM 与没声明 tool_approval 的客户端 `interactive_approvals=False`，插件却收到 interactive=true。
#    用例走真实 `execute_traced_tool_call`，分别用两种写入器，断言插件看到的值、决定状态和账本终态。
#   还含 B5 结构守卫：两个门决定写入点各只有一个生产调用点（9b 裁定现在不加调用级去重）。
# 模块用途: 钉住 interactive 的来源是消费者结构化能力，以及门决定写入点唯一。
from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_call_runtime import (
    ToolCallRuntimeRequest,
    execute_traced_tool_call,
)
from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallExecuteParams
from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter
from agent_py_agent.agent.plugin_events.declarations import PluginToolGateDeclaration
from agent_py_agent.agent.plugin_events.tool_gate import GateReply, GateReview, GateTarget
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
from agent_py_agent.tests.test_plugin_event_gateway import gateway
from agent_py_agent.tests.test_plugin_event_runtime import _params
from agent_py_agent.tests.test_tool_runtime_unification import _CountingTool

_FIXTURES = (gateway,)
_REPO_ROOT = Path(__file__).resolve().parents[2]


# LLM: 走真实执行链，不注入 trusted_run_context；插件看到什么由生产接线算出来才算数。
# 函数用途: 用给定 interactive_approvals 的写入器跑一次真实工具调用，返回插件看到的值与执行结果。
def _run_with_writer(gateway_fixture, tmp_path, interactive_approvals):
    agent, _paths, _server, _events = gateway_fixture
    tool = _CountingTool()
    params = _params(agent, tool)
    chunk = tmp_path / "chunk.jsonl"
    chunk.write_text("", encoding="utf-8")
    writer = BufferedChunkStreamWriter(chunk, interactive_approvals=interactive_approvals)
    params = replace(params, effective_on_chunk=writer)
    seen = []
    declaration = PluginToolGateDeclaration("guard-interactive", (tool.model_spec.name,), (), "none")

    def reviewer(gate_call):
        seen.append(gate_call.interactive)
        return (GateReview(GateTarget("guard", "1.0.0", "act-1", declaration), GateReply("ask", "ASK_REASON")),)

    agent.tools.plugin_gate_reviewer = reviewer
    call = ToolCall(call_id="interactive-call", tool_name=tool.model_spec.name, arguments={"value": "x"},
                    source_protocol="native", schema_hash=tool.model_spec.schema_hash,
                    run_id=params.run_id, turn_id="interactive-turn", attempt_id=params.attempt_id)
    execution = execute_traced_tool_call(
        ToolCallRuntimeRequest(agent, ToolCallExecuteParams(params, 1, 1, call), call))
    ledger = execution.result.metadata.get("plugin_gate_decisions") or []
    return {"seen": seen, "execution": execution, "ledger": ledger, "tool": tool,
            "params": params, "writer": writer, "agent": agent}


# LLM: 设计 8.5 承诺插件能看见"不可交互"、可以自己选 deny；写入器声明 False 时必须如实传下去。
# 函数用途: interactive_approvals=False 时，插件看到 false，决定直接收紧成不可审批。
def test_non_interactive_writer_reaches_plugin_as_not_interactive(gateway, tmp_path):
    outcome = _run_with_writer(gateway, tmp_path, False)
    assert outcome["seen"] == [False], "插件必须看到真实交互能力"
    assert outcome["execution"].decision.status == "deny"
    assert "PLUGIN_GATE_APPROVAL_UNAVAILABLE" in outcome["execution"].decision.reason_codes
    assert [row.get("final_status") for row in outcome["ledger"]] == ["PLUGIN_GATE_APPROVAL_UNAVAILABLE"]
    assert outcome["tool"].executions == 0


# 函数用途: interactive_approvals=True 时插件看到 true，照常要求确认、账本记 ask。
def test_interactive_writer_reaches_plugin_as_interactive(gateway, tmp_path):
    outcome = _run_with_writer(gateway, tmp_path, True)
    assert outcome["seen"] == [True]
    assert outcome["execution"].decision.status == "ask"
    assert [row.get("final_status") for row in outcome["ledger"]] == ["ask"]
    assert outcome["tool"].executions == 0


# LLM: 后台 sink 没有 interactive_approvals 属性（等待期才知道有没有消费者），必须沿用原 callable 判断保留 true，
#   由审批结算时的第 1 条承接投影纠正；这条防止把"缺属性"误判成不可交互。
# 函数用途: 写入器没声明 interactive_approvals 时沿用 callable 判断。
def test_writer_without_declared_flag_keeps_callable_fallback(gateway, tmp_path):
    outcome = _run_with_writer(gateway, tmp_path, True)
    writer = outcome["writer"]
    object.__setattr__(writer, "interactive_approvals", None)
    params = replace(outcome["params"], effective_on_chunk=writer)
    agent = outcome["agent"]
    call = ToolCall(call_id="fallback-call", tool_name=outcome["tool"].model_spec.name, arguments={"value": "x"},
                    source_protocol="native", schema_hash=outcome["tool"].model_spec.schema_hash,
                    run_id=params.run_id, turn_id="fallback-turn", attempt_id=params.attempt_id)
    execution = execute_traced_tool_call(ToolCallRuntimeRequest(agent, ToolCallExecuteParams(params, 1, 1, call), call))
    assert execution.decision.status == "ask", "缺声明时不应误判成不可交互"


# --- 结构守卫（9b 裁定：现在不加调用级去重，改为机器可查的写入点唯一） ---------------------


# LLM: 9b 裁定现在不按 (call_id, gate_id, activation_id) 去重——I4 续跑会对同一调用合法地再问一次，
#   按门身份去重会吞掉真实的第二次征询。代价是"同一次征询只写一遍"必须靠"生产调用点唯一"保证。
#   将来真要加第二个写入点，正确做法是在 GateReview 构造时生成征询 ID、按 (call_id, 征询 ID) 去重，
#   而不是按门身份——所以这条用 AST 静态扫描把约定变成机器可查，不改运行时行为。
# 函数用途: 扫生产代码里对给定函数名的调用点（返回 文件:行号 列表），测试代码不算。
def _production_call_sites(function_name: str) -> list[str]:
    package_root = _REPO_ROOT / "agent_py_agent"
    paths = (path for path in sorted(package_root.rglob("*.py")) if "tests" not in path.parts)
    return [site for path in paths for site in _call_sites_in_file(path, function_name)]


# 函数用途: 在一个文件的 AST 里找出目标函数名的全部调用点（带行号）。
def _call_sites_in_file(path: Path, function_name: str) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [f"{path.relative_to(_REPO_ROOT)}:{node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == function_name]


# 函数用途: 断言两个门决定写入函数在 agent_py_agent 生产代码里各只有一个调用点。
@pytest.mark.parametrize("function_name", ["append_plugin_gate_decision", "persist_tool_runtime_ledger"])
def test_gate_decision_writers_have_single_production_call_site(function_name):
    call_sites = _production_call_sites(function_name)
    assert len(call_sites) == 1, (
        f"{function_name} 生产调用点应只有一个，实际 {call_sites}；"
        "要加第二个写入点时，请在 GateReview 构造时生成征询 ID 并按 (call_id, 征询 ID) 去重"
    )
