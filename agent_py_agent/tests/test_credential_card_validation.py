"""凭据表单错误、卡片类型及原输入重试合同；只用临时owner和合成运输。"""
import io
import json
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from agent_py_agent.agent.adapter import private_controls as controls
from agent_py_agent.agent.conversation.input_dependencies import (
    arm_input_dependency,
    input_dependency_path,
)
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.user_space import task_credentials as tc
from agent_py_agent.tests import test_coordinator_private_credential_transactions as cold
from agent_py_agent.tests.test_credential_callback_replay import _card


# LLM: 只复用原签名/任务/输入和回环HTTP；Count0与冷C各走正式入口，不替换授权或保存函数。
# 函数用途: 在临时目录准备缺目标的原卡，并关闭每次临时监听器。
@contextmanager
def validation_case(tmp_path, monkeypatch, workers, kind='user'):
    # LLM: 只在原卡request入口重发无目标版本，原签名、代次和输入退休继续由产品完成。
    # 函数用途: 给两种临时HTTP执行链生成同一可改正表单。
    def missing_target_card(root, owner_kind):
        agent, _path, old, _body = _card(root, owner_kind)
        ref, _ = tc.request_credential_card(agent, options=tc.CredentialCardOptions(
            refresh_ref=old['credential_ref'], access_scope='task'), call_id='validation')
        path = tc.credential_path(agent.home_paths.owner_home_dir, ref)
        row = json.loads(path.read_text())
        body = {key: row[key] for key in ('reference', 'message_id', 'chat_id')}
        body.update(decision='submit', fields={'username':'synthetic-user', 'secret':'synthetic-secret',
                                             'target_url':'https://example.invalid/path'})
        if owner_kind == 'group':
            body['submitter'] = {'actor_id':row['actor_id'], 'chat_id':row['chat_id'], 'current_group_member':True}
        return agent, path, row, body

    if workers:
        monkeypatch.setattr(cold, '_card', missing_target_card)
        with cold.original_case(tmp_path, monkeypatch, kind=kind) as values:
            yield values
        return
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'home'))
    agent, path, row, body = missing_target_card(tmp_path, kind)
    agent.config.feishu_session_lock_enabled = False
    arm_input_dependency(agent, row['input_id'])
    server = GatewayHTTPServer(0, gateway_paths_from_root(tmp_path / 'gateway'),
                              params=GatewayHTTPServerParams(agent=agent))
    try:
        server.start()
        yield agent, path, row, body, server, None
    finally:
        server.stop()


@pytest.mark.parametrize('workers', [0, 2])
@pytest.mark.parametrize('fault', ['missing', 'extra', 'empty', 'newline', 'too_long', 'bad_url', 'bad_port'])
def test_invalid_form_is_safe_correctable_and_does_not_consume_original_card(tmp_path, monkeypatch, workers, fault):
    with validation_case(tmp_path, monkeypatch, workers) as (agent, path, row, body, server, _runtime):
        valid = dict(body['fields'])
        if fault == 'missing':
            del body['fields']['secret']
        elif fault == 'extra':
            body['fields']['unexpected'] = 'synthetic-untrusted'
        else:
            key, value = {'empty':('secret',''), 'newline':('secret','a\nb'),
                          'too_long':('secret','x'*8193), 'bad_url':('target_url','1'),
                          'bad_port':('target_url','https://example.invalid:bad')}[fault]
            body['fields'][key] = value
        before = path.read_bytes()
        status, result = cold.post(server, row, body)
        assert path.read_bytes() == before
        assert json.loads(input_dependency_path(agent, row['input_id']).read_text())['status'] == 'pending'
        assert not agent.conversation_store.wakes.pending()
        # 先证明原卡可改正成功一次，再核错误分类，避免首个红遮住重试事实。
        body['fields'] = valid
        assert cold.post(server, row, body)[1]['ok'] is True
        assert len(agent.conversation_store.wakes.pending()) == 1
        assert cold.post(server, row, body)[1]['ok'] is True
        assert len(agent.conversation_store.wakes.pending()) == 1
        assert 'synthetic-secret' not in path.read_text()
        assert status == 400
        assert result['error_code'] == 'CREDENTIAL_FORM_INVALID'
        assert result['error_stage'] == 'form'
        assert 'synthetic' not in json.dumps(result)


def test_username_is_text_secret_is_password_and_card_never_prefills():
    row = {'fields':[{'name':'username','label':'账号'}, {'name':'secret','label':'密码'}],
           'authentication':'collected', 'origin':'', 'reference':'synthetic', 'job_id':''}
    inputs = [item for item in tc.credential_card(row)['elements'][1]['elements'] if item['tag']=='input']
    assert len({item['name'] for item in inputs}) == len(inputs)
    assert all('default_value' not in item for item in inputs)
    by_name = {item['name']: item for item in inputs}
    assert by_name['secret']['input_type'] == 'password'
    assert by_name['username'].get('input_type', 'text') == 'text'


def test_custom_fields_do_not_collide_with_form_or_submit_names():
    row = {'fields':[{'name':name, 'label':'自定义'} for name in ('secret','credentials','submit')],
           'authentication':'signature', 'origin':'', 'reference':'synthetic', 'job_id':''}
    form = tc.credential_card(row)['elements'][1]
    names = [form['name'], *[item['name'] for item in form['elements']]]
    assert len(set(names)) == len(names)


@pytest.mark.parametrize('workers', [0, 2])
@pytest.mark.parametrize('fault', ['actor', 'reference', 'message', 'group_member', 'generation', 'owner', 'expiry'])
def test_original_authority_rejection_precedes_form_parser(tmp_path, monkeypatch, workers, fault):
    from agent_py_agent.agent.user_space import task_credential_forms

    with validation_case(tmp_path, monkeypatch, workers, kind='group' if fault=='group_member' else 'user') as (
        agent, path, row, body, server, _runtime
    ):
        def forbidden(*_args):
            pytest.fail('原身份未过不能读取表单')
        monkeypatch.setattr(task_credential_forms, 'credential_submission_values', forbidden)
        before = path.read_bytes()
        kwargs = {}
        if fault == 'actor':
            kwargs['actor'] = 'ou_other'
        elif fault == 'reference':
            body['reference'] += 'invalid'
        elif fault == 'message':
            body['message_id'] = 'wrong'
        elif fault == 'group_member':
            body['submitter']['current_group_member'] = False
        else:
            import base64
            import time

            from agent_py_agent.agent.user_space.interaction_refs import sign_interaction_reference

            encoded = body['reference'].split('.')[1]
            payload = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
            if fault == 'generation':
                payload['generation'] += 1
            elif fault == 'owner':
                payload['owner'] = {**payload['owner'], 'owner_id':'ou_other'}
            else:
                payload['expires_at'] = time.time() - 1
            body['reference'] = sign_interaction_reference(agent.home_paths.owner_home_dir, payload)
        assert cold.post(server, row, body, **kwargs)[0] == 403
        assert path.read_bytes() == before
        assert not agent.conversation_store.wakes.pending()


@pytest.mark.parametrize('workers', [0, 2])
def test_saved_original_card_never_parses_new_fields_or_repeats_wake(tmp_path, monkeypatch, workers):
    from agent_py_agent.agent.user_space import task_credential_forms

    with validation_case(tmp_path, monkeypatch, workers) as (agent, path, row, body, server, _runtime):
        assert cold.post(server, row, body)[1]['ok'] is True
        before = path.read_bytes()
        def forbidden(*_args):
            pytest.fail('已处理卡不能再次读取表单')
        monkeypatch.setattr(task_credential_forms, 'credential_submission_values', forbidden)
        body['fields'] = {'secret':'synthetic-must-ignore'}
        assert cold.post(server, row, body)[1]['ok'] is True
        assert path.read_bytes() == before
        assert len(agent.conversation_store.wakes.pending()) == 1
        body['decision'] = 'cancel'
        assert cold.post(server, row, body)[1]['error_code'] == 'IM_INTERACTION_ALREADY_HANDLED'
        assert path.read_bytes() == before


def test_adapter_preserves_fixed_form_error_and_keeps_original_card(tmp_path, monkeypatch):
    import io
    from types import SimpleNamespace
    from urllib.error import HTTPError

    from agent_py_agent.agent.adapter import private_controls

    adapter = SimpleNamespace(my_agent_home=str(tmp_path), _gateway_port=1,
                              _gateway_auth_token='synthetic-host-token')
    # LLM: 只替代末端认证HTTP传输，响应故意带恶意任意文本；产品只能投影固定码、阶段和文案。
    # 函数用途: 模拟新表单400，确保字段和服务器任意message不会进入toast。
    def invalid_response(*_args, **_kwargs):
        raw = json.dumps({'ok':False, 'error_code':'CREDENTIAL_FORM_INVALID', 'error_stage':'form',
                          'error_field':'target_url', 'message':'synthetic-untrusted-secret'}).encode()
        raise HTTPError('http://127.0.0.1/', 400, 'synthetic', {}, io.BytesIO(raw))
    monkeypatch.setattr(private_controls, 'open_authenticated_request', invalid_response)
    result = private_controls.tool_approval_card_action(adapter, {
        'value':{'kind':'my_agent_task_credential', 'reference':'synthetic', 'decision':'submit'},
        'operator_open_id':'ou_synthetic', 'message_id':'om_synthetic', 'chat_id':'oc_synthetic',
        'form_value':{'secret':'synthetic-input-secret'},
    })
    assert 'card' not in result
    assert 'synthetic' not in json.dumps(result)
    assert 'CREDENTIAL_FORM_INVALID/form' in result['toast']['content']
    assert '原卡' in result['toast']['content']


@pytest.mark.parametrize('workers', [0, 2])
def test_adapter_original_http_round_trip_preserves_editable_form_then_saves_once(tmp_path, monkeypatch, workers):
    from types import SimpleNamespace

    from agent_py_agent.agent.adapter import private_controls

    with validation_case(tmp_path, monkeypatch, workers) as (agent, path, row, body, server, _runtime):
        adapter = SimpleNamespace(my_agent_home=str(agent.home_paths.root),
            _gateway_port=server.server.server_address[1], _gateway_auth_token=server._local_client_credential)
        norm = {'value':{'kind':'my_agent_task_credential', 'reference':row['reference'], 'decision':'submit'},
                'operator_open_id':row['actor_id'], 'message_id':row['message_id'], 'chat_id':row['chat_id'],
                'form_value':{**body['fields'], 'target_url':'1'}}
        before = path.read_bytes()
        invalid = private_controls.tool_approval_card_action(adapter, norm)
        assert 'CREDENTIAL_FORM_INVALID/form' in invalid['toast']['content']
        assert 'card' not in invalid and path.read_bytes() == before
        assert not agent.conversation_store.wakes.pending()
        norm['form_value'] = body['fields']
        ready = private_controls.tool_approval_card_action(adapter, norm)
        assert ready['toast']['type'] == 'success' and 'card' in ready
        assert 'synthetic-secret' not in json.dumps(ready)
        assert len(agent.conversation_store.wakes.pending()) == 1


def test_form_error_only_wraps_new_target_and_has_fixed_safe_metadata():
    from agent_py_agent.agent.user_space.task_credential_forms import (
        CredentialFormValidationError,
        credential_submission_values,
    )
    row = {'fields':[{'name':'secret'}], 'origin':''}
    with pytest.raises(CredentialFormValidationError) as captured:
        credential_submission_values(row, {'secret':'synthetic', 'target_url':'1'}, tc.credential_origin)
    assert captured.value.field == 'target_url'
    assert captured.value.code == 'CREDENTIAL_FORM_INVALID'
    assert captured.value.__suppress_context__ is True
    assert 'synthetic' not in str(captured.value)
    row['origin'] = '1'
    with pytest.raises(ValueError) as existing:
        credential_submission_values(row, {'secret':'synthetic'}, tc.credential_origin)
    assert type(existing.value) is ValueError


@pytest.mark.parametrize('code', [[], {}, None, 7, 'CREDENTIAL_FORM_INVALID'])
@pytest.mark.parametrize('message', [[], {}, None])
def test_http_error_body_type_does_not_escape_fixed_projection(tmp_path, monkeypatch, code, message):
    adapter = SimpleNamespace(my_agent_home=str(tmp_path), _gateway_port=1,
                              _gateway_auth_token='synthetic-host-token')
    def response(*_args, **_kwargs):
        raw = json.dumps({'error_code':code, 'message':message}).encode()
        raise HTTPError('http://127.0.0.1/', 400, 'synthetic', {}, io.BytesIO(raw))
    monkeypatch.setattr(controls, 'open_authenticated_request', response)
    result = controls._submit_control_request(adapter, 'ou_synthetic', {'decision':'submit'})
    expected = ('CREDENTIAL_FORM_INVALID', 'form') if code == 'CREDENTIAL_FORM_INVALID' else ('IM_CONTROL_TRANSPORT_UNAVAILABLE', 'http')
    assert result == {'ok':False, 'error_code':expected[0], 'error_stage':expected[1]}
