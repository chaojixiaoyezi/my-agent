"""真实业务客户端往返验证会话空间选择与并发版本边界。"""
# ruff: noqa: F811
import http.client
import json

from agent_py_agent.cli.chat_client_context import (
    _gateway_request_identity,
    owner_identity_from_config,
)
from agent_py_agent.cli.space_commands import (
    BusinessClient,
    BusinessCommandSession,
    consume_business_command,
)
from agent_py_agent.tests.test_business_v12_background_scheduling import native  # noqa: F401


def client_for(native, conversation):
    server, agent = native
    identity = _gateway_request_identity(owner_identity_from_config(agent.config))

    def transport(path, body, timeout):
        conn = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=timeout)
        try:
            conn.request('POST', path, json.dumps(body), {'X-Gateway-Token': server._local_client_credential,
                'X-User-Id': identity['user_id'], 'X-Channel': identity['channel'],
                'X-Conversation-Id': conversation, 'Content-Type': 'application/json'})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    return BusinessClient(transport)


def test_session_space_wire_selection_cas_replay_and_isolation(native):
    a, b = client_for(native, 'selection-a'), client_for(native, 'selection-b')
    space = a.send(a.prepare('spaces.create', {'name': 'selected'}))['data']['space_id']
    assert a.call('sessions.space.get', {})['data'] == {'space_id': None, 'revision': 0, 'source': 'default'}
    request = a.prepare('sessions.space.set', {'space_id': space}, expected_revision=0)
    first = a.send(request)
    assert first['ok'], first
    assert first['data'] == {'space_id': space, 'revision': 1, 'source': 'thread'}
    assert a.send(request)['data'] == first['data']
    assert b.call('sessions.space.get', {})['data']['space_id'] is None
    conflict = a.send(a.prepare('sessions.space.set', {'space_id': space}, expected_revision=0))
    assert conflict['error']['code'] == 'REVISION_CONFLICT'
    assert a.call('sessions.space.get', {})['data'] == first['data']
    invalid = a.send(a.prepare('sessions.space.set', {'space_id': 'missing'}, expected_revision=1))
    assert not invalid['ok']
    assert a.call('sessions.space.get', {})['data'] == first['data']
    changed = consume_business_command('/space use ' + space, session=BusinessCommandSession(a))
    assert changed['ok'], changed
    assert changed['data']['revision'] == 2


def test_concurrent_session_selection_has_one_cas_winner(native):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    client = client_for(native, 'selection-race')
    space = client.send(client.prepare('spaces.create', {'name': 'race'}))['data']['space_id']
    assert consume_business_command('/space use ' + space, session=BusinessCommandSession(client))['ok']
    barrier = Barrier(2)

    def choose(_):
        worker = client_for(native, 'selection-race')
        request = worker.prepare('sessions.space.set', {'space_id': space}, expected_revision=1)
        barrier.wait(timeout=10)
        return worker.send(request)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(choose, range(2)))
    assert sorted(row['ok'] for row in results) == [False, True]
    assert [row['error']['code'] for row in results if not row['ok']] == ['REVISION_CONFLICT']
    assert client.call('sessions.space.get', {})['data']['revision'] == 2


def test_archived_selection_stays_visible_but_cannot_be_reselected(native, monkeypatch):
    monkeypatch.setattr(native[1], 'business_active_references', lambda *_: [], raising=False)
    client = client_for(native, 'selection-archive')
    created = client.send(client.prepare('spaces.create', {'name': 'archive'}))['data']
    space = created['space_id']
    assert consume_business_command('/space use ' + space, session=BusinessCommandSession(client))['ok']
    archive = client.send(client.prepare('spaces.archive', {'space_id': space}, expected_revision=0))
    assert archive['ok'], archive
    assert client.call('sessions.space.get', {})['data']['space_id'] == space
    attempt = client.send(client.prepare('sessions.space.set', {'space_id': space}, expected_revision=1))
    assert not attempt['ok']
