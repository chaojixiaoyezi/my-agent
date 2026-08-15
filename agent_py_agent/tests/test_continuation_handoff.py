"""CLI→gateway 长任务显式移交(continuation_handoff)测试。

覆盖(2026-08-14 长任务首要约束 owner seq1856/steward seq1857):
- Repository.create_continuation_handoff / pending_continuation_handoffs /
  consume_continuation_handoff: 待接管移交单落账、幂等、CAS 消费防双消费。
- conversation.runtime._consume_pending_handoffs: gateway 调度器扫描接管,
  领到手后用 resume_prompt_for 构造续跑提示、agent.run 执行续跑轮。
- 根因背景: CLI 与 gateway 的 conversation store 按进程 cwd 隔离(真机坐实),
  CLI 预算耗尽后 gateway 看不到 CLI 的 policy → 长任务被截断; runtime.db 是
  owner 级共享权威, handoff 单在 runtime.db 上 CAS 移交不依赖 store 共享。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.runtime_db.repository import RuntimeRepository


@pytest.fixture
def repo(tmp_path):
    return RuntimeRepository(tmp_path / "home" / "runtime.db")


def _handoff(repo, *, seq: int = 1, run_id: str = "run-1", agent_run_id: str = "agentrun-1"):
    return repo.create_continuation_handoff(
        agent_run_id=agent_run_id,
        attempt_id="attempt-1",
        task_run_id="taskrun-1",
        root_run_id=run_id,
        root_request_id="req-1",
        root_thread_id="thread-1",
        root_task_id="task-1",
        user_prompt="继续推进原任务：复刻该项目",
        continuation_seq=seq,
        reason="max_rounds_reached",
    )


# ------------------------------------------------------------ 落账与查询


def test_create_and_list_pending(repo):
    hid = _handoff(repo)
    assert hid
    pending = repo.pending_continuation_handoffs()
    assert len(pending) == 1
    row = pending[0]
    assert row["handoff_id"] == hid
    assert row["task_run_id"] == "taskrun-1"
    assert row["root_task_id"] == "task-1"
    assert row["root_run_id"] == "run-1"
    assert row["root_thread_id"] == "thread-1"
    assert row["root_request_id"] == "req-1"
    assert row["attempt_id"] == "attempt-1"
    assert row["user_prompt"] == "继续推进原任务：复刻该项目"
    assert row["continuation_seq"] == 1
    assert row["reason"] == "max_rounds_reached"
    assert row["consumed_at"] == 0


def test_create_idempotent_same_run_seq_skips(repo):
    first = _handoff(repo, seq=2)
    second = _handoff(repo, seq=2)
    assert first
    assert second == ""
    assert len(repo.pending_continuation_handoffs()) == 1


def test_create_allows_next_seq(repo):
    _handoff(repo, seq=1)
    second = _handoff(repo, seq=2)
    assert second
    pending = repo.pending_continuation_handoffs()
    assert [row["continuation_seq"] for row in pending] == [1, 2]


def test_pending_filters_by_agent_run(repo):
    _handoff(repo, seq=1, agent_run_id="agentrun-1")
    _handoff(repo, seq=1, agent_run_id="agentrun-2", run_id="run-2")
    only = repo.pending_continuation_handoffs(agent_run_id="agentrun-1")
    assert len(only) == 1
    assert only[0]["root_run_id"] == "run-1"


# ------------------------------------------------------------ CAS 消费


def test_consume_cas_only_once(repo):
    hid = _handoff(repo)
    assert repo.consume_continuation_handoff(hid, consumed_by="gateway", now=100.0) is True
    # 已被消费: 同一单第二次消费 CAS 失败
    assert repo.consume_continuation_handoff(hid, consumed_by="other", now=101.0) is False
    assert repo.pending_continuation_handoffs() == []
    row = repo._runtime_connect().execute(
        "SELECT consumed_at, consumed_by FROM continuation_handoffs WHERE handoff_id = ?",
        (hid,),
    ).fetchone()
    assert row["consumed_at"] == 100.0
    assert row["consumed_by"] == "gateway"


def test_consume_missing_handoff(repo):
    assert repo.consume_continuation_handoff("no-such", consumed_by="x", now=1.0) is False


def test_pending_only_unconsumed(repo):
    a = _handoff(repo, seq=1)
    _handoff(repo, seq=2)
    repo.consume_continuation_handoff(a, consumed_by="gateway")
    pending = repo.pending_continuation_handoffs()
    assert len(pending) == 1
    assert pending[0]["continuation_seq"] == 2


# -------------------------------------------------- gateway 调度器接管


def test_scheduler_consumes_and_runs_continuation(repo):
    """gateway 调度器 tick 扫描待接管单 → CAS 领取 → agent.run 续跑轮。"""
    from agent_py_agent.agent.conversation.runtime import _consume_pending_handoffs

    _handoff(repo, seq=3)
    calls: list[tuple[str, dict]] = []

    def fake_run(prompt: str, **kwargs):
        calls.append((prompt, kwargs))
        return SimpleNamespace(runtime_status="ok")

    agent = SimpleNamespace(subagents=SimpleNamespace(runtime_db=repo), run=fake_run)
    scheduler = SimpleNamespace(runtime=SimpleNamespace(agent=agent))
    _consume_pending_handoffs(scheduler, [], 200.0)
    assert len(calls) == 1
    prompt, kwargs = calls[0]
    assert "继续推进原任务：复刻该项目" in prompt
    assert "#4" in prompt or "4" in prompt  # seq=3 → 第 4 轮续跑提示
    assert kwargs["source"] == "gateway"
    assert kwargs["resume_context"] is True
    assert kwargs["save"] is True
    # 移交单已被 CAS 消费, 不重复执行
    assert repo.pending_continuation_handoffs() == []


def test_scheduler_no_pending_no_run(repo):
    from agent_py_agent.agent.conversation.runtime import _consume_pending_handoffs

    calls: list[object] = []

    def fake_run(prompt, **kwargs):
        calls.append(prompt)

    agent = SimpleNamespace(subagents=SimpleNamespace(runtime_db=repo), run=fake_run)
    scheduler = SimpleNamespace(runtime=SimpleNamespace(agent=agent))
    _consume_pending_handoffs(scheduler, [], 300.0)
    assert calls == []


def test_scheduler_fail_silent_no_repo():
    from agent_py_agent.agent.conversation.runtime import _consume_pending_handoffs

    agent = SimpleNamespace(subagents=SimpleNamespace(runtime_db=None))
    scheduler = SimpleNamespace(runtime=SimpleNamespace(agent=agent))
    _consume_pending_handoffs(scheduler, [], 300.0)  # 不抛即通过
