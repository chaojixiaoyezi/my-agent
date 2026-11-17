"""A damaged gateway history projection must not break the closeout of unrelated requests.

W3 F17-class check (2026-11-17): with the history_index.sqlite3 header overwritten, every request that reached
its canonical terminal still raised sqlite3.DatabaseError from the history append at the end of closeout.  That
aborted the queue scan after each request, so later pending requests waited for the next loop.  The history index
is a rebuildable projection, not the completion record.
"""
from __future__ import annotations

import json
import urllib.request

import pytest

from agent_py_agent.agent.backends import get_backend
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import request_worker
from agent_py_agent.agent.gateway_parts.client_credentials import GatewayClientCredentials
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.local_client_token import ensure_local_client_credential
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.gateway_parts.queue_service import ensure_gateway_folders
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from agent_py_agent.tests.test_gateway_conversation_control import _free_port


@pytest.fixture
def gateway(tmp_path):
    config = AgentConfig(model_backend='echo', my_agent_home=str(tmp_path / 'home'), enable_plugins=False)
    agent = SimpleAgent(config, resolve_owner_home(tmp_path / 'home', OwnerIdentity.local_main()).home_dir)
    agent.backend = get_backend('echo', agent.config)
    ensure_local_client_credential(agent.home_paths.root)
    paths = gateway_paths(agent)
    ensure_gateway_folders(paths)
    server = GatewayHTTPServer(_free_port(), paths, params=GatewayHTTPServerParams(agent=agent))
    server.start()

    def ask(goal, message_id):
        data = json.dumps({'goal': goal, 'conversation_id': 'w3-history',
                           'metadata': {'message_id': message_id}}).encode()
        request = urllib.request.Request(f'http://127.0.0.1:{server.port}/ask', data=data, headers={
            'Content-Type': 'application/json', **GatewayClientCredentials(agent.home_paths.root).headers()})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())['request_id']

    try:
        yield agent, paths, ask
    finally:
        server.stop()


def test_closeout_completes_and_leaves_the_projection_marker_for_the_projector(gateway, monkeypatch):
    agent, paths, ask = gateway
    first = ask('第一条合成请求', 'w3-history-1')
    assert request_worker._process_gateway_request_path(agent, paths, paths.inbox / (first + '.json'), 'w')
    first_marker = paths.root / 'projection_state' / 'terminal_complete' / (first + '.json')
    assert 'canonical_stamp' in json.loads(first_marker.read_text())
    index = paths.root / 'history_index.sqlite3'
    damaged = b'NOT-A-SQLITE-DB!' + index.read_bytes()[16:]
    index.write_bytes(damaged)
    reported = []
    monkeypatch.setattr('agent_py_agent.agent.gateway_parts.logging._report_gateway_side_effect_error',
                        lambda operation, request_id, exc: reported.append((operation, request_id, type(exc).__name__)))

    second = ask('第二条合成请求', 'w3-history-2')
    third = ask('第三条合成请求', 'w3-history-3')
    assert request_worker._process_gateway_requests(agent, paths) == 2

    for request_id in (second, third):
        assert (paths.done / (request_id + '.json')).exists()
        assert json.loads((paths.root / 'responses' / (request_id + '.json')).read_text())['status'] == 'done'
        # Archive writes only the settlement note; the completion stamp waits until the history row is projected.
        marker = paths.root / 'projection_state' / 'terminal_complete' / (request_id + '.json')
        assert 'canonical_stamp' not in (json.loads(marker.read_text()) if marker.exists() else {})
    assert reported == [('archive_gateway_history', second, 'DatabaseError'),
                        ('archive_gateway_history', third, 'DatabaseError')]
    assert index.read_bytes()[:16] == damaged[:16]
    assert not list(paths.processing.glob('*.json'))
