from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_py_agent.agent.agent_core.orchestration.dispatch.params import DispatchContext
from agent_py_agent.agent.agent_core.orchestration.dispatch.runner_batches import (
    _runner_candidates_for_context,
)


def test_explicit_packet_recovery_allows_blocked_runner_candidate():
    task = SimpleNamespace(
        id="child-blocked",
        parent_id="root",
        root_id="root",
        status="BLOCKED",
        verification_status="FAILED",
        channel_status="OK",
        failure_type="",
        runner_attempts=0,
        capability_requests=[],
        capability_gaps=[],
    )
    ctx = DispatchContext(
        cfg=MagicMock(),
        planner=False,
        runner_instruction="先读取 latest_continue_packet.json，再按 packet 继续。",
        recovery_mode="rerun_from_continue_packet",
        max_runners=1,
        limit=20,
        reviewer="tester",
        note="",
        take_over_by="",
        locked_files=None,
        router=MagicMock(),
        parent_run_id="root",
        root_id="root",
        include_run_ids=["child-blocked"],
    )

    selected = _runner_candidates_for_context([task], ctx, runner_max_attempts=1)

    assert [item.id for item in selected] == ["child-blocked"]


def test_checkpoint_words_in_runner_instruction_do_not_enable_recovery_candidate():
    task = SimpleNamespace(
        id="child-blocked",
        parent_id="root",
        root_id="root",
        status="BLOCKED",
        verification_status="FAILED",
        channel_status="OK",
        failure_type="",
        runner_attempts=0,
        capability_requests=[],
        capability_gaps=[],
    )
    ctx = DispatchContext(
        cfg=MagicMock(),
        planner=False,
        runner_instruction="这句只是普通补充，里面提到 latest_continue_packet.json。",
        max_runners=1,
        limit=20,
        reviewer="tester",
        note="",
        take_over_by="",
        locked_files=None,
        router=MagicMock(),
        parent_run_id="root",
        root_id="root",
        include_run_ids=["child-blocked"],
    )

    selected = _runner_candidates_for_context([task], ctx, runner_max_attempts=1)

    assert selected == []


def test_explicit_packet_recovery_skips_takeover_chain_exhausted_task():
    task = SimpleNamespace(
        id="child-terminal",
        parent_id="root",
        root_id="root",
        status="BLOCKED",
        verification_status="FAILED",
        channel_status="OK",
        failure_type="takeover_chain_exhausted",
        current_step="takeover chain exhausted; waiting for parent decision",
        result="",
        runner_attempts=0,
        capability_requests=[],
        capability_gaps=[],
    )
    ctx = DispatchContext(
        cfg=MagicMock(),
        planner=False,
        runner_instruction="先读取 latest_continue_packet.json，再按 packet 继续。",
        recovery_mode="rerun_from_continue_packet",
        max_runners=1,
        limit=20,
        reviewer="tester",
        note="",
        take_over_by="",
        locked_files=None,
        router=MagicMock(),
        parent_run_id="root",
        root_id="root",
        include_run_ids=["child-terminal"],
    )

    selected = _runner_candidates_for_context([task], ctx, runner_max_attempts=1)

    assert selected == []
