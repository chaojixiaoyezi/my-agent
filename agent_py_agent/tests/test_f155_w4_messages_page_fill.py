"""公开页只填充已读取工作片，隐藏原始行仍计入游标。"""
from agent_py_agent.tests.test_business_v12_f7_observation import fixture


def test_internal_rows_do_not_consume_public_limit(tmp_path, monkeypatch):
    case = fixture(tmp_path)
    public = case.store.messages.append({'thread_id': 'background', 'role': 'assistant', 'content': '公开', 'metadata': {'conversation_request_id': 'same'}})
    for _ in range(5):
        case.store.messages.append({'thread_id': 'background', 'role': 'system', 'content': '内部', 'metadata': {'conversation_request_id': 'same'}})
    original = case.store.messages.history_page_report
    calls = []

    def bounded(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(case.store.messages, 'history_page_report', bounded)
    result = case.service.query('messages.list', {
        'target': {'kind': 'background_main', 'id': 'background'}, 'limit': 1}, actor=case.actor)
    assert [row['message_id'] for row in result['items']] == [public.message_id]
    assert len(calls) == 1
    assert result['next_cursor'] is None


def test_all_internal_work_slice_remains_partial(tmp_path):
    case = fixture(tmp_path)
    case.store.messages.append({'thread_id': 'background', 'role': 'system', 'content': '内部'})
    result = case.service.query('messages.list', {
        'target': {'kind': 'background_main', 'id': 'background'}, 'limit': 1}, actor=case.actor)
    assert result['items'] == []
    assert result['coverage'] == 'partial'
    assert result['history_boundary']['reason_code'] == 'NO_PUBLIC_MESSAGES_IN_PAGE'
