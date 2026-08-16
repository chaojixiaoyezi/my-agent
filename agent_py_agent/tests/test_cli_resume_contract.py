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
        ("REPEATED_TOOL_FAILURE", "tool_loop", "unfinished"),
        ("REQUIRED_ACTION_HAS_NO_EVIDENCE", "required_action_completion_gate", "unfinished"),
        # 2026-08-15: 未闭合工具块纯格式错误(整轮零执行已保证安全)可续跑
        ("TOOL_CALL_UNCLOSED", "tool_protocol_adapter", "unfinished"),
    ]
    not_continuable = [
        # 2026-08-16 第 4 条(用户裁决+通道运行时 对照): 普通任务正常不自动续跑——
        # 账本未关(TASK_PROGRESS_OPEN)与轮限收口(TOOL_ROUND_LIMIT_REACHED)
        # 不再自动 resume(通道运行时: max_turns 到达即停等用户确认)。
        ("TOOL_ROUND_LIMIT_REACHED", "tool_loop", "unfinished"),
        ("TASK_PROGRESS_OPEN", "tool_loop", "unfinished"),
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
        runtime_reason="REPEATED_TOOL_FAILURE",
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
    (问题C同族兜底不回退)——用 blocked 作不可续跑族代表场景
    (2026-08-16 第 4 条后 TOOL_ROUND_LIMIT_REACHED 亦不再可续跑)。"""
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
        runtime_reason="REPEATED_TOOL_FAILURE",
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
        runtime_reason="REPEATED_TOOL_FAILURE",
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
                runtime_status="unfinished", runtime_reason="REPEATED_TOOL_FAILURE",
                runtime_source="tool_loop", tool_rounds=5, attempt_id="attempt-0",
            )
        if seq < 2:
            return SimpleNamespace(
                runtime_status="unfinished", runtime_reason="REPEATED_TOOL_FAILURE",
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
                runtime_status="unfinished", runtime_reason="REPEATED_TOOL_FAILURE",
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


def test_run_with_resume_first_round_usage_limit_raises(tmp_path):
    """首轮 429 usage/quota limit → re-raise（不无限退避空转）。

    2026-08-17 真机实锤(cell8 卡死根因): 429 被当 ProviderRecoverableError
    无限退避——配额恢复前(分钟~小时级)空转烧资源。修复: usage/quota limit
    re-raise 到 cmd_run 诚实报告(进程正常返回, 非任务中途消失)。
    """
    from agent_py_agent.agent.agent_core.runtime.run_params import (
        run_params_with_request_id,
    )
    from agent_py_agent.agent.backends import ProviderUsageLimitError
    from agent_py_agent.cli.resume_loop import run_with_resume

    class _UsageLimited(_FakeRunAgent):
        def run(self, prompt, *, params=None, **kw):
            self.calls.append(("limited", str(prompt)[:20]))
            raise ProviderUsageLimitError("HTTP 429: 5-hour usage limit reached")

    fake = _UsageLimited(tmp_path)
    base = run_params_with_request_id(
        RunParams(source="cli_run", task_id="task-1", task_attributes={})
    )
    with pytest.raises(ProviderUsageLimitError):
        run_with_resume(fake, initial_prompt="t", base_params=base, max_rounds=5)
    # 只调了一次就 re-raise（不无限退避）
    assert len(fake.calls) == 1


def test_run_with_resume_unresumable_continues_in_process(tmp_path):
    """首轮 blocked（不可续跑族）→ 进程内继续下一轮（不退出），第二轮 ok 完成。

    用户指示(2026-08-16): 进程不能退出（按一年任务设计）——收口后
    进程内续跑，任务终态(runtime_status=ok)才退出。
    """
    from agent_py_agent.agent.agent_core.runtime.run_params import (
        run_params_with_request_id,
    )
    from agent_py_agent.cli.resume_loop import run_with_resume

    class _BlockedThenOk(_FakeRunAgent):
        def run(self, prompt, *, params=None, **kw):
            seq = int(getattr(params, "continuation_seq", 0) or 0)
            self.calls.append((seq, str(prompt)[:40]))
            if seq == 0:
                return SimpleNamespace(
                    runtime_status="blocked", runtime_reason="PROTOCOL_VIOLATION",
                    runtime_source="tool_protocol_adapter", tool_rounds=0,
                    attempt_id="attempt-0",
                )
            return SimpleNamespace(
                runtime_status="ok", runtime_reason="", runtime_source="tool_loop",
                tool_rounds=1, attempt_id=f"attempt-{seq}",
            )

    fake = _BlockedThenOk(tmp_path)
    base = run_params_with_request_id(
        RunParams(source="cli_run", task_id="task-1", task_attributes={})
    )
    outcome = run_with_resume(fake, initial_prompt="t", base_params=base, max_rounds=5)
    assert outcome.status == "completed"  # 第二轮 ok 才退出
    assert len(fake.calls) == 2  # 首轮 blocked + 续跑轮 ok


def test_run_with_resume_provider_error_first_round_retries(tmp_path):
    """首轮 ProviderRecoverableError（输出截断）→ 进程内重试首轮不退出。

    真机 cell2 2026-08-15: stop_reason=max_tokens → ProviderRecoverableError
    → 冒泡到 cmd_run except → 进程退出（违反用户铁律「进程不能退出」）。
    修复后: run_with_resume 进程内退避重试, 第二次成功即完成。
    """
    from agent_py_agent.agent.agent_core.runtime.run_params import (
        run_params_with_request_id,
    )
    from agent_py_agent.agent.backends import ProviderResponseError
    from agent_py_agent.cli.resume_loop import run_with_resume

    class _TruncatedThenOk(_FakeRunAgent):
        def run(self, prompt, *, params=None, **kw):
            seq = int(getattr(params, "continuation_seq", 0) or 0)
            self.calls.append((seq, str(prompt)[:40]))
            if len(self.calls) == 1:  # 首轮第一次调用: 输出截断
                raise ProviderResponseError(
                    "模型响应未完成（stop_reason=max_tokens）",
                    error_code="MODEL_INCOMPLETE_RESPONSE",
                )
            if seq == 0:
                return SimpleNamespace(
                    runtime_status="unfinished", runtime_reason="REPEATED_TOOL_FAILURE",
                    runtime_source="tool_loop", tool_rounds=5, attempt_id="attempt-0",
                )
            return SimpleNamespace(
                runtime_status="ok", runtime_reason="", runtime_source="tool_loop",
                tool_rounds=2, attempt_id=f"attempt-{seq}",
            )

    fake = _TruncatedThenOk(tmp_path)
    base = run_params_with_request_id(
        RunParams(source="cli_run", task_id="task-1", task_attributes={})
    )
    outcome = run_with_resume(fake, initial_prompt="t", base_params=base, max_rounds=5)
    assert outcome.status == "completed"
    assert len(fake.calls) == 3  # 首轮截断重试 + 首轮成功 + 续跑轮 ok


def test_run_with_resume_provider_error_continuation_retries(tmp_path):
    """续跑轮 ProviderRecoverableError → 进程内重试同一续跑轮（不消耗轮次）。"""
    from agent_py_agent.agent.agent_core.runtime.run_params import (
        run_params_with_request_id,
    )
    from agent_py_agent.agent.backends import ProviderResponseError
    from agent_py_agent.cli.resume_loop import run_with_resume

    class _ContinuationTruncatedThenOk(_FakeRunAgent):
        def run(self, prompt, *, params=None, **kw):
            seq = int(getattr(params, "continuation_seq", 0) or 0)
            self.calls.append((seq, str(prompt)[:40]))
            if seq == 1 and self.calls.count((seq, str(prompt)[:40])) == 1:
                raise ProviderResponseError(
                    "模型响应未完成（stop_reason=max_tokens）",
                    error_code="MODEL_INCOMPLETE_RESPONSE",
                )
            if seq == 0:
                return SimpleNamespace(
                    runtime_status="unfinished", runtime_reason="REPEATED_TOOL_FAILURE",
                    runtime_source="tool_loop", tool_rounds=5, attempt_id="attempt-0",
                )
            return SimpleNamespace(
                runtime_status="ok", runtime_reason="", runtime_source="tool_loop",
                tool_rounds=2, attempt_id=f"attempt-{seq}",
            )

    fake = _ContinuationTruncatedThenOk(tmp_path)
    base = run_params_with_request_id(
        RunParams(source="cli_run", task_id="task-1", task_attributes={})
    )
    outcome = run_with_resume(fake, initial_prompt="t", base_params=base, max_rounds=5)
    assert outcome.status == "completed"
    assert len(fake.calls) == 3  # 首轮 + 续跑轮截断重试 + 续跑轮成功


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
                    runtime_reason="REPEATED_TOOL_FAILURE", runtime_source="tool_loop",
                    tool_rounds=5, attempt_id="attempt-0")
            if seq < 2:
                return SimpleNamespace(runtime_status="unfinished",
                    runtime_reason="REPEATED_TOOL_FAILURE", runtime_source="tool_loop",
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


def test_resume_round_blocked_continues_not_exit(tmp_path):
    """续跑轮 blocked/协议违规收口不再退出进程——进程内继续下一轮。

    用户指示(2026-08-16)+缺口C(seq1835)语义保留: 只有 runtime_status=ok
    才算 completed 退出; blocked 收口记录后继续续跑(不误标 completed,
    也不退出进程)。
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
                    runtime_reason="REPEATED_TOOL_FAILURE", runtime_source="tool_loop",
                    tool_rounds=5, attempt_id="attempt-0")
            if seq == 1:
                return SimpleNamespace(runtime_status="blocked",
                    runtime_reason="PROTOCOL_VIOLATION", runtime_source="tool_protocol_adapter",
                    tool_rounds=0, attempt_id=f"attempt-{seq}")
            return SimpleNamespace(runtime_status="ok",
                runtime_reason="", runtime_source="tool_loop",
                tool_rounds=1, attempt_id=f"attempt-{seq}")

    fake = _FirstThenBlocked(tmp_path)
    base = run_params_with_request_id(RunParams(source="cli_run", task_id="task-1", task_attributes={}))
    outcome = run_with_resume(fake, initial_prompt="t", base_params=base, max_rounds=5)
    assert outcome.status == "completed"  # blocked 后进程内继续，第三轮 ok 才退出
    assert len(fake.calls) == 3  # 首轮 + blocked 续跑轮 + ok 续跑轮


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


def test_claim_strict_cas_concurrent_consumers_only_one_wins(tmp_path):
    """双席复核硬门1(严格 CAS): 两个消费者(CLI/gateway 并发)claim 同一
    policy, flock 锁内读-改-写保证只有一个成功——不再读-写窗口互相覆盖。

    两个独立 ConversationStore 实例(模拟两个进程, 同文件系统)并发
    try_claim_cli_resume: 恰一个返回 True, 另一个返回 False(lease 未过期)。
    """
    import threading
    import time as _time

    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.cli.resume_contract import try_claim_cli_resume

    store = ConversationStore(tmp_path / "conv")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "u", "channel": "cli_run",
            "channel_conversation_id": "req-cas", "channel_user_id": "u",
            "owner_id": "local/main", "owner_home": str(tmp_path), "title": "t",
        }
    )
    store.set_progress_policy(
        {
            "thread_id": thread.thread_id, "task_id": "task-cas",
            "interval_seconds": 180, "now": _time.time(),
            "metadata": {"kind": "ordinary_task_resume", "resume_used": 0, "resume_limit": 3},
        }
    )
    # 第二个 store 实例=独立消费者(模拟另一进程, 同一 policy 文件)
    store2 = ConversationStore(tmp_path / "conv")
    results: list[bool] = []
    barrier = threading.Barrier(2)

    def _claimer(s):
        barrier.wait()  # 同时出发, 放大读-改-写竞态窗口
        results.append(try_claim_cli_resume(s, "task-cas"))

    t1 = threading.Thread(target=_claimer, args=(store,))
    t2 = threading.Thread(target=_claimer, args=(store2,))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(results) == [False, True]  # 恰一个成功
    claimed = store.get_progress_policy(
        store.list_progress_policies(enabled_only=True)[0].policy_id
    )
    assert (claimed.metadata or {}).get("cli_claim_owner") == "cli_resume_loop"
    # lease 内再次 claim 仍失败(幂等互斥)
    assert try_claim_cli_resume(store, "task-cas") is False
    # lease 过期后可重 claim(崩溃后接管)
    expired = _time.time() + 400
    assert try_claim_cli_resume(store, "task-cas", now=expired) is True


def test_gateway_suppresses_cli_claimed_policy(tmp_path):
    """双席复核硬门2(gateway 同读 claim): CLI cli_claim_at lease 内,
    gateway 调度器的 _progress_policy_suppression_reason 返回
    "cli_claim_held" → 不在 CLI 续跑链内并发消费(预算语义成立);
    lease 过期后 gateway 自然接管。"""
    import time as _time
    from dataclasses import replace as _replace

    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.conversation.runtime import (
        _progress_policy_suppression_reason,
    )
    from agent_py_agent.agent.conversation.store import ConversationStore

    store = ConversationStore(tmp_path / "conv")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "u", "channel": "cli_run",
            "channel_conversation_id": "req-gw", "channel_user_id": "u",
            "owner_id": "local/main", "owner_home": str(tmp_path), "title": "t",
        }
    )
    store.set_progress_policy(
        {
            "thread_id": thread.thread_id, "task_id": "task-gw",
            "interval_seconds": 180, "now": _time.time(),
            "metadata": {"kind": "ordinary_task_resume", "resume_used": 0, "resume_limit": 3},
        }
    )
    policy = store.list_progress_policies(enabled_only=True)[0]
    now = _time.time()
    # 未 claim → 不 suppress
    assert _progress_policy_suppression_reason(store, policy, now=now) == ""
    # CLI claim 后 → gateway suppress(cli_claim_held)
    store.update_progress_policy_atomic(
        policy.policy_id,
        lambda p: _replace(
            p,
            metadata={
                **p.metadata,
                "cli_claim_at": now,
                "cli_claim_owner": "cli_resume_loop",
            },
        ),
    )
    claimed = store.get_progress_policy(policy.policy_id)
    assert _progress_policy_suppression_reason(store, claimed, now=now) == "cli_claim_held"
    # lease 过期后 gateway 接管(不再 suppress)
    later = now + 400
    assert _progress_policy_suppression_reason(store, claimed, now=later) == ""
    # 非 ordinary_task_resume 的 policy 不受 cli_claim 影响
    store.set_progress_policy(
        {
            "thread_id": thread.thread_id, "task_id": "task-other",
            "interval_seconds": 180, "now": now,
            "metadata": {"kind": "other_kind"},
        }
    )
    other = [p for p in store.list_progress_policies(enabled_only=True) if p.task_id == "task-other"][0]
    assert _progress_policy_suppression_reason(store, other, now=now) == ""


def test_gateway_execute_cas_aborted_when_cli_claims_in_window(tmp_path):
    """双席复核硬门1(seq1845): gateway 读到"未 claim"后、执行前 CLI 写入
    claim 的竞态窗口——gateway 执行前 CAS 领取被 aborted(不执行),
    双向互斥闭合。CLI lease 过期后 gateway 可领取执行。"""
    import time as _time

    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.conversation.runtime import _gateway_consume_policy_cas
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.cli.resume_contract import try_claim_cli_resume

    store = ConversationStore(tmp_path / "conv")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "u", "channel": "cli_run",
            "channel_conversation_id": "req-race", "channel_user_id": "u",
            "owner_id": "local/main", "owner_home": str(tmp_path), "title": "t",
        }
    )
    store.set_progress_policy(
        {
            "thread_id": thread.thread_id, "task_id": "task-race",
            "interval_seconds": 180, "now": _time.time(),
            "metadata": {"kind": "ordinary_task_resume", "resume_used": 0, "resume_limit": 3},
        }
    )
    policy = store.list_progress_policies(enabled_only=True)[0]
    now = _time.time()
    # 竞态窗口: gateway 读检查通过(未 claim) → CLI 先写入 claim
    assert _gateway_consume_policy_cas(store, policy, now=now) is True  # gateway 领取
    assert try_claim_cli_resume(store, "task-race", now=now) is False  # CLI 被 gateway 挡
    # 反过来: CLI 先 claim → gateway 执行前 CAS 被 aborted
    store2 = ConversationStore(tmp_path / "conv")
    store2.set_progress_policy(
        {
            "thread_id": thread.thread_id, "task_id": "task-race2",
            "interval_seconds": 180, "now": now,
            "metadata": {"kind": "ordinary_task_resume", "resume_used": 0, "resume_limit": 3},
        }
    )
    p2 = [p for p in store2.list_progress_policies(enabled_only=True) if p.task_id == "task-race2"][0]
    assert try_claim_cli_resume(store2, "task-race2", now=now) is True  # CLI 先 claim
    assert (
        _gateway_consume_policy_cas(store2, p2, now=now) is False
    )  # gateway 执行前被 aborted, 不双跑
    # lease 过期后 gateway 可领取(CLI 崩溃/退出接管)
    later = now + 400
    assert _gateway_consume_policy_cas(store2, p2, now=later) is True
    # 非 ordinary 类型不受互斥影响
    store2.set_progress_policy(
        {
            "thread_id": thread.thread_id, "task_id": "task-other-race",
            "interval_seconds": 180, "now": now,
            "metadata": {"kind": "other_kind"},
        }
    )
    po = [p for p in store2.list_progress_policies(enabled_only=True) if p.task_id == "task-other-race"][0]
    assert _gateway_consume_policy_cas(store2, po, now=now) is True


def test_continuation_handoff_repository_cas(tmp_path):
    """handoff 移交单: 创建/待接管查询/幂等创建/CAS 消费防双领。"""
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

    repo = RuntimeRepository(tmp_path / "home" / "runtime.db")
    now = 1786000000.0
    hid = repo.create_continuation_handoff(
        agent_run_id="agentrun-1", attempt_id="attempt-1", task_run_id="taskrun-1",
        root_run_id="run-1", root_request_id="req-1", root_thread_id="thread-1",
        root_task_id="task-1", user_prompt="原任务", continuation_seq=8,
        reason="max_rounds_reached", now=now,
    )
    assert hid
    pending = repo.pending_continuation_handoffs(limit=10)
    assert len(pending) == 1
    assert pending[0]["root_run_id"] == "run-1"
    assert pending[0]["user_prompt"] == "原任务"
    assert pending[0]["continuation_seq"] == 8
    # 同 seq 幂等: 不重复创建
    assert (
        repo.create_continuation_handoff(
            agent_run_id="agentrun-1", attempt_id="attempt-1", task_run_id="taskrun-1",
            root_run_id="run-1", root_request_id="req-1", root_thread_id="thread-1",
            root_task_id="task-1", user_prompt="原任务", continuation_seq=8,
            reason="max_rounds_reached", now=now,
        )
        == ""
    )
    # CAS 消费: 第一个成功, 第二个失败(已被领)
    assert repo.consume_continuation_handoff(hid, consumed_by="gw", now=now + 1) is True
    assert repo.consume_continuation_handoff(hid, consumed_by="gw2", now=now + 2) is False
    assert repo.pending_continuation_handoffs(limit=10) == []


def test_handoff_write_on_budget_exhausted(tmp_path):
    """CLI 预算耗尽路径写移交单(长任务显式移交, 不依赖 store 共享)。"""
    from agent_py_agent.agent.agent_core.runtime.run_params import (
        run_params_with_request_id,
    )
    from agent_py_agent.cli.resume_loop import run_with_resume

    class _ExhaustAgent(_FakeRunAgent):
        def __init__(self, tmp_path):
            from types import SimpleNamespace as _NS

            from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

            super().__init__(tmp_path)
            self.subagents = _NS(runtime_db=RuntimeRepository(tmp_path / "home" / "runtime.db"))

        def run(self, prompt, *, params=None, save=False, source="cli_run",
                resume_context=None, delivery_contract=None, on_chunk=None):
            seq = int(getattr(params, "continuation_seq", 0) or 0)
            self.calls.append((seq, str(prompt)[:40]))
            return SimpleNamespace(
                runtime_status="unfinished", runtime_reason="REPEATED_TOOL_FAILURE",
                runtime_source="tool_loop", tool_rounds=2, attempt_id=f"attempt-{seq}",
            )

    fake = _ExhaustAgent(tmp_path)
    base = run_params_with_request_id(
        RunParams(source="cli_run", task_id="task-handoff", task_attributes={})
    )
    # max_rounds=2: 首轮 + 1 续跑轮后预算耗尽 → 写移交单
    outcome = run_with_resume(fake, initial_prompt="原任务描述", base_params=base, max_rounds=2)
    assert outcome.status == "budget_exhausted"
    # 移交单已写(同一 runtime.db, 无 conversation store 依赖)
    repo = fake.subagents.runtime_db
    pending = repo.pending_continuation_handoffs(limit=10)
    assert len(pending) == 1
    assert pending[0]["user_prompt"] == "原任务描述"
    assert pending[0]["reason"] == "max_rounds_reached"
    assert pending[0]["root_task_id"] == "task-handoff"


def test_gateway_handoff_continuation_rewrites_next_handoff(tmp_path):
    """gateway 接管多轮: 续跑轮收口后仍可续跑(共享 gate True) → 续写移交单
    供下个 tick 继续(同一 run 持续推进不截断)。"""
    from types import SimpleNamespace as _NS

    from agent_py_agent.agent.agent_core.runtime.run_params import (
        run_params_with_request_id,
    )
    from agent_py_agent.agent.conversation.runtime import _run_handoff_continuation
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

    repo = RuntimeRepository(tmp_path / "home" / "runtime.db")
    now = 1786001000.0
    repo.create_continuation_handoff(
        agent_run_id="", attempt_id="attempt-1", task_run_id="taskrun-1",
        root_run_id="run-1", root_request_id="req-1", root_thread_id="thread-1",
        root_task_id="task-1", user_prompt="原任务", continuation_seq=9,
        reason="max_rounds_reached", now=now,
    )

    class _GatewayAgent(_FakeRunAgent):
        def __init__(self, tmp_path):
            super().__init__(tmp_path)
            self.subagents = _NS(runtime_db=repo)

        def run(self, prompt, *, params=None, save=False, source="cli_run",
                resume_context=None, delivery_contract=None, on_chunk=None):
            seq = int(getattr(params, "continuation_seq", 0) or 0)
            self.calls.append((seq, str(prompt)[:40]))
            # 续跑轮仍 unfinished(可续跑族) → 应续写移交单
            return SimpleNamespace(
                runtime_status="unfinished", runtime_reason="REPEATED_TOOL_FAILURE",
                runtime_source="tool_loop", tool_rounds=2, attempt_id=f"attempt-{seq}",
            )

    fake = _GatewayAgent(tmp_path)
    handoff = repo.pending_continuation_handoffs(limit=10)[0]
    # 模拟 gateway 领取(CAS)
    assert repo.consume_continuation_handoff(handoff["handoff_id"], consumed_by="gw", now=now)
    _run_handoff_continuation(fake, handoff, now=now)
    # 执行了续跑轮(seq=10 = handoff seq 9 + 1)
    assert fake.calls and fake.calls[0][0] == 10
    assert "max_rounds_reached" in fake.calls[0][1]  # 续跑提示含 handoff reason
    # 续写移交单(seq=10, 下个 tick 继续)
    pending = repo.pending_continuation_handoffs(limit=10)
    assert len(pending) == 1
    assert pending[0]["continuation_seq"] == 10
    assert pending[0]["user_prompt"] == "原任务"


def test_handoff_budget_gate_stops_infinite_continuation(tmp_path):
    """gateway 接管预算闸: 同 run 已消费移交段数达上限(HANDOFF_BUDGET_
    SEGMENTS) → 不再领取/续写(防无限续烧额度, 等用户显式「继续」)。"""
    from types import SimpleNamespace as _NS

    from agent_py_agent.agent.agent_core.runtime.run_params import (
        run_params_with_request_id,
    )
    from agent_py_agent.agent.conversation.runtime import (
        HANDOFF_BUDGET_SEGMENTS,
        _handoff_budget_exhausted,
        _run_handoff_continuation,
    )
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

    repo = RuntimeRepository(tmp_path / "home" / "runtime.db")
    now = 1786002000.0
    # 预算上限内的移交单(已消费)
    for i in range(HANDOFF_BUDGET_SEGMENTS):
        hid = repo.create_continuation_handoff(
            agent_run_id="", attempt_id=f"attempt-{i}", task_run_id="taskrun-b",
            root_run_id="run-b", root_request_id="req-b", root_thread_id="thread-b",
            root_task_id="task-b", user_prompt="任务B", continuation_seq=1 + i,
            reason="max_rounds_reached", now=now + i,
        )
        assert repo.consume_continuation_handoff(hid, consumed_by="gw", now=now + i + 1)
    assert _handoff_budget_exhausted(repo, "run-b") is True  # 预算耗尽
    assert _handoff_budget_exhausted(repo, "run-other") is False  # 其他 run 不受影响

    class _BudgetAgent(_FakeRunAgent):
        def __init__(self, tmp_path):
            super().__init__(tmp_path)
            self.subagents = _NS(runtime_db=repo)

        def run(self, prompt, *, params=None, save=False, source="cli_run",
                resume_context=None, delivery_contract=None, on_chunk=None):
            self.calls.append(int(getattr(params, "continuation_seq", 0) or 0))
            return SimpleNamespace(
                runtime_status="unfinished", runtime_reason="REPEATED_TOOL_FAILURE",
                runtime_source="tool_loop", tool_rounds=2, attempt_id="attempt-x",
            )

    # 预算耗尽后: 即使执行续跑轮也不续写移交单
    fake = _BudgetAgent(tmp_path)
    handoff = {
        "handoff_id": "h", "root_run_id": "run-b", "root_task_id": "task-b",
        "root_request_id": "req-b", "root_thread_id": "thread-b",
        "user_prompt": "任务B", "continuation_seq": HANDOFF_BUDGET_SEGMENTS,
        "reason": "max_rounds_reached", "attempt_id": "attempt-9",
    }
    _run_handoff_continuation(fake, handoff, now=now)
    assert fake.calls == [HANDOFF_BUDGET_SEGMENTS + 1]  # 执行了一轮
    pending = repo.pending_continuation_handoffs(limit=10)
    assert pending == []  # 不续写移交单


def test_gateway_consume_rejects_live_gateway_claim(tmp_path):
    """双席复核硬门2(seq1897): gateway 锁内 CAS 拒绝仍在 lease 内的既有
    gateway_claim_at——多 gateway 实例互斥不依赖单例假设; lease 过期后
    可接管(重启残留让位)。"""
    import time as _time

    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.conversation.runtime import _gateway_consume_policy_cas
    from agent_py_agent.agent.conversation.store import ConversationStore

    store = ConversationStore(tmp_path / "conv")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "u", "channel": "cli_run",
            "channel_conversation_id": "req-gw2", "channel_user_id": "u",
            "owner_id": "local/main", "owner_home": str(tmp_path), "title": "t",
        }
    )
    store.set_progress_policy(
        {
            "thread_id": thread.thread_id, "task_id": "task-gw2",
            "interval_seconds": 180, "now": _time.time(),
            "metadata": {"kind": "ordinary_task_resume", "resume_used": 0, "resume_limit": 3},
        }
    )
    policy = store.list_progress_policies(enabled_only=True)[0]
    now = _time.time()
    # gateway A 领取成功
    assert _gateway_consume_policy_cas(store, policy, now=now) is True
    # gateway B(另一实例)在 lease 内 → 被拒(不覆盖存活领取)
    assert _gateway_consume_policy_cas(store, policy, now=now + 10) is False
    # lease 过期后 B 可接管(A 崩溃/退出让位)
    assert _gateway_consume_policy_cas(store, policy, now=now + 400) is True
    # 非 ordinary 类型不受互斥影响
    store.set_progress_policy(
        {
            "thread_id": thread.thread_id, "task_id": "task-other2",
            "interval_seconds": 180, "now": now,
            "metadata": {"kind": "other_kind"},
        }
    )
    po = [p for p in store.list_progress_policies(enabled_only=True) if p.task_id == "task-other2"][0]
    assert _gateway_consume_policy_cas(store, po, now=now) is True


def test_handoff_round_deadline_interrupts_and_does_not_rehandoff(tmp_path, monkeypatch):
    """双席复核硬门4(seq1903): 单轮 wall-clock deadline——gateway 接管轮
    挂起超时被 interrupt_by_name 中断(InterruptedError) → 不续写移交单
    (fail-closed 释放), 不留 attempt 永久 running。"""
    import threading as _threading
    from types import SimpleNamespace as _NS

    from agent_py_agent.agent.agent_core.runtime.run_params import (
        run_params_with_request_id,
    )
    from agent_py_agent.agent.conversation import runtime as _runtime
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

    repo = RuntimeRepository(tmp_path / "home" / "runtime.db")
    now = 1786003000.0
    repo.create_continuation_handoff(
        agent_run_id="", attempt_id="attempt-1", task_run_id="taskrun-d",
        root_run_id="run-d", root_request_id="req-d", root_thread_id="thread-d",
        root_task_id="task-d", user_prompt="任务D", continuation_seq=1,
        reason="max_rounds_reached", now=now,
    )
    handoff = repo.pending_continuation_handoffs(limit=10)[0]
    assert repo.consume_continuation_handoff(handoff["handoff_id"], consumed_by="gw", now=now)

    class _StuckAgent(_FakeRunAgent):
        def __init__(self, tmp_path):
            super().__init__(tmp_path)
            self.subagents = _NS(runtime_db=repo)

        def run(self, prompt, *, params=None, save=False, source="cli_run",
                resume_context=None, delivery_contract=None, on_chunk=None):
            self.calls.append(1)
            # 模拟挂起: 等待中断(InterruptedError)
            from agent_py_agent.agent.concurrency.interrupt import (
                interrupt_by_name,
                register_interruptible,
            )
            from agent_py_agent.agent.conversation.control_commands import (
                conversation_request_interrupt_name,
            )

            name = conversation_request_interrupt_name("task-d")
            # 注册后立即触发中断(模拟 deadline 到期)
            with register_interruptible(name):
                interrupt_by_name(name)
                raise InterruptedError("deadline")
            raise AssertionError("unreachable")

    fake = _StuckAgent(tmp_path)
    # deadline 常量临时调小不必要——直接验证中断路径: agent.run 抛
    # InterruptedError 被 _run_handoff_continuation 的 finally 清理,
    # 且不续写移交单。
    _runtime._run_handoff_continuation(fake, handoff, now=now)
    assert fake.calls == [1]  # 执行了(被中断)
    assert repo.pending_continuation_handoffs(limit=10) == []  # 不续写


def test_orphan_reclaim_nonterminal_ops_writes_visible_event(tmp_path):
    """僵尸 attempt 可见性(2026-08-15 真机 verify-mr4 坐实): 孤儿 reclaim
    遇 UNKNOWN 工具操作 fail-closed 拒自动裁决时, 必须写 orphan_reclaim_
    blocked 审计事件(不再静默)——attempt 永久 running 但 owner/恢复器可
    发现并人工处理。"""
    import time as _time

    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

    repo = RuntimeRepository(tmp_path / "home" / "runtime.db")
    now = 1786004000.0
    rec = repo.record_run_creation(
        owner_id="local/main", goal="g", run_id="run-zombie", role="main"
    )
    attempt_id = str(rec["attempt_id"])
    # 构造: 过期执行锁 + UNKNOWN 工具操作(结果不可知)
    with repo.transaction() as conn:
        conn.execute(
            "INSERT INTO tool_operations(operation_id, agent_run_id, attempt_id, "
            "attempt_generation, tool_operation_generation, operation_type, "
            "status, created_at, updated_at, outcome_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "op-zombie-1", str(rec["agent_run_id"]), attempt_id, 1, 1,
                "run_command", "UNKNOWN", now - 100, now - 100,
                '{"args_hash": "sha256:x"}',
            ),
        )
        # record_run_creation 不建执行锁(锁在 create_attempt 轮换时建)——
        # 直接 INSERT 一条过期锁模拟僵尸 attempt
        conn.execute(
            "INSERT INTO resource_locks(lock_id, canonical_scope, holder_instance, "
            "pid, start_token, attempt_id, attempt_generation, workspace_epoch, "
            "tool_operation_generation, lease_expires_at, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "lock-zombie-1", f"attempt-exec:{rec['agent_run_id']}",
                "test-holder", 99999, "tok", attempt_id, 1, 1, 0,
                now - 3600, now - 3700, now - 3700,
            ),
        )
    # 触发 reclaim(now 传参使宽限期成立)
    result = repo.reclaim_orphaned_attempt(attempt_id, operator="test", reason="x")
    assert result.get("reclaimed") is False
    assert result.get("reason") == "nonterminal_ops"
    # 双席核对点3(2026-08-15): 拦截转结构化终态——attempt 标 unknown
    assert result.get("marked_unknown") is True
    attempt = repo._runtime_connect().execute(
        "SELECT status, ended_at FROM agent_attempts WHERE attempt_id = ?",
        (attempt_id,),
    ).fetchone()
    assert attempt["status"] == "unknown"
    assert attempt["ended_at"] > 0
    # 可见事件已写(不再静默): orphan_reclaim_blocked + attempt_unknown_terminal
    events = repo._runtime_connect().execute(
        "SELECT payload_json FROM runtime_events "
        "WHERE event_type = 'orphan_reclaim_blocked'",
    ).fetchall()
    assert len(events) == 1
    assert "nonterminal_ops" in events[0]["payload_json"]
    terminal = repo._runtime_connect().execute(
        "SELECT payload_json FROM runtime_events "
        "WHERE event_type = 'attempt_unknown_terminal'",
    ).fetchall()
    assert len(terminal) == 1
    assert "nonterminal_ops" in terminal[0]["payload_json"]
    # 幂等(双席 seq1947 + 核对点3): 再次触发 reclaim 直接 already_terminal,
    # 不重复标记也不重复刷事件
    result2 = repo.reclaim_orphaned_attempt(attempt_id, operator="test", reason="x")
    assert result2.get("reason") == "already_terminal"
    events2 = repo._runtime_connect().execute(
        "SELECT 1 FROM runtime_events WHERE event_type = 'orphan_reclaim_blocked'",
    ).fetchall()
    assert len(events2) == 1  # 同 attempt 只写一次
    terminal2 = repo._runtime_connect().execute(
        "SELECT 1 FROM runtime_events WHERE event_type = 'attempt_unknown_terminal'",
    ).fetchall()
    assert len(terminal2) == 1


def test_reclaim_side_effect_gate_marks_unknown_and_keeps_lock(tmp_path):
    """side_effect_gate 分支同款结构化终态(2026-08-15 双席核对点3):
    外部副作用 op 无 effect_key → attempt 标 unknown + 执行权锁保留
    (锁防并发写; create_attempt 的 unknown 闸拦自动接管)。"""
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

    repo = RuntimeRepository(tmp_path / "home" / "runtime.db")
    now = 1786005000.0
    rec = repo.record_run_creation(
        owner_id="local/main", goal="g", run_id="run-side-effect", role="main"
    )
    attempt_id = str(rec["attempt_id"])
    with repo.transaction() as conn:
        conn.execute(
            "INSERT INTO tool_operations(operation_id, agent_run_id, attempt_id, "
            "attempt_generation, tool_operation_generation, operation_type, "
            "status, created_at, updated_at, outcome_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "op-se-1", str(rec["agent_run_id"]), attempt_id, 1, 1,
                "publish", "UNKNOWN", now - 100, now - 100,
                '{"side_effect": true, "args_hash": "sha256:y"}',
            ),
        )
        conn.execute(
            "INSERT INTO resource_locks(lock_id, canonical_scope, holder_instance, "
            "pid, start_token, attempt_id, attempt_generation, workspace_epoch, "
            "tool_operation_generation, lease_expires_at, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "lock-se-1", f"attempt-exec:{rec['agent_run_id']}",
                "test-holder", 99999, "tok", attempt_id, 1, 1, 0,
                now - 3600, now - 3700, now - 3700,
            ),
        )
    result = repo.reclaim_orphaned_attempt(attempt_id, operator="test", reason="x")
    assert result.get("reclaimed") is False
    assert result.get("reason") == "side_effect_gate"
    assert result.get("marked_unknown") is True
    attempt = repo._runtime_connect().execute(
        "SELECT status, ended_at FROM agent_attempts WHERE attempt_id = ?",
        (attempt_id,),
    ).fetchone()
    assert attempt["status"] == "unknown"
    assert attempt["ended_at"] > 0
    # 执行权锁保留(不释放——recovered 前新 attempt 无法挂载)
    lock = repo._runtime_connect().execute(
        "SELECT 1 FROM resource_locks WHERE attempt_id = ?", (attempt_id,),
    ).fetchone()
    assert lock is not None
    # 幂等: 再次 reclaim 不再重复标记
    result2 = repo.reclaim_orphaned_attempt(attempt_id, operator="test", reason="x")
    assert result2.get("reason") == "already_terminal"


def test_create_attempt_blocked_while_unknown_prevents_auto_continue(tmp_path):
    """unknown 终态不触发自动续跑(双席核对点3): 自动拉起(create_attempt)
    撞 unknown 闸 fail-closed 抛 RuntimeConflictError + 写诊断事件——
    recovered 前该 run 不会被任何调度器/唤醒轮自动挂载。"""
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    from agent_py_agent.agent.runtime_db.operations import RuntimeConflictError

    repo = RuntimeRepository(tmp_path / "home" / "runtime.db")
    now = 1786006000.0
    rec = repo.record_run_creation(
        owner_id="local/main", goal="g", run_id="run-blocked-mount", role="main"
    )
    attempt_id = str(rec["attempt_id"])
    with repo.transaction() as conn:
        conn.execute(
            "INSERT INTO tool_operations(operation_id, agent_run_id, attempt_id, "
            "attempt_generation, tool_operation_generation, operation_type, "
            "status, created_at, updated_at, outcome_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "op-block-1", str(rec["agent_run_id"]), attempt_id, 1, 1,
                "run_command", "UNKNOWN", now - 100, now - 100, "{}",
            ),
        )
        conn.execute(
            "INSERT INTO resource_locks(lock_id, canonical_scope, holder_instance, "
            "pid, start_token, attempt_id, attempt_generation, workspace_epoch, "
            "tool_operation_generation, lease_expires_at, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "lock-block-1", f"attempt-exec:{rec['agent_run_id']}",
                "test-holder", 99999, "tok", attempt_id, 1, 1, 0,
                now - 3600, now - 3700, now - 3700,
            ),
        )
    assert repo.reclaim_orphaned_attempt(attempt_id, operator="test", reason="x") \
        .get("reason") == "nonterminal_ops"
    # 自动挂载被拒(unknown 闸)
    try:
        repo.create_attempt(str(rec["agent_run_id"]))
        raise AssertionError("create_attempt 应被 unknown 闸拒绝")
    except RuntimeConflictError:
        pass
    # 诊断事件落账
    blocked = repo._runtime_connect().execute(
        "SELECT 1 FROM runtime_events WHERE event_type = 'attempt_unknown_blocked'",
    ).fetchall()
    assert len(blocked) == 1
    # 新 attempt 未创建(run 保持 created + 原 attempt 终态)
    runs = repo._runtime_connect().execute(
        "SELECT status, current_attempt_id FROM agent_runs "
        "WHERE agent_run_id = ?", (str(rec["agent_run_id"]),),
    ).fetchall()
    assert runs[0]["status"] == "created"
    assert runs[0]["current_attempt_id"] == attempt_id


def test_recover_attempt_unknown_releases_lock_and_allows_mount(tmp_path):
    """人工恢复路径(双席证据4): recover_attempt_unknown 显式核对副作用后
    释放执行权锁 + attempt → recovered + 审计事件; 之后 create_attempt
    放行(同 run 新 attempt 挂载)。"""
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

    repo = RuntimeRepository(tmp_path / "home" / "runtime.db")
    now = 1786007000.0
    rec = repo.record_run_creation(
        owner_id="local/main", goal="g", run_id="run-recover", role="main"
    )
    attempt_id = str(rec["attempt_id"])
    with repo.transaction() as conn:
        conn.execute(
            "INSERT INTO tool_operations(operation_id, agent_run_id, attempt_id, "
            "attempt_generation, tool_operation_generation, operation_type, "
            "status, created_at, updated_at, outcome_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "op-rec-1", str(rec["agent_run_id"]), attempt_id, 1, 1,
                "run_command", "UNKNOWN", now - 100, now - 100, "{}",
            ),
        )
        conn.execute(
            "INSERT INTO resource_locks(lock_id, canonical_scope, holder_instance, "
            "pid, start_token, attempt_id, attempt_generation, workspace_epoch, "
            "tool_operation_generation, lease_expires_at, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "lock-rec-1", f"attempt-exec:{rec['agent_run_id']}",
                "test-holder", 99999, "tok", attempt_id, 1, 1, 0,
                now - 3600, now - 3700, now - 3700,
            ),
        )
    assert repo.reclaim_orphaned_attempt(attempt_id, operator="test", reason="x") \
        .get("reason") == "nonterminal_ops"
    # 非法 effect_disposition 拒绝
    bad = repo.recover_attempt_unknown(
        attempt_id, operator="human", effect_disposition="whatever"
    )
    assert bad.get("recovered") is False
    assert bad.get("reason") == "invalid_effect_disposition"
    # 显式恢复(人工核对, 副作用已入账)
    ok = repo.recover_attempt_unknown(
        attempt_id, operator="human-checker", effect_disposition="recorded",
        reason="副作用已人工入账",
    )
    assert ok.get("recovered") is True
    attempt = repo._runtime_connect().execute(
        "SELECT status FROM agent_attempts WHERE attempt_id = ?", (attempt_id,),
    ).fetchone()
    assert attempt["status"] == "recovered"
    # 执行权锁已释放
    lock = repo._runtime_connect().execute(
        "SELECT 1 FROM resource_locks WHERE attempt_id = ?", (attempt_id,),
    ).fetchone()
    assert lock is None
    # 审计事件
    rec_ev = repo._runtime_connect().execute(
        "SELECT payload_json FROM runtime_events WHERE event_type = 'attempt_recovered'",
    ).fetchall()
    assert len(rec_ev) == 1
    assert "recorded" in rec_ev[0]["payload_json"]
    # recover 幂等: 已 recovered 再 recover 拒绝
    again = repo.recover_attempt_unknown(
        attempt_id, operator="human-checker", effect_disposition="confirmed_noop"
    )
    assert again.get("recovered") is False
    assert again.get("reason") == "not_unknown"
    # 恢复后自动挂载放行(同 run 新 attempt)
    new_attempt = repo.create_attempt(str(rec["agent_run_id"]))
    assert str(new_attempt["attempt_id"]) != attempt_id
    assert str(new_attempt["attempt_id"])
    # 账链关联(双席边界①): 新 attempt metadata 记 recovered_from_attempt_id,
    # recovered → 新 attempt 可追溯, 且新 attempt 无 tool ledger 可证未重放
    new_row = repo._runtime_connect().execute(
        "SELECT metadata_json FROM agent_attempts WHERE attempt_id = ?",
        (str(new_attempt["attempt_id"]),),
    ).fetchone()
    import json as _json
    meta = _json.loads(new_row["metadata_json"] or "{}")
    assert meta.get("recovered_from_attempt_id") == attempt_id
    # 新 attempt 无工具操作(未重放原 UNKNOWN op)
    ops = repo._runtime_connect().execute(
        "SELECT count(*) FROM tool_operations WHERE attempt_id = ?",
        (str(new_attempt["attempt_id"]),),
    ).fetchone()
    assert ops[0] == 0


def test_recover_rejects_running_attempt(tmp_path):
    """recover 只接受 unknown 终态——活 attempt(运行中)拒绝, 防误释放。"""
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

    repo = RuntimeRepository(tmp_path / "home" / "runtime.db")
    rec = repo.record_run_creation(
        owner_id="local/main", goal="g", run_id="run-live", role="main"
    )
    attempt_id = str(rec["attempt_id"])
    result = repo.recover_attempt_unknown(
        attempt_id, operator="human", effect_disposition="confirmed_noop"
    )
    assert result.get("recovered") is False
    assert result.get("reason") == "not_unknown"
    # 活 attempt 不受影响
    attempt = repo._runtime_connect().execute(
        "SELECT status FROM agent_attempts WHERE attempt_id = ?", (attempt_id,),
    ).fetchone()
    assert attempt["status"] == "running"


def test_classify_claimed_never_started_not_blocking(tmp_path):
    """未启动 op（CLAIMED 且 handler_started_at=0）不阻塞收口(2026-08-15 根因):
    模型坏块整轮零执行留下的 op, 副作用可证明未发生(G.5)——收口矩阵不再
    把可续跑任务误判 UNKNOWN failed。"""
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

    repo = RuntimeRepository(tmp_path / "home" / "runtime.db")
    now = 1786008000.0
    rec = repo.record_run_creation(
        owner_id="local/main", goal="g", run_id="run-unstarted", role="main"
    )
    attempt_id = str(rec["attempt_id"])
    with repo.transaction() as conn:
        conn.execute(
            "INSERT INTO tool_operations(operation_id, agent_run_id, attempt_id, "
            "attempt_generation, tool_operation_generation, operation_type, "
            "status, handler_started_at, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "op-unstarted-1", str(rec["agent_run_id"]), attempt_id, 1, 1,
                "run_command", "CLAIMED", 0, now - 100, now - 100,
            ),
        )
    # 未启动 CLAIMED 不阻塞
    assert repo.classify_attempt_closeout(attempt_id) == "done"
    # 已启动的 CLAIMED(异常)仍 fail-closed 停手
    with repo.transaction() as conn:
        conn.execute(
            "INSERT INTO tool_operations(operation_id, agent_run_id, attempt_id, "
            "attempt_generation, tool_operation_generation, operation_type, "
            "status, handler_started_at, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "op-started-1", str(rec["agent_run_id"]), attempt_id, 1, 2,
                "run_command", "CLAIMED", now - 50, now - 100, now - 100,
            ),
        )
    assert repo.classify_attempt_closeout(attempt_id) is None
    # EXECUTING/UNKNOWN 仍 fail-closed 停手
    with repo.transaction() as conn:
        conn.execute(
            "INSERT INTO tool_operations(operation_id, agent_run_id, attempt_id, "
            "attempt_generation, tool_operation_generation, operation_type, "
            "status, handler_started_at, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "op-unk-1", str(rec["agent_run_id"]), attempt_id, 1, 3,
                "run_command", "UNKNOWN", now - 50, now - 100, now - 100,
            ),
        )
    assert repo.classify_attempt_closeout(attempt_id) is None


def test_settle_marks_unstarted_claimed_ops_cancelled(tmp_path):
    """终态收口时未启动 CLAIMED op 如实落 CANCELLED(G.5), 不留 UNKNOWN 残账。"""
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

    repo = RuntimeRepository(tmp_path / "home" / "runtime.db")
    now = 1786009000.0
    rec = repo.record_run_creation(
        owner_id="local/main", goal="g", run_id="run-settle-unstarted", role="main"
    )
    attempt_id = str(rec["attempt_id"])
    with repo.transaction() as conn:
        conn.execute(
            "INSERT INTO tool_operations(operation_id, agent_run_id, attempt_id, "
            "attempt_generation, tool_operation_generation, operation_type, "
            "status, handler_started_at, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "op-unstarted-2", str(rec["agent_run_id"]), attempt_id, 1, 1,
                "run_command", "CLAIMED", 0, now - 100, now - 100,
            ),
        )
        conn.execute(
            "INSERT INTO tool_operations(operation_id, agent_run_id, attempt_id, "
            "attempt_generation, tool_operation_generation, operation_type, "
            "status, handler_started_at, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "op-exec-1", str(rec["agent_run_id"]), attempt_id, 1, 2,
                "run_command", "EXECUTING", now - 50, now - 100, now - 100,
            ),
        )
    result = repo.settle_agent_run(
        agent_run_id=str(rec["agent_run_id"]),
        status="done",
        attempt_id=attempt_id,
        payload={"status": "done"},
    )
    assert result.get("settled") is True
    # 未启动 op → CANCELLED + settled_at 落账
    row = repo._runtime_connect().execute(
        "SELECT status, settled_at FROM tool_operations WHERE operation_id = 'op-unstarted-2'",
    ).fetchone()
    assert row["status"] == "CANCELLED"
    assert row["settled_at"] > 0
    # 已启动 EXECUTING op 不被动(保持 fail-closed 原状)
    exec_row = repo._runtime_connect().execute(
        "SELECT status FROM tool_operations WHERE operation_id = 'op-exec-1'",
    ).fetchone()
    assert exec_row["status"] == "EXECUTING"


def test_protocol_repair_exhausted_format_only_continuable(tmp_path):
    """repairs 耗尽的 break: violations 全为 TOOL_CALL_UNCLOSED(纯格式错误)
    → unfinished 可续跑; 其他 violation → blocked fail-closed(2026-08-15 根因)。"""
    from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
        _protocol_violation_decision,
    )

    class _Violation:
        def __init__(self, code):
            self.code = code
            self.source_protocol = "text"

        def to_dict(self):
            return {"code": self.code}

    class _Response:
        text = ""
        backend = "anthropic_compatible"

    class _Params:
        protocol_violation_trace = None
        live_archive_state = {}
        tool_context = []
        max_protocol_repairs = 1
        run_id = ""
        attempt_id = ""

    class _Counters:
        protocol_repairs = 99  # 超过 max_repairs → break 分支

    class _Request:
        response = _Response()
        turn_id = "t1"
        params = _Params()
        counters = _Counters()
        agent = None

    # 纯格式类 → 可续跑(unfinished/TOOL_CALL_UNCLOSED)
    req1 = _Request()
    req1.params.live_archive_state = {}
    decision1 = _protocol_violation_decision(
        req1, (_Violation("TOOL_CALL_UNCLOSED"),)
    )
    # cell3 真机(2026-08-15): text parser 的未闭合块(PROTOCOL_VIOLATION@text)
    # 同属格式类可续跑
    req1b = _Request()
    req1b.params.live_archive_state = {}
    decision1b = _protocol_violation_decision(
        req1b, (_Violation("PROTOCOL_VIOLATION"),)
    )
    assert decision1b.action == "break"
    assert decision1b.response.runtime_status == "unfinished"
    assert decision1b.response.runtime_reason == "TOOL_CALL_UNCLOSED"
    assert decision1.action == "break"
    assert decision1.response.runtime_status == "unfinished"
    assert decision1.response.runtime_reason == "TOOL_CALL_UNCLOSED"
    from agent_py_agent.agent.conversation.runtime import should_continue_task

    assert should_continue_task(decision1.response)[0] is True

    # 其他 violation(如 TOOL_CALL_IN_BODY) → blocked fail-closed
    req2 = _Request()
    req2.params.live_archive_state = {}
    decision2 = _protocol_violation_decision(
        req2, (_Violation("TOOL_CALL_IN_BODY"),)
    )
    assert decision2.action == "break"
    assert decision2.response.runtime_status == "blocked"
    assert decision2.response.runtime_reason == "PROTOCOL_VIOLATION"
    assert should_continue_task(decision2.response)[0] is False


def test_tool_call_unclosed_structural_gate():
    """TOOL_CALL_UNCLOSED 结构门(双席 seq1989): 仅 tool_protocol_adapter +
    unfinished 放行续跑; 其他 source/status 误标同一 reason 拒绝。"""
    from agent_py_agent.agent.conversation.runtime import should_continue_task

    ok = SimpleNamespace(
        runtime_reason="TOOL_CALL_UNCLOSED",
        runtime_source="tool_protocol_adapter",
        runtime_status="unfinished",
    )
    assert should_continue_task(ok)[0] is True
    # 反例: 错误 source / 错误 status / 缺 status
    bad1 = SimpleNamespace(
        runtime_reason="TOOL_CALL_UNCLOSED",
        runtime_source="tool_loop",
        runtime_status="unfinished",
    )
    bad2 = SimpleNamespace(
        runtime_reason="TOOL_CALL_UNCLOSED",
        runtime_source="tool_protocol_adapter",
        runtime_status="blocked",
    )
    bad3 = SimpleNamespace(
        runtime_reason="TOOL_CALL_UNCLOSED",
        runtime_source="tool_protocol_adapter",
        runtime_status="",
    )
    assert should_continue_task(bad1)[0] is False
    assert should_continue_task(bad2)[0] is False
    assert should_continue_task(bad3)[0] is False


def test_settle_unstarted_op_cancelled_outcome_schema_valid(tmp_path):
    """CANCELLED 未启动 op 的 outcome_json schema-valid(双席 seq1989 缺口4):
    重放(replay)能如实读到取消结果, 不因空 result 降级 UNKNOWN。"""
    import json as _json

    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

    repo = RuntimeRepository(tmp_path / "home" / "runtime.db")
    now = 1786010000.0
    rec = repo.record_run_creation(
        owner_id="local/main", goal="g", run_id="run-replay-safe", role="main"
    )
    attempt_id = str(rec["attempt_id"])
    with repo.transaction() as conn:
        conn.execute(
            "INSERT INTO tool_operations(operation_id, agent_run_id, attempt_id, "
            "attempt_generation, tool_operation_generation, operation_type, "
            "status, handler_started_at, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "op-replay-1", str(rec["agent_run_id"]), attempt_id, 1, 1,
                "run_command", "CLAIMED", 0, now - 100, now - 100,
            ),
        )
    assert repo.settle_agent_run(
        agent_run_id=str(rec["agent_run_id"]),
        status="done",
        attempt_id=attempt_id,
        payload={"status": "done"},
    ).get("settled") is True
    row = repo._runtime_connect().execute(
        "SELECT status, outcome_json FROM tool_operations WHERE operation_id = 'op-replay-1'",
    ).fetchone()
    assert row["status"] == "CANCELLED"
    payload = _json.loads(row["outcome_json"])
    # schema-valid: result 带 schema_version + handler_executed=false + not_started
    assert payload["schema"] == "managed_operation.v1"
    result = payload["result"]
    assert result["schema_version"] == "tool_execution_result.v1"
    assert result["handler_executed"] is False
    assert result["effect_outcome"] == "not_started"
    assert result["error_code"] == "TOOL_OPERATION_CANCELLED_NOT_STARTED"
    # 重放语义: _result_from_record 读 result 时 schema_version 匹配 → 不降 UNKNOWN
    # (error_code 不是 TOOL_OPERATION_OUTCOME_UNKNOWN)
    assert result["error_code"] != "TOOL_OPERATION_OUTCOME_UNKNOWN"


def test_cancelled_unstarted_op_replay_stable_not_unknown(tmp_path):
    """replay 实测(双席 seq1990): CANCELLED 未启动 op 的 outcome_json 经
    _result_from_record 重放稳定返回取消结果, 不降级 TOOL_OPERATION_OUTCOME_UNKNOWN。"""
    import json as _json

    from agent_py_agent.agent.local_storage.tool_operations import ToolOperationRecord
    from agent_py_agent.agent.tooling.tool_operation_coordinator import (
        _result_from_record,
    )

    payload = {
        "schema": "managed_operation.v1",
        "result": {
            "schema_version": "tool_execution_result.v1",
            "tool": "run_command",
            "ok": False,
            "output": "未启动(not_started): 整轮零执行, 操作从未执行, 无副作用(G.5 CANCELLED)",
            "error_code": "TOOL_OPERATION_CANCELLED_NOT_STARTED",
            "effect_outcome": "not_started",
            "handler_executed": False,
        },
        "error_code": "TOOL_OPERATION_CANCELLED_NOT_STARTED",
    }
    record = ToolOperationRecord(
        owner_id="local/main", run_id="r1", task_id="", operation_id="op-replay-2",
        tool="run_command", args_hash="h", idempotency_key="k", idempotency_scope="s",
        idempotency_namespace="n", status="CANCELLED", holder_id="h1", holder_host="h",
        holder_pid=1, holder_process_start_token="t", generation=1,
        lease_expires_at=0.0, result=payload["result"], created_at=1.0,
        updated_at=2.0, completed_at=2.0,
    )
    result = _result_from_record(record)
    assert result.error_code != "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert result.error_code == "TOOL_OPERATION_CANCELLED_NOT_STARTED"
    assert result.effect_outcome == "not_started"
    assert result.handler_executed is False
    assert result.ok is False
    # 空 result 的旧记录仍 fail-closed 降 UNKNOWN(不自动重做)——安全边界保持
    record_empty = ToolOperationRecord(
        owner_id="local/main", run_id="r1", task_id="", operation_id="op-replay-3",
        tool="run_command", args_hash="h", idempotency_key="k", idempotency_scope="s",
        idempotency_namespace="n", status="CANCELLED", holder_id="h1", holder_host="h",
        holder_pid=1, holder_process_start_token="t", generation=1,
        lease_expires_at=0.0, result={}, created_at=1.0, updated_at=2.0,
        completed_at=2.0,
    )
    result_empty = _result_from_record(record_empty)
    assert result_empty.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"


def test_unknown_halt_reason_aligns_with_taxonomy(monkeypatch):
    """第三层(2026-08-15 cell1): UNKNOWN halt 收口按触发报码对齐错误合同——
    retryable=True 的已知失败(COMMAND_FAILED)→ REPEATED_TOOL_FAILURE 可续跑;
    真未知(无码/retryable=False)→ TOOL_OPERATION_OUTCOME_UNKNOWN 不续跑。"""
    import agent_py_agent.agent.agent_core._tool_loop_service as svc
    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.agent.conversation.runtime import should_continue_task

    captured = {}

    def _fake_generate(*a, **kw):
        return ModelResponse(text="ok", backend="x")

    def _fake_without(params, final, reason=None):
        return final

    def _fake_build(agent, params):
        return "prompt"

    monkeypatch.setattr(svc, "build_tool_loop_prompt", _fake_build)
    monkeypatch.setattr(svc, "generate_model_response", _fake_generate)
    monkeypatch.setattr(svc, "without_tool_call_after_limit", _fake_without)

    class _P:
        unknown_outcome_halt = ("run_command", "COMMAND_FAILED", "not_started", False)

    # COMMAND_FAILED + 执行器声明非 unknown(not_started/handler 未执行)
    # → REPEATED_TOOL_FAILURE 可续跑
    resp = svc._final_response_after_unknown_outcome_halt(None, _P(), 5)
    final = resp[1]
    assert final.runtime_reason == "REPEATED_TOOL_FAILURE"
    assert should_continue_task(final)[0] is True
    # 双席 seq1992 收紧: handler 真实执行的写命令失败(effect=unknown) →
    # 保持 UNKNOWN 人工核对闸, 不自动续跑
    p1b = _P()
    p1b.unknown_outcome_halt = ("run_command", "COMMAND_FAILED", "unknown", True)
    resp1b = svc._final_response_after_unknown_outcome_halt(None, p1b, 5)
    final1b = resp1b[1]
    assert final1b.runtime_reason == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert should_continue_task(final1b)[0] is False
    # 真未知(无码) → TOOL_OPERATION_OUTCOME_UNKNOWN 不续跑
    p2 = _P()
    p2.unknown_outcome_halt = ("run_command", "", "unknown", True)
    resp2 = svc._final_response_after_unknown_outcome_halt(None, p2, 5)
    final2 = resp2[1]
    assert final2.runtime_reason == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert should_continue_task(final2)[0] is False


def test_unknown_outcome_keeps_reported_output_preview(tmp_path):
    """UNKNOWN 包装保留失败输出有界摘要(2026-08-15 cell1): 模型可见编译错误。"""
    from agent_py_agent.agent.tooling.tool_operation_coordinator import (
        _unknown_outcome_result,
    )
    from agent_py_agent.agent.tooling.models import ToolHandlerOutcome

    class _Req:
        tool_name = "run_command"

    reported = ToolHandlerOutcome(
        "run_command",
        False,
        "compile error line 3: undefined: foo",
        error_code="COMMAND_FAILED",
        handler_executed=True,
    )
    result = _unknown_outcome_result(_Req(), reported)
    import json as _json

    payload = _json.loads(result.output)
    assert payload["reported_error_code"] == "COMMAND_FAILED"
    assert "compile error line 3" in payload["reported_output_preview"]
