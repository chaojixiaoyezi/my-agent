"""网关后台线程循环的永不停机硬保障单测。

真机实锤(1.10):网关进程被 systemd 拉着 active,但干活的后台主循环线程死了再也不回来。
`_gateway_background_main_loop` 的 tick 编排缝隙(种子重扫/owner 池同步/提交/汇报)原先
不在任何保护内,一个异常就杀死整条线程。撤修复即 FAIL:异常直接传播出循环函数。
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

from agent_py_agent.agent.backends.errors import ProviderTransientError
from agent_py_agent.agent.gateway_parts.paths import GatewayPaths
from agent_py_agent.agent.gateway_parts.queue_service import GatewayClaim


def test_background_main_loop_survives_tick_exceptions_and_reports_supply_error(monkeypatch, capsys) -> None:
    from agent_py_agent.cli import gateway_loops

    stop_event = threading.Event()
    ticks = {"count": 0}

    class _ExplodingSupervisor:
        poll_interval = 0.01

        def __init__(self, context) -> None:
            pass

        def tick(self) -> bool:
            ticks["count"] += 1
            if ticks["count"] >= 3:
                stop_event.set()
            raise ProviderTransientError('HTTP 429: rate_limit_error "已达到 Token Plan 用量上限"')

    monkeypatch.setattr(gateway_loops, "_BackgroundMainSupervisor", _ExplodingSupervisor)
    monkeypatch.setattr(gateway_loops, "_start_orphan_reconcile_loop", lambda _context, _stop: None)
    monkeypatch.setattr(gateway_loops, "_start_owner_maintenance_loop", lambda _context, _stop: None)
    monkeypatch.setattr(gateway_loops, "_start_scheduler_due_loop", lambda _context, _stop: None)

    gateway_loops._gateway_background_main_loop(SimpleNamespace(), stop_event)  # 撤修复:异常在这里炸出

    assert ticks["count"] >= 3  # 循环在异常后继续跑,只随 stop_event 退出
    err = capsys.readouterr().err
    assert "[gateway-loop-error]" in err
    assert '"category": "provider_transient"' in err  # 联动分类修复:429 不再误标 programmer_bug
    assert '"recoverable": true' in err
    assert "programmer_bug" not in err


def test_heartbeat_loop_survives_write_failure(monkeypatch) -> None:
    from agent_py_agent.cli import gateway_loops

    stop_event = threading.Event()
    writes = {"count": 0}

    def _failing_write(paths, agent, *, status, pid) -> None:
        writes["count"] += 1
        if writes["count"] >= 2:
            stop_event.set()
        raise OSError("disk full")

    monkeypatch.setattr(gateway_loops, "_write_gateway_heartbeat", _failing_write)
    context = SimpleNamespace(
        paths=SimpleNamespace(),
        agent=SimpleNamespace(config=SimpleNamespace(gateway_heartbeat_interval=0)),
        options=SimpleNamespace(),
    )

    gateway_loops._gateway_heartbeat_loop(context, stop_event)  # 撤修复:OSError 在这里炸出

    assert writes["count"] >= 2  # 写失败不杀心跳线程


def test_request_worker_initialization_error_is_terminalized(tmp_path, monkeypatch) -> None:
    """A failure before the normal request handler must not strand processing state."""
    from agent_py_agent.cli import gateway_loops

    root = tmp_path / "gateway"
    paths = GatewayPaths(
        root=root,
        pid=root / "gateway.pid",
        adapter_pid=root / "adapter.pid",
        state=root / "gateway_state.json",
        heartbeat=root / "gateway_heartbeat.json",
        stop_request=root / "gateway_stop.request",
        log=root / "gateway.log",
        inbox=root / "requests" / "pending",
        processing=root / "requests" / "processing",
        done=root / "requests" / "done",
        failed=root / "requests" / "failed",
        responses=root / "responses",
        history=root / "gateway_requests.jsonl",
    )
    for folder in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses):
        folder.mkdir(parents=True, exist_ok=True)
    processing = paths.processing / "req-ast.json"
    processing.write_text(
        json.dumps(
            {
                "id": "req-ast",
                "kind": "ask",
                "created_at": 1.0,
                "attempts": 1,
                "status": "processing",
                "turn_phase": "open",
                "execution_attempt_id": "attempt-test",
                "lease_epoch": 1,
                "lease_owner": "attempt-test",
            }
        ),
        encoding="utf-8",
    )

    dispatcher = object.__new__(gateway_loops._RequestDispatcher)
    dispatcher.paths = paths

    def explode():
        raise SystemError("AST constructor recursion depth mismatch (before=48, after=53)")

    dispatcher._thread_agent = explode
    released: list[tuple[str, str]] = []
    monkeypatch.setattr(
        gateway_loops.admission,
        "release",
        lambda user_key, *, conversation_key: released.append((user_key, conversation_key)),
    )

    dispatcher._execute(
        GatewayClaim(processing, "req-ast", "attempt-test", 1, "attempt-test"),
        "user-1",
        "conversation-1",
    )

    response = json.loads((paths.responses / "req-ast.json").read_text(encoding="utf-8"))
    archived = json.loads((paths.failed / "req-ast.json").read_text(encoding="utf-8"))
    assert response["status"] == "failed"
    assert response["error_code"] == "GATEWAY_WORKER_UNHANDLED_ERROR"
    assert response["worker_error"]["category"] == "programmer_bug"
    assert archived["status"] == "failed"
    assert processing.exists() is False
    assert released == [("user-1", "conversation-1")]


class _ImmediateFuture:
    """Submit 即执行;done 恒 True(可被 _run_due_curators 清 in-flight)。"""

    def __init__(self, fn, args) -> None:
        self._value = None
        self._error = None
        try:
            self._value = fn(*args)
        except Exception as exc:  # noqa: BLE001 - 测试辅助
            self._error = exc

    def done(self) -> bool:
        return True

    def result(self):
        if self._error is not None:
            raise self._error
        return self._value


class _RunningFuture:
    def done(self) -> bool:
        return False  # 模拟 curator 提取仍在跑(LLM 调用中)


def _curator_supervisor(*, base_calls, scoped_calls, running=()) -> object:
    from agent_py_agent.cli import gateway_loops

    base_agent = SimpleNamespace(
        config=SimpleNamespace(
            owner_maintenance_scan_interval_seconds=60,
            memory_curator_workers=2,
        ),
        memory_curator=SimpleNamespace(run_if_due=lambda: base_calls.append(1)),
        owner_id="local",
    )
    scoped_agent = SimpleNamespace(
        memory_curator=SimpleNamespace(run_if_due=lambda: scoped_calls.append(1)),
        owner_id="owner-a",
    )
    pool = SimpleNamespace(
        hard_agents=lambda: [scoped_agent],
        evict_soft_agent_id=lambda _agent_id: False,
    )

    supervisor = object.__new__(gateway_loops._BackgroundMainSupervisor)
    supervisor._base_agent = base_agent
    supervisor._owner_pool = pool
    supervisor._registry = SimpleNamespace(soft_snapshot=lambda: [])
    supervisor._curator_inflight = {}
    supervisor._next_curator_run_at = 0.0
    supervisor._curator_quota_count = 0
    supervisor._curator_quota_date = ""
    supervisor._curator_quota_path = None
    supervisor._get_curator_executor = lambda: SimpleNamespace(
        submit=lambda fn, *args: _ImmediateFuture(fn, args)
    )
    supervisor._curator_inflight.update({id(agent): _RunningFuture() for agent in running})
    return supervisor, base_agent, scoped_agent


def test_background_main_supervisor_wakes_base_and_scoped_curators_with_throttle() -> None:
    base_calls: list[int] = []
    scoped_calls: list[int] = []
    supervisor, _base, _scoped = _curator_supervisor(
        base_calls=base_calls, scoped_calls=scoped_calls
    )

    supervisor._run_due_curators()
    assert base_calls == [1] and scoped_calls == [1]  # base + 每个活跃 scoped owner 都被唤醒
    assert supervisor._next_curator_run_at > 0.0  # 节流已生效

    supervisor._run_due_curators()  # 节流窗口内再调 → 不重复唤醒
    assert base_calls == [1] and scoped_calls == [1]


def test_background_main_supervisor_curator_dedupes_and_isolates_failures(monkeypatch) -> None:
    from agent_py_agent.cli import gateway_loops

    base_calls: list[int] = []
    scoped_calls: list[int] = []
    supervisor, base_agent, scoped_agent = _curator_supervisor(
        base_calls=base_calls, scoped_calls=scoped_calls
    )

    # base 的 curator 抛异常 → _safe_run_curator 兜住,scoped 照常唤醒(单 owner 失败隔离)
    def _boom():
        raise RuntimeError("curator backend down")

    base_agent.memory_curator.run_if_due = _boom
    supervisor._run_due_curators()
    assert base_calls == [] and scoped_calls == [1]

    # in-flight 去重:某 owner 上一轮还没跑完 → 本轮不重复提交;其余照常
    supervisor._next_curator_run_at = 0.0  # 破节流
    supervisor._curator_inflight[id(scoped_agent)] = _RunningFuture()
    base_agent.memory_curator.run_if_due = lambda: base_calls.append(1)
    supervisor._run_due_curators()
    assert base_calls == [1] and scoped_calls == [1]  # base 被唤醒,scoped 仍在跑被跳过


def test_memory_curator_uses_separate_bounded_lane() -> None:
    from agent_py_agent.cli import gateway_loops

    agents = [
        SimpleNamespace(
            memory_curator=SimpleNamespace(run_if_due=lambda: None),
            owner_id=f"owner-{index}",
        )
        for index in range(4)
    ]
    base_agent = SimpleNamespace(
        config=SimpleNamespace(
            owner_maintenance_scan_interval_seconds=60,
            memory_curator_workers=2,
        ),
        memory_curator=SimpleNamespace(run_if_due=lambda: None),
        owner_id="base",
    )
    submitted: list[str] = []

    class _CapturingExecutor:
        def submit(self, _fn, agent, _label):
            submitted.append(agent.owner_id)
            return _RunningFuture()

    supervisor = object.__new__(gateway_loops._BackgroundMainSupervisor)
    supervisor._base_agent = base_agent
    supervisor._owner_pool = SimpleNamespace(
        hard_agents=lambda: agents,
        evict_soft_agent_id=lambda _agent_id: False,
    )
    supervisor._registry = SimpleNamespace(soft_snapshot=lambda: [])
    supervisor._curator_inflight = {}
    supervisor._next_curator_run_at = 0.0
    supervisor._curator_quota_count = 0
    supervisor._curator_quota_date = ""
    supervisor._curator_quota_path = None
    supervisor._get_curator_executor = lambda: _CapturingExecutor()
    supervisor._get_executor = lambda: (_ for _ in ()).throw(
        AssertionError("curator must not consume the conversation executor")
    )

    supervisor._run_due_curators()

    assert submitted == ["base", "owner-0"]
    assert len(supervisor._curator_inflight) == 2


def test_scheduler_sync_does_not_materialize_curator_only_owners() -> None:
    """纯记忆 owner 只保留轻量身份，不能在 scheduler 同步时批量构建完整 Agent。"""
    from agent_py_agent.cli import gateway_loops

    soft_owner = SimpleNamespace(provider="test", owner_kind="user", owner_id="soft-1")
    built: list[str] = []
    pool = SimpleNamespace(
        get=lambda owner, **_kwargs: built.append(owner.owner_id),
        hard_agents=lambda: [],
    )
    supervisor = object.__new__(gateway_loops._BackgroundMainSupervisor)
    supervisor._registry = SimpleNamespace(
        snapshot=lambda: [soft_owner],
        hard_snapshot=lambda: [],
        soft_snapshot=lambda: [soft_owner],
    )
    supervisor._owner_pool = pool
    supervisor._owner_schedulers = {}
    supervisor._channels = SimpleNamespace()

    supervisor._sync_owner_schedulers()

    assert built == []
    assert supervisor._owner_schedulers == {}


def test_curator_soft_owner_materialization_is_worker_bounded_and_rotates() -> None:
    """软 owner 只按空闲工位构建，完成后释放，并从下一位继续轮转。"""
    from agent_py_agent.cli import gateway_loops

    owners = [
        SimpleNamespace(provider="test", owner_kind="user", owner_id=f"soft-{index}")
        for index in range(5)
    ]
    built: list[str] = []
    soft_agents: dict[str, object] = {}

    def _get(owner, *, hard=False):
        assert hard is False
        agent = SimpleNamespace(
            memory_curator=SimpleNamespace(run_if_due=lambda: None),
            owner_id=owner.owner_id,
        )
        built.append(owner.owner_id)
        soft_agents[owner.owner_id] = agent
        return agent

    def _evict(agent_id):
        for owner_id, agent in list(soft_agents.items()):
            if id(agent) == agent_id:
                soft_agents.pop(owner_id)
                return True
        return False

    submitted: list[str] = []

    class _CapturingExecutor:
        def submit(self, _fn, agent, _label):
            submitted.append(agent.owner_id)
            return _RunningFuture()

    base_agent = SimpleNamespace(
        config=SimpleNamespace(
            owner_maintenance_scan_interval_seconds=60,
            memory_curator_workers=2,
        ),
        memory_curator=SimpleNamespace(run_if_due=lambda: None),
        owner_id="base",
    )
    supervisor = object.__new__(gateway_loops._BackgroundMainSupervisor)
    supervisor._base_agent = base_agent
    supervisor._owner_pool = SimpleNamespace(
        get=_get,
        hard_agents=lambda: [],
        evict_soft_agent_id=_evict,
    )
    supervisor._registry = SimpleNamespace(soft_snapshot=lambda: owners)
    supervisor._curator_inflight = {}
    supervisor._curator_soft_agent_ids = set()
    supervisor._curator_candidate_cursor = 0
    supervisor._next_curator_run_at = 0.0
    supervisor._curator_quota_count = 0
    supervisor._curator_quota_date = ""
    supervisor._curator_quota_path = None
    supervisor._get_curator_executor = lambda: _CapturingExecutor()

    supervisor._run_due_curators()

    assert submitted == ["base", "soft-0"]
    assert built == ["soft-0"]
    assert list(soft_agents) == ["soft-0"]

    for key in list(supervisor._curator_inflight):
        supervisor._curator_inflight[key] = _ImmediateFuture(lambda: None, ())
    supervisor._next_curator_run_at = 0.0
    supervisor._run_due_curators()

    assert "soft-0" not in soft_agents
    assert submitted[-2:] == ["soft-1", "soft-2"]
    assert built == ["soft-0", "soft-1", "soft-2"]
    assert len(soft_agents) == 2


def _quota_supervisor(tmp_path, *, base_calls, scoped_calls):
    """构造配额可注入的 supervisor:base 常规车道、scoped 紧急车道(pending reason)。"""
    from agent_py_agent.cli import gateway_loops

    base_agent = SimpleNamespace(
        config=SimpleNamespace(
            owner_maintenance_scan_interval_seconds=60,
            memory_curator_workers=2,
        ),
        memory_curator=SimpleNamespace(run_if_due=lambda: base_calls.append(1)),
        owner_id="local",
        home_paths=SimpleNamespace(owner_home_dir=""),
    )
    scoped_home = tmp_path / "scoped-home"
    scoped_home.mkdir(parents=True)
    (scoped_home / "memory" / "curator").mkdir(parents=True)
    (scoped_home / "memory" / "curator" / "state.json").write_text(
        json.dumps({"pending_reasons": ["session_close"]}),
        encoding="utf-8",
    )
    scoped_agent = SimpleNamespace(
        memory_curator=SimpleNamespace(run_if_due=lambda: scoped_calls.append(1)),
        owner_id="owner-a",
        home_paths=SimpleNamespace(owner_home_dir=str(scoped_home)),
    )
    pool = SimpleNamespace(
        hard_agents=lambda: [scoped_agent],
        evict_soft_agent_id=lambda _agent_id: False,
    )

    supervisor = object.__new__(gateway_loops._BackgroundMainSupervisor)
    supervisor._base_agent = base_agent
    supervisor._owner_pool = pool
    supervisor._registry = SimpleNamespace(soft_snapshot=lambda: [])
    supervisor._curator_inflight = {}
    supervisor._next_curator_run_at = 0.0
    supervisor._curator_quota_count = 0
    supervisor._curator_quota_date = ""
    supervisor._curator_quota_path = None
    supervisor._get_curator_executor = lambda: SimpleNamespace(
        submit=lambda fn, *args: _ImmediateFuture(fn, args)
    )
    return supervisor, base_agent, scoped_agent


def test_curator_daily_quota_blocks_routine_but_not_urgent(tmp_path) -> None:
    """每日全局配额:常规车道(无 pending reason)满额即跳过;紧急车道(pending reason)不受限。"""
    base_calls: list[int] = []
    scoped_calls: list[int] = []
    supervisor, _base, scoped_agent = _quota_supervisor(
        tmp_path, base_calls=base_calls, scoped_calls=scoped_calls
    )
    # 预置配额满(默认 5000)但日期是昨天 → 本轮 spend 跨天重置,全部放行
    supervisor._curator_quota_date = "2000-01-01"
    supervisor._curator_quota_count = 5000

    supervisor._run_due_curators()
    assert base_calls == [1] and scoped_calls == [1]  # 跨天重置后两个车道都跑

    # 配额当天已满:常规 base 被挡;紧急 scoped(pending_reasons)照跑
    from datetime import datetime, timezone

    supervisor._next_curator_run_at = 0.0  # 破节流
    supervisor._curator_inflight.clear()
    supervisor._curator_quota_date = datetime.now(timezone.utc).date().isoformat()
    supervisor._curator_quota_count = 5000
    supervisor._run_due_curators()
    assert base_calls == [1] and scoped_calls == [1, 1]  # base 被挡;紧急 scoped 照跑


def test_curator_quota_available_checks_but_does_not_record(tmp_path) -> None:
    """配额检查不记账:available 只看剩余额度,实际消耗由 record_consumed 单独记账。"""
    base_calls: list[int] = []
    scoped_calls: list[int] = []
    supervisor, _base, _scoped = _quota_supervisor(
        tmp_path, base_calls=base_calls, scoped_calls=scoped_calls
    )
    supervisor._curator_quota_path = tmp_path / "quota.json"

    assert supervisor._curator_quota_available() is True
    assert not (tmp_path / "quota.json").exists()  # 检查不写盘、不扣次数

    supervisor._curator_quota_count = 5000  # 满额
    assert supervisor._curator_quota_available() is False


def test_curator_quota_record_consumed_persists_to_disk(tmp_path) -> None:
    """实际消耗记账落盘:重启后按文件恢复计数(防超跑)。"""
    base_calls: list[int] = []
    scoped_calls: list[int] = []
    supervisor, _base, _scoped = _quota_supervisor(
        tmp_path, base_calls=base_calls, scoped_calls=scoped_calls
    )
    supervisor._curator_quota_path = tmp_path / "quota.json"

    supervisor._curator_quota_record_consumed()
    payload = json.loads((tmp_path / "quota.json").read_text(encoding="utf-8"))
    assert payload["count"] == 1
    assert payload["date"]  # 当天日期

    # 新实例按文件恢复计数
    from agent_py_agent.cli import gateway_loops

    fresh = object.__new__(gateway_loops._BackgroundMainSupervisor)
    fresh._curator_quota_path = tmp_path / "quota.json"
    fresh._curator_quota_count = 0
    fresh._curator_quota_date = ""
    fresh._load_curator_quota()
    assert fresh._curator_quota_count == 1
    assert fresh._curator_quota_date == payload["date"]


def test_curator_quota_counts_only_real_runs(tmp_path) -> None:
    """配额按实际消耗扣:run_if_due 返回 succeeded/failed(真跑了事务)才记账;
    not_due/busy/disabled(没碰 LLM)不算消耗(旧语义把每次提交检查都扣,真机
    当日配额被推到 3651)。"""
    from agent_py_agent.cli import gateway_loops

    statuses = ["succeeded", "failed", "not_due", "busy", "disabled"]
    counts: dict[str, int] = {}
    for status in statuses:
        result = SimpleNamespace(status=status)
        base_agent = SimpleNamespace(
            config=SimpleNamespace(owner_maintenance_scan_interval_seconds=60),
            memory_curator=SimpleNamespace(run_if_due=lambda: result),
            owner_id="local",
        )
        supervisor = object.__new__(gateway_loops._BackgroundMainSupervisor)
        supervisor._base_agent = base_agent
        supervisor._curator_quota_count = 0
        supervisor._curator_quota_date = ""
        supervisor._curator_quota_path = None
        supervisor._safe_run_curator(base_agent, "test")
        counts[status] = supervisor._curator_quota_count

    assert counts["succeeded"] == 1
    assert counts["failed"] == 1
    assert counts["not_due"] == 0
    assert counts["busy"] == 0
    assert counts["disabled"] == 0


def test_background_main_supervisor_writes_heartbeat(tmp_path) -> None:
    """调度器存活心跳:节流写 ts+pid,诊断「调度循环死了没有」;60s 内不重复写。"""
    base_calls: list[int] = []
    scoped_calls: list[int] = []
    supervisor, _base, _scoped = _curator_supervisor(
        base_calls=base_calls, scoped_calls=scoped_calls
    )
    heartbeat = tmp_path / "heartbeat.json"
    supervisor._heartbeat_path = heartbeat
    supervisor._next_heartbeat_at = 0.0

    supervisor._maybe_write_heartbeat()
    assert heartbeat.is_file()
    payload = json.loads(heartbeat.read_text(encoding="utf-8"))
    assert payload["pid"] == __import__("os").getpid()
    assert int(payload["ts"]) > 0
    first_mtime = heartbeat.stat().st_mtime

    supervisor._maybe_write_heartbeat()  # 节流窗口内 → 不重写
    assert heartbeat.stat().st_mtime == first_mtime
