"""日志分页签名在多 owner 轮换后的真实 HTTP 回归。"""
from types import SimpleNamespace

from agent_py_agent.agent.conversation.display_archive import archive_display_rows
from agent_py_agent.agent.gateway_parts.business_api_v13_readers import _OwnerReaders
from agent_py_agent.tests import test_v13_w0_observation_readers as readers

background_http = readers.background_http
isolated_home = readers.isolated_home


def test_log_cursor_survives_namespace_churn(background_http, tmp_path):
    services, task, runtime, *_ = readers.seeded(background_http)
    reference = archive_display_rows(SimpleNamespace(conversation_store=services.host.conversation_store),
        thread_id=task.target_thread_id, rows=[{'kind': 'text', 'text': 'x' * 120}])
    readers.wire(services, runtime)
    params = {'source_ref': reference['archive_id'], 'max_bytes': 40}
    status, first = readers.background_call(background_http, 'logs.read', params)
    assert status == 200 and first['data']['next_cursor']
    for index in range(300):
        _OwnerReaders(tmp_path / str(index), f'local/owner-{index}', None, None)
    status, second = readers.background_call(background_http, 'logs.read',
        {**params, 'cursor': first['data']['next_cursor']})
    assert status == 200, second
    assert second['data']['chunks'][0]['text'] == 'x' * 40


def test_log_cursor_is_still_bound_to_namespace(tmp_path):
    original = _OwnerReaders(tmp_path / 'one', 'local/a', None, None)
    cursor = original.log_cursor('source', 2, 10)
    import pytest

    from agent_py_agent.agent.conversation.business_access import BusinessError

    for reader in (_OwnerReaders(tmp_path / 'one', 'local/b', None, None),
                   _OwnerReaders(tmp_path / 'two', 'local/a', None, None)):
        with pytest.raises(BusinessError, match='CURSOR_EXPIRED'):
            reader.log_position('source', cursor)
