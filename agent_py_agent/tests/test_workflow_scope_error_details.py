"""四类范围拒绝保留原码和未执行事实，并公开可操作的具体约束。"""
from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.conversation.business_contracts import WorkflowScope
from agent_py_agent.agent.gateway_parts.business_api import _error_details
from agent_py_agent.agent.plugin_host.business_action_specs import business_action_spec
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.plugin_host.schemas import validate_object
from agent_py_agent.cli.space_commands import PreparedBusinessCall
from agent_py_agent.tests.test_acceptance_business_error_transport import gateway as gateway
from agent_py_agent.tests.test_business_v12_f4_workflows import definition, service

CASES = [
    ('owner_global', ['private-space-sentinel'], 'maxItems', 0),
    ('unassigned', ['private-space-sentinel'], 'maxItems', 0),
    ('spaces', [], 'minItems', 1),
    ('spaces', ['private-space-sentinel'] * 2, 'uniqueItems', True),
]


def request(mode, ids):
    return {'action': 'workflows.scope.set', 'request_id': 'scope-error-diagnostics',
            'idempotency_key': 'scope-error-diagnostics-once', 'expected_revision': 1,
            'params': {'workflow_ref': 'unused-workflow', 'scope': {'scope_mode': mode, 'space_ids': ids}}}


@pytest.mark.parametrize('mode,ids,constraint,expected', CASES)
def test_schema_scope_refusal_has_precise_safe_details(mode, ids, constraint, expected):
    with pytest.raises(HostAPIError) as caught:
        validate_object(request(mode, ids), business_action_spec('workflows.scope.set').native_request_schema)
    error = caught.value
    assert error.code == 'INVALID_SCHEMA' and error.outcome == 'not_started'
    assert error.details['field_path'] == '$.params.scope.space_ids'
    assert error.details['constraint'] == constraint and error.details['expected'] == expected
    assert error.details['next_action'] and 'private-space-sentinel' not in json.dumps(error.details)
    if constraint != 'uniqueItems':
        assert json.loads(error.details['schema_selector']) == {'scope_mode': mode}


@pytest.mark.parametrize('mode,ids,constraint,expected', CASES)
def test_original_domain_scope_guard_reports_same_constraint_without_write(tmp_path, mode, ids, constraint, expected):
    api, actor, _ = service(tmp_path)
    with pytest.raises(BusinessError) as caught:
        api.execute('workflows.create', {'definition': definition(), 'scope': {'scope_mode': mode, 'space_ids': ids}},
                    actor=actor, idempotency_key='scope-error-domain')
    error = caught.value
    assert error.code == 'INVALID_SCHEMA'
    assert error.details['constraint'] == constraint and error.details['expected'] == expected
    assert error.details['schema_selector'] == mode
    assert error.details['next_action'] and 'private-space-sentinel' not in json.dumps(error.details)
    assert api.execute('workflows.list', {}, actor=actor)['items'] == []
    assert {row['key']: row['value'] for row in _error_details(error)} == error.details


@pytest.mark.parametrize('mode,ids,constraint,expected', CASES)
def test_real_client_server_scope_error_roundtrip(gateway, mode, ids, constraint, expected):
    _, client = gateway
    response = client.send(PreparedBusinessCall(json.dumps(request(mode, ids))))
    assert response['http_status'] == 400 and response['state'] == 'failed'
    assert response['effect_outcome'] == 'not_started' and not response['ok']
    assert response['error']['code'] == 'INVALID_SCHEMA'
    details = {item['key']: item['value'] for item in response['error']['details']}
    assert details['field_path'] == '$.params.scope.space_ids'
    assert details['constraint'] == constraint and details['expected'] == expected
    assert details['next_action'] and 'private-space-sentinel' not in json.dumps(details)


@pytest.mark.parametrize('mode,ids', [('owner_global', []), ('unassigned', []), ('spaces', ['one']), ('spaces', ['one', 'two'])])
def test_valid_scope_unchanged(mode, ids):
    value = request(mode, ids)
    assert validate_object(value, business_action_spec('workflows.scope.set').native_request_schema) == value
    assert WorkflowScope(mode, ids, 1).space_ids == tuple(ids)
