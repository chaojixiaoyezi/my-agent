"""真实调用准备和 Gateway 压缩出口的身份隔离；仅供应商响应使用本地替身。"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import tool_model_generation as generation
from agent_py_agent.agent.agent_core.model import call_runtime, context_pressure
from agent_py_agent.agent.backends.errors import ProviderContextWindowError
from agent_py_agent.agent.conversation.context_usage import public_context_usage
from agent_py_agent.agent.gateway_parts import request_context, request_execution
from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter
from agent_py_agent.agent.runtime_context import (
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent_py_agent.tests.test_gateway_compact_recovery import _history_request
from agent_py_agent.tests.test_gateway_model_adoption import fake_http
from agent_py_agent.tests.test_gateway_request_runtime_errors import _make_agent
from agent_py_agent.tests.test_tool_model_generation import _tool_loop_params


@pytest.fixture(autouse=True)
def isolated_usage_home(tmp_path, monkeypatch):
    home = tmp_path / "usage-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    monkeypatch.setenv("LLM_MAX_INFLIGHT", "2")
    monkeypatch.setenv("LLM_ADMISSION_WAIT_SECONDS", "0")


# LLM: 原 SimpleAgent/线程与计量准备保留；不替换快照、CAS或 writer，离线 echo 后端不连接网络。
# 函数用途: 在模型调用前通过真实 Gateway 预检将流绑定到自己的会话。
def _call_case(tmp_path):
    agent, _ = _make_agent(tmp_path)
    agent.config.model_context_window_tokens = 400_000
    stream = BufferedChunkStreamWriter(tmp_path / "events.jsonl", rich_transcript=True)
    request = {"conversation": {"canonical_user_id": agent.home_paths.owner_id,
                               "channel": "chat", "channel_conversation_id": "main"}}
    conversation = request_context.preflight_gateway_conversation(request_context.GatewayConversationLoadRequest(
        agent, request, "usage-request", "测试输入", stream))
    child = agent.conversation_store.threads.get_or_create({"canonical_user_id": agent.home_paths.owner_id,
        "channel": "chat", "channel_conversation_id": "child"})
    params = replace(_tool_loop_params(), context_scope="conversation", run_id="main-run", request_id="usage-request",
                     effective_on_chunk=stream, task_attributes={"conversation_thread_id": conversation.thread_id})
    return agent, conversation.thread_id, child.thread_id, stream, params


def _events(stream):
    return [json.loads(line) for line in stream.chunk_path.read_text().splitlines()] if stream.chunk_path.exists() else []


@pytest.mark.parametrize("child", [False, True])
def test_real_preparation_snapshot_and_saved_projection_name_thread_and_producer(tmp_path, child):
    agent, main, child_id, stream, params = _call_case(tmp_path)
    target = child_id if child else main
    attrs = {"conversation_thread_id": main, **({"agent_thread_id": child_id, "agent_run_id": "child-run"} if child else {})}
    params = replace(params, task_attributes=attrs, run_id="child-run" if child else "main-run")
    request = generation.ModelGenerateParams(agent, params, "完整合成输入", 0)
    previous = set_current_subagent_context(agent, run_id="child-run" if child else "")
    try:
        state = generation._start_model_generation(request)
    finally:
        restore_current_subagent_context(agent, previous)
    saved = agent.conversation_store.threads.require(target).model_context_usage
    assert saved.get("thread_id") == target
    assert saved.get("producer") == {"scope": "child" if child else "main", "agent_run_id": params.run_id}
    assert state.ledger.records()[0].input_tokens == saved["current_tokens"]
    assert saved["context_window_tokens"] == 400_000
    assert "usage_binding" not in saved and "context_surface_fingerprint" not in saved
    if child:
        assert not [event for event in _events(stream) if event["kind"] == "context_usage_updated"]
    else:
        assert _events(stream)[0]["context_usage"]["thread_id"] == main


@pytest.mark.parametrize("child_attrs", ["parent-conversation", "child-conversation"])
def test_e3_shared_owner_agent_child_cannot_write_parent_stream(tmp_path, child_attrs):
    agent, main, child, stream, params = _call_case(tmp_path)
    call_runtime.start_model_call_record(generation.ModelGenerateParams(agent, params, "主输入", 0))
    before = [event for event in _events(stream) if event["kind"] == "context_usage_updated"]
    attrs = {"conversation_thread_id": main if child_attrs == "parent-conversation" else child,
             "agent_thread_id": child, "agent_run_id": "child-run"}
    child_params = replace(params, task_attributes=attrs, run_id="child-run", context_scope="task_local")
    previous = set_current_subagent_context(agent, run_id="child-run")
    try:
        call_runtime.start_model_call_record(generation.ModelGenerateParams(agent, child_params, "子输入", 0))
    finally:
        restore_current_subagent_context(agent, previous)
    assert [event for event in _events(stream) if event["kind"] == "context_usage_updated"] == before, "同一个 owner Agent 的子调用不得覆盖父流的上下文状态"
    saved = agent.conversation_store.threads.require(child).model_context_usage
    assert saved.get("thread_id") == child
    assert agent.conversation_store.threads.require(main).model_context_usage["thread_id"] == main


def test_public_projection_keeps_sanitized_identity_without_private_fields(tmp_path):
    agent, main, _child, _stream, params = _call_case(tmp_path)
    public = context_pressure.model_visible_context_snapshot(agent, params, "私有正文").to_public_dict()
    projected = public_context_usage({**public, "thread_id": " " + main + " ",
        "producer": {"scope": "main", "agent_run_id": " run-1 ", "prompt": "私有正文"},
        "usage_binding": object(), "prompt": "私有正文"})
    assert projected.get("thread_id") == main
    assert projected.get("producer") == {"scope": "main", "agent_run_id": "run-1"}
    assert "私有正文" not in str(projected) and "usage_binding" not in projected
    assert public_context_usage(projected) == projected
    malformed = public_context_usage({**public, "thread_id": {"prompt": "私有正文"},
                                    "producer": {"scope": "foreign", "agent_run_id": object()}})
    assert not malformed.get("thread_id") and not malformed.get("producer")


def test_e3_legacy_shared_stream_cannot_follow_child_conversation_params(tmp_path):
    agent, main, child, _stream, params = _call_case(tmp_path)
    snapshots = []
    sink = SimpleNamespace(write_context_usage=snapshots.append)
    params = replace(params, effective_on_chunk=sink)
    call_runtime.start_model_call_record(generation.ModelGenerateParams(agent, params, "主输入", 0))
    assert len(snapshots) == 1 and snapshots[0]["thread_id"] == main
    child_params = replace(params, run_id="child-run", context_scope="task_local", task_attributes={
        "conversation_thread_id": child, "agent_thread_id": child, "agent_run_id": "child-run"})
    call_runtime.start_model_call_record(generation.ModelGenerateParams(agent, child_params, "子输入", 0))
    assert len(snapshots) == 1, "旧流的canonical归属不能跟着共享Agent的子调用参数改变"


# LLM: 从原Gateway进入、由真实供应商错误触发摘要及CAS，再走下一次普通请求；不手工构造压缩事件。
# 函数用途: 同时钉住进度/边界身份与压缩后400K显式档案，证明不是旧快照或旧调用。
@pytest.mark.parametrize("surface", ["progress", "boundary", "post-compact"])
def test_actual_gateway_compact_frames_and_next_call_keep_explicit_400k(tmp_path, monkeypatch, surface):
    fixture = _history_request(tmp_path, original_window=400_000)
    stream = BufferedChunkStreamWriter(tmp_path / "compact-events.jsonl", rich_transcript=True)
    context = replace(fixture.context, on_chunk=stream)
    seen = []

    def on_business(wire):
        seen.append(wire["model"])
        if len(seen) == 1:
            raise ProviderContextWindowError("合成供应商窗口溢出")

    business, _ = fake_http(monkeypatch, fixture, on_business=on_business)
    request_execution._run_gateway_ask(context)
    thread = fixture.agent.conversation_store.threads.require(fixture.thread_id)
    assert thread.compact_generation == 1 and thread.model_profile_id == fixture.original
    assert len(business) == 3 and set(seen) == {"original-model"}
    events = _events(stream)
    if surface == "progress":
        frames = [row["compact_progress"] for row in events if row["kind"] == "conversation_compaction_progress"]
        assert frames
        assert all(row.get("thread_id") == fixture.thread_id and row.get("model_name") == "original-model" for row in frames)
        started_usage = [row["context_usage"] for row in events if row["kind"] == "context_usage_updated"]
        assert started_usage and all(row.get("model_name") == "original-model" for row in started_usage)
    elif surface == "boundary":
        frames = [row for row in events if row["kind"] == "conversation_compacted"]
        assert len(frames) == 1
        assert frames[0].get("thread_id") == fixture.thread_id
        assert frames[0].get("model_name") == "original-model"
    else:
        saved = public_context_usage(thread.model_context_usage)
        assert saved.get("thread_id") == fixture.thread_id
        assert saved["model_name"] == "original-model" and saved["context_window_tokens"] == 400_000
        assert saved.get("producer", {}).get("scope") == "main"
        # 本测试入口止于原模型回合，队列终态不在本slice；按原精确claim API收尾后才发第二个请求。
        claim = fixture.agent.conversation_store.claims.load(fixture.thread_id)
        fixture.agent.conversation_store.claims.finish({"thread_id": fixture.thread_id,
            "claim_id": claim["claim_id"], "task_id": claim["task_id"], "status": "finished",
            "runtime_facts": {"execution_source": "gateway", "request_id": context.request_id}})
        assert fixture.agent.conversation_store.claims.load(fixture.thread_id)["status"] == "finished"
        next_request = {"id": "usage-next", "kind": "ask", "prompt": "继续整理", "status": "processing",
                        "turn_phase": "open", "execution_attempt_id": "transport-two",
                        "conversation": context.request["conversation"]}
        next_path = fixture.paths.processing / "usage-next.json"
        next_path.write_text(json.dumps(next_request), encoding="utf-8")
        next_context = replace(context, request=next_request, request_id="usage-next", request_path=next_path)
        request_execution._run_gateway_ask(next_context)
        after = fixture.agent.conversation_store.threads.require(fixture.thread_id)
        assert after.compact_generation == 1 and after.model_profile_id == fixture.original
        assert after.model_context_usage["model_name"] == "original-model"
        assert after.model_context_usage["context_window_tokens"] == 400_000
        assert business[-1][0]["model"] == "original-model"


def test_import_origin_is_the_owner_snapshot():
    assert Path(call_runtime.__file__).resolve() == Path(__file__).resolve().parents[1] / "agent/agent_core/model/call_runtime.py"
