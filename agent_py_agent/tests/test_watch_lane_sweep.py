"""编队补岗扫描:窗口未走完 + 岗上 run 终态 → 建接管 run 续盯(全结构化判据)。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from types import SimpleNamespace

from agent.agent_core.orchestration.dispatch.watch_lane_sweep import respawn_dead_watch_lanes
from agent.ingestion.watch_state import list_states, new_state, persist_state


@dataclass
class _StubTask:
    id: str
    status: str
    takeover_by: str = ""


@dataclass
class _StubManager:
    tasks: dict[str, _StubTask]
    created: list = field(default_factory=list)
    create_result_created: bool = True

    def load(self, run_id: str):
        task = self.tasks.get(run_id)
        if task is None:
            raise FileNotFoundError(run_id)
        return task

    def create_takeover_run(self, request):
        self.created.append(request)
        tk_id = f"tk-{request.source_run_id}"
        self.tasks[tk_id] = _StubTask(tk_id, "PLANNING")  # 建出的接管 run 可被 auto_start 加载
        return SimpleNamespace(
            source_run_id=request.source_run_id,
            takeover_run_id=tk_id,
            created=self.create_result_created,
            applied=self.create_result_created,
            message="stub",
        )


def _agent(owner_home, manager):
    # subagents.workspace 缺失 → auto_start_tasks 在 workspace 守卫处安全退出(不spawn真进程),
    # 正好让单测只验"建接管+记账",auto_start 的真实拉起在真机回归里验证。
    return SimpleNamespace(
        home_paths=SimpleNamespace(owner_home_dir=str(owner_home), owner_id="u"),
        subagents=manager,
        conversation_store=None,
        dispatch_subagents=lambda *a, **k: SimpleNamespace(records=[], summary={}),
    )


def _lane(owner_home, url: str, *, window: int, puller: str, opened_ago: float = 60.0, closed: bool = False):
    state = new_state(owner_home, url, {})
    state.watch_window_seconds = window
    state.opened_at = time.time() - opened_ago
    state.last_puller_run_id = puller
    state.closed = closed
    persist_state(state)
    return state


def test_dead_done_lane_gets_takeover_and_respawn_recorded(tmp_path):
    manager = _StubManager(tasks={"run-a": _StubTask("run-a", "DONE")})
    _lane(tmp_path, "http://127.0.0.1:9/pull", window=1200, puller="run-a")
    actions = respawn_dead_watch_lanes(_agent(tmp_path, manager))
    assert len(actions) == 1
    assert actions[0]["takeover_run_id"] == "tk-run-a"
    assert manager.created and manager.created[0].source_run_id == "run-a"
    assert "watch_stream" in manager.created[0].reason
    row = list_states(tmp_path)[0]
    assert row["respawn_count"] == 1


def test_window_complete_lane_left_alone(tmp_path):
    manager = _StubManager(tasks={"run-a": _StubTask("run-a", "DONE")})
    _lane(tmp_path, "http://127.0.0.1:9/pull", window=30, puller="run-a", opened_ago=120.0)
    assert respawn_dead_watch_lanes(_agent(tmp_path, manager)) == []
    assert manager.created == []


def test_window_complete_lane_with_backlog_gets_drain_takeover(tmp_path):
    # P1 消费吞吐:窗口走完时岗上 run 终态、spool 还剩已抬未判完的候选——积压是窗口内
    # 的事件,必须补清账岗把它判完;接管指令讲清「清积压、别重开新窗」。
    manager = _StubManager(tasks={"run-a": _StubTask("run-a", "DONE")})
    state = _lane(tmp_path, "http://127.0.0.1:9/pull", window=30, puller="run-a", opened_ago=120.0)
    state.totals["spool_candidates"] = 12
    persist_state(state)
    actions = respawn_dead_watch_lanes(_agent(tmp_path, manager))
    assert len(actions) == 1
    reason = manager.created[0].reason
    assert "12 条" in reason
    assert "不带 watch_window_seconds" in reason
    assert "窗口未走完" not in reason


def test_window_complete_lane_with_backlog_cleared_stops_respawning(tmp_path):
    # 清账岗的终点:积压清零(ack 追平写入)后不再补岗,不会永续换人。
    import json as _json

    from agent.ingestion.watch_state import state_dir

    manager = _StubManager(tasks={"run-a": _StubTask("run-a", "DONE")})
    state = _lane(tmp_path, "http://127.0.0.1:9/pull", window=30, puller="run-a", opened_ago=120.0)
    state.totals["spool_candidates"] = 12
    persist_state(state)
    sidecar = state_dir(tmp_path) / f"{state.watch_id}.read.json"
    sidecar.write_text(
        _json.dumps({"read_seq": 99, "candidates_consumed": 12, "candidates_acked": 12, "updated_at": time.time() - 9000}),
        encoding="utf-8",
    )
    assert respawn_dead_watch_lanes(_agent(tmp_path, manager)) == []
    assert manager.created == []


def test_windowless_audit_lane_with_backlog_respawned(tmp_path):
    # /audit 保证档的无窗路(window=0):判读员 DONE 但 spool 还有未判积压 → 按积压驱动补岗。
    # 真机实锤:5 路无窗 audit 判读员 DONE 后 respawn_count 恒 0、积压 15k 永久卡死——旧判据
    # window<=0 直接放弃这一整类(与 wake_backstop 已按积压驱动重建 policy 的口径不一致)。
    manager = _StubManager(tasks={"run-a": _StubTask("run-a", "DONE")})
    state = _lane(tmp_path, "http://127.0.0.1:9/pull", window=0, puller="run-a")
    state.audit_guarantee = True
    state.totals["spool_candidates"] = 20  # 无 read.json=0 acked → backlog=20
    persist_state(state)
    actions = respawn_dead_watch_lanes(_agent(tmp_path, manager))
    assert len(actions) == 1
    assert manager.created[0].source_run_id == "run-a"


def test_windowless_audit_lane_backlog_cleared_not_respawned(tmp_path):
    # 无窗 audit 路的终点:积压清零(ack 追平写入)后不再补岗,不会永续换人。
    import json as _json

    from agent.ingestion.watch_state import state_dir

    manager = _StubManager(tasks={"run-a": _StubTask("run-a", "DONE")})
    state = _lane(tmp_path, "http://127.0.0.1:9/pull", window=0, puller="run-a")
    state.audit_guarantee = True
    state.totals["spool_candidates"] = 20
    persist_state(state)
    (state_dir(tmp_path) / f"{state.watch_id}.read.json").write_text(
        _json.dumps({"read_seq": 99, "candidates_consumed": 20, "candidates_acked": 20, "updated_at": time.time() - 9000}),
        encoding="utf-8",
    )
    assert respawn_dead_watch_lanes(_agent(tmp_path, manager)) == []
    assert manager.created == []


def test_windowless_nonaudit_lane_left_alone(tmp_path):
    # 非 audit 的无窗尽力盯守(洪水直通、有损可接受):即便有积压也不补岗,避免为 lossy-OK
    # 的积压反复起判读子代理白烧模型(与两层限流 triage 设计一致)。
    manager = _StubManager(tasks={"run-a": _StubTask("run-a", "DONE")})
    state = _lane(tmp_path, "http://127.0.0.1:9/pull", window=0, puller="run-a")
    state.audit_guarantee = False
    state.totals["spool_candidates"] = 20
    persist_state(state)
    assert respawn_dead_watch_lanes(_agent(tmp_path, manager)) == []
    assert manager.created == []


def test_closed_or_running_or_unmanned_lanes_left_alone(tmp_path):
    manager = _StubManager(tasks={"run-r": _StubTask("run-r", "RUNNING")})
    _lane(tmp_path, "http://127.0.0.1:1/pull", window=1200, puller="run-r")
    _lane(tmp_path, "http://127.0.0.1:2/pull", window=1200, puller="", opened_ago=10.0)
    _lane(tmp_path, "http://127.0.0.1:3/pull", window=1200, puller="run-r", closed=True)
    assert respawn_dead_watch_lanes(_agent(tmp_path, manager)) == []


def test_chain_walk_respawns_latest_dead_takeover(tmp_path):
    manager = _StubManager(tasks={
        "run-a": _StubTask("run-a", "TAKEN_OVER", takeover_by="tk-1"),
        "tk-1": _StubTask("tk-1", "DONE"),
    })
    _lane(tmp_path, "http://127.0.0.1:9/pull", window=1200, puller="run-a")
    actions = respawn_dead_watch_lanes(_agent(tmp_path, manager))
    assert len(actions) == 1
    assert manager.created[0].source_run_id == "tk-1"


def test_existing_alive_takeover_is_idempotent_no_new_action(tmp_path):
    manager = _StubManager(tasks={"run-a": _StubTask("run-a", "DONE")}, create_result_created=False)
    _lane(tmp_path, "http://127.0.0.1:9/pull", window=1200, puller="run-a")
    assert respawn_dead_watch_lanes(_agent(tmp_path, manager)) == []
    assert list_states(tmp_path)[0]["respawn_count"] == 0


def test_solo_main_agent_watch_not_managed(tmp_path):
    manager = _StubManager(tasks={})
    _lane(tmp_path, "http://127.0.0.1:9/pull", window=1200, puller="main-run-xyz")
    assert respawn_dead_watch_lanes(_agent(tmp_path, manager)) == []


# ---------------------------------------------------------------------------
# 活性否决:终态 run ≠ 死岗(真机实锤:正常判读中被误判挂了反复换人 respawn=3)。
# 判据全结构化:消费新鲜时间戳 / enabled policy 存在性 / running claim。
# ---------------------------------------------------------------------------


@dataclass
class _StubStore:
    policies: list = field(default_factory=list)
    claims: dict = field(default_factory=dict)
    threads: dict = field(default_factory=dict)

    def list_progress_policies(self, enabled_only: bool = False):
        return list(self.policies)

    def thread_for_task(self, task_id: str):
        thread_id = self.threads.get(task_id, "")
        return SimpleNamespace(thread_id=thread_id) if thread_id else None

    def load_background_run_claim(self, thread_id: str):
        return self.claims.get(thread_id)


def _lane_pulled(owner_home, url: str, *, puller: str, pulled_ago: float):
    state = _lane(owner_home, url, window=1200, puller=puller)
    state.last_pull_at = time.time() - pulled_ago
    from agent.ingestion.watch_state import persist_state

    persist_state(state)
    return state


def test_done_lane_with_fresh_consumption_not_respawned(tmp_path):
    # 岗上 run 终态 DONE,但 30s 前刚有人 pull(唤醒续驱在岗)——不换人。
    manager = _StubManager(tasks={"run-a": _StubTask("run-a", "DONE")})
    _lane_pulled(tmp_path, "http://127.0.0.1:9/pull", puller="run-a", pulled_ago=30.0)
    assert respawn_dead_watch_lanes(_agent(tmp_path, manager)) == []
    assert manager.created == []


def test_done_lane_with_enabled_policy_not_respawned_until_starved(tmp_path):
    # 消费已停 500s(>2×120 新鲜窗)但 run 名下还有 enabled 循环提醒(续驱排期在场)——
    # 不换人;停摆超过 6×120=720s 视为续驱僵死,照常补岗(防饿死)。
    manager = _StubManager(tasks={"run-a": _StubTask("run-a", "DONE")})
    _lane_pulled(tmp_path, "http://127.0.0.1:9/pull", puller="run-a", pulled_ago=500.0)
    agent = _agent(tmp_path, manager)
    agent.conversation_store = _StubStore(policies=[SimpleNamespace(task_id="run-a")])
    assert respawn_dead_watch_lanes(agent) == []
    # 同样的 policy 在场,但消费停摆 800s(>6×cap):补岗照常发生。
    manager2 = _StubManager(tasks={"run-b": _StubTask("run-b", "DONE")})
    _lane_pulled(tmp_path / "o2", "http://127.0.0.1:8/pull", puller="run-b", pulled_ago=800.0)
    agent2 = _agent(tmp_path / "o2", manager2)
    agent2.conversation_store = _StubStore(policies=[SimpleNamespace(task_id="run-b")])
    actions = respawn_dead_watch_lanes(agent2)
    assert len(actions) == 1 and manager2.created[0].source_run_id == "run-b"


def test_done_lane_with_running_claim_not_respawned(tmp_path):
    # 判读轮进行中(线程有未过期 running claim):即便状态 DONE 且消费暂停也不换人。
    manager = _StubManager(tasks={"run-a": _StubTask("run-a", "DONE")})
    _lane_pulled(tmp_path, "http://127.0.0.1:9/pull", puller="run-a", pulled_ago=500.0)
    agent = _agent(tmp_path, manager)
    agent.conversation_store = _StubStore(
        threads={"run-a": "th-1"},
        claims={"th-1": {"status": "running", "expires_at": time.time() + 60}},
    )
    assert respawn_dead_watch_lanes(agent) == []


def test_stale_done_lane_without_liveness_respawned_with_backlog_line(tmp_path):
    # 真死岗(消费停摆久、无 policy 无 claim)照常补岗;spool 有未判完候选时,
    # 接管指令里带上"先接手缓冲区"的账(继任者不再只续游标)。
    manager = _StubManager(tasks={"run-a": _StubTask("run-a", "DONE")})
    state = _lane_pulled(tmp_path, "http://127.0.0.1:9/pull", puller="run-a", pulled_ago=900.0)
    state.totals["spool_candidates"] = 3  # 已抬升 3 条、无人消费(read.json 不存在=0 acked)
    from agent.ingestion.watch_state import persist_state

    persist_state(state)
    actions = respawn_dead_watch_lanes(_agent(tmp_path, manager))
    assert len(actions) == 1
    reason = manager.created[0].reason
    assert "3 条" in reason and "缓冲区" in reason


# ---------------------------------------------------------------------------
# P2 补全:非终态但卡死的 puller(重启+churn 后 attempt 上限/能力闸把复活排除)——
# 补岗只认终态、复活又排除它 → 夹缝里无人驱动。本扫描接手;可复活/在岗的不抢(不双驱)。
# ---------------------------------------------------------------------------


def _pending_orphan(run_id: str, *, attempts: int) -> SimpleNamespace:
    """非终态 PENDING 孤儿(带复活候选判定要用的完整字段)。attempts≥cap 时复活会被排除。"""
    return SimpleNamespace(
        id=run_id, status="PENDING", takeover_by="", runner_attempts=attempts,
        runner_session={}, runner_active_attempt_id="", failure_type="",
        attributes={"background_start": {}}, verification_status="", channel_status="",
        capability_requests=[], capability_gaps=[],
    )


def test_stuck_pending_capped_puller_respawned(tmp_path):
    # PENDING 但 attempts 超上限(churn 打满)→ auto_start 复活排除、补岗原只认终态 → 夹缝。
    # 本棒:消费停摆 + 不活 + 复活拉不起来 → 建接管(fresh run)打破死锁。
    manager = _StubManager(tasks={})
    manager.tasks["run-p"] = _pending_orphan("run-p", attempts=5)
    _lane_pulled(tmp_path, "http://127.0.0.1:9/pull", puller="run-p", pulled_ago=900.0)
    actions = respawn_dead_watch_lanes(_agent(tmp_path, manager))
    assert len(actions) == 1
    assert manager.created[0].source_run_id == "run-p"
    assert "停摆无法复活" in manager.created[0].reason


def test_revivable_pending_puller_not_respawned(tmp_path):
    # PENDING 且 attempts 未过闸(auto_start 能复活)→ 交给复活通道,补岗不抢(防双驱)。
    manager = _StubManager(tasks={})
    manager.tasks["run-q"] = _pending_orphan("run-q", attempts=0)
    _lane_pulled(tmp_path, "http://127.0.0.1:9/pull", puller="run-q", pulled_ago=900.0)
    assert respawn_dead_watch_lanes(_agent(tmp_path, manager)) == []
    assert manager.created == []


def test_stuck_pending_with_fresh_consumption_not_respawned(tmp_path):
    # PENDING 卡死态,但 30s 前刚有人 pull(在岗消费)→ 不抢。
    manager = _StubManager(tasks={})
    manager.tasks["run-p"] = _pending_orphan("run-p", attempts=5)
    _lane_pulled(tmp_path, "http://127.0.0.1:9/pull", puller="run-p", pulled_ago=30.0)
    assert respawn_dead_watch_lanes(_agent(tmp_path, manager)) == []
    assert manager.created == []
