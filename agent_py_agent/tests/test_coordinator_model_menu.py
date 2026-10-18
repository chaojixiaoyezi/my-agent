"""首次模型菜单复用实际HTTP/G1/C来源与原领域账；不构造冷Agent或调用真实供应商。"""
import http.client
import json

import pytest

from agent_py_agent.tests.test_coordinator_model_card_delivery import delivery_case


# LLM: 只走当前真实临时HTTP监听器及G1头，合成目录字段不写测试命令/日志，不替换权限门。
# 函数用途: 发送本片原模型请求并读真实响应。
def post_model(server, body, *, route='/client/models', actor='ou_fresh_model', credential=True):
    connection = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=3)
    headers = {'Content-Type': 'application/json', 'X-Channel': 'feishu', 'X-User-Id': actor}
    if credential:
        headers['X-Gateway-Token'] = server._local_client_credential
    try:
        connection.request('POST', route, json.dumps(body), headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.mark.parametrize('route', ['/client/models', '/control'])
def test_cold_first_model_menu_has_real_metadata_source(tmp_path, monkeypatch, route):
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        _agent, _host, _path, _row, _body, server, _runtime, _sent = case
        body = {'conversation_id': 'fresh-model-session', 'metadata': {'message_id': 'first-model-menu'}}
        body.update({'operation': 'list'} if route == '/client/models' else {'command': '/model'})
        status, result = post_model(server, body, route=route)
        assert status == 200 and result.get('ok') is True, (status, result)
        assert server.agent is None
        if route == '/client/models':
            assert result['thread_id'].startswith('thread-') and result['model_execution']['bound'] is None
            assert not delivered
        else:
            assert result['control_state'] == 'completed' and len(delivered) == 1
            assert post_model(server, body, route=route) == (status, result)
            assert len(delivered) == 1


# LLM: 仅合成原client/control输入，owner始终由实际header/config裁决；不构造领域source或current execution。
# 函数用途: 生成合法原模型菜单请求。
def model_body(*, operation='list', command=None, conversation='model-settings', message='model-message', group=''):
    metadata = {'message_id': message, 'channel_chat_type': 'group' if group else 'p2p'}
    if group:
        metadata['channel_chat_id'] = group
    result = {'conversation_id': group or conversation, 'metadata': metadata}
    result.update({'operation': operation} if command is None else {'command': command})
    return result


@pytest.mark.parametrize('kind', ['user', 'group'])
def test_cold_client_models_uses_original_catalog_selection_default_and_owner(tmp_path, monkeypatch, kind):
    from agent_py_agent.agent.settings.model_profiles import (
        model_profiles_path,
        read_model_profiles,
    )
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import (
        OwnerIdentity,
        home_paths_with_owner,
        resolve_owner_home,
    )
    with delivery_case(tmp_path, monkeypatch, kind=kind) as (case, delivered):
        _agent, host, _path, row, _body, server, _runtime, _sent = case
        actor = row['actor_id'] if kind == 'group' else 'ou_fresh_model'
        group = row['owner']['owner_id'] if kind == 'group' else ''
        request = model_body(group=group)
        status, listing = post_model(server, request, actor=actor)
        assert status == 200 and listing['ok'], listing
        thread_id = listing['thread_id']
        profile_id = 'd301ae84-2604-4d9e-9782-cd4560b54f03'
        secret = 'SYNTHETIC_MENU_SECRET'
        profile = {'model_backend': 'openai_compatible', 'api_base': 'https://menu.example.invalid/v1',
                   'model_name': 'from-first-menu', 'api_key': secret, 'model_context_window_tokens': 8192}
        add = {**request, 'operation': 'add', 'profile_id': profile_id, 'profile': profile,
               'user_id': 'ou_foreign', 'host_request_owner.v1': {'provider': 'local', 'owner_kind': 'main', 'owner_id': 'main'}}
        for payload in (add, add, {**request, 'operation': 'select', 'profile_id': profile_id},
                        {**request, 'operation': 'set_default', 'profile_id': profile_id}):
            status, result = post_model(server, payload, actor=actor)
            assert status == 200 and result['ok'] and result['thread_id'] == thread_id, result
            assert secret not in json.dumps(result)
        owner = OwnerIdentity.provider_group('feishu', group) if group else OwnerIdentity.provider_user('feishu', actor)
        directory = resolve_owner_home(server.control_plane.home_root, owner).home_dir
        canonical_home = home_paths_with_owner(home_paths(server.control_plane.home_root), resolve_owner_home(server.control_plane.home_root, owner))
        catalog = read_model_profiles(model_profiles_path(canonical_home))
        assert catalog['selected'] == profile_id and profile_id in catalog['profiles']
        assert len(catalog['profiles']) == (2 if group else 1)
        assert not delivered
        assert server.agent is None
        if kind == 'user':
            assert not resolve_owner_home(server.control_plane.home_root, OwnerIdentity.provider_user('feishu', 'ou_foreign')).home_dir.exists()
            assert profile_id not in read_model_profiles(model_profiles_path(host.home_paths))['profiles']


@pytest.mark.parametrize('route,operation', [('/client/models', 'probe'), ('/client/models', 'auth_start'),
    ('/client/models', 'embedding_list'), ('/client/models', 'set_shared'), ('/control', '/model vector'), ('/control', '/status')])
def test_cold_model_does_not_open_other_domains(tmp_path, monkeypatch, route, operation):
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        _agent, _host, _path, _row, _body, server, _runtime, _sent = case
        body = model_body(operation=operation) if route == '/client/models' else model_body(command=operation)
        status, result = post_model(server, body, route=route)
        if route == '/control' and operation == '/status':
            assert status == 202 and result['control_state'] == 'prepared'
            receipt = json.loads(next((server.paths.root/'control_operations').glob('*.json')).read_text())
            assert receipt['command_kind'] == 'status' and receipt['dispatch'] and 'result' not in receipt
            assert post_model(server, body, route=route) == (status, result)
        else:
            assert status == 503 and result['error_code'] == 'COORDINATOR_BUSINESS_NOT_READY'
        assert not delivered


@pytest.mark.parametrize('route', ['/client/models', '/control'])
def test_cold_first_model_requires_actual_credential_before_body(tmp_path, monkeypatch, route):
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        _agent, _host, _path, _row, _body, server, _runtime, _sent = case
        assert post_model(server, None, route=route, credential=False)[0] == 403
        assert not delivered


@pytest.mark.parametrize('route', ['/client/models', '/control'])
@pytest.mark.parametrize('kind', ['context', 'outcome', 'bounded', 'tool'])
def test_model_actual_typed_error_reaches_original_handler_same_object(tmp_path, monkeypatch, route, kind):
    from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError
    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionContextUnavailable,
        RequestExecutionOutcomeUncertainError,
    )
    from agent_py_agent.agent.common.tool_write_context import ToolWriteContextUnavailable
    from agent_py_agent.agent.gateway_parts import model_metadata_transaction as domain
    errors = {'context': RequestExecutionContextUnavailable('synthetic'), 'tool': ToolWriteContextUnavailable('synthetic'),
        'bounded': BoundedCallExitUnconfirmedError('synthetic-call', 'worker_exit_unconfirmed'),
        'outcome': RequestExecutionOutcomeUncertainError({'id': 'request', 'execution_attempt_id': 'attempt',
            'lease_epoch': 1, 'lease_owner': 'worker'}, 'synthetic')}
    error = errors[kind]
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        _agent, _host, _path, _row, _body, server, _runtime, _sent = case
        original, observed = domain.execute_coordinator_model_metadata, []
        def observe(*args, **kwargs):
            try:
                return original(*args, **kwargs)
            except BaseException as actual:
                observed.append(actual)
                raise
        def fail(*args, **kwargs):
            raise error
        monkeypatch.setattr(domain, 'execute_coordinator_model_metadata', observe)
        monkeypatch.setattr(domain, 'execute_model_profile_operation', fail)
        body = model_body() if route == '/client/models' else model_body(command='/model')
        status, result = post_model(server, body, route=route)
        assert status == 503 and result['error_code'] == error.error_code
        assert observed == [error] and observed[0] is error
        assert not delivered
        if route == '/control':
            receipts = list((server.paths.root / 'control_operations').glob('*.json'))
            receipt = json.loads(receipts[0].read_text())
            assert receipt['state'] == 'executing' and 'result' not in receipt and 'error' not in receipt
            # 不发第二次请求：这里证明同对象及原未结，不用新source替旧异常执行或补账。


def test_first_model_delivery_is_outside_writer_and_has_original_signed_row(tmp_path, monkeypatch):
    import re
    import stat

    from agent_py_agent.agent.adapter.feishu import FeishuAdapter
    from agent_py_agent.agent.gateway_parts.private_interaction_transaction import (
        _PrivateInteractionTransaction,
    )
    from agent_py_agent.agent.user_space.interaction_refs import verify_interaction_reference
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
    active = []
    original = _PrivateInteractionTransaction.perform
    def perform(source, operation):
        def inside():
            active.append(source)
            try:
                return operation()
            finally:
                active.pop()
        return original(source, inside)
    monkeypatch.setattr(_PrivateInteractionTransaction, 'perform', perform)
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        _agent, _host, _path, _row, _body, server, _runtime, _sent = case
        send = FeishuAdapter.send_control_card
        def outside(*args, **kwargs):
            assert not active
            return send(*args, **kwargs)
        monkeypatch.setattr(FeishuAdapter, 'send_control_card', outside)
        status, result = post_model(server, model_body(command='/model'), route='/control')
        assert status == 200 and result['ok']
        reference = re.search(r'"reference": "([^"]+)"', json.dumps(delivered[0][1])).group(1)
        payload = verify_interaction_reference(server.control_plane.home_root, reference, channel='feishu', actor_id='ou_fresh_model')
        owner = OwnerIdentity.provider_user('feishu', 'ou_fresh_model')
        home = resolve_owner_home(server.control_plane.home_root, owner).home_dir
        from agent_py_agent.agent.gateway_parts.model_control_cards import _record_path
        row = json.loads(_record_path(home, payload['control_id']).read_text())
        assert row['status'] == 'active' and row['thread_id'] == payload['thread_id']
        assert row['owner'] == {'provider': 'feishu', 'owner_kind': 'user', 'owner_id': 'ou_fresh_model'}
        key = next(home.rglob('key.json'))
        assert stat.S_IMODE(key.stat().st_mode) == 0o600
        receipt = json.loads(next((server.paths.root / 'control_operations').glob('*.json')).read_text())
        assert receipt['conversation_thread_id'] == row['thread_id']


@pytest.mark.parametrize('known', [False, True])
def test_first_model_receipt_preserves_real_effect_and_does_not_resend(tmp_path, monkeypatch, known):
    from agent_py_agent.agent.adapter.feishu import FeishuAdapter
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        _agent, _host, _path, _row, _body, server, _runtime, _sent = case
        called = []
        def fail(*args, **kwargs):
            called.append(True)
            if known:
                return {'error_code': 'CHANNEL_CONTROL_PROVIDER_REJECTED', 'provider_code': 99991663}
            raise OSError('synthetic transport failure')
        monkeypatch.setattr(FeishuAdapter, 'send_control_card', fail)
        body = model_body(command='/model')
        status, result = post_model(server, body, route='/control')
        assert result['ok'] is False and result['control_state'] == 'completed'
        assert status == (200 if known else 202)
        assert result['details']['effect_outcome'] == ('not_started' if known else 'unknown')
        assert result['error_code'] == ('CHANNEL_CONTROL_PROVIDER_REJECTED' if known else 'CHANNEL_CONTROL_IO_FAILED')
        if known:
            assert result['details']['provider_code'] == 99991663
        saved = {p: p.read_bytes() for p in server.control_plane.home_root.rglob('*.json')}
        assert post_model(server, body, route='/control') == (status, result)
        assert {p: p.read_bytes() for p in saved} == saved
        assert len(called) == 1 and not delivered


def test_first_model_stop_after_delivery_keeps_executing_without_late_row(tmp_path, monkeypatch):
    from agent_py_agent.agent.adapter.feishu import FeishuAdapter
    from agent_py_agent.agent.gateway_parts import model_metadata_transaction as domain
    from agent_py_agent.agent.gateway_parts.multiprocess_global_write import GlobalWriteError
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        _agent, _host, _path, _row, _body, server, runtime, _sent = case
        send, execute, observed = FeishuAdapter.send_control_card, domain.execute_coordinator_model_metadata, []
        def stop(*args, **kwargs):
            result = send(*args, **kwargs)
            runtime.stop_producers()
            return result
        def observe(*args, **kwargs):
            try:
                return execute(*args, **kwargs)
            except BaseException as error:
                observed.append(error)
                raise
        monkeypatch.setattr(FeishuAdapter, 'send_control_card', stop)
        monkeypatch.setattr(domain, 'execute_coordinator_model_metadata', observe)
        status, result = post_model(server, model_body(command='/model'), route='/control')
        assert status == 503 and result['error_code'] == 'GLOBAL_WRITE_REJECTED'
        assert len(observed) == 1 and type(observed[0]) is GlobalWriteError
        assert len(delivered) == 1
        receipt = json.loads(next((server.paths.root / 'control_operations').glob('*.json')).read_text())
        assert receipt['state'] == 'executing' and 'result' not in receipt and 'error' not in receipt
        home = resolve_owner_home(server.control_plane.home_root, OwnerIdentity.provider_user('feishu', 'ou_fresh_model')).home_dir
        assert not list(home.rglob('model-*.json'))


def test_model_source_copy_and_finished_work_cannot_commit(tmp_path, monkeypatch):
    import contextvars
    import copy

    from agent_py_agent.agent.gateway_parts.multiprocess_global_write import GlobalWriteError
    from agent_py_agent.agent.gateway_parts.private_interaction_transaction import (
        _PrivateInteractionTransaction,
    )
    captured = []
    original = _PrivateInteractionTransaction.perform
    def observe(source, operation):
        if source._model_scope is not None and not captured:
            captured.append((source, contextvars.copy_context()))
            with pytest.raises(GlobalWriteError):
                copy.copy(source).perform(lambda: pytest.fail('copy不能进入writer'))
        return original(source, operation)
    monkeypatch.setattr(_PrivateInteractionTransaction, 'perform', observe)
    with delivery_case(tmp_path, monkeypatch) as (case, _delivered):
        _agent, _host, _path, _row, _body, server, _runtime, _sent = case
        assert post_model(server, model_body())[0] == 200
        source, context = captured[0]
        saved = {p: p.read_bytes() for p in server.control_plane.home_root.rglob('*.json')}
        with pytest.raises(GlobalWriteError):
            context.run(source.perform, lambda: pytest.fail('已结束来源不能进入writer'))
        assert {p: p.read_bytes() for p in saved} == saved
        assert source.pin not in source.lease._commit_pins


def test_model_first_thread_lock_is_nonblocking_and_original_retry_works(tmp_path, monkeypatch):
    import hashlib
    import time

    from agent_py_agent.agent.gateway_parts.io import locked_file_transition
    from agent_py_agent.agent.gateway_parts.owner_conversation_store import (
        owner_conversation_store_from_config,
    )
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import (
        OwnerIdentity,
        home_paths_with_owner,
        resolve_owner_home,
    )
    with delivery_case(tmp_path, monkeypatch) as (case, _delivered):
        _agent, _host, _path, _row, _body, server, _runtime, _sent = case
        owner = OwnerIdentity.provider_user('feishu', 'ou_fresh_model')
        home = home_paths_with_owner(home_paths(server.control_plane.home_root), resolve_owner_home(server.control_plane.home_root, owner))
        from agent_py_agent.agent.owner_scoped_pool import _config_with_owner
        store = owner_conversation_store_from_config(_config_with_owner(server.control_plane.config, owner), home)
        key = '\x1f'.join(('feishu', 'model-settings', 'ou_fresh_model'))
        lock = store.storage.threads_dir / ('.channel-' + hashlib.sha256(key.encode()).hexdigest())
        with locked_file_transition(lock):
            started = time.monotonic()
            status, result = post_model(server, model_body())
            assert status == 500 and result['ok'] is False
            assert time.monotonic() - started < 1.5
            assert not list(store.storage.threads_dir.glob('*.json'))
        assert post_model(server, model_body())[0] == 200


def test_model_first_key_lock_is_nonblocking_and_does_not_create_while_busy(tmp_path, monkeypatch):
    import time

    from agent_py_agent.agent.common.json_io import locked_json_path
    from agent_py_agent.agent.path_access_policy import PRIVATE_INTERACTIONS_OWNER_PARTS
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        _agent, _host, _path, _row, _body, server, _runtime, _sent = case
        home = resolve_owner_home(server.control_plane.home_root, OwnerIdentity.provider_user('feishu', 'ou_fresh_model')).home_dir
        key = home.joinpath(*PRIVATE_INTERACTIONS_OWNER_PARTS, 'key.json')
        with locked_json_path(key):
            started = time.monotonic()
            status, result = post_model(server, model_body(command='/model'), route='/control')
            assert status == 200 and result['ok'] is False
            assert time.monotonic() - started < 1.5 and not key.exists() and not delivered
        assert post_model(server, model_body(command='/model', message='new-model-message'), route='/control')[1]['ok'] is True
        assert len(delivered) == 1


@pytest.mark.parametrize('command', ['/model {id}', '/model default {id}'])
def test_cold_text_selection_and_default_use_original_profile_and_receipt(tmp_path, monkeypatch, command):
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        _agent, host, _path, row, _body, server, _runtime, _sent = case
        request = model_body(command=command.format(id='b00f0289-a209-40fe-a3b7-a01cdf972abb'))
        status, result = post_model(server, request, route='/control', actor=row['actor_id'])
        assert status == 200 and result['ok'] and result['control_state'] == 'completed'
        assert post_model(server, request, route='/control', actor=row['actor_id']) == (status, result)
        thread = host.conversation_store.threads.resolve(channel='feishu', channel_conversation_id='model-settings', channel_user_id=row['actor_id'])
        from agent_py_agent.agent.settings.model_profiles import (
            model_profiles_path,
            read_model_profiles,
        )
        if 'default' in command:
            assert read_model_profiles(model_profiles_path(host.home_paths))['selected'] == 'b00f0289-a209-40fe-a3b7-a01cdf972abb'
        else:
            assert thread.model_profile_id == 'b00f0289-a209-40fe-a3b7-a01cdf972abb'
            assert thread.model_selection_revision == 2 and thread.model_selection_source == 'explicit'
        assert not delivered


def test_first_menu_then_original_add_and_save_callbacks_share_canonical_thread(tmp_path, monkeypatch):
    import re

    from agent_py_agent.agent.gateway_parts.model_control_cards import _record_path
    from agent_py_agent.agent.user_space.interaction_refs import verify_interaction_reference
    from agent_py_agent.tests.test_coordinator_model_card_delivery import _child, seed
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        _agent, host, _path, row, _body, server, _runtime, _sent = case
        status, result = post_model(server, model_body(command='/model'), route='/control', actor=row['actor_id'])
        assert status == 200 and result['ok'] and len(delivered) == 1
        reference = re.search(r'"reference": "([^"]+)"', json.dumps(delivered[0][1])).group(1)
        payload = verify_interaction_reference(server.control_plane.home_root, reference, channel='feishu', actor_id=row['actor_id'])
        first = json.loads(_record_path(host.home_paths.owner_home_dir, payload['control_id']).read_text())
        body = {'reference': reference, 'decision': 'add', 'message_id': first['message_id'], 'chat_id': first['chat_id'], 'fields': {'profile_id': ''}}
        assert seed.post(server, first, body)[1]['ok'] and len(delivered) == 2
        assert seed.post(server, first, body)[1]['ok'] and len(delivered) == 2
        child, child_reference = _child(host, delivered, server, row['actor_id'])
        assert child['thread_id'] == first['thread_id'] and child['parent_control_id'] == first['control_id']
        save = {'reference': child_reference, 'decision': 'save', 'message_id': child['message_id'], 'chat_id': child['chat_id'],
            'fields': {'model_backend': 'openai_compatible', 'api_base': 'https://model.example.invalid/v1',
                       'model_name': 'from-full-menu', 'api_key': 'SYNTHETIC_COMPLETE_MENU_KEY', 'model_context_window_tokens': '8192'}}
        assert seed.post(server, child, save)[1]['ok']
        assert seed.post(server, child, save)[1]['ok'] and len(delivered) == 2
        from agent_py_agent.agent.settings.model_profiles import (
            model_profiles_path,
            read_model_profiles,
        )
        data = read_model_profiles(model_profiles_path(host.home_paths))
        assert any(value['model_name'] == 'from-full-menu' for value in data['profiles'].values())
        assert server.agent is None


def test_first_menu_group_configure_never_requests_monitor_membership(tmp_path, monkeypatch):
    from agent_py_agent.agent.adapter.feishu import FeishuAdapter
    def forbidden(*args, **kwargs):
        pytest.fail('模型配置不能调用群监控成员网络')
    monkeypatch.setattr(FeishuAdapter, 'group_authority_snapshot', forbidden)
    with delivery_case(tmp_path, monkeypatch, kind='group') as (case, delivered):
        _agent, _host, _path, row, _body, server, _runtime, _sent = case
        request = model_body(command='/model', group=row['owner']['owner_id'])
        status, result = post_model(server, request, route='/control', actor=row['actor_id'])
        assert status == 200 and result['ok'] and len(delivered) == 1
        assert delivered[0][0] == row['owner']['owner_id']


def test_supported_model_operation_bad_fields_are_original_error_not_waiting(tmp_path, monkeypatch):
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        _agent, _host, _path, _row, _body, server, _runtime, _sent = case
        status, result = post_model(server, model_body(operation='add'))
        assert status == 400 and result['ok'] is False and result.get('status') != 'waiting'
        assert not delivered


def test_first_model_bad_binding_is_not_empty_or_rebuilt(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.owner_conversation_store import (
        owner_conversation_store_from_config,
    )
    from agent_py_agent.agent.owner_scoped_pool import _config_with_owner
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import (
        OwnerIdentity,
        home_paths_with_owner,
        resolve_owner_home,
    )
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        _agent, _host, _path, _row, _body, server, _runtime, _sent = case
        owner = OwnerIdentity.provider_user('feishu', 'ou_fresh_model')
        home = home_paths_with_owner(home_paths(server.control_plane.home_root), resolve_owner_home(server.control_plane.home_root, owner))
        store = owner_conversation_store_from_config(_config_with_owner(server.control_plane.config, owner), home)
        path = store.storage.bindings_path
        path.parent.mkdir(parents=True)
        path.write_text('{invalid-json')
        saved = path.read_bytes()
        assert post_model(server, model_body())[0] == 500
        assert path.read_bytes() == saved and not list(store.storage.threads_dir.glob('*.json'))
        assert not delivered


@pytest.mark.parametrize('route', ['/client/models', '/control'])
def test_positive_c_existing_agent_still_uses_metadata_source_not_foreign_pool(tmp_path, monkeypatch, route):
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        agent, _host, _path, _row, _body, server, _runtime, _sent = case
        server.agent = agent  # 已存在的真实Agent，fixture仍禁止任何新Agent/foreign pool。
        body = model_body() if route == '/client/models' else model_body(command='/model')
        status, result = post_model(server, body, route=route)
        assert status == 200 and result['ok']
        assert len(delivered) == (0 if route == '/client/models' else 1)


# LLM: 只读原control operation ID，原HTTP/G1头及ACL保持，不直接调用reconcile或篡改回执。
# 函数用途: 读取本次模型控制的真实状态。
def get_model_status(server, operation_id, *, actor='ou_fresh_model', credential=True, conversation='model-settings', chat_type='p2p', chat_id=''):
    connection = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=3)
    headers = {'X-Channel': 'feishu', 'X-User-Id': actor, 'X-Conversation-Id': conversation,
               'X-Channel-Chat-Type': chat_type, 'X-Channel-Chat-Id': chat_id}
    if credential:
        headers['X-Gateway-Token'] = server._local_client_credential
    try:
        connection.request('GET', '/control-status/' + operation_id, headers=headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_first_menu_unknown_receipt_is_readable_without_reconcile_or_filesystem_write(tmp_path, monkeypatch):
    from agent_py_agent.agent.adapter.feishu import FeishuAdapter
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        _agent, _host, _path, _row, _body, server, _runtime, _sent = case
        def unknown(*args, **kwargs):
            raise OSError('synthetic transport unknown')
        monkeypatch.setattr(FeishuAdapter, 'send_control_card', unknown)
        status, result = post_model(server, model_body(command='/model'), route='/control')
        assert status == 202 and result['delivery_status'] == 'unknown'
        roots = (server.paths.root, server.control_plane.home_root)
        def snapshot():
            return {str(p): (p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
                for root in roots for p in root.rglob('*')}
        before = snapshot()
        assert get_model_status(server, result['operation_id']) == (200, result)
        assert snapshot() == before
        assert get_model_status(server, result['operation_id'], credential=False)[0] == 403
        assert get_model_status(server, result['operation_id'], actor='ou_foreign')[0] == 403
        assert get_model_status(server, result['operation_id'], conversation='other-session')[0] == 403
        assert not delivered
