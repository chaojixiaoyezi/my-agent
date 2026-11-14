"""同一前台请求的容量切片只限制连续无进展；不重置原工具预算或签发重放权限。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.gateway_parts import request_execution as target
from agent_py_agent.agent.gateway_parts.request_context import GatewayAskRunContext


def record(attempt, number, **changes):
    return {'run_id': 'run-main', 'attempt_id': attempt, 'turn_id': f'model-turn-{number}', 'call_id': f'call-{number}',
            'request_id': 'req-main', 'conversation_request_id': 'req-main', 'tool': 'read_file',
            'ok': True, 'handler_executed': True, 'effect_outcome': 'none', **changes}


def harness(tmp_path, monkeypatch, run):
    request = {'id': 'req-main', 'execution_attempt_id': 'transport'}
    context = GatewayAskRunContext(agent=SimpleNamespace(run=run), request=request, request_id='req-main',
        request_path=tmp_path / 'request.json', response_path=tmp_path / 'response.json', on_chunk=lambda event: None)
    conversation = SimpleNamespace(compact_generation=0)
    monkeypatch.setattr(target, '_gateway_recovered_active_turn_tool_calls', lambda ctx: [])
    monkeypatch.setattr(target, '_recover_gateway_active_turn_authority', lambda *args: None)
    monkeypatch.setattr(target, '_gateway_run_params', lambda values: SimpleNamespace(
        carried_archive_tool_calls=list(values.carried_archive_tool_calls)))
    monkeypatch.setattr(target, '_gateway_compact_overflowing_turn', lambda *args, **kwargs: conversation)
    return context, conversation


def bind(context, attempt):
    context.request['runtime_authority'] = {'schema_version': 'gateway_runtime_authority.v1',
        'request_id': context.request_id, 'gateway_execution_attempt_id': 'transport', 'task_id': 'task-main',
        'run_id': 'run-main', 'agent_run_id': 'agent-main', 'attempt_id': attempt}


def test_healthy_tool_slices_pass_the_ninth_without_resetting_carried_budget(tmp_path, monkeypatch):
    rows, calls = [], []

    def run(prompt, *, params):
        calls.append(params)
        attempt = f'a-{len(calls)}'
        bind(context, attempt)
        assert len(params.carried_archive_tool_calls) == len(rows)
        rows.append(record(attempt, len(calls)))
        return SimpleNamespace(runtime_status='context_overflow' if len(calls) <= 12 else 'ok',
            runtime_source='preflight', archive_tool_calls=list(rows), active_turn_user_inputs=[])

    context, conversation = harness(tmp_path, monkeypatch, run)
    result, _ = target._run_gateway_turn_with_conversation_compact(context, '继续', conversation)
    assert result.runtime_status == 'ok' and len(calls) == 13
    assert [len(p.carried_archive_tool_calls) for p in calls] == list(range(13))


@pytest.mark.parametrize('mutation', [
    {'ok': False}, {'handler_executed': False}, {'effect_outcome': 'unknown'},
    {'tool_operation_replayed': True}, {'tool_operation_status': 'unknown'},
    {'conversation_request_id': 'foreign'}, {'request_id': 'foreign'}, {'call_id': ''},
    {'attempt_id': 'raw-old-attempt'}, {'handler_executed': None},
])
def test_eight_no_confirmed_progress_stops_even_when_attempt_and_generation_change(tmp_path, monkeypatch, mutation):
    rows, calls = [], []

    def run(prompt, *, params):
        calls.append(params)
        attempt = f'a-{len(calls)}'
        bind(context, attempt)
        rows.append(record(attempt, len(calls), **mutation))
        return SimpleNamespace(runtime_status='context_overflow', runtime_source='provider_error',
            archive_tool_calls=list(rows), active_turn_user_inputs=[])

    context, conversation = harness(tmp_path, monkeypatch, run)
    with pytest.raises(ConversationCompactError) as caught:
        target._run_gateway_turn_with_conversation_compact(context, '继续', conversation)
    assert caught.value.code == 'COMPACT_RECOVERY_EXHAUSTED' and len(calls) == 8
    assert '连续8次' in str(caught.value) and 'provider_error' in str(caught.value)


def test_attempt_or_summary_alone_cannot_refresh_no_progress_counter(tmp_path, monkeypatch):
    calls = []

    def run(prompt, *, params):
        calls.append(params)
        bind(context, f'a-{len(calls)}')
        conversation.compact_generation += 1
        return SimpleNamespace(runtime_status='context_overflow', runtime_source='preflight', archive_tool_calls=[])

    context, conversation = harness(tmp_path, monkeypatch, run)
    with pytest.raises(ConversationCompactError, match='连续8次'):
        target._run_gateway_turn_with_conversation_compact(context, '继续', conversation)
    assert len(calls) == 8


def test_success_plus_unknown_cannot_extend_the_request(tmp_path, monkeypatch):
    rows, calls = [], []

    def run(prompt, *, params):
        calls.append(params)
        attempt = f'a-{len(calls)}'
        bind(context, attempt)
        rows.extend([record(attempt, 2 * len(calls)),
                     record(attempt, 2 * len(calls) + 1, effect_outcome='unknown')])
        return SimpleNamespace(runtime_status='context_overflow', runtime_source='preflight', archive_tool_calls=list(rows))

    context, conversation = harness(tmp_path, monkeypatch, run)
    with pytest.raises(ConversationCompactError, match='连续8次'):
        target._run_gateway_turn_with_conversation_compact(context, '继续', conversation)
    assert len(calls) == 8


def test_reordered_archive_does_not_count_old_rows_as_fresh():
    cursor = target._GatewayCompactProgressCursor()
    assert cursor.advance([record('a1', 1)], authority={'run_id': 'run-main', 'attempt_id': 'a1'}, request_id='req-main')
    assert not cursor.advance([record('a2', 2), record('a1', 1)],
        authority={'run_id': 'run-main', 'attempt_id': 'a2'}, request_id='req-main')


def test_cancellation_propagates_after_more_than_eight_healthy_slices(tmp_path, monkeypatch):
    rows, calls = [], []

    def run(prompt, *, params):
        calls.append(params)
        if len(calls) == 11:
            raise InterruptedError('cancelled')
        attempt = f'a-{len(calls)}'
        bind(context, attempt)
        rows.append(record(attempt, len(calls)))
        return SimpleNamespace(runtime_status='context_overflow', runtime_source='preflight', archive_tool_calls=list(rows))

    context, conversation = harness(tmp_path, monkeypatch, run)
    with pytest.raises(InterruptedError, match='cancelled'):
        target._run_gateway_turn_with_conversation_compact(context, '继续', conversation)
    assert len(calls) == 11


@pytest.mark.parametrize('overflow', [False, True])
def test_degraded_recovery_can_finish_but_cannot_repeat_capacity_recovery(tmp_path, monkeypatch, overflow):
    from agent_py_agent.agent.conversation.compact_guard import CompactCapacityFacts

    calls = []

    def run(prompt, *, params):
        calls.append(params)
        return SimpleNamespace(runtime_status='context_overflow' if overflow else 'ok',
                               runtime_source='provider_error', archive_tool_calls=[])

    context, conversation = harness(tmp_path, monkeypatch, run)
    capacity = CompactCapacityFacts(90000, 100000, 0, 40000, 1, 0, 0, 1)
    recovery = SimpleNamespace(committed=False, degraded=True, degraded_capacity=capacity,
                               degraded_recovery_facts=None)
    observer = SimpleNamespace(compact_recovery=recovery)
    if overflow:
        with pytest.raises(ConversationCompactError) as caught:
            target._run_gateway_turn_with_conversation_compact(context, '继续', conversation, observer=observer)
        assert caught.value.code == 'COMPACT_LOCAL_RECOVERY_EXHAUSTED'
        assert caught.value.capacity is capacity
    else:
        result, _ = target._run_gateway_turn_with_conversation_compact(context, '继续', conversation, observer=observer)
        assert result.runtime_status == 'ok'
    assert len(calls) == 1


def test_confirmed_progress_resets_only_the_consecutive_streak(tmp_path, monkeypatch):
    rows, calls = [], []

    def run(prompt, *, params):
        calls.append(params)
        attempt = f'a-{len(calls)}'
        bind(context, attempt)
        rows.append(record(attempt, len(calls), ok=len(calls) == 7))
        return SimpleNamespace(runtime_status='context_overflow', runtime_source='tool_context_overflow',
                               archive_tool_calls=list(rows))

    context, conversation = harness(tmp_path, monkeypatch, run)
    with pytest.raises(ConversationCompactError, match='连续8次'):
        target._run_gateway_turn_with_conversation_compact(context, '继续', conversation)
    assert len(calls) == 15, '第7片的确认进展清零，但随后的8片无进展必须停止'
