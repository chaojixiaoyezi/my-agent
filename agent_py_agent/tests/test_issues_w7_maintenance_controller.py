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


# LLM: 先有真实续扫游标，再让每次monotonic观察推进25秒；预算耗尽后仅基础owner运行，下一拍可继续。
# 函数用途: 验证时间耗尽不重置或跳过已保存分页游标。
def test_advancing_clock_stops_after_base_owner_and_preserves_cursor(tmp_path, monkeypatch, maintenance_clock):
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
    assert controller.tick(now=200_001)['scanned'] == 0
    assert calls == [home.owner_home_dir]
    assert controller._cursor == cursor
    monkeypatch.setattr(gateway_loops.time, 'monotonic', lambda: maintenance_clock.now)
    calls.clear()
    assert controller.tick(now=200_002)['scanned'] == 4
    assert calls == [home.owner_home_dir, *(o.home_dir for o in owners[4:8])]


# LLM: 真实优先续作过程中由维护回调耗尽时间；未处理的队列项与普通分页游标必须保留。
# 函数用途: 验证时间预算也约束优先队列且下一拍不会丢owner。
def test_priority_deadline_leaves_unvisited_entries(tmp_path, monkeypatch, maintenance_clock):
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
    assert controller.tick(now=200_001)['scanned'] == 0
    assert calls == [home.owner_home_dir, owners[0].home_dir]
    assert [k[2] for k in controller._priority] == ['owner-001', 'owner-002', 'owner-000']


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
