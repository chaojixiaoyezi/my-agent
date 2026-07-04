"""盯守自唤醒节奏兜底(§8.3:模型选 2700s 间隔→半数用户冻死 的防回归)。

真机实锤:自唤醒间隔 100% 由模型拍脑袋(60s vs 2700s),排太长盯守静默失效——
consumed 死钉、backlog 只涨不消、funnelB 冻死。兜底两半(复用反压同一 backlog 信号):
① 排期封顶:owner 有未判读积压时,"睡过头"的盯守 policy next_due_at 钳到响应上限;
   不改 interval_seconds,backlog 清零回到模型自选节奏。
② 排期自愈:窗口未到期的活跃 backlog 路连 enabled 盯守 policy 都没了 → 机制层重建。
"""

from __future__ import annotations

import json

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.ingestion.wake_backstop import (
    expedite_watch_policies_for_backlog,
    rebuild_missing_watch_policies,
    watch_response_cap_seconds,
)
from agent_py_agent.agent.ingestion.watch_state import new_state, persist_state, state_dir

NOW = 100_000.0


class _HomePaths:
    def __init__(self, owner_home):
        self.owner_home_dir = str(owner_home)


class _Config:
    subagent_watch_interval_seconds = 120
    # scheduler 构造用(集成挂点测试):
    background_claim_ttl_seconds = 600
    background_claim_heartbeat_interval_seconds = 5
    orphan_supervision_interval_seconds = 0


class _Agent:
    def __init__(self, owner_home, store=None):
        self.home_paths = _HomePaths(owner_home)
        self.config = _Config()
        self.conversation_store = store


def _store(tmp_path) -> ConversationStore:
    return ConversationStore(tmp_path / "conversations")


def _watch_policy(store, *, interval: int, now: float = NOW, task_id: str = "task-w1"):
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": f"conv:{task_id}",
            "channel_user_id": "local-main-agent",
            "now": now,
        }
    )
    store.bind_task({"thread_id": thread.thread_id, "task_id": task_id, "goal": "盯守", "now": now})
    return store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": task_id,
            "interval_seconds": interval,
            "route_channel": "internal",
            "route_target": "",
            "now": now,
            "metadata": {"kind": "subagent_progress_watch", "tool": "wait", "watch_run_id": task_id},
        }
    )


def _lane(owner_home, *, url="http://src.example/a", written=50, consumed=0, window=2700, opened_at=NOW - 60.0, closed=False, puller=""):
    state = new_state(owner_home, url, {"watch_window_seconds": window})
    state.opened_at = opened_at
    state.closed = closed
    state.totals["spool_candidates"] = written
    state.last_puller_run_id = puller
    persist_state(state)
    sidecar = state_dir(owner_home) / f"{state.watch_id}.read.json"
    sidecar.write_text(
        json.dumps({"read_seq": 0, "candidates_consumed": consumed, "updated_at": opened_at}),
        encoding="utf-8",
    )
    return state


# ---------------------------------------------------------------------------
# store 原语:单调提前
# ---------------------------------------------------------------------------


def test_expedite_progress_policy_only_moves_earlier(tmp_path) -> None:
    store = _store(tmp_path)
    policy = _watch_policy(store, interval=2700)
    # 提前:钳到 now+120。
    updated = store.expedite_progress_policy(policy.policy_id, due_at=NOW + 120, reason="watch_backlog", now=NOW)
    assert updated is not None
    assert updated.next_due_at == NOW + 120
    assert updated.interval_seconds == 2700  # 模型意图字段不动
    assert updated.metadata["expedite_count"] == 1
    assert updated.metadata["expedite_reason"] == "watch_backlog"
    # 只往早不往晚:目标晚于现值 → 原样返回、不写。
    same = store.expedite_progress_policy(policy.policy_id, due_at=NOW + 999_999, now=NOW)
    assert same is not None
    assert same.next_due_at == NOW + 120
    assert same.metadata["expedite_count"] == 1
    # disabled → None。
    store.disable_progress_policy(policy.policy_id, now=NOW)
    assert store.expedite_progress_policy(policy.policy_id, due_at=NOW + 1, now=NOW) is None


# ---------------------------------------------------------------------------
# ① 排期封顶
# ---------------------------------------------------------------------------


def test_sleepy_watch_policy_capped_when_backlog_pending(tmp_path) -> None:
    # 真机病灶原样:2700s 自选间隔 + spool 有 200+ 未判读候选 → 排期钳到响应上限。
    store = _store(tmp_path)
    owner_home = tmp_path / "owner"
    policy = _watch_policy(store, interval=2700)
    _lane(owner_home, written=231, consumed=48)
    agent = _Agent(owner_home, store)

    actions = expedite_watch_policies_for_backlog(agent, store, [policy], now=NOW)

    cap = watch_response_cap_seconds(agent)
    assert cap == 120
    assert [a["action"] for a in actions] == ["expedited"]
    reloaded = store.get_progress_policy(policy.policy_id)
    assert reloaded.next_due_at == NOW + cap
    assert reloaded.interval_seconds == 2700


def test_no_cap_when_backlog_clear(tmp_path) -> None:
    # 消费者追平(backlog=0)→ 模型自选的长间隔受尊重,一动不动。
    store = _store(tmp_path)
    owner_home = tmp_path / "owner"
    policy = _watch_policy(store, interval=2700)
    _lane(owner_home, written=100, consumed=100)

    actions = expedite_watch_policies_for_backlog(_Agent(owner_home, store), store, [policy], now=NOW)

    assert actions == []
    assert store.get_progress_policy(policy.policy_id).next_due_at == policy.next_due_at


def test_no_cap_for_closed_or_expired_watch(tmp_path) -> None:
    # closed / 窗口+收尾余量已过的 backlog 是死账,不该无限拉人回来判。
    store = _store(tmp_path)
    owner_home = tmp_path / "owner"
    policy = _watch_policy(store, interval=2700)
    _lane(owner_home, url="http://src.example/closed", written=50, consumed=0, closed=True)
    _lane(owner_home, url="http://src.example/expired", written=50, consumed=0, window=600, opened_at=NOW - 1200.0)

    actions = expedite_watch_policies_for_backlog(_Agent(owner_home, store), store, [policy], now=NOW)

    assert actions == []
    assert store.get_progress_policy(policy.policy_id).next_due_at == policy.next_due_at


def test_within_cap_and_non_watch_policies_untouched(tmp_path) -> None:
    store = _store(tmp_path)
    owner_home = tmp_path / "owner"
    _lane(owner_home, written=231, consumed=48)
    agent = _Agent(owner_home, store)
    # 健康 60s policy(cap 内)不动。
    healthy = _watch_policy(store, interval=60, task_id="task-healthy")
    # 外呼渠道的用户定期汇报(非盯守 kind)不碰——哪怕间隔很长。
    thread = store.get_or_create_thread(
        {"canonical_user_id": "user-1", "channel": "feishu", "channel_conversation_id": "chat-r", "channel_user_id": "u1", "now": NOW}
    )
    report = store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-report",
            "interval_seconds": 3600,
            "route_channel": "feishu",
            "route_target": "chat-r",
            "now": NOW,
            "metadata": {"kind": "user_scheduled_report"},
        }
    )

    actions = expedite_watch_policies_for_backlog(agent, store, [healthy, report], now=NOW)

    assert actions == []
    assert store.get_progress_policy(healthy.policy_id).next_due_at == healthy.next_due_at
    assert store.get_progress_policy(report.policy_id).next_due_at == report.next_due_at


def test_unwindowed_longwatch_backlog_still_caps(tmp_path) -> None:
    # 无窗长守(window=0)有活堆着同样兜底;边界由 policy 生命周期(任务终态退休)收。
    store = _store(tmp_path)
    owner_home = tmp_path / "owner"
    policy = _watch_policy(store, interval=7200)
    _lane(owner_home, written=10, consumed=0, window=0)

    actions = expedite_watch_policies_for_backlog(_Agent(owner_home, store), store, [policy], now=NOW)

    assert len(actions) == 1
    assert store.get_progress_policy(policy.policy_id).next_due_at == NOW + 120


# ---------------------------------------------------------------------------
# ② 排期自愈
# ---------------------------------------------------------------------------


def test_rebuild_policy_when_none_scheduled(tmp_path) -> None:
    # 窗口未到期 + backlog>0 + 零 enabled 盯守 policy + 无 running claim → 机制层重建。
    store = _store(tmp_path)
    owner_home = tmp_path / "owner"
    thread = store.get_or_create_thread(
        {"canonical_user_id": "user-1", "channel": "internal", "channel_conversation_id": "conv:run-9", "channel_user_id": "local", "now": NOW}
    )
    store.bind_task({"thread_id": thread.thread_id, "task_id": "run-9", "goal": "盯守", "now": NOW})
    # 同一消费者的两路流:只该重建一份 policy(唤醒轮会 pull 所有路)。
    _lane(owner_home, url="http://src.example/a", written=60, consumed=10, puller="run-9")
    _lane(owner_home, url="http://src.example/b", written=40, consumed=0, puller="run-9")
    agent = _Agent(owner_home, store)

    actions = rebuild_missing_watch_policies(agent, now=NOW)

    assert len(actions) == 1
    assert actions[0]["action"] == "policy_rebuilt"
    policies = store.list_progress_policies(enabled_only=True)
    assert len(policies) == 1
    rebuilt = policies[0]
    assert rebuilt.thread_id == thread.thread_id
    assert rebuilt.task_id == "run-9"
    assert rebuilt.interval_seconds == watch_response_cap_seconds(agent)
    assert rebuilt.metadata["kind"] == "subagent_progress_watch"
    assert rebuilt.metadata["tool"] == "watch_backlog_backstop"
    # 幂等:已有 enabled 盯守 policy → 短路不再建。
    assert rebuild_missing_watch_policies(agent, now=NOW) == []


def test_rebuild_skips_running_claim_and_unlinked_lanes(tmp_path) -> None:
    store = _store(tmp_path)
    owner_home = tmp_path / "owner"
    thread = store.get_or_create_thread(
        {"canonical_user_id": "user-1", "channel": "internal", "channel_conversation_id": "conv:run-c", "channel_user_id": "local", "now": NOW}
    )
    store.bind_task({"thread_id": thread.thread_id, "task_id": "run-c", "goal": "盯守", "now": NOW})
    _lane(owner_home, url="http://src.example/c", written=30, consumed=0, puller="run-c")
    # 线程正有 running claim(唤醒轮在跑)→ 不重建。
    claim = store.claim_background_run(
        {"thread_id": thread.thread_id, "task_id": "run-c", "reason": "test", "lease_seconds": 600, "now": NOW}
    )
    assert claim is not None
    agent = _Agent(owner_home, store)
    assert rebuild_missing_watch_policies(agent, now=NOW) == []
    # 关联不上(puller 没绑过 thread)→ 诚实跳过。
    owner_home2 = tmp_path / "owner2"
    _lane(owner_home2, url="http://src.example/d", written=30, consumed=0, puller="run-unknown")
    assert rebuild_missing_watch_policies(_Agent(owner_home2, store), now=NOW) == []


def test_rebuild_only_for_windowed_active_backlog(tmp_path) -> None:
    store = _store(tmp_path)
    owner_home = tmp_path / "owner"
    thread = store.get_or_create_thread(
        {"canonical_user_id": "user-1", "channel": "internal", "channel_conversation_id": "conv:run-e", "channel_user_id": "local", "now": NOW}
    )
    store.bind_task({"thread_id": thread.thread_id, "task_id": "run-e", "goal": "盯守", "now": NOW})
    _lane(owner_home, url="http://src.example/nowin", written=30, consumed=0, window=0, puller="run-e")
    _lane(owner_home, url="http://src.example/done", written=30, consumed=30, puller="run-e")
    _lane(owner_home, url="http://src.example/over", written=30, consumed=0, window=600, opened_at=NOW - 700.0, puller="run-e")
    _lane(owner_home, url="http://src.example/shut", written=30, consumed=0, closed=True, puller="run-e")

    assert rebuild_missing_watch_policies(_Agent(owner_home, store), now=NOW) == []


# ---------------------------------------------------------------------------
# 调度器挂点:唤醒轮扫描路上真的会钳
# ---------------------------------------------------------------------------


def test_scheduler_due_scan_expedites_sleepy_watch_policy(tmp_path) -> None:
    from agent_py_agent.agent.conversation.runtime import BackgroundMainAgentScheduler

    store = _store(tmp_path)
    owner_home = tmp_path / "owner"
    policy = _watch_policy(store, interval=2700)
    _lane(owner_home, written=231, consumed=48)
    agent = _Agent(owner_home, store)

    class _Runtime:
        def __init__(self):
            self.agent = agent

        def run_once(self, kwargs):  # 不该被走到:policy 未 due
            raise AssertionError("no policy should be due in this scenario")

    scheduler = BackgroundMainAgentScheduler({"runtime": _Runtime(), "store": store, "collaboration_store": None})
    reports: list = []
    scheduler._run_due_policies(reports, set(), NOW)

    assert reports == []
    assert store.get_progress_policy(policy.policy_id).next_due_at == NOW + 120
