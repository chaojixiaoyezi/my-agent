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
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.cli.resume_contract import (
    CliContinuationContext,
    continuation_from_params,
    record_budget_exhausted,
    resume_prompt_for,
    try_claim_cli_resume,
)


@pytest.fixture
def repo(tmp_path):
    return RuntimeRepository(tmp_path / "home" / "runtime.db")


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


# ------------------------------------------------- 切片3: 轮/任务终态分层


def test_settle_continuation_round_keeps_task_nonterminal(repo):
    """续跑轮(seq>0) unfinished 不落 failed——任务级非终态由 resume_loop 决定。

    审查意见3: continuation_seq>0 必须绕过 cli_one_shot 的「未完成即 failed」
    兜底, 区分 round/attempt 终态与 task 终态。
    """
    from agent_py_agent.agent.agent_core.runtime_mixin import _settle_main_agent_run

    rec = repo.record_run_creation(owner_id="local/main", goal="g", run_id="run-1", role="main")
    params = SimpleNamespace(
        run_id="run-1",
        attempt_id=rec["attempt_id"],
        source="cli_run",
        continuation_seq=1,
    )
    result = SimpleNamespace(
        runtime_status="unfinished",
        runtime_reason="TOOL_ROUND_LIMIT_REACHED",
        runtime_source="tool_loop",
        tool_rounds=9,
    )
    _settle_main_agent_run(SimpleNamespace(subagents=SimpleNamespace(runtime_db=repo)), params, result)
    # run 保持 created(任务级非终态), attempt 不落 failed
    row = repo._runtime_connect().execute(
        "SELECT status FROM agent_runs WHERE agent_run_id = ?", (rec["agent_run_id"],)
    ).fetchone()
    assert row["status"] == "created"
    attempts = repo._runtime_connect().execute(
        "SELECT status FROM agent_attempts WHERE agent_run_id = ?", (rec["agent_run_id"],)
    ).fetchall()
    assert all(a["status"] != "failed" for a in attempts)


def test_settle_first_round_still_falls_back_failed(repo):
    """缺口E(双席复核 seq1835): 首轮不可续跑族(blocked)仍 one-shot 兜底 failed
    (问题C同族兜底不回退)——TOOL_ROUND_LIMIT_REACHED 已属可续跑族, 换
    blocked 作不可续跑族代表场景。"""
    from agent_py_agent.agent.agent_core.runtime_mixin import _settle_main_agent_run

    rec = repo.record_run_creation(owner_id="local/main", goal="g", run_id="run-2", role="main")
    params = SimpleNamespace(
        run_id="run-2",
        attempt_id=rec["attempt_id"],
        source="cli_run",
        continuation_seq=0,
    )
    result = SimpleNamespace(
        runtime_status="blocked",
        runtime_reason="MISSING_EVIDENCE",
        runtime_source="acceptance_gate",
        tool_rounds=7,
    )
    _settle_main_agent_run(SimpleNamespace(subagents=SimpleNamespace(runtime_db=repo)), params, result)
    row = repo._runtime_connect().execute(
        "SELECT status FROM agent_runs WHERE agent_run_id = ?", (rec["agent_run_id"],)
    ).fetchone()
    assert row["status"] == "failed"


def test_settle_first_round_continuable_keeps_nonterminal(repo):
    """缺口E(双席复核 seq1835): 首轮可续跑族(unfinished + TOOL_ROUND_LIMIT_
    REACHED, 共享 gate 判定 True)不落 failed——保留任务级非终态, resume_loop
    续跑(旧语义此场景落 failed, 缺口 E 修正)。"""
    from agent_py_agent.agent.agent_core.runtime_mixin import _settle_main_agent_run

    rec = repo.record_run_creation(owner_id="local/main", goal="g", run_id="run-2b", role="main")
    params = SimpleNamespace(
        run_id="run-2b",
        attempt_id=rec["attempt_id"],
        source="cli_run",
        continuation_seq=0,
    )
    result = SimpleNamespace(
        runtime_status="unfinished",
        runtime_reason="TOOL_ROUND_LIMIT_REACHED",
        runtime_source="tool_loop",
        tool_rounds=7,
    )
    _settle_main_agent_run(SimpleNamespace(subagents=SimpleNamespace(runtime_db=repo)), params, result)
    row = repo._runtime_connect().execute(
        "SELECT status FROM agent_runs WHERE agent_run_id = ?", (rec["agent_run_id"],)
    ).fetchone()
    assert row["status"] == "created"  # 非终态保留, 等 resume_loop 续跑


def test_settle_continuation_round_keeps_nonterminal(repo):
    """缺口E(双席复核 seq1835): 续跑轮(seq>0)非终态一律不落账——轮内
    unfinished 由 resume_loop 决定是否继续, 任务级终态不由此处覆盖。"""
    from agent_py_agent.agent.agent_core.runtime_mixin import _settle_main_agent_run

    rec = repo.record_run_creation(owner_id="local/main", goal="g", run_id="run-2c", role="main")
    params = SimpleNamespace(
        run_id="run-2c",
        attempt_id=rec["attempt_id"],
        source="cli_run",
        continuation_seq=3,
    )
    result = SimpleNamespace(
        runtime_status="unfinished",
        runtime_reason="TOOL_ROUND_LIMIT_REACHED",
        runtime_source="tool_loop",
        tool_rounds=7,
    )
    _settle_main_agent_run(SimpleNamespace(subagents=SimpleNamespace(runtime_db=repo)), params, result)
    row = repo._runtime_connect().execute(
        "SELECT status FROM agent_runs WHERE agent_run_id = ?", (rec["agent_run_id"],)
    ).fetchone()
    assert row["status"] == "created"  # 续跑轮不覆盖任务级终态


def test_record_budget_exhausted_event(repo):
    """预算耗尽写 continuation_budget_exhausted 事件（审查意见5，绝不 DONE）。"""
    from agent_py_agent.cli.resume_contract import record_budget_exhausted

    rec = repo.record_run_creation(owner_id="local/main", goal="g", run_id="run-3", role="main")
    agent = SimpleNamespace(subagents=SimpleNamespace(runtime_db=repo))
    record_budget_exhausted(
        agent=agent,
        run_id="run-3",
        attempt_id=rec["attempt_id"],
        budget_before=3,
        budget_after=3,
        reason="resume_limit_reached",
    )
    rows = repo._runtime_connect().execute(
        "SELECT * FROM runtime_events WHERE event_type = 'continuation_budget_exhausted'"
    ).fetchall()
    assert len(rows) == 1
    payload = rows[0]["payload_json"]
    assert '"budget_before": 3' in payload
    assert '"budget_after": 3' in payload
    assert '"needs_user_continue": true' in payload


# ------------------------------------------------- 切片4: resume_loop 循环


class _FakeRunAgent:
    """fake agent：首轮 unfinished，续跑轮逐个演进（第 3 轮 ok）。"""

    def __init__(self, tmp_path):
        import time

        from agent_py_agent.agent.conversation.store import ConversationStore

        self.conversation_store = ConversationStore(tmp_path / "conv")
        self.calls = []
        # 建续跑 policy（kind=ordinary_task_resume），claim 互斥检查需要
        self._thread = self.conversation_store.get_or_create_thread(
            {
                "canonical_user_id": "u", "channel": "cli_run",
                "channel_conversation_id": "req-1", "channel_user_id": "u",
                "owner_id": "local/main", "owner_home": str(tmp_path), "title": "t",
            }
        )
        self.conversation_store.set_progress_policy(
            {
                "thread_id": self._thread.thread_id, "task_id": "task-1",
                "interval_seconds": 180, "now": time.time(),
                "metadata": {"kind": "ordinary_task_resume", "resume_used": 0, "resume_limit": 3},
            }
        )

    def run(self, prompt, *, params=None, save=False, source="cli_run",
            resume_context=None, delivery_contract=None, on_chunk=None):
        seq = int(getattr(params, "continuation_seq", 0) or 0)
        self.calls.append((seq, str(prompt)[:40]))
        if seq == 0:
            return SimpleNamespace(
                runtime_status="unfinished", runtime_reason="TOOL_ROUND_LIMIT_REACHED",
                runtime_source="tool_loop", tool_rounds=5, attempt_id="attempt-0",
            )
        if seq < 2:
            return SimpleNamespace(
                runtime_status="unfinished", runtime_reason="TOOL_ROUND_LIMIT_REACHED",
                runtime_source="tool_loop", tool_rounds=5, attempt_id=f"attempt-{seq}",
            )
        return SimpleNamespace(
            runtime_status="ok", runtime_reason="", runtime_source="",
            tool_rounds=2, attempt_id=f"attempt-{seq}",
        )


def test_run_with_resume_loop_until_completed(tmp_path):
    """首轮 unfinished → 自动续跑 2 轮 → 第 3 轮 ok 完成（同一契约链路）。"""
    from agent_py_agent.agent.agent_core.runtime.run_params import (
        run_params_with_request_id,
    )
    from agent_py_agent.cli.resume_loop import run_with_resume

    fake = _FakeRunAgent(tmp_path)
    base = run_params_with_request_id(
        RunParams(source="cli_run", task_id="task-1", task_attributes={})
    )
    outcome = run_with_resume(
        fake, initial_prompt="测试任务", base_params=base, max_rounds=5,
    )
    assert outcome.status == "completed"
    assert outcome.rounds == 3  # 首轮 + 2 续跑轮
    assert len(fake.calls) == 3
    assert fake.calls[0][0] == 0
    assert fake.calls[1][0] == 1
    assert fake.calls[2][0] == 2
    # 续跑提示不含验收词
    assert "代码规模" not in fake.calls[1][1]
    assert "达标" not in fake.calls[1][1]


def test_run_with_resume_budget_exhausted(tmp_path):
    """max_rounds 耗尽 → budget_exhausted（写事件，不 DONE）。"""
    from agent_py_agent.agent.agent_core.runtime.run_params import (
        run_params_with_request_id,
    )
    from agent_py_agent.cli.resume_loop import run_with_resume

    class _AlwaysUnfinished(_FakeRunAgent):
        def run(self, prompt, *, params=None, **kw):
            seq = int(getattr(params, "continuation_seq", 0) or 0)
            self.calls.append((seq, str(prompt)[:40]))
            return SimpleNamespace(
                runtime_status="unfinished", runtime_reason="TOOL_ROUND_LIMIT_REACHED",
                runtime_source="tool_loop", tool_rounds=5, attempt_id=f"attempt-{seq}",
            )

    fake = _AlwaysUnfinished(tmp_path)
    base = run_params_with_request_id(
        RunParams(source="cli_run", task_id="task-1", task_attributes={})
    )
    outcome = run_with_resume(fake, initial_prompt="t", base_params=base, max_rounds=2)
    assert outcome.status == "budget_exhausted"
    assert outcome.reason == "max_rounds_reached"
    assert len(fake.calls) == 3  # 首轮 + 2 续跑轮后停


def test_run_with_resume_unresumable_stops(tmp_path):
    """首轮 blocked（不可续跑族）→ 立即停止，不续跑。"""
    from agent_py_agent.agent.agent_core.runtime.run_params import (
        run_params_with_request_id,
    )
    from agent_py_agent.cli.resume_loop import run_with_resume

    class _BlockedAgent(_FakeRunAgent):
        def run(self, prompt, *, params=None, **kw):
            self.calls.append((0, str(prompt)[:40]))
            return SimpleNamespace(
                runtime_status="blocked", runtime_reason="PROTOCOL_VIOLATION",
                runtime_source="tool_protocol_adapter", tool_rounds=0,
                attempt_id="attempt-0",
            )

    fake = _BlockedAgent(tmp_path)
    base = run_params_with_request_id(
        RunParams(source="cli_run", task_id="task-1", task_attributes={})
    )
    outcome = run_with_resume(fake, initial_prompt="t", base_params=base, max_rounds=5)
    assert outcome.status == "unresumable"
    assert len(fake.calls) == 1  # 只首轮


# ------------------------------------------------- 切片5: 崩溃/双消费者回归


def test_claim_mutual_exclusion_second_consumer_skips(tmp_path):
    """双 consumer：第一个 claim 后第二个在 lease 内跳过（防双跑）。

    审查硬条件2: claim/lease CAS 可审计; 双 consumer 有可复现测试。
    """
    import time

    from agent_py_agent.agent.conversation.store import ConversationStore

    store = ConversationStore(tmp_path / "conv")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "u", "channel": "cli_run",
            "channel_conversation_id": "req-1", "channel_user_id": "u",
            "owner_id": "local/main", "owner_home": str(tmp_path), "title": "t",
        }
    )
    store.set_progress_policy(
        {
            "thread_id": thread.thread_id, "task_id": "task-1",
            "interval_seconds": 180, "now": time.time(),
            "metadata": {"kind": "ordinary_task_resume", "resume_used": 0, "resume_limit": 3},
        }
    )
    # consumer A claim
    assert try_claim_cli_resume(store, "task-1", now=1000.0) is True
    # consumer B 在 lease 内 → 跳过
    assert try_claim_cli_resume(store, "task-1", now=1100.0) is False
    # lease 过期 → B 可接管
    assert try_claim_cli_resume(store, "task-1", now=1400.0) is True


def test_reclaim_after_process_crash(tmp_path):
    """进程崩溃后 claim 残留：lease 过期即可重 claim（恢复矩阵）。

    审查硬条件4: 进程崩溃、claim 后未建 attempt 有可复现测试。
    """
    import time

    from agent_py_agent.agent.conversation.store import ConversationStore

    store = ConversationStore(tmp_path / "conv")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "u", "channel": "cli_run",
            "channel_conversation_id": "req-1", "channel_user_id": "u",
            "owner_id": "local/main", "owner_home": str(tmp_path), "title": "t",
        }
    )
    store.set_progress_policy(
        {
            "thread_id": thread.thread_id, "task_id": "task-1",
            "interval_seconds": 180, "now": time.time(),
            "metadata": {"kind": "ordinary_task_resume", "resume_used": 0, "resume_limit": 3},
        }
    )
    # 进程 A claim 后崩溃(lease 残留)
    assert try_claim_cli_resume(store, "task-1", now=2000.0) is True
    # 进程 B 在 lease 内发现残留 → 跳过
    assert try_claim_cli_resume(store, "task-1", now=2100.0) is False
    # lease(300s)过期 → 进程 B 可重 claim 继续
    assert try_claim_cli_resume(store, "task-1", now=2400.0) is True


def test_resume_round_attempt_chain(tmp_path):
    """续跑轮 attempt 链：每轮 attempt_id 递增且 parent 指向上一轮。"""
    from agent_py_agent.agent.agent_core.runtime.run_params import (
        run_params_with_request_id,
    )
    from agent_py_agent.cli.resume_loop import run_with_resume

    class _ChainAgent(_FakeRunAgent):
        def run(self, prompt, *, params=None, **kw):
            seq = int(getattr(params, "continuation_seq", 0) or 0)
            self.calls.append((seq, str(getattr(params, "attempt_id", "")),
                               str(getattr(params, "continuation_parent_attempt_id", ""))))
            if seq == 0:
                return SimpleNamespace(runtime_status="unfinished",
                    runtime_reason="TOOL_ROUND_LIMIT_REACHED", runtime_source="tool_loop",
                    tool_rounds=5, attempt_id="attempt-0")
            if seq < 2:
                return SimpleNamespace(runtime_status="unfinished",
                    runtime_reason="TOOL_ROUND_LIMIT_REACHED", runtime_source="tool_loop",
                    tool_rounds=5, attempt_id=f"attempt-{seq}")
            return SimpleNamespace(runtime_status="ok", runtime_reason="",
                runtime_source="", tool_rounds=2, attempt_id=f"attempt-{seq}")

    fake = _ChainAgent(tmp_path)
    base = run_params_with_request_id(RunParams(source="cli_run", task_id="task-1", task_attributes={}))
    outcome = run_with_resume(fake, initial_prompt="t", base_params=base, max_rounds=5)
    assert outcome.status == "completed"
    # 缺口B(双席复核 seq1834): 续跑轮 attempt_id 不再合成 attempt-{seq},
    # 传空由 agent.run 内部 create_attempt 发放 DB attempt——params 里
    # attempt_id 应为空(真实 ID 由 runtime.db 发放, 不在 params 合成)。
    seq0, attempt0, parent0 = fake.calls[0]
    assert seq0 == 0 and parent0 == ""
    seq1, attempt1, parent1 = fake.calls[1]
    assert seq1 == 1
    assert attempt1 == ""  # 不合成: DB 发放真实 attempt, params 不预置
    assert parent1 == attempt0  # parent 链: cont-1 parent=首轮 attempt


def test_resume_round_blocked_is_unresumable(tmp_path):
    """续跑轮 blocked/协议违规收口必须标 unresumable，不能误标 completed。

    缺口C(双席复核 seq1835): 只有 runtime_status=ok 才算 completed。
    """
    from agent_py_agent.agent.agent_core.runtime.run_params import (
        run_params_with_request_id,
    )
    from agent_py_agent.cli.resume_loop import run_with_resume

    class _FirstThenBlocked(_FakeRunAgent):
        def run(self, prompt, *, params=None, **kw):
            seq = int(getattr(params, "continuation_seq", 0) or 0)
            self.calls.append((seq, str(prompt)[:40]))
            if seq == 0:
                return SimpleNamespace(runtime_status="unfinished",
                    runtime_reason="TOOL_ROUND_LIMIT_REACHED", runtime_source="tool_loop",
                    tool_rounds=5, attempt_id="attempt-0")
            return SimpleNamespace(runtime_status="blocked",
                runtime_reason="PROTOCOL_VIOLATION", runtime_source="tool_protocol_adapter",
                tool_rounds=0, attempt_id=f"attempt-{seq}")

    fake = _FirstThenBlocked(tmp_path)
    base = run_params_with_request_id(RunParams(source="cli_run", task_id="task-1", task_attributes={}))
    outcome = run_with_resume(fake, initial_prompt="t", base_params=base, max_rounds=5)
    assert outcome.status == "unresumable"  # 不是 completed
    assert outcome.reason == "PROTOCOL_VIOLATION"
    assert len(fake.calls) == 2  # 首轮 + 1 续跑轮后停


def test_resume_without_precreated_policy_ensures_and_continues(tmp_path):
    """真机洞回归(2026-08-14 testbox): 真实 CLI run 从不预建 ordinary_task_
    resume policy(它是收口 finalization 的 gateway 调度器路径产物), 旧实现
    try_claim_cli_resume 找不到 policy 永远 False → 永不续跑(测试用 fake
    agent 显式建 policy 掩盖)。修后 run_with_resume 先 ensure_ordinary_task_
    resume 建同一 policy 再 claim——无 policy 场景也应正常续跑完成。"""
    from agent_py_agent.agent.agent_core.runtime.run_params import (
        run_params_with_request_id,
    )
    from agent_py_agent.cli.resume_loop import run_with_resume

    class _NoPolicyAgent(_FakeRunAgent):
        def __init__(self, tmp_path):
            import time

            from agent_py_agent.agent.conversation.store import ConversationStore

            # 与 _FakeRunAgent 唯一区别: 不预建 policy(真实 CLI 场景)
            self.conversation_store = ConversationStore(tmp_path / "conv")
            self.calls = []
            self._thread = self.conversation_store.get_or_create_thread(
                {
                    "canonical_user_id": "u", "channel": "cli_run",
                    "channel_conversation_id": "req-nopol", "channel_user_id": "u",
                    "owner_id": "local/main", "owner_home": str(tmp_path), "title": "t",
                }
            )
            # 不发 set_progress_policy

    fake = _NoPolicyAgent(tmp_path)
    base = run_params_with_request_id(
        RunParams(source="cli_run", task_id="task-nopol", task_attributes={})
    )
    outcome = run_with_resume(fake, initial_prompt="t", base_params=base, max_rounds=5)
    # 不再 unresumable: 修后 ensure 自动建 policy 并续跑 3 轮完成
    assert outcome.status == "completed"
    assert len(fake.calls) == 3  # 首轮 + 2 续跑轮
    # policy 已创建(预算单一权威落点)
    policies = fake.conversation_store.list_progress_policies(enabled_only=True)
    assert any(
        p.task_id == "task-nopol"
        and str((p.metadata or {}).get("kind") or "") == "ordinary_task_resume"
        for p in policies
    )
