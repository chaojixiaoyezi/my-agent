# LLM: 仅本次临时HTTP、原C/P3与真实W控制泵；业务不替换dispatcher，供应商只用Echo，禁止真实网络模型和服务。
# 模块用途: 验证基础owner隔离时冷C仍把健康owner控制沿唯一回执交给原Worker执行。
import http.client
import json
import os
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass

import pytest

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.control_plane import GatewayControlPlane
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.io import write_gateway_request
from agent_py_agent.agent.gateway_parts.multiprocess_admission import (
    CoordinatorAdmissionScope,
    PersistentAdmission,
    session_scope_for_request,
)
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
from agent_py_agent.agent.gateway_parts.multiprocess_route import owner_digest
from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import (
    prepare_coordinator_takeover,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.request_worker import AdmissionLimits
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity


# LLM: 载体只保留本fixture创建的实际对象，不实现Agent/权限接口或代替业务执行。
# 类用途: 让HTTP和后续Worker测试共享同一临时根与真实控制设施。
@dataclass
class ForwardCase:
    agent: object
    server: object
    controller: object
    owner: object
    seeded: object
    thread: object
    job: dict
    worker_config: object
    constructions: list
    task: object = None
    claim: object = None


# LLM: 先由原P3公开入口留下未结主owner预留，新C自然隔离主owner；start_business原门不调用factory，远端owner保持健康。
# 函数用途: 创建真实冷C的临时loopback，结束时关闭本测试监听器，不启动外部服务。
@pytest.fixture
def cold(tmp_path, monkeypatch, request):
    from agent_py_agent.agent.owner_scoped_pool import _config_with_owner
    from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest
    home, workspace = (tmp_path/'home').resolve(), (tmp_path/'workspace').resolve()
    monkeypatch.setenv('MY_AGENT_HOME', str(home))
    config = AgentConfig(my_agent_home=str(home), model_backend='echo',
                         gateway_per_user_owner_scoping=True, enable_plugins=False, enable_subagents=True)
    worker_config = deepcopy(config)
    agent = SimpleAgent(config, workspace)
    parameter = getattr(request, 'param', 'local')
    provider, stop_task = parameter if isinstance(parameter, tuple) else (parameter, False)
    owner = OwnerIdentity.provider_user(provider, 'alice')
    seeded = SimpleAgent(_config_with_owner(config, owner), workspace)
    thread = seeded.conversation_store.threads.get_or_create({'canonical_user_id': 'alice', 'channel': provider,
        'channel_conversation_id': 'control-session', 'channel_user_id': 'alice',
        'owner_id': seeded.home_paths.owner_id, 'owner_home': str(seeded.home_paths.owner_home_dir)})
    creator = {'local': True} if provider == 'local' else {'channel': provider, 'actor_id': 'alice'}
    job = seeded.scheduler_repository.create_job(SchedulerJobCreateRequest(name='原入口创建的监控', prompt='已有材料',
        thread_id=thread.thread_id, schedule={'kind': 'every', 'every_seconds': 600, 'timezone': 'UTC', 'anchor_at': 1000},
        creator=creator, now=1000))[0]
    task = claim = None
    if stop_task:
        task = seeded.conversation_store.tasks.bind({'thread_id': thread.thread_id, 'task_id': 'task-forward-stop',
            'goal': '等待本次停止控制', 'status': 'active'})
        claim = seeded.conversation_store.claims.acquire({'thread_id': thread.thread_id, 'task_id': task.task_id,
            'reason': 'thread_goal_continue'})
        assert claim is not None
    paths = gateway_paths_from_root(tmp_path/'gateway')
    payload = {'id': 'prior-main-request', 'kind': 'ask', 'goal': '既有主用户请求', 'user_id': 'local-agent', 'metadata': {},
               'conversation': {'channel': 'local', 'channel_conversation_id': 'main-session', 'channel_user_id': 'local-agent'}}
    with coordinator_guard(paths.root, 2):
        prior = prepare_coordinator_takeover(paths, 2, 'prior-C')
        prior_control = GatewayControlPlane(config, home, paths, 2, prior.c_run_id, agent_root=workspace)
        payload = prior_control.prepare_input(payload)
        gate = PersistentAdmission(paths, agent, CoordinatorAdmissionScope(2, 'prior-C'))
        path = write_gateway_request(paths, payload)
        decision = gate.admit(path, (OwnerIdentity.local_main(), session_scope_for_request(payload)), AdmissionLimits(8, 8))
        assert decision['status'] == 'admitted', decision
    paths = gateway_paths_from_root(tmp_path/'gateway')
    with coordinator_guard(paths.root, 2):
        controller = prepare_coordinator_takeover(paths, 2, 'forward-C')
        assert controller.admission_rejection(owner_digest(OwnerIdentity.local_main())) is not None
        assert controller.admission_rejection(owner_digest(owner)) is None
        calls = []
        control = GatewayControlPlane(config, home, paths, 2, controller.c_run_id,
            agent_root=workspace, workspace_roots=(workspace,), agent_factory=lambda: calls.append(True))
        assert control.start_business(control.agent_factory) is None and calls == []
        runtime = GatewayDrainRuntime(control)
        server = GatewayHTTPServer(0, paths, params=GatewayHTTPServerParams(control_plane=control, drain_runtime=runtime))
        constructions, constructor = [], SimpleAgent.__init__

        def observe_constructor(actual, *args, **kwargs):
            constructions.append(actual)
            constructor(actual, *args, **kwargs)

        monkeypatch.setattr(SimpleAgent, '__init__', observe_constructor)
        server.start()
        try:
            assert server.agent is None
            yield ForwardCase(agent, server, controller, owner, seeded, thread, job, worker_config, constructions, task, claim)
        finally:
            server.stop()


# LLM: 凭据来自本fixture原server，命令只通过真实HTTP投递，不直接调用控制业务函数。
# 函数用途: 提交一条带稳定消息编号的普通控制，并返回原响应。
def post(case, command='/status', *, message='control-one', metadata=None, actor='alice', credential=True):
    server = case.server
    constructions = len(case.constructions)
    body = {'command': command, 'conversation_id': 'control-session',
            'metadata': {'message_id': message, **(metadata or {})}}
    headers = {'Content-Type': 'application/json', 'X-Channel': case.owner.provider, 'X-User-Id': actor}
    if credential:
        headers['X-Gateway-Token'] = server._local_client_credential
    connection = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=3)
    try:
        connection.request('POST', '/control', json.dumps(body), headers)
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        assert len(case.constructions) == constructions, 'C运输不得物化业务Agent'
        return result
    finally:
        connection.close()


def test_naturally_cold_main_still_prepares_healthy_owner_control(cold):
    status, receipt = post(cold)
    assert status == 202 and receipt['control_state'] == 'prepared', (status, receipt)
    assert receipt['operation_id'].startswith('gwctl-msg-')
    assert cold.server.agent is None


# LLM: 原W登记用本测试活进程完整出生；mode只在同步Worker片设置，原pool/P5/get/pin及领域均真对象，不替换dispatcher。
# 函数用途: 创建既有Worker控制泵，关闭自有线程后恢复C的进程模式。
@contextmanager
def worker(case, monkeypatch):
    from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
    from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap import freeze_worker_bootstrap
    from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap_ready import (
        registered_worker_heartbeat,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_control_worker import WorkerControlPump
    from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition, owner_worker
    from agent_py_agent.agent.gateway_parts.multiprocess_worker_agent import create_worker_pool

    paths, root = case.server.paths, case.agent.home_paths.root
    launch = mode.WorkerLaunch(WorkerPartition(owner_worker(case.owner, 2), 2, case.controller.c_run_id),
        paths.root, root, os.getppid(), 'forward-worker', workspace_roots=(case.agent.root,))
    with monkeypatch.context() as scoped:
        scoped.setattr(mode, '_WORKER_PARTITION', launch.partition)
        frozen = freeze_worker_bootstrap(launch)
        heartbeat = registered_worker_heartbeat(launch, launch.instance_id,
            {'ready': True, 'gateway_ports': [case.server.server.server_address[1]]}, time.time())
        registry = {'schema_version': 'multiprocess_worker_registry.v1', 'c_run_id': launch.partition.c_run_id,
            'worker_count': 2, 'process_identity': heartbeat['process_identity'],
            'http_port': case.server.server.server_address[1], 'workers': [{
                'worker': launch.partition.index, 'instance_id': launch.instance_id,
                'process_identity': heartbeat['process_identity'],
                'executor_ledger_ref': f'workers/executors/w{launch.partition.index}'}]}
        write_json_file_atomic(paths.root/'workers/registry.json', registry)
        write_json_file_atomic(paths.root/'workers'/f'w{launch.partition.index}.json', heartbeat)
        pool = create_worker_pool(frozen, case.worker_config, case.agent.root)
        actual = pool.get(case.owner)
        assert type(actual) is SimpleAgent and actual is not case.seeded
        assert actual.conversation_store.storage.root == case.seeded.conversation_store.storage.root
        if case.task is not None:
            from agent_py_agent.agent.gateway_parts.execution_target import (
                read_gateway_execution_target,
            )
            current = read_gateway_execution_target(paths, actual.conversation_store, case.thread.thread_id)
            assert current is not None and current.task_id == case.task.task_id
        pump = WorkerControlPump(frozen, pool)
        try:
            yield actual, pump
        finally:
            pump.close()


# LLM: 只轮询当前原回执，不造终态或手动调用业务；泵用原独立控制/stop槽消费。
# 函数用途: 等本测试Worker完成其已认领的原控制。
def consume(case, pump, operation_id):
    from agent_py_agent.agent.gateway_parts.control_operation_service import (
        read_gateway_control_operation,
    )
    deadline = time.monotonic()+5
    while time.monotonic() < deadline:
        pump.poll()
        receipt = read_gateway_control_operation(case.server.paths, operation_id, read_only=True)
        if receipt.state in {'completed', 'terminal_unknown'}:
            return receipt
        time.sleep(.01)
    pytest.fail('原Worker控制回执未在测试时限内收口')


@pytest.mark.parametrize('cold', ['local', 'feishu'], indirect=True)
def test_actual_worker_status_and_same_id_use_original_agent_and_receipt(cold, monkeypatch):
    status, queued = post(cold)
    assert status == 202
    with worker(cold, monkeypatch) as (actual, pump):
        receipt = consume(cold, pump, queued['operation_id'])
        assert receipt.state == 'completed' and receipt.result['ok'], receipt.to_dict()
        assert actual.home_paths.owner_id == cold.seeded.home_paths.owner_id
        assert receipt.dispatch['executor']['pid'] == os.getpid()
        path = cold.server.paths.root/'control_operations'/f'{receipt.operation_id}.json'
        before = path.read_bytes()
    status, replay = post(cold)
    assert status == 200 and replay['operation_id'] == receipt.operation_id
    assert path.read_bytes() == before and cold.server.agent is None


@pytest.mark.parametrize('cold', ['feishu'], indirect=True)
@pytest.mark.parametrize('operation', ['list', 'history', 'pause'])
def test_actual_worker_schedule_uses_original_repository(cold, monkeypatch, operation):
    command = {'list': '/schedule', 'history': '/schedule history '+cold.job['alias'],
               'pause': '/schedule pause '+cold.job['alias']}[operation]
    status, queued = post(cold, command)
    assert status == 202
    with worker(cold, monkeypatch) as (actual, pump):
        receipt = consume(cold, pump, queued['operation_id'])
        assert receipt.state == 'completed' and receipt.result['ok'], receipt.to_dict()
        job = actual.scheduler_repository.get_job(cold.job['job_id'])
        assert job['status'] == ('paused' if operation == 'pause' else 'active')
        assert job['version'] == cold.job['version'] + (operation == 'pause')
        assert job['creator'] == cold.job['creator']
        path = cold.server.paths.root/'control_operations'/f'{receipt.operation_id}.json'
        before = path.read_bytes()
    assert post(cold, command)[0] == 200 and path.read_bytes() == before


@pytest.mark.parametrize('cold', ['feishu'], indirect=True)
@pytest.mark.parametrize('command', ['/delegate status', '/delegate enable', '/delegate disable'])
def test_actual_worker_delegate_uses_original_preference(cold, monkeypatch, command):
    from agent_py_agent.agent.settings.im_task_preferences import delegation_preference
    status, queued = post(cold, command)
    assert status == 202
    with worker(cold, monkeypatch) as (actual, pump):
        receipt = consume(cold, pump, queued['operation_id'])
        assert receipt.state == 'completed' and receipt.result['ok'], receipt.to_dict()
        preference = delegation_preference(actual, 'feishu', 'alice')
        assert preference['enabled'] == (command == '/delegate enable')
        assert not delegation_preference(actual, 'feishu', 'bob')['enabled']
    assert post(cold, command)[0] == 200


@pytest.mark.parametrize('cold', [('local', True)], indirect=True)
@pytest.mark.parametrize('explicit', [True, False], ids=['client-target', 'legacy-target'])
def test_actual_worker_stop_consumes_exact_real_task_on_stop_slot(cold, monkeypatch, explicit):
    from agent_py_agent.agent.gateway_parts.execution_target import read_gateway_execution_target
    target = read_gateway_execution_target(cold.server.paths, cold.seeded.conversation_store, cold.thread.thread_id,
        blocking=False, deadline=time.monotonic()+5)
    assert target is not None and target.task_id == cold.task.task_id and target.claim_id == cold.claim['claim_id']
    metadata = {'expected_turn_id': target.turn_id, 'expected_execution': target.to_dict()} if explicit else {}
    status, queued = post(cold, '/stop', metadata=metadata)
    assert status == 202, queued
    with worker(cold, monkeypatch) as (actual, pump):
        receipt = consume(cold, pump, queued['operation_id'])
        assert receipt.state == 'completed' and receipt.result['ok'], receipt.to_dict()
        assert receipt.dispatch['execution'] == target.to_dict()
        assert pump._drain_resources[1]._born_threads and not pump._drain_resources[0]._born_threads
        assert actual.conversation_store.tasks.load(cold.task.task_id).status == 'interrupted'
        assert receipt.result['request_id'] == target.turn_id
    before = (cold.server.paths.root/'control_operations'/f'{receipt.operation_id}.json').read_bytes()
    assert post(cold, '/stop', metadata=metadata)[0] == 200
    assert (cold.server.paths.root/'control_operations'/f'{receipt.operation_id}.json').read_bytes() == before


# LLM: 与原HTTP scope同字段只计算原identity；不构造运输source或冻结target，不取得执行权。
# 函数用途: 定位本次合成消息的原回执路径，供普通锁争用测试持有原锁。
def operation_path(case, message):
    from agent_py_agent.agent.gateway_parts.control_operation_service import (
        gateway_control_operation_identity,
        gateway_control_operation_path,
    )
    from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
    scope = GatewayControlScope('alice', case.owner.provider, 'control-session',
        metadata={'message_id': message}, resolved_owner=case.owner)
    identity, _digest = gateway_control_operation_identity(scope, '/status')
    return gateway_control_operation_path(case.server.paths, identity)


@pytest.mark.parametrize('lock_kind', ['operation', 'json'])
def test_natural_busy_never_waits_inside_root_or_creates_half_receipt(cold, lock_kind):
    from agent_py_agent.agent.common.json_io import locked_json_path
    from agent_py_agent.agent.gateway_parts.control_operation_service import (
        gateway_control_operation_transition,
    )
    path = operation_path(cold, 'busy-control')
    lock = (gateway_control_operation_transition(cold.server.paths, path.stem)
            if lock_kind == 'operation' else locked_json_path(path))
    with lock:
        started = time.monotonic()
        status, result = post(cold, message='busy-control')
        assert status == 503 and time.monotonic()-started < 1, result
        assert result['error_code'] == ('GLOBAL_WRITE_REJECTED' if lock_kind == 'operation' else 'BlockingIOError')
        if lock_kind == 'operation':
            assert result['reason'] == 'control_operation_busy'
        assert not path.exists()
        assert cold.server.drain_runtime._gate.acquire(blocking=False)
        cold.server.drain_runtime._gate.release()
        assert cold.controller._mutex.acquire(blocking=False)
        cold.controller._mutex.release()
    assert post(cold, message='busy-control')[0] == 202


def test_prepared_same_id_is_byte_identical_even_while_operation_lock_busy(cold):
    from agent_py_agent.agent.gateway_parts.control_operation_service import (
        gateway_control_operation_transition,
    )
    status, receipt = post(cold)
    path = cold.server.paths.root/'control_operations'/f"{receipt['operation_id']}.json"
    before = path.read_bytes()
    with gateway_control_operation_transition(cold.server.paths, receipt['operation_id']):
        assert post(cold) == (status, receipt)
        assert path.read_bytes() == before


def test_same_id_different_command_keeps_original_receipt(cold):
    status, receipt = post(cold)
    assert status == 202
    path = operation_path(cold, 'control-one')
    before = path.read_bytes()
    status, conflict = post(cold, '/schedule')
    assert status == 409 and conflict['error_code'] == 'IDEMPOTENCY_CONFLICT'
    assert path.read_bytes() == before and json.loads(before)['operation_id'] == receipt['operation_id']


def test_expired_resource_budget_does_not_publish_or_retain_pin(cold, monkeypatch):
    from agent_py_agent.agent.gateway_parts import coordinator_control_transport as transport
    monkeypatch.setattr(transport, 'input_resource_deadline', lambda: time.monotonic()-1)
    status, result = post(cold)
    assert status == 503 and result['error_code'] == 'BlockingIOError'
    assert not operation_path(cold, 'control-one').exists() and not cold.controller._lease._commit_pins


def test_legacy_foreground_read_uses_original_try_lock_and_same_deadline(cold):
    from agent_py_agent.agent.gateway_parts.io import _locked_json_path, write_json_file_atomic
    store, paths = cold.seeded.conversation_store, cold.server.paths
    request_id = 'ordinary-foreground'
    claim = store.claims.acquire({'thread_id': cold.thread.thread_id, 'task_id': 'gateway:'+request_id,
        'reason': 'gateway_foreground_turn'})
    assert claim is not None
    path = paths.processing/f'{request_id}.json'
    write_json_file_atomic(path, {'id': request_id, 'status': 'processing', 'turn_phase': 'open',
        'conversation_claim': {'schema_version': 'gateway_conversation_claim.v1',
            'request_id': request_id, 'thread_id': cold.thread.thread_id, 'task_id': 'gateway:'+request_id}})
    with _locked_json_path(path):
        started = time.monotonic()
        status, busy = post(cold, '/stop')
        assert status == 503 and busy['error_code'] == 'BlockingIOError' and time.monotonic()-started < 1
        assert not list((paths.root/'control_operations').glob('*.json'))
    status, receipt = post(cold, '/stop')
    assert status == 202 and receipt['control_state'] == 'prepared'
    saved = json.loads(next((paths.root/'control_operations').glob('*.json')).read_text())
    assert saved['dispatch']['execution']['claim_id'] == claim['claim_id']
    assert saved['dispatch']['execution']['thread_id'] == cold.thread.thread_id


@pytest.mark.parametrize('kind', ['global', 'context', 'outcome', 'bounded', 'tool'])
@pytest.mark.parametrize('published', [False, True], ids=['before-publish', 'after-publish'])
def test_actual_typed_writer_error_keeps_same_object_without_result(cold, monkeypatch, kind, published):
    from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError
    from agent_py_agent.agent.common import json_io
    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionContextUnavailable,
        RequestExecutionOutcomeUncertainError,
    )
    from agent_py_agent.agent.common.tool_write_context import ToolWriteContextUnavailable
    from agent_py_agent.agent.gateway_parts import coordinator_control_transport as transport
    from agent_py_agent.agent.gateway_parts.multiprocess_global_write import GlobalWriteError
    errors = {'global': GlobalWriteError('control_source_closed', 'control_transport'),
        'context': RequestExecutionContextUnavailable('synthetic'), 'tool': ToolWriteContextUnavailable('synthetic'),
        'bounded': BoundedCallExitUnconfirmedError('synthetic-call', 'worker_exit_unconfirmed'),
        'outcome': RequestExecutionOutcomeUncertainError({'id': 'request', 'execution_attempt_id': 'attempt',
            'lease_epoch': 1, 'lease_owner': 'worker'}, 'synthetic')}
    error, seen, writes = errors[kind], [], []
    original = transport.execute_coordinator_control
    writer = json_io.write_private_json_file_atomic_no_newline_unlocked

    def observe(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except BaseException as actual:
            seen.append(actual)
            raise

    def fail(*args, **kwargs):
        writes.append(True)
        if published:
            writer(*args, **kwargs)
        raise error

    monkeypatch.setattr(transport, 'execute_coordinator_control', observe)
    monkeypatch.setattr(json_io, 'write_private_json_file_atomic_no_newline_unlocked', fail)
    status, result = post(cold)
    assert status == 503 and result['error_code'] == error.error_code
    assert len(seen) == 1 and seen[0] is error
    receipts = list((cold.server.paths.root/'control_operations').glob('*.json'))
    assert len(receipts) == int(published)
    if published:
        before = receipts[0].read_bytes()
        stored = json.loads(before)
        assert stored['state'] == 'prepared' and stored['dispatch'] and 'result' not in stored and 'error' not in stored
        assert post(cold)[0] == 202
        assert receipts[0].read_bytes() == before and writes == [True]
    assert not cold.controller._lease._commit_pins
