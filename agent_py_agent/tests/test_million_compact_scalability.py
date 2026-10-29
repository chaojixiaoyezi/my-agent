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
