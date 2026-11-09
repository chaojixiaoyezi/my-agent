# LLM: 真实 owner 目录、分页与 due 判定；仅替换维护副作用及 gateway 自有时间代理，不造冷 Agent。
# 模块用途: 覆盖多页追赶、优先续作、游标保留、时间与页数边界及异常隔离。
import json
from collections import Counter
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.owner_maintenance import OwnerMaintenanceResult
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home
from agent_py_agent.cli import gateway_loops


# LLM: 复制既有 maintenance_clock 的代理模式，不修改全局 time 或启动保护逻辑。
# 函数用途: 提供可手动推进的 gateway 单调时钟。
@pytest.fixture
def maintenance_clock(monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    gateway_time = SimpleNamespace(**vars(gateway_loops.time))
    gateway_time.monotonic = lambda: clock.now
    monkeypatch.setattr(gateway_loops, 'time', gateway_time)
    return clock


# LLM: 保留真实解析、扫描和 maintenance.json due 输入；维护替身只记录调用、落合成 marker 并返回原 DTO。
# 函数用途: 创建指定数量的 owner 与可观察调度控制器。
def _controller(tmp_path, monkeypatch, maintenance_clock, count, *, partial=()):
    home = ensure_my_agent_home(tmp_path)
    owners = [ensure_owner_home(home.root, OwnerIdentity.provider_user('feishu', f'owner-{i:03d}')) for i in range(count)]
    calls = []
    agent = SimpleNamespace(home_paths=home, config=SimpleNamespace(
        owner_maintenance_scan_interval_seconds=60, owner_agent_pool_max_agents=64))
    monkeypatch.setattr(gateway_loops, '_gateway_agent_from_context', lambda context: agent)

    # LLM: 替代昂贵业务副作用而不替代分页或 due；partial 值写入真实 owner 的 marker。
    # 函数用途: 记录被访问的隔离目录并写入合成维护状态。
    def run(scoped_home, *, now):
        calls.append(scoped_home.owner_home_dir)
        (scoped_home.owner_data_dir / 'maintenance.json').write_text(json.dumps({
            'last_attempt_at': now,
            'maintenance_partial': scoped_home.owner_home_dir.name in partial,
        }))
        return OwnerMaintenanceResult(ran=True, status='success')

    monkeypatch.setattr(gateway_loops, 'run_owner_retention_if_due', run)
    controller = gateway_loops._GatewayOwnerMaintenanceController(SimpleNamespace())
    maintenance_clock.now += gateway_loops.STARTUP_GRACE_WINDOW_SECONDS + 1
    return controller, home, owners, calls


# LLM: 真实10 owner目录跨多个4项页面，末页回绕时必须停，不增加摘要字段。
# 函数用途: 验证一拍不再只访问4个 owner。
def test_one_tick_visits_ten_owners_and_preserves_summary(tmp_path, monkeypatch, maintenance_clock):
    controller, home, owners, calls = _controller(tmp_path, monkeypatch, maintenance_clock, 10)
    result = controller.tick(now=200_000)
    assert calls == [home.owner_home_dir, *(o.home_dir for o in owners)]
    assert result == {'scanned': 10, 'ran': 11, 'failed': 0, 'refused': 0, 'isolated': 0}
    assert controller._cursor is None
    assert not controller._priority


# LLM: 真实分页上限缩成一页，冻结时间不触发另一预算；连续拍必须从保存游标续扫。
# 函数用途: 验证页数边界及游标推进而不是每拍重扫第一页。
def test_page_cap_advances_cursor_across_ticks(tmp_path, monkeypatch, maintenance_clock):
    controller, home, owners, calls = _controller(tmp_path, monkeypatch, maintenance_clock, 10)
    monkeypatch.setattr(gateway_loops, '_OWNER_MAINTENANCE_MAX_PAGES_COUNT', 1)
    first = controller.tick(now=200_000)
    cursor = controller._cursor
    assert cursor is not None and first['scanned'] == 4
    second = controller.tick(now=200_000)
    assert controller._cursor != cursor and second['scanned'] == 4
    assert calls == [home.owner_home_dir, *(o.home_dir for o in owners[:4]),
                     home.owner_home_dir, *(o.home_dir for o in owners[4:8])]
    assert controller.tick(now=200_000)['scanned'] == 2
    assert controller._cursor is None


# LLM: marker 保持 partial，真实 due 判定触发优先队列；第二拍同一 owner 不因正常分页重复执行。
# 函数用途: 验证优先续作在普通第一页之前且去重。
def test_partial_owner_revisited_first_and_duplicates_collapsed(tmp_path, monkeypatch, maintenance_clock):
    controller, home, owners, calls = _controller(tmp_path, monkeypatch, maintenance_clock, 10, partial={'owner-009'})
    controller.tick(now=200_000)
    key = ('feishu', 'user', 'owner-009')
    assert list(controller._priority) == [key]
    calls.clear()
    result = controller.tick(now=200_001)
    assert calls[:2] == [home.owner_home_dir, owners[-1].home_dir]
    assert Counter(calls) == Counter([home.owner_home_dir, *(o.home_dir for o in owners)])
    assert list(controller._priority) == [key]
    assert result['scanned'] == 10 and result['ran'] == 11


# LLM: 用真实64项常量和70个partial输入验证容量与 FIFO，不以缩小常量代替实际上限。
# 函数用途: 验证优先队列最多64项，续作顺序稳定且普通分页仍覆盖其余owner。
def test_priority_is_fifo_and_capped_at_sixty_four(tmp_path, monkeypatch, maintenance_clock):
    names = {f'owner-{i:03d}' for i in range(70)}
    controller, home, owners, calls = _controller(tmp_path, monkeypatch, maintenance_clock, 70, partial=names)
    controller.tick(now=200_000)
    assert len(controller._priority) == gateway_loops._OWNER_MAINTENANCE_PRIORITY_OWNERS_COUNT == 64
    queued = list(controller._priority)
    assert [key[2] for key in queued] == [f'owner-{i:03d}' for i in range(64)]
    calls.clear()
    controller.tick(now=200_001)
    assert calls[:65] == [home.owner_home_dir, *(o.home_dir for o in owners[:64])]
    assert Counter(calls) == Counter([home.owner_home_dir, *(o.home_dir for o in owners)])
    assert list(controller._priority) == queued


# LLM: 先有真实续扫游标，再让每次monotonic观察推进25秒；预算耗尽也必须完成一页，下一拍从新游标继续。
# 函数用途: 验证时间耗尽不阻止发现，亦不重置或跳过已保存分页游标。
def test_advancing_clock_still_discovers_one_page_and_advances_cursor(tmp_path, monkeypatch, maintenance_clock):
    controller, home, owners, calls = _controller(tmp_path, monkeypatch, maintenance_clock, 10)
    monkeypatch.setattr(gateway_loops, '_OWNER_MAINTENANCE_MAX_PAGES_COUNT', 1)
    controller.tick(now=200_000)
    cursor = controller._cursor
    assert cursor is not None
    calls.clear()

    # LLM: 单独推进 gateway 的代理时钟，不影响底层文件操作与其它模块时间。
    # 函数用途: 模拟每次时钟观察之间已过去25秒。
    def jump():
        current = maintenance_clock.now
        maintenance_clock.now += 25
        return current

    monkeypatch.setattr(gateway_loops.time, 'monotonic', jump)
    assert controller.tick(now=200_001)['scanned'] == 4
    assert calls == []  # 每次观测跳25秒：base准入前deadline已尽，也不能再启动。
    assert [key[2] for key in controller._priority] == [f'owner-{i:03d}' for i in range(4, 8)]
    assert controller._cursor is not None and controller._cursor != cursor
    monkeypatch.setattr(gateway_loops.time, 'monotonic', lambda: maintenance_clock.now)
    calls.clear()
    assert controller.tick(now=200_002)['scanned'] == 2
    assert calls == [home.owner_home_dir, *(o.home_dir for o in owners[4:])]
    assert not controller._priority and controller._cursor is None


# LLM: 真实优先续作过程中由维护回调耗尽时间；发现仍处理一页，但不重复执行已经优先访问的 owner。
# 函数用途: 验证超时后发现仍前进，优先 FIFO 与同拍去重保持。
def test_priority_overrun_preserves_fifo_and_discovery_deduplication(tmp_path, monkeypatch, maintenance_clock):
    names = {'owner-000', 'owner-001', 'owner-002'}
    controller, home, owners, calls = _controller(tmp_path, monkeypatch, maintenance_clock, 3, partial=names)
    controller.tick(now=200_000)
    original = gateway_loops.run_owner_retention_if_due

    # LLM: 仅对一个scoped owner模拟耗时，基础owner及真实due分类不变。
    # 函数用途: 在第一个优先owner之后耗尽本拍预算。
    def slow(scoped, *, now):
        result = original(scoped, now=now)
        if scoped.owner_home_dir == owners[0].home_dir:
            maintenance_clock.now += 25
        return result

    monkeypatch.setattr(gateway_loops, 'run_owner_retention_if_due', slow)
    calls.clear()
    assert controller.tick(now=200_001)['scanned'] == 3
    assert calls == [home.owner_home_dir, owners[0].home_dir]
    assert [k[2] for k in controller._priority] == ['owner-001', 'owner-002', 'owner-000']
    calls.clear()
    controller.tick(now=200_002)
    assert calls == [home.owner_home_dir, owners[1].home_dir, owners[2].home_dir, owners[0].home_dir]
    assert len(calls) == len(set(calls))


# LLM: 普通分页和due保持真实；单owner业务失败不入优先队列也不得中止后续owner。
# 函数用途: 验证维护失败隔离。
def test_owner_failure_does_not_stop_following_owners(tmp_path, monkeypatch, maintenance_clock, capsys):
    controller, home, owners, calls = _controller(tmp_path, monkeypatch, maintenance_clock, 3)
    original = gateway_loops.run_owner_retention_if_due

    # LLM: 在第一个scoped owner抛明确业务异常，其它owner沿原测试维护回调。
    # 函数用途: 提供单owner执行失败观察点。
    def broken(scoped, *, now):
        if scoped.owner_home_dir == owners[0].home_dir:
            raise OSError('synthetic owner maintenance failure')
        return original(scoped, now=now)

    monkeypatch.setattr(gateway_loops, 'run_owner_retention_if_due', broken)
    result = controller.tick(now=200_000)
    assert calls == [home.owner_home_dir, owners[1].home_dir, owners[2].home_dir]
    assert result['scanned'] == 3 and result['ran'] == 3
    assert not controller._priority
    assert 'gateway_owner_maintenance.owner' in capsys.readouterr().err


# LLM: 成功结果已在reports中；due分类读取失败只阻止入队，不抹掉执行结果。
# 函数用途: 验证优先分类失败安全回退到普通分页。
def test_due_error_does_not_enqueue_or_lose_report(tmp_path, monkeypatch, maintenance_clock):
    controller, home, owners, calls = _controller(tmp_path, monkeypatch, maintenance_clock, 2)

    # LLM: 只替换额外due检查的读取故障，不替换维护执行返回值或发现结果。
    # 函数用途: 模拟优先资格无法确认。
    def unavailable(*args, **kwargs):
        raise OSError('synthetic due read unavailable')

    monkeypatch.setattr(gateway_loops, 'owner_maintenance_due', unavailable)
    assert controller.tick(now=200_000) == {'scanned': 2, 'ran': 3, 'failed': 0, 'refused': 0, 'isolated': 0}
    assert len(calls) == 3 and not controller._priority


# LLM: 用真实优先 FIFO、发现分页与代理时钟覆盖半拍预算及单个 owner 超时；不替换调度入口。
# 函数用途: 优先续作再忙也须推进至少一页发现，且未访问的优先项不丢失。
@pytest.mark.parametrize('elapsed', [11, 25], ids=['half-budget', 'overrun'])
def test_priority_cannot_starve_discovery(tmp_path, monkeypatch, maintenance_clock, elapsed):
    partial = {f'owner-{i:03d}' for i in range(6, 10)}
    controller, home, owners, calls = _controller(tmp_path, monkeypatch, maintenance_clock, 10, partial=partial)
    controller.tick(now=200_000)
    assert [key[2] for key in controller._priority] == sorted(partial)
    original = gateway_loops.run_owner_retention_if_due
    monkeypatch.setattr(gateway_loops, '_OWNER_MAINTENANCE_MAX_PAGES_COUNT', 1)

    # 只让真实优先候选消耗虚拟时间，避免用 sleep 制造时序不稳定。
    def slow_priority(scoped_home, *, now):
        result = original(scoped_home, now=now)
        if scoped_home.owner_home_dir in {owner.home_dir for owner in owners[6:]}:
            maintenance_clock.now += elapsed
        return result

    monkeypatch.setattr(gateway_loops, 'run_owner_retention_if_due', slow_priority)
    calls.clear()
    result = controller.tick(now=200_001)
    assert result['scanned'] == 4
    expected = [home.owner_home_dir, owners[6].home_dir]
    if elapsed < gateway_loops._OWNER_MAINTENANCE_TICK_BUDGET_SECONDS:
        expected.extend(owner.home_dir for owner in owners[:4])
    assert calls == expected
    assert controller._cursor is not None
    pending = ['owner-007', 'owner-008', 'owner-009', 'owner-006']
    if elapsed >= gateway_loops._OWNER_MAINTENANCE_TICK_BUDGET_SECONDS:
        pending.extend(f'owner-{i:03d}' for i in range(4))
    assert [key[2] for key in controller._priority] == pending
    assert len(calls) == len(set(calls))
    if elapsed >= gateway_loops._OWNER_MAINTENANCE_TICK_BUDGET_SECONDS:
        # 继续保持旧owner每次25秒overrun，四个发现债务仍须在至多五拍获得服务。
        served = set()
        for tick in range(5):
            calls.clear()
            controller.tick(now=200_002 + tick)
            assert len(calls) == len(set(calls)) and len(controller._priority) <= 64
            served.update(calls)
        assert all(owner.home_dir in served for owner in owners[:4])


# LLM: 真实维护先填满64项优先队列，再让每次维护消耗1秒；分页和入队仍用原实现。
# 函数用途: 验证满优先队列最多使用半拍，并给不在优先队列的新 owner 留下发现机会。
def test_sixty_four_priority_owners_leave_half_budget_for_discovery(tmp_path, monkeypatch, maintenance_clock):
    partial = {f'owner-{index:03d}' for index in range(6, 70)}
    controller, home, owners, calls = _controller(tmp_path, monkeypatch, maintenance_clock, 70, partial=partial)
    controller.tick(now=200_000)
    assert len(controller._priority) == gateway_loops._OWNER_MAINTENANCE_PRIORITY_OWNERS_COUNT == 64
    original = gateway_loops.run_owner_retention_if_due

    def one_second(scoped_home, *, now):
        result = original(scoped_home, now=now)
        maintenance_clock.now += 1
        return result

    monkeypatch.setattr(gateway_loops, 'run_owner_retention_if_due', one_second)
    monkeypatch.setattr(gateway_loops, '_OWNER_MAINTENANCE_MAX_PAGES_COUNT', 1)
    calls.clear()
    summary = controller.tick(now=200_001)
    assert summary['scanned'] == 4
    # base同样计入整体预算：1秒base+9秒优先项恰到半拍，仍留一页发现。
    assert calls == [home.owner_home_dir, *(owner.home_dir for owner in owners[6:15]),
                     *(owner.home_dir for owner in owners[:4])]
    assert len(calls) == len(set(calls))
    assert len(controller._priority) == 64
    assert [key[2] for key in controller._priority] == [
        *(f'owner-{index:03d}' for index in range(15, 70)),
        *(f'owner-{index:03d}' for index in range(6, 15)),
    ]
    assert controller._cursor is not None


@pytest.mark.parametrize('style', ['keyword', 'positional', 'varargs', 'kwargs'])
def test_controller_transports_exact_config_to_base_and_scoped_owners(tmp_path, monkeypatch, maintenance_clock, style):
    controller, home, owners, calls = _controller(tmp_path, monkeypatch, maintenance_clock, 2)
    original, seen = gateway_loops.run_owner_retention_if_due, []
    config = controller._base_agent.config

    def run(scoped_home, received, now):
        seen.append(received)
        return original(scoped_home, now=now)

    if style == 'keyword':
        def callback(scoped_home, *, now, config):
            return run(scoped_home, config, now)
    elif style == 'positional':
        def callback(scoped_home, settings, /, *, now):
            return run(scoped_home, settings, now)
    elif style == 'varargs':
        def callback(scoped_home, *settings, now):
            return run(scoped_home, settings[0], now)
    else:
        def callback(scoped_home, **options):
            return run(scoped_home, options['config'], options['now'])

    monkeypatch.setattr(gateway_loops, 'run_owner_retention_if_due', callback)
    assert controller.tick(now=200_000)['ran'] == 3
    assert calls == [home.owner_home_dir, *(owner.home_dir for owner in owners)]
    assert len(seen) == 3 and all(received is config for received in seen)


@pytest.mark.parametrize('scoped', [False, True])
def test_controller_internal_typeerror_runs_once_and_preserves_isolation(tmp_path, monkeypatch, maintenance_clock, scoped, capsys):
    controller, home, owners, calls = _controller(tmp_path, monkeypatch, maintenance_clock, 2)
    original, seen = gateway_loops.run_owner_retention_if_due, []
    target = owners[0].home_dir if scoped else home.owner_home_dir
    error = TypeError('maintenance callback body failed')

    def broken(scoped_home, *, now, config=None):
        seen.append(scoped_home.owner_home_dir)
        if scoped_home.owner_home_dir == target:
            raise error
        return original(scoped_home, now=now)

    monkeypatch.setattr(gateway_loops, 'run_owner_retention_if_due', broken)
    if scoped:
        assert controller.tick(now=200_000)['ran'] == 2
        assert calls == [home.owner_home_dir, owners[1].home_dir]
        assert 'gateway_owner_maintenance.owner' in capsys.readouterr().err
    else:
        with pytest.raises(TypeError) as raised:
            controller.tick(now=200_000)
        assert raised.value is error and not calls
    assert seen.count(target) == 1


def test_full_continuously_due_fifo_cannot_swallow_discovery_debt(tmp_path, monkeypatch, maintenance_clock):
    partial = {f'owner-{i:03d}' for i in range(64)}
    controller, home, owners, calls = _controller(tmp_path, monkeypatch, maintenance_clock, 64, partial=partial)
    controller.tick(now=200_000)
    assert len(controller._priority) == 64
    newcomers = [ensure_owner_home(home.root, OwnerIdentity.provider_user('feishu', f'z-new-{i}')) for i in range(3)]
    original = gateway_loops.run_owner_retention_if_due
    starts = []

    def overrun(scoped, *, now):
        starts.append((scoped.owner_home_dir, maintenance_clock.now))
        result = original(scoped, now=now)
        if scoped.owner_home_dir != home.owner_home_dir:
            maintenance_clock.now += 25
        return result

    monkeypatch.setattr(gateway_loops, 'run_owner_retention_if_due', overrun)
    calls.clear()
    # 64旧owner/每页4项，最迟第17拍发现新页；后继新/旧债交替，三项最迟第22拍兑现。
    for tick in range(22):
        began = maintenance_clock.now
        starts.clear()
        controller.tick(now=200_001 + tick)
        assert len(controller._priority) <= 64
        assert all(start - began < 20 for _, start in starts)
    assert all(calls.count(owner.home_dir) == 1 for owner in newcomers)
    assert all(key[2] in partial for key in controller._priority)


def test_due_hints_do_not_expand_a_full_priority_fifo(tmp_path, monkeypatch, maintenance_clock):
    import threading

    partial = {f'owner-{i:03d}' for i in range(64)}
    controller, home, owners, calls = _controller(tmp_path, monkeypatch, maintenance_clock, 64, partial=partial)
    controller.tick(now=200_000)
    expected = list(controller._priority)
    assert len(expected) == 64
    keys = [('feishu', 'user', f'z-hint-{i:03d}') for i in range(64)]
    hints = SimpleNamespace(
        _closed_event=threading.Event(), _scheduler_changes=0, _hint_failures=0, backstop=60,
        index=SimpleNamespace(priority_owners=lambda **kwargs: keys,
                              row=lambda key: {'next_maintenance_at': 0}),
        note_maintenance=lambda *args, **kwargs: None,
    )
    controller._wake_hints = hints
    controller._wake_event = threading.Event()
    controller._hint_kind = 'maintenance'

    def inspect():
        assert list(controller._priority) == expected
        return {}

    monkeypatch.setattr(controller, 'tick', inspect)
    monkeypatch.setattr(gateway_loops, '_run_loop_with_backoff', lambda stop, tick, site: tick())
    gateway_loops._run_hinted_controller(controller, threading.Event(),
        gateway_loops._LoopSite('owner-maintenance', 'test', 'test', 60))


def test_discovery_debt_recovery_deduplicates_priority_overlap(tmp_path, monkeypatch, maintenance_clock):
    import threading

    partial = {f'owner-{i:03d}' for i in range(64)}
    controller, home, owners, calls = _controller(tmp_path, monkeypatch, maintenance_clock, 64, partial=partial)
    controller.tick(now=200_000)
    newcomer = ensure_owner_home(home.root, OwnerIdentity.provider_user('feishu', 'owner-000a'))
    controller._cursor = ('feishu', 'user', 'owner-000')
    recovered = threading.Event()
    original = gateway_loops.run_owner_retention_if_due

    def transition(scoped, *, now):
        result = original(scoped, now=now)
        if scoped.owner_home_dir == owners[0].home_dir and not recovered.is_set():
            maintenance_clock.now += 25
        return result

    monkeypatch.setattr(gateway_loops, 'run_owner_retention_if_due', transition)
    controller.tick(now=200_001)
    assert controller._discovery_debt and controller._cursor == ('feishu', 'user', 'owner-000')
    recovered.set()
    partial.remove('owner-001')  # 同一恢复拍既有完成者，也有仍due者。
    calls.clear()
    controller.tick(now=200_002)
    assert Counter(calls) == Counter([home.owner_home_dir, newcomer.home_dir, *(owner.home_dir for owner in owners)])
    assert len(calls) == len(set(calls)) and len(controller._priority) == 63
    assert ('feishu', 'user', 'owner-001') not in controller._priority
    assert ('feishu', 'user', 'owner-002') in controller._priority


def test_continuous_discovery_overruns_also_serve_old_fifo(tmp_path, monkeypatch, maintenance_clock):
    partial = {f'owner-{i:03d}' for i in range(64)}
    controller, home, owners, calls = _controller(tmp_path, monkeypatch, maintenance_clock, 64, partial=partial)
    controller.tick(now=200_000)
    newcomers = [ensure_owner_home(home.root, OwnerIdentity.provider_user('feishu', f'z-new-{i:03d}')) for i in range(16)]
    controller._cursor = ('feishu', 'user', 'owner-063')
    original = gateway_loops.run_owner_retention_if_due

    def overrun(scoped, *, now):
        result = original(scoped, now=now)
        if scoped.owner_home_dir != home.owner_home_dir:
            maintenance_clock.now += 25
        return result

    monkeypatch.setattr(gateway_loops, 'run_owner_retention_if_due', overrun)
    controller.tick(now=200_001)
    assert controller._discovery_debt
    services = []
    for tick in range(12):
        calls.clear()
        controller.tick(now=200_002 + tick)
        scoped = [path for path in calls if path != home.owner_home_dir]
        assert len(scoped) == 1 and len(controller._priority) == 64
        services.append(scoped[0] in {owner.home_dir for owner in newcomers})
    # 新集合尚未扫完时，任意相邻两拍两类债务各服务一次，不等“新流耗尽”才救旧队列。
    assert services == [True, False] * 6
    assert controller._discovery_debt


@pytest.mark.parametrize('slow_stage', ['retention', 'idle_compact', 'both'])
def test_base_overrun_cannot_permanently_exclude_scoped_owners(tmp_path, monkeypatch, maintenance_clock, slow_stage):
    partial = {f'owner-{i:03d}' for i in range(3)}
    controller, home, owners, calls = _controller(tmp_path, monkeypatch, maintenance_clock, 3, partial=partial)
    controller.tick(now=200_000)
    original = gateway_loops.run_owner_retention_if_due
    starts, idle_starts, cleanup_calls = [], [], []
    from agent_py_agent.agent.gateway_parts import compact_operations
    original_cleanup = gateway_loops._cleanup_loaded_runtime_safety

    def cleanup(agent):
        cleanup_calls.append(agent)
        return original_cleanup(agent)

    def idle(*args, **kwargs):
        starts.append(maintenance_clock.now)
        idle_starts.append(maintenance_clock.now)
        if slow_stage in {'idle_compact', 'both'}:
            maintenance_clock.now += 25

    def always_overrun(scoped, *, now):
        starts.append(maintenance_clock.now)
        result = original(scoped, now=now)
        if scoped.owner_home_dir != home.owner_home_dir or slow_stage in {'retention', 'both'}:
            maintenance_clock.now += 25
        return result

    monkeypatch.setattr(gateway_loops, '_cleanup_loaded_runtime_safety', cleanup)
    monkeypatch.setattr(compact_operations, 'idle_compact_maintenance_tick', idle)
    monkeypatch.setattr(gateway_loops, 'run_owner_retention_if_due', always_overrun)
    calls.clear()
    ticks = 8 if slow_stage == 'both' else 6
    for tick in range(ticks):
        began = maintenance_clock.now
        starts.clear()
        controller.tick(now=200_001 + tick)
        assert all(start - began < 20 for start in starts)
    assert calls.count(home.owner_home_dir) == (2 if slow_stage == 'both' else 3)
    if slow_stage == 'both':
        assert sum(calls.count(owner.home_dir) for owner in owners) == 4
        assert all(owner.home_dir in calls for owner in owners)
    else:
        assert all(calls.count(owner.home_dir) == 1 for owner in owners)
    assert len(controller._priority) == 3
    assert len(cleanup_calls) == ticks
    assert len(idle_starts) == (3 if slow_stage == 'idle_compact' else 2)


def test_failed_base_overrun_preserves_exception_and_next_scoped_opportunity(tmp_path, monkeypatch, maintenance_clock):
    partial = {'owner-000', 'owner-001'}
    controller, home, owners, calls = _controller(tmp_path, monkeypatch, maintenance_clock, 2, partial=partial)
    controller.tick(now=200_000)
    original = gateway_loops.run_owner_retention_if_due
    error = OSError('synthetic base overrun failure')

    def overrun(scoped, *, now):
        maintenance_clock.now += 25
        if scoped.owner_home_dir == home.owner_home_dir:
            raise error
        return original(scoped, now=now)

    monkeypatch.setattr(gateway_loops, 'run_owner_retention_if_due', overrun)
    calls.clear()
    for tick in range(4):
        if tick % 2 == 0:
            with pytest.raises(OSError) as raised:
                controller.tick(now=200_001 + tick)
            assert raised.value is error and controller._base_yield
        else:
            result = controller.tick(now=200_001 + tick)
            assert result['ran'] == 1  # 只报告真实scoped服务，不给失败/未执行base造结果。
    assert calls == [owner.home_dir for owner in owners]


def test_failed_base_overrun_still_discovers_previously_unknown_owners(tmp_path, monkeypatch, maintenance_clock):
    partial = {'owner-000', 'owner-001'}
    controller, home, owners, calls = _controller(tmp_path, monkeypatch, maintenance_clock, 0, partial=partial)
    controller.tick(now=200_000)
    newcomers = [ensure_owner_home(home.root, OwnerIdentity.provider_user('feishu', name)) for name in sorted(partial)]
    original = gateway_loops.run_owner_retention_if_due
    error = OSError('synthetic base fails before discovery')

    def overrun(scoped, *, now):
        maintenance_clock.now += 25
        if scoped.owner_home_dir == home.owner_home_dir:
            raise error
        return original(scoped, now=now)

    monkeypatch.setattr(gateway_loops, 'run_owner_retention_if_due', overrun)
    calls.clear()
    for tick in range(8):
        if tick % 2 == 0:
            with pytest.raises(OSError) as raised:
                controller.tick(now=200_001 + tick)
            assert raised.value is error
        else:
            controller.tick(now=200_001 + tick)
        assert len(controller._priority) <= 64
    assert all(owner.home_dir in calls for owner in newcomers)


def test_base_cleanup_clock_failure_does_not_replace_source_exception(tmp_path, monkeypatch, maintenance_clock):
    controller, home, owners, calls = _controller(tmp_path, monkeypatch, maintenance_clock, 1)
    source_error = OSError('synthetic original maintenance failure')
    timing_error = RuntimeError('synthetic cleanup clock unavailable')

    def clock_unavailable():
        raise timing_error

    def broken(scoped, *, now):
        monkeypatch.setattr(gateway_loops.time, 'monotonic', clock_unavailable)
        raise source_error

    monkeypatch.setattr(gateway_loops, 'run_owner_retention_if_due', broken)
    with pytest.raises(OSError) as raised:
        controller.tick(now=200_000)
    assert raised.value is source_error
    assert controller._base_yield and controller._discovery_debt
