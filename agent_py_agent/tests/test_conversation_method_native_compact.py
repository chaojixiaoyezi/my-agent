# LLM: 真实线程CAS、检查点、IR裁剪和下一build容量门；只使用本地合成工具记录及摘要后端，不接Gateway。
# 模块用途: 验证原生压缩挂点确实带回、配对不拆开、同代不重复，以及下一build不额外回收工具对。
from dataclasses import replace

import pytest

from agent_py_agent.agent.agent_core import _tool_loop_service as service
from agent_py_agent.agent.backends.tool_ir import RuntimeFactsTurn, ToolResult
from agent_py_agent.agent.capability.method_carry import prepare_conversation_method_carry
from agent_py_agent.agent.conversation.authority import CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR
from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE
from agent_py_agent.agent.conversation.compact_summary_view import (
    AppliedCompactContext,
    resolve_compact_summary_view,
)
from agent_py_agent.agent.memory_archive import estimate_tokens
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot
from agent_py_agent.tests.test_conversation_method_carry import (
    method_fixture,
    read_package,
    records,
    switch,
)
from agent_py_agent.tests.test_conversation_method_restore import carry
from agent_py_agent.tests.test_native_tool_ir_compact_and_orphan_sweep import (
    _record_large_write_calls,
    _SummaryBackend,
)


# LLM: 场景只固定合成窗口和协议，保留真实 Store/CAS/build，不连接真实运行时。
# 函数用途: 准备多个可保留工具对及开关对照。
def _native_carry_fixture(tmp_path, monkeypatch, enabled):
    fixture = method_fixture(tmp_path)
    assert read_package(fixture).ok and carry(fixture) == []
    agent, params = fixture.agent, fixture.params
    agent.backend = _SummaryBackend(context_window_tokens=40_000)
    agent.config.model_context_window_tokens = 40_000
    agent.config.memory_auto_compact_trigger_percent = 90
    agent.config.tool_output_externalize_min_chars = 10_000_000
    agent.config.tool_output_externalize_on_low_headroom = False
    monkeypatch.setattr(agent.prompts, "build", lambda **_kw: "base-prompt")
    thread = agent.conversation_store.threads.require(fixture.link.thread_id)
    params = replace(params, save=True,
        task_attributes={**params.task_attributes, CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR: True},
        tool_protocol_snapshot=make_test_protocol_snapshot(run_id=params.run_id, source_protocol="native"),
        compact_context=AppliedCompactContext(thread.thread_id, THREAD_COMPACT_SCOPE,
            resolve_compact_summary_view(agent, thread, THREAD_COMPACT_SCOPE)))
    fixture.params = params
    agent._current_run_params = params
    if not enabled:
        switch(fixture, False)
    _record_large_write_calls(agent, params, start=1, stop=12, chars=12_000)
    # 最近尾部必须真能保留至少两对，避免单对禁止回收的保护让容量断言失去判别力。
    _record_large_write_calls(agent, params, start=13, stop=15, chars=200)
    return fixture, thread


# LLM: 包裹而不替换容量裁剪，记录实际入口执行前后的配对和回收计数。
# 函数用途: 安装一个只读观察器供下一 build 的断言使用。
def _observe_native_fits(monkeypatch):
    fits = []
    original_fit = service._fit_native_ir_to_shared_budget
    def fit(agent_, params_, prompt, **options):
        before = service._native_tool_call_ids(params_)
        dropped = original_fit(agent_, params_, prompt, **options)
        fits.append((before, service._native_tool_call_ids(params_), dropped))
        return dropped
    monkeypatch.setattr(service, "_fit_native_ir_to_shared_budget", fit)
    return fits


@pytest.mark.parametrize("enabled", [True, False], ids=["carry_on", "carry_off"])
def test_native_cas_carry_preserves_nonempty_pairs_in_next_real_build(tmp_path, monkeypatch, enabled):
    fixture, thread = _native_carry_fixture(tmp_path, monkeypatch, enabled)
    agent, params = fixture.agent, fixture.params
    fits = _observe_native_fits(monkeypatch)
    service.build_tool_loop_prompt(agent, params)
    committed = agent.conversation_store.threads.require(thread.thread_id)
    assert committed.compact_generation == 1 and committed.compact_checkpoint_id
    assert params.compact_context.view.generation == 1
    before, retained, dropped = fits[-1]
    assert dropped > 0 and before and len(retained) >= 2 and len(retained) < len(before)
    turns = [turn for turn in params.tool_ir_history if isinstance(turn, RuntimeFactsTurn)
             and turn.source == "conversation_method_carry"]
    assert len(turns) == int(enabled) and records(fixture)[0]["carried_generation"] == int(enabled)
    if enabled:
        index = params.tool_ir_history.index(turns[0])
        assert isinstance(params.tool_ir_history[index - 1], ToolResult)
        assert estimate_tokens(turns[0].text) <= 3000
    service.build_tool_loop_prompt(agent, params)
    assert fits[-1] == (retained, retained, 0)
    assert agent.conversation_store.threads.require(thread.thread_id).compact_generation == 1
    params = replace(params, conversation_method_carry_attempts=set())
    agent._current_run_params = params
    prepare_conversation_method_carry(agent, params)
    assert sum(isinstance(turn, RuntimeFactsTurn) and turn.source == "conversation_method_carry"
               for turn in params.tool_ir_history) == int(enabled)
    print("native_post_carry_fit", enabled, len(before), len(retained), fits[-1][2])
