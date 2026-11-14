"""默认装配回归；只替换工具业务体及最底层模型，不给 Engine 补端口。"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Event, Lock
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.host_turn_scope import host_turn_scope
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.models import (
    ApprovalPolicy,
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ToolHandlerOutcome,
    ToolModelSpec,
    ToolRuntimePolicy,
)
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore


@pytest.fixture
def host(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    monkeypatch.setenv('MY_AGENT_HOME', str(home))
    agent = SimpleAgent(AgentConfig(model_backend='echo', enable_tools=True, business_spaces_enabled=True,
        memory_path='memory.jsonl', my_agent_home=str(home)), tmp_path / 'workspace')
    params = RunParams(run_id='host-run', request_id='host-request',
        attempt_id='host-attempt', allowed_tools=['read_file', 'workflows'], save=False)
    with host_turn_scope(agent, '', params, reason='workflow_test', tool_rounds=1) as turn:
        yield agent
        turn.runtime_status = 'ok'


def flow(name, steps, *, dag=False):
    return {'descriptor': {'id': name, 'kind': 'workflow', 'name': name, 'access_targets': [],
        'data_sources': [], 'credential_refs': [], 'io_contract': {},
        'metadata': {'execution_mode': 'dag'} if dag else {}}, 'steps': steps}


def local_run(host, definition, *, key='native-run-0000001', inputs=None):
    tool = host.tools.tools['workflows']
    created = tool.execute({'action': 'workflows.create', 'definition': definition,
        'idempotency_key': 'create-' + definition['descriptor']['id']})
    assert created.ok, created
    result = tool.execute({'action': 'workflows.run', 'workflow_ref': definition['descriptor']['id'],
        'idempotency_key': key, 'inputs': inputs or {}})
    [record] = WorkflowRunStore(host.home_paths.owner_home_dir).list_runs(limit=10)
    return result, record


def test_default_tool_mapping_and_durable_replay(host):
    path = host.home_paths.owner_home_dir / 'input.txt'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('native input', encoding='utf-8')
    definition = flow('native-read', [{'step_id': 'read', 'mode': 'tool_call', 'name': 'read',
        'on_error': 'fail', 'tool_spec': {'tool_name': 'read_file',
            'arguments_map': {'path': {'step_id': '$inputs', 'key': 'path'}}}}])
    result, record = local_run(host, definition, inputs={'path': str(path)})
    assert record.status == 'succeeded', record.to_dict()
    receipt = record.steps[0].escalation_result['tool_receipt']
    assert receipt['status'] == 'succeeded'
    # 原read_file是只读；原ToolResult.operation为空，不把请求key当持久操作。
    assert receipt['operation_id'] == '' and record.steps[0].output_snapshot['operation'] is None
    assert record.steps[0].effect_id
    assert 'native input' in json.dumps(record.steps[0].output_snapshot)
    again = host.tools.tools['workflows'].execute({'action': 'workflows.run', 'workflow_ref': 'native-read',
        'idempotency_key': 'native-run-0000001', 'inputs': {'path': str(path)}})
    assert result.ok and again.ok
    assert WorkflowRunStore(host.home_paths.owner_home_dir).load_run(record.workflow_run_id).revision == record.revision


@pytest.mark.parametrize('dag', [False, True])
def test_original_analysis_scope_single_and_dag(host, dag):
    calls = []
    barrier = Barrier(2) if dag else None

    def generate_structured(prompt, response_schema, *, system_prompt=None, temperature=None):
        material = json.loads(prompt)
        sid = material['step']['step_id']
        calls.append((sid, host._current_run_params))
        if barrier is not None and sid in ('left', 'right'):
            barrier.wait(timeout=5)
        return type('Response', (), {'text': json.dumps({'value': sid}), 'runtime_status': 'ok'})()

    host.backend.generate_structured = generate_structured
    steps = [{'step_id': 'left', 'name': 'left', 'mode': 'llm_skill_analysis', 'on_error': 'fail'}]
    if dag:
        steps += [{'step_id': 'right', 'name': 'right', 'mode': 'llm_skill_analysis', 'on_error': 'fail'},
            {'step_id': 'join', 'name': 'join', 'mode': 'llm_skill_analysis', 'on_error': 'fail',
             'depends_on': ['left', 'right'], 'inputs_map': {
                 'left': {'step_id': 'left', 'key': 'value'}, 'right': {'step_id': 'right', 'key': 'value'}}}]
    _, record = local_run(host, flow('native-analysis', steps, dag=dag))
    assert record.status == 'succeeded', record.to_dict()
    assert {sid for sid, _ in calls} == {s['step_id'] for s in steps}
    assert all(params is host._current_run_params for _, params in calls)


class CounterTool(BaseTool):
    model_spec = ToolModelSpec('native_counter', '离线计数器', {'type': 'object', 'properties': {
        'value': {'type': 'integer'}}, 'required': ['value'], 'additionalProperties': False})
    runtime_policy = ToolRuntimePolicy(EffectResolverPolicy(default_effect='read_only'))

    def __init__(self):
        self.calls = []

    def execute(self, params):
        self.calls.append(params)
        if params['value'] == -1:
            raise RuntimeError('offline counter failure')
        return ToolHandlerOutcome('native_counter', True, json.dumps(params))


@pytest.mark.parametrize('case,code,count', [
    ('schema', 'TOOL_PARAMETER_TYPE_INVALID', 0), ('permission', 'TOOL_NOT_ALLOWED', 0),
    # 原ToolExecutor对retryable的只读handler失败本来重试一次；不改变该策略。
    ('failure', 'TOOL_ERROR', 2), ('ok', '', 1)])
def test_registered_fake_tool_schema_permission_error_and_replay(host, case, code, count):
    counter = CounterTool()
    host.tools.register(counter)
    if case != 'permission':
        host._current_run_params.allowed_tools.append('native_counter')
    value = 'invalid' if case == 'schema' else -1 if case == 'failure' else 7
    spec = {'step_id': 'count', 'name': 'count', 'mode': 'tool_call', 'on_error': 'fail',
        'tool_spec': {'tool_name': 'native_counter', 'arguments': {'value': value},
            # 工作流自报schema不能盖掉原Registry的严格schema。
            'parameters_schema': {'type': 'object'}}}
    _, record = local_run(host, flow('native-counter', [spec]))
    step = record.steps[0]
    assert step.status == ('failed' if code else 'succeeded'), step.to_dict()
    assert step.error_code == code, step.to_dict()
    assert len(counter.calls) == count
    receipt = step.escalation_result['tool_receipt']
    assert receipt['error_code'] == code and receipt['status'] == step.status
    assert receipt['operation_id'] == '' and step.effect_id
    if case == 'permission':
        # 未授权工具在调用Executor前拒绝；不存在原工具操作可投影。
        assert receipt['effect_outcome'] == 'not_started'
    else:
        # schema预拒绝及只读Counter成功/失败都没有原ToolResult.operation。
        assert receipt['output_summary']['operation'] is None
        assert receipt['output_summary']['handler_executed'] is (count > 0)
        if case == 'schema':
            assert receipt['effect_outcome'] == 'not_started'
    host.tools.tools['workflows'].execute({'action': 'workflows.run', 'workflow_ref': 'native-counter',
        'idempotency_key': 'native-run-0000001'})
    assert len(counter.calls) == count
    assert WorkflowRunStore(host.home_paths.owner_home_dir).load_run(record.workflow_run_id).revision == record.revision


def test_production_client_native_tool_wire(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.cli.chat_parts.gateway_client import NetworkGatewayObserver
    from agent_py_agent.cli.space_commands import BusinessClient

    home = tmp_path / 'home'
    monkeypatch.setenv('MY_AGENT_HOME', str(home))
    agent = SimpleAgent(AgentConfig(model_backend='echo', enable_tools=True, business_spaces_enabled=True,
        my_agent_home=str(home)), tmp_path / 'workspace')
    path = agent.home_paths.owner_home_dir / 'input.txt'
    path.write_text('native wire input', encoding='utf-8')
    server = GatewayHTTPServer(0, gateway_paths_from_root(agent.home_paths.root / 'gateway'),
        params=GatewayHTTPServerParams(agent=agent))
    server.start()
    try:
        agent.gateway_network_headers = lambda: {'X-Gateway-Token': server._local_client_credential}
        observer = NetworkGatewayObserver(agent, f'http://127.0.0.1:{server.server.server_address[1]}')
        client = BusinessClient(lambda route, payload, timeout: observer.request_json(
            'POST', route, payload, total_deadline_seconds=timeout))
        definition = flow('wire-read', [{'step_id': 'read', 'name': 'read', 'mode': 'tool_call', 'on_error': 'fail',
            'tool_spec': {'tool_name': 'read_file', 'arguments': {'path': str(path)}}}])
        created = client.send(client.prepare('workflows.create', {'definition': definition}))
        assert created['ok'], created
        prepared = client.prepare('workflows.run', {'workflow_ref': 'wire-read'})
        result = client.send(prepared)
        [record] = WorkflowRunStore(agent.home_paths.owner_home_dir).list_runs(limit=10)
        assert record.status == 'succeeded', record.to_dict()
        assert result['ok'], result
        again = client.send(prepared)
        assert again['data']['workflow_run_id'] == record.workflow_run_id
        view = client.call('workflow_runs.get', {'workflow_run_id': record.workflow_run_id})
        assert view['data']['run']['status'] == 'succeeded', view
    finally:
        server.stop()


@pytest.fixture
def wire(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.cli.chat_parts.gateway_client import NetworkGatewayObserver
    from agent_py_agent.cli.space_commands import BusinessClient

    home = tmp_path / 'home'
    monkeypatch.setenv('MY_AGENT_HOME', str(home))
    agent = SimpleAgent(AgentConfig(model_backend='echo', enable_tools=True, business_spaces_enabled=True,
        my_agent_home=str(home)), tmp_path / 'workspace')
    server = GatewayHTTPServer(0, gateway_paths_from_root(agent.home_paths.root / 'gateway'),
        params=GatewayHTTPServerParams(agent=agent))
    server.start()
    agent.gateway_network_headers = lambda: {'X-Gateway-Token': server._local_client_credential}
    observer = NetworkGatewayObserver(agent, f'http://127.0.0.1:{server.server.server_address[1]}')
    client = BusinessClient(lambda route, payload, timeout: observer.request_json(
        'POST', route, payload, total_deadline_seconds=timeout))
    try:
        yield agent, client
    finally:
        server.stop()


def analysis_steps():
    return [{'step_id': sid, 'name': sid, 'mode': 'llm_skill_analysis', 'on_error': 'fail'}
        for sid in ('left', 'right')] + [{'step_id': 'join', 'name': 'join', 'mode': 'llm_skill_analysis',
            'on_error': 'fail', 'depends_on': ['left', 'right'], 'inputs_map': {
                sid: {'step_id': sid, 'key': 'value'} for sid in ('left', 'right')}}]


def test_native_analysis_dag_wire_bounded_inputs_and_replay(wire):
    agent, client = wire
    calls, active, peak = [], [], []
    lock, barrier = Lock(), Barrier(2)

    def generate_structured(prompt, response_schema, *, system_prompt=None, temperature=None):
        material = json.loads(prompt)
        sid = material['step']['step_id']
        with lock:
            active.append(sid)
            peak.append(len(active))
            calls.append((material, agent._current_run_params.run_id))
        if sid != 'join':
            barrier.wait(5)
        with lock:
            active.remove(sid)
        return type('Response', (), {'text': json.dumps({'value': sid})})()

    agent.backend.generate_structured = generate_structured
    definition = flow('wire-analysis', analysis_steps(), dag=True)
    definition['descriptor']['metadata']['max_parallel_steps'] = 2
    assert client.call('workflows.create', {'definition': definition})['ok']
    prepared = client.prepare('workflows.run', {'workflow_ref': 'wire-analysis'})
    result = client.send(prepared)
    [record] = WorkflowRunStore(agent.home_paths.owner_home_dir).list_runs(limit=10)
    assert result['ok'] and record.status == 'succeeded', (result, record.to_dict())
    assert max(peak) == 2 and len(calls) == 3
    join = next(m for m, _ in calls if m['step']['step_id'] == 'join')
    assert join['step_inputs'] == {'left': 'left', 'right': 'right'}
    assert all(run_id == record.workflow_run_id for _, run_id in calls)
    assert client.send(prepared)['data']['workflow_run_id'] == record.workflow_run_id and len(calls) == 3
    assert client.call('workflow_runs.get', {'workflow_run_id': record.workflow_run_id})['data']['run']['status'] == 'succeeded'


def test_dag_failure_drains_started_peer_without_join_or_replay(wire):
    agent, client = wire
    left, right, release = Event(), Event(), Event()
    calls = []

    def generate_structured(prompt, response_schema, *, system_prompt=None, temperature=None):
        sid = json.loads(prompt)['step']['step_id']
        calls.append(sid)
        if sid == 'left':
            assert right.wait(5)
            left.set()
            return type('Response', (), {'text': 'invalid json'})()
        assert sid == 'right'
        right.set()
        assert release.wait(5)
        return type('Response', (), {'text': '{"value":"right"}'})()

    agent.backend.generate_structured = generate_structured
    assert client.call('workflows.create', {'definition': flow('wire-failure', analysis_steps(), dag=True)})['ok']
    prepared = client.prepare('workflows.run', {'workflow_ref': 'wire-failure'})
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(client.send, prepared)
        try:
            assert left.wait(5) and right.is_set()
            assert not future.done()  # 已失败节点不能丢下仍在执行的peer提前释放宿主轮。
        finally:
            release.set()
        future.result(10)
    [record] = WorkflowRunStore(agent.home_paths.owner_home_dir).list_runs(limit=10)
    assert record.status == 'failed'
    assert {s.step_id: s.status for s in record.steps} == {'left': 'failed', 'right': 'succeeded', 'join': 'pending'}
    assert client.send(prepared)['data']['workflow_run_id'] == record.workflow_run_id
    assert sorted(calls) == ['left', 'right']


def test_tool_approval_gate_still_rejects_without_handler(host):
    counter = CounterTool()
    counter.runtime_policy = replace(counter.runtime_policy, approval_policy=ApprovalPolicy(mode='always'))
    host.tools.register(counter)
    host._current_run_params.allowed_tools.append('native_counter')
    _, record = local_run(host, flow('approval', [{'step_id': 'count', 'name': 'count', 'mode': 'tool_call',
        'on_error': 'fail', 'tool_spec': {'tool_name': 'native_counter', 'arguments': {'value': 7}}}]))
    assert record.status == 'failed' and not counter.calls, record.to_dict()
    receipt = record.steps[0].escalation_result['tool_receipt']
    assert receipt['effect_outcome'] == 'not_started'
    assert receipt['output_summary']['handler_executed'] is False
    assert receipt['output_summary']['status'] == 'approval_required'


def test_event_cancel_drain_terminal_and_proof_monotonic(host):
    from agent_py_agent.agent.agent_core.tool_loop.round_execution import (
        _capture_parallel_thread_context,
        run_with_parallel_thread_context,
    )
    from agent_py_agent.agent.common.cancellation import (
        CancellationToken,
        ToolCancelled,
        bind_cancellation_token,
    )

    reached, release = Event(), Event()
    token = CancellationToken()
    entered = Barrier(2)
    def generate_structured(prompt, response_schema, *, system_prompt=None, temperature=None):
        entered.wait(5)
        reached.set()
        assert release.wait(5)
        return type('Response', (), {'text': '{"value":1}'})()
    host.backend.generate_structured = generate_structured
    tool = host.tools.tools['workflows']
    definition = flow('cancelled-flow', analysis_steps(), dag=True)
    assert tool.execute({'action': 'workflows.create', 'definition': definition, 'idempotency_key': 'cancel-create-01'}).ok
    context = _capture_parallel_thread_context(host)
    engine = tool._business_engine(tool.owner_home, run_authorizer=lambda record, phase: None, agent_steps=None)
    # 只用原生产engine和原分析器；Event控制底层LLM返回顺序。
    def run():
        with bind_cancellation_token(token):
            return run_with_parallel_thread_context(host, context, lambda: engine.run_workflow(definition,
                workflow_run_id='cancel-event', owner_id=host.home_paths.owner_id))
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(run)
        try:
            assert reached.wait(5)
            from agent_py_agent.agent.workflows.run_store import _decode_run
            snapshot = _decode_run(engine.store._read('cancel-event', 'run.json'))
            # 原run锁仍持有；相同run不允许第二执行体进入。
            with pytest.raises(RuntimeError, match='工作流正在执行'):
                with engine.store.execution_lock('cancel-event'):
                    raise AssertionError('同run第二执行体取得了锁')
            token.cancel('event-test')
        finally:
            release.set()
        with pytest.raises(ToolCancelled):
            future.result(10)
    paused = engine.store.load_run('cancel-event')
    assert paused.status == 'paused_for_resume'
    engine.cancel_workflow_run('cancel-event', expected_revision=paused.revision, operation_key='cancel-control-01')
    cancelled = engine.store.load_run('cancel-event')
    assert cancelled.status == 'cancelled'
    proof = engine.store._read('cancel-event', 'run.json')
    with pytest.raises((RuntimeError, ValueError)):
        engine.store.save_run(replace(snapshot, revision=cancelled.revision, status='running'))
    assert engine.store._read('cancel-event', 'run.json') == proof
    assert engine.cancel_workflow_run('cancel-event', expected_revision=paused.revision, operation_key='cancel-control-01') == []
    assert engine.store._read('cancel-event', 'run.json') == proof


def test_captured_host_scope_cannot_revive_closed_attempt(host):
    from agent_py_agent.agent.agent_core.self_map_bootstrap import _workflow_runtime_ports
    from agent_py_agent.agent.runtime_db.managed_operation_store import AuthorityContextMissing

    calls = []
    with host_turn_scope(host, '', RunParams(run_id='expired-caller', request_id='expired-request', save=False),
            reason='scope_expiry_test', tool_rounds=0) as turn:
        _, invoke = _workflow_runtime_ports(host)
        invoke(lambda: calls.append('active'))
        turn.runtime_status = 'ok'
    with pytest.raises(AuthorityContextMissing):
        invoke(lambda: calls.append('revived'))
    assert calls == ['active']


def test_queued_native_run_opens_core_scope_only_when_executing(wire, monkeypatch):
    from contextlib import contextmanager

    from agent_py_agent.agent.agent_core import host_turn_scope as scope_module

    agent, client = wire
    scopes, calls = [], []
    reached, release, finished = Event(), Event(), Event()
    real_scope = scope_module.host_turn_scope

    @contextmanager
    def observe_scope(agent, prompt, params, **kwargs):
        scopes.append(params.run_id)
        with real_scope(agent, prompt, params, **kwargs) as turn:
            yield turn
        if len(scopes) == 2:
            finished.set()

    monkeypatch.setattr(scope_module, 'host_turn_scope', observe_scope)
    def generate_structured(prompt, response_schema, *, system_prompt=None, temperature=None):
        material = json.loads(prompt)
        calls.append(agent._current_run_params.run_id)
        if material['run_inputs']['hold']:
            reached.set()
            assert release.wait(5)
        return type('Response', (), {'text': '{"value":1}'})()
    agent.backend.generate_structured = generate_structured
    assert client.call('workflows.create', {'definition': flow('queued-native', analysis_steps()[:1])})['ok']
    tool = agent.tools.tools['workflows']
    def run(key, hold):
        return tool.execute({'action': 'workflows.run', 'workflow_ref': 'queued-native',
            'inputs': {'hold': hold}, 'idempotency_key': key, 'concurrency_policy': 'queue'})
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(run, 'queue-key-01', True)
        try:
            assert reached.wait(5)
            second = run('queue-key-02', False)
            second_data = json.loads(second.output)
            assert second_data['status'] == 'queued' and len(scopes) == 1, second_data
            queued_id = second_data['workflow_run_id']
            assert queued_id not in scopes and queued_id not in calls
        finally:
            release.set()
        assert first.result(10).ok
    assert finished.wait(10)  # 原worker自己推进，不手动执行队列或补端口。
    rows = WorkflowRunStore(tool.owner_home).list_runs(limit=10)
    assert len(rows) == 2 and all(row.status == 'succeeded' for row in rows)
    assert len(scopes) == 2 and len(set(scopes)) == 2 and sorted(scopes) == sorted(calls)


@pytest.mark.parametrize('unknown', [False, True])
def test_original_coordinator_dedupes_step_across_effect_attempts(host, unknown, monkeypatch):
    # 跨effect attempt核对要求显式task；只读取本core轮已经登记的canonical任务，不用run_id冒充。
    with host.tools.operation_store._repo._runtime_connection() as conn:
        row = conn.execute('SELECT tr.task_id FROM agent_runs ar JOIN task_runs tr ON tr.task_run_id=ar.task_run_id '
            'WHERE ar.run_id=?', (host._current_run_params.run_id,)).fetchone()
    assert row is not None and row['task_id']
    host._current_run_params = replace(host._current_run_params, task_id=row['task_id'])
    claim_errors = []
    original_claim = host.tools.operation_store.claim_tool_operation
    def observed_claim(request):
        try:
            return original_claim(request)
        except Exception as exc:
            claim_errors.append((type(exc).__name__, str(exc)))
            raise
    monkeypatch.setattr(host.tools.operation_store, 'claim_tool_operation', observed_claim)
    counter = CounterTool()
    counter.runtime_policy = replace(counter.runtime_policy,
        effect_resolver=EffectResolverPolicy(default_effect='mutating'), approval_policy=ApprovalPolicy(mode='never'),
        idempotency_policy=IdempotencyPolicy(scope='operation'))
    def execute(params):
        counter.calls.append(params)
        return ToolHandlerOutcome('native_counter', not unknown, json.dumps(params),
            error_code='TOOL_ERROR' if unknown else '', effect_outcome='unknown' if unknown else '')
    counter.execute = execute
    host.tools.register(counter)
    host._current_run_params.allowed_tools.append('native_counter')
    _, record = local_run(host, flow('step-idempotency', [{'step_id': 'count', 'name': 'count', 'mode': 'tool_call',
        'on_error': 'fail', 'tool_spec': {'tool_name': 'native_counter', 'arguments': {'value': 7}}}]))
    assert len(counter.calls) == 1
    receipt = record.steps[0].escalation_result['tool_receipt']
    dispatcher = host.tools.tools['workflows']._execution_engine().tool_dispatcher
    repeated_step = replace(record.steps[0], escalation_result={'attempt_id': 'changed-effect-attempt'})
    again = dispatcher.execute_scoped('native_counter', {'value': 7}, owner_id=record.owner_id,
        space_id='', idempotency_key='changed-effect-attempt',
        logical_step_key=f'workflow-step:{record.workflow_run_id}:count', run=record, step=repeated_step)
    assert len(counter.calls) == 1
    assert again['operation_id'] == receipt['operation_id']
    assert again['ok'] is (not unknown), (again, claim_errors)
    if unknown:
        # 本次拒绝重做是not_started，原operation仍为unknown；不能把两层事实相互覆盖。
        assert again['effect_outcome'] == 'not_started' and not again['handler_executed'], again
        assert again['error_code'] == 'TOOL_OPERATION_OUTCOME_UNKNOWN', again
        assert again['operation']['status'] == 'unknown', again


def test_native_pause_resume_reuses_run_with_fresh_core_attempt(wire):
    agent, client = wire
    attempts = []
    def generate_structured(prompt, response_schema, *, system_prompt=None, temperature=None):
        attempts.append((agent._current_run_params.run_id, agent._current_run_params.attempt_id))
        return type('Response', (), {'text': '{"_pause_for_resume":true,"value":1}' if len(attempts) == 1 else '{"value":2}'})()
    agent.backend.generate_structured = generate_structured
    assert client.call('workflows.create', {'definition': flow('native-resume', analysis_steps()[:1])})['ok']
    result = client.call('workflows.run', {'workflow_ref': 'native-resume'})
    run_id = result['data']['workflow_run_id']
    store = WorkflowRunStore(agent.home_paths.owner_home_dir)
    paused = store.load_run(run_id)
    assert paused.status == 'paused_for_resume'
    resumed = client.send(client.prepare('workflow_runs.resume', {'workflow_run_id': run_id},
        expected_revision=paused.revision))
    assert store.load_run(run_id).status == 'succeeded', (resumed, store.load_run(run_id).to_dict())
    assert len(attempts) == 2 and all(run == run_id for run, _ in attempts)
    assert attempts[0][1] != attempts[1][1]


def test_workflow_ids_never_fill_missing_core_identity(wire, monkeypatch):
    agent, _ = wire
    counter, calls = CounterTool(), []
    agent.tools.register(counter)
    original = agent.tools.execute_tool
    def observe(call, **kwargs):
        calls.append(call)
        return original(call, **kwargs)
    monkeypatch.setattr(agent.tools, 'execute_tool', observe)
    dispatcher = agent.tools.tools['workflows']._execution_engine().tool_dispatcher
    result = dispatcher.execute_scoped('native_counter', {'value': 7}, owner_id=agent.home_paths.owner_id,
        space_id='', idempotency_key='effect-attempt', logical_step_key='workflow-step:uncanonical:count',
        run=SimpleNamespace(workflow_run_id='uncanonical'),
        step=SimpleNamespace(escalation_result={'attempt_id': 'uncanonical-effect-attempt'}))
    assert result == {'ok': False, 'error_code': 'TOOL_AUTHORITY_CONTEXT_MISSING',
        'effect_outcome': 'not_started', 'handler_executed': False, 'failure_stage': 'authorization',
        'call_id': 'workflow-step:uncanonical:count', 'tool_name': 'native_counter',
        'error_message': '当前宿主缺少真实run_id/attempt_id；未进入handler、未执行工具。请在正常授权执行轮重新发起，不能用工作流或effect身份代填。'}
    assert not calls and not counter.calls


@pytest.mark.parametrize('replacement', [False, True])
def test_authority_expires_after_admission_keeps_executor_receipt(host, monkeypatch, replacement):
    from agent_py_agent.agent.agent_core.self_map_bootstrap import _WorkflowToolDispatcher
    from agent_py_agent.agent.agent_core.tool_loop.round_execution import (
        _capture_parallel_thread_context,
        run_with_parallel_thread_context,
    )

    counter, registry_calls = CounterTool(), []
    host.tools.register(counter)
    entered, release = Event(), Event()
    original_dispatch = _WorkflowToolDispatcher.execute_scoped
    original_execute = host.tools.execute_tool

    def dispatch(self, *args, **kwargs):
        entered.set()  # 原步骤准入已通过，真实dispatcher/Executor尚未进入。
        assert release.wait(10)
        return original_dispatch(self, *args, **kwargs)

    def execute(call, **kwargs):
        registry_calls.append(call)
        return original_execute(call, **kwargs)

    monkeypatch.setattr(_WorkflowToolDispatcher, 'execute_scoped', dispatch)
    monkeypatch.setattr(host.tools, 'execute_tool', execute)
    definition = flow('expired-after-admission', [{'step_id': 'count', 'name': 'count',
        'mode': 'tool_call', 'on_error': 'fail',
        'tool_spec': {'tool_name': 'native_counter', 'arguments': {'value': 7}}}])
    with ThreadPoolExecutor(1) as pool:
        try:
            with host_turn_scope(host, '', RunParams(run_id='expired-dispatch', request_id='expired-dispatch-request',
                    allowed_tools=['native_counter', 'workflows'], save=False), reason='expiry_receipt', tool_rounds=0) as turn:
                context = _capture_parallel_thread_context(host)
                old_attempt = turn.params.attempt_id
                future = pool.submit(run_with_parallel_thread_context, host, context,
                    lambda: local_run(host, definition))
                assert entered.wait(10)
                turn.runtime_status = 'ok'
            if replacement:
                with host_turn_scope(host, '', RunParams(run_id='expired-dispatch', request_id='replacement-request',
                        save=False), reason='replacement_receipt', tool_rounds=0) as next_turn:
                    assert next_turn.params.attempt_id != old_attempt
                    release.set()
                    _, record = future.result(10)
                    next_turn.runtime_status = 'ok'
            else:
                release.set()
                _, record = future.result(10)
        finally:
            release.set()
    step = record.steps[0]
    receipt = step.escalation_result['tool_receipt']
    assert not counter.calls
    assert step.error_code == 'TOOL_AUTHORITY_CONTEXT_MISSING', step.to_dict()
    assert receipt['effect_outcome'] == 'not_started', receipt
    assert receipt['output_summary']['handler_executed'] is False, receipt
    logical = f'workflow-step:{record.workflow_run_id}:count'
    # 请求身份仍透传给原Executor，但其核权拒绝没有签发canonical operation。
    assert receipt['operation_id'] == '' and receipt['output_summary']['operation'] is None, receipt
    assert len(registry_calls) == 1 and registry_calls[0].operation_id == logical
