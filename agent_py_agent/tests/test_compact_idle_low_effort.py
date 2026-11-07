"""闲置维护压缩（不带工具、不复用主对话前缀）自动用低档的端到端回归；主请求档位不受影响。"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.gateway_parts import compact_operations as idle
from agent_py_agent.tests.test_cache_prefix_regression import _environment, _observe_compact_requests


# LLM: 真实 Gateway ask 两轮后走真实闲置维护入口；只观察出站载荷的档位与工具数，不读正文。
# 函数用途: 钉住闲置摘要按 memory_compact_reasoning_level 降到 low，而线程主请求仍是 max（缓存分区不被连带改变）。
def test_idle_maintenance_summary_uses_low_effort(tmp_path, monkeypatch):
    chain, simulator, scenario = _environment(tmp_path, monkeypatch)
    _requests, ranges = _observe_compact_requests(monkeypatch, simulator)
    chain.ask("SIM", "SIM-COMPACT-SEED\n" + ("stable prior conversation text " * 3_000))
    chain.ask("SIM", "SIM-TURN1 第一条：看一下工作区。")
    main_payload = scenario.payloads[-1]
    store = chain.agent.conversation_store
    row = store.threads.record_user_activity(chain.threads["SIM"], now=100.0)
    monkeypatch.setattr(idle, "inspect_conversation_context",
                        lambda *_: SimpleNamespace(projected_tokens=650000, context_window_tokens=1000000))

    result = idle.maybe_auto_compact_idle_session(chain.agent, store, row, now=7400.0)

    assert result["compacted"], result
    assert ranges and ranges[0][2] > ranges[0][1], "闲置维护没有发出摘要请求"
    summary = scenario.payloads[ranges[0][1]]
    assert not summary.get("tools")
    assert main_payload.get("reasoning_effort") == "max"
    assert summary.get("reasoning_effort") == "low"
