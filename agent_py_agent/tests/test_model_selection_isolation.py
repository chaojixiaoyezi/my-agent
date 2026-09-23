"""同一Agent的两条Gateway车道并发采用/保留；HTTP为替身，配置和发送事务均沿原入口。"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent import gateway_model_adoption
from agent_py_agent.agent.agent_core.model.call_runtime import model_call_summary
from agent_py_agent.agent.backends import http
from agent_py_agent.agent.gateway_parts import request_context, request_execution
from agent_py_agent.agent.model_request_selection import _HOST
from agent_py_agent.agent.settings import model_profiles
from agent_py_agent.tests.test_decision_settings import patch
from agent_py_agent.tests.test_gateway_model_adoption import actual_request
from agent_py_agent.tests.test_gateway_model_observation import (
    _optional_admission,  # noqa: F401
    install_backend,
)
from agent_py_agent.tests.test_model_profiles import add


# LLM: 复用同一owner/Agent创建另一条真实队列消息和线程，不复制运行权限或借用第一条请求的绑定。
# 函数用途: 让并发测试覆盖相同实例的ContextVar、线程CAS和用量隔离，只写pytest临时目录。
def _peer(first):
    request = {
        "id": "peer-request", "kind": "ask", "prompt": "独立核对第二份资料",
        "status": "processing", "turn_phase": "open", "execution_attempt_id": "transport-peer",
        "conversation": {"canonical_user_id": first.agent.home_paths.owner_id, "channel": "chat",
                         "channel_conversation_id": "peer-session", "channel_user_id": first.agent.home_paths.owner_id},
    }
    path = first.paths.processing / "peer-request.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    context = request_context.GatewayAskRunContext(
        first.agent, request, path, first.paths.responses / "peer-response.json", request["id"], None,
    )
    current = request_context.preflight_gateway_conversation(request_context.GatewayConversationLoadRequest(
        first.agent, request, request["id"], request["prompt"],
    ))
    model_profiles.execute_model_profile_operation(
        first.agent, "select", {"profile_id": first.original}, thread_id=current.thread_id,
    )
    return SimpleNamespace(agent=first.agent, context=context, thread_id=current.thread_id)


@pytest.mark.parametrize("window,original_window", [(1_000_000, 250_000), (250_000, 1_000_000)])
@pytest.mark.parametrize("failure", ["off", "explicit_selection", "admission_busy", "unknown_protocol"])
def test_parallel_candidate_rejection_keeps_other_thread_adoption(tmp_path, monkeypatch, window, original_window, failure):
    monkeypatch.setenv("LLM_MAX_INFLIGHT", "2" if failure == "admission_busy" else "3")
    first = actual_request(tmp_path, window=window, original_window=original_window)
    second = _peer(first)
    agent = first.agent
    defaults = agent.config, agent.backend, agent.prompts
    decision = install_backend(monkeypatch, first)
    if failure == "unknown_protocol":
        unsupported, _ = add(agent, model_name="unproven-responses", model_backend="openai_responses",
                             model_context_window_tokens=window)
        original_decide = decision.decide

        def decide(request, *, deadline):
            response = original_decide(request, deadline=deadline)
            if request.binding.thread_id == second.thread_id:
                return replace(response, answers=(replace(response.answers[0], value=unsupported),))
            return response

        monkeypatch.setattr(decision, "decide", decide)
    barrier = threading.Barrier(2)
    first_ready = threading.Event()
    rejection_checked = threading.Event()
    original_before = gateway_model_adoption.GatewayModelAdoption.before_send
    arrivals, sent = [], []
    lock = threading.Lock()

    def before(adoption, backend, prompt, state):
        if adoption.candidate is not None and not adoption.submitted:
            tid = adoption.thread.thread_id
            with lock:
                arrivals.append(tid)
            if tid == first.thread_id:
                first_ready.set()
            barrier.wait(timeout=10)
            if tid == first.thread_id and failure not in {"admission_busy", "unknown_protocol"}:
                if failure == "off":
                    changed = patch(agent, {"points.model_selection.mode": "off"}, thread_id=tid, scope="thread")
                    assert changed["ok"], changed
                else:
                    model_profiles.execute_model_profile_operation(
                        agent, "select", {"profile_id": first.original}, thread_id=tid,
                    )
            if tid == second.thread_id:
                assert rejection_checked.wait(10), "先完成第一条的拒绝复核，再核对第二条独立采用"
        try:
            return original_before(adoption, backend, prompt, state)
        finally:
            if adoption.thread.thread_id == first.thread_id:
                rejection_checked.set()

    def send(request):
        host = _HOST.get()
        assert host is not None
        tid = first.thread_id if host.context.request_id == first.context.request_id else second.thread_id
        if failure in {"admission_busy", "unknown_protocol"} and tid == second.thread_id:
            assert host.adoption is None or host.adoption.candidate is None
            barrier.wait(timeout=10)
        else:
            assert host.adoption is not None
        wire = json.loads(json.dumps(request.payload))
        with lock:
            sent.append((tid, wire, agent.config.model_name, agent.backend.model_name))
        assert wire["max_tokens"] == 4096
        retained = tid == (second.thread_id if failure in {"admission_busy", "unknown_protocol"} else first.thread_id)
        assert agent.config.model_context_window_tokens == (original_window if retained else window)
        return {"content": [{"type": "text", "text": "本会话核对完成。"}], "stop_reason": "end_turn",
                "usage": {"input_tokens": 101 if tid == first.thread_id else 203, "output_tokens": 5}}

    monkeypatch.setattr(gateway_model_adoption.GatewayModelAdoption, "before_send", before)
    monkeypatch.setattr(http, "post_json", send)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(request_execution._run_gateway_ask, first.context)]
        assert first_ready.wait(10), "第一条已准备候选才开始交错另一条请求"
        pending.append(pool.submit(request_execution._run_gateway_ask, second.context))
        results = [item.result(timeout=30) for item in pending]
    assert all(item.response == "本会话核对完成。" for item in results)
    expected_decisions = {first.thread_id} if failure == "admission_busy" else {first.thread_id, second.thread_id}
    expected_arrivals = {first.thread_id} if failure in {"admission_busy", "unknown_protocol"} else expected_decisions
    assert set(arrivals) == expected_arrivals and len(arrivals) == len(expected_arrivals)
    assert len(sent) == 2 and len(decision.calls) == len(expected_decisions)
    assert {item[0].binding.thread_id for item in decision.calls} == expected_decisions
    by_thread = {tid: (wire, config_name, backend_name) for tid, wire, config_name, backend_name in sent}
    retained_fixture, adopted_fixture = (second, first) if failure in {"admission_busy", "unknown_protocol"} else (first, second)
    for fixture, expected, profile in ((retained_fixture, "original-model", first.original), (adopted_fixture, "candidate-large", first.candidate)):
        wire, config_name, backend_name = by_thread[fixture.thread_id]
        assert wire["model"] == config_name == backend_name == expected
        current = agent.conversation_store.threads.require(fixture.thread_id)
        assert current.model_profile_id == profile
        assert current.model_selection_source == ("explicit" if fixture is retained_fixture else "automatic")
        usage = model_call_summary(agent, request_id=fixture.context.request_id)
        if fixture.thread_id in expected_decisions:
            assert usage["purpose_breakdown"]["decision"]["usage_breakdown"]["provider"]["input_tokens"] == 17
    assert (agent.config, agent.backend, agent.prompts) == defaults
    assert _HOST.get() is None


@pytest.mark.parametrize("window,expected_model", [(250_000, "original-model"), (1_000_000, "candidate-large")])
def test_full_large_request_keeps_history_when_small_candidate_cannot_fit(tmp_path, monkeypatch, window, expected_model):
    from agent_py_agent.agent.conversation.native_history import (
        CANONICAL_NATIVE_MESSAGES_METADATA_KEY,
        canonical_native_messages_envelope,
    )
    from agent_py_agent.tests.test_gateway_model_adoption import fake_http

    fixture = actual_request(tmp_path, tools=True, original_window=1_000_000, window=window)
    requirement = "COMPLETE_CURRENT_BEGIN " + "a" * 760_000 + " COMPLETE_CURRENT_END"
    fixture.agent.config.system_prompt += "\n" + requirement
    history = [
        {"role": "user", "content": "OLD_TOOL_REQUEST"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "old-read", "name": "read_file", "input": {"path": "prior.txt"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "old-read", "content": "COMPLETE_OLD_RESULT" * 100}]},
        {"role": "assistant", "content": "OLD_TOOL_DONE"},
    ]
    fixture.agent.conversation_store.messages.append({
        "thread_id": fixture.thread_id, "role": "assistant", "content": "旧材料已经核对。",
        "metadata": {"conversation_request_id": "completed-old-tool",
                     CANONICAL_NATIVE_MESSAGES_METADATA_KEY: canonical_native_messages_envelope(history)},
    })
    decision = install_backend(monkeypatch, fixture)
    measured = []
    original_capacity = gateway_model_adoption._capacity

    def capacity(config, payload):
        from agent_py_agent.agent.memory_archive import estimate_tokens

        measured.append((config.model_context_window_tokens, estimate_tokens(payload), payload["max_tokens"]))
        return original_capacity(config, payload)

    monkeypatch.setattr(gateway_model_adoption, "_capacity", capacity)
    business, _ = fake_http(monkeypatch, fixture)
    result = request_execution._run_gateway_ask(fixture.context)
    assert result.response == "资料整理完成。" and len(decision.calls) == 1
    assert len(business) == 1 and business[0][0]["model"] == expected_model
    wire = business[0][0]
    encoded = json.dumps(wire, ensure_ascii=False)
    assert requirement in encoded, "当前要求不能为适应小窗而裁剪"
    assert "COMPLETE_OLD_RESULT" * 100 in encoded and "old-read" in encoded
    assert wire.get("tools") and wire["max_tokens"] == 4096
    assert measured and all(250_000 < tokens + cap < 1_000_000 for _, tokens, cap in measured)
    thread = fixture.agent.conversation_store.threads.require(fixture.thread_id)
    assert thread.model_profile_id == (fixture.original if window == 250_000 else fixture.candidate)
    assert thread.compact_generation == 0, "窗口充足的原模型不能因候选不够而被强行改写历史"


@pytest.mark.parametrize("protocol", ["native", "text"])
def test_connection_rotation_keeps_active_snapshot_but_rejects_old_calibration(tmp_path, protocol):
    from agent_py_agent.agent.agent_core.model.context_pressure import (
        model_visible_context_snapshot,
        record_provider_context_observation,
    )
    from agent_py_agent.agent.settings.model_scope import selected_model_scope
    from agent_py_agent.tests.test_context_pressure_native_trigger import _params

    fixture = actual_request(tmp_path, mode="disabled")
    other = _peer(fixture)
    agent = fixture.agent
    prompt = "稳定模型输入 " + "material " * 1000
    params = replace(_params(protocol=protocol), task_attributes={"conversation_thread_id": fixture.thread_id})
    with selected_model_scope(agent, thread_id=fixture.thread_id):
        before = model_visible_context_snapshot(agent, params, prompt)
        assert record_provider_context_observation(
            agent, params, raw_estimated_tokens=before.raw_estimated_tokens,
            context_surface_fingerprint=before.context_surface_fingerprint,
            response=SimpleNamespace(usage={"input_tokens": before.raw_estimated_tokens // 3}),
        )
        calibrated = model_visible_context_snapshot(agent, params, prompt)
        assert calibrated.current_tokens < calibrated.raw_estimated_tokens
        peer_params = replace(_params(protocol=protocol), task_attributes={"conversation_thread_id": other.thread_id})
        peer_snapshot = model_visible_context_snapshot(agent, peer_params, prompt)
        assert peer_snapshot.current_tokens == peer_snapshot.raw_estimated_tokens
        data = model_profiles.read_model_profiles(model_profiles.model_profiles_path(agent.home_paths))
        row = data["profiles"][fixture.original]
        old_endpoint = agent.backend.api_base
        model_profiles.execute_model_profile_operation(agent, "save_provider", {
            "provider_id": row["provider_id"], "editing": True,
            "provider": {**data["providers"][row["provider_id"]], "api_base": "https://rotated.example.test/anthropic"},
        })
        model_profiles.execute_model_profile_operation(agent, "save_model", {
            "profile_id": fixture.original, "editing": True,
            "profile": {**row, "model_context_window_tokens": 1_000_000},
        })
        assert agent.backend.api_base == old_endpoint
        assert model_visible_context_snapshot(agent, params, prompt) == calibrated
    with selected_model_scope(agent, thread_id=fixture.thread_id):
        assert agent.config.model_name == "original-model"
        assert agent.config.model_context_window_tokens == 1_000_000
        assert agent.backend.api_base != old_endpoint
        fresh = replace(_params(protocol=protocol), task_attributes={"conversation_thread_id": fixture.thread_id})
        changed_protocol = replace(_params(protocol="text" if protocol == "native" else "native"), task_attributes=fresh.task_attributes)
        for source in (params, fresh, changed_protocol):
            after = model_visible_context_snapshot(agent, source, prompt)
            assert after.context_surface_fingerprint != before.context_surface_fingerprint
            assert after.current_tokens == after.raw_estimated_tokens
            assert after.context_window_tokens == 1_000_000
        persisted = agent.conversation_store.threads.require(fixture.thread_id).provider_context_observation
        assert persisted["context_surface_fingerprint"] == before.context_surface_fingerprint
        assert "rotated.example" not in json.dumps(persisted), "原账仅存摘要事实，不写入连接明文"
