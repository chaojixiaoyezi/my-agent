"""BIZ-006 原错误动作经真实HTTP/生产客户端验证，不调用模型或正式Gateway。"""
import json

import pytest

from agent_py_agent.tests.test_acceptance_business_error_transport import gateway as gateway


def _write(client, action, params, **options):
    return client.send(client.prepare(action, params, **options))


def _setup(gateway):
    agent, client = gateway
    # 复用fixture提供的真实NetworkGatewayObserver；只为本隔离owner补认证主体。
    original_headers = agent.gateway_network_headers
    agent.gateway_network_headers = lambda: {**original_headers(), 'X-User-Id': 'business-alice', 'X-Channel': 'feishu'}
    first = _write(client, 'spaces.create', {'name': 'space-a'}, idempotency_key='biz006-space-a-0001')
    second = _write(client, 'spaces.create', {'name': 'space-b'}, idempotency_key='biz006-space-b-0001')
    assert first['ok'] and second['ok']
    created = _write(client, 'background_agents.create', {'space_id': first['data']['space_id'], 'name': 'agent-a'},
                          idempotency_key='biz006-agent-a-0001')
    assert created['ok'], created
    return client, first['data']['space_id'], second['data']['space_id'], created['data']['agent']


@pytest.mark.parametrize('scenario', ['wrong-space', 'missing-model'])
def test_original_rejected_write_has_precise_error_and_no_state_change(gateway, scenario, tmp_path):
    client, space_a, space_b, agent = _setup(gateway)
    before = client.call('background_agents.model.get', {'agent_id': agent['agent_id']})
    tasks_before = client.call('tasks.list', {'filter': {'agent_id': agent['agent_id']}})
    assert before['ok'] and tasks_before['ok'], (before, tasks_before)
    if scenario == 'wrong-space':
        prepared = client.prepare('tasks.assign', {'agent_id': agent['agent_id'], 'space_id': space_b,
                                                   'instruction': 'isolated rejected request'},
                                  idempotency_key='biz006-wrong-space-0001')
    else:
        prepared = client.prepare('background_agents.model.set', {'agent_id': agent['agent_id'],
                                    'model_profile_id': 'nonexistent-biz006-profile'},
                                  expected_revision=before['data']['selection_revision'],
                                  idempotency_key='biz006-missing-model-0001')
    rejected = client.send(prepared)
    after = client.call('background_agents.model.get', {'agent_id': agent['agent_id']})
    tasks_after = client.call('tasks.list', {'filter': {'agent_id': agent['agent_id']}})
    evidence = {'scenario': scenario, 'rejection': rejected, 'model_before': before['data'],
                'model_after': after['data'], 'tasks_before': tasks_before['data'], 'tasks_after': tasks_after['data']}
    destination = tmp_path / ('BIZ006-' + scenario + '.json')
    destination.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
    assert not rejected['ok']
    assert rejected['error']['code'] == ('SPACE_MISMATCH' if scenario == 'wrong-space' else 'MODEL_PROFILE_NOT_FOUND')
    assert rejected['state'] == 'failed' and rejected['effect_outcome'] == 'not_started'
    assert rejected['request_id'] == prepared.payload()['request_id']
    assert rejected['error']['code'] != 'TOOL_OPERATION_OUTCOME_UNKNOWN'
    assert before['data'] == after['data']
    assert tasks_before['data'] == tasks_after['data']


@pytest.mark.parametrize('field', ['space_id', 'project_ref'])
def test_assignment_scope_error_names_bound_field_without_creating_task(gateway, field):
    client, space_a, space_b, agent = _setup(gateway)
    params = {'agent_id': agent['agent_id'], 'space_id': space_a, 'instruction': '独立合成验收'}
    params[field] = space_b if field == 'space_id' else 'test-marker-is-not-a-project'
    before = client.call('tasks.list', {'filter': {'agent_id': agent['agent_id']}})
    reply = _write(client, 'tasks.assign', params, idempotency_key='assignment-field-conflict-' + field)
    assert reply['error']['code'] == 'SPACE_MISMATCH'
    details = {row['key']: row['value'] for row in reply['error']['details']}
    assert details['field_path'] == '$.params.' + field
    assert details['actual'] == params[field]
    assert details['expected'] == (space_a if field == 'space_id' else None)
    assert details['side_effect_state'] == 'not_started' and details['task_created'] is False
    assert details['retry_same_arguments'] is False
    assert 'background_agents.get' in details['read_example']
    if field == 'project_ref':
        assert '省略project_ref' in details['next_action']
    after = client.call('tasks.list', {'filter': {'agent_id': agent['agent_id']}})
    assert before['data'] == after['data']


@pytest.mark.parametrize('field,value,constraint,expected,actual_length,unit', [
    ('name', '合成秘密片段' * 60, 'maxLength', 120, 360, 'characters'),
    ('state', 'legacy-done-synthetic-secret', 'enum', '["active","archived"]', None, None),
    ('revision', 'synthetic-private-revision', 'type', 'integer', None, None),
    ('space_id', '中' * 43, 'x-max-bytes', 128, 129, 'utf8_bytes'),
])
def test_domain_result_validation_survives_real_http_without_entity_text(
        gateway, monkeypatch, tmp_path, field, value, constraint, expected, actual_length, unit):
    from agent_py_agent.agent.conversation.business_spaces import BusinessSpaceService

    _, client = gateway
    created = _write(client, 'spaces.create', {'name': 'isolated-valid-space'},
                     idempotency_key='result-validation-shape-0001')
    assert created['ok'], created
    original = BusinessSpaceService.space_view
    calls = []

    def invalid_view(self, row, *, actor):
        result = original(self, row, actor=actor)
        calls.append(row['space_id'])
        return {**result, field: value}

    monkeypatch.setattr(BusinessSpaceService, 'space_view', invalid_view)
    prepared = client.prepare('spaces.get', {'space_id': created['data']['space_id']})
    reply = client.send(prepared)
    assert calls and set(calls) == {created['data']['space_id']}
    assert reply['error']['code'] == 'DOMAIN_RESULT_INVALID'
    assert reply['state'] == 'failed' and reply['data'] == {}
    assert reply['request_id'] == prepared.payload()['request_id']
    details = {row['key']: row['value'] for row in reply['error']['details']}
    assert details['field_path'] == '$.space.' + field
    assert details['constraint'] == constraint and details['expected'] == expected
    assert details['validation_stage'] == 'domain_result'
    assert '不要重新发起写操作' in details['next_action']
    if actual_length is not None:
        assert details['actual_length'] == actual_length and details['length_unit'] == unit
    assert value not in json.dumps(reply, ensure_ascii=False)
    assert len(json.dumps(reply, ensure_ascii=False).encode()) < 8192
    (tmp_path / ('domain-result-' + field + '.json')).write_text(
        json.dumps(reply, ensure_ascii=False, indent=2) + '\n')
    monkeypatch.setattr(BusinessSpaceService, 'space_view', original)
    assert client.call('spaces.get', {'space_id': created['data']['space_id']})['ok']
