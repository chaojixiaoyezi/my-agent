# LLM: 正式包内回归复用独立 Compact 探针；只读冻结输入并使用临时 store/fake 调用，不写日常 home、不连真实模型或网络。
# 模块用途: 独立核对历史前缀、当前媒体屏障、有界恢复与迟到遥测 CAS，正文和断言沿原非作者证据保留。
"""Compact 独立合同：从非作者探针原样迁入正式测试，覆盖与运行行为均保留。"""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.compact_transcript_prefix import (
    TranscriptInputPrefix,
    TranscriptPrefixSelection,
    _joint_prefix_end,
)
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.backends.errors import ProviderConfigurationError
from agent_py_agent.agent.backends.tool_ir import CompactionSummary, RuntimeFactsTurn, UserTurn
from agent_py_agent.agent.conversation import compact
from agent_py_agent.agent.conversation import compact_request_budget as budget
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.compact_message_source import CompactMessageSource
from agent_py_agent.agent.conversation.compact_provider_surface import (
    ConversationCompactProviderSurface,
)
from agent_py_agent.agent.conversation.context_usage import (
    record_model_context_usage,
    save_context_usage_snapshot,
)
from agent_py_agent.agent.conversation.native_history import provider_history_messages_from_rows
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.conversation.tool_context_window import native_carried_tool_handoff
from agent_py_agent.tests.test_compact_request_budget import _request
from agent_py_agent.tests.test_conversation_context_usage import usage


def test_prefix_continuity_replay_copy_and_protected_media():
    rows = tuple(SimpleNamespace(message_id=f'non-author-{i}', role='user', content=f'独立原文{i}', metadata={})
                 for i in range(4))
    history = tuple(provider_history_messages_from_rows(rows))
    prefix = TranscriptInputPrefix(deepcopy(history), 0, rows, tuple(row.message_id for row in rows))
    replay = list(prefix(rows))
    replay[0]['content'] = '调用方不得污染下一次重放'
    assert list(prefix(rows)) == list(history)
    for selection in ((), rows[1:], (rows[0], rows[2]), tuple(reversed(rows))):
        with pytest.raises(ConversationCompactError) as error:
            prefix(selection)
        assert error.value.code == 'COMPACT_SOURCE_CHANGED'
    joint = replace(prefix, joint_end=len(history))
    with pytest.raises(ConversationCompactError):
        joint(rows[:2])
    records = ({'call_id': 'selected-exact'},)
    context = ['独立工具结果']
    handoff = CompactionSummary(native_carried_tool_handoff(context, records), source='carried_tool_handoff')
    media = ({'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'offline-sentinel'}},)
    facts = RuntimeFactsTurn('保留当前宿主事实', source='conversation.runtime', media=media)
    source = SimpleNamespace(source_ir_history=(), source_records=records, retained_records=())
    params = SimpleNamespace(archive_tool_calls=records)
    frozen = SimpleNamespace(provider_history_messages=history, tool_context=tuple(context),
        tool_ir_history=(UserTurn('保留当前要求'), facts, handoff))
    assert _joint_prefix_end(frozen, source, TranscriptPrefixSelection(params)) == 0
    frozen.tool_ir_history = (UserTurn('保留当前要求'), handoff, facts)
    assert _joint_prefix_end(frozen, source, TranscriptPrefixSelection(params)) > len(history)
    foreign = SimpleNamespace(source_ir_history=(), source_records=({'call_id': 'foreign'},), retained_records=())
    assert _joint_prefix_end(frozen, foreign, TranscriptPrefixSelection(params)) == 0
    assert facts.media == media and facts.text == '保留当前宿主事实'


def test_empty_retry_is_once_and_capacity_fallback_is_typed_once(monkeypatch):
    for second in (ModelResponse(text='', backend='offline'),
                   ModelResponse(text='', stop_reason='length', backend='offline'),
                   ProviderConfigurationError('offline-provider-denial')):
        calls = []
        def generate(candidate):
            calls.append(candidate)
            if len(calls) == 1:
                return ModelResponse(text=' ', backend='offline')
            if isinstance(second, BaseException):
                raise second
            return second
        monkeypatch.setattr(budget, 'generate_auxiliary_model_response', generate)
        request = _request('完整材料保持原样')
        if isinstance(second, BaseException):
            with pytest.raises(ProviderConfigurationError):
                budget.generate_bounded_compact_response(request)
        else:
            assert budget.generate_bounded_compact_response(request) is second
        assert len(calls) == 2 and calls[0] == calls[1]
    surface = ConversationCompactProviderSurface('stable', None, '', transcript_prefix=lambda _rows: None)
    seen = []
    def too_large(*args, **kwargs):
        seen.append(kwargs['call'].provider_surface.transcript_prefix)
        raise ConversationCompactError('same typed code', code='COMPACT_PREFIX_TOO_LARGE')
    monkeypatch.setattr(compact, '_summarize_once', too_large)
    with pytest.raises(ConversationCompactError):
        compact._summarize(SimpleNamespace(), '', {}, (), call=compact._CompactSummaryCall(provider_surface=surface))
    assert len(seen) == 2 and seen[0] is not None and seen[1] is None
    seen.clear()
    def text_is_not_authority(*args, **kwargs):
        seen.append(True)
        raise ConversationCompactError('COMPACT_PREFIX_TOO_LARGE', code='COMPACT_SOURCE_CHANGED')
    monkeypatch.setattr(compact, '_summarize_once', text_is_not_authority)
    with pytest.raises(ConversationCompactError):
        compact._summarize(SimpleNamespace(), '', {}, (), call=compact._CompactSummaryCall(provider_surface=surface))
    assert seen == [True]


def test_late_model_context_snapshot_does_not_rebind_to_fresh_revision(tmp_path):
    store = ConversationStore(tmp_path / 'isolated-home')
    captured = store.threads.get_or_create({'canonical_user_id': 'owner-a'})
    old_usage = usage(12345) | {'model_name': 'sampled-model-old'}
    selected = store.threads.update_atomic(captured.thread_id, lambda row: replace(row,
        model_profile_id='model-new', model_selection_revision=1,
        model_selection_source='explicit', model_selection_last_explicit_revision=1, model_context_usage={}))
    assert not save_context_usage_snapshot(store, captured, old_usage), '显式旧thread快照正确被CAS拒绝'
    agent = SimpleNamespace(conversation_store=store)
    params = SimpleNamespace(context_scope='conversation', task_attributes={'conversation_thread_id': captured.thread_id})
    assert not record_model_context_usage(agent, params, old_usage), '迟到旧采样不能在fresh load后盖上新revision'
    assert store.threads.require(selected.thread_id).model_context_usage == {}
