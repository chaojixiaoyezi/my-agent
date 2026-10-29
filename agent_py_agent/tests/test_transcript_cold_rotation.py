# LLM: 只使用pytest临时owner与真实会话存储；种子及Compact必须选择同一组规范行，不能放松来源一致性检查。
# 模块用途: 回归历史冷热读取及Gateway Compact对背景审计、非聊天和本请求记录的隔离。
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import compact_transcript_prefix as prefixes
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.models import AUDIT_BACKGROUND_TRANSCRIPT_REASONS
from agent_py_agent.agent.gateway_parts import request_context
from agent_py_agent.tests.test_compact_scoped_checkpoint import compact_case as _compact_case

compact_case = _compact_case


# LLM: 审计行使用产品真实reason枚举和完整投递标记，不靠message_id文字推测后台身份。
# 函数用途: 创建夹在两条普通历史间的非模型行及非尾部当前请求记录。
def _gateway_history(case, noise):
    rows = []
    entries = [('user', 'old-user', {})]
    if noise == 'audit':
        entries.append(('assistant', 'background-turn-marker', {
            'task_id': 'audit-task', 'reason': sorted(AUDIT_BACKGROUND_TRANSCRIPT_REASONS)[0],
            'background_delivery_reason': 'watch-event',
        }))
    elif noise == 'current_request':
        entries.append(('assistant', 'current-request-middle', {'gateway_request_id': 'current'}))
    else:
        entries.append((noise, 'non-chat-marker', {}))
    entries.append(('assistant', 'old-answer', {}))
    for role, content, metadata in entries:
        rows.append(case.store.messages.append({
            'thread_id': case.thread_id, 'role': role, 'content': content, 'metadata': metadata,
        }))
    return rows


# LLM: 真实defer入口只略过模型调用；公共消息选源、冻结地址与主历史种子均不替换。
# 函数用途: 验证两条真实来源对审计、非聊天和中段本请求行逐ID一致。
@pytest.mark.parametrize('noise', ['audit', 'system', 'tool', 'display', 'current_request'])
def test_gateway_compact_source_matches_main_seed_projection(compact_case, noise):
    case = compact_case
    rows = _gateway_history(case, noise)
    case.agent.config = SimpleNamespace()
    inputs = request_context.GatewayConversationLoadRequest(
        agent=case.agent, request={}, request_id='current', prompt='继续', defer_compact=True,
    )
    loaded = request_context._load_gateway_compact_context(
        inputs, case.store, case.store.threads.require(case.thread_id),
    )
    compact_source = loaded[4]
    seed = request_context._gateway_history_source(compact_source.messages, 'current', None)
    actual_ids = tuple(row.message_id for row in compact_source.messages)
    seed_ids = tuple(row.message_id for row in seed.projected_rows())
    assert actual_ids == seed_ids == (rows[0].message_id, rows[-1].message_id)
    assert compact_source.messages.path.read_bytes().count(b'\n') == 3


# LLM: 使用同次真实冻结种子与provider历史；只替换主请求构造器，缺失/重排有效历史仍必须拒绝。
# 函数用途: 证明修复不把Compact来源不一致的安全检查变成静默过滤或空成功。
@pytest.mark.parametrize('changed', [False, True])
def test_gateway_prefix_keeps_real_source_change_guard(compact_case, monkeypatch, changed):
    case = compact_case
    _gateway_history(case, 'audit')
    case.agent.config = SimpleNamespace()
    inputs = request_context.GatewayConversationLoadRequest(
        agent=case.agent, request={}, request_id='current', prompt='继续', defer_compact=True,
    )
    source = request_context._load_gateway_compact_context(
        inputs, case.store, case.store.threads.require(case.thread_id),
    )[4]
    seed = request_context._gateway_history_source(source.messages, 'current', None)
    rows = seed.projected_rows()
    history = prefixes.provider_history_messages_from_rows(rows)
    frozen = SimpleNamespace(provider_history_messages=history, tool_ir_history=())
    monkeypatch.setattr(prefixes, 'project_tool_loop_request', lambda _frozen: SimpleNamespace(
        status='ready', messages=list(history),
    ))
    params = SimpleNamespace(conversation_history_seed=SimpleNamespace(source=seed))
    if changed:
        source = SimpleNamespace(messages=tuple(reversed(tuple(source.messages))))
        with pytest.raises(ConversationCompactError) as failure:
            prefixes.capture_transcript_prefix(frozen, source, prefixes.TranscriptPrefixSelection(params))
        assert failure.value.code == 'COMPACT_SOURCE_CHANGED'
    else:
        prefix = prefixes.capture_transcript_prefix(frozen, source, prefixes.TranscriptPrefixSelection(params))
        assert list(prefix(source.messages)) == list(history)


# LLM: 真实writer/CAS之后才检查冷转；阈值降低只缩小样本，不替换存储、覆盖和游标算法。
# 函数用途: 构造含旧展示行、精确幂等键和保留尾部的完整线程提交。
def _commit_rotation(case, monkeypatch, *, scope=None, publish=True, provider=None):
    from agent_py_agent.agent.conversation import compact_checkpoint as checkpoints
    from agent_py_agent.agent.conversation import message_scan
    from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE
    from agent_py_agent.agent.conversation.models import ConversationCompactCommit

    monkeypatch.setattr(message_scan, 'TRANSCRIPT_ROTATE_BYTES', 1024, raising=False)
    messages = []
    for number in range(24):
        messages.append(case.store.messages.append({
            'thread_id': case.thread_id, 'role': 'user', 'content': f'row-{number}:' + '正文' * 100,
            'metadata': {'dedupe_key': f'key-{number}'},
        }))
        if number == 1:
            case.store.messages.append({'thread_id': case.thread_id, 'role': 'display', 'content': 'old-progress'})
    path = case.store.messages.storage.message_path(case.thread_id)
    original = path.read_bytes()
    boundary = case.store.messages.byte_offset_after(case.thread_id, messages[-2].message_id)
    before = case.store.threads.require(case.thread_id)
    checkpoint_id = checkpoints.write_compact_checkpoint(case.agent, checkpoints.CompactCheckpointRequest(
        thread=before, summary='已覆盖旧行', operation_evidence={}, compact_rows=tuple(messages[:-1]),
        retained_tail=(messages[-1],), source_end_byte_offset=boundary,
        projected_tokens_before=900, projected_tokens_after=300, policy=case.policy, forced=False,
        scope=scope or THREAD_COMPACT_SCOPE, provider_compaction=provider,
    ))
    thread = case.store.threads.update_compact_state(case.thread_id, commit=ConversationCompactCommit(
        summary='已覆盖旧行', operation_evidence={}, checkpoint_id=checkpoint_id,
        compacted_through_message_id=messages[-2].message_id, compacted_through_byte_offset=boundary,
        source_messages=23, source_tool_pairs=0, publish_thread_view=publish,
    ), expected_generation=0)
    return thread, messages, original, path


# LLM: 冷行必须逐字保留；物理热文件变短不改变公开字节游标、历史、精确来源或旧幂等键。
# 函数用途: 验证全线程CAS真实触发冷转及冷热完整回读。
def test_thread_commit_rotates_prefix_and_preserves_history_cursors_and_dedupe(compact_case, monkeypatch):
    import json

    case = compact_case
    thread, messages, original, path = _commit_rotation(case, monkeypatch)
    assert path.with_suffix('.manifest.json').exists()
    manifest = json.loads(path.with_suffix('.manifest.json').read_text())
    assert manifest['hot_base'] > 0 and manifest['segment_count'] > 0
    assert messages[0].message_id.encode() not in path.read_bytes()
    assert messages[-2].message_id.encode() in path.read_bytes()
    archived = b''.join(part.read_bytes() for part in sorted((path.parent / 'archive' / case.thread_id).glob('seg-*.jsonl')))
    assert archived + path.read_bytes() == original
    end, errors = case.store.messages.complete_offset_report(case.thread_id)
    assert errors == [] and end == len(original)
    assert case.store.messages.byte_offset_after(case.thread_id, messages[-2].message_id) == thread.compacted_through_byte_offset
    tail, errors = case.store.messages.after_compact_report(thread)
    assert errors == [] and [row.message_id for row in tail] == [messages[-1].message_id]
    found, errors = case.store.messages.by_id_report(case.thread_id, messages[0].message_id)
    assert errors == [] and found == messages[0]
    found, errors = case.store.messages.by_id_report(case.thread_id, messages[-1].message_id)
    assert errors == [] and found == messages[-1]
    page = case.store.messages.history_page_report(case.thread_id, limit=100)
    assert not page.errors and len(page.rows) == 25 and page.after == len(original)
    rows, cursor, errors = case.store.messages.page_after_offset_report(case.thread_id, after=0, limit=100, through=end)
    assert errors == [] and len(rows) == 25 and cursor == end
    replay = case.store.messages.append_once({
        'thread_id': case.thread_id, 'role': messages[0].role, 'content': messages[0].content,
    }, dedupe_key='key-0')
    assert replay == messages[0] and case.store.messages.complete_offset_report(case.thread_id)[0] == end
    visited = []
    assert case.store.messages.visit_all_report(case.thread_id, lambda row: visited.append(row.message_id)) == []
    assert visited == [row.message_id for row in messages]


# LLM: 局部scope即使publish参数被误传为真，也不获得全线程冷转授权。
# 函数用途: 防止task/turn压缩提前归档其它任务尚需的原始消息。
@pytest.mark.parametrize('kind', ['task', 'turn'])
def test_local_scope_does_not_rotate_thread_prefix(compact_case, monkeypatch, kind):
    from agent_py_agent.agent.conversation.compact_scope import CompactScope

    scope = CompactScope(kind='task', task_id='A', created_at=5.0) if kind == 'task' else CompactScope(kind='turn', turn_id='turn-A')
    _, _, original, path = _commit_rotation(compact_case, monkeypatch, scope=scope, publish=False)
    assert not path.with_suffix('.manifest.json').exists() and path.read_bytes() == original


# LLM: 在清单读取和热文件打开之间真实轮转，模拟另一提交者；不替换原读取、位置计算或冷转实现。
# 函数用途: 防止并发冷热切换把旧清单与新热文件拼成错误的逻辑快照。
@pytest.mark.parametrize('entrypoint', ['reader', 'complete_offset', 'history', 'forward_page',
                                      'byte_offset', 'after_id', 'tail_marker'])
def test_read_snapshot_survives_rotation_between_manifest_and_open(compact_case, monkeypatch, entrypoint):
    from agent_py_agent.agent.conversation import message_scan

    rotate = message_scan.rotate_committed_transcript
    monkeypatch.setattr(message_scan, 'rotate_committed_transcript', lambda *_args: None)
    thread, messages, original, path = _commit_rotation(compact_case, monkeypatch)
    monkeypatch.setattr(message_scan, 'rotate_committed_transcript', rotate)
    expected_marker = compact_case.store.messages.tail_marker_report(thread.thread_id)
    manifest, switched = message_scan.transcript_manifest, []

    # LLM: 仅第一次读取制造真实布局转换，后续递归和重试都读取产品清单。
    # 函数用途: 精确触发读取快照最容易漏掉的旧清单/新inode竞态。
    def manifest_then_rotate(target):
        old = manifest(target)
        if not switched:
            switched.append(True)
            rotate(compact_case.store.messages.storage, thread)
        return old

    monkeypatch.setattr(message_scan, 'transcript_manifest', manifest_then_rotate)
    monkeypatch.setattr('agent_py_agent.agent.conversation.store_messages.transcript_manifest', manifest_then_rotate)
    monkeypatch.setattr('agent_py_agent.agent.conversation.history_page.transcript_manifest', manifest_then_rotate)
    if entrypoint == 'reader':
        with message_scan.TranscriptReader(path) as reader:
            assert reader.end == len(original) and reader.read() == original
    elif entrypoint == 'complete_offset':
        assert compact_case.store.messages.complete_offset_report(thread.thread_id) == (len(original), [])
    elif entrypoint == 'history':
        page = compact_case.store.messages.history_page_report(thread.thread_id, limit=100)
        assert not page.errors and page.after == len(original) and len(page.rows) == len(messages) + 1
    elif entrypoint == 'forward_page':
        rows, cursor, errors = compact_case.store.messages.page_after_offset_report(thread.thread_id, limit=100)
        assert not errors and cursor == len(original) and len(rows) == len(messages) + 1
    elif entrypoint == 'byte_offset':
        assert compact_case.store.messages.byte_offset_after(thread.thread_id, messages[-2].message_id) == thread.compacted_through_byte_offset
    elif entrypoint == 'after_id':
        rows, errors = compact_case.store.messages.after_report(thread.thread_id, after_message_id=messages[0].message_id, limit=100)
        assert not errors and [row.message_id for row in rows] == [row.message_id for row in messages[1:]]
    else:
        assert compact_case.store.messages.tail_marker_report(thread.thread_id) == expected_marker


# LLM: 只注入清单耐久写入故障，CAS、分段原字节、追加和恢复仍走产品入口；不是操作系统断电测试。
# 函数用途: 验证rename前后维护中断都不回滚已提交摘要、不丢旧历史，也不覆盖迟到追加。
@pytest.mark.parametrize('point', ['before_rename', 'after_rename'])
def test_rotation_pending_layout_preserves_append_and_retry(compact_case, monkeypatch, point):
    import json

    from agent_py_agent.agent.common import nofollow_fs
    from agent_py_agent.agent.conversation.message_scan import (
        TranscriptReader,
        recover_transcript_transition,
    )

    original_write, injected = nofollow_fs.write_text_atomic_beneath, []
    target = f'{compact_case.thread_id}.manifest.json'

    # LLM: 只在选定提交窗口故障一次，后续恢复继续使用原始安全写入器。
    # 函数用途: 分别保留旧热inode和已rename的新热inode，覆盖两种prepared状态。
    def fail_manifest_write(root, parts, text, **kwargs):
        if tuple(parts) == (target,) and not injected:
            pending = json.loads(text).get('phase') == 'prepared'
            if (point == 'before_rename' and pending) or (point == 'after_rename' and not pending):
                injected.append(True)
                if pending:
                    original_write(root, parts, text, **kwargs)
                raise OSError('injected manifest maintenance failure')
        return original_write(root, parts, text, **kwargs)

    monkeypatch.setattr(nofollow_fs, 'write_text_atomic_beneath', fail_manifest_write)
    thread, messages, original, path = _commit_rotation(compact_case, monkeypatch)
    monkeypatch.setattr(nofollow_fs, 'write_text_atomic_beneath', original_write)
    assert injected and thread.compact_generation == 1 and thread.summary == '已覆盖旧行'
    assert thread.metadata['compact_transcript_maintenance']['status'] == 'pending'
    assert json.loads(path.with_suffix('.manifest.json').read_text())['phase'] == 'prepared'
    with TranscriptReader(path) as reader:
        assert reader.read() == original
    late = compact_case.store.messages.append({'thread_id': thread.thread_id, 'role': 'assistant', 'content': '迟到的新尾行'})
    recover_transcript_transition(path)
    with TranscriptReader(path) as reader:
        whole = reader.read()
    assert whole.startswith(original) and whole.count(late.message_id.encode()) == 1
    assert compact_case.store.messages.append_once({
        'thread_id': thread.thread_id, 'role': messages[0].role, 'content': messages[0].content,
    }, dedupe_key='key-0') == messages[0]


# LLM: 改坏真实冷段而非mock返回值；所有对外读取必须报告失败，完整遍历不得提前回调部分历史。
# 函数用途: 验证冷段内容校验失败时，ID、分页和访问器都不会伪装成空成功。
@pytest.mark.parametrize('entrypoint', ['history', 'by_id', 'forward_page', 'visit_all'])
def test_cold_segment_corruption_is_reported(compact_case, monkeypatch, entrypoint):
    thread, messages, _, path = _commit_rotation(compact_case, monkeypatch)
    segment = next((path.parent / 'archive' / thread.thread_id).glob('seg-*.jsonl'))
    segment.write_bytes(segment.read_bytes().replace(b'row-0', b'bad-0', 1))
    if entrypoint == 'history':
        page = compact_case.store.messages.history_page_report(thread.thread_id, limit=100)
        assert page.errors and not page.rows
    elif entrypoint == 'by_id':
        row, errors = compact_case.store.messages.by_id_report(thread.thread_id, messages[0].message_id)
        assert errors and row is None
    elif entrypoint == 'forward_page':
        rows, cursor, errors = compact_case.store.messages.page_after_offset_report(thread.thread_id, limit=100)
        assert errors and not rows and cursor == 0
    else:
        seen = []
        assert compact_case.store.messages.visit_all_report(thread.thread_id, seen.append)
        assert seen == []


# LLM: visitor是调用方业务代码，验证通过后的业务异常必须原样传播，不能伪装成消息读坏。
# 函数用途: 保持冷热与未轮转账本的访问器异常语义一致。
def test_cold_visit_does_not_swallow_visitor_exception(compact_case, monkeypatch):
    thread, _, _, _ = _commit_rotation(compact_case, monkeypatch)

    # LLM: 明确模拟调用方异常，不改变消息存储或读取结果。
    # 函数用途: 给访问器异常提供可精确断言的故障点。
    def visitor(_row):
        raise RuntimeError('visitor business failure')

    with pytest.raises(RuntimeError, match='visitor business failure'):
        compact_case.store.messages.visit_all_report(thread.thread_id, visitor)


# LLM: provider不可读时使用真实resolver和选源恢复原文；冷热布局不能被当成当前后端已覆盖历史的证据。
# 函数用途: 回归已轮转会话换后端后仍能看到全部未被可用摘要覆盖的规范消息。
def test_provider_switch_restores_uncovered_cold_source(compact_case, monkeypatch):
    from agent_py_agent.agent.conversation.compact import load_conversation_compact_source
    from agent_py_agent.agent.conversation.compact_remote import provider_compaction_record
    from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE

    case = compact_case
    case.agent.config = SimpleNamespace()
    case.agent.backend.provider_compaction_scope = lambda: {
        'protocol': 'openai_responses', 'endpoint': 'https://provider.invalid/v1',
    }
    provider = provider_compaction_record(case.agent.backend, {'type': 'compaction', 'encrypted_content': 'test-only'})
    thread, messages, _, path = _commit_rotation(case, monkeypatch, provider=provider)
    assert path.with_suffix('.manifest.json').exists()
    case.agent.backend = SimpleNamespace(name='anthropic_compatible', model_name='other')
    source = load_conversation_compact_source(case.agent, case.store, thread, scope=THREAD_COMPACT_SCOPE)
    assert [row.message_id for row in source.messages] == [row.message_id for row in messages]
    source.messages.validate()


# LLM: 只在冷查询完成后注入一次真实轮转，原查找与热索引都照常执行；不能把存在的消息当成无错误未命中。
# 函数用途: 防止冷热两阶段ID查询在交接窗口静默丢失目标。
def test_by_id_retries_rotation_after_cold_lookup(compact_case, monkeypatch):
    from agent_py_agent.agent.conversation import message_scan, store_messages

    rotate = message_scan.rotate_committed_transcript
    monkeypatch.setattr(message_scan, 'rotate_committed_transcript', lambda *_args: None)
    thread, messages, _original, _path = _commit_rotation(compact_case, monkeypatch)
    monkeypatch.setattr(message_scan, 'rotate_committed_transcript', rotate)
    lookup, switched = store_messages.cold_message_lookup, []

    # LLM: 保留产品查找返回值，只在它返回和下一阶段开始之间切换布局。
    # 函数用途: 稳定复现旧热行移入冷段的最窄竞态窗口。
    def lookup_then_rotate(*args, **kwargs):
        result = lookup(*args, **kwargs)
        if not switched:
            switched.append(True)
            rotate(compact_case.store.messages.storage, thread)
        return result

    monkeypatch.setattr(store_messages, 'cold_message_lookup', lookup_then_rotate)
    found, errors = compact_case.store.messages.by_id_report(thread.thread_id, messages[0].message_id)
    assert not errors and found == messages[0] and switched


# LLM: 全量/尾读须保留旧EOF合同：坏半行不能被LF裁界藏掉，完整无LF的JSON仍可读取；仅使用测试副本。
# 函数用途: 对比冷热轮转后的全历史、近期与压缩尾读在无LF物理尾部的行为。
@pytest.mark.parametrize('entrypoint', ['visit_all', 'recent_all', 'recent_tail', 'after_compact'])
@pytest.mark.parametrize('tail_kind', ['broken', 'valid'])
def test_full_reads_validate_unfinished_hot_tail(compact_case, monkeypatch, entrypoint, tail_kind):
    thread, messages, _original, path = _commit_rotation(compact_case, monkeypatch)
    if tail_kind == 'broken':
        path.write_bytes(path.read_bytes() + b'{"message_id":')
    else:
        extra = compact_case.store.messages.append({
            'thread_id': thread.thread_id, 'role': 'user', 'content': '完整JSON末行没有LF',
        })
        messages.append(extra)
        path.write_bytes(path.read_bytes()[:-1])
    if entrypoint == 'visit_all':
        rows = []
        errors = compact_case.store.messages.visit_all_report(thread.thread_id, rows.append)
        if tail_kind == 'broken':
            assert rows == []
    elif entrypoint == 'after_compact':
        rows, errors = compact_case.store.messages.after_compact_report(thread)
    else:
        rows, errors = compact_case.store.messages.recent_report(
            thread.thread_id, limit=0 if entrypoint == 'recent_all' else 1,
        )
    if tail_kind == 'broken':
        assert errors
    else:
        assert not errors and rows[-1] == messages[-1]
        if entrypoint in ('visit_all', 'recent_all'):
            assert rows == messages


# LLM: 只移动测试副本的热文件，冷段和清单仍在；文件缺失不能将已有归档历史伪装成空会话。
# 函数用途: 验证完整冷热会话丢失热文件时各读取入口明确报错。
@pytest.mark.parametrize('entrypoint', ['recent_all', 'recent_tail', 'projection', 'visit_all', 'by_id', 'after_compact'])
def test_missing_hot_file_is_not_empty_history(compact_case, monkeypatch, entrypoint):
    thread, messages, _original, path = _commit_rotation(compact_case, monkeypatch)
    path.rename(path.with_suffix('.missing-hot-test'))
    if entrypoint == 'visit_all':
        visited = []
        errors = compact_case.store.messages.visit_all_report(thread.thread_id, visited.append)
        assert not visited
    elif entrypoint == 'by_id':
        row, errors = compact_case.store.messages.by_id_report(thread.thread_id, messages[0].message_id)
        assert row is None
    elif entrypoint == 'after_compact':
        _rows, errors = compact_case.store.messages.after_compact_report(thread)
    elif entrypoint == 'projection':
        _rows, errors = compact_case.store.messages.recent_projection_report(thread.thread_id, limit=1, project=str)
    else:
        _rows, errors = compact_case.store.messages.recent_report(
            thread.thread_id, limit=0 if entrypoint == 'recent_all' else 1,
        )
    assert errors
