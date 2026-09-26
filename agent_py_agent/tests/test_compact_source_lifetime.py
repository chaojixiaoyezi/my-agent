"""真实消息文件到摘要提交的来源完整性与正文生命周期。"""
from __future__ import annotations

import gc
import hashlib
import json
import tracemalloc
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.conversation import ConversationStore, compact_request_budget
from agent_py_agent.agent.conversation.compact import (
    ConversationCompactOptions,
    load_conversation_compact_source,
    prepare_conversation_context,
)
from agent_py_agent.agent.conversation.compact_checkpoint import committed_compact_checkpoint_chain
from agent_py_agent.agent.conversation.compact_projection import ConversationCompactProjection
from agent_py_agent.agent.conversation.compact_provider_surface import (
    ConversationCompactProviderSurface,
)
from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE
from agent_py_agent.agent.conversation.message_replay import MessageSnapshotRows
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.tests.test_compact_request_budget import segment_part


# LLM: 夹具逐行写真实canonical文件，只返回轻量hash/ID；测量前不保留正文或provider列表，不替换生产reader/摘要/提交。
# 函数用途: 构造全部选中的大历史，以真实分段和CAS验证来源驻留，不发网络请求。
def _case(tmp_path, count=128):
    store = ConversationStore(tmp_path / 'conversation')
    thread = store.threads.get_or_create({'canonical_user_id': 'owner', 'now': 1})
    path = store.storage.message_path(thread.thread_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    expected = hashlib.sha256(b'[')
    ids, body_chars = [], 0
    with path.open('wb') as handle:
        for index in range(count):
            content = f'原文{index}:' + 'x' * 32768 + f':结束{index}'
            role = 'user' if index % 2 == 0 else 'assistant'
            message_id = f'm-{index}'
            ids.append(message_id)
            body_chars += len(content)
            handle.write((json.dumps({'message_id': message_id, 'thread_id': thread.thread_id,
                'role': role, 'content': content, 'metadata': {}}, ensure_ascii=False) + '\n').encode())
            if index:
                expected.update(b', ')
            expected.update(json.dumps({'role': role, 'content': [{'type': 'text', 'text': content}]},
                                       ensure_ascii=False).encode())
    expected.update(b']')
    agent = SimpleNamespace(
        conversation_store=store, home_paths=SimpleNamespace(owner_compact_dir=tmp_path / 'compact'),
        backend=SimpleNamespace(name='fake', model_name='fake', max_tokens=128),
        prompts=SimpleNamespace(build=lambda prompt, *_args, **_kwargs: prompt),
        config=SimpleNamespace(model_context_window_tokens=20_000, model_context_window_explicit=True),
    )
    return agent, thread, ids, expected.digest(), body_chars


@pytest.mark.parametrize('count', [128, 256])
def test_selected_history_to_summary_commit_releases_source_bodies(tmp_path, monkeypatch, count):
    agent, thread, ids, expected, body_chars = _case(tmp_path, count=count)
    digest = hashlib.sha256()
    covered, calls, resident = 0, 0, []

    def generate(request):
        nonlocal covered, calls
        assert request.messages is None, '该来源必须完整走真实多段摘要'
        start, end, _, part = segment_part(request.prompt)
        assert start == covered
        assert len(part) == end - start
        digest.update(part.encode())
        covered, calls = end, calls + 1
        resident.append(tracemalloc.get_traced_memory()[0])
        return ModelResponse(text='已完整核对原始材料，保留待办及来源。', backend='fake')

    monkeypatch.setattr(compact_request_budget, 'generate_auxiliary_model_response', generate)
    gc.collect()
    tracemalloc.start()
    try:
        source = load_conversation_compact_source(agent, agent.conversation_store, thread, scope=THREAD_COMPACT_SCOPE)
        result = prepare_conversation_context(agent, agent.conversation_store, thread, options=ConversationCompactOptions(
            source=source, force=True, exclude_request_id='current-request',
            provider_surface=ConversationCompactProviderSurface('稳定前缀', None, '摘要系统'),
        ))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert calls > 2 and digest.digest() == expected
    assert result.compacted and result.thread.compact_generation == 1
    chain = committed_compact_checkpoint_chain(agent, result.thread)
    assert len(chain) == 1 and chain[0]['source_message_ids'] == ids
    assert len(source.messages) == len(ids)
    print(f'source summary body={body_chars} peak={peak} resident={max(resident)} calls={calls}')
    assert peak < body_chars // 2, '选中历史或摘要provider正文仍在整份驻留'


# LLM: 只给原入口传入同次scope和真实provider面，不替换reader/分段器/检查点/CAS；额外参数用于边界控制。
# 函数用途: 让生命周期案例复用与内存样例相同的正式摘要通路。
def _prepare(agent, thread, source, **kwargs):
    return prepare_conversation_context(agent, agent.conversation_store, thread, options=ConversationCompactOptions(
        source=source, force=kwargs.pop('force', True), exclude_request_id='current-request',
        provider_surface=ConversationCompactProviderSurface('稳定前缀', None, '摘要系统'), **kwargs,
    ))


@pytest.mark.parametrize('failure', ['transport', 'cancel', 'pre_cas_cancel', 'rewrite', 'pre_cas_rewrite', 'cas'])
def test_failed_summary_or_commit_preserves_original_coverage(tmp_path, monkeypatch, failure):
    agent, thread, _, _, _ = _case(tmp_path, count=4)
    store, stopped, calls = agent.conversation_store, [False], []
    source = load_conversation_compact_source(agent, store, thread, scope=THREAD_COMPACT_SCOPE)
    path = store.storage.message_path(thread.thread_id)
    before = path.read_bytes()

    def generate(_request):
        calls.append(True)
        if failure == 'transport':
            raise RuntimeError('provider unavailable')
        if failure == 'cancel':
            stopped[0] = True
        if failure == 'rewrite':
            path.write_bytes(before.replace(b'xxxx', b'yyyy', 1))
        return ModelResponse(text='保留已知事实与未完成工作。', backend='fake')

    def progress(event):
        if failure == 'pre_cas_rewrite' and event['stage'] == 'committing':
            path.write_bytes(before.replace(b'xxxx', b'yyyy', 1))
        if failure == 'pre_cas_cancel' and event['stage'] == 'committing':
            stopped[0] = True

    if failure == 'cas':
        original = store.threads.update_compact_state

        def conflict(thread_id, **kwargs):
            kwargs['expected_generation'] += 1
            return original(thread_id, **kwargs)

        monkeypatch.setattr(store.threads, 'update_compact_state', conflict)
    monkeypatch.setattr(compact_request_budget, 'generate_auxiliary_model_response', generate)
    expected_error = InterruptedError if failure.endswith('cancel') else DataCorruptionError if 'rewrite' in failure else RuntimeError
    with pytest.raises(expected_error):
        _prepare(agent, thread, source, interrupt_check=lambda: stopped[0], progress_callback=progress)
    current = store.threads.require(thread.thread_id)
    assert calls and current.compact_generation == 0 and not current.compact_checkpoint_id and not current.summary
    assert committed_compact_checkpoint_chain(agent, current) == ()
    assert current.compacted_through_byte_offset == 0 and not current.compacted_through_message_id
    assert current.compact_consecutive_failures == (0 if failure.endswith('cancel') else 1)
    assert path.read_bytes() == (before.replace(b'xxxx', b'yyyy', 1) if 'rewrite' in failure else before)


def test_late_append_during_summary_remains_next_source(tmp_path, monkeypatch):
    agent, thread, ids, _, _ = _case(tmp_path, count=4)
    store, late = agent.conversation_store, []
    source = load_conversation_compact_source(agent, store, thread, scope=THREAD_COMPACT_SCOPE)

    def generate(_request):
        if not late:
            late.append(store.messages.append({'thread_id': thread.thread_id, 'role': 'user', 'content': '摘要期间的新消息'}))
        return ModelResponse(text='只概括冻结来源。', backend='fake')

    monkeypatch.setattr(compact_request_budget, 'generate_auxiliary_model_response', generate)
    result = _prepare(agent, thread, source)
    assert committed_compact_checkpoint_chain(agent, result.thread)[0]['source_message_ids'] == ids
    assert [row.message_id for row in source.messages] == ids
    following = load_conversation_compact_source(agent, store, result.thread, scope=THREAD_COMPACT_SCOPE)
    assert following.messages == tuple(late)


@pytest.mark.parametrize('second', ['oversized', 'summary_failure'])
def test_two_candidates_commit_own_retained_snapshot_and_projection(tmp_path, monkeypatch, second):
    agent, thread, ids, _, _ = _case(tmp_path, count=4)
    store, tail, views, materials = agent.conversation_store, [], [], []
    for role in ('user', 'assistant'):
        tail.append(store.messages.append({'thread_id': thread.thread_id, 'role': role, 'content': '近期完整问答'}))
    before = store.storage.message_path(thread.thread_id).read_bytes()
    source = load_conversation_compact_source(agent, store, thread, scope=THREAD_COMPACT_SCOPE)

    def project(view):
        views.append(view)
        materials.append(object())
        tokens = source.policy.trigger_tokens - 1 if len(views) == 2 else source.policy.trigger_tokens + 1
        return ConversationCompactProjection(tokens, materials[-1])

    def generate(_request):
        if second == 'summary_failure' and len(views) == 2:
            raise RuntimeError('第二候选摘要失败')
        return ModelResponse(text='第一候选仍保留近期问答。', backend='fake')

    monkeypatch.setattr(compact_request_budget, 'generate_auxiliary_model_response', generate)
    result = _prepare(agent, thread, source, force=False, request_projector=project)
    # 超上限的第二候选会先缩小原话备份再计量一次（本替身按调用次序给大小，仍超限），然后才放弃。
    assert len(views) == (4 if second == 'oversized' else 2)
    assert all(isinstance(view.messages, MessageSnapshotRows) for view in views[:2])
    assert result.messages == tuple(tail) and result.request_projection.material is materials[1]
    chain = committed_compact_checkpoint_chain(agent, result.thread)
    assert result.thread.compact_generation == 1 and chain[0]['source_message_ids'] == ids
    assert chain[0]['retained_tail_message_ids'] == [row.message_id for row in tail]
    assert store.storage.message_path(thread.thread_id).read_bytes() == before


def test_scoped_source_loader_checks_cancel_during_initial_scan(tmp_path):
    agent, thread, _, _, _ = _case(tmp_path, count=4)
    checks = []

    def interrupted():
        checks.append(True)
        return len(checks) >= 3

    with pytest.raises(InterruptedError):
        load_conversation_compact_source(agent, agent.conversation_store, thread,
                                        scope=THREAD_COMPACT_SCOPE, interrupt_check=interrupted)
    assert len(checks) == 3


def test_source_cancellation_is_not_erased_by_default_options(tmp_path):
    agent, thread, _, _, _ = _case(tmp_path, count=2)
    stopped = [False]
    source = load_conversation_compact_source(agent, agent.conversation_store, thread,
        scope=THREAD_COMPACT_SCOPE, interrupt_check=lambda: stopped[0])
    stopped[0] = True
    with pytest.raises(InterruptedError):
        _prepare(agent, thread, source)
    assert agent.conversation_store.threads.require(thread.thread_id).compact_generation == 0
