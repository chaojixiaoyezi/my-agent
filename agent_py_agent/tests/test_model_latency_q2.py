# LLM: 只在pytest临时owner测量组装、账本和遥测，不请求真实模型或修改运行配置。
# 模块用途: 回归Q2的只读投影、计量复用、轮间延迟与M7文件消失边界。
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from inspect import signature
from threading import local
from types import MappingProxyType, SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import tool_ir_history
from agent_py_agent.agent.agent_core.model import call_runtime, context_pressure
from agent_py_agent.agent.backends import message_adapter
from agent_py_agent.agent.backends.tool_ir import AssistantTurn, ToolCall, UserTurn
from agent_py_agent.agent.contracts.model_call_ledger import ModelCallLedger, ModelCallLedgerContext
from agent_py_agent.agent.observability.task_call_inspector import TaskCallInspector
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.settings.model_selection_status import latest_thread_model_call
from agent_py_agent.agent.tooling.gateway_status import _metadata_projection
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot


# LLM: 运行状态与协议快照真实兼容；只有后端网络被省略，输入估算和投影仍用产品函数。
# 函数用途: 创建无凭据、无远程调用的native上下文夹具。
def _context_case():
    agent = SimpleNamespace(config=AgentConfig(auto_save_memory=False, memory_context_calibration_carry_enabled=False),
                            backend=SimpleNamespace(name="fake", model_name="q2", context_window_tokens=128000))
    params = SimpleNamespace(tool_protocol_snapshot=make_test_protocol_snapshot(source_protocol="native"),
                             live_archive_state={}, tool_ir_history=[UserTurn("当前请求")], tool_context=[],
                             provider_history_messages=[{"role": "user", "content": f"旧消息{index}"} for index in range(3)],
                             context_scope="default", task_attributes={}, run_id="run-q2", request_id="req-q2",
                             session_id="session-q2")
    return agent, params


# LLM: 默认出站仍隔离嵌套输入；显式借用只用于只读消费者，MappingProxyType会暴露意外深拷贝。
# 函数用途: 验证新增copy_prior开关和原默认隔离语义。
def test_q2_readonly_projection_and_default_send_isolation():
    assert "copy_prior" in signature(message_adapter.project_native_history_messages).parameters
    prior = [{"role": "user", "content": [{"type": "text", "text": "原始正文"}]}]
    before = deepcopy(prior)
    borrowed = message_adapter.project_native_history_messages([], prior, copy_prior=False)
    assert borrowed[0] is prior[0]
    assert prior == before
    sent = message_adapter.project_native_history_messages([], prior)
    sent[0]["content"][0]["text"] = "backend修改"
    assert prior == before
    frozen = [{"role": "user", "content": [{"type": "image", "source": MappingProxyType({"type": "url", "url": "fixture"})}]}]
    assert message_adapter.project_native_history_messages([], frozen, copy_prior=False)[0] is frozen[0]


# LLM: 容量和逻辑身份只是读投影；统计真实deepcopy调用，不用替身消息冒充等价出站。
# 函数用途: 验证估算和provider指纹不复制prior，默认发送仍与优化前值相等。
def test_q2_estimate_and_fingerprint_do_not_deepcopy_prior(monkeypatch):
    agent, params = _context_case()
    copies = []
    original = message_adapter.deepcopy

    # LLM: 记录适配器实际深拷贝，仍执行原函数以保留产品行为。
    # 函数用途: 统计只读阶段是否复制历史。
    def copied(value):
        copies.append(value)
        return original(value)

    monkeypatch.setattr(message_adapter, "deepcopy", copied)
    before = deepcopy(params.provider_history_messages)
    context_pressure.model_visible_context_snapshot(agent, params, "prompt")
    assert tool_ir_history.provider_messages_fingerprint(params)
    assert copies == []
    assert params.provider_history_messages == before
    sent = tool_ir_history.project_native_provider_messages(params.tool_ir_history, prior_messages=before)
    assert sent == tool_ir_history.project_native_provider_messages(params.tool_ir_history, prior_messages=before, copy_prior=False)
    sent[0]["content"] = "send mutation"
    assert before == params.provider_history_messages


# LLM: 三个真实计量入口应共享同一次纯估算，校准与预算决策仍逐次运行；计数的是实际components构建。
# 函数用途: 验证列表重建后仍复用，且中段等长结构变化立即失效。
def test_q2_context_estimate_reused_and_middle_mutation_invalidates(monkeypatch):
    agent, params = _context_case()
    calls = []
    original = context_pressure.projected_model_context_components

    # LLM: 保留真实计量与分类，只记录全量估算入口。
    # 函数用途: 统计preflight与快照是否重复估算。
    def measured(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(context_pressure, "projected_model_context_components", measured)
    first = context_pressure.model_visible_context_tokens(agent, params, "prompt")
    params.provider_history_messages = deepcopy(params.provider_history_messages)
    snapshot = context_pressure.model_visible_context_snapshot(agent, params, "prompt")
    request = SimpleNamespace(agent=agent, params=params, prompt="prompt")
    assert context_pressure.preflight_context_pressure_response(request) is None
    assert snapshot.current_tokens == first
    assert len(calls) == 1
    params.provider_history_messages[1]["content"] += "新增" * 100
    assert context_pressure.model_visible_context_tokens(agent, params, "prompt") > first
    assert len(calls) == 2


# LLM: 相同首尾和消息数不足以证明中间IR未变；工具schema内容、会话身份也必须进入失效键。
# 函数用途: 验证IR内原位追加调用、schema变化和session切换不会用旧计量。
@pytest.mark.parametrize("change", ["tool_call", "schema", "session"])
def test_q2_context_memo_invalidates_visible_inputs(monkeypatch, change):
    agent, params = _context_case()
    params.tool_ir_history = [AssistantTurn(text="思考")]
    tools = [{"name": "read", "input_schema": {"type": "object"}}]
    monkeypatch.setattr(context_pressure, "resolve_native_tools", lambda *_args: tools)
    calls = []
    original = context_pressure.projected_model_context_components

    # LLM: 不模拟估算值，确保失效后确实调用原估算入口。
    # 函数用途: 记录对应输入变化是否重算。
    def measured(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(context_pressure, "projected_model_context_components", measured)
    context_pressure.model_visible_context_tokens(agent, params, "prompt")
    context_pressure.model_visible_context_tokens(agent, params, "prompt")
    assert len(calls) == 1
    if change == "tool_call":
        params.tool_ir_history[0].tool_calls.append(ToolCall(
            call_id="c1", tool_name="read", arguments={"path": "fixture"}, source_protocol="native", schema_hash="sha256:" + "f" * 64,
            run_id="run-q2", turn_id="turn-q2", attempt_id="attempt-q2"))
    elif change == "schema":
        tools[0]["description"] = "schema changed"
    else:
        params.session_id = "other-session"
    context_pressure.model_visible_context_tokens(agent, params, "prompt")
    assert len(calls) == 2


# LLM: 压缩校准只需要稳定表面指纹，不应为取指纹触发任何消息token估算或深拷贝。
# 函数用途: 验证轻量指纹与完整路径一致且不计量消息。
def test_q2_calibration_surface_does_not_estimate_messages(monkeypatch):
    agent, params = _context_case()
    expected = context_pressure._model_visible_context_components(agent, params, "prompt")[3]
    calls = []
    monkeypatch.setattr(context_pressure, "estimate_tokens", lambda _value: calls.append(1) or 1)
    context_pressure.compact_request_calibration_for_request(agent, params, "prompt")
    assert calls == []
    assert context_pressure._model_visible_surface_fingerprint(agent, params, "prompt") == expected


# LLM: 可注入账本时钟与组装单调时钟分开，遥测落到临时owner，不能借用真实调用或身份。
# 函数用途: 创建可复现轮间时差的实际调用链夹具。
def _call_case(tmp_path, monkeypatch):
    agent, params = _context_case()
    clock = [10.0]
    ledger = ModelCallLedger(context=ModelCallLedgerContext(now=lambda: clock[0]))
    agent._model_call_ledger = ledger
    agent.home_paths = SimpleNamespace(home_dir=tmp_path, owner_id="one")
    params.context_scope = "isolated"
    params.task_id = "task-q2"
    params.task_attributes = {"conversation_thread_id": "thread-q2"}
    request = SimpleNamespace(agent=agent, params=params, prompt="prompt", tool_rounds=0,
                              context_assembly_started_at=9.0)
    snapshot = SimpleNamespace(current_tokens=100, raw_estimated_tokens=120, to_public_dict=lambda: {})
    monkeypatch.setattr(call_runtime, "_LAST_MODEL_CALL_COMPLETED_AT", local(), raising=False)
    monkeypatch.setattr(call_runtime, "time", SimpleNamespace(monotonic=lambda: 10.0), raising=False)
    return request, ledger, clock, snapshot


# LLM: 第一轮没有前序完成点时为未知；第二轮1250ms只来自完成→开始，不混入10秒模型耗时。
# 函数用途: 验证估算/实报区分、任务轮次持久化与两层状态投影的完整链路。
def test_q2_gap_estimate_and_status_reach_task_round(tmp_path, monkeypatch):
    request, ledger, clock, snapshot = _call_case(tmp_path, monkeypatch)
    _, first, _ = call_runtime.start_model_call_record(request, context_snapshot=snapshot)
    assert ledger.records()[-1].metadata.get("estimated_input_tokens") == 100
    assert ledger.records()[-1].metadata.get("inter_round_gap_ms") is None
    clock[0] = 20.0
    response = SimpleNamespace(text="done", usage={"input_tokens": 80, "output_tokens": 5, "cache_read_input_tokens": 30})
    call_runtime.record_model_call_finished(ledger, first, response)
    clock[0] = 21.25
    request.tool_rounds = 1
    _, second, _ = call_runtime.start_model_call_record(request, context_snapshot=snapshot)
    assert ledger.records()[-1].metadata.get("inter_round_gap_ms") == 1250.0
    clock[0] = 23.0
    call_runtime.record_model_call_finished(ledger, second, response)
    latest = latest_thread_model_call(request.agent, "thread-q2")
    public = _metadata_projection({"model_selection": {"last_request": latest}})["model_selection"]["last_request"]
    assert latest.get("estimated_input_tokens") == 100
    assert latest.get("inter_round_gap_ms") == 1250.0
    for key in ("context_assembly_ms", "inter_round_gap_ms", "estimated_input_tokens", "input_tokens",
                "cached_input_tokens", "uncached_input_tokens", "output_tokens"):
        assert public.get(key) == latest.get(key)
    assert latest["input_tokens"] == ledger.records()[-1].accounted_input_tokens
    detail = TaskCallInspector(tmp_path, owner_id="one").get_task_call_detail("task-q2")
    assert len(detail["rounds"]) == 2
    assert detail["rounds"][-1].get("estimated_input_tokens") == 100
    assert detail["rounds"][-1].get("inter_round_gap_ms") == 1250.0


# LLM: 组装起点是time.monotonic，不能与测试或调用者注入的ledger时钟相减。
# 函数用途: 验证context_assembly_ms的两端时钟一致。
def test_q2_assembly_uses_monotonic_clock(tmp_path, monkeypatch):
    request, ledger, _clock, snapshot = _call_case(tmp_path, monkeypatch)
    _clock[0] = 10000.0
    request.context_assembly_started_at = 8.0
    call_runtime.start_model_call_record(request, context_snapshot=snapshot)
    assert ledger.records()[-1].metadata["context_assembly_ms"] == 2000.0


# LLM: 不同worker或run没有可靠前序点就保持未知，线程池不能继承主线程的完成时间。
# 函数用途: 验证轮间时间按线程和调用作用域隔离。
def test_q2_gap_does_not_cross_thread_or_run(tmp_path, monkeypatch):
    request, ledger, clock, snapshot = _call_case(tmp_path, monkeypatch)
    _, first, _ = call_runtime.start_model_call_record(request, context_snapshot=snapshot)
    clock[0] = 12.0
    call_runtime.record_model_call_finished(ledger, first, SimpleNamespace(text="done", usage={}))
    clock[0] = 15.0

    # LLM: 在真实独立线程上调用原产品登记函数，不替代线程局部存储。
    # 函数用途: 捕获另一个worker看到的轮间时差。
    def start_in_worker():
        _, call_id, _ = call_runtime.start_model_call_record(request, context_snapshot=snapshot)
        return next(row for row in ledger.records() if row.call_id == call_id).metadata

    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(start_in_worker).result().get("inter_round_gap_ms") is None
    request.params.run_id = "other-run"
    call_runtime.start_model_call_record(request, context_snapshot=snapshot)
    assert ledger.records()[-1].metadata.get("inter_round_gap_ms") is None


# LLM: 列目录后文件可能真的被清理；必须过滤缺失而非吞掉其它损坏记录，后续列表仍走公开入口。
# 函数用途: 重现M7目录与读取之间的文件消失并保证现存任务不丢。
def test_q2_m7_records_skip_concurrently_removed_task(tmp_path, monkeypatch):
    inspector = TaskCallInspector(tmp_path, owner_id="one")
    inspector.upsert_task_call({"task_id": "gone", "started_at": 1.0})
    live = inspector.upsert_task_call({"task_id": "live", "started_at": 2.0})
    original = inspector._read

    # LLM: 删除只发生在pytest临时目录中，模拟真实清理方而不伪造解析返回值。
    # 函数用途: 在目录快照之后移走待读取任务。
    def read_after_cleanup(task_id):
        if task_id == "gone":
            (tmp_path / "data/telemetry/task_calls/gone.json").unlink(missing_ok=True)
        return original(task_id)

    monkeypatch.setattr(inspector, "_read", read_after_cleanup)
    assert inspector._records() == [live]
    assert [row["task_id"] for row in inspector.list_task_calls()] == ["live"]
