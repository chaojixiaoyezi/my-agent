# LLM: 原 ledger/metrics/store 的离线组合合同；用途、物理尝试、耗时和 provider usage 是不同事实，不猜真实缓存命中。
# 模块用途: 保留当前 main 新统计能力，确认公平排队事件和高命中摘要不会伪造主回答的缓存用量。
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.call_runtime import (
    record_model_call_finished,
    record_model_provider_attempt,
)
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.backends.cache_diagnostics import request_surface
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallFirstTokenParams,
    ModelCallLedger,
    ModelCallLedgerContext,
    ModelCallStartedParams,
)
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.model_metrics import (
    model_metrics_from_thread,
    publish_model_metrics,
)


# LLM: 虚拟时钟只驱动原账本记录；queued 从真实 call_runtime 过滤口进入，不能成为物理尝试或 cachehit 证据。
# 函数用途: 记录一次主或摘要模型边界，使用显式 provider usage，返回原完整响应。
def _settle(ledger, clock, thread_id, *, call_id, purpose, seconds, cached):
    surface = request_surface({'model': 'same-model', 'messages': [{'role': 'user', 'content': 'same-prefix'}]},
        'https://provider.invalid/v1')
    ledger.started(ModelCallStartedParams(call_id, 'fake', 'same-model', 100,
        request_id='request', run_id='run', metadata={'thread_id': thread_id, 'purpose': purpose,
            'auxiliary': purpose != 'main', 'logical_call_id': call_id}))
    original = ledger.records()[-1]
    for phase in ('waiting', 'admitted', 'cancelled'):
        record_model_provider_attempt(ledger, call_id, {'status': 'queued', 'provider_queue': {
            'phase': phase, 'wait_seconds': 3, 'admission_wait_reason': 'provider_concurrency'}})
    assert ledger.records()[-1] == original and original.provider_attempt_count == 0
    record_model_provider_attempt(ledger, call_id, {'status': 'started', 'attempt_id': call_id + '-http',
        'request_surface': surface})
    clock.now += 0.5
    ledger.first_token(ModelCallFirstTokenParams(call_id))
    clock.now += seconds - 0.5
    usage = {'prompt_tokens': 100, 'completion_tokens': 10}
    if cached is not None:
        usage['prompt_tokens_details'] = {'cached_tokens': cached}
    response = ModelResponse(text='完成', backend='fake', usage=usage)
    record_model_call_finished(ledger, call_id, response)
    return response


@pytest.mark.parametrize('main_cached', [None, 0, 60])
def test_main_and_compact_cache_usage_keep_original_purpose_latency_and_physical_counts(tmp_path, main_cached):
    store = ConversationStore(tmp_path / 'home' / 'conversations')
    thread = store.threads.get_or_create({'canonical_user_id': 'synthetic-owner'})
    clock = SimpleNamespace(now=10.0)
    ledger = ModelCallLedger(context=ModelCallLedgerContext(now=lambda: clock.now))
    main = _settle(ledger, clock, thread.thread_id, call_id='main', purpose='main', seconds=2, cached=main_cached)
    _settle(ledger, clock, thread.thread_id, call_id='summary', purpose='conversation_compact_summary', seconds=3, cached=80)
    summary = ledger.cumulative_summary(request_id='request', run_id='run')
    main_usage = summary['purpose_breakdown']['main']['usage_breakdown']['provider']
    compact_usage = summary['purpose_breakdown']['auxiliary']['usage_breakdown']['provider']
    assert main_usage['input_tokens'] == compact_usage['input_tokens'] == 100
    assert compact_usage['cache_read_input_tokens'] == 80
    assert main_usage['cache_read_input_tokens_reported_call_count'] == int(main_cached is not None)
    assert summary['provider_http_attempt_count'] == summary['physical_model_attempt_count'] == 2
    assert summary['model_latency_ms'] == 5000
    assert summary['purpose_breakdown']['main']['model_latency_ms'] == 2000
    assert summary['purpose_breakdown']['auxiliary']['model_latency_ms'] == 3000
    assert all(row.metadata['cache_diagnostic']['server_cache_state'] == 'unknown' for row in ledger.records())
    agent = SimpleNamespace(conversation_store=store, _model_call_ledger=ledger)
    params = SimpleNamespace(request_id='request', run_id='run', live_archive_state={},
        task_attributes={'conversation_thread_id': thread.thread_id})
    metrics = publish_model_metrics(agent, params, pending=False, response=main, call_id='main')
    assert metrics['cache_percent'] == main_cached
    assert metrics['request_model_latency_ms'] == 5000
    assert metrics['request_main_latency_ms'] == 2000 and metrics['request_auxiliary_latency_ms'] == 3000
    assert metrics['request_physical_model_attempt_count'] == metrics['request_logical_model_turn_count'] == 2
    replay = publish_model_metrics(agent, params, pending=False, response=main, call_id='main')
    assert replay['cache_percent'] == main_cached and replay['input_tokens'] == metrics['input_tokens']
    assert model_metrics_from_thread(ConversationStore(store.storage.root), thread.thread_id) == replay
    request = {'thread_id': thread.thread_id, 'request_id': 'request', 'run_id': 'run',
        'model_calls': {'schema': 'model_call_summary.v1', **summary}}
    store.model_usage.append_snapshot_once(request)
    store.model_usage.append_snapshot_once(request)
    events, errors = ConversationStore(store.storage.root).model_usage.events_report(thread.thread_id)
    assert not errors and len(events) == 1
    assert events[0].model_calls['model_latency_ms'] == 5000
    assert events[0].model_calls['provider_http_attempt_count'] == 2
    assert events[0].model_calls['purpose_breakdown']['auxiliary']['usage_breakdown']['provider']['cache_read_input_tokens'] == 80
