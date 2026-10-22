"""compact 连续失败熔断的钉子测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.memory_archive.compact_circuit_breaker import (
    DEFAULT_COMPACT_COOLDOWN_SECONDS,
    compact_circuit_blocker_payload,
    compact_circuit_open,
    read_compact_circuit,
    record_compact_outcome,
)


def test_closed_until_threshold(tmp_path: Path) -> None:
    record_compact_outcome(tmp_path, ok=False, status="blocked_after_action_guard", now=1000.0)
    state = record_compact_outcome(tmp_path, ok=False, status="blocked_after_action_guard", now=1001.0)
    assert state.consecutive_failures == 2
    assert not compact_circuit_open(state, now=1001.0)


def test_opens_at_threshold(tmp_path: Path) -> None:
    for i in range(3):
        record_compact_outcome(tmp_path, ok=False, status="blocked_self_check_failed", now=1000.0 + i)
    state = read_compact_circuit(tmp_path)
    assert state.consecutive_failures == 3
    assert compact_circuit_open(state, now=1002.0)


def test_cooldown_reopens_then_half_open(tmp_path: Path) -> None:
    for i in range(3):
        record_compact_outcome(tmp_path, ok=False, status="blocked_after_action_guard", now=1000.0 + i)
    state = read_compact_circuit(tmp_path)
    # 冷却期内 open
    assert compact_circuit_open(state, now=1002.0 + DEFAULT_COMPACT_COOLDOWN_SECONDS - 1)
    # 冷却过后 half-open 放行重试
    assert not compact_circuit_open(state, now=1002.0 + DEFAULT_COMPACT_COOLDOWN_SECONDS + 1)


def test_success_resets(tmp_path: Path) -> None:
    for i in range(3):
        record_compact_outcome(tmp_path, ok=False, status="blocked_after_action_guard", now=1000.0 + i)
    state = record_compact_outcome(tmp_path, ok=True, status="ready_after_action_guard", now=2000.0)
    assert state.consecutive_failures == 0
    assert not compact_circuit_open(state, now=2000.0)


def test_threshold_zero_never_opens(tmp_path: Path) -> None:
    for i in range(10):
        record_compact_outcome(tmp_path, ok=False, status="blocked_after_action_guard", now=1000.0 + i)
    state = read_compact_circuit(tmp_path)
    assert not compact_circuit_open(state, now=1010.0, threshold=0)


def test_blocker_payload_carries_code(tmp_path: Path) -> None:
    for i in range(3):
        record_compact_outcome(tmp_path, ok=False, status="blocked_self_check_failed", now=1000.0 + i)
    payload = compact_circuit_blocker_payload(read_compact_circuit(tmp_path))
    assert payload["code"] == "COMPACT_CIRCUIT_OPEN"
    assert payload["consecutive_failures"] == 3


def test_auto_cycle_skips_when_circuit_open(tmp_path: Path) -> None:
    """集成：熔断 open 时 run_memory_compact_auto_cycle 不 apply，返回 blocked_circuit_open。"""
    from agent_py_agent.agent.memory_archive.compact import MemoryCompactPlanOptions
    from agent_py_agent.agent.memory_archive.compact_auto import (
        MemoryCompactAutoCycleOptions,
        run_memory_compact_auto_cycle,
    )

    # 预先把熔断打到 open
    for i in range(3):
        record_compact_outcome(tmp_path, ok=False, status="blocked_after_action_guard", now=1000.0 + i)

    cycle = run_memory_compact_auto_cycle(
        tmp_path,
        MemoryCompactAutoCycleOptions(
            current_tokens=10_000_000,  # 远超阈值，确保 should_prompt
            max_context_tokens=200_000,
            plan_options=MemoryCompactPlanOptions(session_id="s", request_id="", run_id="", task_id=""),
            trigger_percent=70,
            allow_apply=True,
            now=1002.5,  # 注入熔断时间基准，落在 opened_at=1002 的冷却期内
        ),
    )
    assert cycle["status"] == "blocked_circuit_open"
    assert cycle["ok"] is False
    assert cycle["apply_id"] == ""


# LLM: 仅用真实临时会话、失败账和摘要后端；显式恢复绕过冷却但仍走原checkpoint/CAS及成功清零。
# 函数用途: 验证熔断期间默认强制压缩仍被拒，显式手动恢复可成功并重置熔断。
def test_p2a_explicit_compact_bypasses_and_resets_circuit(tmp_path):
    import time

    from agent_py_agent.agent.conversation.compact import (
        ConversationCompactOptions,
        prepare_conversation_context,
    )
    from agent_py_agent.agent.conversation.compact_guard import ConversationCompactCircuitOpenError
    from agent_py_agent.agent.gateway_parts.request_history import (
        append_gateway_conversation_message,
    )
    from agent_py_agent.tests.test_gateway_conversation_compact import (
        _agent,
        _context,
        _request,
        _SummaryBackend,
    )

    agent = _agent(tmp_path, context_tokens=1_000_000)
    agent.backend = _SummaryBackend()
    context = _context(agent, _request(), "create", "开始")
    for role in ("user", "assistant"):
        assert append_gateway_conversation_message(agent, {}, context, request_id=f"old-{role}", role=role, content="原始资料")
    for _ in range(3):
        agent.conversation_store.threads.record_compact_failure(
            context.thread_id, failure_code="COMPACT_TEST_FAILURE", expected_generation=0, now=time.time())
    thread = agent.conversation_store.threads.require(context.thread_id)
    with pytest.raises(ConversationCompactCircuitOpenError):
        prepare_conversation_context(agent, agent.conversation_store, thread, options=ConversationCompactOptions(force=True))
    options = ConversationCompactOptions(force=True, bypass_circuit_breaker=True)
    result = prepare_conversation_context(agent, agent.conversation_store, thread, options=options)
    assert result.compacted and result.thread.compact_generation == 1
    stored = agent.conversation_store.threads.require(context.thread_id)
    assert stored.compact_consecutive_failures == 0 and stored.compact_failure_updated_at == 0
    assert stored.compact_failure_code == ""


# LLM: 固定完整投影估算，只测试阈值/物理窗口边界；熔断与时钟使用真实guard，不能以90%触发线代替容量上限。
# 函数用途: 验证熔断open时触发线以上但装得下的自动请求跳过摘要，等于或超过窗口仍不放行。
@pytest.mark.parametrize(("tokens", "expected"), [(899, True), (900, True), (999, True), (1000, False), (1001, False)])
def test_p2a_automatic_open_circuit_noop_respects_window(monkeypatch, tokens, expected):
    import time

    from agent_py_agent.agent.agent_core import compact_request_recovery as recovery

    policy = SimpleNamespace(trigger_tokens=900, context_window_tokens=1000, failure_threshold=3, failure_cooldown_seconds=600)
    thread = SimpleNamespace(compact_consecutive_failures=3, compact_failure_updated_at=time.time())
    source = SimpleNamespace(policy=policy, thread=thread, messages=("history",))
    host = SimpleNamespace(agent=SimpleNamespace(config=SimpleNamespace()), source=source, calibration=None)
    monkeypatch.setattr(recovery, "project_tool_loop_request", lambda _: SimpleNamespace(status="ready"))
    monkeypatch.setattr(recovery, "compact_request_source_supported", lambda *a, **k: True)
    monkeypatch.setattr(recovery, "projected_model_context_components", lambda *a, **k: (tokens, {}))
    assert recovery._automatic_recovery_noop(host, object(), None) is expected
