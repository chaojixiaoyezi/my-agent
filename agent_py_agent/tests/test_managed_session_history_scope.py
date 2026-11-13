"""受管新订单按原身份隔离，普通会话保留原历史/摘要继承。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.background_compact_context import background_compact_scope
from agent_py_agent.agent.conversation.background_context import (
    BackgroundContextLoad,
    TaskScopeDecision,
    history_scope_selector_factory,
    task_scope_decision,
)
from agent_py_agent.agent.conversation.background_history_seed import history_scope_rows
from agent_py_agent.agent.conversation.compact_scope import (
    THREAD_COMPACT_SCOPE,
    CompactScope,
    scope_can_inherit,
)
from agent_py_agent.agent.conversation.models import MessageLogEntry


def row(identifier, task):
    return MessageLogEntry.from_dict({'message_id': identifier, 'thread_id': 'shared-controller',
        'role': 'assistant', 'content': '', 'created_at': 1.0,
        'metadata': {'task_id': task, 'canonical_native_messages': {'messages': [
            {'role': 'assistant', 'content': [{'type': 'text', 'text': 'original-' + task}]}]}}})


@pytest.mark.parametrize('managed', [True, False])
def test_new_order_excludes_other_native_history_same_order_resume_preserved(managed):
    rows = [row('old', 'order-old'), row('new', 'order-new'), row('resume', 'order-new')]
    decision = TaskScopeDecision('order-new', frozenset({'order-new'}),
        {'task_id': 'order-new', 'created_at': 5.0}, False, managed)
    expected = ['new', 'resume'] if managed else ['old', 'new', 'resume']
    assert [r.message_id for r in history_scope_rows(decision, rows)] == expected
    predicate = history_scope_selector_factory(decision)({r.message_id: i for i, r in enumerate(rows)})
    assert [r.message_id for r in rows if predicate(r)] == expected
    request = SimpleNamespace(reason='session_task', task_id='order-new')
    scope = background_compact_scope(request, decision)
    assert scope_can_inherit(scope, scope, 6.0)
    assert scope_can_inherit(scope, THREAD_COMPACT_SCOPE, 1.0) is (not managed)
    if managed:
        other = CompactScope(kind='session_task', task_id='order-old', task_ids=('order-old',), created_at=1.0)
        assert not scope_can_inherit(scope, other, 1.0)
        assert CompactScope.from_dict(scope.to_dict()) == scope
    else:
        assert scope is THREAD_COMPACT_SCOPE


def test_scope_uses_original_session_task_and_frozen_business_identity():
    from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext

    context = BusinessExecutionContext("local/main", "space-new", None, "order-new", 1, None, (), (), "business-execution-context.v1")
    thread = SimpleNamespace(thread_id='shared-controller', owner_id=context.owner_id)
    task = SimpleNamespace(task_id='order-new', target_thread_id=thread.thread_id,
        metadata={'business_context': context.to_dict()})
    store = SimpleNamespace(session_tasks=SimpleNamespace(load=lambda identifier: task))
    state = BackgroundContextLoad(SimpleNamespace(), store, thread, 'order-new', None, None, [])
    decision = task_scope_decision(state, {'tasks': [{'task_id': 'order-new', 'created_at': 5.0}]})
    assert decision.managed_session and not decision.detached
    task.target_thread_id = 'different-thread'
    # 同次裁决冻结；新一次对冲突的真实关系拒绝，不能退回全会话。
    assert task_scope_decision(state, {'tasks': []}).managed_session
    fresh = BackgroundContextLoad(SimpleNamespace(), store, thread, 'order-new', None, None, [])
    with pytest.raises(ValueError, match='SESSION_TASK_HISTORY_SCOPE_MISMATCH'):
        task_scope_decision(fresh, {'tasks': []})


def test_original_wake_identity_persisted_in_native_and_delivery_without_changing_request(tmp_path):
    from agent_py_agent.agent.conversation import ConversationStore
    from agent_py_agent.agent.conversation.background_context import background_task_scope_id
    from agent_py_agent.agent.conversation.background_delivery import (
        _background_owner_message_metadata,
    )
    from agent_py_agent.agent.conversation.background_execution import (
        _persist_background_native_turn,
    )

    store = ConversationStore(tmp_path / 'conversations')
    thread = store.threads.get_or_create({'canonical_user_id': 'owner'})
    request = SimpleNamespace(task_id='', thread_id=thread.thread_id, reason='session_task',
        route_channel='internal', conversation_turn_id='original-turn',
        wake_signal={'metadata': {'session_task_id': 'order-new'}})
    assert background_task_scope_id(request) == 'order-new'
    _persist_background_native_turn(store, request, SimpleNamespace(internal_response='original internal result'))
    rows, errors = store.messages.recent_report(thread.thread_id, limit=5)
    assert not errors and rows[0].metadata['task_id'] == 'order-new'
    metadata = _background_owner_message_metadata(request, delivery_reason='original', projection_status='plain_text',
        delivery_artifacts=(), evidence_refs=(), operation_verification=None, frozen_retry=None)
    assert metadata['task_id'] == 'order-new'
    assert request.task_id == ''  # 原请求与执行/路由权威不改写。
    ordinary = SimpleNamespace(task_id='', reason='chat', wake_signal=None)
    assert background_task_scope_id(ordinary) == ''


def test_old_same_order_rows_without_task_id_use_exact_original_turn_only():
    rows = [row('old', 'order-old'), row('legacy-own', ''), row('legacy-other', '')]
    rows[1].metadata['conversation_request_id'] = 'original-own-turn'
    rows[2].metadata['conversation_request_id'] = 'original-other-turn'
    decision = TaskScopeDecision('order-new', frozenset({'order-new'}),
        {'created_at': 5.0}, False, True, frozenset({'original-own-turn'}))
    assert [r.message_id for r in history_scope_rows(decision, rows)] == ['legacy-own']
    predicate = history_scope_selector_factory(decision)({r.message_id: i for i, r in enumerate(rows)})
    assert [r.message_id for r in rows if predicate(r)] == ['legacy-own']
