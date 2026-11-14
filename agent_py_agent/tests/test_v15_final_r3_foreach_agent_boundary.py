"""P1-03：真实业务往返拒绝异步foreach，不丢原任务或取消责任。"""
from __future__ import annotations

import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.assets.workflow_store import WorkflowDefinition, WorkflowStore
from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
from agent_py_agent.agent.conversation.host_actor import HostChannelActor
from agent_py_agent.agent.conversation.session_tasks import SessionTaskUpdate
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts.business_api import (
    BusinessServices,
    _BusinessBackgroundService,
    bind_business_context,
    execute_business,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.workflows.agent_steps import WorkflowAgentSteps
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.models import StepExecutionRecord, WorkflowRunRecord
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from agent_py_agent.cli.space_commands import BusinessClient, PreparedBusinessCall

REFUSAL = 'WORKFLOW_FOREACH_AGENT_TASK_UNSUPPORTED'
CONFIGURATIONS = [('fail_fast', 1), ('continue', 1), ('fail_fast', 2), ('continue', 2)]


def definition(*, policy='fail_fast', parallel=1, foreach=True):
    step = {'step_id': 'agent', 'name': '原任务', 'mode': 'agent_task', 'on_error': 'fail',
        'config': {'instruction': '核对本次输入'}}
    if foreach:
        step['foreach'] = {'from': 'inputs.rows', 'batch_size': 1, 'max_parallel': parallel,
            'on_batch_error': policy, 'mode': 'map'}
    return {'descriptor': {'id': 'boundary-flow', 'kind': 'workflow', 'name': '批次边界',
        'access_targets': [], 'data_sources': [], 'credential_refs': [], 'io_contract': {}}, 'steps': [step]}


@pytest.fixture
def business(tmp_path):
    """仅运输用本地JSON往返；不替换领域派发、引擎或任务提交。"""
    from agent_py_agent.agent.workflows import engine
    assert Path(engine.__file__).resolve().is_relative_to(Path(__file__).resolve().parents[1])
    home = replace(home_paths(tmp_path / 'home'), owner_id='local/main', owner_provider='local', owner_kind='main')
    conversations = ConversationStore(home.owner_home_dir / 'conversations')
    source = conversations.threads.create(owner_id=home.owner_id, canonical_user_id='boundary-user')
    actor = HostChannelActor(home.owner_id, source.thread_id, 'boundary-request', 'local', 'local', 'human')
    host = SimpleNamespace(home_paths=home, conversation_store=conversations,
        config=AgentConfig(business_spaces_enabled=True, model_backend='echo'))
    services = BusinessServices(host)
    access = {'allowed': True}
    authority = bind_business_context(host, actor=actor, authorize=lambda _a, _f: access['allowed'])
    background = _BusinessBackgroundService(services, authority)
    completions = []

    def completion(task_id, **identity):
        # 宿主完成端口只消费准确canonical任务；等待时绝不伪造完成。
        task = ConversationStore(conversations.storage.root).session_tasks.require(task_id)
        completions.append(task.task_id)
        if task.status != 'done':
            return None
        return {**identity, 'task_id': task.task_id, 'status': 'completed',
            'completion_ref': task.task_id + ':done', 'output': {'values': [7]},
            'run_id': task.task_run_id, 'result_ref': task.result_refs[0]}

    def cancel(task_id, *, actor, idempotency_key):
        return background.cancel_task(task_id, actor=actor, include_descendants=True)

    agents = WorkflowAgentSteps(background, actor_provider=lambda _run: actor,
        completion_reader=completion, cancel_handler=cancel)
    services.workflows.agent_steps = agents
    services.workflows.engine_factory = WorkflowEngine
    requests = []

    def transport(path, request, *, timeout):
        assert path == '/client/business' and timeout > 0
        request = json.loads(json.dumps(request))
        requests.append(request)
        status, response = execute_business(request, authority, services)
        return status, json.loads(json.dumps(response))

    client = BusinessClient(transport)
    space = client.call('spaces.create', {'name': '原任务空间'})
    assert space['ok'], space
    space = space['data']
    context = BusinessExecutionContext(home.owner_id, space['space_id'], None, 'original-parent-task',
        space['revision'], None, (), (), 'business-context.v1')
    services.workflows.context_provider = lambda _actor, _request: context
    return SimpleNamespace(home=home.owner_home_dir, conversations=conversations, client=client,
        services=services, agents=agents, background=background, actor=actor, space=space,
        requests=requests, completions=completions, access=access, scope_actor=services.actor(authority))


def call(case, action, params, *, revision=None, key=None):
    return case.client.send(case.client.prepare(action, params, expected_revision=revision,
        idempotency_key='boundary-regression-' + key.replace('.', '-') if key else None))


def tasks(case):
    # 每次重开canonical store，绝不拿返回字典当作任务计数。
    rows, errors = ConversationStore(case.conversations.storage.root).session_tasks.list_report(limit=100)
    assert not errors
    return rows


def run_store(case):
    return WorkflowRunStore(case.home)


def create(case, value, *, key='create'):
    return call(case, 'workflows.create', {'definition': value,
        'scope': {'scope_mode': 'spaces', 'space_ids': [case.space['space_id']]}}, key=key)


def start(case, *, key='start'):
    return call(case, 'workflows.run', {'workflow_ref': 'boundary-flow',
        'space_id': case.space['space_id'], 'inputs': {'rows': [1, 2]}}, key=key)


def assert_refused(receipt):
    assert receipt['ok'] is False, receipt
    assert receipt['state'] == 'failed' and receipt['http_status'] == 400, receipt
    assert receipt['effect_outcome'] == 'not_started', receipt
    assert receipt['error']['code'] == REFUSAL, receipt
    assert receipt['error']['retryable'] is False


def install_historical(case, value):
    """模拟升级前的真实存储字节；共享codec仍须可读，不经新内容写入口。"""
    valid = WorkflowDefinition.from_any(value)
    valid = replace(valid, descriptor=replace(valid.descriptor, owner_id=case.actor.owner_id))
    with WorkflowStore(case.home)._locked():
        WorkflowStore(case.home)._save(valid)
    return WorkflowStore(case.home).get_workflow(valid.descriptor.id)


@pytest.mark.parametrize('policy,parallel', CONFIGURATIONS)
def test_scope_create_rejects_async_foreach_before_any_commit(business, policy, parallel):
    case = business
    with pytest.raises(ValueError) as caught:
        case.services.workflows.execute('workflows.create', {'definition': definition(policy=policy, parallel=parallel)},
            actor=case.scope_actor, idempotency_key='scope-create')
    assert getattr(caught.value, 'code', None) == REFUSAL
    assert caught.value.details['step_id'] == 'agent'
    assert tasks(case) == []
    assert WorkflowStore(case.home).get_workflow('boundary-flow') is None
    assert run_store(case).list_runs(workflow_id='boundary-flow') == []


def test_wire_create_keeps_frozen_schema_refusal(business):
    # 冻结的W3 wire schema原本不接foreach；WF-07（d2d710c62）按任务要求把既有foreach作为可选字段公开后，
    # agent_task+foreach 仍由原领域准入在写入前拒绝：不改协议来代替领域准入修复，拒绝/未开始/无任务/无定义不变。
    receipt = business.client.send(PreparedBusinessCall(json.dumps({'action': 'workflows.create',
        'params': {'definition': definition()}, 'request_id': 'boundary-wire-schema',
        'idempotency_key': 'boundary-wire-schema-create'})))
    assert receipt['ok'] is False and receipt['error']['code'] == 'WORKFLOW_FOREACH_AGENT_TASK_UNSUPPORTED'
    assert receipt['effect_outcome'] == 'not_started' and tasks(business) == []
    assert WorkflowStore(business.home).get_workflow('boundary-flow') is None


@pytest.mark.parametrize('policy,parallel', CONFIGURATIONS)
def test_old_definition_run_refuses_without_new_run_or_tasks(business, policy, parallel):
    case = business
    saved = install_historical(case, definition(policy=policy, parallel=parallel))
    before = (case.home / 'workflows/boundary-flow/workflow.json').read_bytes()
    receipt = start(case)
    assert_refused(receipt)
    assert tasks(case) == []
    assert run_store(case).list_runs(workflow_id=saved.descriptor.id) == []
    assert (case.home / 'workflows/boundary-flow/workflow.json').read_bytes() == before
    assert call(case, 'workflows.get', {'workflow_ref': saved.descriptor.id})['ok']


def test_content_update_rejected_but_old_definition_control_and_decode_remain(business):
    case = business
    assert create(case, definition(foreach=False))['ok']
    saved = WorkflowStore(case.home).get_workflow('boundary-flow')
    before = (case.home / 'workflows/boundary-flow/workflow.json').read_bytes()
    with pytest.raises(ValueError) as caught:
        case.services.workflows.execute('workflows.update', {'workflow_ref': 'boundary-flow', 'definition': definition()},
            actor=case.scope_actor, expected_revision=saved.descriptor.revision, idempotency_key='bad-update')
    assert getattr(caught.value, 'code', None) == REFUSAL
    assert (case.home / 'workflows/boundary-flow/workflow.json').read_bytes() == before
    old = install_historical(case, definition())
    assert old.steps[0].foreach and old.steps[0].mode == 'agent_task'
    for action in ('workflows.disable', 'workflows.enable'):
        receipt = call(case, action, {'workflow_ref': old.descriptor.id},
            revision=old.descriptor.revision, key=action)
        assert receipt['ok'], receipt
        old = WorkflowStore(case.home).get_workflow(old.descriptor.id)
    assert tasks(case) == []


def test_non_foreach_submits_once_and_resumes_same_canonical_task(business):
    case = business
    assert create(case, definition(foreach=False))['ok']
    receipt = start(case)
    assert receipt['ok'], receipt
    original = run_store(case).load_run(receipt['data']['workflow_run_id'])
    assert original.status == 'waiting_for_agent'
    task, = tasks(case)
    assert original.steps[0].escalation_result['task_id'] == task.task_id
    waiting = call(case, 'workflow_runs.resume', {'workflow_run_id': original.workflow_run_id},
        revision=original.revision, key='still-waiting')
    assert waiting['ok'], waiting
    current = run_store(case).load_run(original.workflow_run_id)
    assert current.status == 'waiting_for_agent' and len(tasks(case)) == 1
    for status in ('accepted', 'done'):
        case.conversations.session_tasks.advance(task.task_id, SessionTaskUpdate(status=status,
            task_run_id='original-agent-run', result_refs=('original-result',)))
    done = call(case, 'workflow_runs.resume', {'workflow_run_id': original.workflow_run_id},
        revision=current.revision, key='complete-original')
    assert done['ok'], done
    finished = run_store(case).load_run(original.workflow_run_id)
    assert finished.status == 'succeeded' and finished.final_output == {'values': [7]}
    assert finished.steps[0].escalation_result['attempt_id'] == original.steps[0].escalation_result['attempt_id']
    assert finished.steps[0].escalation_result['task_id'] == task.task_id
    assert [row.task_id for row in tasks(case)] == [task.task_id]
    assert tasks(case)[0].status == 'done' and case.completions == [task.task_id, task.task_id]


# LLM: 先真实提交一个原任务，再保存升级前可出现的冻结快照；不替换任何派发端口。
# 函数用途: 构造保留准确任务、批次attempt和旧状态的历史运行。
def affected_run(case, *, policy='fail_fast', parallel=1, status='failed', lost_identity=False):
    assert create(case, definition(foreach=False))['ok']
    receipt = start(case)
    assert receipt['ok'], receipt
    store = run_store(case)
    run = store.load_run(receipt['data']['workflow_run_id'])
    task, = tasks(case)
    snapshot = store.load_workflow_snapshot(run.workflow_run_id)
    snapshot['steps'][0]['foreach'] = definition(policy=policy, parallel=parallel)['steps'][0]['foreach']
    step = run.steps[0]
    step.batches = [{'index': 0, 'status': 'failed', 'input_range': [0, 1], 'item_count': 1,
        'attempt_id': step.escalation_result['attempt_id'], 'task_id': task.task_id,
        'submission_key': step.escalation_result['submission_key']}]
    step.batches_summary = {'total': 1, 'completed': 0, 'failed': 1, 'blocked': 0, 'skipped': 0}
    if lost_identity:
        for key in ('task_id', 'submission_key', 'submission_state', 'session_task_dispatch'):
            step.escalation_result.pop(key, None)
        # 历史批worker和父步骤各有自己的attempt；丢失子身份后旧恢复使用的是父attempt。
        step.escalation_result['attempt_id'] = uuid.uuid4().hex
    step.status, step.needs_repair = 'failed', True
    run.status, run.needs_repair = status, True
    store.save_run(run, snapshot, resuming=True)
    return store.load_run(run.workflow_run_id), task


# LLM: 只读此run登记的canonical文件，不以响应对象代替耐久状态，也不扫全Owner历史。
# 函数用途: 在拒绝前后逐字节比较运行、冻结快照及步骤证据。
def run_bytes(case, run):
    folder = run_store(case).run_dir(run.workflow_run_id)
    # canonical冻结定义在run.json内；新建运行不会额外创建独立workflow_snapshot.json。
    return {name: (folder / name).read_bytes() for name in ('run.json', 'steps/agent.json')}


# LLM: 缺任务身份的旧父步骤不能触发原生恢复重提；每次都用真实Client到领域恢复入口。
# 函数用途: 证明四种冻结批配置反复恢复零新增任务、零控制/状态改写。
@pytest.mark.parametrize('policy,parallel', CONFIGURATIONS)
def test_old_snapshot_resume_never_resubmits_or_rewrites(business, policy, parallel, record_property):
    case = business
    original, task = affected_run(case, policy=policy, parallel=parallel, lost_identity=True)
    before = run_bytes(case, original)
    counts, receipts = [len(tasks(case))], []
    for index in range(2):
        current = run_store(case).load_run(original.workflow_run_id)
        receipts.append(call(case, 'workflow_runs.resume', {'workflow_run_id': original.workflow_run_id},
            revision=current.revision, key=f'old-resume-{index}'))
        counts.append(len(tasks(case)))
    record_property('canonical_task_counts', json.dumps(counts))
    assert counts == [1, 1, 1], counts
    for receipt in receipts:
        assert_refused(receipt)
    assert run_bytes(case, original) == before
    assert [row.task_id for row in tasks(case)] == [task.task_id]
    assert call(case, 'workflow_runs.get', {'workflow_run_id': original.workflow_run_id})['ok']
    assert case.completions == []


# LLM: 拒绝不借终态或取消证据取得新执行权，已接受的原任务终态不可倒退。
# 函数用途: 验证运行终态与canonical任务状态、attempt在重复恢复后完整保留。
@pytest.mark.parametrize('status', ['cancelled', 'succeeded', 'failed', 'waiting_for_agent'])
def test_old_snapshot_refusal_preserves_terminal_and_dispatch_evidence(business, status):
    case = business
    original, task = affected_run(case, status=status)
    for task_status in ('accepted', 'done'):
        case.conversations.session_tasks.advance(task.task_id, SessionTaskUpdate(status=task_status,
            task_run_id='late-original-agent-run', result_refs=('original-result',)))
    before = run_bytes(case, original)
    task_before = tasks(case)[0]
    for index in range(2):
        receipt = call(case, 'workflow_runs.resume', {'workflow_run_id': original.workflow_run_id},
            revision=original.revision, key=f'terminal-{index}')
        assert_refused(receipt)
    assert run_bytes(case, original) == before
    assert run_store(case).load_run(original.workflow_run_id).status == status
    assert tasks(case)[0] == task_before
    assert call(case, 'workflow_runs.get', {'workflow_run_id': original.workflow_run_id})['ok']


# LLM: runtime限制不进入取消链；只有原background确认清理后才允许取消收口。
# 函数用途: 验证已有准确任务的旧快照仍能经真实业务取消，且不创建替代任务。
def test_old_snapshot_keeps_cancellation_access(business):
    case = business
    original, task = affected_run(case)
    receipt = call(case, 'workflow_runs.cancel', {'workflow_run_id': original.workflow_run_id},
        revision=original.revision, key='cancel-original')
    assert receipt['ok'], receipt
    current = run_store(case).load_run(original.workflow_run_id)
    assert current.status == 'cancelled'
    assert current.steps[0].escalation_result['task_id'] == task.task_id
    assert current.steps[0].escalation_result['attempt_id'] == original.steps[0].escalation_result['attempt_id']
    assert current.steps[0].batches == original.steps[0].batches
    only, = tasks(case)
    assert only.task_id == task.task_id and only.status == 'cancelled'
    assert current.steps[0].escalation_result['cancellation_receipt']['state'] == 'completed'


# LLM: 直接覆盖内部入口，而非让外层Store检查替代最后的执行边界。
# 函数用途: 验证内部调用也在建批、写attempt及副作用准入前拒绝，DTO原样保留。
@pytest.mark.parametrize('entry', ['_perform', '_foreach_step', '_drive', '_resume_agent'])
def test_internal_execution_boundaries_refuse_before_any_mutation(business, entry):
    case = business
    value = WorkflowDefinition.from_any(definition()).to_dict()
    step = StepExecutionRecord('agent', '原任务', 'agent_task', 'running', 0, 0, 0, False)
    run = WorkflowRunRecord('internal-boundary', 'boundary-flow', case.actor.owner_id,
        'manual', 'running', 0, 0, 0, steps=(step,), inputs={'rows': [1, 2]})
    before = run.to_dict()
    admissions = []
    engine = WorkflowEngine(case.home, agent_steps=case.agents,
        run_authorizer=lambda *args: admissions.append(args))
    with pytest.raises(ValueError) as caught:
        if entry in ('_perform', '_foreach_step'):
            getattr(engine, entry)(step, value['steps'][0], {}, value, run)
        elif entry == '_drive':
            engine._drive(run, value, 0)
        else:
            engine._resume_agent(run, value)
    assert caught.value.error_code == REFUSAL
    assert run.to_dict() == before and admissions == [] and tasks(case) == []
    assert not (case.home / 'data/workflow_runs/internal-boundary/run.json').exists()


# LLM: 外层claim前校验和锁内最终校验各自独立，不能只靠公开wrapper兜底。
# 函数用途: 覆盖原生恢复内部入口及显式并发策略，拒绝不写新claim或control。
@pytest.mark.parametrize('policy', ['internal', 'allow', 'queue', 'skip_if_running'])
def test_resume_guards_precede_claim_and_control_writes(business, policy):
    case = business
    original, task = affected_run(case, lost_identity=True)
    before = run_bytes(case, original)
    engine = case.services.workflows._engine(case.scope_actor)
    with pytest.raises(ValueError) as caught:
        if policy == 'internal':
            engine._resume_workflow_run(original.workflow_run_id, expected_revision=original.revision,
                operation_key='internal-resume')
        else:
            engine.resume_workflow_run(original.workflow_run_id, expected_revision=original.revision,
                operation_key='policy-resume', concurrency_policy=policy)
    assert caught.value.error_code == REFUSAL
    assert run_bytes(case, original) == before
    assert [row.task_id for row in tasks(case)] == [task.task_id]


# LLM: Event固定原run锁内交错；竞争者必须在进入恢复执行体之前被原锁拒绝。
# 函数用途: 证明同run恢复临界区活跃数不超过1，拒绝退出后释放原锁和全部证据。
def test_same_run_resume_is_exclusive_even_at_refusal(business, monkeypatch):
    from agent_py_agent.agent.workflows import engine as module

    case = business
    original, _ = affected_run(case, lost_identity=True)
    before = run_bytes(case, original)
    entered, release = threading.Event(), threading.Event()
    active, maximum = [0], [0]
    lock = threading.Lock()
    validate = module.validate_definition_eligibility

    # LLM: 只控制纯校验的时序，不替换任何派发或存储行为。
    # 函数用途: 将第一个恢复停在持有原执行锁的最后准入门。
    def gate(value):
        with lock:
            active[0] += 1
            maximum[0] = max(maximum[0], active[0])
        try:
            entered.set()
            assert release.wait(10)
            return validate(value)
        finally:
            with lock:
                active[0] -= 1

    monkeypatch.setattr(module, 'validate_definition_eligibility', gate)
    first = case.services.workflows._engine(case.scope_actor)
    second = case.services.workflows._engine(case.scope_actor)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(first._resume_workflow_run, original.workflow_run_id)
        try:
            assert entered.wait(5)
            with pytest.raises(RuntimeError, match='工作流正在执行'):
                second._resume_workflow_run(original.workflow_run_id)
        finally:
            release.set()
        with pytest.raises(ValueError) as caught:
            future.result(10)
    assert caught.value.error_code == REFUSAL and maximum == [1] and active == [0]
    assert run_bytes(case, original) == before and len(tasks(case)) == 1
    with run_store(case).execution_lock(original.workflow_run_id):
        assert run_store(case).load_run(original.workflow_run_id).revision == original.revision


# LLM: 取消在外层纯校验等待时提交；晚到恢复必须保留取消终态和已ACK的清理凭据。
# 函数用途: 通过真实Client交错取消与恢复，禁止旧记录或拒绝路径覆盖已接受控制。
def test_cancellation_proof_survives_late_refused_resume(business, monkeypatch):
    from agent_py_agent.agent.workflows import engine as module

    case = business
    original, task = affected_run(case)
    entered, release = threading.Event(), threading.Event()
    validate = module.validate_definition_eligibility

    def gate(value):
        entered.set()
        assert release.wait(10)
        return validate(value)

    monkeypatch.setattr(module, 'validate_definition_eligibility', gate)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(call, case, 'workflow_runs.resume', {'workflow_run_id': original.workflow_run_id},
            revision=original.revision, key='late-resume')
        try:
            assert entered.wait(5)
            cancelled = call(case, 'workflow_runs.cancel', {'workflow_run_id': original.workflow_run_id},
                revision=original.revision, key='concurrent-cancel')
            assert cancelled['ok'], cancelled
            committed = run_bytes(case, original)
        finally:
            release.set()
        assert_refused(future.result(10))
    assert run_bytes(case, original) == committed
    with pytest.raises(ValueError, match='WORKFLOW_RUN_REVISION_CONFLICT'):
        run_store(case).save_run(original, resuming=True)
    current = run_store(case).load_run(original.workflow_run_id)
    assert current.status == 'cancelled'
    assert current.steps[0].escalation_result['cancellation_receipt']['state'] == 'completed'
    assert current.steps[0].batches == original.steps[0].batches
    only, = tasks(case)
    assert only.task_id == task.task_id and only.status == 'cancelled'


# LLM: Unix读取真实FD表且关闭枚举器；Windows检查当前进程句柄，不把未知平台计为0。
# 函数用途: 为成功、拒绝、取消及异常退出测量真实宿主资源差额。
def resource_count():
    for path in ('/proc/self/fd', '/dev/fd'):
        if Path(path).is_dir():
            with os.scandir(path) as entries:
                return sum(1 for _ in entries)
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes

        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.GetCurrentProcess.restype = wintypes.HANDLE
        api.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        count = wintypes.DWORD()
        assert api.GetProcessHandleCount(api.GetCurrentProcess(), ctypes.byref(count))
        return count.value
    pytest.fail('当前平台无可用的FD或句柄观察入口')


# LLM: 测量真实执行/控制链的资源，不通过关闭产品连接或清理对象来制造零差额。
# 函数用途: 证明本次准入门所有出口fd_delta为0，异常不遗留运行锁。
@pytest.mark.parametrize('outcome', ['success', 'refusal', 'cancellation', 'exception'])
def test_resource_delta_is_zero_at_all_boundary_exits(business, outcome, record_property):
    case = business
    if outcome == 'refusal':
        original, task = affected_run(case, lost_identity=True)
    else:
        assert create(case, definition(foreach=False))['ok']
        receipt = start(case)
        assert receipt['ok'], receipt
        original = run_store(case).load_run(receipt['data']['workflow_run_id'])
        task, = tasks(case)
    if outcome == 'success':
        for status in ('accepted', 'done'):
            case.conversations.session_tasks.advance(task.task_id, SessionTaskUpdate(status=status,
                task_run_id='resource-original-run', result_refs=('original-result',)))
    assert call(case, 'workflow_runs.get', {'workflow_run_id': original.workflow_run_id})['ok']
    engine = case.services.workflows._engine(case.scope_actor)
    before = resource_count()
    if outcome == 'exception':
        error = OSError('simulated admission observation error')

        def authorize(_record, _phase):
            raise error

        engine.run_authorizer = authorize
        with pytest.raises(OSError) as caught:
            engine._resume_workflow_run(original.workflow_run_id)
        assert caught.value is error
    else:
        receipt = call(case, 'workflow_runs.cancel' if outcome == 'cancellation' else 'workflow_runs.resume',
            {'workflow_run_id': original.workflow_run_id}, revision=original.revision, key='resource-' + outcome)
        if outcome == 'refusal':
            assert_refused(receipt)
        else:
            assert receipt['ok'], receipt
            assert run_store(case).load_run(original.workflow_run_id).status == (
                'cancelled' if outcome == 'cancellation' else 'succeeded')
    delta = resource_count() - before
    record_property('fd_delta' if os.name != 'nt' else 'handle_delta', delta)
    assert delta == 0
    assert [row.task_id for row in tasks(case)] == [task.task_id]
    with run_store(case).execution_lock(original.workflow_run_id):
        assert run_store(case).load_run(original.workflow_run_id).workflow_run_id == original.workflow_run_id


# 运行时观察真实scope入口：无效定义不得先发表宿主attempt；有效对照必须能抵达相同入口。
@pytest.mark.parametrize('eligible', [False, True])
def test_execution_eligibility_precedes_host_execution_scope(business, eligible, tmp_path):
    from contextlib import contextmanager

    case = business
    value = WorkflowDefinition.from_any(definition(foreach=not eligible)).to_dict()
    step = StepExecutionRecord('agent', '原任务', 'agent_task', 'running', 0, 0, 0, False)
    run = WorkflowRunRecord('scope-boundary', 'boundary-flow', case.actor.owner_id,
        'manual', 'running', 0, 0, 0, steps=(step,), inputs={'rows': [1]})
    before = run.to_dict()
    marker = tmp_path / 'host-attempt-started'

    @contextmanager
    def execution_scope(engine, record):
        marker.write_text('scope-entered')
        raise RuntimeError('QA_SCOPE_ENTERED')
        yield engine

    engine = WorkflowEngine(case.home, agent_steps=case.agents, execution_scope=execution_scope)
    if eligible:
        with pytest.raises(RuntimeError, match='QA_SCOPE_ENTERED'):
            engine._drive(run, value, 0)
        assert marker.read_text() == 'scope-entered'
    else:
        with pytest.raises(ValueError) as error:
            engine._drive(run, value, 0)
        assert error.value.error_code == REFUSAL
        assert not marker.exists()
    assert run.to_dict() == before and tasks(case) == []


# LLM: 工具与HTTP保留同一结构化资格拒绝码；写前拒绝不能新增执行或丢失原任务责任。
# 函数用途: 实测旧工具save/run/resume均拒绝且不新增执行，不把公开HTTP门当作唯一保护。
@pytest.mark.parametrize('action', ['save', 'run', 'resume'])
def test_native_tool_refuses_without_claiming_new_execution(business, action, record_property):
    from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool

    case = business
    if action == 'resume':
        original, _ = affected_run(case, lost_identity=True)
        params = {'action': action, 'workflow_run_id': original.workflow_run_id}
        before = run_bytes(case, original)
    else:
        if action == 'run':
            install_historical(case, definition())
        params = {'action': action, 'workflow_id': 'boundary-flow'}
        if action == 'save':
            params['definition'] = definition()
    task_ids = [task.task_id for task in tasks(case)]
    tool = WorkflowsTool(case.home, engine=case.services.workflows._engine(case.scope_actor),
        business_binding_provider=lambda: (case.services.workflows, case.scope_actor))
    receipt = tool.execute(params)
    assert receipt.ok is False and receipt.error_code == REFUSAL
    assert receipt.reported_error_code == REFUSAL
    assert receipt.error_category == 'contract' and receipt.retryable is False
    assert receipt.effect_outcome == 'not_started'
    data = json.loads(receipt.output)
    assert data['error_code'] == REFUSAL
    record_property('native_tool_business_error_code', data.get('error_code'))
    record_property('native_tool_projection_fields', json.dumps(sorted(data)))
    assert [task.task_id for task in tasks(case)] == task_ids
    if action == 'resume':
        assert run_bytes(case, original) == before
    else:
        assert run_store(case).list_runs(workflow_id='boundary-flow') == []


# LLM: 丢失身份时只保留已有取消责任，不扫描全部任务、不猜子任务归属或伪造已清理证明。
# 函数用途: 锁住历史坏投影的取消pending语义，拒绝恢复不能偷偷结清旧责任。
def test_missing_identity_cancellation_retains_unknown_obligation(business):
    case = business
    original, task = affected_run(case, lost_identity=True)
    receipt = call(case, 'workflow_runs.cancel', {'workflow_run_id': original.workflow_run_id},
        revision=original.revision, key='cancel-unknown-identity')
    assert receipt['ok'], receipt
    assert receipt['data']['state'] == 'pending'
    assert receipt['data']['outstanding_refs'] == [original.steps[0].escalation_result['attempt_id']]
    current = run_store(case).load_run(original.workflow_run_id)
    assert current.status == 'cancelling' and current.steps[0].batches == original.steps[0].batches
    assert 'cancellation_receipt' not in current.steps[0].escalation_result
    before = run_bytes(case, current)
    assert_refused(call(case, 'workflow_runs.resume', {'workflow_run_id': current.workflow_run_id},
        revision=current.revision, key='refuse-unknown-cleanup'))
    assert run_bytes(case, current) == before
    only, = tasks(case)
    assert only.task_id == task.task_id and only.status == 'queued'
