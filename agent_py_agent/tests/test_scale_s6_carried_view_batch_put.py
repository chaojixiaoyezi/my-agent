"""批量构建 carried HAMT 保持原根字节、重复项语义及近期窗口，并减少实际存储节点。"""
from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import compact_checkpoint as checkpoints
from agent_py_agent.agent.conversation import compact_summary_view as views
from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE
from agent_py_agent.agent.conversation.store_io import ImmutableJsonIndex


def _ref(identity):
    return {'run_id': 'run', 'attempt_id': 'attempt', 'turn_id': 'turn', 'call_id': identity}


def _key(ref):
    return json.dumps(ref, sort_keys=True)


def _index(tmp_path):
    return ImmutableJsonIndex(tmp_path / 'compact', ('evidence', 'thread', 'indexes'))


def _put_each(index, items):
    root = None
    for key, value in items:
        root = index.put(root, key, value)
    return root


def _prepare(index, monkeypatch, view, messages=(), tools=()):
    # 仅固定已选摘要视图；真实 HAMT、节点序列化和文件读写始终走产品实现。
    monkeypatch.setattr(views, 'resolve_compact_summary_view', lambda *_args: view)
    thread = SimpleNamespace(thread_id='thread', compact_checkpoint_id='', compact_generation=0)
    agent = SimpleNamespace(home_paths=SimpleNamespace(owner_compact_dir=index.root))
    request = SimpleNamespace(thread=thread, scope=THREAD_COMPACT_SCOPE)
    row = {'thread_id': thread.thread_id, 'summary_base_checkpoint_id': view.checkpoint_id,
           'scope': THREAD_COMPACT_SCOPE.to_dict(),
           'source_message_ids': list(messages), 'source_tool_refs': list(tools)}
    return checkpoints._prepare_carried_view(agent, request, row)


def _seed_view(index, count):
    messages = [f'old-message-{number}' for number in range(count)]
    refs = [_ref(f'old-call-{number}') for number in range(count)]
    descriptor = {'checkpoint_id': 'seed-head', 'generation': 1, 'created_at': 1.0}
    base = {
        'messages': index.put_absent(None, ((identity, True) for identity in messages)),
        'tools': index.put_absent(None, ((_key(ref), {'ref': ref, 'order': number})
                                      for number, ref in enumerate(refs))),
        'message_count': count, 'tool_count': count,
        'recent_messages': messages[-256:], 'recent_tools': refs[-128:],
        'checkpoint_lineage': index.put_absent(None, [('seed-head', True)]),
        'readable_bases': index.put_absent(None, [('*', descriptor)]),
    }
    return views.CompactSummaryView(checkpoint_id='seed-head', carried_metadata=base)


def _node_files(index):
    from agent_py_agent.agent.conversation.immutable_node_store import ImmutableNodeStore

    names = {path.name for path in index.root.joinpath(*index.parts).glob('*.json')}
    store = ImmutableNodeStore(index.root, index.parts)
    if store.exists():
        with store.connection() as conn:
            names.update(row[0] for row in conn.execute('SELECT name FROM nodes WHERE raw IS NOT NULL'))
    return names


def _reachable_nodes(index, carried):
    pending = [carried.get(key) for key in (
        'messages', 'tools', 'checkpoint_lineage', 'readable_bases', 'scope_heads')]
    found = set()
    while pending:
        reference = pending.pop()
        if reference is None or reference['ref']['ref'] in found:
            continue
        found.add(reference['ref']['ref'])
        node = index._load(reference)
        pending.extend(node.get('children', {}).values())
    return found


def test_legacy_duplicates_keep_last_tool_value_and_occurrence_counts(tmp_path, monkeypatch):
    index = _index(tmp_path)
    a, b, c, d = [_ref(name) for name in ('调用甲', 'b', 'c', 'd')]
    reordered_a = dict(reversed(tuple(a.items())))
    view = views.CompactSummaryView(
        checkpoint_id='legacy-head', source_message_ids=('a', 'a', 'b'),
        source_tool_refs=(a, b, reordered_a), legacy_message_end_ids=('legacy-end',))
    carried = _prepare(index, monkeypatch, view, ('b', 'c', 'c', 'd'), (a, c, d, c))
    oracle = ImmutableJsonIndex(tmp_path / 'oracle', ('index',))
    expected_tools = {_key(a): {'ref': a, 'order': 2}, _key(b): {'ref': b, 'order': 1},
                      _key(c): {'ref': c, 'order': 3}, _key(d): {'ref': d, 'order': 4}}

    assert carried['messages'] == _put_each(oracle, dict.fromkeys(('a', 'b', 'c', 'd'), True).items())
    assert carried['tools'] == _put_each(oracle, expected_tools.items())
    assert carried['message_count'] == carried['tool_count'] == 5
    assert dict(index.items(carried['tools'])) == expected_tools
    assert carried['recent_messages'] == ['a', 'b', 'c', 'd']
    assert carried['recent_tools'] == [a, b, c, d]
    assert carried['checkpoint_lineage'] == _put_each(oracle, [('legacy-head', True)])
    assert carried['legacy_message_end_ids'] == ['legacy-end']
    assert carried['schema'] == 'conversation_carried_view.v1'
    assert carried['base_checkpoint_id'] == 'legacy-head'
    assert carried['scope'] == THREAD_COMPACT_SCOPE.to_dict()
    assert carried['scope_heads'] is carried['thread_timeline'] is carried['readable_bases'] is None


def test_current_batch_keeps_first_values_roots_and_recent_windows(tmp_path, monkeypatch):
    index = _index(tmp_path)
    view = _seed_view(index, 1)
    before = deepcopy(view.carried_metadata)
    old_message, old_ref = before['recent_messages'][0], before['recent_tools'][0]
    messages = [f'new-message-{number}' for number in range(260)]
    refs = [_ref(f'new-call-{number}') for number in range(132)]
    carried = _prepare(index, monkeypatch, view,
        [old_message, *messages, messages[0], old_message],
        [old_ref, *refs, dict(reversed(tuple(refs[0].items()))), old_ref])
    oracle = ImmutableJsonIndex(tmp_path / 'oracle', ('index',))
    expected_messages = dict.fromkeys([old_message, *messages], True)
    expected_tools = {_key(ref): {'ref': ref, 'order': number}
                      for number, ref in enumerate([old_ref, *refs])}

    assert carried['messages'] == _put_each(oracle, expected_messages.items())
    assert carried['tools'] == _put_each(oracle, expected_tools.items())
    assert carried['message_count'] == 261 and carried['tool_count'] == 133
    assert carried['recent_messages'] == messages[-256:]
    assert carried['recent_tools'] == refs[-128:]
    assert view.carried_metadata == before
    assert carried['checkpoint_lineage'] == before['checkpoint_lineage']
    assert carried['readable_bases'] == before['readable_bases']
    assert index.get(before['messages'], messages[0]) is None
    assert index.get(before['tools'], _key(refs[0])) is None
    assert index.get(carried['tools'], _key(refs[0])) == {'ref': refs[0], 'order': 1}


@pytest.mark.parametrize('replay', [False, True])
def test_empty_or_replayed_batch_preserves_roots_and_node_files(tmp_path, monkeypatch, replay):
    index = _index(tmp_path)
    view = _seed_view(index, 16)
    before = deepcopy(view.carried_metadata)
    files = _node_files(index)
    carried = _prepare(index, monkeypatch, view,
        before['recent_messages'] * 2 if replay else (),
        before['recent_tools'] * 2 if replay else ())

    for key in ('messages', 'tools', 'message_count', 'tool_count', 'readable_bases', 'checkpoint_lineage'):
        assert carried[key] == before[key]
    assert _node_files(index) == files
    assert view.carried_metadata == before


@pytest.mark.parametrize('mode', ['fresh', 'legacy', 'existing'])
def test_batch_writes_only_nodes_reachable_from_final_roots(
    tmp_path, monkeypatch, record_property, mode
):
    index = _index(tmp_path)
    messages = [f'new-message-{number}' for number in range(64)]
    refs = [_ref(f'new-call-{number}') for number in range(64)]
    messages += messages[:3]
    refs += refs[:3]
    if mode == 'legacy':
        view = views.CompactSummaryView(checkpoint_id='legacy-head',
            source_message_ids=tuple(messages), source_tool_refs=tuple(refs))
        additions, tool_additions = (), ()
    else:
        view = _seed_view(index, 16) if mode == 'existing' else views.CompactSummaryView()
        additions, tool_additions = messages, refs
    before = _node_files(index)
    carried = _prepare(index, monkeypatch, view, additions, tool_additions)
    created = _node_files(index) - before
    orphaned = created - _reachable_nodes(index, carried)
    record_property('new_nodes', len(created))
    record_property('unreachable_nodes', len(orphaned))

    assert created
    assert not orphaned, f'{len(orphaned)} 个中间版本节点不属于最终根'
    assert len(dict(index.items(carried['messages']))) == (80 if mode == 'existing' else 64)
    assert len(dict(index.items(carried['tools']))) == (80 if mode == 'existing' else 64)
