# LLM: B5 9b 终审必须修 1 的回归：插件要求确认之后，`_resolve_tool_approval` 的每个出口都要把本次真实征询
#   交给归档与账本；9b 探针原来在批准/拒绝两条路上拿到空条目，无法审批那条 final_status 也记错。
#   用例走完 resolve → 归档投影 → persist_tool_runtime_ledger → B6 读回，每个出口一条，另有"批准不写两遍"。
#   只读结构化字段（verdict/final_status/条目数），不解析文案。
# 模块用途: 钉住审批阶段的门决定条目不被丢失、不被重复、无法审批时终态投影正确。
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_call_archive_record import _compact_result_envelope
from agent_py_agent.agent.agent_core.tool_loop import round_execution
from agent_py_agent.agent.contracts.model_call_ledger import (
    MODEL_CALL_ADMISSION_CLOSED_ERROR_CODE,
    ModelCallAdmissionClosure,
)
from agent_py_agent.agent.contracts.tool_approval import ToolApprovalDecision
from agent_py_agent.agent.plugin_events.decision_ledger import plugin_gate_decisions_from_archive
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.tooling.runtime_contracts import tool_arguments_hash
from agent_py_agent.tests.test_plugin_event_display import decisions as b6_decisions
from agent_py_agent.tests.test_plugin_event_display import info_message
from agent_py_agent.tests.test_plugin_gate_event_combination import _persist_execution
from agent_py_agent.tests.test_plugin_gate_reapproval import _execute
from agent_py_agent.tests.test_plugin_gate_reapproval import reapproval_case as reapproval_case
from agent_py_agent.tests.test_plugin_management import manager
from agent_py_agent.tests.test_plugin_manifest_v8 import _bundle, _v8

_FIXTURES = (reapproval_case,)


# LLM: 复用既有重跑夹具的假宿主；consumer 只返回宿主审批决定，不造第二条征询路径。
# 类用途: 按给定决定类型应答 request_permission，供四条出口参数化。
class _Consumer:
    def __init__(self, decision: str) -> None:
        self.decision = decision
        self.calls = 0

    def request_permission(self, value, *, cancellation_token=None):
        self.calls += 1
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


# LLM: 用临时 owner 安装可被真实 B6 info 命中的 v8 壳；仅模拟插件审批回执，不启动插件进程或 Gateway。
# 函数用途: 为审批出口的持久化回读建立隔离插件目录和真实 PluginManagement 服务。
def _b6_service(tmp_path, plugin_ids: tuple[str, ...]):
    root = tmp_path / "b6-owner"
    root.mkdir(parents=True, exist_ok=True)
    service, _source = manager(root)
    for plugin_id in plugin_ids:
        package = root / f"{plugin_id}.zip"
        package.write_bytes(_bundle(_v8(
            plugin_id=plugin_id,
            version="1.0.0",
            tool_gates=[{"id": "check", "tools": ["gate_probe"], "effects": [], "arguments": "none"}],
        )))
        result = service.command(
            f'/plugins install "{package}"', revision=service.catalog().revision,
            request_id=f"install-{plugin_id}",
        )
        assert result["state"] == "succeeded", result
    return service


# LLM: 保留原 ToolCall 与快照的 run/attempt 身份；只让首个 fake reviewer row 对应 B6 临时安装的插件。
# 函数用途: 清掉 fixture 的旧发送记录，按指定首插件重新生成真实 ask，供归档持久化链复用。
def _prepare_b6_case(ctx, plugin_ids: tuple[str, ...]) -> None:
    # 保留原 fixture 的 ToolCall 身份与默认 guard/act-1，避免让执行器看到快照外的 run/attempt。
    ctx.harness.rows = [ctx.harness.row(plugin_ids[0])]
    ctx.harness.sent.clear()
    ctx.probe.executed.clear()
    ctx.first = _execute(ctx, ctx.call, [])
    assert ctx.first.decision.status == "ask"


# LLM: B6 info 与持久写入共用同一临时 owner/runtime.db；AgentRun 的 run_id 必须等于真实 ToolCall 身份。
# 函数用途: 在测试 owner 库注册当前调用对应的运行行，供产品 persist 入口追加事件。
def _ledger_host_for_call(service, root, run_id: str):
    owner_home = service.context.owner.home_dir
    repo = RuntimeRepository(runtime_db_path(owner_home))
    task = repo.create_task(owner_id="local/main")
    task_run = repo.create_task_run(task_id=task["task_id"])
    repo.create_agent_run(task_run_id=task_run["task_run_id"], run_id=run_id)
    return SimpleNamespace(
        root=root,
        config=AgentConfig(),
        home_paths=SimpleNamespace(owner_home_dir=owner_home),
        subagents=SimpleNamespace(runtime_db=repo),
    )


# LLM: 各出口用例只差这几项结构化旋钮；收成小数据类，让辅助函数参数不超过 4 个（code-size 参数门）。
# 类用途: 描述一次审批出口用例：审批决定、安装的插件、有无审批消费者、准入关门时机、是否先有同参拒绝、重跑钩子。
@dataclass(frozen=True)
class _B6Exit:
    decision: str
    plugin_ids: tuple[str, ...] = ("guard",)
    has_consumer: bool = True
    close_reads: tuple[ModelCallAdmissionClosure | None, ...] = ()
    repeated_rejection: bool = False
    on_rerun: object = None


# LLM: 审批参数只放 resolver 会读的结构化字段；先有同参拒绝时按 args_hash 预置一条，模拟"同工具同参数已拒绝"。
# 函数用途: 构造 _resolve_tool_approval 所需的 params 替身。
def _approval_params(ctx, consumer, repeated_rejection: bool) -> SimpleNamespace:
    params = SimpleNamespace(
        effective_on_chunk=consumer,
        request_id="b5fix9bt",
        cancellation_token=SimpleNamespace(cancelled=False),
        runtime_approved_actions=[],
        runtime_rejected_actions=[],
        tool_runtime_snapshot=ctx.snapshot,
        tool_context=[],
    )
    if repeated_rejection:
        params.runtime_rejected_actions.append({
            "tool_name": ctx.call.tool_name,
            "args_hash": tool_arguments_hash(ctx.call.arguments),
            "decision": "denied",
        })
    return params


# LLM: 读回链路固定为 compact 投影 → 真实 archive builder/writer 落临时 RuntimeRepository → B6 查询与 /plugins info；
#   每个插件各读一次，供 _assert_full_b6_chain 逐层比对。
# 函数用途: 把一次 resolver 结果经归档落账后，返回 compact、归档、持久事件和展示文本的结构化回读。
def _read_back(service, ctx, resolved, plugin_ids: tuple[str, ...]) -> dict:
    compact = {"tool_result_envelope": _compact_result_envelope(resolved.result)}
    agent = _ledger_host_for_call(service, ctx.root, resolved.call.run_id)
    archive = _persist_execution(agent, resolved)
    return {
        "compact_entries": plugin_gate_decisions_from_archive(compact),
        "archive_entries": plugin_gate_decisions_from_archive(archive),
        "persisted": {plugin_id: b6_decisions(service, plugin_id=plugin_id) for plugin_id in plugin_ids},
        "messages": {plugin_id: info_message(service, plugin_id=plugin_id) for plugin_id in plugin_ids},
    }


# LLM: 审批之后只能由归档 envelope 提供门决定；经真实 archive builder/writer 写临时 RuntimeRepository，再读实际 B6 info。
# 函数用途: 运行 resolver 出口并返回 compact、归档、持久事件及 /plugins info 的结构化回读。
def _resolve_persist_and_read(ctx, service, case: _B6Exit, monkeypatch):
    _prepare_b6_case(ctx, (case.plugin_ids[0],))
    consumer = _Consumer(case.decision) if case.has_consumer else None
    params = _approval_params(ctx, consumer, case.repeated_rejection)
    reads = iter(case.close_reads)
    monkeypatch.setattr(round_execution, "model_call_admission_closure", lambda: next(reads, None))

    def execute_one(execute_params):
        if case.on_rerun is not None:
            case.on_rerun(ctx)
        return _execute(ctx, execute_params.call, list(params.runtime_approved_actions))

    request = SimpleNamespace(
        agent=SimpleNamespace(), params=params, tool_rounds=1, actor="model",
        calls=[ctx.call], execute_one=execute_one,
    )
    resolved = round_execution._resolve_tool_approval(request, 1, ctx.call, ctx.first)
    outcome = _read_back(service, ctx, resolved, case.plugin_ids)
    outcome.update(
        resolved=resolved,
        consumer=consumer,
        handler_runs=len(ctx.probe.executed),
        plugin_requests=len(ctx.harness.sent),
    )
    return outcome


# LLM: 同一次真实征询在 compact、归档、权威 runtime_events 与 B6 查询/展示只能出现一次；状态仅从结构化决定字段核对。
# 函数用途: 对每个安装插件验证完整链条、最终状态和重复写入防线。
def _assert_full_b6_chain(outcome, plugin_ids: tuple[str, ...]) -> None:
    assert outcome["archive_entries"] == outcome["compact_entries"]
    for plugin_id in plugin_ids:
        expected = [row for row in outcome["compact_entries"] if row["plugin_id"] == plugin_id]
        actual = outcome["persisted"][plugin_id]
        message = outcome["messages"][plugin_id]
        assert [(row["verdict"], row["final_status"]) for row in actual] == [
            (row["verdict"], row["final_status"]) for row in expected
        ]
        assert len(actual) == len(expected)
        assert message.count("征询 ") == len(expected)
        for row in expected:
            assert f"征询 {row['outcome']}" in message
            assert f"结论 {row['final_status']}" in message
            assert f"原因码 {row['reason_code']}" in message
        unavailable = sum(row["final_status"] == "PLUGIN_GATE_APPROVAL_UNAVAILABLE" for row in expected)
        assert f"无法审批：{unavailable} 次" in message


# LLM: 停机准入在请求审批前复核；关门路径不得弹出审批或丢失首次真实征询。
# 函数用途: 完整验证发审批前已关门的决定进入 B6。
def test_b6_chain_preserves_decision_when_admission_closed_before_approval(reapproval_case, tmp_path, monkeypatch):
    service = _b6_service(tmp_path, ("guard",))
    closure = ModelCallAdmissionClosure("HostShutdownInterrupted", MODEL_CALL_ADMISSION_CLOSED_ERROR_CODE)
    outcome = _resolve_persist_and_read(
        reapproval_case, service, _B6Exit("approved", close_reads=(closure,)), monkeypatch,
    )
    _assert_full_b6_chain(outcome, ("guard",))
    assert outcome["resolved"].result.error_code == "HOST_SHUTDOWN_TOOL_NOT_STARTED"
    assert [(row["verdict"], row["final_status"]) for row in outcome["compact_entries"]] == [("ask", "ask")]
    assert outcome["consumer"].calls == 0
    assert outcome["handler_runs"] == 0 and outcome["plugin_requests"] == 1


# LLM: 缺少审批消费者与“消费者返回 unavailable”是两个出口，均须带同一征询且正确投影 B6 终态。
# 函数用途: 完整验证没有审批 consumer 时仍将决定记为无法审批。
def test_b6_chain_preserves_decision_when_consumer_is_missing(reapproval_case, tmp_path, monkeypatch):
    service = _b6_service(tmp_path, ("guard",))
    outcome = _resolve_persist_and_read(
        reapproval_case, service, _B6Exit("approved", has_consumer=False), monkeypatch,
    )
    _assert_full_b6_chain(outcome, ("guard",))
    assert [(row["verdict"], row["final_status"]) for row in outcome["compact_entries"]] == [
        ("ask", "PLUGIN_GATE_APPROVAL_UNAVAILABLE")
    ]
    assert outcome["resolved"].result.error_code == "PLUGIN_GATE_APPROVAL_UNAVAILABLE"
    assert outcome["handler_runs"] == 0 and outcome["plugin_requests"] == 1


# LLM: 审批等待期间再次读取准入；关门后即使收到批准也不执行，原 ask 仍要经归档落账。
# 函数用途: 完整验证等待审批时关门的路径与 B6 回读。
def test_b6_chain_preserves_decision_when_admission_closes_during_wait(reapproval_case, tmp_path, monkeypatch):
    service = _b6_service(tmp_path, ("guard",))
    closure = ModelCallAdmissionClosure("HostShutdownInterrupted", MODEL_CALL_ADMISSION_CLOSED_ERROR_CODE)
    outcome = _resolve_persist_and_read(
        reapproval_case, service, _B6Exit("approved", close_reads=(None, closure)), monkeypatch,
    )
    _assert_full_b6_chain(outcome, ("guard",))
    assert outcome["resolved"].result.error_code == "HOST_SHUTDOWN_TOOL_NOT_STARTED"
    assert [(row["verdict"], row["final_status"]) for row in outcome["compact_entries"]] == [("ask", "ask")]
    assert outcome["consumer"].calls == 1
    assert outcome["handler_runs"] == 0 and outcome["plugin_requests"] == 1


# LLM: 批准后重跑若插件集合变了，必须同时保留原 ask 和新插件的真实 ask；不可只测原集合不变的批准。
# 函数用途: 让重跑期间新增插件，并核对两条不同插件决定各自进入实际 B6 info。
def test_b6_chain_records_new_plugin_consultation_after_approved_rerun(reapproval_case, tmp_path, monkeypatch):
    service = _b6_service(tmp_path, ("guard", "new-plugin"))

    def add_plugin(ctx):
        ctx.harness.rows.append(ctx.harness.row("new-plugin", "act-new-plugin"))

    outcome = _resolve_persist_and_read(
        reapproval_case, service,
        _B6Exit("approved", plugin_ids=("guard", "new-plugin"), on_rerun=add_plugin), monkeypatch,
    )
    _assert_full_b6_chain(outcome, ("guard", "new-plugin"))
    assert [(row["plugin_id"], row["verdict"], row["final_status"])
            for row in outcome["compact_entries"]] == [
        ("guard", "ask", "ask"), ("new-plugin", "ask", "ask")
    ]
    assert outcome["resolved"].decision.status == "ask"
    assert outcome["consumer"].calls == 1
    assert outcome["handler_runs"] == 0 and outcome["plugin_requests"] == 2


# LLM: 审批消费者明确返回 unavailable 时才进入该分支；不得与 consumer 缺失路径互相替代。
# 函数用途: 完整验证审批阶段返回 unavailable 的终态和 B6 计数。
def test_b6_chain_projects_consumer_unavailable(reapproval_case, tmp_path, monkeypatch):
    service = _b6_service(tmp_path, ("guard",))
    outcome = _resolve_persist_and_read(
        reapproval_case, service, _B6Exit("unavailable"), monkeypatch,
    )
    _assert_full_b6_chain(outcome, ("guard",))
    assert [(row["verdict"], row["final_status"]) for row in outcome["compact_entries"]] == [
        ("ask", "PLUGIN_GATE_APPROVAL_UNAVAILABLE")
    ]
    assert outcome["resolved"].result.error_code == "PLUGIN_GATE_APPROVAL_UNAVAILABLE"
    assert outcome["consumer"].calls == 1
    assert outcome["handler_runs"] == 0 and outcome["plugin_requests"] == 1


# LLM: 拒绝和取消都不运行 handler；carry 必须保留原询问，审批选择不改写插件 verdict/final_status。
# 函数用途: 完整验证拒绝或取消出口均只持久化一次原 ask。
@pytest.mark.parametrize(
    "exit_case", [("denied", "APPROVAL_REJECTED"), ("cancelled", "CANCELLED")], ids=["denied", "cancelled"],
)
def test_b6_chain_preserves_decision_after_rejection_or_cancel(reapproval_case, tmp_path, monkeypatch, exit_case):
    decision, error_code = exit_case
    service = _b6_service(tmp_path, ("guard",))
    outcome = _resolve_persist_and_read(
        reapproval_case, service, _B6Exit(decision), monkeypatch,
    )
    _assert_full_b6_chain(outcome, ("guard",))
    assert [(row["verdict"], row["final_status"]) for row in outcome["compact_entries"]] == [("ask", "ask")]
    assert outcome["resolved"].result.error_code == error_code
    assert outcome["consumer"].calls == 1
    assert outcome["handler_runs"] == 0 and outcome["plugin_requests"] == 1


# LLM: 先前同工具同参数的结构化拒绝会直接复用，不能再询问，但仍须把本轮首次插件征询记账。
# 函数用途: 完整验证重复拒绝出口写入原决定且不重复弹出审批。
def test_b6_chain_preserves_decision_after_repeated_rejection(reapproval_case, tmp_path, monkeypatch):
    service = _b6_service(tmp_path, ("guard",))
    outcome = _resolve_persist_and_read(
        reapproval_case, service, _B6Exit("approved", repeated_rejection=True), monkeypatch,
    )
    _assert_full_b6_chain(outcome, ("guard",))
    assert [(row["verdict"], row["final_status"]) for row in outcome["compact_entries"]] == [("ask", "ask")]
    assert outcome["resolved"].result.error_code == "APPROVAL_REJECTED"
    assert outcome["consumer"].calls == 0
    assert outcome["handler_runs"] == 0 and outcome["plugin_requests"] == 1


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
