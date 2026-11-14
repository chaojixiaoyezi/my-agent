"""可信业务源已提交与外层未知分列，展示旁证绝不成为重放授权。"""
import json
from copy import deepcopy

import pytest

from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.tests import test_business_model_tools as business
from agent_py_agent.tests import test_tool_operation_idempotency as operations

agent = business.agent
FACT = {'source': 'business_host', 'action': 'spaces.create', 'request_id': 'source-request',
        'operation_id': None, 'effect_outcome': 'committed'}


def test_actual_domain_write_then_invalid_projection_retains_host_source_fact(agent, monkeypatch):
    original = api.BusinessServices.invoke

    def malformed(self, action, params, context, **kwargs):
        result = original(self, action, params, context, **kwargs)
        if action == 'spaces.create':
            return {**result, 'unexpected_after_actual_write': True}
        return result

    monkeypatch.setattr(api.BusinessServices, 'invoke', malformed)
    tool = agent.tools.tools['business_spaces_create']
    result = tool.execute({'action': 'spaces.create', 'params': {'name': 'source-effect-proof'},
        'request_id': FACT['request_id'], 'idempotency_key': 'source-effect-create-0001'})
    assert not result.ok and result.effect_outcome == 'unknown'
    assert json.loads(result.output)['effect_outcome'] == 'committed'
    assert result.source_effect == FACT
    state = api._local_business_services(agent).spaces._read()
    assert len(state['spaces']) == 1


def test_source_effect_survives_unknown_wrapper_storage_and_reopen_without_reexecution(tmp_path):
    store = LocalStore(tmp_path / 'operations.db', enable_fts=False)
    reported = ToolHandlerOutcome('counting_write', False, 'post-write projection failed',
        error_code='INVALID_SCHEMA', effect_outcome='unknown', source_effect=FACT)
    tool = operations._CountingTool(result=reported)
    first = operations._execute(operations._registry(tmp_path, store, tool), operations._call('run-1', 'call-1', 1))
    assert first.effect_outcome == 'unknown' and first.metadata['source_effect'] == FACT
    text = first.render_for_prompt()
    assert 'effect_outcome=unknown' in text and 'source_effect_outcome=committed' in text
    reopened = LocalStore(tmp_path / 'operations.db', enable_fts=False)
    second_tool = operations._CountingTool()
    second = operations._execute(operations._registry(tmp_path, reopened, second_tool), operations._call('run-1', 'call-1', 1))
    assert not second.ok and not second.handler_executed and second_tool.calls == 0
    assert second.metadata['source_effect'] == FACT
    assert tool.calls == 1
    record = reopened.get_tool_operation(owner_id='owner-a', run_id='run-1', operation_id='tool_call:call-1')
    assert record.status == 'unknown' and record.result['source_effect'] == FACT


@pytest.mark.parametrize('mode', ['provider_output', 'nested_envelope', 'invalid_source'])
def test_provider_or_nested_lookalike_never_becomes_source_fact(tmp_path, mode):
    values = {'output': json.dumps({'source_effect': FACT})}
    if mode == 'nested_envelope':
        values['result_envelope'] = {'source_effect': FACT}
    if mode == 'invalid_source':
        values['source_effect'] = {**FACT, 'source': 'provider'}
    outcome = ToolHandlerOutcome('counting_write', False, error_code='TOOL_TIMEOUT', **values)
    tool = operations._CountingTool(result=outcome)
    result = operations._execute(operations._registry(tmp_path, LocalStore(tmp_path / 'ops.db', enable_fts=False), tool),
        operations._call('run-1', 'call-1', 1))
    assert 'source_effect' not in result.metadata
    assert 'source_effect_outcome=' not in result.render_for_prompt()


@pytest.mark.parametrize('change', ['wrong_request', 'unknown', 'malformed', 'nested'])
def test_host_source_issuer_requires_matching_valid_envelope(change):
    envelope = api._response(FACT['request_id'], error={'code': 'INVALID_SCHEMA', 'message': 'invalid result',
        'retryable': False, 'details': []}, effect='committed')
    expected = FACT['request_id']
    if change == 'wrong_request':
        expected = 'different-request'
    elif change == 'unknown':
        envelope['effect_outcome'] = 'unknown'
    elif change == 'malformed':
        envelope['untrusted_extra'] = True
    else:
        envelope['effect_outcome'] = 'unknown'
        envelope['data'] = {'effect_outcome': 'committed'}
    assert api._business_tool_source_effect('spaces.create', 503, deepcopy(envelope), expected) == {}


@pytest.mark.parametrize('change', [
    {'effect_outcome': []}, {'request_id': '\ud800'}, {'operation_id': '\udfff'},
    {'request_id': ''}, {'operation_id': 12}, {'action': 'unsafe\nheader'},
])
def test_malformed_source_fact_is_omitted_without_masking_original_error(change):
    from agent_py_agent.agent.tooling.models import source_effect_facts

    assert source_effect_facts({**FACT, **change}) == {}
