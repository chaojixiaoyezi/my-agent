"""固定官方子表请求穿过真实Registry/HTTP/操作账，无真实飞书调用。"""
import json

import pytest

from agent_py_agent.agent.tooling.feishu_cloud_tool import FeishuCloudTool
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests import test_feishu_cloud_collaborator_authorization as collaborators
from agent_py_agent.tests import test_feishu_cloud_tool as cloud
from agent_py_agent.tests._tool_runtime_harness import (
    _authority_attempt_id,
    execute_registry_test_call,
)
from agent_py_agent.tests.test_ingestion_task_credential_scope import _active

cloud_server = cloud.cloud_server
server = collaborators.server


@pytest.mark.parametrize('index', [None, 0, 2])
def test_add_then_rename_real_registry_http_exact_receipt(tmp_path, cloud_server, index):
    agent = cloud._agent(tmp_path, OwnerIdentity.local_main())
    cloud._grant(agent, agent, operations=['sheet_add', 'sheet_rename'])
    options = {} if index is None else {'index': index}
    result = cloud._call(agent, 'sheet_add', title='新子表', **options)
    assert result.ok, (result.error_code, result.output)
    assert json.loads(result.output)['sheet'] == {'sheet_id': 'new_sheet_id', 'title': '新子表', 'index': index or 0}
    writes = [row for row in cloud_server.calls if row[1].endswith('/sheets_batch_update')]
    assert writes[0][0] == 'POST'
    assert writes[0][3] == {'requests': [{'addSheet': {'properties': {'title': '新子表', **options}}}]}
    renamed = cloud._call(agent, 'sheet_rename', sheet_id='new_sheet_id', title='重命名')
    assert renamed.ok, renamed.output
    assert json.loads(renamed.output)['sheet']['sheet_id'] == 'new_sheet_id'
    assert cloud_server.calls[-1][3] == {'requests': [{'updateSheet': {'properties': {'sheetId': 'new_sheet_id', 'title': '重命名'}}}]}
    assert cloud.SECRET not in result.output + renamed.output and cloud.TOKEN not in result.output + renamed.output
    assert all('protect' not in str(row[3]) and '/values' not in row[1] for row in writes)
    assert {'sheet_add', 'sheet_rename'} <= set(FeishuCloudTool.model_spec.input_schema['properties']['action']['enum'])


@pytest.mark.parametrize('action,extra', [('sheet_add', {}), ('sheet_rename', {'sheet_id': 'sid'})])
@pytest.mark.parametrize('kind,identity,member', [
    ('openid', OwnerIdentity.provider_user('feishu', 'ou_one'), 'ou_one'),
    ('openchat', OwnerIdentity.provider_group('feishu', 'oc_one'), 'oc_one')])
@pytest.mark.parametrize('permission', ['view', 'edit'])
def test_current_user_and_group_edit_required_without_cell_protection_bypass(tmp_path, server, action, extra, kind, identity, member, permission):
    agent = cloud._agent(tmp_path, identity)
    agent._capability_config_runtime_snapshot.config.feishu_cloud_collaborator_authorization_enabled = True
    server.members['sheet_one'] = [collaborators._member(kind, member, permission)]
    result = cloud._call(agent, action, title='当前资源子表', **extra)
    writes = [row for row in server.calls if row[1].endswith('/sheets_batch_update')]
    assert result.ok is (permission == 'edit'), (result.error_code, result.output)
    assert len(writes) == (1 if permission == 'edit' else 0)
    assert not any('/values' in row[1] or '/protected_range' in row[1] for row in server.calls)


@pytest.mark.parametrize('params', [
    {'title': ''}, {'title': 'a/b'}, {'title': 'a' * 101}, {'title': 'ok', 'index': True},
    {'title': 'ok', 'index': -1}, {'title': 'ok', 'index': 1.5}])
def test_invalid_add_never_accesses_credentials_or_http(tmp_path, cloud_server, params):
    agent = cloud._agent(tmp_path, OwnerIdentity.local_main())
    cloud._grant(agent, agent, operations=['sheet_add'])
    result = cloud._call(agent, 'sheet_add', **params)
    expected = 'TOOL_PARAMETER_TYPE_INVALID' if 'index' in params and type(params['index']) is not int else 'TOOL_INVALID_ARGUMENTS'
    assert not result.ok and result.reported_error_code == expected
    assert not cloud_server.calls


def _tracked(agent, action, **params):
    current = agent._current_run_params
    return execute_registry_test_call(agent.tools, 'feishu_cloud', {'action': action, 'resource_token': 'sheet_one', **params},
        run_id=current.run_id, call_id='same-sheet-operation', operation_id='original-sheet-operation',
        attempt_id=agent._sheet_attempt, write_boundary={'task_id': current.task_id}, register_with=agent)


@pytest.mark.parametrize('action,extra', [('sheet_add', {}), ('sheet_rename', {'sheet_id': 'sid'})])
@pytest.mark.parametrize('bad_response', [False, True])
def test_unknown_5xx_or_missing_receipt_never_replays_structure_write(tmp_path, cloud_server, action, extra, bad_response):
    agent = cloud._agent(tmp_path, OwnerIdentity.local_main())
    cloud._grant(agent, agent, operations=[action])
    _active(agent)
    agent._sheet_attempt = _authority_attempt_id(agent, agent._current_run_params.run_id, task_id=agent._current_run_params.task_id)
    # 先经正式只读获取宿主token，随后只让业务请求失败。
    adapter = agent.channel_registry.adapter_for('feishu')
    adapter._get_tenant_access_token()
    if bad_response:
        cloud_server.response_data_override = {'replies': []}
    else:
        cloud_server.fail_http = True
    first = _tracked(agent, action, title='不可重复', **extra)
    assert not first.ok and first.effect_outcome == 'unknown'
    cloud_server.fail_http = False
    cloud_server.response_data_override = None
    second = _tracked(agent, action, title='不可重复', **extra)
    assert not second.ok and second.error_code == 'TOOL_OPERATION_OUTCOME_UNKNOWN'
    assert not second.handler_executed
    assert len([row for row in cloud_server.calls if row[1].endswith('/sheets_batch_update')]) == 1


@pytest.mark.parametrize('properties', [{'sheetId': 'other', 'title': '重命名'}, {'sheetId': 'sid', 'title': 'different'}])
def test_rename_conflicting_provider_receipt_stays_unknown(tmp_path, cloud_server, properties):
    agent = cloud._agent(tmp_path, OwnerIdentity.local_main())
    cloud._grant(agent, agent, operations=['sheet_rename'])
    cloud_server.response_data_override = {'replies': [{'updateSheet': {'properties': properties}}]}
    result = cloud._call(agent, 'sheet_rename', sheet_id='sid', title='重命名')
    assert not result.ok and result.effect_outcome == 'unknown'
    assert result.reported_error_code == 'FEISHU_CLOUD_RESPONSE_INVALID'
    assert len([row for row in cloud_server.calls if row[1].endswith('/sheets_batch_update')]) == 1
