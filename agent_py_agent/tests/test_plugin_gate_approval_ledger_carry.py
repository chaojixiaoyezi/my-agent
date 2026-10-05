# LLM: B5 9b 终审必须修 1 的回归：插件要求确认之后，`_resolve_tool_approval` 的每个出口都要把本次真实征询
#   交给归档与账本；9b 探针原来在批准/拒绝两条路上拿到空条目，无法审批那条 final_status 也记错。
#   用例走完 resolve → 归档投影 → persist_tool_runtime_ledger → B6 读回，每个出口一条，另有"批准不写两遍"。
#   只读结构化字段（verdict/final_status/条目数），不解析文案。
# 模块用途: 钉住审批阶段的门决定条目不被丢失、不被重复、无法审批时终态投影正确。
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_call_archive_record import _compact_result_envelope
from agent_py_agent.agent.agent_core.tool_loop import round_execution
from agent_py_agent.agent.contracts.tool_approval import ToolApprovalDecision
from agent_py_agent.agent.plugin_events.decision_ledger import plugin_gate_decisions_from_archive
from agent_py_agent.tests.test_plugin_gate_reapproval import _execute
from agent_py_agent.tests.test_plugin_gate_reapproval import reapproval_case as reapproval_case

_FIXTURES = (reapproval_case,)


# LLM: 复用既有重跑夹具的假宿主；consumer 只返回宿主审批决定，不造第二条征询路径。
# 类用途: 按给定决定类型应答 request_permission，供四条出口参数化。
class _Consumer:
    def __init__(self, decision: str) -> None:
        self.decision = decision

    def request_permission(self, value, *, cancellation_token=None):
        return ToolApprovalDecision(value["permission_id"], self.decision).to_dict()


# LLM: 只走真实 resolve → 归档投影，不落库；条目形状与 plugin_gate_decisions_from_archive 同口径。
# 函数用途: 跑一条审批出口，返回归档里的门决定条目与端到端事实。
def _resolve_and_archive(fixture_value, decision: str) -> dict:
    ctx = fixture_value
    params = SimpleNamespace(effective_on_chunk=_Consumer(decision), request_id="b5fix9b",
                             cancellation_token=SimpleNamespace(cancelled=False), runtime_approved_actions=[],
                             runtime_rejected_actions=[], tool_runtime_snapshot=ctx.snapshot, tool_context=[])

    def execute_one(execute_params):
        return _execute(ctx, execute_params.call, list(params.runtime_approved_actions))

    request = SimpleNamespace(agent=SimpleNamespace(), params=params, tool_rounds=1, actor="model",
                              calls=[ctx.call], execute_one=execute_one)
    resolved = round_execution._resolve_tool_approval(request, 1, ctx.call, ctx.first)
    entries = plugin_gate_decisions_from_archive(
        {"tool_result_envelope": _compact_result_envelope(resolved.result)})
    return {"resolved": resolved, "entries": entries, "handler_runs": len(ctx.probe.executed),
            "plugin_requests": len(ctx.harness.sent)}


# LLM: 用户批准后重跑会换掉 result；第一次真实征询的条目必须跟着走，否则设计第 9 节在主要 ask 路径上不成立。
# 函数用途: 批准出口的条目保留。
def test_approved_exit_keeps_the_first_real_consultation(reapproval_case):
    outcome = _resolve_and_archive(reapproval_case, "approved")
    assert [(row["verdict"], row["final_status"]) for row in outcome["entries"]] == [("ask", "ask")]
    assert outcome["resolved"].result.ok is True
    assert outcome["handler_runs"] == 1
    assert outcome["plugin_requests"] == 1


# 函数用途: 拒绝出口的条目保留（9b 实测原来是空）。
def test_denied_exit_keeps_the_first_real_consultation(reapproval_case):
    outcome = _resolve_and_archive(reapproval_case, "denied")
    assert [(row["verdict"], row["final_status"]) for row in outcome["entries"]] == [("ask", "ask")]
    assert outcome["resolved"].result.error_code == "APPROVAL_REJECTED"
    assert outcome["handler_runs"] == 0


# 函数用途: 取消出口的条目保留。
def test_cancelled_exit_keeps_the_first_real_consultation(reapproval_case):
    outcome = _resolve_and_archive(reapproval_case, "cancelled")
    assert [(row["verdict"], row["final_status"]) for row in outcome["entries"]] == [("ask", "ask")]
    assert outcome["resolved"].result.error_code == "CANCELLED"
    assert outcome["handler_runs"] == 0


# LLM: 无法审批是审批阶段才判定的结论，执行器阶段那次投影看不到；final_status 必须在这里改成不可用，
#   否则 B6 的"无法审批"计数会漏掉所有"审批时才判定"的情况（9b 实测原值是 ask）。
# 函数用途: 无法审批出口的终态投影。
def test_unavailable_exit_projects_unavailable_final_status(reapproval_case):
    outcome = _resolve_and_archive(reapproval_case, "unavailable")
    assert [(row["verdict"], row["final_status"]) for row in outcome["entries"]] == [
        ("ask", "PLUGIN_GATE_APPROVAL_UNAVAILABLE")]
    assert outcome["resolved"].result.error_code == "PLUGIN_GATE_APPROVAL_UNAVAILABLE"
    assert outcome["handler_runs"] == 0


# LLM: 9b 的要求：批准路径不能把同一次征询写两遍。这条专门数条目数——不是只看"有没有"。
#   重复会让 B6 的"最近 10 次收紧决定"和"无法审批"计数翻倍。
# 函数用途: 四条出口都只写一条，不重复。
@pytest.mark.parametrize("decision", ["approved", "denied", "cancelled", "unavailable"])
def test_each_exit_writes_exactly_one_entry(reapproval_case, decision):
    outcome = _resolve_and_archive(reapproval_case, decision)
    assert len(outcome["entries"]) == 1, [row.get("final_status") for row in outcome["entries"]]
    assert outcome["plugin_requests"] == 1
