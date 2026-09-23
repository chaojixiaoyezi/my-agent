"""Gateway overflow 走原恢复准备、Compact CAS 与真实 provider builder；仅 HTTP 为内存替身。"""
from __future__ import annotations

from dataclasses import replace

import pytest

from agent_py_agent.agent import gateway_compact_recovery as recovery
from agent_py_agent.agent.agent_core import provider_transient_auto_resume, runtime_mixin
from agent_py_agent.agent.backends.errors import (
    ProviderContextWindowError,
    ProviderTimeoutError,
    ProviderTransientError,
)
from agent_py_agent.agent.conversation import compact
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.gateway_model_adoption import _payload
from agent_py_agent.agent.gateway_parts import request_context, request_execution
from agent_py_agent.agent.gateway_parts.request_history import append_gateway_conversation_message
from agent_py_agent.agent.model_request_selection import _HOST
from agent_py_agent.tests.test_gateway_model_adoption import actual_request, fake_http
from agent_py_agent.tests.test_gateway_model_observation import install_backend


# LLM: 初始化真实owner/thread和历史；只用测试模型配置，不伪造恢复参数、来源或Compact提交。
# 函数用途: 给恢复链验收准备一条旧历史与带重复注入文本的普通新请求。
def _history_request(tmp_path, *, tools=False, mode="disabled", candidate_backend="anthropic_compatible"):
    fixture = actual_request(tmp_path, mode=mode, tools=tools, candidate_backend=candidate_backend)
    agent = fixture.agent
    context = fixture.context
    context.request["inject"] = ["# Conversation Context", "", "# Conversation Context"]
    conversation = request_context.gateway_conversation_context(request_context.GatewayConversationLoadRequest(
        agent, context.request, context.request_id, context.request["prompt"],
    ))
    for role in ("user", "assistant"):
        assert append_gateway_conversation_message(agent, {}, conversation, request_id="previous-" + role,
                                                   role=role, content="之前核对过的原始资料" * 100)
    return fixture


@pytest.mark.parametrize("tools", [False, True])
@pytest.mark.parametrize("mode,candidate_backend", [
    ("disabled", "anthropic_compatible"),
    ("apply", "anthropic_compatible"),
    ("apply", "openai_compatible"),
])
def test_full_gateway_recovery_reuses_preparation_and_sends_selected_payload(tmp_path, monkeypatch, tools, mode, candidate_backend):
    fixture = _history_request(tmp_path, mode=mode, tools=tools, candidate_backend=candidate_backend)
    decision = install_backend(monkeypatch, fixture)
    agent, context = fixture.agent, fixture.context
    prepares, projections, rendered = [], [], []
    original_prepare = runtime_mixin._prepare_runtime_context
    original_project = recovery._project_recovery_candidate
    original_render = recovery.PreparedCompactRecovery.render

    def prepare(*args, **kwargs):
        prepares.append(args)
        return original_prepare(*args, **kwargs)

    def project(*args):
        value = original_project(*args)
        if args[-1].is_candidate:
            projections.append(value)
        return value

    def render(self, *args):
        value = original_render(self, *args)
        if value is not None:
            rendered.append(self.prompt_input)
        return value

    monkeypatch.setattr(runtime_mixin, "_prepare_runtime_context", prepare)
    monkeypatch.setattr(recovery, "_project_recovery_candidate", project)
    monkeypatch.setattr(recovery.PreparedCompactRecovery, "render", render)
    seen = []

    def on_business(wire):
        host = _HOST.get()
        if not seen:
            seen.append("overflow")
            raise ProviderContextWindowError("测试供应商上下文溢出")
        if host.compact_recovery is not None and not host.compact_recovery.committed:
            seen.append("summary")
            return
        seen.append("restored")
        material = projections[-1]
        projected = material.projection
        expected = _payload(agent.backend, projected.provider_prompt,
                            list(material.request_input.native_tools) or None, projected.tool_choice,
                            projected.messages, projected.system_instruction)
        assert wire == expected

    business, _ = fake_http(monkeypatch, fixture, on_business=on_business)
    request_execution._run_gateway_ask(context)
    assert seen == ["overflow", "summary", "restored"]
    assert len(business) == 3
    assert len(prepares) == 2 and len(rendered) == 1 and len(projections) == 1
    material = projections[0]
    assert material.request_input.prompt_input.injection_fragments[:3] == tuple(context.request["inject"])
    assert material.host_state.compact_operation_evidence_ref
    assert agent.conversation_store.threads.require(fixture.thread_id).compact_generation == 1
    assert len(decision.calls) == (1 if mode == "apply" else 0)


@pytest.mark.parametrize("failure", ["generation", "cancel", "token", "unknown", "summary_transient", "summary_timeout"])
def test_uncommitted_recovery_never_sends_business_request(tmp_path, monkeypatch, failure):
    fixture = _history_request(tmp_path)
    agent = fixture.agent
    original_project = recovery._project_recovery_candidate
    hosts = []
    waits = []
    monkeypatch.setattr(provider_transient_auto_resume, "_wait_before_retry", lambda *_: waits.append(True))
    if failure.startswith("summary_"):
        def summary(*args, **kwargs):
            if failure == "summary_transient":
                raise ProviderTransientError("503 temporary overload")
            raise ProviderTimeoutError("first event timeout", stage="first_event")

        monkeypatch.setattr(compact, "_summarize", summary)

    def project(*args):
        view = args[-1]
        if view.is_candidate:
            if failure == "generation":
                agent.conversation_store.threads.update_atomic(
                    fixture.thread_id, lambda thread: replace(thread, compact_generation=thread.compact_generation + 1,
                                                              summary="另一个提交者已经获胜"),
                )
            elif failure == "cancel":
                raise InterruptedError("测试取消候选")
            elif failure == "token":
                token = hosts[0].compact_recovery.render_params.cancellation_token
                assert token is not None
                token.cancel("只取消运行令牌")
            else:
                raise ConversationCompactError("测试未知完整输入", code="COMPACT_REQUEST_PROJECTION_UNKNOWN")
        return original_project(*args)

    monkeypatch.setattr(recovery, "_project_recovery_candidate", project)
    seen = []

    def on_business(wire):
        host = _HOST.get()
        if not seen:
            seen.append("overflow")
            hosts.append(host)
            raise ProviderContextWindowError("测试供应商上下文溢出")
        assert host.compact_recovery is not None and not host.compact_recovery.committed
        seen.append("summary")

    fake_http(monkeypatch, fixture, on_business=on_business)
    with pytest.raises((RuntimeError, InterruptedError)):
        request_execution._run_gateway_ask(fixture.context)
    assert seen == (["overflow"] if failure.startswith("summary_") else ["overflow", "summary"])
    assert waits == []
    assert hosts[0].compact_recovery.committed is False
    assert hosts[0].compact_recovery.host_state.compact_generation == 0
    thread = agent.conversation_store.threads.require(fixture.thread_id)
    assert thread.compact_generation == (1 if failure == "generation" else 0)
    if failure == "generation":
        assert thread.summary == "另一个提交者已经获胜"
