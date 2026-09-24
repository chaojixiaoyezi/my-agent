"""Gateway overflow 走原恢复准备、Compact CAS 与真实 provider builder；仅 HTTP 为内存替身。"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent_py_agent.agent import gateway_compact_recovery as recovery
from agent_py_agent.agent.agent_core import _tool_loop_service as service
from agent_py_agent.agent.agent_core import provider_transient_auto_resume, runtime_mixin
from agent_py_agent.agent.backends import http
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
def _history_request(tmp_path, *, tools=False, mode="disabled", candidate_backend="anthropic_compatible", original_window=250_000, history_repeat=100):
    fixture = actual_request(tmp_path, mode=mode, tools=tools, candidate_backend=candidate_backend, original_window=original_window)
    agent = fixture.agent
    context = fixture.context
    context.request["inject"] = ["# Conversation Context", "", "# Conversation Context"]
    conversation = request_context.gateway_conversation_context(request_context.GatewayConversationLoadRequest(
        agent, context.request, context.request_id, context.request["prompt"],
    ))
    for role in ("user", "assistant"):
        assert append_gateway_conversation_message(agent, {}, conversation, request_id="previous-" + role,
                                                   role=role, content="之前核对过的原始资料" * history_repeat)
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
    assert len(prepares) == 2 and len(rendered) == 2 and len(projections) == 1
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


@pytest.mark.parametrize("mode,reject", [("disabled", False), ("apply", False), ("apply", True)])
def test_initial_compact_precedes_model_adoption_and_rejection_baseline(tmp_path, monkeypatch, mode, reject):
    from agent_py_agent.tests.test_decision_settings import patch
    from agent_py_agent.tests.test_gateway_model_adoption import before_final_check

    fixture = _history_request(tmp_path, mode=mode, original_window=20_000, history_repeat=2500)
    decision = install_backend(monkeypatch, fixture)
    prepares, summaries, sends, candidates = [], [], [], []
    original = runtime_mixin._prepare_runtime_context
    original_project = recovery._project_recovery_candidate

    def project(*args):
        material = original_project(*args)
        if args[-1].is_candidate:
            candidates.append(material)
        return material

    monkeypatch.setattr(recovery, "_project_recovery_candidate", project)

    def prepare(*args, **kwargs):
        prepares.append(True)
        return original(*args, **kwargs)

    def on_business(wire):
        host = _HOST.get()
        compact_host = host.compact_recovery
        if not compact_host.committed:
            assert not compact_host.force
            summaries.append(wire)
        else:
            sends.append(wire)
            assert compact_host.committed_thread.compact_generation == 1
            assert compact_host.resolved_input is not None
            material = candidates[-1]
            assert material.params.conversation_history_seed.compact_generation == 1
            if wire["model"] == "original-model":
                projected = material.projection
                assert wire == _payload(fixture.agent.backend, projected.provider_prompt,
                                        list(material.request_input.native_tools) or None,
                                        projected.tool_choice, projected.messages, projected.system_instruction)

    monkeypatch.setattr(runtime_mixin, "_prepare_runtime_context", prepare)
    if reject:
        before_final_check(monkeypatch, lambda *_: patch(fixture.agent, {"points.model_selection.mode": "off"}))
    fake_http(monkeypatch, fixture, on_business=on_business)
    request_execution._run_gateway_ask(fixture.context)
    assert len(prepares) == 1 and summaries and len(sends) == 1
    assert all(wire["model"] == "original-model" for wire in summaries)
    assert sends[0]["model"] == ("candidate-large" if mode == "apply" and not reject else "original-model")
    assert len(decision.calls) == (mode == "apply")
    thread = fixture.agent.conversation_store.threads.require(fixture.thread_id)
    assert thread.compact_generation == 1


def test_initial_without_compactable_source_can_adopt_larger_model(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.model.context_pressure import (
        model_request_input_ceiling,
        projected_model_context_components,
    )
    from agent_py_agent.agent.agent_core.tool_request_projection import project_tool_loop_request

    fixture = actual_request(tmp_path, original_window=15_000)
    fixture.agent.config.memory_compact_auto_trigger_percent = 50
    decision = install_backend(monkeypatch, fixture)
    evaluated = []
    original = recovery.PreparedCompactRecovery._automatic_noop

    def check(self, frozen, tool_source):
        projection = project_tool_loop_request(frozen)
        tokens, _ = projected_model_context_components(projection)
        evaluated.append((tokens, self.source.policy.trigger_tokens,
                          model_request_input_ceiling(self.agent, self.source.policy.context_window_tokens)))
        assert not self.source.messages and tool_source is None
        return original(self, frozen, tool_source)

    monkeypatch.setattr(recovery.PreparedCompactRecovery, "_automatic_noop", check)
    business, _ = fake_http(monkeypatch, fixture)
    request_execution._run_gateway_ask(fixture.context)
    assert len(evaluated) == 1
    tokens, trigger, ceiling = evaluated[0]
    assert trigger < tokens < ceiling
    assert [wire["model"] for wire, _ in business] == ["candidate-large"]
    assert len(decision.calls) == 1
    assert fixture.agent.conversation_store.threads.require(fixture.thread_id).compact_generation == 0


# LLM: 复现"提交恢复候选后瞬断重试发回压缩前旧请求"：同一次模型请求的每次重试都必须原样发送已提交候选，
# 不在旧参数上重建、共享预算回收或注入插话；摘要与 CAS 各一次，代次保持 1，重试耗尽也不会再恢复一次。
# 成功后下一工具轮沿候选参数正常重建，不再命中该记录。
@pytest.mark.parametrize("outcome", ["recovers", "exhausted"])
def test_transient_retry_after_commit_resends_committed_candidate(tmp_path, monkeypatch, outcome):
    fixture = _history_request(tmp_path, tools=outcome == "recovers")
    agent = fixture.agent
    material_path = agent.root / "material.txt"
    material_path.write_text("需要继续核对的资料", encoding="utf-8")
    projections, builds, fits, waits = [], [], [], []
    original_project = recovery._project_recovery_candidate
    original_build = service.build_tool_loop_prompt
    original_fit = service._fit_native_ir_to_shared_budget

    def project(*args):
        value = original_project(*args)
        if args[-1].is_candidate:
            projections.append(value)
        return value

    def build(*args, **kwargs):
        builds.append(True)
        return original_build(*args, **kwargs)

    def fit(*args, **kwargs):
        fits.append(True)
        return original_fit(*args, **kwargs)

    monkeypatch.setattr(recovery, "_project_recovery_candidate", project)
    monkeypatch.setattr(service, "build_tool_loop_prompt", build)
    monkeypatch.setattr(service, "_fit_native_ir_to_shared_budget", fit)
    monkeypatch.setattr(provider_transient_auto_resume, "_wait_before_retry", lambda *_: waits.append(True))
    seen, marks, wires = [], [], []

    def on_business(wire):
        host = _HOST.get()
        if not seen:
            seen.append("overflow")
            raise ProviderContextWindowError("测试供应商上下文溢出")
        if not host.compact_recovery.committed:
            seen.append("summary")
            return
        material = projections[-1]
        projected = material.projection
        expected = _payload(agent.backend, projected.provider_prompt, list(material.request_input.native_tools) or None,
                            projected.tool_choice, projected.messages, projected.system_instruction)
        seen.append("candidate" if wire == expected else "rebuilt")
        marks.append((len(builds), len(fits)))
        wires.append(wire)
        if outcome == "exhausted" or seen.count("candidate") == 1:
            raise ProviderTransientError("503 after commit")

    fake_http(monkeypatch, fixture, on_business=on_business)
    installed = http.post_json

    # 重试成功的那次候选回一个工具调用，让本回合进入下一工具轮。
    def send(request):
        result = installed(request)
        if outcome == "recovers" and seen[-1:] == ["candidate"] and seen.count("candidate") == 2 and len(wires) == 2:
            return {"content": [{"type": "tool_use", "id": "read-material", "name": "read_file",
                                 "input": {"path": str(material_path)}}],
                    "stop_reason": "tool_use", "usage": {"input_tokens": 101, "output_tokens": 5}}
        return result

    monkeypatch.setattr(http, "post_json", send)
    if outcome == "recovers":
        request_execution._run_gateway_ask(fixture.context)
    else:
        with pytest.raises(ProviderTransientError):
            request_execution._run_gateway_ask(fixture.context)

    sends = seen[2:]
    assert seen[:2] == ["overflow", "summary"]
    if outcome == "recovers":
        assert sends == ["candidate", "candidate", "rebuilt"]
        # 重试与首次候选之间没有重建、没有共享预算回收；下一工具轮才按候选参数正常重建。
        assert marks[0] == marks[1] and marks[2][0] > marks[1][0]
        assert "read-material" in json.dumps(wires[2], ensure_ascii=False)
    else:
        assert waits and sends == ["candidate"] * (1 + len(waits))
        assert len(set(marks)) == 1
    thread = agent.conversation_store.threads.require(fixture.thread_id)
    assert thread.compact_generation == 1
    checkpoints = agent.home_paths.owner_compact_dir / "conversations" / f"{fixture.thread_id}.jsonl"
    rows = [json.loads(line) for line in checkpoints.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [row["generation"] for row in rows if row.get("schema") == "conversation_compact_checkpoint.v3"] == [1]
