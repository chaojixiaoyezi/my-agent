from dataclasses import replace

from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts.control_service import (
    GatewayControlScope,
    execute_gateway_conversation_control,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.session_control import _selected_scope
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_schedule_control import _agent


def _source(agent):
    return agent.conversation_store.threads.get_or_create({'channel':'local','channel_user_id':'local-agent',
        'channel_conversation_id':'synthetic-tui','canonical_user_id':'local-agent','owner_id':agent.home_paths.owner_id,
        'owner_home':str(agent.home_paths.owner_home_dir)})


def test_admin_im_queries_original_tui_without_creating_thread_or_task(tmp_path):
    agent=_agent(tmp_path)
    thread=_source(agent)
    scope=GatewayControlScope('ou_admin','feishu','oc_private',metadata={'channel_chat_type':'p2p'},resolved_owner=OwnerIdentity.local_main())
    paths=gateway_paths_from_root(tmp_path/'gateway')
    before=agent.conversation_store.threads.list()
    result=execute_gateway_conversation_control(agent,paths,parse_conversation_control('/sessions control '+thread.thread_id+' /status'),scope)
    assert result.ok and result.details['target_thread_id']==thread.thread_id
    assert agent.conversation_store.threads.list()==before
    assert _selected_scope(agent,thread,scope).conversation_id=='synthetic-tui'
    listing=execute_gateway_conversation_control(agent,paths,parse_conversation_control('/sessions threads'),scope)
    assert listing.ok and thread.thread_id in listing.message


def test_control_rejects_other_owner_and_nested_control(tmp_path):
    agent=_agent(tmp_path)
    thread=_source(agent)
    scope=GatewayControlScope('ou_admin','feishu','oc_private',metadata={'channel_chat_type':'p2p'},resolved_owner=OwnerIdentity.local_main())
    paths=gateway_paths_from_root(tmp_path/'gateway')
    bad=execute_gateway_conversation_control(agent,paths,parse_conversation_control('/sessions control '+thread.thread_id+' /sessions'),scope)
    assert not bad.ok and bad.error_code=='CONTROL_ARGUMENT_INVALID'
    agent.conversation_store.threads.update_atomic(thread.thread_id,lambda old:replace(old,owner_id='foreign'))
    result=execute_gateway_conversation_control(agent,paths,parse_conversation_control('/sessions control '+thread.thread_id+' /stop'),scope)
    assert not result.ok and result.error_code=='CONTROL_TARGET_UNAVAILABLE'


def test_group_or_ordinary_user_cannot_take_admin_tui(tmp_path):
    agent=_agent(tmp_path,group='oc_team')
    scope=GatewayControlScope('ou_one','feishu','oc_team',metadata={'channel_chat_type':'group'},resolved_owner=OwnerIdentity.provider_group('feishu','oc_team'))
    result=execute_gateway_conversation_control(agent,gateway_paths_from_root(tmp_path/'gateway'),
        parse_conversation_control('/sessions control thread-any /stop'),scope)
    assert not result.ok and result.error_code=='COMMAND_ADMIN_REQUIRED'


def test_bound_admin_im_approves_original_tui_wait_and_nested_secret_is_redacted(tmp_path):
    import json

    from agent_py_agent.agent.command_catalog import sensitive_command_name
    from agent_py_agent.agent.conversation.control_commands import persisted_control_command_text
    from agent_py_agent.agent.gateway_parts.control_operation_service import (
        execute_gateway_control_operation,
    )
    from agent_py_agent.agent.user_space.admin_channel_identity import bind_admin_channel_identity
    from agent_py_agent.agent.user_space.admin_password import set_admin_password
    from agent_py_agent.tests.test_admin_identity_gateway import (
        _permission,
        _processing_request,
        _start_waiting,
    )
    agent = _agent(tmp_path)
    thread = _source(agent)
    secret = 'Synthetic-Cross-Entry-Password-123'
    set_admin_password(agent.home_paths.root, secret)
    bind_admin_channel_identity(agent.home_paths.root, 'feishu', 'ou_admin')
    paths = gateway_paths_from_root(tmp_path/'gateway')
    record = _processing_request(paths, 'req-cross', user='local-agent', conversation='synthetic-tui')
    data = json.loads(record.read_text())
    data['metadata'].update(channel='local', channel_chat_type='')
    data['conversation']['channel'] = 'local'
    record.write_text(json.dumps(data))
    waiter, decision = _start_waiting(record, 'req-cross', _permission('req-cross'))
    scope = GatewayControlScope('ou_admin','feishu','oc_private',metadata={'channel_chat_type':'p2p','message_id':'om-cross-test'},resolved_owner=OwnerIdentity.local_main())
    text = f'/sessions control {thread.thread_id} /approve {secret}'
    command = parse_conversation_control(text)
    assert sensitive_command_name(text) == 'sessions'
    assert sensitive_command_name(text.replace(' control ', ' CONTROL ')) == 'sessions'
    saved = persisted_control_command_text(command, text)
    assert secret not in saved and thread.thread_id in saved and '/approve ******' in saved
    receipt = execute_gateway_control_operation(agent, paths, command, scope, command_text=text)
    assert receipt.result['ok'], receipt.result
    assert secret not in receipt.command_text and thread.thread_id in receipt.command_text
    waiter.join(timeout=3)
    assert not waiter.is_alive() and decision['decision']=='approved'
    assert all(secret.encode() not in path.read_bytes() for path in paths.root.rglob('*') if path.is_file())
