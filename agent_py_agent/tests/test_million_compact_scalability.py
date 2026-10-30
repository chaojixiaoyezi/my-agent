# LLM: 仅在pytest临时目录构建合成规模；高代次播种不等于实际执行百万次模型压缩，证据分开记录。
# 模块用途: 约束不可变索引、检查点和消息热链路的规模、I/O与耗时，不用旧进程或旧产物代替当前验证。
from __future__ import annotations

from agent_py_agent.agent.conversation.store_io import ImmutableJsonIndex


# LLM: 更新后旧根必须仍可准确读取；查询miss不能误用别的键或跨代值。
# 函数用途: 验证内容寻址索引的不可变更新、分裂和显式全历史遍历。
def test_immutable_index_keeps_previous_roots_and_exact_keys(tmp_path):
    index = ImmutableJsonIndex(tmp_path, ('index',))
    first = index.put(None, 'first', {'generation': 1, 'checkpoint_id': 'one'})
    assert isinstance(first, dict)
    root = first
    expected = {'first': {'generation': 1, 'checkpoint_id': 'one'}}
    for number in range(100):
        expected[f'key-{number}'] = {'generation': number + 2, 'checkpoint_id': str(number)}
        root = index.put(root, f'key-{number}', expected[f'key-{number}'])
    assert index.get(first, 'key-99') is None
    assert index.get(first, 'first') == expected['first']
    assert dict(index.items(root)) == expected
    assert index.get(root, 'missing') is None


# LLM: 时间可回退且相同时间可对应更高代次；要求按时间过滤后取最大generation，不能误取最大时间值。
# 函数用途: 验证任务创建前线程摘要的准确索引，不遍历历史代次。
def test_ordered_index_returns_max_generation_before_cutoff(tmp_path):
    index = ImmutableJsonIndex(tmp_path, ('timeline',), ordered=True)
    root = None
    for number in range(100):
        root = index.put(root, f'{number:064x}', {'generation': 100 - number, 'checkpoint_id': str(number)})
    assert isinstance(root, dict)
    assert index.range_max(root, f'{25:064x}') == {'generation': 100, 'checkpoint_id': '0'}
    assert index.range_max(root, f'{99:064x}') == {'generation': 100, 'checkpoint_id': '0'}


# LLM: 相同叶不能被外部改写后复用；节点必须拒绝字节摘要损坏，不返回空集合。
# 函数用途: 验证冷热索引节点损坏会显式失败。
def test_index_node_digest_is_checked(tmp_path):
    import pytest

    index = ImmutableJsonIndex(tmp_path, ('index',))
    root = index.put(None, 'one', True)
    assert isinstance(root, dict)
    path = tmp_path / 'index' / root['ref']['ref']
    path.write_text('{}\n')
    with pytest.raises(OSError):
        index.get(root, 'one')


# LLM: 使用真实writer及原线程CAS连续提交；显式完整核验可线性遍历，重启后热恢复不能再读历史链。
# 函数用途: 证明检查点账本有界、冷链不丢失及摘要热路径不依赖逐代回溯。
def test_hot_checkpoint_rotation_keeps_cold_chain_and_bounded_restart(compact_case, monkeypatch):
    import json

    from agent_py_agent.agent.conversation import compact_checkpoint as checkpoints
    from agent_py_agent.agent.conversation import compact_summary_view as views
    from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE
    from agent_py_agent.tests.test_compact_scoped_checkpoint import (
        _checkpoint_path,
        _commit_transcript,
    )

    case = compact_case
    message_ids = []
    for number in range(80):
        case.clock[0] = 10.0 + number
        thread, message, _ = _commit_transcript(
            case, scope=THREAD_COMPACT_SCOPE, summary=f'summary-{number}', content=str(number), publish=True,
        )
        message_ids.append(message.message_id)
    hot = _checkpoint_path(case)
    assert len(hot.read_bytes().splitlines()) <= 64
    assert hot.stat().st_size < 2 * 1024 * 1024
    meta = json.loads(hot.with_suffix('.meta.json').read_text())
    assert meta['latest_generation'] == thread.compact_generation
    chain = checkpoints.committed_compact_checkpoint_chain(case.agent, thread)
    assert len(chain) == 80 and [row['generation'] for row in chain] == list(range(1, 81))
    views._SUMMARY_VIEWS.clear()
    views._VERIFIED_CHAINS.clear()
    visits = []
    original = views.iter_committed_compact_checkpoints

    # LLM: 委托原完整验证器，记录真实历史遍历而不伪造结果。
    # 函数用途: 检出恢复重新进入逐代链路径。
    def counted(*args, **kwargs):
        for row in original(*args, **kwargs):
            visits.append(row['generation'])
            yield row

    monkeypatch.setattr(views, 'iter_committed_compact_checkpoints', counted)
    view = views.resolve_compact_summary_view(case.agent, thread, THREAD_COMPACT_SCOPE)
    assert view.summary == 'summary-79' and message_ids[0] in view.source_message_ids
    assert visits == []


# LLM: 夹杂两个任务及后来线程摘要；任务创建时间之前的基础不能被最新全线程摘要替代。
# 函数用途: 对热锚点验证scope与summary_base继承而不是提交前驱合并。
def test_carried_scope_view_preserves_creation_cutoff_and_exact_coverage(compact_case):
    from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE, CompactScope
    from agent_py_agent.agent.conversation.compact_summary_view import resolve_compact_summary_view
    from agent_py_agent.tests.test_compact_scoped_checkpoint import _commit_transcript

    case = compact_case
    first, old, old_id = _commit_transcript(
        case, scope=THREAD_COMPACT_SCOPE, summary='early', content='old', publish=True,
    )
    task = CompactScope(kind='task', task_id='cutoff', created_at=15.0)
    case.clock[0] = 20.0
    later, newer, _ = _commit_transcript(
        case, scope=THREAD_COMPACT_SCOPE, summary='later', content='newer', publish=True,
    )
    inherited = resolve_compact_summary_view(case.agent, later, task)
    assert inherited.checkpoint_id == old_id and inherited.source_message_ids == frozenset({old.message_id})
    current, local, local_id = _commit_transcript(
        case, scope=task, summary='local', content='private', publish=False, base=old_id,
    )
    scoped = resolve_compact_summary_view(case.agent, current, task)
    assert scoped.checkpoint_id == local_id
    assert scoped.source_message_ids == frozenset({old.message_id, local.message_id})
    assert newer.message_id not in scoped.source_message_ids
    assert current.compacted_through_message_id == newer.message_id


from agent_py_agent.tests.test_compact_scoped_checkpoint import compact_case as compact_case


# LLM: 通过真实writer构造恰好2MiB的最终热行，边界必须在候选发布前拒绝，不能把<=写成严格小于。
# 函数用途: 验证硬字节预算的等号边界且不破坏原可解析head。
def test_exact_hot_byte_budget_is_rejected_before_append(compact_case):
    import json

    import pytest

    from agent_py_agent.agent.conversation import compact_checkpoint as checkpoints
    from agent_py_agent.tests.test_compact_scoped_checkpoint import _checkpoint_path, _message

    case = compact_case
    thread = case.store.threads.require(case.thread_id)
    message = _message(case, 'source')

    # LLM: 只替换摘要长度，其它来源、时钟和目录根均保持一致，以实际编码热行计算边界。
    # 函数用途: 写入同一候选骨架用于字节精确对照。
    def write(summary):
        return checkpoints.write_compact_checkpoint(case.agent, checkpoints.CompactCheckpointRequest(
            thread=thread, summary=summary, operation_evidence={}, compact_rows=(message,), retained_tail=(),
            source_end_byte_offset=0, projected_tokens_before=900, projected_tokens_after=300,
            policy=case.policy, forced=False,
        ))

    write('x')
    path = _checkpoint_path(case)
    original = path.read_bytes()
    row = json.loads(original)
    overhead = len((json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n').encode()) - 1
    summary = 'x' * (checkpoints._ACTIVE_CHECKPOINT_BYTE_BUDGET - overhead)
    with pytest.raises(ValueError, match='byte budget'):
        write(summary)
    assert path.read_bytes() == original


# LLM: 少于32行也必须按累计编码字节维护；旧提交头从冷副本仍可恢复，不靠丢弃历史凑预算。
# 函数用途: 验证三个大摘要使热窗口按字节收缩而原head均可读。
def test_fewer_than_32_rows_still_obeys_hot_byte_budget(compact_case):
    from agent_py_agent.agent.conversation import compact_checkpoint as checkpoints
    from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE
    from agent_py_agent.tests.test_compact_scoped_checkpoint import (
        _checkpoint_path,
        _commit_transcript,
    )

    history = []
    for i in range(3):
        compact_case.clock[0] = 10.0 + i
        thread, _, _ = _commit_transcript(compact_case, scope=THREAD_COMPACT_SCOPE,
                                          summary=str(i) + 'x' * 750000, content=str(i), publish=True)
        history.append(thread)
    assert _checkpoint_path(compact_case).stat().st_size < 2097152
    assert len(_checkpoint_path(compact_case).read_bytes().splitlines()) < 3
    for thread in history:
        restored = checkpoints.load_latest_committed_compact_checkpoint(compact_case.agent, thread)
        assert restored['summary'] == thread.summary


# LLM: 使用真实不可变节点把一个准确键推到SHA路由允许的64层最坏深度，不伪造百万个物理条目。
# 函数用途: 构造比通常百万条随机键更深的合法查询路径，成本仍受固定64层约束。
def _max_depth_root(index, key, value):
    root = index._save({'entries': {key: value}})
    for digit in reversed(index._route(key)):
        root = index._save({'children': {digit: root}})
    return root


# LLM: 高代次是隔离合成head种子，不代表实际执行百万次压缩或完整百万代冷链；CAS、磁盘节点和三个热入口均用真实现。
# 函数用途: 约束百万代等效最坏深度下的load、commit及selection成本，防止恢复热循环随累计代数展开。
def test_million_equivalent_depth_bounds_real_load_commit_selection(compact_case, monkeypatch, request):
    import json
    import time
    from dataclasses import replace

    from agent_py_agent.agent.conversation import compact_checkpoint as checkpoints
    from agent_py_agent.agent.conversation import compact_summary_view as views
    from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE
    from agent_py_agent.agent.conversation.message_selection import select_message_snapshot
    from agent_py_agent.tests.test_compact_scoped_checkpoint import (
        _checkpoint_path,
        _commit_transcript,
    )

    case = compact_case
    before, old, _ = _commit_transcript(case, scope=THREAD_COMPACT_SCOPE, summary='seed', content='old', publish=True)
    path = _checkpoint_path(case)
    row = json.loads(path.read_bytes())
    index = ImmutableJsonIndex(case.root / 'compact', ('evidence', case.thread_id, 'indexes'))
    row['carried_view']['messages'] = _max_depth_root(index, old.message_id, True)
    row['carried_view']['recent_messages'] = []
    row.update(generation=999999, previous_generation=999998, previous_checkpoint_id='synthetic-predecessor')
    row['checkpoint_id'] = checkpoints.scoped_compact_checkpoint_id(row)
    checkpoints._save_checkpoint_copy(case.root / 'compact', row)
    path.write_text(json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n')
    seeded = case.store.threads.update_atomic(case.thread_id, lambda current: replace(
        current, compact_generation=999999, compact_checkpoint_id=row['checkpoint_id'],
        metadata={**current.metadata, 'compact_commit_boundary': {
            'checkpoint_id': row['checkpoint_id'], 'generation': 999999,
            'boundary_message_id': current.compacted_through_message_id,
            'summary_sha256': row['summary_sha256'],
        }},
    ))
    reads, traversals = [], []
    original_load, original_items = ImmutableJsonIndex._load, ImmutableJsonIndex.items

    # LLM: 所有实际磁盘节点读取均计入，不使用返回值替身。
    # 函数用途: 为三条真实热入口测量精确节点访问数。
    def counted_load(self, reference):
        reads.append(self.parts)
        return original_load(self, reference)

    # LLM: 显式全表枚举仍执行原实现，但必须被测试发现，不能靠缓存掩盖累计扫描。
    # 函数用途: 捕捉热入口意外调用冷索引全表遍历。
    def counted_items(self, reference):
        traversals.append(self.parts)
        yield from original_items(self, reference)

    monkeypatch.setattr(ImmutableJsonIndex, '_load', counted_load)
    monkeypatch.setattr(ImmutableJsonIndex, 'items', counted_items)
    measurements = {}
    start = time.perf_counter()
    view = views.resolve_compact_summary_view(case.agent, seeded, THREAD_COMPACT_SCOPE)
    assert old.message_id in view.source_message_ids
    measurements['load'] = (time.perf_counter() - start, len(reads))
    assert measurements['load'][1] <= 70
    reads.clear()
    case.clock[0] = 20.0
    start = time.perf_counter()
    current, new, _ = _commit_transcript(case, scope=THREAD_COMPACT_SCOPE, summary='million', content='new', publish=True)
    measurements['commit'] = (time.perf_counter() - start, len(reads))
    assert current.compact_generation == 1000000
    assert measurements['commit'][1] <= 180
    reads.clear()
    latest = views.resolve_compact_summary_view(case.agent, current, THREAD_COMPACT_SCOPE)
    reads.clear()
    tail = case.store.messages.append({'thread_id': case.thread_id, 'role': 'user', 'content': 'tail'})
    start = time.perf_counter()
    selected = select_message_snapshot(case.store.messages, case.thread_id, exclude_message_ids=latest.source_message_ids)
    assert [item.message_id for item in selected] == [tail.message_id]
    measurements['selection'] = (time.perf_counter() - start, len(reads))
    assert measurements['selection'][1] <= 140
    assert traversals == []
    assert old.message_id in latest.source_message_ids and new.message_id in latest.source_message_ids
    assert _checkpoint_path(case).stat().st_size < 2097152
    # 墙钟受本机私有原子写入/fsync影响；此门禁约束本机上界，不冒称供应商或Linux的20ms性能。
    assert all(seconds < 1.0 for seconds, _ in measurements.values()), measurements
    request.node.user_properties.extend([
        ('synthetic_generation', current.compact_generation),
        ('max_index_depth', 64),
        ('hot_api_seconds_and_node_reads', json.dumps(measurements, sort_keys=True)),
    ])
