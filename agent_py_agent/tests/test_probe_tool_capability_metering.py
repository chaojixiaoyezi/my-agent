"""C7 计量修正：工具能力探测计入 model_usage（独立用途标签 probe:tool_capability）。

零网络：真实 OpenAI 兼容后端，只替换 request_json。锁定：
- select_tool_protocol 触发真实探测时，每次 generate 都记入模型调用账：is_probe=True、
  purpose=probe:tool_capability、归入请求/运行累计 scope，成功用供应商 usage 收尾；
- 探测缓存命中不再发请求、不重复记账；没有宿主 scope 的直调保持旧行为（不记账）；
- 探测请求失败也记 failed，不把成本漏出用量账。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.native_tool_protocol import select_tool_protocol
from agent_py_agent.agent.agent_core.runtime.context_compactor import RuntimeCompactPolicy
from agent_py_agent.agent.backends import get_backend
from agent_py_agent.agent.backends.errors import ProviderRecoverableError
from agent_py_agent.agent.backends.http import (
    reset_unaccounted_probe_attempt_count,
    unaccounted_probe_attempt_count,
)
from agent_py_agent.agent.backends.provider_headers import (
    ProbeAccountingScope,
    current_probe_accounting,
    probe_accounting_scope,
)
from agent_py_agent.agent.backends.vision_capability import reset_vision_capability_cache
from agent_py_agent.agent.contracts.model_call_ledger import ModelCallLedger, model_call_purpose
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation import compact as compact_module
from agent_py_agent.agent.conversation.compact import _CompactRunRequest
from agent_py_agent.agent.conversation.compact_media_policy import CompactMediaDecision
from agent_py_agent.agent.conversation.native_history import CANONICAL_NATIVE_MESSAGES_METADATA_KEY

_API = "https://relay.example.test/v1"
_KEY = "<redacted>"


# 函数用途: 构造一个真实 OpenAI 兼容后端的最小配置（窗口 128000 → 输出上限 32000，非流式）。
def _backend_config() -> SimpleNamespace:
    return SimpleNamespace(
        model_backend="openai_compatible",
        model_name="relay-model",
        api_base=_API,
        api_key=_KEY,
        request_timeout=240,
        model_context_window_tokens=128000,
        stream_enabled=False,
        anthropic_prompt_cache_enabled=True,
        model_reasoning_control="auto",
        model_reasoning_levels=(),
        model_structured_output="auto",
        model_custom_headers={},
        model_session_header="",
        model_auth_ref={},
        input_media_max_bytes=16 * 1024 * 1024,
        top_p=None,
        temperature=None,
    )


class _ProbeWire:
    """按探测提示里的 nonce 回放一次原生工具调用（native=False 时回放不支持），记录每次出站载荷。"""

    # 函数用途: 准备假传输；fail 为真时每次请求都抛供应商可恢复错误，native=False 时回放无工具调用。
    def __init__(self, *, fail: bool = False, native: bool = True) -> None:
        self.payloads: list[dict] = []
        self.fail = fail
        self.native = native

    # 函数用途: 记录载荷并按 nonce 返回带 my_agent_capability_probe 工具调用（或空响应）的回复。
    def request_json(self, path, payload, headers):
        self.payloads.append(dict(payload))
        if self.fail:
            raise ProviderRecoverableError("probe transport failed")
        prompt = str(payload["messages"][-1]["content"])
        nonce = prompt.split("with nonce ", 1)[1].split(".", 1)[0].strip()
        if not self.native:
            return {"choices": [{"message": {"role": "assistant", "content": "no tool call"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 30, "completion_tokens": 5, "total_tokens": 35}}
        return {
            "choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call-probe-1", "type": "function",
                 "function": {"name": "my_agent_capability_probe", "arguments": json.dumps({"nonce": nonce})},
                 }]}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42},
        }


# 函数用途: 建一个带真实后端与空账本的宿主代理（enable_tools 打开才走探测）。
def _agent(wire: _ProbeWire, monkeypatch) -> SimpleNamespace:
    backend = get_backend("openai_compatible", _backend_config())
    monkeypatch.setattr(backend, "request_json", wire.request_json)
    ledger = ModelCallLedger()
    agent = SimpleNamespace(
        config=SimpleNamespace(enable_tools=True),
        backend=backend,
        _model_call_ledger=ledger,
    )
    return agent


def test_probe_calls_are_accounted_with_dedicated_purpose(monkeypatch):
    wire = _ProbeWire()
    agent = _agent(wire, monkeypatch)
    snapshot = select_tool_protocol(agent, run_id="run-1", request_id="req-1")
    assert snapshot.source_protocol == "native" and len(wire.payloads) == 1

    ledger: ModelCallLedger = agent._model_call_ledger
    records = ledger.records()
    assert len(records) == 1
    record = records[0]
    assert record.is_probe is True
    assert record.metadata.get("purpose") == "probe:tool_capability"
    assert model_call_purpose(record) == "probe:tool_capability"
    assert (record.request_id, record.run_id) == ("req-1", "run-1")
    assert record.status == "finished"
    assert record.output_tokens == 12  # 供应商 usage 的 completion_tokens

    summary = ledger.cumulative_summary(request_id="req-1")
    probe_bucket = summary["purpose_breakdown"]["probe:tool_capability"]
    assert probe_bucket["logical_model_turn_count"] == 1
    assert probe_bucket["physical_model_attempt_count"] == 1
    assert probe_bucket["output_tokens"] == 12
    assert summary["physical_model_attempt_count"] == 1  # 探测计入总账，只是按用途分开统计


def test_probe_cache_hit_does_not_duplicate_accounting(monkeypatch):
    wire = _ProbeWire()
    agent = _agent(wire, monkeypatch)
    select_tool_protocol(agent, run_id="run-1", request_id="req-1")
    select_tool_protocol(agent, run_id="run-2", request_id="req-2")  # 同一 backend 缓存命中，不再发请求
    assert len(wire.payloads) == 1
    assert len(agent._model_call_ledger.records()) == 1


def test_direct_probe_without_scope_is_not_accounted(monkeypatch):
    wire = _ProbeWire()
    agent = _agent(wire, monkeypatch)
    select_tool_protocol(agent, run_id="run-1", request_id="req-1")
    assert len(agent._model_call_ledger.records()) == 1
    # 直调 backend 探测（测试或旧路径）没有宿主 scope：真实请求仍发生，但不记账，行为与改动前一致。
    backend2 = get_backend("openai_compatible", _backend_config())
    monkeypatch.setattr(backend2, "request_json", wire.request_json)
    capability = backend2.probe_tool_capability()
    assert capability.native_supported is True
    assert len(agent._model_call_ledger.records()) == 1


def test_probe_failure_is_accounted_as_failed(monkeypatch):
    wire = _ProbeWire(fail=True)
    agent = _agent(wire, monkeypatch)
    # 探测的可恢复错误沿原合同重抛（select_tool_protocol 只把“探测完成但不支持”转成 ToolProtocolSelectionError）。
    with pytest.raises(ProviderRecoverableError):
        select_tool_protocol(agent, run_id="run-1", request_id="req-1")
    record = agent._model_call_ledger.records()[0]
    assert record.status == "failed" and record.error_type == "ProviderRecoverableError"
    assert model_call_purpose(record) == "probe:tool_capability"


def test_compact_vision_resolution_probe_is_accounted(monkeypatch):
    # 压缩入口绑定记账 scope 后，resolve_vision_candidate 触发的工具能力探测进 probe 桶；
    # 探针未证明原生工具支持时视觉探针不发（本轮范围），决策回落到归档引用。
    reset_vision_capability_cache()
    try:
        wire = _ProbeWire(native=False)
        agent = _agent(wire, monkeypatch)
        scope = ProbeAccountingScope(agent._model_call_ledger, request_id="req-compact", run_id="run-compact")
        decision = CompactMediaDecision("archived_refs", "vision_fact_pending", vision_candidate=True)
        with probe_accounting_scope(scope):
            resolved = compact_module.resolve_vision_candidate(agent, decision)
        assert resolved.policy == "archived_refs" and resolved.fact_source == "probe_unavailable"
        records = agent._model_call_ledger.records()
        # 工具能力探针有界重试 3 次（不支持结论不缓存），每次真实 generate 都记账
        assert len(records) == 3
        assert all(model_call_purpose(record) == "probe:tool_capability" for record in records)
        assert all((record.request_id, record.run_id) == ("req-compact", "run-compact") for record in records)
        assert all(record.status == "finished" for record in records)
    finally:
        reset_vision_capability_cache()


def test_compact_build_binds_probe_accounting_scope(monkeypatch, tmp_path):
    # 压缩候选构造（_build_compact_candidate）在范围内确有媒体块时，把 resolve_vision_candidate
    # 包在带 request_id/run_id 的记账 scope 里；spy 确认调用时刻 scope 已绑定且账本一致。
    wire = _ProbeWire()
    agent = _agent(wire, monkeypatch)
    store = ConversationStore(Path(tmp_path) / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "compact-probe"})
    image = {"type": "image", "source": {"type": "local_file", "path": "/owner/attachments/x", "sha256": "b" * 64,
                                         "media_type": "image/png", "size_bytes": 321, "name": "chart.png"}}
    envelope = {"schema": "conversation_native_messages.v1", "messages": [
        {"role": "user", "content": [image]}]}
    rows = [SimpleNamespace(role="user", metadata={CANONICAL_NATIVE_MESSAGES_METADATA_KEY: envelope})]

    seen: dict[str, object] = {}

    def spy(agent_arg, decision):
        seen["scope"] = current_probe_accounting()
        return CompactMediaDecision("archived_refs", "test")

    monkeypatch.setattr(compact_module, "resolve_vision_candidate", spy)
    monkeypatch.setattr(compact_module, "_candidate_limits", lambda request: SimpleNamespace(target=1, ceiling=10 ** 9))
    monkeypatch.setattr(compact_module, "_summarize", lambda *args, **kwargs: "s")
    monkeypatch.setattr(compact_module, "_measure_candidate",
                        lambda request, tail, summary: compact_module._MeasuredSummary(summary, None, 100))
    monkeypatch.setattr(compact_module, "_fit_landmarks_to_target",
                        lambda limits, measured, landmark_outcome, remeasure: measured)

    policy = RuntimeCompactPolicy(
        context_window_tokens=128000, trigger_percent=80, trigger_tokens=102400,
        recovery_target_percent=60, recovery_target_tokens=76800, allow_persistent_apply=True,
        recent_tail_max_turns=4, recent_tail_tokens=20000, failure_threshold=3, failure_cooldown_seconds=60,
    )
    request = _CompactRunRequest(
        agent=agent, store=store, thread=thread, current_prompt="", pending=rows,
        policy=policy, projected_tokens=0, forced=False, attempted_at=0.0, operation_id="op-1",
        request_id="req-compact", run_id="run-compact",
    )
    decision = CompactMediaDecision("archived_refs", "vision_fact_pending", vision_candidate=True)
    compact_module._build_compact_candidate(
        request, rows, [], media_decision=decision, provider_surface=None,
    )

    scope = seen["scope"]
    assert isinstance(scope, ProbeAccountingScope)
    assert (scope.request_id, scope.run_id) == ("req-compact", "run-compact")
    assert scope.ledger is agent._model_call_ledger


def test_unaccounted_probe_attempt_is_counted(monkeypatch):
    # 没有绑定记账范围的真实探测尝试留结构化计数（诊断入口只读），绑定 scope 的探测不计入。
    reset_unaccounted_probe_attempt_count()
    wire = _ProbeWire()
    backend2 = get_backend("openai_compatible", _backend_config())
    monkeypatch.setattr(backend2, "request_json", wire.request_json)
    assert backend2.probe_tool_capability().native_supported is True
    assert unaccounted_probe_attempt_count() == 1
    reset_unaccounted_probe_attempt_count()
    agent = _agent(wire, monkeypatch)
    select_tool_protocol(agent, run_id="run-1", request_id="req-1")
    assert unaccounted_probe_attempt_count() == 0, "绑定记账 scope 的探测不计入未绑定计数"
    assert len(agent._model_call_ledger.records()) == 1


# LLM: 两项进程内计数的线上出口是 GET /status 的 usage_accounting 段（http_handlers._usage_accounting_diagnostics）；
#   这里走真实 handle_status（假 handler 只收 JSON），计数来自真实的无 scope 探测与真实的开放世界读法，不直接塞数。
# 函数用途: /status 能看到没绑记账范围的探测次数和用量账里读到的未知用途键。
def test_status_endpoint_projects_usage_accounting_counters(monkeypatch, tmp_path):
    from agent_py_agent.agent.conversation.store_usage import (
        _PURPOSE_SCHEMA,
        _sum_purpose_breakdowns,
        reset_unknown_purpose_key_counts,
    )
    from agent_py_agent.agent.gateway_parts.http_handlers import handle_status
    from agent_py_agent.tests.test_gateway_admission_wait import _paths

    reset_unaccounted_probe_attempt_count()
    reset_unknown_purpose_key_counts()
    try:
        wire = _ProbeWire()
        backend2 = get_backend("openai_compatible", _backend_config())
        monkeypatch.setattr(backend2, "request_json", wire.request_json)
        assert backend2.probe_tool_capability().native_supported is True  # 无 scope 直调：真实探测、不记账
        _sum_purpose_breakdowns([{"purpose_breakdown": {"schema": _PURPOSE_SCHEMA, "probe:tool_capabilty": {}}}])
        paths = _paths(tmp_path)
        paths.root.mkdir(parents=True, exist_ok=True)
        paths.state.write_text(
            json.dumps({"status": "running", "pid": os.getpid(), "started_at": time.time()}), encoding="utf-8"
        )
        sent: list[tuple[int, dict]] = []
        handle_status(
            SimpleNamespace(_send_json=lambda status, body: sent.append((status, body))),
            SimpleNamespace(paths=paths),
        )
        assert sent[0][0] == 200 and sent[0][1]["status"] == "running"
        assert sent[0][1]["usage_accounting"] == {
            "unaccounted_probe_attempt_count": 1,
            "unknown_purpose_keys": {"keys": {"probe:tool_capabilty": 1}, "overflow_count": 0},
        }
    finally:
        reset_unaccounted_probe_attempt_count()
        reset_unknown_purpose_key_counts()
