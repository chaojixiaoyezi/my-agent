"""CLI 自动续跑契约层单测（2026-08-14 根因3 设计 v2）。

覆盖审查硬条件1：root_task_id/root_run_id/thread_id/continuation_seq/
parent_attempt_id 必须贯穿 params、conversation binding、attempt 与
ledger——仅 resume_context=True 或同 thread 不算通过。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.cli_run_conversation import (
    bind_cli_run_conversation,
)
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.cli.resume_contract import (
    CliContinuationContext,
    continuation_from_params,
    resume_prompt_for,
    try_claim_cli_resume,
)


def _params(**overrides) -> RunParams:
    base = dict(
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        source="cli_run",
        task_attributes={},
    )
    base.update(overrides)
    return RunParams(**base)


def test_continuation_context_derives_request_id_per_round():
    """每轮 request_id 派生 {root}#cont-{seq}，幂等键唯一且可溯根。"""
    ctx = CliContinuationContext(
        root_task_id="task-1", root_run_id="run-1", root_thread_id="thread-1"
    )
    assert ctx.request_id_for(1) == "run-1#cont-1"
    assert ctx.request_id_for(2) == "run-1#cont-2"
    assert ctx.request_id_for(1) != ctx.request_id_for(2)


def test_continuation_context_apply_preserves_root_ids():
    """apply_to 覆写 request/run/task 为根 ID，不新建根。"""
    ctx = CliContinuationContext(
        root_task_id="task-1", root_run_id="run-1", root_thread_id="thread-1"
    )
    p = _params(request_id="other", run_id="other-run", task_id="other-task")
    p2 = ctx.apply_to(p, seq=1, attempt_id="attempt-2")
    assert p2.request_id == "run-1#cont-1"
    assert p2.run_id == "run-1"
    assert p2.task_id == "task-1"
    assert p2.attempt_id == "attempt-2"
    assert p2.continuation_seq == 1
    assert p2.continuation_root_task_id == "task-1"
    assert p2.continuation_root_run_id == "run-1"
    assert p2.continuation_root_thread_id == "thread-1"


def test_continuation_context_next_advances_seq_and_parent():
    """next() 推进 seq 并把 parent_attempt 指向刚结束的轮。"""
    ctx = CliContinuationContext(
        root_task_id="task-1", root_run_id="run-1", root_thread_id="thread-1",
        continuation_seq=0,
    )
    nxt = ctx.next(attempt_id="attempt-1")
    assert nxt.continuation_seq == 1
    assert nxt.parent_attempt_id == "attempt-1"
    nxt2 = nxt.next(attempt_id="attempt-2")
    assert nxt2.continuation_seq == 2
    assert nxt2.parent_attempt_id == "attempt-2"
    # 根 ID 不变
    assert nxt2.root_task_id == "task-1"
    assert nxt2.root_run_id == "run-1"


def test_continuation_from_params_roundtrip():
    """RunParams → 契约 → apply_to 还原同一链路。"""
    ctx = CliContinuationContext(
        root_task_id="task-1", root_run_id="run-1", root_thread_id="thread-1",
        continuation_seq=2, parent_attempt_id="attempt-2",
    )
    p = _params()
    p2 = ctx.apply_to(p, seq=2, attempt_id="attempt-3")
    restored = continuation_from_params(p2)
    assert restored is not None
    assert restored.root_task_id == "task-1"
    assert restored.root_run_id == "run-1"
    assert restored.root_thread_id == "thread-1"
    assert restored.continuation_seq == 2
    assert restored.parent_attempt_id == "attempt-2"


def test_continuation_from_params_none_when_no_contract():
    """无续跑契约字段的 params 返回 None（首轮/非续跑）。"""
    assert continuation_from_params(_params()) is None


def test_resume_prompt_no_acceptance_semantics():
    """续跑提示只引用结构化 reason，不得含验收词（审查意见4）。"""
    prompt = resume_prompt_for(
        continuation_reason="TOOL_ROUND_LIMIT_REACHED",
        continuation_seq=1,
        user_task="把项目换成 Go 重写",
    )
    assert "TOOL_ROUND_LIMIT_REACHED" in prompt
    assert "续跑" in prompt
    # 禁验收语义词
    for banned in ("代码规模", "测试达标", "达标", "通过验收"):
        assert banned not in prompt


def test_bind_cli_run_conversation_continuation_reuses_thread(tmp_path):
    """续跑轮复用同一 conversation thread（root channel id），消息带标记。"""
    from agent_py_agent.agent.conversation.store import ConversationStore

    store = ConversationStore(tmp_path / "conv")
    agent = SimpleNamespace(
        conversation_store=store,
        root="/tmp",
        config=SimpleNamespace(),
        runtime_guard_policy=None,
    )

    # 首轮
    p0 = _params(request_id="req-1", run_id="run-1", task_id="task-1")
    bound0 = bind_cli_run_conversation(agent, p0, "用户任务")
    thread0 = bound0.task_attributes["conversation_thread_id"]

    # 续跑轮（request_id=run-1#cont-1 但复用 root channel=req-1）
    ctx = CliContinuationContext(
        root_task_id="task-1", root_run_id="run-1", root_thread_id=thread0,
        root_request_id="req-1",
    )
    p1 = ctx.apply_to(_params(request_id="req-1"), seq=1, attempt_id="attempt-2")
    bound1 = bind_cli_run_conversation(agent, p1, "续跑提示")
    assert bound1.task_attributes["conversation_thread_id"] == thread0

    # 同一 thread 两条消息，续跑消息带 is_continuation
    messages = store.recent_messages(thread0, limit=10)
    assert len(messages) == 2
    cont_msgs = [
        m for m in messages if (m.metadata or {}).get("is_continuation")
    ]
    assert len(cont_msgs) == 1
    assert cont_msgs[0].metadata["continuation_seq"] == 1


# ------------------------------------------------- 切片2: 共享 gate + claim 互斥


def test_should_continue_task_truth_table():
    """可续跑族与不可续跑族 truth table（审查意见6）。"""
    from agent_py_agent.agent.conversation.runtime import should_continue_task

    continuable = [
        ("TOOL_ROUND_LIMIT_REACHED", "tool_loop", "unfinished"),
        ("TASK_PROGRESS_OPEN", "tool_loop", "unfinished"),
        ("REPEATED_TOOL_FAILURE", "tool_loop", "unfinished"),
        ("REQUIRED_ACTION_HAS_NO_EVIDENCE", "required_action_completion_gate", "unfinished"),
    ]
    not_continuable = [
        ("PROTOCOL_VIOLATION", "tool_protocol_adapter", "blocked"),
        ("TOOL_OPERATION_OUTCOME_UNKNOWN", "tool_loop", "unfinished"),
        ("BLOCKED", "x", "blocked"),
        ("", "x", "ok"),
        ("REQUIRED_ACTION_HAS_NO_EVIDENCE", "other_source", "unfinished"),
    ]
    for reason, source, status in continuable:
        resp = SimpleNamespace(runtime_reason=reason, runtime_source=source, runtime_status=status)
        assert should_continue_task(resp)[0] is True, (reason, source, status)
    for reason, source, status in not_continuable:
        resp = SimpleNamespace(runtime_reason=reason, runtime_source=source, runtime_status=status)
        assert should_continue_task(resp)[0] is False, (reason, source, status)


def test_try_claim_cli_resume_mutual_exclusion(tmp_path):
    """CLI claim 互斥：已 claim 且 lease 未过期 → 跳过；过期后可重 claim。"""
    import time

    from agent_py_agent.agent.conversation.store import ConversationStore

    store = ConversationStore(tmp_path / "conv")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "u",
            "channel": "cli_run",
            "channel_conversation_id": "req-1",
            "channel_user_id": "u",
            "owner_id": "local/main",
            "owner_home": str(tmp_path),
            "title": "t",
        }
    )
    policy = store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "interval_seconds": 180,
            "now": time.time(),
            "metadata": {"kind": "ordinary_task_resume", "resume_used": 0, "resume_limit": 3},
        }
    )
    # 首次 claim 成功
    assert try_claim_cli_resume(store, "task-1", now=1000.0) is True
    # lease 未过期（now 未推进 300s）→ 拒绝（防双 consumer）
    assert try_claim_cli_resume(store, "task-1", now=1100.0) is False
    # lease 过期 → 可重 claim
    assert try_claim_cli_resume(store, "task-1", now=1400.0) is True


def test_try_claim_cli_resume_no_policy(tmp_path):
    """无续跑 policy 的 task → 不可续跑。"""
    import time

    from agent_py_agent.agent.conversation.store import ConversationStore

    store = ConversationStore(tmp_path / "conv")
    assert try_claim_cli_resume(store, "no-such-task", now=time.time()) is False
