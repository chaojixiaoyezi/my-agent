"""compact 连续失败熔断的钉子测试。"""

from __future__ import annotations

from pathlib import Path

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
