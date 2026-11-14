"""真实 Gateway 执行、流解析与 canonical 持久化；仅供应商流为确定性夹具。"""
from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import replace

import pytest

from agent_py_agent.agent.backends.anthropic import AnthropicCompatibleBackend
from agent_py_agent.agent.gateway_parts import request_context, request_execution
from agent_py_agent.tests._gateway_history_helpers import context_history
from agent_py_agent.tests.test_gateway_model_adoption import actual_request, fake_http


# LLM: 所有事件进入真实 parser，停止原因来自供应商结构字段，不直接制造最终交付。
# 函数用途: 为一段正文生成完整的 Messages 流事件。
def _events(text, stop):
    return iter(json.dumps(row) for row in [
        {"type": "message_start", "message": {"usage": {"input_tokens": 9}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": 4}},
        {"type": "message_stop"},
    ])


# LLM: 复用正式请求准备夹具；能力探针沿原网络替身，业务流只有模型端为替身。
# 函数用途: 安装可计数供应商流并保留所有真实出站 messages。
def _gateway(monkeypatch, tmp_path, script):
    fixture = actual_request(tmp_path, tools=True, mode="disabled")
    fixture.agent.config.stream_enabled = True
    fixture.agent.config.memory_curator_enabled = False
    fake_http(monkeypatch, fixture)
    calls = []

    def stream(self, path, payload, headers, **kwargs):
        if [tool.get("name") for tool in payload.get("tools", [])] == ["my_agent_capability_probe"]:
            nonce = re.search(r"nonce ([0-9a-f]+)", json.dumps(payload))[1]
            return iter(json.dumps(row) for row in [
                {"type": "message_start", "message": {"usage": {"input_tokens": 9}}},
                {"type": "content_block_start", "index": 0, "content_block": {
                    "type": "tool_use", "id": "probe", "name": "my_agent_capability_probe", "input": {},
                }},
                {"type": "content_block_delta", "index": 0, "delta": {
                    "type": "input_json_delta", "partial_json": json.dumps({"nonce": nonce}),
                }},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
                {"type": "message_stop"},
            ])
        calls.append(deepcopy(payload))
        text, stop = script[len(calls) - 1]
        return _events(text, stop)

    monkeypatch.setattr(AnthropicCompatibleBackend, "request_stream", stream)
    monkeypatch.setattr(AnthropicCompatibleBackend, "request_stream_iter", stream)
    return fixture, calls


@pytest.mark.parametrize("prefixes", [("PREFIX_7d24",), ("PREFIX_7d24", "PREFIX2_c291")])
def test_gateway_length_resume_final_canonical_and_next_request(monkeypatch, tmp_path, prefixes):
    fixture, calls = _gateway(monkeypatch, tmp_path, [
        *((text, "max_tokens") for text in prefixes), ("SUFFIX_91be", "end_turn"),
        ("FOLLOWUP", "end_turn"),
    ])
    original_request = deepcopy(fixture.context.request)
    result = request_execution._run_gateway_ask(fixture.context)
    conversation = request_context.gateway_conversation_context(request_context.GatewayConversationLoadRequest(
        fixture.agent, fixture.context.request, "inspect", "读取历史",
    ))
    visible = context_history(conversation)
    expected = "\n\n".join((*prefixes, "SUFFIX_91be"))
    assert result.response == expected
    assert [text for role, text in visible if role == "assistant"] == [expected]
    assert [text for role, text in visible if role == "user"] == [original_request["prompt"]]
    assert len(calls) == len(prefixes) + 1
    for text in (*prefixes, "SUFFIX_91be"):
        assert result.response.count(text) == 1

    from agent_py_agent.agent.gateway_parts.recovery import _finish_gateway_conversation_claim

    _finish_gateway_conversation_claim(
        json.loads(fixture.context.request_path.read_text()), fixture.context.request_id,
        fixture.agent.conversation_store,
    )
    follow = {**original_request, "id": "length-follow", "prompt": "请核对完整历史"}
    path = fixture.paths.processing / "length-follow.json"
    path.write_text(json.dumps(follow), encoding="utf-8")
    next_context = replace(fixture.context, request=follow, request_id=follow["id"], request_path=path)
    assert request_execution._run_gateway_ask(next_context).response == "FOLLOWUP"
    wire = json.dumps(calls[-1]["messages"], ensure_ascii=False)
    for text in (*prefixes, "SUFFIX_91be"):
        assert wire.count(text) == 1
    assert wire.count(original_request["prompt"]) == 1
    assert not any(row.get("role") == "tool" for row in calls[-1]["messages"])


# LLM: 脚本只替换供应商；可配置宿主生命周期夹具，仍运行真实response decision与最外层收口。
# 函数用途: 执行确定性的完整工具循环并回读params证据。
def _loop(monkeypatch, tmp_path, script, *, cancel_after=None, configure=None):
    import threading

    from agent_py_agent.agent.agent_core import _tool_loop_service as tls
    from agent_py_agent.agent.concurrency.interrupt import set_interrupt
    from agent_py_agent.tests.test_timeout_recovery_delivery import (
        _FakeAgent,
        _params,
        _RecordingPrompts,
    )

    params = _params(run_id="length-edges")
    main_thread = threading.get_ident()

    class Backend:
        name = "length-edge-fixture"
        calls = 0

        def generate(self, prompt, **kwargs):
            step = script[self.calls]
            self.calls += 1
            if cancel_after == self.calls:
                set_interrupt(True, main_thread)
            if isinstance(step, BaseException):
                raise step
            return step

    backend = Backend()
    agent = _FakeAgent(backend, _RecordingPrompts(), tmp_path)
    if configure is not None:
        params = configure(agent, params) or params
    monkeypatch.setattr(tls, "build_tool_loop_prompt", lambda a, p: tls._render_tool_loop_prompt(a, p))
    set_interrupt(False)
    try:
        try:
            result = tls.execute_tool_loop(agent, params)
        except (RuntimeError, InterruptedError) as exc:
            result = exc
        return result, params, backend.calls
    finally:
        set_interrupt(False)


def _response(text, *, truncated=False, status="ok"):
    from agent_py_agent.agent.backends import ModelResponse

    return ModelResponse(text=text, backend="length-edge-fixture", truncated=truncated,
                         runtime_status="unfinished" if truncated else status,
                         runtime_reason="MODEL_RESPONSE_TRUNCATED" if truncated else "",
                         runtime_source="model_provider" if truncated or status != "ok" else "")


def test_thinking_only_length_arms_no_visible_prefix(monkeypatch, tmp_path):
    result, params, calls = _loop(monkeypatch, tmp_path, [_response("", truncated=True), _response("DONE")])
    assert result.final_response.text == "DONE" and calls == 2
    assert not params.live_archive_state.get("_output_limit_resume_segments")
    assert "_assistant_delivery_segments" not in params.live_archive_state


@pytest.mark.parametrize("terminal", ["cancel", "error", "filtered", "exhausted", "provider_error"])
def test_unsuccessful_continuation_discards_prefix_with_reason(monkeypatch, tmp_path, terminal):
    first = _response("PREFIX_NEVER_SUCCESS", truncated=True)
    script = [first, _response("terminal", status="error" if terminal == "error" else "filtered" if terminal == "filtered" else "ok")]
    if terminal == "exhausted":
        script = [first, _response("SECOND", truncated=True), _response("LAST", truncated=True)]
    if terminal == "provider_error":
        script = [first, RuntimeError("simulated provider error")]
    result, params, calls = _loop(monkeypatch, tmp_path, script, cancel_after=2 if terminal == "cancel" else None)
    if isinstance(result, (RuntimeError, InterruptedError)):
        assert terminal in {"provider_error", "cancel"}
    else:
        assert result.final_response.runtime_status != "ok"
        assert "PREFIX_NEVER_SUCCESS" not in result.final_response.text
    assert calls == (3 if terminal == "exhausted" else 2)
    assert not params.live_archive_state.get("_output_limit_resume_segments")
    rows = params.live_archive_state["_assistant_delivery_segments"]["segments"]
    prefixes = [row for row in rows if row["origin"] == "output_limit_resume_prefix"]
    assert prefixes and all(not row["delivered"] and row["excluded_reason"] for row in prefixes)


def test_length_carry_is_bounded_consumed_once_and_fd_stable():
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
        _TRUNCATED_OUTPUT_RESUME_COUNT,
    )
    from agent_py_agent.agent.agent_core.tool_model_generation import (
        arm_output_limit_resume_segment,
        take_output_limit_resume_segments,
    )

    before = _fd_count()
    for _ in range(20):
        params = SimpleNamespace(live_archive_state={})
        for sequence in range(1, 10):
            params.live_archive_state.update(_model_turn_sequence=sequence, _current_model_turn_id=str(sequence))
            armed = arm_output_limit_resume_segment(params, _response(str(sequence), truncated=True))
            assert armed is (sequence <= _TRUNCATED_OUTPUT_RESUME_COUNT)
        segments = take_output_limit_resume_segments(params)
        assert [part.sequence for part in segments] == list(range(1, _TRUNCATED_OUTPUT_RESUME_COUNT + 1))
        assert take_output_limit_resume_segments(params) == ()
        assert _fd_count() - before == 0


def _fd_count():
    import os
    import sys

    if os.name == "nt":
        import ctypes

        count = ctypes.c_ulong()
        process = ctypes.windll.kernel32.GetCurrentProcess()
        assert ctypes.windll.kernel32.GetProcessHandleCount(process, ctypes.byref(count))
        return count.value
    return len(os.listdir("/dev/fd" if sys.platform == "darwin" else "/proc/self/fd"))


# LLM: Gateway正文和控制都走正式入口；独立解释器重开同一canonical库和已删除记忆，不启动服务或请求真实模型。
# 函数用途: 验证长答续写、原地新会话、进程重启后的历史连续性及永久遗忘。
def test_new_then_process_restart_preserves_full_reply_and_deletion(monkeypatch, tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
    from agent_py_agent.agent.gateway_parts.control_service import (
        GatewayControlScope,
        execute_gateway_conversation_control,
    )
    from agent_py_agent.agent.gateway_parts.recovery import _finish_gateway_conversation_claim
    from agent_py_agent.tests.test_memory_tombstone_longevity import _old_store

    fixture, calls = _gateway(monkeypatch, tmp_path, [("PREFIX_RESTART", "max_tokens"), ("SUFFIX_RESTART", "end_turn")])
    expected = "PREFIX_RESTART\n\nSUFFIX_RESTART"
    assert request_execution._run_gateway_ask(fixture.context).response == expected
    assert len(calls) == 2
    store = fixture.agent.conversation_store
    _finish_gateway_conversation_claim(json.loads(fixture.context.request_path.read_text()), fixture.context.request_id, store)
    # 内层ask已返回，夹具驱动补齐外层队列工人的已关闭信封；不修改/new或其busy判断。
    closed = json.loads(fixture.context.request_path.read_text())
    closed.update(status="completed", turn_phase="closed")
    fixture.context.request_path.write_text(json.dumps(closed))
    command = parse_conversation_control("/new")
    assert command is not None and command.valid and command.kind == "new"
    result = execute_gateway_conversation_control(fixture.agent, fixture.paths, command,
        GatewayControlScope(channel="chat", conversation_id="session", user_id=fixture.agent.home_paths.owner_id))
    assert result.ok, result.to_dict()
    new_id = result.details["thread_id"]
    memory, _ = _old_store(tmp_path, 900)
    victim = memory.add("user", "unique deletion before restart")
    memory.remove(victim.entry_id)
    code = '''
import json, sys
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
store = ConversationStore(sys.argv[1], initialize=False)
old, errors = store.context_bundle_report(sys.argv[2], recent_limit=20)
new, new_errors = store.context_bundle_report(sys.argv[3], recent_limit=20)
memory = JsonlMemory(sys.argv[4])
print(json.dumps({"old": old, "new": new, "errors": errors + new_errors,
    "live": [row.entry_id for row in memory.search("needle", 100)],
    "deleted": [row.entry_id for row in memory.search("unique deletion before restart", 100)]}))
'''
    restarted = subprocess.run([sys.executable, "-c", code, str(store.storage.root), fixture.thread_id, new_id, str(memory.path)],
        cwd=Path(__file__).resolve().parents[2], check=True, capture_output=True, text=True, timeout=30)
    observed = json.loads(restarted.stdout)
    assert observed["errors"] == []
    assert observed["old"]["thread"]["status"] == "archived"
    assert [row["content"] for row in observed["old"]["messages"] if row["role"] == "assistant"] == [expected]
    assert observed["new"]["messages"] == []
    assert len(observed["live"]) == 100 and observed["deleted"] == []


# LLM: decision仅产生交付候选；外层等待/审计改道前不允许登记成功交付。
# 函数用途: 钉住外层生命周期覆盖合成答复时账本与实际返回不一致。
@pytest.mark.parametrize("route", ["children", "named", "audit"])
def test_outer_lifecycle_reroute_does_not_confirm_candidate_prefix(monkeypatch, tmp_path, route):
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core import _tool_loop_service as tls

    observed = []
    def configure(agent, params):
        if route == "children":
            params = replace(params, context_scope="task_local")
            parent = SimpleNamespace(id=params.run_id, status="RUNNING", owner="synthetic-owner",
                root_id=params.run_id, attributes={}, child_ids=["child"])
            child = SimpleNamespace(id="child", status="RUNNING", owner="synthetic-owner",
                root_id=params.run_id, parent_id=params.run_id, attributes={})
            class Manager:
                def load(self, run_id):
                    return {params.run_id: parent, "child": child}[run_id]
                def mutate(self, run_id, operation):
                    operation(self.load(run_id))
            agent.subagents = Manager()
            return params
        else:
            def reroute(agent, params, **kwargs):
                first = not observed
                ledger = params.live_archive_state.get("_assistant_delivery_segments", {})
                if first:
                    assert not any(row["delivered"] for row in ledger.get("segments", [])
                                   if row["origin"] == "output_limit_resume_prefix")
                observed.append(first)
                return first
            name = "queue_interim_reply_for_active_named_work" if route == "named" else "queue_reply_for_audit_prepare"
            monkeypatch.setattr(tls, name, reroute)

    result, params, calls = _loop(monkeypatch, tmp_path,
        [_response("PREFIX_NEVER_CONFIRMED", truncated=True), _response("SUFFIX"), _response("FINAL")],
        configure=configure)
    assert result.final_response.text == ("SUFFIX" if route == "children" else "FINAL")
    assert calls == (2 if route == "children" else 3)
    if route == "children":
        assert result.final_response.runtime_reason == "SUBAGENTS_ACTIVE"
    rows = params.live_archive_state["_assistant_delivery_segments"]["segments"]
    prefixes = [row for row in rows if row["origin"] == "output_limit_resume_prefix"]
    assert prefixes and all(not row["delivered"] and row["excluded_reason"] for row in prefixes)
    assert not params.live_archive_state.get("_pending_assistant_delivery_segments")
