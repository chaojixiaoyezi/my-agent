"""C7 计量修正：工具能力探测计入 model_usage（独立用途标签 probe:tool_capability）。

零网络：真实 OpenAI 兼容后端，只替换 request_json。锁定：
- select_tool_protocol 触发真实探测时，每次 generate 都记入模型调用账：is_probe=True、
  purpose=probe:tool_capability、归入请求/运行累计 scope，成功用供应商 usage 收尾；
- 探测缓存命中不再发请求、不重复记账；没有宿主 scope 的直调保持旧行为（不记账）；
- 探测请求失败也记 failed，不把成本漏出用量账。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.native_tool_protocol import select_tool_protocol
from agent_py_agent.agent.backends import get_backend
from agent_py_agent.agent.backends.errors import ProviderRecoverableError
from agent_py_agent.agent.contracts.model_call_ledger import ModelCallLedger, model_call_purpose

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
    """按探测提示里的 nonce 回放一次成功的原生工具调用，记录每次出站载荷。"""

    # 函数用途: 准备假传输；fail 为真时每次请求都抛供应商可恢复错误。
    def __init__(self, *, fail: bool = False) -> None:
        self.payloads: list[dict] = []
        self.fail = fail

    # 函数用途: 记录载荷并按 nonce 返回带 my_agent_capability_probe 工具调用的响应。
    def request_json(self, path, payload, headers):
        self.payloads.append(dict(payload))
        if self.fail:
            raise ProviderRecoverableError("probe transport failed")
        prompt = str(payload["messages"][-1]["content"])
        nonce = prompt.split("with nonce ", 1)[1].split(".", 1)[0].strip()
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