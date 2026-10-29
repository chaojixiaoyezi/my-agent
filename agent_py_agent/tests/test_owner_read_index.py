# LLM: 所有规范来源和派生数据库都位于pytest临时owner中，使用真实JSONL、SQLite与文件代次，不把投影当写权威。
# 模块用途: 验证owner读索引的有界推进、独立结果、坏源披露、代次失效与小批membership。
from __future__ import annotations

import json
from pathlib import Path

from agent_py_agent.agent.local_storage.owner_read_index import OwnerReadIndex
from agent_py_agent.agent.observability.telemetry_ledger import ToolCallRecord

PARTS = ('data', 'telemetry', 'tool_calls.jsonl')


# LLM: 复用真实遥测记录格式，构造与被测读取量分离，不通过索引写规范事实。
# 函数用途: 生成一条有稳定身份与时间排序字段的有效样本。
def row(index):
    return ToolCallRecord(call_id=f'call-{index}', tool_name='tool', run_id=f'run-{index}',
                          owner_id='owner', started_at=float(index + 1), ended_at=float(index + 1),
                          duration_ms=float(index)).to_dict()


# LLM: 测试只用临时owner下固定来源路径；每条物理LF记录一次编码。
# 函数用途: 准备规范来源，不预先创建cache或数据库。
def source(home, count=5):
    path = home.joinpath(*PARTS)
    path.parent.mkdir(parents=True)
    path.write_text(''.join(json.dumps(row(index)) + '\n' for index in range(count)))
    return path


# LLM: 首次不存在来源可解释为有效空，不得为一次只读空查询创建owner数据目录。
# 函数用途: 区分真实首次空源与坏文件的未知总数。
def test_owner_index_first_missing_source_is_complete_empty_without_writes(tmp_path):
    index = OwnerReadIndex(tmp_path, owner_id='owner')
    view = index.advance('telemetry', PARTS)
    assert view['status'] == 'complete'
    result = index.query(view)
    assert result['rows'] == [] and result['total_count'] == 0
    assert not list(tmp_path.iterdir())


# LLM: 每拍最多两行，checkpoint与已投影行必须同事务；部分结果不能给伪精确总数。
# 函数用途: 从多次有界推进到完整查询，并核对真实唯一物理数据库。
def test_owner_index_budget_resume_and_page(tmp_path):
    path = source(tmp_path)
    original = path.read_bytes()
    index = OwnerReadIndex(tmp_path, owner_id='owner')
    view = index.advance('telemetry', PARTS, max_rows=2)
    assert view['status'] == 'partial' and view['through'] > 0
    page = index.query(view)
    assert page['total_count'] is None and page['coverage']['status'] == 'partial'
    for _ in range(4):
        view = index.advance('telemetry', PARTS, max_rows=2)
        if view['status'] == 'complete':
            break
    assert view['status'] == 'complete'
    page = index.query(view, limit=2, offset=2)
    assert page['total_count'] == 5
    assert [item['call_id'] for item in page['rows']] == ['call-2', 'call-3']
    assert (tmp_path / 'cache' / 'owner_read_index.sqlite3').is_file()
    assert path.read_bytes() == original
    page['rows'][0]['args_summary']['changed'] = True
    assert not index.query(view, limit=1, offset=2)['rows'][0]['args_summary']


# LLM: warm查询和受信前缀后的追加不能再次解码旧行；源前缀复核的字节I/O不计为JSON解码。
# 函数用途: 以真实领域validator调用量而非耗时验证增量解析。
def test_owner_index_warm_and_append_decode_only_delta(tmp_path):
    path = source(tmp_path)
    index = OwnerReadIndex(tmp_path, owner_id='owner')
    decoded = []

    # LLM: 旁听真实输入对象，并拒绝不支持的领域版本。
    # 函数用途: 计量到底重新解析了多少规范行。
    def validate(item):
        assert item['schema'] == 'tool_call_telemetry.v1'
        decoded.append(item['call_id'])

    first = index.advance('telemetry', PARTS, validator=validate)
    assert first['status'] == 'complete' and len(decoded) == 5
    assert index.advance('telemetry', PARTS, validator=validate)['status'] == 'complete'
    assert len(decoded) == 5
    with path.open('a') as handle:
        handle.write(json.dumps(row(5)) + '\n')
    latest = index.advance('telemetry', PARTS, validator=validate)
    assert latest['status'] == 'complete' and len(decoded) == 6
    assert index.query(first)['total_count'] == 5
    assert index.query(latest)['total_count'] == 6
    membership = index.membership(latest, ['call-1', 'absent'])
    assert membership['coverage']['status'] == 'complete'
    assert membership['ids'] == frozenset({'call-1'})


# LLM: 改写或删除使旧view失效，不能从派生库复活被撤掉的正文；新代必须反映新事实。
# 函数用途: 核对前缀同尺寸改写加追加不会被误认成可信纯追加。
def test_owner_index_rewrite_and_deletion_invalidate_old_view(tmp_path):
    path = source(tmp_path)
    index = OwnerReadIndex(tmp_path, owner_id='owner')
    old = index.advance('telemetry', PARTS)
    assert old['status'] == 'complete'
    text = path.read_text().replace('call-0', 'call-X') + json.dumps(row(5)) + '\n'
    path.write_text(text)
    new = index.advance('telemetry', PARTS)
    assert new['status'] == 'complete'
    assert index.query(old)['coverage']['status'] != 'complete'
    assert index.query(new, filters={'call_id': 'call-0'})['total_count'] == 0
    assert index.query(new, filters={'call_id': 'call-X'})['total_count'] == 1
    path.unlink()
    report = index.query(new)
    assert report['coverage']['status'] == 'unavailable'
    assert report['total_count'] is None and report['rows'] == []


# LLM: 坏UTF8/JSON可能影响任意筛选，不能跳过后给有效零或complete；来源字节必须不变。
# 函数用途: 防止解析损坏被投影层伪装成没有数据。
def test_owner_index_corruption_is_not_zero(tmp_path):
    path = source(tmp_path)
    with path.open('ab') as handle:
        handle.write(b'\xff\n')
    before = path.read_bytes()
    index = OwnerReadIndex(tmp_path, owner_id='owner')
    view = index.advance('telemetry', PARTS)
    report = index.query(view)
    assert view['status'] == 'unavailable'
    assert report['coverage']['status'] == 'unavailable' and report['total_count'] is None
    assert path.read_bytes() == before


# LLM: SQLite及来源均不可借链接越出当前owner；拒绝必须发生在对foreign数据库做任何更改之前。
# 函数用途: 保护固定派生库路径的no-follow边界。
def test_owner_index_refuses_linked_database_without_touching_target(tmp_path):
    home = tmp_path / 'owner'
    home.mkdir()
    source(home)
    (home / 'cache').mkdir(mode=0o700)
    foreign = tmp_path / 'foreign.sqlite3'
    foreign.write_bytes(b'foreign-owner-content')
    (home / 'cache' / 'owner_read_index.sqlite3').symlink_to(foreign)
    index = OwnerReadIndex(home, owner_id='owner')
    view = index.advance('telemetry', PARTS)
    assert view['status'] == 'unavailable'
    assert foreign.read_bytes() == b'foreign-owner-content'
    assert index.query(view)['total_count'] is None


# LLM: 临时store原路径是owner根推导，不用runtime配置或真实用户数据。
# 函数用途: 创建未写盘的使用统计存储。
def _usage_store(home):
    from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore

    return SkillLearningStore(home / 'data' / 'skill_learning', home / 'skills')


# LLM: 原summary仍是lifetime权威，缓存删除和裁剪明细均不能降低累计计数。
# 函数用途: 验证索引只投影summary而不重建丢失历史。
def test_indexed_usage_preserves_lifetime_after_detail_trim(tmp_path):
    from agent_py_agent.agent.capability import skill_usage_ledger as module

    assert callable(getattr(module, 'load_skill_usage_summary_report', None))
    store = _usage_store(tmp_path)
    module.record_skill_usage_event(store, event='manual_get', skill_id='builtin:lifetime', source='test')
    ledger, summary_path = store.directory / 'usage_ledger.jsonl', store.directory / 'usage_summary.json'
    item, summary = json.loads(ledger.read_text()), json.loads(summary_path.read_text())
    for counts in (summary['totals'], summary['by_skill']['builtin:lifetime']):
        counts['manual_get'] = counts['effective_uses'] = 5000
    summary['last_sequence'] = 5000
    summary['daily_history'][item['date']]['manual_get'] = 5000
    summary_path.write_text(json.dumps(summary))
    ledger.write_text(''.join(json.dumps({**item, 'sequence': seq}) + '\n' for seq in (4999, 5000)))
    before = summary_path.read_bytes(), ledger.read_bytes()
    report = module.load_skill_usage_summary_report(store)
    assert report['coverage']['status'] == 'complete'
    assert report['summary']['totals']['effective_uses'] == 5000
    assert report['coverage']['detail_history']['status'] == 'partial'
    assert (summary_path.read_bytes(), ledger.read_bytes()) == before
    (tmp_path / 'cache' / 'owner_read_index.sqlite3').unlink()
    assert module.load_skill_usage_summary(store)['last_sequence'] == 5000


# LLM: 缺summary且明细已经裁剪，不能据最近事件补造lifetime；旧接口也不应返回空计数。
# 函数用途: 验证生命周期权威缺失的结构化未知。
def test_indexed_usage_missing_summary_does_not_rebuild_trimmed_lifetime(tmp_path):
    import pytest

    from agent_py_agent.agent.capability import skill_usage_ledger as module

    assert callable(getattr(module, 'load_skill_usage_summary_report', None))
    store = _usage_store(tmp_path)
    module.record_skill_usage_event(store, event='manual_get', skill_id='builtin:missing', source='test')
    ledger = store.directory / 'usage_ledger.jsonl'
    item = json.loads(ledger.read_text())
    ledger.write_text(json.dumps({**item, 'sequence': 1001}) + '\n')
    (store.directory / 'usage_summary.json').unlink()
    before = ledger.read_bytes()
    report = module.load_skill_usage_summary_report(store)
    assert report['summary'] is None and report['coverage']['status'] == 'unavailable'
    with pytest.raises(ValueError) as caught:
        module.load_skill_usage_summary(store)
    assert caught.value.coverage['status'] == 'unavailable'
    assert ledger.read_bytes() == before and not (store.directory / 'usage_summary.json').exists()


# LLM: 派生缓存故障不影响原writer、配额、锁和双文件提交。
# 函数用途: 验证写账不依赖读索引成功。
def test_indexed_usage_writer_remains_independent_of_cache(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability import skill_usage_ledger as module

    assert callable(getattr(module, 'load_skill_usage_summary_report', None))
    store = _usage_store(tmp_path)

    # LLM: 只替换读投影边界，原writer仍执行真实事务。
    # 函数用途: 注入派生缓存故障。
    def fail(*args, **kwargs):
        raise OSError('injected cache failure')

    monkeypatch.setattr(module.OwnerReadIndex, 'advance', fail)
    module.record_skill_usage_event(store, event='manual_get', skill_id='builtin:independent', source='test')
    saved = json.loads((store.directory / 'usage_summary.json').read_text())
    assert saved['totals']['effective_uses'] == 1
    report = module.load_skill_usage_summary_report(store)
    assert report['summary'] is None and report['coverage']['status'] == 'unavailable'


# LLM: 首次缺源有效零而坏summary未知，两个状态均不应由读取修改事实。
# 函数用途: 验证首次只读及坏汇总结构异常。
def test_indexed_usage_absence_and_bad_summary_are_distinct(tmp_path):
    import pytest

    from agent_py_agent.agent.capability import skill_usage_ledger as module

    assert callable(getattr(module, 'load_skill_usage_summary_report', None))
    store = _usage_store(tmp_path)
    empty = module.load_skill_usage_summary_report(store)
    assert empty['coverage']['status'] == 'complete' and empty['summary']['last_sequence'] == 0
    assert not list(tmp_path.iterdir())
    module.record_skill_usage_event(store, event='manual_get', skill_id='builtin:bad', source='test')
    path = store.directory / 'usage_summary.json'
    path.write_text('{broken')
    report = module.load_skill_usage_summary_report(store)
    assert report['summary'] is None and report['coverage']['status'] == 'unavailable'
    with pytest.raises(ValueError) as caught:
        module.load_skill_usage_summary(store)
    assert caught.value.coverage['status'] == 'unavailable'
    assert path.read_text() == '{broken'


# LLM: 真实并发重建只改派生代次不改规范源；检查点与查询必须同一事务，不能给假complete零。
# 函数用途: 在检查点观察后让另一实例实际重建同源缓存。
def test_owner_index_read_transaction_keeps_checkpoint_count_and_page_together(tmp_path, monkeypatch):
    source(tmp_path)
    index, other = OwnerReadIndex(tmp_path), OwnerReadIndex(tmp_path)
    view = index.advance('telemetry', PARTS)
    original, triggered, attempted = index._checkpoint, [], []

    # LLM: validator不同促使真实重建，不写原JSONL也不伪造查询结果。
    # 函数用途: 创建新的投影格式版本。
    def revalidate(item):
        assert item['schema'] == 'tool_call_telemetry.v1'

    # LLM: 只在本次读已取得检查点后插入第二实例真实事务，不吞SQL异常或伪造返回。
    # 函数用途: 稳定复现检查点和样本之间的并发窗口。
    def checkpoint(connection, sid, domain, parts):
        result = original(connection, sid, domain, parts)
        if not triggered:
            triggered.append(True)
            attempted.append(other.advance('telemetry', PARTS, validator=revalidate))
        return result

    monkeypatch.setattr(index, '_checkpoint', checkpoint)
    result = index.query(view)
    assert triggered and attempted
    assert result['coverage']['status'] != 'complete' or result['total_count'] == 5
    if result['coverage']['status'] == 'complete':
        assert len(result['rows']) == 5


# LLM: 来源跟踪与撤销仅存小键也必须有硬界；耗尽时拒绝新增来源，不能遗忘后把删除说成首次空。
# 函数用途: 真实多源与重复重写验证元数据上限和fail-closed状态。
def test_owner_index_tracking_is_bounded_without_forgetting_deletion(tmp_path, monkeypatch):
    from agent_py_agent.agent.local_storage import owner_read_index as module

    monkeypatch.setattr(module, '_MAX_TRACKED_SOURCES', 4, raising=False)
    index = OwnerReadIndex(tmp_path)
    paths = []
    for number in range(6):
        path = tmp_path / f'{number}.jsonl'
        path.write_text(json.dumps(row(number)) + '\n')
        paths.append(path)
        index.advance('telemetry', (path.name,))
    assert len(index._seen) <= 4
    paths[-1].unlink()
    deleted = index.advance('telemetry', (paths[-1].name,))
    assert deleted['status'] != 'complete'
    for number in range(8):
        paths[0].write_text(json.dumps(row(number)) + '\n')
        index.advance('telemetry', (paths[0].name,))
    assert len(index._revoked) <= 4
