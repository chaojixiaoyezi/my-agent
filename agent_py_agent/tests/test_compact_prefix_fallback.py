# LLM: 真实 Compact/归档/checkpoint 仅使用显式临时 home 与本地摘要后端，不接触日常资料或网络。
# 模块用途: 保留原小窗口大历史的分段恢复能力，冻结主前缀只作为可容纳时的优化。
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.backends.errors import ProviderTransientError
from agent_py_agent.agent.conversation import compact, compact_request_budget
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.compact_provider_surface import (
    ConversationCompactModelSurface,
    ConversationCompactProviderSurface,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests.test_compact_quota_wake import _seed_large_history


# LLM: 前缀优化开关只在本测试剥离内部载体；原配置、历史、预算及 checkpoint 生产链全部真实执行。
# 函数用途: 在相同窗口和完整八条历史上记录 main 原来源与 F6 优化的实际摘要调用数。
def _original_window_compact(tmp_path, monkeypatch, *, freeze):
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path='memory.jsonl',
        my_agent_home=str(tmp_path / 'home')), tmp_path)
    sent, segments = [], []

    def generate(prompt, **kwargs):
        sent.append((prompt, kwargs))
        return ModelResponse(text='已完整核对旧资料，保留用户要求和未完成工作。', backend='fake')

    agent.backend = SimpleNamespace(name='compact-local-test', generate=generate)
    thread = agent.conversation_store.threads.get_or_create({'canonical_user_id': 'local'})
    _seed_large_history(agent.conversation_store, thread.thread_id, '')
    original = agent.conversation_store.messages.recent(thread.thread_id, limit=0)
    summarize_segments = compact_request_budget._summarize_segments
    resolved_surface = compact._resolved_provider_surface

    def segment(*args, **kwargs):
        segments.append(True)
        return summarize_segments(*args, **kwargs)

    def surface(request):
        result = resolved_surface(request)
        return result if freeze else replace(result, transcript_prefix=None)

    with monkeypatch.context() as patch:
        patch.setattr(compact_request_budget, '_summarize_segments', segment)
        patch.setattr(compact, '_resolved_provider_surface', surface)
        result = compact.prepare_conversation_context(agent, agent.conversation_store, thread,
            options=compact.ConversationCompactOptions(force=True,
                model_surface=ConversationCompactModelSurface(context_scope='conversation')))
    assert segments and sent, '原窗口必须通过完整来源分段恢复，不能要求用户换更大模型'
    assert result.compacted and result.thread.compact_generation == 1
    assert result.thread.compact_source_messages == len(original)
    assert agent.conversation_store.messages.recent(thread.thread_id, limit=0) == original
    return len(sent), len(segments)


def test_original_window_large_history_still_compacts_with_complete_source(tmp_path, monkeypatch):
    baseline = _original_window_compact(tmp_path / 'main', monkeypatch, freeze=False)
    optimized = _original_window_compact(tmp_path / 'f6xx', monkeypatch, freeze=True)
    assert optimized == baseline, '本地容量不足必须在发送前解除优化，不能多一次失败请求'
    print('original_window_summary_calls', {'main': baseline, 'F6': optimized})


@pytest.mark.parametrize('kind', ['cancel', 'network', 'identity'])
def test_prefix_fallback_never_consumes_cancel_network_or_identity_failures(monkeypatch, kind):
    errors = {'cancel': InterruptedError('取消'), 'network': ProviderTransientError('离线故障'),
              'identity': ConversationCompactError('来源身份变化', code='COMPACT_SOURCE_CHANGED')}
    observed = []

    def fail(*args, **kwargs):
        observed.append(True)
        raise errors[kind]

    monkeypatch.setattr(compact, '_summarize_once', fail)
    surface = ConversationCompactProviderSurface('stable', None, '', transcript_prefix=lambda _rows: None)
    with pytest.raises(type(errors[kind])) as failure:
        compact._summarize(SimpleNamespace(), '', {}, (), call=compact._CompactSummaryCall(provider_surface=surface))
    assert failure.value is errors[kind] and len(observed) == 1


def test_cancel_after_local_capacity_refusal_never_enters_source_fallback(monkeypatch):
    observed = []

    def fail(*args, **kwargs):
        observed.append(True)
        raise ConversationCompactError('本地容量不足', code='COMPACT_PREFIX_TOO_LARGE')

    monkeypatch.setattr(compact, '_summarize_once', fail)
    surface = ConversationCompactProviderSurface('stable', None, '', transcript_prefix=lambda _rows: None)
    with pytest.raises(InterruptedError):
        compact._summarize(SimpleNamespace(), '', {}, (), call=compact._CompactSummaryCall(
            provider_surface=surface, interrupt_check=lambda: True))
    assert len(observed) == 1
