# LLM: 用真实包存储验证列表与详情边界；订单用固定seq对照原语义，不执行安装。
# 模块用途: 防止目录重复全表读取或把元信息当作ZIP完整性证明。
# ruff: noqa: F811
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_py_agent.agent.capability.learnpack_service import _superseded_by, open_orders
from agent_py_agent.agent.capability.learnpack_store import InstallOrder, LearnpackStore
from agent_py_agent.tests.test_learnpack_store import _PROVENANCE, _built
from agent_py_agent.tests.test_self_map_runtime import _read, make_agent  # noqa: F401


# LLM: 不按时间排序，包含同字节重单、已完成、同字节安装、不同包及退回旧版。
# 函数用途: 为批量订单选择建立原语义对照与读取计数。
def test_open_orders_reads_orders_and_installs_once():
    orders = [InstallOrder(f'lp-{i:08x}', sha * 64, 'capability_pack', package, str(i), str(100 - i), i)
              for i, sha, package in [(1, 'a', 'one'), (2, 'b', 'one'), (3, 'a', 'one'),
                                      (4, 'a', 'one'), (5, 'c', 'two'), (6, 'd', 'done'), (7, 'e', 'installed')]]
    installs = [{'package_id': 'two', 'seq': 8, 'version': 'rollback', 'sha256': 'b' * 64},
                {'package_id': 'one', 'seq': 9, 'version': 'same', 'sha256': 'a' * 64}]
    calls = []
    store = SimpleNamespace(all_orders=lambda: calls.append('orders') or orders,
        orders_for=lambda package: [row for row in orders if row.package_id == package],
        installs=lambda: calls.append('installs') or installs,
        order_outcome=lambda key: {} if key == 'lp-00000006' else None)
    entries = (SimpleNamespace(manifest=SimpleNamespace(plugin_id='installed'), package_sha256='e' * 64),)
    result = open_orders(store, entries)
    assert [row.seq for row in result] == [4, 3]
    assert calls.count('orders') == 1 and calls.count('installs') == 1
    # 确认时原入口仍新读，不能复用上次目录观察。
    installs.append({'package_id': 'one', 'seq': 10, 'version': 'new', 'sha256': 'f' * 64})
    assert _superseded_by(store, orders[3]) == 'new（ffffffffffff）'


# LLM: 总览既不扫构建也不读订单全集；授权安装表允许读取一次。
# 函数用途: 包总览不进入历史或ZIP校验。
def test_package_overview_never_scans_history(make_agent, monkeypatch):
    agent = make_agent()
    monkeypatch.setattr(LearnpackStore, 'all_orders', lambda *_: pytest.fail('orders read'))
    monkeypatch.setattr(LearnpackStore, 'builds_for', lambda *_: pytest.fail('build directory scan'))
    monkeypatch.setattr(LearnpackStore, 'build', lambda *_: pytest.fail('zip read'))
    row = next(row for row in _read(agent)['sections'] if row['section'] == 'packages')
    assert row['count'] is None and row['completeness'] == 'partial'


# LLM: 目录只能称元信息未核验；准确详情必须调用原校验，篡改ZIP不能仍显示成功。
# 函数用途: 复现None通配bug并守住准确构建的完整性校验。
def test_build_list_metadata_and_exact_detail_verification(make_agent, tmp_path, monkeypatch):
    agent = make_agent()
    store = LearnpackStore(agent.home_paths.owner_home_dir)
    record = store.save_build(_built(tmp_path), _PROVENANCE)
    with patch.object(LearnpackStore, 'build', side_effect=AssertionError('list read zip')):
        value = _read(agent, 'packages')
    row = next(row for row in value['items'] if row['id'] == 'build:' + record.sha256)
    assert row['integrity_state'] == 'not_verified'
    original, calls = LearnpackStore.build, []
    monkeypatch.setattr(LearnpackStore, 'build', lambda self, sha: calls.append(sha) or original(self, sha))
    detail = _read(agent, 'packages', item=row['id'])['item']
    assert calls == [record.sha256] and detail['integrity_state'] == 'verified'
    store.build_path(record.sha256).write_bytes(b'tampered')
    detail = _read(agent, 'packages', item=row['id'])['item']
    assert detail['state'] == 'unavailable' and detail['integrity_state'] == 'unavailable'


# LLM: 外部身份和child不能借准确item访问管理员构建库存，校验前即拒读。
# 函数用途: 构建详情保留原管理员边界。
@pytest.mark.parametrize('kind', ['user', 'group'])
def test_nonadmin_build_detail_cannot_read_store(make_agent, monkeypatch, kind):
    agent = make_agent(kind=kind)
    monkeypatch.setattr(LearnpackStore, 'build', lambda *_: pytest.fail('foreign build read'))
    result = agent.tools.tools['self_map'].execute({'section': 'packages', 'item': 'build:' + 'a' * 64})
    assert not result.ok


# LLM: 受限child不继承主会话管理员库存，准确构建也须在读前拒绝。
# 函数用途: 验证child的build详情不触碰构建store。
def test_child_build_detail_cannot_read_store(make_agent, monkeypatch):
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    agent = make_agent()
    agent._current_run_params = RunParams(context_scope='task_local', task_attributes={'parent_run_id': 'parent'})
    monkeypatch.setattr(LearnpackStore, 'build', lambda *_: pytest.fail('child build read'))
    assert not agent.tools.tools['self_map'].execute({'section': 'packages', 'item': 'build:' + 'a' * 64}).ok


# LLM: 共享快照只能活在同一次context，下一调用必须重新读取，不得缓存启停授权。
# 函数用途: packages/plugins总览只读一次安装表且下次重读。
def test_overview_shares_install_snapshot_only_within_call(make_agent, monkeypatch):
    from agent_py_agent.agent.plugin_self_map import PluginInstallStore
    agent = make_agent()
    calls, original = [], PluginInstallStore.snapshot
    monkeypatch.setattr(PluginInstallStore, 'snapshot', lambda self: calls.append(1) or original(self))
    _read(agent)
    assert len(calls) == 1
    _read(agent)
    assert len(calls) == 2


# LLM: 扫描触及边界只能说明观测数；白名单排除来源文字和运行身份，不验证ZIP。
# 函数用途: 检查构建投影上限、损坏报告和字段白名单。
def test_build_metadata_bound_errors_and_whitelist(make_agent, tmp_path, monkeypatch):
    from agent_py_agent.agent.capability import learnpack_projection as projection
    agent = make_agent()
    store = LearnpackStore(agent.home_paths.owner_home_dir)
    store.save_build(_built(tmp_path, '1.0'), _PROVENANCE)
    store.save_build(_built(tmp_path, '2.0'), _PROVENANCE)
    monkeypatch.setattr(projection, '_MAX_BUILD_SCAN_COUNT', 1)
    report = projection.build_metadata_report(store)
    assert report.completeness == 'partial' and len(report.rows) == 1
    assert 'SELF_MAP_BUILD_SCAN_BOUNDED' in report.errors
    assert set(report.rows[0]) == {'sha256', 'kind', 'package_id', 'version', 'built_at', 'file_count', 'integrity_state'}
    monkeypatch.setattr(projection, '_MAX_BUILD_SCAN_COUNT', 2000)
    (store.root / 'builds' / ('a' * 64 + '.json')).write_text('{bad')
    report = projection.build_metadata_report(store)
    assert report.completeness == 'partial' and 'SELF_MAP_BUILD_METADATA_UNREADABLE' in report.errors


# LLM: installs原读口不验证package_id类型，坏的无关记录不能扩大正常订单及确认的失败范围。
# 函数用途: 复现不可哈希包名污染批量分组，并守住确认时的旧忽略语义。
@pytest.mark.parametrize('package', [[], {}, None, 17])
def test_malformed_install_package_does_not_break_valid_orders(package):
    order = InstallOrder('lp-00000001', 'a' * 64, 'capability_pack', 'one', '1', '', 1)
    bad_order = InstallOrder('lp-00000002', 'b' * 64, 'capability_pack', package, '1', '', 2)
    rows = [{'package_id': package, 'seq': 2, 'version': 'bad', 'sha256': 'b' * 64}]
    store = SimpleNamespace(all_orders=lambda: [order, bad_order], installs=lambda: rows, orders_for=lambda _: [order, bad_order], order_outcome=lambda _: None)
    assert open_orders(store) == [order]
    assert _superseded_by(store, order) == ''


# LLM: 显式limit测试不足以覆盖默认列式入口；ZIP被改坏后目录仍必须公开未核验而非隐去校验状态。
# 函数用途: 默认紧凑构建列表保留integrity_state列。
def test_compact_build_list_exposes_integrity_state(make_agent, tmp_path):
    import json
    agent = make_agent()
    store = LearnpackStore(agent.home_paths.owner_home_dir)
    record = store.save_build(_built(tmp_path), _PROVENANCE)
    store.build_path(record.sha256).write_bytes(b'tampered')
    result = agent.tools.tools['self_map'].execute({'section': 'packages'})
    assert result.ok, result.output
    value = json.loads(result.output)
    assert 'integrity_state' in value['item_columns']
    row = next(row for row in value['items'] if row[0]['item'] == 'build:' + record.sha256)
    assert row[value['item_columns'].index('integrity_state')] == 'not_verified'


# LLM: glob可能吞目录权限错误；未能枚举不能成为完整零库存证据。
# 函数用途: 构建目录读取失败必须保留partial而非假空库。
def test_build_metadata_scan_failure_is_not_complete_empty(make_agent, monkeypatch):
    import os
    from pathlib import Path

    from agent_py_agent.agent.capability.learnpack_projection import build_metadata_report
    agent = make_agent()
    store = LearnpackStore(agent.home_paths.owner_home_dir)
    directory = store.root / 'builds'
    directory.mkdir(parents=True, exist_ok=True)
    original = os.scandir
    # LLM: 仅拒绝被测目录的枚举，不影响Agent准备或其它文件读口。
    # 函数用途: 确定性模拟权限拒绝，避免依赖平台权限位或root身份。
    def denied(path):
        if Path(path) == directory:
            raise PermissionError(13, 'private unavailable')
        return original(path)
    monkeypatch.setattr(os, 'scandir', denied)
    report = build_metadata_report(store)
    assert report.completeness == 'partial' and report.errors
