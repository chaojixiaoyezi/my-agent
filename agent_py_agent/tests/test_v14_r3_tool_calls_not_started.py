"""V14F-09：原收口事务写的未启动取消回执经真实业务读口可读，其余坏结果仍失败关闭。"""
from __future__ import annotations

import gc
import json
import os
import sys

import pytest

from agent_py_agent.agent.business_spaces.v13_store import V13Store
from agent_py_agent.agent.conversation.business_access import BusinessActor
from agent_py_agent.agent.conversation.session_tasks import SessionTaskUpdate
from agent_py_agent.agent.plugin_host.business_action_specs import business_action_spec
from agent_py_agent.agent.tooling.runtime_contracts import tool_arguments_hash
from agent_py_agent.tests import test_v13_w0_observation_readers as observation
from agent_py_agent.tests.test_mega_business_api_actions import seed_observed_runtime
from agent_py_agent.tests.test_v13_w0_harness import v13_seed

# 绑定原夹具对象，保留 pytest 发现语义；本模块只补审批读口需要的真实待处理目录。
_observation_gateway = observation.background_http


# LLM: 所有测试沿任务tmp home，不读取真实运行环境。
# 函数用途: 让夹具创建的原生Gateway始终使用隔离HOME。
@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'home'))


# LLM: 原生审批读口要求真实队列目录已初始化；只补服务器启动条件，不签票或替换reader。
# 函数用途: 在原HTTP服务器上初始化待处理目录，让脚本运行观察具备真实审批来源。
@pytest.fixture
def background_http(_observation_gateway):
    _observation_gateway[0].paths.processing.mkdir(parents=True, exist_ok=True)
    return _observation_gateway


# LLM: 只计当前进程描述符个数，不打开额外句柄。
# 函数用途: 为fd_delta断言取样。
def _fd_count():
    return len(os.listdir('/dev/fd' if sys.platform == 'darwin' else '/proc/self/fd'))


# LLM: 原RuntimeRepository领取口只建CLAIMED占位且handler_started_at=0，不进入handler。
# 函数用途: 在当前attempt下领取一次从未启动的工具操作。
def _claim_unstarted(runtime, run, tool):
    return runtime.create_tool_operation(agent_run_id=run['agent_run_id'], attempt_id=run['attempt_id'],
        operation_type=tool)['operation_id']


# LLM: 原settle_agent_attempt事务调用_cancel_unstarted_tool_operations写回执，测试不手写回执字典。
# 函数用途: 结束当前attempt并确认真实收口成功。
def _settle_attempt(runtime, run):
    settled = runtime.settle_agent_attempt(agent_run_id=run['agent_run_id'], attempt_id=run['attempt_id'])
    assert settled['settled'] is True, settled


# LLM: 真实writer→reader→service→HTTP→冻结schema；未启动行必须投影为未执行，覆盖完整。
# 函数用途: 验证list/get都把原未启动取消回执读成not_started，且读取不残留描述符。
def test_not_started_cancellation_receipt_is_readable_as_not_executed(background_http):
    services, task = observation.observed_task(background_http)
    runtime, run = seed_observed_runtime(services, task)
    operation_id = _claim_unstarted(runtime, run, 'write_file')
    _settle_attempt(runtime, run)
    row = runtime.get_operation(operation_id)
    stored = json.loads(row['outcome_json'])['result']
    assert row['status'] == 'CANCELLED' and row['handler_started_at'] == 0 and row['settled_at'] > 0
    assert stored['effect_outcome'] == 'not_started' and stored['handler_executed'] is False
    assert 'result_ref' not in stored and 'duration_ms' not in stored, '回执形状变化时需复核读口识别条件'
    observation.wire(services, runtime)
    expected = {'operation_id': operation_id, 'run_id': 'observed-' + task.task_id, 'attempt_id': run['attempt_id'],
        'task_id': task.task_id, 'tool_name': 'write_file', 'status': 'CANCELLED', 'executed': False,
        'effect_outcome': 'not_started', 'duration_ms': None,
        'error': {'code': 'TOOL_OPERATION_CANCELLED_NOT_STARTED', 'message': '操作从未启动(not_started, G.5 CANCELLED)，无副作用；任务层面可继续/重发。',
            'retryable': False, 'details': [{'key': 'recommended_action', 'value': 'continue'}]},
        'result_ref': operation_id}
    status, listed = observation.background_call(background_http, 'tool_calls.list')
    assert status == 200, listed
    data = business_action_spec('tool_calls.list').validate_result(listed['data'])
    assert data['coverage'] == 'complete' and data['items'] == [expected], data
    status, detail = observation.background_call(background_http, 'tool_calls.get', {'operation_id': operation_id})
    assert status == 200, detail
    data = business_action_spec('tool_calls.get').validate_result(detail['data'])
    assert data['tool_call'] == expected
    assert data['effect_outcome'] == 'not_started' and data['result_ref'] == operation_id
    actor = BusinessActor(services.owner_id, 'r3-reader', services.observation, lambda action, facts: True)
    gc.collect()
    before = _fd_count()
    for _ in range(3):
        page = services.observation.query('tool_calls.list', {}, actor=actor)
        point = services.observation.query('tool_calls.get', {'operation_id': operation_id}, actor=actor)
    gc.collect()
    assert _fd_count() - before <= 0
    assert page['items'] == [expected] and page['coverage'] == 'complete'
    assert point['tool_call'] == expected


# LLM: 已批准脚本的任务历史同时含真实脚本结算与一条从未启动的scripts领取；后者不能让get_run降为unknown，
#   也不能被当成已执行的脚本结果。脚本资产与准入记录沿原HTTP/V13Store，结果沿原managed写口。
# 函数用途: 验证scripts.runs.get仍从真实结算恢复终态。
def test_approved_script_history_with_not_started_row_recovers_terminal_result(background_http):
    services, task = observation.observed_task(background_http)
    status, created = observation.background_call(background_http, 'scripts.create', {
        'script_id': 'r3-script', 'name': '未启动历史', 'code': 'print(\'{"value": 7}\')',
        'input_schema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
        idempotency_key='r3-script-create')
    assert status == 200, created
    status, executed = observation.background_call(background_http, 'scripts.run',
        {'script_id': 'r3-script', 'inputs': {}}, idempotency_key='r3-script-native-run')
    assert status == 200, executed
    actual = executed['data']['run']
    assert actual['state'] == 'completed' and actual['ok'] is True, actual
    admitted = V13Store(services.owner_home).get('script_runs', actual['execution_id'])
    args = {'action': 'run', 'script_id': 'r3-script', 'inputs': {}}
    hashes = [tool_arguments_hash(args), tool_arguments_hash({'action': 'run', 'script_id': 'r3-script'})]
    raw = {'script_id': 'r3-script', 'ok': True, 'output_data': actual['output'],
        'duration_ms': actual['duration_ms'], 'started_at': 1, 'ended_at': 2,
        'error_code': actual['error'], 'effect_outcome': 'confirmed'}
    runtime, run, script_operation, _payload = observation.managed_roundtrip_operation(
        services, task, arguments=args, raw=raw, ok=True)
    unstarted = _claim_unstarted(runtime, run, 'scripts')
    _settle_attempt(runtime, run)
    tasks = services.host.conversation_store.session_tasks
    tasks.advance(task.task_id, SessionTaskUpdate(status='accepted'))
    tasks.advance(task.task_id, SessionTaskUpdate(status='done'))
    # 消费端初态由真实V13Store保存，不注入观察reader或结果字典。
    bound = {**admitted, 'execution_id': 'r3-approved', 'task_id': task.task_id, 'requires_approval': True,
        'state': 'accepted', 'ok': None, 'output': None, 'error': None, 'started_at': None, 'finished_at': None,
        'tool_args_hashes': hashes}
    v13_seed(services.owner_home, 'script_runs', {'r3-approved': bound})
    status, body = observation.background_call(background_http, 'scripts.runs.get', {'execution_id': 'r3-approved'})
    assert status == 200, body
    result = body['data']['run']
    assert result['state'] == 'completed' and result['ok'] is True, result
    assert result['output'] == actual['output'] and result['task_id'] == task.task_id
    status, listed = observation.background_call(background_http, 'tool_calls.list',
        {'filter': {'task_id': task.task_id}})
    assert status == 200 and listed['data']['coverage'] == 'complete', listed
    rows = {item['operation_id']: item for item in listed['data']['items']}
    assert set(rows) == {script_operation, unstarted}
    assert rows[unstarted]['executed'] is False and rows[unstarted]['effect_outcome'] == 'not_started'
    assert rows[unstarted]['result_ref'] == unstarted and rows[unstarted]['duration_ms'] is None
    assert rows[script_operation]['executed'] is True and rows[script_operation]['status'] == 'SUCCEEDED'
    actor = BusinessActor(services.owner_id, 'r3-reader', services.observation, lambda action, facts: True)
    gc.collect()
    before = _fd_count()
    page = services.observation.query('tool_calls.list', {'filter': {'task_id': task.task_id}}, actor=actor)
    gc.collect()
    assert _fd_count() - before <= 0
    assert {item['operation_id'] for item in page['items']} == {script_operation, unstarted}
    status, again = observation.background_call(background_http, 'scripts.runs.get', {'execution_id': 'r3-approved'})
    assert status == 200 and again['data']['run'] == result, again


# LLM: 识别器只认原未启动回执的准确形状；其它行即便貌似未启动也仍走原严格校验并失败关闭。
# 函数用途: 对另一条行写入损坏结果，验证同页整体unknown、该行点读503，合法未启动行点读不受影响。
@pytest.mark.parametrize('status,change', [
    ('CANCELLED', {'tool': 'other_tool'}),
    ('CANCELLED', {'handler_executed': True}),
    ('CANCELLED', {'ok': 'false'}),
    ('CANCELLED', {'effect_outcome': 'committed'}),
    ('CANCELLED', {'schema_version': 'other.v1'}),
    ('SUCCEEDED', {}),
])
def test_other_malformed_rows_still_raise_invalid_native_tool_result(background_http, status, change):
    services, task = observation.observed_task(background_http)
    runtime, run = seed_observed_runtime(services, task)
    kept = _claim_unstarted(runtime, run, 'read_file')
    damaged = _claim_unstarted(runtime, run, 'write_file')
    _settle_attempt(runtime, run)
    with runtime._runtime_connection() as conn:
        outcome = json.loads(conn.execute('SELECT outcome_json FROM tool_operations WHERE operation_id=?',
            (damaged,)).fetchone()[0])
        outcome['result'].update(change)
        conn.execute('UPDATE tool_operations SET status=?, outcome_json=? WHERE operation_id=?',
            (status, json.dumps(outcome), damaged))
        conn.commit()
    readers = observation.wire(services, runtime)
    gc.collect()
    before = _fd_count()
    with pytest.raises(ValueError, match='invalid native tool result'):
        readers['tool_calls']({})
    with pytest.raises(ValueError, match='invalid native tool result'):
        readers['tool_calls']({'operation_id': damaged})
    [record] = readers['tool_calls']({'operation_id': kept}).records
    gc.collect()
    assert _fd_count() - before <= 0
    assert record.value['executed'] is False and record.value['effect_outcome'] == 'not_started'
    assert record.value['result_ref'] == kept and record.value['duration_ms'] is None
    status_code, body = observation.background_call(background_http, 'tool_calls.list')
    assert status_code == 200 and body['data']['coverage'] == 'unknown' and body['data']['items'] == [], body
    status_code, body = observation.background_call(background_http, 'tool_calls.get', {'operation_id': damaged})
    assert status_code == 503 and body['error']['code'] == 'HOST_NOT_READY', body
