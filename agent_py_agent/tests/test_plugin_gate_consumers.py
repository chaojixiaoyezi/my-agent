"""B5 第3段：真实主/子审批事实与隔离恢复；不启动 Gateway 或启用真实 v8。"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_loop import round_execution
from agent_py_agent.agent.backends import gateway_helpers, http
from agent_py_agent.agent.contracts import model_call_ledger
from agent_py_agent.agent.contracts.tool_approval import ToolApprovalDecision, ToolApprovalRequest
from agent_py_agent.agent.conversation import agent_tool_approval, store_claims
from agent_py_agent.agent.conversation.agent_activity import BackgroundMainActivitySink
from agent_py_agent.agent.conversation.agent_control import (
    AgentControlError,
    resolve_agent_permission,
)
from agent_py_agent.agent.conversation.agent_tool_approval import (
    list_pending_agent_tool_approvals,
    publish_agent_tool_approval,
    resolve_agent_tool_approval,
    wait_for_agent_tool_approval,
)
from agent_py_agent.agent.conversation.background_transcript import BackgroundTranscriptSink
from agent_py_agent.agent.gateway_parts import (
    plugin_panels_http,
    request_execution,
    stream_approval,
)
from agent_py_agent.agent.gateway_parts.http_handlers import read_gateway_client_notices
from agent_py_agent.agent.gateway_parts.io import read_json_file
from agent_py_agent.agent.gateway_parts.permission_bridge import write_gateway_permission_decision
from agent_py_agent.agent.gateway_parts.recovery import recover_gateway_processing_requests
from agent_py_agent.agent.plugin_events import tool_gate_review
from agent_py_agent.agent.plugin_events.declarations import PluginToolGateDeclaration
from agent_py_agent.agent.tooling.executor import ToolExecutor, ToolExecutorRequest
from agent_py_agent.agent.tooling.plugin_gate_policy import plugin_gate_approval_request
from agent_py_agent.agent.user_space import approval_mode, operation_grants
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_test_call,
    make_test_model_spec,
    runtime_snapshot_for_tools,
)
from agent_py_agent.tests.test_gateway_agent_control_service import _bound_agent_tree
from agent_py_agent.tests.test_plugin_tool_gate_execution import _Harness, _Probe
from agent_py_agent.tests.test_shutdown_turn_resume import (
    _claim_again,
    _gateway,
    _processing,
    _real_agent,
    _reply,
)


# LLM: 任务、线程、claim 与 child attempt 全部来自真实隔离 store；只用假插件传输，不替换审批裁决。
# 函数用途: 为主/子两条后台链准备真实消费者，测试退出时收掉等待线程。
@pytest.fixture(params=["main", "subagent"])
def gate_context(tmp_path, request):
    agent, scope, child = _bound_agent_tree(tmp_path)
    store = agent.conversation_store
    link = store.tasks.load(child.root_id)
    claim = store.claims.acquire({"thread_id": link.thread_id, "task_id": link.task_id, "reason": "thread_goal_continue"})
    run_id = link.task_id if request.param == "main" else child.id
    thread_id = link.thread_id if request.param == "main" else child.agent_thread_id
    sink = (BackgroundMainActivitySink(agent, thread_id=thread_id, task_id=run_id) if request.param == "main"
            else BackgroundTranscriptSink(agent, thread_id=thread_id, task_id=run_id))
    probe, harness = _Probe(), _Harness()
    probe.model_spec = make_test_model_spec("gate_probe", input_schema={"type": "object",
        "properties": {"mode": {"type": "string", "enum": ["write"]}}, "additionalProperties": False})
    probe.runtime_policy = replace(probe.runtime_policy, approval_policy=replace(
        probe.runtime_policy.approval_policy, owner_grant_parameters=(("mode", ("write",)),)))
    agent.tools.register(probe)
    snapshot = runtime_snapshot_for_tools({"gate_probe": probe}, run_id=run_id,
        owner_type="main_agent" if request.param == "main" else "subagent")
    attempt_id = claim["claim_id"] if request.param == "main" else child.runner_active_attempt_id
    call = canonical_test_call(snapshot, "gate_probe", {"mode": "write"}, attempt_id=attempt_id)
    params = SimpleNamespace(effective_on_chunk=sink, request_id="gate-consumer", cancellation_token=SimpleNamespace(cancelled=False),
        runtime_approved_actions=[], runtime_rejected_actions=[], tool_runtime_snapshot=snapshot, tool_context=[])
    ctx = SimpleNamespace(agent=agent, scope=scope, child=child, kind=request.param, root_id=link.task_id,
        claim=claim, sink=sink, snapshot=snapshot, call=call, harness=harness, probe=probe, params=params,
        request=SimpleNamespace(agent=agent, params=params, tool_rounds=1, actor="model"), results=[], errors=[], waiter=None)
    yield ctx
    params.cancellation_token.cancelled = True
    if ctx.waiter is not None:
        ctx.waiter.join(timeout=3)
        assert not ctx.waiter.is_alive()
    assert ctx.errors == []


# LLM: 使用真实 ToolExecutor 和 B2 池征询；可信交互与审批模式由宿主配置读取，handler 未执行时仍需保留原身份。
# 函数用途: 产生要进入原轮审批的插件确认，不伪造 plugin_gate_ref。
def _execution(ctx):
    return ToolExecutor().execute(ToolExecutorRequest(ctx.call, ctx.snapshot, ctx.agent.root,
        operation_store_required=False, approval_mode=approval_mode.read_approval_mode(ctx.agent.home_paths),
        write_boundary={"approved_actions": ctx.params.runtime_approved_actions},
        trusted_run_context={"interactive": True}, plugin_gate_reviewer=ctx.harness.reviewer.review))


# LLM: 只在测试线程执行原审批编排；异常独立收集，不能让空结果被误算为等待成功。
# 函数用途: 开始一条真实执行器到后台消费者的挂起确认。
def _start(ctx):
    execution = _execution(ctx)
    assert execution.decision.status == "ask" and ctx.probe.executed == []
    def resolve():
        try:
            ctx.results.append(round_execution._resolve_tool_approval(ctx.request, 1, ctx.call, execution))
        except Exception as exc:  # noqa: BLE001 测试保留原异常供最终断言
            ctx.errors.append(exc)
    ctx.waiter = threading.Thread(target=resolve)
    ctx.waiter.start()


# LLM: HTTP handler 直接调用真实 canonical 读取函数，不启动服务；投影应保留生成的引用与两个选项。
# 函数用途: 从所属用户入口读到当前等待的精确审批请求。
def _pending(ctx):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = read_gateway_client_notices(ctx.agent, scope=ctx.scope, after=0, interactive_approvals=True)
        rows = result["agent_permission_requests"]
        if rows:
            assert len(rows) == 1 and rows[0]["run_id"] == ctx.call.run_id
            assert rows[0]["agent_kind"] == ctx.kind
            return ToolApprovalRequest.from_mapping(rows[0]["request"])
        if ctx.errors or ctx.results:
            break
        time.sleep(.01)
    raise AssertionError(f"没有发布当前插件确认：results={ctx.results}, errors={ctx.errors}")


# LLM: 决定经真实 owner/thread/root 门和完整请求核验；测试不直写批准文件。
# 函数用途: 由所属用户拒绝原调用并确认 handler 没有执行。
def _deny(ctx, pending):
    result = resolve_agent_permission(ctx.agent, scope=ctx.scope, run_id=ctx.call.run_id,
        request=pending.to_dict(), decision=ToolApprovalDecision(pending.permission_id, "denied").to_dict())
    assert result["ok"] is True
    ctx.waiter.join(timeout=3)
    assert not ctx.waiter.is_alive() and ctx.errors == []
    assert ctx.results[-1].result.error_code == "APPROVAL_REJECTED"
    assert not ctx.results[-1].result.handler_executed and ctx.probe.executed == []


@pytest.mark.parametrize("mode", ["session_cache", "owner_grant", "autonomous", "auto"])
def test_canonical_consumers_keep_executor_reference_and_require_user(gate_context, mode):
    ctx = gate_context
    transcript = ctx.sink._transcript if ctx.kind == "main" else ctx.sink
    cache_key = f"{ctx.call.run_id}:{ctx.claim['claim_id'] if ctx.kind == 'main' else ''}:gate_probe:{ctx.call.args_hash}"
    if mode == "session_cache":
        transcript._approved_session_keys = {cache_key}
    if mode == "owner_grant":
        operation_grants.record_owner_operation_grant(ctx.agent.home_paths, "gate_probe:mode=write", source="test")
        assert operation_grants.owner_operation_granted(ctx.agent.home_paths, "gate_probe:mode=write")
    if mode == "auto":
        approval_mode.execute_approval_mode_operation(ctx.agent.home_paths, "set", "auto")
    before = ctx.agent.home_paths.owner_tool_policy_json.read_bytes()
    read_gateway_client_notices(ctx.agent, scope=ctx.scope, after=0, interactive_approvals=True)
    _start(ctx)
    pending = _pending(ctx)
    reference = json.loads(pending.binding["plugin_gate_ref"])
    assert all(reference[key] == getattr(ctx.call, key) for key in ("operation_id", "call_id", "idempotency_key", "args_hash"))
    assert reference["gates"][0]["activation_id"] == "act-1"
    assert pending.description.startswith("[插件 guard 要求确认：CONFIRM")
    assert [item["decision"] for item in pending.options] == ["approved", "denied"]
    if mode == "autonomous":
        approval_mode.execute_approval_mode_operation(ctx.agent.home_paths, "set", "auto")
        before = ctx.agent.home_paths.owner_tool_policy_json.read_bytes()
        time.sleep(.12)
    assert ctx.results == [] and ctx.probe.executed == []
    _deny(ctx, pending)
    assert ctx.agent.home_paths.owner_tool_policy_json.read_bytes() == before
    assert getattr(transcript, "_approved_session_keys", set()) == ({cache_key} if mode == "session_cache" else set())
    assert ctx.harness.sent[0][2]["call"]["actor"] == ctx.kind


def test_canonical_consumers_report_plugin_approval_unavailable(gate_context):
    ctx = gate_context
    _start(ctx)  # 没有 notices 消费者租约
    ctx.waiter.join(timeout=3)
    assert not ctx.waiter.is_alive() and ctx.errors == []
    result = ctx.results[-1].result
    assert result.error_code == "PLUGIN_GATE_APPROVAL_UNAVAILABLE" and result.error_category == "permission"
    assert not result.handler_executed and result.effect_outcome == "not_started"
    text = "".join(block.text for block in result.content_blocks)
    assert "插件 guard 要求确认" in text and "无法审批" in text


# LLM: 旧轮通过真实 DB 封存，再由原 lifecycle 领取下一代；不得只手填一个新 attempt 冒充恢复。
# 函数用途: 在隔离任务里模拟崩溃后的合法子代理接班。
def _successor_child(ctx):
    repo = ctx.agent.subagents.runtime_db
    agent_run = repo.agent_run_for_run_id(ctx.child.id)
    assert repo.settle_agent_attempt(agent_run_id=agent_run["agent_run_id"],
        attempt_id=ctx.child.runner_active_attempt_id)["settled"] is True
    ctx.agent.subagents.lifecycle.abandon_runner_attempt(ctx.child.id, ctx.child.runner_active_attempt_id, reason="test-restart")
    successor = ctx.agent.subagents.lifecycle.prepare_runner_attempt(ctx.child.id)
    assert successor.runner_active_attempt_id != ctx.child.runner_active_attempt_id
    return successor


# LLM: 只经真实claim收尾与重新领取换轮，子代理由原lifecycle换attempt；不篡改审批行或校验函数。
# 函数用途: 切换canonical执行身份，保持新轮本身仍active以独立考验旧行复核。
def _successor_execution(ctx):
    if ctx.kind == "subagent":
        return _successor_child(ctx)
    claims = ctx.agent.conversation_store.claims
    assert claims.finish({"thread_id": ctx.sink.thread_id, "claim_id": ctx.claim["claim_id"],
                          "status": "finished"}) is not None
    successor = claims.acquire({"thread_id": ctx.sink.thread_id, "task_id": ctx.root_id,
                               "reason": "thread_goal_continue"})
    assert successor and successor["claim_id"] != ctx.claim["claim_id"]
    return successor


# LLM: 请求引用来自真实执行器收紧和原审批合同，不手填plugin_gate_ref或批准记录。
# 函数用途: 无等待线程地发布一条真实插件确认，便于精确核对停止与写回入口。
def _published_plugin_approval(ctx):
    from agent_py_agent.agent.contracts.tool_approval import build_tool_approval_request

    execution = _execution(ctx)
    request = plugin_gate_approval_request(execution, build_tool_approval_request(execution.call,
        request_id="scope-probe", round_number=1, call_index=1, description="原轮插件确认"))
    return publish_agent_tool_approval(ctx.agent, run_id=ctx.call.run_id,
        thread_id=ctx.sink.thread_id, request_value=request)


# LLM: 真实claim换轮或子DONE使scope失效，任何一次sleep轮询都业务失败，不借租约过期兜底。
# 函数用途: 核对停止分支立即unavailable、保原permission并清旧行。
def test_invalid_scope_wait_returns_unavailable_without_poll_and_cleans_old_row(gate_context, monkeypatch):
    ctx = gate_context
    handle = _published_plugin_approval(ctx)
    assert handle.path.exists() and handle.scope_valid()
    if ctx.kind == "subagent":
        ctx.agent.subagents.lifecycle.set_status(ctx.child.id, "DONE")
    else:
        _successor_execution(ctx)
    assert not handle.scope_valid()

    # LLM: 不放宽计时断言；若失效分支被移走，进入一次轮询就直接业务失败，不等待租约兜底。
    # 函数用途: 固定“立即”返回的结构含义为零轮询。
    def no_poll(_interval):
        raise AssertionError("失效作用域不得进入审批轮询")

    monkeypatch.setattr(agent_tool_approval, "time", SimpleNamespace(time=time.time, sleep=no_poll))
    decision = wait_for_agent_tool_approval(handle)
    assert decision.decision == "unavailable"
    assert decision.permission_id == handle.request.permission_id
    assert not handle.path.exists() and ctx.probe.executed == []


# LLM: 新轮仍active但旧审批身份已失效；只调用resolve入口，不借list_pending/wait帮它拦截。
# 函数用途: 独立钉住claim/attempt写回复核且原行不被修改。
def test_resolve_rechecks_claim_or_attempt_before_writing_old_plugin_approval(gate_context):
    ctx = gate_context
    handle = _published_plugin_approval(ctx)
    before = handle.path.read_bytes()
    _successor_execution(ctx)
    # 不调用list_pending或等待入口：新作用域仍有效，必须由resolve自己发现旧行身份不匹配。
    with pytest.raises(FileNotFoundError, match="pending subagent approval not found"):
        resolve_agent_tool_approval(ctx.agent, run_id=ctx.call.run_id, request_value=handle.request,
            decision_value=ToolApprovalDecision(handle.request.permission_id, "approved"))
    assert handle.path.read_bytes() == before and ctx.probe.executed == []


def test_child_old_plugin_approval_cannot_cross_recovered_attempt(tmp_path):
    agent, scope, child = _bound_agent_tree(tmp_path)
    ctx = SimpleNamespace(agent=agent, child=child)
    probe, harness = _Probe(), _Harness()
    snapshot = runtime_snapshot_for_tools({"gate_probe": probe}, run_id=child.id, owner_type="subagent")
    call = canonical_test_call(snapshot, "gate_probe", {}, attempt_id=child.runner_active_attempt_id)
    execution = ToolExecutor().execute(ToolExecutorRequest(call, snapshot, tmp_path, operation_store_required=False,
        plugin_gate_reviewer=harness.reviewer.review, trusted_run_context={"interactive": True}))
    from agent_py_agent.agent.contracts.tool_approval import build_tool_approval_request
    request = plugin_gate_approval_request(execution, build_tool_approval_request(call, request_id="restart-child",
        round_number=1, call_index=1, description="旧轮插件确认"))
    handle = publish_agent_tool_approval(agent, run_id=child.id, thread_id=child.agent_thread_id, request_value=request)
    _successor_child(ctx)
    assert list_pending_agent_tool_approvals(agent, root_task_id=child.root_id) == []
    with pytest.raises(AgentControlError):
        resolve_agent_permission(agent, scope=scope, run_id=child.id, request=request.to_dict(),
            decision=ToolApprovalDecision(request.permission_id, "approved").to_dict())
    assert wait_for_agent_tool_approval(handle).decision == "unavailable"
    assert probe.executed == []


# LLM: 仅替插件安装/client/transport与模型HTTP线；保留真实执行器、原审批和请求恢复入口。
# 函数用途: 为隔离I4场景安装响应原生协议探针的假供应商与插件线路。
def _i4_transport(monkeypatch, harness):
    row = harness.rows[0]
    row.manifest.tool_gates = (PluginToolGateDeclaration("check", ("list_files",), (), "none"),)
    monkeypatch.setattr(tool_gate_review, "enabled_gate_installations", lambda _owner: tuple(harness.rows))
    monkeypatch.setattr(plugin_panels_http, "current_plugin_channel_pool", lambda: harness.pool)
    calls = []
    # LLM: 原生探针与业务调用同走已声明协议；返回假线响应，不读真实模型或凭据。
    # 函数用途: 让隔离请求有一次读目录工具调用及结束回复。
    def wire(request):
        payload = request.payload
        calls.append(payload)
        gateway_helpers._emit_provider_attempt({"attempt_id": f"gate-i4-{len(calls)}", "status": "started"})
        names = [item["function"]["name"] for item in payload.get("tools", [])]
        if names == ["my_agent_capability_probe"]:
            nonce = re.search(r"nonce ([0-9a-f]+)", json.dumps(payload, ensure_ascii=False))
            return _reply(payload, None, ("my_agent_capability_probe", {"nonce": nonce.group(1) if nonce else ""}), len(calls))
        has_result = any(item.get("role") == "tool" for item in payload.get("messages", []))
        return _reply(payload, "恢复轮已处理" if has_result else None,
            None if has_result else ("list_files", {"path": "."}), len(calls))
    monkeypatch.setattr(http, "post_json", wire)


# LLM: 宿主关机异常只用于测试故障注入；确认行保持挂起，不返回批准也不启动真实Gateway。
# 函数用途: 在原等待入口中断一次隔离回合。
def _shutdown_i4_wait(*_args, **_kwargs):
    raise model_call_ledger.ModelCallAdmissionClosedError(
        model_call_ledger.ModelCallAdmissionClosure("HostShutdownInterrupted", "MODEL_CALL_ADMISSION_CLOSED"))


# LLM: 只读隔离回合真实chunks，旧permission不算新确认；无新行时返回None由业务断言失败。
# 函数用途: 在恢复等待线程活着时读取第二轮新发布的插件确认。
def _pending_i4_permission(chunks, old, waiter):
    deadline, pending = time.monotonic() + 5, None
    while time.monotonic() < deadline and waiter.is_alive():
        rows = [json.loads(line) for line in chunks.read_text().splitlines()]
        pending = next((row["permission"] for row in reversed(rows) if row.get("kind") == "permission_requested"
            and row["permission"]["permission_id"] != old["permission_id"]), None)
        if pending:
            break
        time.sleep(.01)
    return pending


# LLM: 保留全部门引用和稳定协议字段的逐字段断言；恢复的call_id必须新建，不能借旧调用批准。
# 函数用途: 单独核对I4两轮插件征询的稳定身份与新调用身份。
def _assert_i4_reference_identity(harness, old, pending):
    request = ToolApprovalRequest.from_mapping(pending)
    reference = json.loads(request.binding["plugin_gate_ref"])
    original_reference = json.loads(old["binding"]["plugin_gate_ref"])
    # I4新执行身份与permission不同；插件门引用的每个字段（含版本/激活/门/原因码）仍与旧轮完全一致。
    assert reference["gates"] == original_reference["gates"]
    assert reference["args_hash"] == original_reference["args_hash"]
    assert {key: value for key, value in harness.sent[1][2]["call"].items() if key != "call_id"} == {
        key: value for key, value in harness.sent[0][2]["call"].items() if key != "call_id"}
    assert reference["call_id"] != original_reference["call_id"]


# LLM: 原请求处理与恢复入口复用隔离store；只重建宿主对象，要求重新征询并等用户新决定。
# 函数用途: 验证I4挂起确认不会因恢复变成已批准。
def test_i4_gateway_resume_reasks_plugin_and_needs_new_decision(tmp_path, monkeypatch):
    harness = _Harness()
    _i4_transport(monkeypatch, harness)
    original_wait = stream_approval.wait_for_gateway_permission_decision
    monkeypatch.setattr(stream_approval, "wait_for_gateway_permission_decision", _shutdown_i4_wait)
    # B5×B7：总开关关着时装配点不接收紧征询；本用例要测真实征询链，所以构造时就打开。
    agent = _real_agent(tmp_path, plugin_events_enabled=True)
    paths = _gateway(agent)
    owner = agent.home_paths.owner_id
    path = _processing(paths, "gate-i4", prompt="读取当前目录", client_capabilities={"tool_approval": True},
        conversation={"canonical_user_id": owner, "channel": "chat", "channel_conversation_id": "i4-gate", "channel_user_id": owner})
    first = request_execution._handle_gateway_request(agent, path)
    assert first.get("restart_resume") and len(harness.sent) == 1, json.dumps({key: first.get(key) for key in ("error", "error_code", "user_error")}, ensure_ascii=False)
    chunks = path.with_suffix(".chunks.jsonl")
    old = [json.loads(line)["permission"] for line in chunks.read_text().splitlines()
           if json.loads(line).get("kind") == "permission_requested"][-1]
    assert old["binding"]["plugin_gate_ref"]
    # 同一测试进程仅重建宿主对象与已死 claim 的观察；不启停真实 Gateway，旧批准不存在。
    monkeypatch.setattr(model_call_ledger, "_ADMISSION_REGISTRY", model_call_ledger._ModelCallAdmissionRegistry())
    monkeypatch.setattr(store_claims, "process_identity_is_live", lambda _identity: False)
    monkeypatch.setattr(stream_approval, "wait_for_gateway_permission_decision", original_wait)
    successor = _real_agent(tmp_path, plugin_events_enabled=True)
    recovered = recover_gateway_processing_requests(paths, startup=True, agent=successor, planned_restart=True)
    assert recovered["requeued"] == 1
    assert read_json_file(paths.inbox / path.name)["active_turn_recovery"]["cause"] == "gateway_safe_restart"
    _claim_again(paths, path)
    results = []
    waiter = threading.Thread(target=lambda: results.append(request_execution._handle_gateway_request(successor, path)))
    waiter.start()
    pending = None
    try:
        pending = _pending_i4_permission(chunks, old, waiter)
        assert pending and results == []
        _assert_i4_reference_identity(harness, old, pending)
    finally:
        # 失败也必须释放真实等待者，不能让测试退出后留线程；只写测试内精确请求。
        if pending:
            write_gateway_permission_decision(chunks, pending, ToolApprovalDecision(pending["permission_id"], "denied"))
        waiter.join(timeout=5)
    assert not waiter.is_alive() and results[0]["ok"]
    assert results[0]["channel_delivery"]["host_notices"][0]["code"] == "gateway_safe_restart"
