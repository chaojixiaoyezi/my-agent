"""W6清单：仅临时Owner，覆盖确认、CAS、原子发布和告警窗口。"""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from agent_py_agent.agent.assets import source_manifest as manifests
from agent_py_agent.agent.tooling.datasources_tool import DatasourcesTool


# LLM: 全部值为合成的不透明引用，不访问真实凭据。
# 函数用途: 构造一个清单来源。
def source(source_id='events-a', *, core=False):
    return {'source_id': source_id, 'core': core, 'credential_ref': 'cref-a', 'endpoint_ref': 'endpoint-a'}


# LLM: 读口必须不创建任何目录，包括锁文件。
# 函数用途: 验证缺失清单的只读默认值。
def test_missing_manifest_read_has_no_filesystem_effect(tmp_path):
    home = tmp_path / 'owner'
    result = manifests.load_manifest(home, 'events')
    assert isinstance(result, dict)
    assert result['schema_version'] == 'datasource_manifest.v1'
    assert result['revision'] == 0 and result['sources'] == []
    assert not home.exists()


# LLM: 旧观察不能覆盖新状态；prune复用同一版本门。
# 函数用途: 验证增量登记、CAS冲突和明确移除。
def test_save_merge_and_prune_are_compare_and_swap(tmp_path):
    first = manifests.save_manifest(tmp_path, 'events', [source()], expected_revision=0)
    assert isinstance(first, dict)
    assert first['revision'] == 1
    second = manifests.save_manifest(tmp_path, 'events', [source('events-b')], expected_revision=1)
    assert second['revision'] == 2
    assert {s['source_id'] for s in second['sources']} == {'events-a', 'events-b'}
    with pytest.raises(ValueError, match='ASSET_REVISION_CONFLICT'):
        manifests.save_manifest(tmp_path, 'events', [source('events-c')], expected_revision=1)
    with pytest.raises(ValueError, match='ASSET_REVISION_CONFLICT'):
        manifests.save_manifest(tmp_path, 'events', [source('events-c')])
    with pytest.raises(ValueError, match='ASSET_REVISION_CONFLICT'):
        manifests.prune_sources(tmp_path, 'events', ['events-a'], expected_revision=1)
    pruned = manifests.prune_sources(tmp_path, 'events', ['events-a'], expected_revision=2)
    assert pruned['revision'] == 3 and pruned['sources'] == [source('events-b')]


# LLM: 写叶失败不得发表新revision，复用真实原子写口的替身。
# 函数用途: 验证模拟IO失败保持旧清单。
def test_atomic_write_failure_keeps_prior_revision(tmp_path, monkeypatch):
    old = manifests.save_manifest(tmp_path, 'events', [source()])
    assert isinstance(old, dict)
    before = (tmp_path / 'data/source_manifests/events.json').read_bytes()

    # LLM: 只替换原子写叶，不篡改读取或CAS结果。
    # 函数用途: 模拟持久化失败。
    def unavailable(root, parts, text, *, file_mode=0o600):
        raise OSError('simulated I/O error')

    monkeypatch.setattr(manifests, 'write_text_atomic_beneath', unavailable)
    with pytest.raises(OSError):
        manifests.save_manifest(tmp_path, 'events', [source('events-b')], expected_revision=1)
    assert manifests.load_manifest(tmp_path, 'events') == old
    assert (tmp_path / 'data/source_manifests/events.json').read_bytes() == before


# LLM: 必须在工具入口显式确认，不能用truthy字符串冒充。
# 函数用途: 验证未确认的登记和移除零写入。
@pytest.mark.parametrize('action', ['manifest_register', 'manifest_prune'])
@pytest.mark.parametrize('confirmed', [None, False, 'true', 1])
def test_manifest_mutations_require_literal_confirmation(tmp_path, action, confirmed):
    tool = DatasourcesTool(tmp_path / 'owner')
    params = {'action': action, 'source_system': 'events', 'sources': [source()],
              'source_ids': ['events-a'], 'expected_revision': 0}
    if confirmed is not None:
        params['confirmed'] = confirmed
    result = tool.execute(params)
    assert not result.ok and result.effect_outcome == 'not_started'
    assert not (tmp_path / 'owner/data/source_manifests').exists()


# LLM: 从公开工具执行真实读写；不把内部helper成功当作工具接通。
# 函数用途: 验证清单动作、CAS错误和只读投影。
def test_manifest_tool_roundtrip_and_conflict(tmp_path):
    tool = DatasourcesTool(tmp_path / 'owner')
    got = tool.execute({'action': 'manifest_get', 'source_system': 'events'})
    assert got.ok
    assert json.loads(got.output)['manifest']['revision'] == 0
    assert not tool.owner_home.exists()
    saved = tool.execute({'action': 'manifest_register', 'source_system': 'events',
        'sources': [source()], 'expected_revision': None, 'confirmed': True})
    assert saved.ok and json.loads(saved.output)['manifest']['revision'] == 1
    conflict = tool.execute({'action': 'manifest_prune', 'source_system': 'events',
        'source_ids': ['events-a'], 'expected_revision': 0, 'confirmed': True})
    assert not conflict.ok and conflict.error_code == 'TOOL_INVALID_ARGUMENTS'
    assert json.loads(conflict.output)['error_code'] == 'ASSET_REVISION_CONFLICT'
    pruned = tool.execute({'action': 'manifest_prune', 'source_system': 'events',
        'source_ids': ['events-a'], 'expected_revision': 1, 'confirmed': True})
    assert pruned.ok and json.loads(pruned.output)['manifest']['sources'] == []


# LLM: 同一观察版本只能有一个提交者；线程共用既有锁原语。
# 函数用途: 验证并发CAS不会丢失已发表清单。
def test_concurrent_registration_has_one_cas_winner(tmp_path):
    first = manifests.save_manifest(tmp_path, 'events', [source()])
    assert isinstance(first, dict)

    # LLM: 只吞明确的版本冲突，不掩盖其他IO失败。
    # 函数用途: 竞争同一清单revision。
    def contender(index):
        try:
            return manifests.save_manifest(tmp_path, 'events', [source(f'events-{index}')], expected_revision=1)
        except ValueError as error:
            assert 'ASSET_REVISION_CONFLICT' in str(error)
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(contender, range(2)))
    assert sum(value is not None for value in values) == 1
    assert manifests.load_manifest(tmp_path, 'events')['revision'] == 2


# LLM: 清单和健康账不能经链接写入Owner外目录。
# 函数用途: 验证路径身份及受控父链。
@pytest.mark.parametrize('system', ['../outside', '/outside', 'events.health'])
def test_invalid_manifest_system_is_rejected(tmp_path, system):
    with pytest.raises(ValueError):
        manifests.save_manifest(tmp_path, system, [source()])
    assert not (tmp_path / 'data').exists()


# LLM: 注入固定UTC时间，不等待真实一天；不同Owner和引用独立。
# 函数用途: 验证24小时窗口边界、持久化及Owner隔离。
def test_alert_dedupe_window_and_identity(tmp_path):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    assert manifests.should_emit_alert(tmp_path, 'events', 'events-a', 'cref-a', now=now) is True
    assert manifests.should_emit_alert(tmp_path, 'events', 'events-a', 'cref-a', now=now + 86399) is False
    assert manifests.should_emit_alert(tmp_path, 'events', 'events-a', 'cref-a', now=now + 86400) is True
    assert manifests.should_emit_alert(tmp_path, 'events', 'events-a', 'cref-b', now=now) is True
    assert manifests.should_emit_alert(tmp_path / 'other', 'events', 'events-a', 'cref-a', now=now) is True
    state = json.loads((tmp_path / 'data/source_manifests/events.health.json').read_text())
    assert state['schema_version'] == 'datasource_health.v1'
    assert set(state['alerts']) == {'events-a|cref-a', 'events-a|cref-b'}
