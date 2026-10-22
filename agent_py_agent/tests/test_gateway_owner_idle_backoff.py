"""Q3：空规划指数降频，但新事实、活动车道与真实入站登记不能被退避挡住。"""
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.owner_scoped_pool import ActiveOwnerRegistry, OwnerScopedAgentPool
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.cli import gateway_loops as loops
from agent_py_agent.cli.gateway_lane_retry import BackgroundLaneRetry


# LLM: 只替换模型无关的规划业务，使用真实目录与共享登记本；不构造Agent或执行外部请求。
# 函数用途: 准备带可控单调时钟、来源目录和真实登记表的后台规划者。
def _supervisor(tmp_path, monkeypatch, *, cap=10.0):
    clock, calls, submitted = [100.0], [], []
    monkeypatch.setattr(loops.time, "monotonic", lambda: clock[0])
    root = tmp_path / "conversations"
    store = SimpleNamespace(root=root)
    for attr, name in (("wake_queue_dir", "wake_queue"), ("policies_dir", "progress_policies"),
                       ("tasks_dir", "tasks"), ("observations_dir", "observations")):
        path = root / name
        path.mkdir(parents=True)
        setattr(store, attr, path)
    db = tmp_path / "runtime.db"
    db.write_bytes(b"initial")
    agent = SimpleNamespace(
        config=SimpleNamespace(background_owner_workers=4, background_threads_per_owner=4,
                               background_idle_owner_max_backoff_seconds=cap, sqlite_connection_reuse=False),
        home_paths=SimpleNamespace(owner_home_dir=tmp_path),
        subagents=SimpleNamespace(runtime_db=SimpleNamespace(db_path=db)),
    )
    scheduler = SimpleNamespace(store=store, runtime=SimpleNamespace(agent=agent), ready=())
    scheduler.prepare_tick = lambda **kwargs: calls.append(clock[0])
    scheduler.ready_thread_ids = lambda **kwargs: scheduler.ready
    supervisor = object.__new__(loops._BackgroundMainSupervisor)
    supervisor._base_agent, supervisor._base_scheduler = agent, scheduler
    supervisor._owner_schedulers, supervisor._inflight = {}, {}
    supervisor._lane_retry, supervisor._registry = BackgroundLaneRetry(), ActiveOwnerRegistry()
    supervisor._owner_pool = None
    supervisor._submit_thread_candidates = lambda rows, **kwargs: submitted.extend(rows)
    return supervisor, scheduler, clock, calls, submitted


# LLM: 用单调时钟精确走过每一个截止点，不sleep或放宽次数断言。
# 函数用途: 验证连续空规划按1、2、4、8、10秒退避并封顶。
def test_empty_owner_exponential_backoff_and_cap(tmp_path, monkeypatch):
    supervisor, _scheduler, clock, calls, _submitted = _supervisor(tmp_path, monkeypatch)
    for delay in (1.0, 2.0, 4.0, 8.0, 10.0, 10.0):
        supervisor._submit_ready_thread_ticks()
        assert getattr(supervisor, "_owner_next_check_at", {}).get("base") == pytest.approx(clock[0] + delay)
        before = list(calls)
        clock[0] += delay - 0.01
        supervisor._submit_ready_thread_ticks()
        assert calls == before
        clock[0] += 0.01
    assert len(calls) == 6


# LLM: 只改变真实配置值，关闭或无效上限不能残留旧的等待截止点。
# 函数用途: 验证零上限保留原每拍规划，配置关闭立即清空退避。
def test_disabled_backoff_keeps_original_planning(tmp_path, monkeypatch):
    supervisor, _scheduler, _clock, calls, _submitted = _supervisor(tmp_path, monkeypatch)
    supervisor._submit_ready_thread_ticks()
    supervisor._base_agent.config.background_idle_owner_max_backoff_seconds = 0.0
    for _ in range(3):
        supervisor._submit_ready_thread_ticks()
    assert len(calls) == 4
    assert getattr(supervisor, "_owner_idle_streaks", {}).get("base", 0) == 0


# LLM: 新文件及数据库原地写入均使用真实stat，不把调用过规划门当作唤醒已发现。
# 函数用途: 验证新wake、策略、任务、观察和SQLite写入在截止前立即恢复规划。
@pytest.mark.parametrize("source", ["wake_queue_dir", "policies_dir", "tasks_dir", "observations_dir", "database", "wal"])
def test_new_owner_fact_resets_backoff(tmp_path, monkeypatch, source):
    supervisor, scheduler, clock, calls, _submitted = _supervisor(tmp_path, monkeypatch)
    supervisor._submit_ready_thread_ticks()
    clock[0] += 0.1
    if source in {"database", "wal"}:
        path = Path(str(scheduler.runtime.agent.subagents.runtime_db.db_path) + ("-wal" if source == "wal" else ""))
    else:
        path = getattr(scheduler.store, source) / "new-fact.json"
    path.write_bytes(b"new structured fact")
    supervisor._submit_ready_thread_ticks()
    assert len(calls) == 2
    assert getattr(supervisor, "_owner_idle_streaks", {}).get("base", 0) == 0


# LLM: 真实ActiveOwnerRegistry.record更新时间戳；表内旧身份本身不能永久禁用退避。
# 函数用途: 验证同owner新入站登记立即复位，而仅保留硬身份仍能进入空闲退避。
def test_registry_new_activity_resets_only_that_owner(tmp_path, monkeypatch):
    supervisor, _scheduler, clock, calls, _submitted = _supervisor(tmp_path, monkeypatch)
    owner = OwnerIdentity.local_main()
    supervisor._registry.record(owner)
    supervisor._submit_ready_thread_ticks()
    clock[0] += 0.1
    supervisor._registry.record(OwnerIdentity.provider_user("feishu", "unrelated"))
    supervisor._submit_ready_thread_ticks()
    assert len(calls) == 1
    supervisor._registry.record(owner)
    supervisor._submit_ready_thread_ticks()
    assert len(calls) == 2
    assert getattr(supervisor, "_owner_idle_streaks", {}).get("base", 0) == 0


# LLM: 活动车道及真实owner池租用都只是进程内活动事实，不能改变durable claim或吞掉别的会话。
# 函数用途: 验证正在执行的owner立即取消退避，并继续发现同owner其它就绪会话。
@pytest.mark.parametrize("activity", ["inflight", "pool"])
def test_active_owner_never_waits_for_idle_backoff(tmp_path, monkeypatch, activity):
    supervisor, scheduler, clock, calls, submitted = _supervisor(tmp_path, monkeypatch)
    supervisor._submit_ready_thread_ticks()
    clock[0] += 0.1
    if activity == "inflight":
        supervisor._inflight[("base", "already-running")] = object()
        pin = nullcontext()
    else:
        pool = OwnerScopedAgentPool(supervisor._base_agent.config, tmp_path)
        pool._builder = lambda *args, **kwargs: supervisor._base_agent
        pool.get(OwnerIdentity.local_main())
        supervisor._owner_pool = pool
        pin = pool.pin(supervisor._base_agent)
    scheduler.ready = ("new-thread",)
    with pin:
        supervisor._submit_ready_thread_ticks()
    assert len(calls) == 2
    assert submitted[0][2] == ["new-thread"]
    assert getattr(supervisor, "_owner_idle_streaks", {}).get("base", 0) == 0


# LLM: 未来策略期限来自原progress读取口，退避不能把已知到期点推迟到配置上限。
# 函数用途: 验证下一次规划不晚于已知策略截止点。
def test_backoff_is_bounded_by_known_policy_deadline(tmp_path, monkeypatch):
    supervisor, scheduler, clock, _calls, _submitted = _supervisor(tmp_path, monkeypatch)
    wall_now = loops.time.time()
    scheduler.store.progress = SimpleNamespace(
        list_report=lambda **kwargs: ([SimpleNamespace(next_due_at=wall_now + 0.2)], [])
    )
    supervisor._submit_ready_thread_ticks()
    assert getattr(supervisor, "_owner_next_check_at", {}).get("base", float("inf")) <= clock[0] + 0.21


# LLM: 真实池的缓存驻留不等于pin或执行；只替换昂贵Agent构造，不替换池内语义。
# 函数用途: 复现并防止把所有缓存owner误当活跃导致退避完全失效。
def test_cached_unpinned_owner_can_back_off(tmp_path, monkeypatch):
    supervisor, _scheduler, clock, calls, _submitted = _supervisor(tmp_path, monkeypatch)
    pool = OwnerScopedAgentPool(supervisor._base_agent.config, tmp_path)
    pool._builder = lambda *args, **kwargs: supervisor._base_agent
    pool.get(OwnerIdentity.local_main())
    supervisor._owner_pool = pool
    assert pool.active_agents() == [supervisor._base_agent]
    supervisor._submit_ready_thread_ticks()
    clock[0] += 0.1
    supervisor._submit_ready_thread_ticks()
    assert len(calls) == 1


# LLM: 同时推进wall/monotonic，耗时来自真实prepare调用，不依赖sleep或调度抖动。
# 函数用途: 复现策略在规划期间已到期时不得再额外等待一个规划耗时。
def test_policy_deadline_elapsed_during_planning_is_not_deferred(tmp_path, monkeypatch):
    supervisor, scheduler, clock, _calls, _submitted = _supervisor(tmp_path, monkeypatch)
    wall = [200.0]
    monkeypatch.setattr(loops.time, "time", lambda: wall[0])
    scheduler.store.progress = SimpleNamespace(
        list_report=lambda **kwargs: ([SimpleNamespace(next_due_at=200.2)], [])
    )

    # LLM: 只有规划耗时被注入；来源指纹保持不变，使测试只观察deadline映射。
    # 函数用途: 在一拍内部让已知策略到期。
    def slow_plan(**kwargs):
        clock[0] += 0.5
        wall[0] += 0.5

    scheduler.prepare_tick = slow_plan
    supervisor._submit_ready_thread_ticks()
    assert supervisor._owner_next_check_at["base"] <= clock[0]


# LLM: scope为空也保留真实创建者和closed语义；槽位满只阻止提交，不能阻止资源寿命维护。
# 函数用途: 复现全局执行槽满时规划连接仍须按60秒TTL关闭。
def test_full_execution_slots_do_not_skip_scope_retirement(tmp_path, monkeypatch):
    from agent_py_agent.agent.runtime_db.sqlite_scope import new_persistent_sqlite_scope

    supervisor, _scheduler, clock, _calls, _submitted = _supervisor(tmp_path, monkeypatch)
    scope = new_persistent_sqlite_scope()
    supervisor._owner_planner_sqlite_scopes = {"base": scope}
    supervisor._owner_planner_scope_used_at = {"base": clock[0]}
    supervisor._inflight = {("base", str(index)): object() for index in range(4)}
    clock[0] += 61.0
    supervisor._submit_ready_thread_ticks()
    assert scope.closed
