"""工具详情返回的原结果引用可经真实客户端读取命令双流及覆盖状态。"""
import json

import pytest

from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.agent.tooling.tool_operation_coordinator import _result_payload
from agent_py_agent.cli.space_commands import BusinessClient
from agent_py_agent.tests.test_mega_business_api_actions import (
    background_http as background_http,
)
from agent_py_agent.tests.test_mega_business_api_actions import (
    native_request,
    observed_native_task,
    seed_observed_runtime,
)


def command_source(native, *, display=None, stored_input=None):
    services, task = observed_native_task(native)
    repo, run = seed_observed_runtime(services, task)
    operation = repo.create_tool_operation(agent_run_id=run['agent_run_id'],
        attempt_id=run['attempt_id'], operation_type='run_command')
    ref = 'tool-operation://command-test/' + operation['operation_id']
    view = {'kind': 'command', 'return_code': 7, 'stdout': '标准输出\n' * 20,
        'stderr': 'api_key=synthetic-secret-value\nwarning\n',
        'stdout_truncated': False, 'stderr_truncated': False, 'capture_complete': True}
    envelope = {'display': view if display is None else display,
        'tool_operation': {'result_ref': ref}}
    result = ToolHandlerOutcome(tool='run_command', ok=False, output='',
        result_envelope=envelope, handler_executed=True, duration_ms=12, effect_outcome='failed')
    repo.mark_operation_executing(operation['operation_id'])
    repo.settle_operation(operation['operation_id'], 'FAILED', {'result': _result_payload(result,
        redacted_input={'command': 'command --safe', 'working_dir': './work'} if stored_input is None else stored_input)})

    def transport(path, request, **kwargs):
        status, _, raw = native_request(native, 'POST', path, request)
        return status, json.loads(raw)

    return BusinessClient(transport), operation['operation_id'], ref


def test_detail_reference_reads_bounded_redacted_command_streams(background_http):
    client, operation_id, ref = command_source(background_http)
    detail = client.call('tool_calls.get', {'operation_id': operation_id})
    assert detail['ok'] and detail['data']['result_ref'] == ref
    assert detail['data']['redacted_input']['working_dir'] == './work'
    params = {'source_ref': ref, 'max_bytes': 64}
    streams = {'host': '', 'stdout': '', 'stderr': ''}
    seen = set()
    for _ in range(30):
        reply = client.call('logs.read', params)
        assert reply['ok'], reply
        data = reply['data']
        assert sum(chunk['byte_count'] for chunk in data['chunks']) <= 64
        for chunk in data['chunks']:
            streams[chunk['stream']] += chunk['text']
        cursor = data['next_cursor']
        if cursor is None:
            break
        assert cursor not in seen
        seen.add(cursor)
        params['cursor'] = cursor
    assert data['coverage'] == 'complete' and data['gap'] is None
    assert streams['stdout'] == '标准输出\n' * 20
    assert 'warning' in streams['stderr'] and 'synthetic-secret-value' not in streams['stderr']
    assert json.loads(streams['host'])['return_code'] == 7
    status, _, _ = native_request(background_http, 'POST', '/client/business',
        {'action': 'logs.read', 'request_id': 'other-owner', 'params': {'source_ref': ref}},
        user='business-bob')
    assert status != 200


@pytest.mark.parametrize(('display', 'coverage', 'reason'), [
    ({}, 'unknown', 'STRUCTURED_COMMAND_LOG_UNAVAILABLE'),
    ({'kind': 'command', 'stdout': 'x', 'stderr': '', 'stdout_truncated': True,
      'capture_complete': True}, 'partial', 'COMMAND_CAPTURE_TRUNCATED'),
    ({'kind': 'command', 'stdout': 'x', 'capture_complete': False},
     'partial', 'COMMAND_CAPTURE_INCOMPLETE'),
])
def test_missing_and_incomplete_receipts_do_not_report_complete(background_http, display, coverage, reason):
    client, _, ref = command_source(background_http, display=display)
    result = client.call('logs.read', {'source_ref': ref})
    assert result['ok'], result
    assert result['data']['coverage'] == coverage
    assert result['data']['gap']['reason_code'] == reason


def test_command_log_cursor_and_reference_must_match_original_receipt(background_http):
    client, _, ref = command_source(background_http)
    first = client.call('logs.read', {'source_ref': ref, 'max_bytes': 8})
    assert first['ok'] and first['data']['next_cursor']
    invalid = client.call('logs.read', {'source_ref': ref, 'cursor': 'tampered'})
    assert not invalid['ok'] and invalid['error']['code'] == 'CURSOR_EXPIRED'
    invented = client.call('logs.read', {'source_ref': ref.replace('command-test', 'invented')})
    assert not invented['ok']


@pytest.mark.parametrize('value', ['syntheticCLIToken123', '"synthetic CLI Token123"',
    "'synthetic CLI Token123'", '"synthetic\nCLI Token123"'])
def test_legacy_raw_command_receipts_are_redacted_on_both_public_reads(background_http, value):
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

    raw_input = {'command': 'command --token ' + value + ' --output kept',
        'working_dir': '/synthetic/private/work', 'api_key': 'syntheticNested123',
        'environment_ref': 'command --token "$TOKEN" --verbose'}
    display = {'kind': 'command', 'return_code': 0, 'capture_complete': True,
        'stdout': 'command --password ' + value + '\nordinary output',
        'stderr': 'Authorization: Bearer syntheticAuthorization123\ncommand --token "$TOKEN"'}
    client, operation_id, ref = command_source(background_http, display=display, stored_input=raw_input)
    # 模拟历史未预脱敏回执，原数据库确实仍有明文；读取必须独立防守且不能回写历史。
    [services] = [s for s in background_http[0]._business_services.values() if s.owner_id.endswith('/business-alice')]
    repo = RuntimeRepository(services.owner_home / 'runtime.db', read_only=True)
    with repo._runtime_connection() as db:
        before = db.execute('SELECT outcome_json FROM tool_operations WHERE operation_id=?', (operation_id,)).fetchone()[0]
    assert value in json.loads(before)['result']['redacted_input']['command']
    detail = client.call('tool_calls.get', {'operation_id': operation_id})
    assert detail['ok'], detail
    assert detail['data']['redacted_input']['command'] == 'command --token <redacted> --output kept'
    assert detail['data']['redacted_input']['working_dir'] == '/synthetic/private/work'
    assert detail['data']['redacted_input']['environment_ref'] == raw_input['environment_ref']
    assert 'syntheticNested123' not in json.dumps(detail)
    logs = client.call('logs.read', {'source_ref': ref})
    assert logs['ok'], logs
    streams = {chunk['stream']: chunk['text'] for chunk in logs['data']['chunks']}
    assert streams['stdout'] == 'command --password <redacted>\nordinary output'
    assert 'syntheticAuthorization123' not in streams['stderr']
    assert 'command --token "$TOKEN"' in streams['stderr']
    header = json.loads(streams['host'])
    assert header['redacted_input'] == detail['data']['redacted_input']
    assert 'synthetic' not in json.dumps(logs['data']).replace('/synthetic/private/work', '')
    with repo._runtime_connection() as db:
        after = db.execute('SELECT outcome_json FROM tool_operations WHERE operation_id=?', (operation_id,)).fetchone()[0]
    assert after == before


@pytest.mark.parametrize('source', ['tool_operation:synthetic',
    'tool-operation://synthetic/tool_operation:synthetic/logs', 'audit:synthetic'])
def test_invalid_log_reference_gives_exact_field_and_read_next_step(background_http, source):
    client, _, _ = command_source(background_http)
    reply = client.call('logs.read', {'source_ref': source})
    assert not reply['ok'] and reply['error']['code'] == 'INVALID_SCHEMA'
    details = {item['key']: item['value'] for item in reply['error']['details']}
    assert details['field_path'] == '$.params.source_ref'
    assert 'data.result_ref' in details['next_action'] and details['retry_same_request'] is False
    assert reply['effect_outcome'] == 'not_started'


def test_missing_tool_record_does_not_imply_execution_status(background_http):
    client, _, _ = command_source(background_http)
    reply = client.call('tool_calls.get', {'operation_id': 'synthetic-missing'})
    assert not reply['ok'] and reply['error']['code'] == 'OBSERVATION_NOT_FOUND'
    details = {item['key']: item['value'] for item in reply['error']['details']}
    assert details['field_path'] == '$.params.operation_id'
    assert '缺记录不能证明' in details['reason'] and details['retry_same_request'] is False
