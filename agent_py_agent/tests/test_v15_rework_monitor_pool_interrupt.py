"""R3：真实 claim → MonitorPool → 工作流 → 脚本进程的停止和复用合同。"""
from __future__ import annotations

import ast
import inspect
import json
import os
import time
from functools import partial
from pathlib import Path
from threading import Event, Thread, get_ident
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import (
    WorkflowDefinition,
    WorkflowStepSpec,
    WorkflowStore,
)
from agent_py_agent.agent.concurrency.interrupt import (
    interrupt_by_name,
    is_interrupted,
    is_interruptible_registered,
)
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
)
from agent_py_agent.agent.conversation.claim_execution_binding import current_claim_execution
from agent_py_agent.agent.conversation.control_commands import conversation_request_interrupt_name
from agent_py_agent.agent.conversation.run_claim import conversation_claim_interrupt_name
from agent_py_agent.agent.scheduler.control import cancel_monitor
from agent_py_agent.agent.scheduler.execution import _run_with_claim_interrupts
from agent_py_agent.agent.scheduler.execution_policy import execution_definition_ref
from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest
from agent_py_agent.agent.workflows import script_runner
from agent_py_agent.agent.workflows.run_artifacts import current_run_out_target
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from agent_py_agent.agent.workflows.script_runner import ScriptRunner
from agent_py_agent.tests.test_schedule_control import _agent
from agent_py_agent.tests.test_scheduler_runtime import _RetryDelivery


def _fd_count():
    root = '/proc/self/fd' if Path('/proc/self/fd').is_dir() else '/dev/fd'
    return len(os.listdir(root))


@pytest.fixture
def case(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    home = agent.home_paths.owner_home_dir
    scripts = ScriptStore(home, workspace_root=tmp_path)
    for index in range(1, 4):
        code = f'''import json, os, sys, time
from pathlib import Path
inputs = json.load(sys.stdin)
root = Path(os.environ['MY_AGENT_RUN_OUT'])
(root / 's{index}.started').write_text('started')
time.sleep(1.2)
(root / 's{index}').write_text('finished')
if inputs.get('fail') and {index} == 1:
    sys.exit(3)
print(json.dumps({{'step': {index}}}))
'''
        scripts.save_script(AssetDescriptor(id=f'r3_s{index}', kind='script', name=f'步骤{index}'), code)
    channels = _RetryDelivery()
    channels.status = 'sent'
    agent.delivery_service = channels
    store, service = agent.conversation_store, agent.scheduler_service
    service.monitor_jitter_window_seconds = 0
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    scheduler = BackgroundMainAgentScheduler({'runtime': runtime, 'store': store, 'scheduler_service': service})
    calls, threads, markers, completed = [], [], {}, {}
    original = ScriptRunner.run_script
    execute_process = script_runner._execute

    # 只观察真实 runner；不在测试 callback 中补注册，不替代进程或执行结果。
    def observed(runner, script_id, inputs=None, **options):
        calls.append((script_id, get_ident(), current_claim_execution()))
        return original(runner, script_id, inputs, **options)

    monkeypatch.setattr(ScriptRunner, 'run_script', observed)

    # 标记只写原 run_out；进程副作用与成功归档是两个观察点，失败或停止不得靠归档判断是否执行。
    def observed_process(command, folder, environment, input_json, timeout, capture):
        target = current_run_out_target(home)
        assert target is not None and environment.get('MY_AGENT_RUN_OUT')
        label = json.loads(input_json)['run_label']
        scratch = Path(environment['MY_AGENT_RUN_OUT'])
        archived = (home / 'data/workflow_runs' / target.workflow_run_id / 'artifacts'
            / target.step_id / target.attempt_id)
        markers[label].append((target.step_id, scratch, archived))
        result = execute_process(command, folder, environment, input_json, timeout, capture)
        # 原执行器已回收真实进程，runner尚未清理scratch；只读实物，不补文件、不伪造回执。
        files = {}
        for index in range(1, 4):
            path = scratch / f's{index}'
            if path.is_file():
                files[path.name] = path.read_text()
        completed[label].append(SimpleNamespace(step_id=target.step_id,
            exit_code=result[0], cleanup=result[4], markers=files))
        return result

    monkeypatch.setattr(script_runner, '_execute', observed_process)
    value = SimpleNamespace(agent=agent, home=home, store=store, service=service,
        repo=agent.scheduler_repository, scheduler=scheduler, channels=channels,
        workflows=WorkflowStore(home, workspace_root=tmp_path), runs=WorkflowRunStore(home),
        root=tmp_path, calls=calls, threads=threads, markers=markers, completed=completed)
    try:
        yield value
    finally:
        for thread in threads:
            thread.join(timeout=30)
            assert not thread.is_alive(), 'scheduler tick did not release its claim'
        service.monitor_pool.shutdown()


def _schedule(case, name, *, fail=False):
    now = time.time()
    markers = case.markers[name] = []
    completed = case.completed[name] = []
    workflow_id = 'r3_' + name
    case.workflows.save_workflow(WorkflowDefinition(
        AssetDescriptor(id=workflow_id, kind='workflow', name=name),
        steps=tuple(WorkflowStepSpec(step_id=f's{index}', name=f'步骤{index}',
            mode='deterministic_script', script_id=f'r3_s{index}', on_error='fail')
            for index in range(1, 4))))
    thread = case.store.threads.get_or_create({'channel': 'feishu', 'channel_user_id': 'ou_r3',
        'channel_conversation_id': 'oc_' + name, 'canonical_user_id': 'ou_r3',
        'owner_id': case.agent.home_paths.owner_id, 'owner_home': str(case.home),
        'cwd': str(case.root), 'now': now - 100})
    job = case.repo.create_job(SchedulerJobCreateRequest(name=name, prompt='', thread_id=thread.thread_id,
        execution_target={'kind': 'workflow', 'asset_id': workflow_id,
            'execution_definition_ref': execution_definition_ref('workflow', workflow_id,
                case.workflows.get_workflow(workflow_id)),
            'input_ref': {'inputs': {'run_label': name, 'fail': fail}}},
        delivery={'mode': 'origin'}, creator={'local': True},
        schedule={'kind': 'every', 'every_seconds': 600, 'anchor_at': now, 'timezone': 'UTC'},
        now=now - 100))[0]
    case.service.enqueue_ready_runs(now=now)
    runs = case.repo.queued_runs(now=now)
    run_id = next(row['run_id'] for row in runs if row['job_id'] == job['job_id'])
    signals = [signal for signal in case.store.wakes.pending() if signal.root_task_id == run_id]
    assert len(signals) == 1
    return SimpleNamespace(job=job, signal=signals[0], markers=markers, completed=completed,
        workflow_id=workflow_id, now=now)


def _start(case, run, *, thread_only=False):
    errors = []

    def tick():
        try:
            if thread_only:
                case.scheduler.tick_thread(run.signal.thread_id, now=run.now)
            else:
                case.scheduler.tick(now=run.now)
        except BaseException as exc:
            errors.append(exc)

    thread = Thread(target=tick, name='r3-scheduler-tick')
    case.threads.append(thread)
    thread.start()
    return thread, errors


def _join(thread, errors):
    thread.join(timeout=30)
    assert not thread.is_alive(), 'scheduler tick is still running'
    assert not errors, errors


def _await_step(run, thread, errors):
    deadline = time.monotonic() + 20
    while not any((scratch / 's1.started').exists() for step, scratch, _ in run.markers if step == 's1') and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    scratch = next((scratch for step, scratch, _ in run.markers if step == 's1'), None)
    assert scratch is not None and (scratch / 's1.started').exists(), errors
    assert not (scratch / 's1').exists(), 'stop must land during the real first process'


def _markers(run):
    return [f's{index}' for index in range(1, 4)
        if any(process.markers.get(f's{index}') == 'finished' for process in run.completed)]


def _archived_markers(run):
    return [f's{index}' for index in range(1, 4)
        if any((archived / f's{index}').exists() for _, _, archived in run.markers)]


def _claim(binding):
    assert binding is not None, 'the real claim ContextVar must cross the pool boundary'
    return binding.claims.load(binding.thread_id, claim_scope_id=binding.claim_scope_id)


def _assert_occupied(case, run, binding):
    assert _claim(binding)['status'] == 'running'
    active = case.repo.get_active_run(run.signal.root_task_id)
    assert active is not None and active['claim_id']
    unexpected = Event()
    duplicate = case.service.monitor_pool.submit(case.home, run.job['job_id'], 'r3-duplicate', unexpected.set)
    assert duplicate.result(timeout=2)['reason'] == 'already_running'
    assert not unexpected.is_set()


def _assert_done(case, run):
    records = case.runs.list_runs(workflow_id=run.workflow_id)
    history, errors = case.repo.history(job_id=run.job['job_id'])
    assert not errors and len(history) == len(records) == 1
    if records[0].status != 'succeeded' or history[0]['status'] != 'done':
        pytest.fail(json.dumps({'workflow_status': records[0].status,
            'errors': [(step.step_id, step.error_code, step.error_message) for step in records[0].steps]}, ensure_ascii=False))
    assert _markers(run) == ['s1', 's2', 's3']
    assert _archived_markers(run) == ['s1', 's2', 's3']
    assert [process.exit_code for process in run.completed] == [0, 0, 0]
    assert case.store.tasks.load(run.signal.root_task_id).status == 'completed'


@pytest.mark.parametrize('sender', ['request', 'cancel_monitor', 'claim'])
def test_stop_during_real_script_prevents_later_steps_and_delivery(case, sender):
    warm = _schedule(case, 'warm')
    _join(*_start(case, warm))
    _assert_done(case, warm)
    fd_before, delivery_before, call_before = _fd_count(), len(case.channels.attempts), len(case.calls)
    run = _schedule(case, 'stopped')
    thread, errors = _start(case, run)
    _await_step(run, thread, errors)
    binding = case.calls[-1][2]
    _assert_occupied(case, run, binding)
    if sender == 'cancel_monitor':
        current = case.repo.get_job(run.job['job_id'])
        receipt = cancel_monitor(case.agent, case.repo, current['job_id'], current['version'])
        assert receipt['cancellation_status'] == 'cancel_requested', receipt
        assert case.store.tasks.load(run.signal.root_task_id).status == 'cancelled'
    else:
        name = (conversation_request_interrupt_name(run.signal.root_task_id) if sender == 'request'
            else conversation_claim_interrupt_name(binding.claim_id))
        assert interrupt_by_name(name)
    # 接受停止不是物理退出证明；当前脚本仍在途时，原 claim 和同监控占位不能提前释放。
    _assert_occupied(case, run, binding)
    _join(thread, errors)
    records = case.runs.list_runs(workflow_id=run.workflow_id)
    history, load_errors = case.repo.history(job_id=run.job['job_id'])
    assert _markers(run) == ['s1'], {'markers': _markers(run), 'workflow': [r.status for r in records], 'history': history}
    assert [process.step_id for process in run.completed] == ['s1']
    assert run.completed[0].exit_code == 0 and not run.completed[0].cleanup
    assert all(not scratch.exists() for _, scratch, _ in run.markers)
    assert len(records) == 1 and records[0].status == 'paused_for_resume'
    assert not load_errors and not history, history
    assert len(case.channels.attempts) == delivery_before
    assert [call[0] for call in case.calls[call_before:]] == ['r3_s1']
    assert case.store.tasks.load(run.signal.root_task_id).status == ('cancelled' if sender == 'cancel_monitor' else 'active')
    assert case.store.wakes.pending_one(run.signal.wake_signal_id) is not None
    assert _claim(binding)['status'] == 'cancelled'
    assert not is_interruptible_registered(conversation_request_interrupt_name(run.signal.root_task_id))
    assert not is_interruptible_registered(conversation_claim_interrupt_name(binding.claim_id))
    assert _fd_count() - fd_before == 0

    # C：原 worker 必须清旗；取消操作仅在 worker 退出后结为 UNKNOWN 并释放 scope 锁。
    probe = case.service.monitor_pool.submit(case.home, run.job['job_id'], 'r3-reuse-probe',
        partial(_run_with_claim_interrupts, (), Event(), lambda: (get_ident(), is_interrupted())))
    assert probe.result(timeout=2) == (case.calls[call_before][1], False)
    # 保留原 pending wake；取消操作的 scope 锁只在 worker 退出后释放，新的真实定时线程可正常完成。
    next_run = _schedule(case, 'fresh')
    case.scheduler = BackgroundMainAgentScheduler({'runtime': case.scheduler.runtime,
        'store': case.store, 'scheduler_service': case.service})
    _join(*_start(case, next_run, thread_only=True))
    _assert_done(case, next_run)
    assert {call[1] for call in case.calls[call_before:]} == {case.calls[call_before][1]}
    assert case.store.tasks.load(run.signal.root_task_id).status == ('cancelled' if sender == 'cancel_monitor' else 'active')
    assert len(case.channels.attempts) == delivery_before + 1
    assert _fd_count() - fd_before == 0


@pytest.mark.parametrize('sender', ['request', 'claim'])
def test_stop_while_queued_keeps_claim_until_worker_acknowledges(case, monkeypatch, sender):
    pool = case.service.monitor_pool
    release, submitted = Event(), Event()
    started = [Event() for _ in range(4)]
    futures, bindings = [], []
    submit = pool.submit

    def occupied(index):
        started[index].set()
        assert release.wait(20)

    def observed_submit(*args, **kwargs):
        bindings.append(current_claim_execution())
        future = submit(*args, **kwargs)
        submitted.set()
        return future

    run = _schedule(case, 'queued')
    try:
        # 保留生产池的四个worker和容量；用真实占位将本次业务留在原有界队列。
        for index, event in enumerate(started):
            futures.append(submit(case.home, f'occupied-{index}', f'occupied-run-{index}',
                lambda index=index: occupied(index)))
            assert event.wait(5)
        monkeypatch.setattr(pool, 'submit', observed_submit)
        thread, errors = _start(case, run)
        assert submitted.wait(10)
        binding = bindings[0]
        _assert_occupied(case, run, binding)
        name = (conversation_request_interrupt_name(run.signal.root_task_id) if sender == 'request'
            else conversation_claim_interrupt_name(binding.claim_id))
        assert interrupt_by_name(name)
        _assert_occupied(case, run, binding)
        assert not case.calls and not run.markers
        release.set()
        _join(thread, errors)
        assert not case.calls and not case.runs.list_runs(workflow_id=run.workflow_id)
        assert _claim(binding)['status'] == 'cancelled'
        assert case.store.tasks.load(run.signal.root_task_id).status == 'active'
        assert case.store.wakes.pending_one(run.signal.wake_signal_id) is not None
        assert case.repo.history(job_id=run.job['job_id']) == ([], [])
        assert not case.channels.attempts
    finally:
        release.set()
        for future in futures:
            future.result(timeout=20)


def test_stop_after_worker_exit_is_rechecked_before_parent_records_or_delivers(case, monkeypatch):
    ready, release = Event(), Event()
    submit = case.service.monitor_pool.submit

    def held_result(*args, **kwargs):
        future = submit(*args, **kwargs)
        result = future.result

        def after_worker(*args, **kwargs):
            value = result(*args, **kwargs)
            ready.set()
            assert release.wait(20)
            return value

        monkeypatch.setattr(future, 'result', after_worker)
        return future

    monkeypatch.setattr(case.service.monitor_pool, 'submit', held_result)
    run = _schedule(case, 'late')
    thread, errors = _start(case, run)
    try:
        assert ready.wait(20)
        # 工作流的成功已在停止之前持久化；本例只禁止父线程在晚到停止之后记成功或外发。
        assert _markers(run) == ['s1', 's2', 's3']
        assert interrupt_by_name(conversation_request_interrupt_name(run.signal.root_task_id))
        release.set()
        _join(thread, errors)
        assert case.runs.list_runs(workflow_id=run.workflow_id)[0].status == 'succeeded'
        assert case.repo.history(job_id=run.job['job_id']) == ([], [])
        assert case.store.tasks.load(run.signal.root_task_id).status == 'active'
        assert case.store.wakes.pending_one(run.signal.wake_signal_id) is not None
        assert not case.channels.attempts
    finally:
        release.set()


def test_real_script_failure_preserves_terminal_agreement_and_zero_fd_delta(case):
    warm = _schedule(case, 'warm')
    _join(*_start(case, warm))
    _assert_done(case, warm)
    fd_before, delivery_before = _fd_count(), len(case.channels.attempts)
    run = _schedule(case, 'failed', fail=True)
    _join(*_start(case, run))
    records = case.runs.list_runs(workflow_id=run.workflow_id)
    history, errors = case.repo.history(job_id=run.job['job_id'])
    assert not errors and len(history) == len(records) == 1
    assert records[0].status == history[0]['status'] == 'failed'
    assert _markers(run) == ['s1']
    assert _archived_markers(run) == []
    assert [process.exit_code for process in run.completed] == [3]
    assert all(not scratch.exists() for _, scratch, _ in run.markers)
    assert case.store.tasks.load(run.signal.root_task_id).status == 'abandoned'
    assert len(case.channels.attempts) == delivery_before
    assert _fd_count() - fd_before == 0


def test_interrupt_names_follow_real_claim_binding_and_bound_turn_precedence(case):
    from agent_py_agent.agent.conversation.claim_execution_binding import bind_claim_execution
    from agent_py_agent.agent.conversation.control_commands import session_task_interrupt_name
    from agent_py_agent.agent.scheduler.execution import _claim_interrupt_names

    run = _schedule(case, 'names')
    _join(*_start(case, run))
    _assert_done(case, run)
    binding = case.calls[0][2]
    with bind_claim_execution(binding):
        claim_name = conversation_claim_interrupt_name(binding.claim_id)
        assert _claim_interrupt_names({'task_id': '  ' + run.signal.root_task_id + '  '}) == (
            conversation_request_interrupt_name(run.signal.root_task_id), claim_name)
        assert _claim_interrupt_names({'task_id': run.signal.root_task_id,
            'host_session_task_id': run.signal.root_task_id}) == (
                session_task_interrupt_name(run.signal.root_task_id), claim_name)
        assert _claim_interrupt_names({}) == (claim_name,)
    assert _claim_interrupt_names({'task_id': ' '}) == ()


def test_all_scheduler_pool_handoffs_are_scoped_and_fenced_before_effects():
    from agent_py_agent.agent.scheduler import execution

    tree = ast.parse(inspect.getsource(execution))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    submits = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute) and node.func.attr == 'submit']
    assert len(submits) == 1, 'every new cross-thread handoff needs the same cancellation contract'
    submit = submits[0]
    assert ast.unparse(submit.func) == 'service.monitor_pool.submit'
    callback = submit.args[3]
    assert isinstance(callback, ast.Call) and ast.unparse(callback.func) == 'partial'
    assert ast.unparse(callback.args[0]) == '_run_with_claim_interrupts'
    assert ast.unparse(callback.args[1]) == '_claim_interrupt_names(kwargs)'
    assert ast.unparse(callback.args[2]) == 'early'

    target = functions['execute_scheduled_target']
    guarded = next(node for node in target.body if isinstance(node, ast.Try))
    handoff = next(node for node in guarded.body if isinstance(node, ast.With))
    assert ast.unparse(handoff.items[0].context_expr) == 'register_interrupt_callback(early.set)'
    assert any(isinstance(node, ast.Call) and ast.unparse(node.func) == 'future.result'
        for node in ast.walk(handoff))
    after = guarded.body[guarded.body.index(handoff) + 1]
    assert isinstance(after, ast.If) and ast.unparse(after.test) == 'is_interrupted()'
    assert isinstance(after.body[0], ast.Raise)
    before = guarded.body[guarded.body.index(handoff) - 2]
    assert isinstance(before, ast.If) and ast.unparse(before.test) == 'is_interrupted()'
    assert [ast.unparse(handler.type) for handler in guarded.handlers] == ['ScheduleValidationError']

    worker = functions['_run_with_claim_interrupts']
    scope = next(node for node in worker.body if isinstance(node, ast.With))
    assert ast.unparse(scope.items[0].context_expr) == 'ExitStack()'
    assert isinstance(scope.body[0], ast.For)
    assert any(isinstance(node, ast.Call) and ast.unparse(node.func) == 'register_interruptible'
        for node in ast.walk(scope.body[0]))
    assert isinstance(scope.body[1], ast.If) and isinstance(scope.body[1].body[0], ast.Raise)
    assert ast.unparse(scope.body[2]) == 'return callback()'
    assert not any(isinstance(node, (ast.Assign, ast.AnnAssign)) for node in tree.body)
    assert not any(isinstance(node, ast.Call) and ast.unparse(node.func).split('.')[-1] == 'set_interrupt'
        for node in ast.walk(tree))
