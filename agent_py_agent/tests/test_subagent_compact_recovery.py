"""子代理完整 Compact 恢复：真实 runner/store/builder，只替换最终 HTTP。"""
from __future__ import annotations

import json
import re
from dataclasses import replace

import pytest

from agent_py_agent.agent.agent_core import provider_transient_auto_resume, runtime_mixin
from agent_py_agent.agent.agent_core.subagent import compact_recovery
from agent_py_agent.agent.backends import http
from agent_py_agent.agent.backends.base import ProviderRequestOptions
from agent_py_agent.agent.backends.errors import ProviderContextWindowError, ProviderTransientError
from agent_py_agent.agent.conversation import compact
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


# LLM: child、旧历史和 attempt 都由原 store/lifecycle 建立；只在 HTTP 层模拟供应商响应。
# 函数用途: 构造真实子代理恢复场景，旧历史低于预检阈值，让供应商 overflow 触发压缩。
def _child(tmp_path, *, backend: str, tools: bool):
    api_base = "https://api.minimaxi.com/anthropic" if backend == "anthropic_compatible" else "https://opencode.ai/zen/go/v1"
    model_name = "MiniMax-M2.7" if backend == "anthropic_compatible" else "deepseek-v4-flash"
    agent = SimpleAgent(AgentConfig(
        model_backend=backend, model_name=model_name, api_base=api_base,
        api_key="fake-private-key", stream_enabled=False, enable_tools=tools,
        model_context_window_tokens=200_000, model_context_window_explicit=True,
        max_tool_rounds=1, tool_context_ptl_retry_max=0,
    ), tmp_path)
    task = agent.subagents.create_run(goal="核对材料并给出结论", thought="", plan=[], allowed_tools=["read_file"])
    for role, content in (("user", "之前需要核对的原始材料" * 100), ("assistant", "已核对旧材料，等待下一步" * 100)):
        agent.conversation_store.messages.append({
            "thread_id": task.agent_thread_id, "role": role, "content": content,
            "metadata": {"conversation_request_id": "completed-prior-turn"},
        })
    return agent, task


# LLM: 仅探针、摘要、业务的物理 HTTP 返回是假数据；供应商 payload 始终由真实 backend builder 产生。
# 函数用途: 捕获业务发送并可在首次发送时引发 typed provider overflow。
def _http(monkeypatch, *, backend: str, on_business):
    business, probes = [], []

    def response(text: str, *, tool=None):
        if backend == "anthropic_compatible":
            return {"content": [tool] if tool else [{"type": "text", "text": text}], "stop_reason": "tool_use" if tool else "end_turn"}
        message = {"content": text}
        if tool:
            message["tool_calls"] = [{"id": tool["id"], "type": "function", "function": {"name": tool["name"], "arguments": json.dumps(tool["input"])}}]
        return {"choices": [{"message": message, "finish_reason": "tool_calls" if tool else "stop"}]}

    def send(request):
        wire = json.loads(json.dumps(request.payload, ensure_ascii=False))
        names = [row.get("name") or row.get("function", {}).get("name") for row in wire.get("tools", [])]
        if names == ["my_agent_capability_probe"]:
            probes.append(wire)
            nonce = re.search(r"nonce ([0-9a-f]+)", json.dumps(wire))[1]
            return response("", tool={"type": "tool_use", "id": "probe", "name": names[0], "input": {"nonce": nonce}})
        business.append(wire)
        on_business(wire, len(business))
        return response("旧轮已归纳；继续当前工作。" if len(business) == 2 else "材料核对完成。")

    monkeypatch.setattr(http, "post_json", send)
    return business, probes


@pytest.mark.parametrize("backend", ["anthropic_compatible", "openai_compatible"])
@pytest.mark.parametrize("tools", [False, True])
def test_child_overflow_commits_full_candidate_and_sends_identical_wire(tmp_path, monkeypatch, backend, tools):
    agent, task = _child(tmp_path, backend=backend, tools=tools)
    prepares, candidates, recovery_instances, sent_attempts, generations = [], [], [], [], []
    original_prepare = runtime_mixin._prepare_runtime_context
    original_project = compact_recovery._project_subagent_candidate
    original_recovery = compact_recovery.prepare_subagent_compact_recovery

    def prepare(*args, **kwargs):
        prepares.append(args)
        return original_prepare(*args, **kwargs)

    def project(*args):
        material = original_project(*args)
        if args[-1].is_candidate:
            candidates.append(material)
        return material

    def recovery(*args, **kwargs):
        value = original_recovery(*args, **kwargs)
        recovery_instances.append(value)
        return value

    monkeypatch.setattr(runtime_mixin, "_prepare_runtime_context", prepare)
    monkeypatch.setattr(compact_recovery, "_project_subagent_candidate", project)
    monkeypatch.setattr(compact_recovery, "prepare_subagent_compact_recovery", recovery)

    def on_business(wire, number):
        sent_attempts.append(agent.subagents.load(task.id).runner_active_attempt_id)
        generations.append(agent.conversation_store.threads.require(task.agent_thread_id).compact_generation)
        if number == 1:
            raise ProviderContextWindowError("测试供应商上下文溢出")
        if number == 3:
            material = candidates[-1]
            projection = material.projection
            frozen = material.request_input
            expected = agent.backend.project_generate_payload(
                projection.provider_prompt, tools=list(frozen.native_tools) or None,
                tool_choice=projection.tool_choice if frozen.native_tools else None,
                messages=projection.messages, request_options=ProviderRequestOptions(
                    system_instruction=projection.system_instruction,
                    thinking_disabled=bool(frozen.native_tools) and projection.tool_choice.mode != "auto",
                ),
            )
            assert wire == expected

    business, probes = _http(monkeypatch, backend=backend, on_business=on_business)
    result = agent.run_subagent(task.id, dry_run=False, probe=False)
    assert result.ok
    assert len(business) == 3 and len(candidates) == 1
    assert generations == [0, 0, 1]
    assert len(prepares) == 2
    assert len(set(sent_attempts)) == 1 and sent_attempts[0]
    assert candidates[0].params.run_id == task.id
    assert candidates[0].params.attempt_id == sent_attempts[0]
    assert bool(probes) is tools
    assert len(recovery_instances) == 1 and recovery_instances[0].committed
    thread = agent.conversation_store.threads.require(task.agent_thread_id)
    assert thread.compact_generation == 1
    current = agent.subagents.load(task.id)
    assert current.id == task.id and current.runner_attempts == result.runner_attempts == 1


@pytest.mark.parametrize("failure", ["cancel_token", "generation", "summary_transient"])
def test_child_uncommitted_recovery_never_sends_restored_business(tmp_path, monkeypatch, failure):
    agent, task = _child(tmp_path, backend="anthropic_compatible", tools=False)
    recoveries, waits = [], []
    original_recovery = compact_recovery.prepare_subagent_compact_recovery
    original_project = compact_recovery._project_subagent_candidate
    monkeypatch.setattr(provider_transient_auto_resume, "_wait_before_retry", lambda *_: waits.append(True))

    def recovery(*args, **kwargs):
        value = original_recovery(*args, **kwargs)
        recoveries.append(value)
        return value

    def project(*args):
        if args[-1].is_candidate:
            if failure == "cancel_token":
                recoveries[0].render_params.cancellation_token.cancel("只取消当前运行令牌")
            elif failure == "generation":
                agent.conversation_store.threads.update_atomic(task.agent_thread_id, lambda thread: replace(
                    thread, compact_generation=thread.compact_generation + 1, summary="并发提交已获胜",
                ))
        return original_project(*args)

    if failure == "summary_transient":
        def summary(*_args, **_kwargs):
            raise ProviderTransientError("503 temporary overload")
        monkeypatch.setattr(compact, "_summarize", summary)
    monkeypatch.setattr(compact_recovery, "prepare_subagent_compact_recovery", recovery)
    monkeypatch.setattr(compact_recovery, "_project_subagent_candidate", project)

    def on_business(_wire, number):
        if number == 1:
            raise ProviderContextWindowError("测试供应商上下文溢出")

    business, _ = _http(monkeypatch, backend="anthropic_compatible", on_business=on_business)
    result = agent.run_subagent(task.id, dry_run=False, probe=False)
    assert not result.ok
    assert len(business) == (1 if failure == "summary_transient" else 2)
    assert len(recoveries) == 1 and not recoveries[0].committed
    assert waits == []
    thread = agent.conversation_store.threads.require(task.agent_thread_id)
    assert thread.compact_generation == (1 if failure == "generation" else 0)
    if failure == "generation":
        assert thread.summary == "并发提交已获胜"


# LLM: 来源读取失败要沿真实 runner 退出；不能把损坏来源当空历史后继续发业务。
# 函数用途: 验证延迟来源加载失败时没有摘要提交、没有恢复请求。
def test_child_deferred_source_failure_is_not_treated_as_empty_history(tmp_path, monkeypatch):
    agent, task = _child(tmp_path, backend="anthropic_compatible", tools=False)
    original_load = compact.load_conversation_compact_source
    after_overflow, failed_loads = [False], []

    def load(*args, **kwargs):
        if after_overflow[0]:
            failed_loads.append(True)
            raise OSError("损坏的子代理历史来源")
        return original_load(*args, **kwargs)

    monkeypatch.setattr(compact, "load_conversation_compact_source", load)

    def on_business(_wire, number):
        if number == 1:
            after_overflow[0] = True
            raise ProviderContextWindowError("测试供应商上下文溢出")

    business, _ = _http(monkeypatch, backend="anthropic_compatible", on_business=on_business)
    result = agent.run_subagent(task.id, dry_run=False, probe=False)
    assert not result.ok
    assert len(business) == 1 and failed_loads
    assert agent.conversation_store.threads.require(task.agent_thread_id).compact_generation == 0
