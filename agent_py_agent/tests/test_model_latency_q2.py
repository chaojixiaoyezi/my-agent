# LLM: 只在pytest临时owner测量组装、账本和遥测，不请求真实模型或修改运行配置。
# 模块用途: 回归Q2计量及W2严格IR缓存的失效、借用、复制隔离和并发边界。
import json
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from inspect import signature
from threading import local
from types import MappingProxyType, SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import tool_ir_history
from agent_py_agent.agent.agent_core.model import call_runtime, context_pressure
from agent_py_agent.agent.backends import message_adapter
from agent_py_agent.agent.backends.tool_ir import (
    AssistantTurn,
    CompactionSummary,
    RuntimeFactsTurn,
    ToolCall,
    ToolResult,
    UserTurn,
)
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


# LLM: 缓存逐例隔离，包裹真实适配器而非返回假消息；计数仅表示实际IR重投影次数。
# 函数用途: 为W2缓存回归记录真实投影，并保留独立原函数作等价基准。
@pytest.fixture
def projection_cache(monkeypatch):
    monkeypatch.setattr(tool_ir_history, "_PROJECTION_CACHE", OrderedDict(), raising=False)
    original, calls = message_adapter.project_native_history_messages, []

    # LLM: 真实适配器始终执行，缓存命中必须由上层跳过本入口。
    # 函数用途: 记录实际发生的IR翻译次数。
    def projected(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(message_adapter, "project_native_history_messages", projected)
    return calls, original


# LLM: 使用canonical ToolCall/ToolResult，嵌套参数仅合成数据，不启动工具或请求模型。
# 函数用途: 构造能观察assistant回填、工具参数及结果投影的真实IR。
def _w21_projection_history():
    call = ToolCall("cache-call", "read", {"nested": {"value": 1}}, "native", "sha256:fixture",
                    "cache-run", "cache-turn", "cache-attempt")
    return [AssistantTurn(text="原文", tool_calls=[call]), ToolResult.succeeded(call, "原结果")]


# LLM: 只读零适配器deepcopy且默认发送逐条复制，调用计数与真实消息及输入隔离同时断言。
# 函数用途: 验证探测和发送共用缓存但不共享发送副本。
def test_w21_projection_reuses_readonly_fingerprint_and_send_with_split_copy_contract(projection_cache, monkeypatch):
    calls, original = projection_cache
    history, prior = _w21_projection_history(), [{"role": "user", "content": [{"type": "text", "text": "旧文"}]}]
    copies, copy_item = [], message_adapter.deepcopy

    # LLM: 仍运行原deepcopy，禁止假复制隐藏嵌套污染。
    # 函数用途: 记录默认发送复制的每条消息。
    def counted(item):
        copies.append(item)
        return copy_item(item)

    monkeypatch.setattr(message_adapter, "deepcopy", counted)
    borrowed = tool_ir_history.project_native_provider_messages(history, prior_messages=prior, copy_prior=False)
    repeated = tool_ir_history.project_native_provider_messages(list(history), prior_messages=list(prior), copy_prior=False)
    assert borrowed == repeated and borrowed is not repeated and borrowed[0] is prior[0]
    assert copies == []
    sent = tool_ir_history.project_native_provider_messages(history, prior_messages=prior)
    assert len(copies) == len(sent)
    assert len(calls) == 1, "只读探测和发送应复用同一投影"
    assert sent == original(history, prior, copy_prior=False)
    sent[0]["content"][0]["text"] = "send mutation"
    sent[1]["content"][-1]["input"]["nested"]["value"] = 99
    assert tool_ir_history.project_native_provider_messages(history, prior_messages=prior) == repeated


# LLM: 冻结容器内部仍可回填，缓存失效后须与真实未缓存适配器的wire相等。
# 函数用途: 检查正文、推理块、调用、嵌套参数、旧历史及结果改写的失效。
@pytest.mark.parametrize("change", ["assistant", "blocks", "arguments", "prior", "result", "assistant_call"])
def test_w21_projection_invalidates_in_place_nested_inputs(projection_cache, change):
    calls, original = projection_cache
    history, prior = _w21_projection_history(), [{"role": "user", "content": [{"type": "text", "text": "旧文"}]}]
    project = tool_ir_history.project_native_provider_messages
    before = deepcopy(project(history, prior_messages=prior, copy_prior=False))
    project(history, prior_messages=prior, copy_prior=False)
    assert len(calls) == 1
    if change == "assistant":
        object.__setattr__(history[0], "text", "回填正文")
    elif change == "blocks":
        history[0].content_blocks.append({"type": "thinking", "thinking": "内部思考", "signature": "synthetic"})
    elif change == "arguments":
        history[0].tool_calls[0].arguments["nested"]["value"] = True
    elif change == "prior":
        prior[0]["content"][0]["text"] = "旧文嵌套改写"
    elif change == "assistant_call":
        history[0].tool_calls.append(ToolCall("new-call", "read", {}, "native", "sha256:fixture",
                                             "cache-run", "cache-turn", "cache-attempt"))
    else:
        history[1].metadata["live_prompt_projection"] = "新的安全投影"
    after = project(history, prior_messages=prior, copy_prior=False)
    assert len(calls) == 2
    assert json.dumps(after, ensure_ascii=False) != json.dumps(before, ensure_ascii=False)
    assert after == original(history, prior, copy_prior=False)


# LLM: 改旧嵌套对象后用新对象恢复原值，不能让旧结构键返回被别名污染的缓存。
# 函数用途: 验证派生IR消息在存入缓存时已隔离输入。
def test_w21_projection_does_not_retain_mutable_ir_aliases_across_key_reversion(projection_cache):
    history = _w21_projection_history()
    expected = tool_ir_history.project_native_provider_messages(history)
    tool_ir_history.project_native_provider_messages(history, copy_prior=False)
    nested = history[0].tool_calls[0].arguments["nested"]
    nested["value"] = 2
    history[0].tool_calls[0].arguments["nested"] = {"value": 1}
    assert tool_ir_history.project_native_provider_messages(history) == expected


# LLM: 仅显式改变seen才改变投影，计量/指纹不得提前登记消费或追加原历史。
# 函数用途: 检查guidance去重副本和重复投影的无副作用性。
def test_w21_guidance_projection_is_repeatable_and_never_consumes_seen_or_history(projection_cache):
    calls, _ = projection_cache
    history, seen, context = [UserTurn("用户原话")], {"已转发"}, ["已转发", "新指引", "新指引"]
    before = deepcopy((history, seen, context))
    kwargs = {"forwarded_guidance": seen, "tool_context": context, "copy_prior": False}
    first = tool_ir_history.project_native_provider_messages(history, **kwargs)
    assert first == tool_ir_history.project_native_provider_messages(history, **kwargs)
    assert (history, seen, context) == before and len(calls) == 1
    assert first[-1]["content"] == [{"type": "text", "text": "新指引"}]
    seen.add("新指引")
    assert tool_ir_history.project_native_provider_messages(history, **kwargs) != first
    assert len(calls) == 2


# LLM: 同值不同prior对象的只读输出必须借用对应对象，不能跨调用偷换引用。
# 函数用途: 验证prior身份是值结构之外的必要缓存键。
def test_w21_equal_prior_values_never_substitute_other_object_references(projection_cache):
    prior = [{"role": "user", "content": "相同正文"}]
    first = tool_ir_history.project_native_provider_messages([], prior_messages=prior, copy_prior=False)
    replacement = deepcopy(prior)
    second = tool_ir_history.project_native_provider_messages([], prior_messages=replacement, copy_prior=False)
    assert first == second and first[0] is prior[0] and second[0] is replacement[0]


# LLM: MappingProxy不可deepcopy；普通prior必须仍可缓存并借用原引用。
# 函数用途: 检查只读图片历史的代理类型兼容和命中。
def test_w21_mapping_proxy_prior_is_borrowed_without_deepcopy(projection_cache, monkeypatch):
    proxy = MappingProxyType({"type": "url", "url": "fixture"})
    prior = [{"role": "user", "content": [{"type": "image", "source": proxy}]}]
    monkeypatch.setattr(message_adapter, "deepcopy", lambda _item: pytest.fail("只读投影不应深拷贝prior"))
    for _ in range(2):
        messages = tool_ir_history.project_native_provider_messages([], prior_messages=prior, copy_prior=False)
        assert messages[0] is prior[0] and messages[0]["content"][0]["source"] is proxy
    assert len(projection_cache[0]) == 1


# LLM: 原适配器支持含代理的派生消息；缓存不能新增deepcopy错误，旁路必须保持相同输出。
# 函数用途: 回归独审发现的孤儿清扫及IR媒体代理兼容问题。
@pytest.mark.parametrize("source", ["orphan_prior", "ir_media"])
def test_w21_proxy_after_projection_preserves_readonly_adapter_compatibility(projection_cache, source):
    calls, original = projection_cache
    proxy = MappingProxyType({"type": "url", "url": "fixture"})
    history, prior = [], []
    if source == "orphan_prior":
        prior.append({"role": "user", "content": [
            {"type": "image", "source": proxy},
            {"type": "tool_result", "tool_use_id": "missing", "content": "orphan"},
        ]})
    else:
        history.append(UserTurn("媒体", media=({"media_type": "image/png", "annotation": proxy},)))
    expected = original(history, prior, copy_prior=False)
    for _ in range(2):
        actual = tool_ir_history.project_native_provider_messages(history, prior_messages=prior, copy_prior=False)
        assert actual == expected
    assert len(calls) == 2, "无法安全隔离的派生块应旁路缓存而非拒绝只读投影"
    assert not tool_ir_history._PROJECTION_CACHE


# LLM: 用户/宿主媒体和服务端压缩密文均影响真实出站，不得只冻文本字段。
# 函数用途: 验证附件原位修改及压缩字段改变会重投影。
@pytest.mark.parametrize("turn_type", [UserTurn, RuntimeFactsTurn])
def test_w21_projection_media_and_compaction_fields_are_in_cache_key(projection_cache, turn_type):
    calls, original = projection_cache
    media = {"media_type": "image/png", "sha256": "synthetic-a", "path": "fixture-a"}
    history = [CompactionSummary("摘要", source="applied_compact"), turn_type("带附件", media=(media,))]
    first = tool_ir_history.project_native_provider_messages(history)
    tool_ir_history.project_native_provider_messages(history)
    assert len(calls) == 1
    media["path"] = "fixture-b"
    assert tool_ir_history.project_native_provider_messages(history) != first
    assert len(calls) == 2
    object.__setattr__(history[0], "provider_compaction", "synthetic-encrypted")
    assert tool_ir_history.project_native_provider_messages(history) == original(history)
    assert len(calls) == 3


# LLM: 直接验证结构值键，避免prior对象id差异掩盖标量类型碰撞。
# 函数用途: 检查投影键的bool、负零、NaN和Mapping顺序规则。
def test_w21_projection_scalar_keys_preserve_type_sign_and_mapping_order(projection_cache):
    key = getattr(tool_ir_history, "_projection_cache_key", None)
    assert callable(key), "缺少投影结构键"
    freeze = tool_ir_history._freeze_projection_value
    assert freeze({"v": 1}) != freeze({"v": True})
    assert freeze({"v": -0.0}) != freeze({"v": 0.0})
    assert freeze({"a": 1, "b": 2}) != freeze({"b": 2, "a": 1})
    assert freeze(MappingProxyType({"a": 1})) == freeze({"a": 1})
    assert freeze(float("nan")) == freeze(float("nan"))


# LLM: 真实适配器作为输出基准，超过64项后检查命中/淘汰及并发结果。
# 函数用途: 守住投影缓存的容量和线程共享边界。
def test_w21_projection_lru_is_bounded_and_concurrent_calls_match(projection_cache):
    calls, original = projection_cache
    for index in range(70):
        history = [UserTurn(f"bounded-{index}")]
        assert tool_ir_history.project_native_provider_messages(history) == original(history)
    assert len(tool_ir_history._PROJECTION_CACHE) == 64
    before = len(calls)
    tool_ir_history.project_native_provider_messages([UserTurn("bounded-69")])
    assert len(calls) == before
    tool_ir_history.project_native_provider_messages([UserTurn("bounded-0")])
    assert len(calls) == before + 1
    cases = [[UserTurn(f"concurrent-{index % 5}")] for index in range(30)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        actual = list(pool.map(tool_ir_history.project_native_provider_messages, cases))
    assert actual == [original(history) for history in cases]


# LLM: 投影缓存只存占位符不保留调用方可变prior引用，且投影途中输入变动禁止污染缓存键。
# 函数用途: 验证borrowed prior膨胀不突破缓存字节预算、并发输入回填不写脏缓存。
def test_w21_projection_borrowed_prior_placeholder_and_midflight_mutation_guard(projection_cache, monkeypatch):
    _calls, original = projection_cache
    prior = {"role": "user", "content": "small"}
    output = tool_ir_history.project_native_provider_messages([], prior_messages=[prior], copy_prior=False)
    assert output[0] is prior
    prior["content"] = "x" * (3 * 1024 * 1024)
    retained = sum(
        tool_ir_history._projection_entry_bytes(k, v)
        for k, v in tool_ir_history._PROJECTION_CACHE.items()
    )
    assert retained < 64 * 1024

    turn = AssistantTurn(text="initial")
    mutated = False

    def mutating_adapter(ir_history, prior_messages=None, *, copy_prior=True):
        nonlocal mutated
        res = original(ir_history, prior_messages, copy_prior=copy_prior)
        if not mutated:
            mutated = True
            object.__setattr__(turn, "text", "backfilled")
        return res

    monkeypatch.setattr(message_adapter, "project_native_history_messages", mutating_adapter)
    tool_ir_history.project_native_provider_messages([turn], copy_prior=False)
    object.__setattr__(turn, "text", "initial")
    monkeypatch.setattr(message_adapter, "project_native_history_messages", original)
    fresh = tool_ir_history.project_native_provider_messages([turn], copy_prior=False)
    assert fresh[0]["content"][0]["text"] == "initial"
