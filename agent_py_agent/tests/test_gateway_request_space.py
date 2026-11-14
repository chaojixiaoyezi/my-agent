"""空间只在新输入受理时冻结，重放和执行不重读当前选择。"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.route_mode import _setting_key
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts.request_space import (
    REQUEST_SPACE_KEY,
    capture_request_space,
    request_space_for_execution,
)


def selected_thread(store, owner, actor, space, revision=1):
    thread = store.threads.create(owner_id=owner)
    home = SimpleNamespace(owner_id=owner)
    key = _setting_key(home, thread, actor)
    thread = replace(thread, metadata={"session_space.v1": {"owner_id": owner, "actors": {
        key: {"space_id": space, "revision": revision}}}})
    return home, thread


def test_selection_freezes_and_replay_does_not_follow_switch(tmp_path):
    home, thread = selected_thread(ConversationStore(tmp_path), "owner", "actor", "space-one")
    frozen = capture_request_space(home, thread, actor_id="actor")
    key = _setting_key(home, thread, "actor")
    switched = replace(thread, metadata={"session_space.v1": {"owner_id": "owner", "actors": {
        key: {"space_id": "space-two", "revision": 2}}}})
    assert capture_request_space(home, switched, actor_id="actor")["space_id"] == "space-two"
    request = {REQUEST_SPACE_KEY: frozen}
    assert request_space_for_execution(request, owner_id="owner", thread_id=thread.thread_id,
        actor_id="actor") == frozen
    assert frozen["space_id"] == "space-one"


@pytest.mark.parametrize("field,value", [("owner_id", "other-owner"), ("actor_id", "other-actor"), ("thread_id", "other-thread")])
def test_frozen_selection_rejects_cross_identity(tmp_path, field, value):
    home, thread = selected_thread(ConversationStore(tmp_path), "owner", "actor", "space")
    request = {REQUEST_SPACE_KEY: capture_request_space(home, thread, actor_id="actor")}
    expected = {"owner_id": "owner", "actor_id": "actor", "thread_id": thread.thread_id, field: value}
    with pytest.raises(ValueError, match="快照"):
        request_space_for_execution(request, **expected)


def test_no_selection_remains_absent_and_other_actor_does_not_inherit(tmp_path):
    home, thread = selected_thread(ConversationStore(tmp_path), "owner", "actor", "space")
    assert capture_request_space(home, thread, actor_id="other") is None
    assert request_space_for_execution({}, owner_id="owner", thread_id=thread.thread_id, actor_id="actor") is None


def test_http_input_cannot_supply_host_snapshot():
    from agent_py_agent.agent.gateway_parts.http_handlers import _prepare_http_gateway_input
    with pytest.raises(ValueError, match="客户端"):
        _prepare_http_gateway_input(None, None, {REQUEST_SPACE_KEY: {"space_id": "forged"}})


def test_same_actor_in_two_owners_has_independent_selection(tmp_path):
    snapshots = []
    for owner in ("owner-a", "owner-b"):
        home, thread = selected_thread(ConversationStore(tmp_path / owner), owner, "actor", owner + "-space")
        snapshots.append(capture_request_space(home, thread, actor_id="actor"))
    assert [row["owner_id"] for row in snapshots] == ["owner-a", "owner-b"]
    assert [row["space_id"] for row in snapshots] == ["owner-a-space", "owner-b-space"]


@pytest.fixture
def space_http(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_business_v12_background_scheduling import native
    yield from native.__wrapped__(tmp_path, monkeypatch)


def test_real_chat_space_switch_freezes_accepted_request(space_http, monkeypatch):
    import json
    import urllib.request
    from dataclasses import replace

    from agent_py_agent.agent.gateway_parts.input_delivery_service import read_gateway_input_receipt
    from agent_py_agent.agent.session import SessionManager
    from agent_py_agent.cli.chat_parts.gateway_client import ChatRequestContent, submit_chat_request
    from agent_py_agent.cli.chat_parts.session_state import create_or_resume_session
    from agent_py_agent.cli.space_commands import (
        BusinessCommandSession,
        business_client_for_agent,
        consume_business_command,
    )
    from agent_py_agent.tests.test_local_ask_owner_transport import _client

    server, agent = space_http
    client = _client(agent, server)
    _, session_id = create_or_resume_session(SessionManager(agent.config), None)
    business = business_client_for_agent(client, conversation_id=session_id)
    commands = BusinessCommandSession(business)
    spaces = [business.send(business.prepare('spaces.create', {'name': name}))['data']['space_id']
              for name in ('空间A', '空间B')]
    captured = []
    original_open = urllib.request.urlopen

    def observe(request, *args, **kwargs):
        captured.append((request.full_url.rsplit('/', 1)[-1], dict(request.header_items()),
                         json.loads(request.data) if request.data else {}))
        return original_open(request, *args, **kwargs)

    monkeypatch.setattr(urllib.request, 'urlopen', observe)
    assert consume_business_command('/space use ' + spaces[0], session=commands)['ok']
    first = ChatRequestContent(prompt='检查当前空间', inject=[], prompt_files=[], save=True,
        show_prompt=False, resume_context=None, chat_session_id=session_id, client_message_id='first-input')
    first_id = submit_chat_request(server.paths, first, client)[0]
    accepted = read_gateway_input_receipt(server.paths, first_id).prepared_request
    assert accepted[REQUEST_SPACE_KEY]['space_id'] == spaces[0]
    assert consume_business_command('/space use ' + spaces[1], session=commands)['ok']
    # 同一客户端消息重放必须复用原回执，不能在换空间后改写已受理的空间。
    assert submit_chat_request(server.paths, first, client)[0] == first_id
    assert read_gateway_input_receipt(server.paths, first_id).prepared_request == accepted
    second_id = submit_chat_request(server.paths, replace(first, client_message_id='second-input'), client)[0]
    second = read_gateway_input_receipt(server.paths, second_id).prepared_request
    assert second[REQUEST_SPACE_KEY]['space_id'] == spaces[1]
    assert accepted[REQUEST_SPACE_KEY]['thread_id'] == second[REQUEST_SPACE_KEY]['thread_id']
    for path, headers, body in captured:
        if path == 'business':
            assert headers['X-conversation-id'] == session_id
        elif path == 'ask':
            assert body['conversation_id'] == session_id


def test_cleared_current_selection_does_not_erase_accepted_snapshot(tmp_path):
    home, thread = selected_thread(ConversationStore(tmp_path), 'owner', 'actor', 'original-space')
    frozen = capture_request_space(home, thread, actor_id='actor')
    key = _setting_key(home, thread, 'actor')
    cleared = replace(thread, metadata={'session_space.v1': {'owner_id': 'owner', 'actors': {
        key: {'space_id': None, 'revision': 2, 'operation_key': 'clear'}}}})
    assert capture_request_space(home, cleared, actor_id='actor') is None
    assert request_space_for_execution({REQUEST_SPACE_KEY: frozen}, owner_id='owner',
        thread_id=thread.thread_id, actor_id='actor') == frozen
