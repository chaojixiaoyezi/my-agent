"""启动窗口、终态回读与控制回执的确定性交错回归。"""
from __future__ import annotations

import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.run_store import WorkflowExecutionError


# LLM: 只用真实运行账本和本地分析端口，不启动外部动作。
# 函数用途: 提供单步工作流。
def definition():
    return {'descriptor': {'id': 'window-flow', 'kind': 'workflow', 'name': '窗口检查', 'io_contract': {}},
            'steps': [{'step_id': 'one', 'name': '检查', 'mode': 'llm_skill_analysis'}]}


# LLM: Event固定提交、取消与取得执行锁的顺序；真实账本和取消控制不被替换。
# 函数用途: 验证三种并发策略都返回取消终态，不执行步骤或制造通知摘要。
@pytest.mark.parametrize('policy', ['allow', 'skip_if_running', 'queue'])
def test_cancel_committed_before_execution_lock_has_no_phantom_state(tmp_path, monkeypatch, policy):
    calls, notices, summaries = [], [], []
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda *args: calls.append(1) or {'value': 7})
    engine._notify_breakpoint = lambda row: notices.append(row.status)
    engine._record_summary = lambda row: summaries.append(row.status)
    created, release = threading.Event(), threading.Event()
    create = engine.store.create_run

    # LLM: 先提交真实create_run，再暂停启动线程；不伪造持久记录。
    # 函数用途: 将取消窗口固定在提交后、执行前。
    def publish(*args, **kwargs):
        create(*args, **kwargs)
        created.set()
        assert release.wait(10)

    monkeypatch.setattr(engine.store, 'create_run', publish)
    with ThreadPoolExecutor(1) as pool:
        started = pool.submit(engine.run_workflow, definition(), workflow_run_id='window', concurrency_policy=policy)
        try:
            assert created.wait(5)
            revision = engine.store.load_run('window').revision
            assert engine.cancel_workflow_run('window', expected_revision=revision, operation_key='cancel') == []
            sealed = copy.deepcopy(engine.store._read('window', 'run.json')['control_operations']['cancel'])
            assert engine.store.load_run('window').status == 'cancelled'
            release.set()
            try:
                result = started.result(5)
            except WorkflowExecutionError as exc:
                result = exc
            assert calls == []
            assert engine.store.load_run('window').status == 'cancelled'
            assert notices == [] and summaries == []
            assert not isinstance(result, Exception), repr(result)
            assert result.status == 'cancelled'
            assert engine.cancel_workflow_run('window', expected_revision=revision, operation_key='cancel') == []
            assert engine.store._read('window', 'run.json')['control_operations']['cancel'] == sealed
            restarted = WorkflowEngine(tmp_path, analysis_handler=lambda *args: calls.append(2))
            with pytest.raises(ValueError, match='WORKFLOW_CANCELLED'):
                restarted.resume_workflow_run('window')
            assert restarted.store.load_run('window').status == 'cancelled' and calls == []
        finally:
            release.set()


# LLM: 未取消的控制组沿原正式执行，不通过测试替身跳过步骤。
# 函数用途: 验证三种策略的普通启动仍恰好执行一次。
@pytest.mark.parametrize('policy', ['allow', 'skip_if_running', 'queue'])
def test_normal_start_executes_once(tmp_path, policy):
    calls = []
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda *args: calls.append(1) or {'value': 7})
    assert engine.run_workflow(definition(), concurrency_policy=policy).status == 'succeeded'
    assert calls == [1]


# LLM: 执行锁已持有时保留等待及原修订冲突，不把执行中的取消外推为启动窗口取消。
# 函数用途: 固定持锁取消的既有控制语义。
def test_cancel_while_execution_lock_held_keeps_wait_and_cas_semantics(tmp_path):
    engine = WorkflowEngine(tmp_path)
    entered, release = threading.Event(), threading.Event()

    # LLM: 业务端口内等待Event，确保真实执行锁已经持有。
    # 函数用途: 固定运行中而非启动前的观察点。
    def analyze(*args):
        entered.set()
        assert release.wait(10)
        return {'value': 7}

    engine.analysis_handler = analyze
    with ThreadPoolExecutor(2) as pool:
        future = pool.submit(engine.run_workflow, definition(), workflow_run_id='held')
        try:
            assert entered.wait(5)
            checking = threading.Event()

            # LLM: 真实取消入口仍携带原修订及操作键，不屏蔽其冲突结果。
            # 函数用途: 从另一线程发起持锁期间的取消。
            def cancel():
                checking.set()
                return engine.cancel_workflow_run('held', expected_revision=1, operation_key='cancel')

            cancellation = pool.submit(cancel)
            assert checking.wait(5) and not cancellation.done()
        finally:
            release.set()
        assert future.result(5).status == 'succeeded'
        with pytest.raises(ValueError, match='WORKFLOW_RUN_REVISION_CONFLICT'):
            cancellation.result(5)


# LLM: 在save_run失败出口精确注入两种稳定码；回读使用真实已封存终态。
# 函数用途: 验证未落盘暂停不覆盖终态，也不产生通知或摘要。
@pytest.mark.parametrize('status', ['cancelled', 'succeeded', 'skipped', 'failed'])
@pytest.mark.parametrize('code', ['WORKFLOW_RUN_TERMINAL', 'WORKFLOW_RUN_REVISION_CONFLICT'])
def test_unsaved_pause_returns_durable_terminal_without_hooks(tmp_path, monkeypatch, status, code):
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda *args: {'value': 1})
    original = engine.run_workflow(definition(), workflow_run_id='finished')
    durable = replace(original, status=status)
    engine.store.save_run(durable, resuming=True)
    stale = replace(durable, status='running', revision=durable.revision - 1)
    notices, summaries = [], []
    engine._notify_breakpoint = lambda row: notices.append(row.status)
    engine._record_summary = lambda row: summaries.append(row.status)

    # LLM: 原执行错误与后续保存错误相互独立，不能通过前者冒充保存失败分支。
    # 函数用途: 触发正式失败收口。
    def fail(*args, **kwargs):
        raise RuntimeError('simulated execution error')

    # LLM: 只注入保存出口的稳定码，终态读取仍来自真实Store。
    # 函数用途: 分别覆盖终态拒写和修订冲突。
    def reject_save(*args, **kwargs):
        raise ValueError(code)

    monkeypatch.setattr(engine, '_drive_steps', fail)
    monkeypatch.setattr(engine.store, 'save_run', reject_save)
    try:
        result = engine._drive(stale, definition(), 0)
    except WorkflowExecutionError as exc:
        result = exc
    assert notices == [] and summaries == []
    assert not isinstance(result, Exception), repr(result)
    assert result.status == status


# LLM: 未取消且可保存暂停的执行错误应继续报原工作流错误，不误判为终态成功。
# 函数用途: 保留普通失败的暂停与通知行为。
def test_nonterminal_save_error_keeps_original_error_behavior(tmp_path, monkeypatch):
    engine = WorkflowEngine(tmp_path)
    original = engine.run_workflow
    notices = []
    engine._notify_breakpoint = lambda row: notices.append(row.status)

    # LLM: 仅业务执行端口失败，暂停保存及回读不被替换。
    # 函数用途: 触发非终态的原异常路径。
    def fail(*args, **kwargs):
        raise OSError('simulated I/O error')

    monkeypatch.setattr(engine, '_drive_steps', fail)
    with pytest.raises(WorkflowExecutionError) as caught:
        original(definition())
    assert caught.value.record.status == 'paused_for_resume' and notices == ['paused_for_resume']


# LLM: 真实资产claim保持忙碌，Event将第二个运行的取消固定在准入之前。
# 函数用途: 验证忙碌策略不以skipped或queued遮盖已落盘的取消终态。
@pytest.mark.parametrize('policy', ['skip_if_running', 'queue'])
def test_cancelled_start_keeps_terminal_receipt_when_concurrency_is_busy(tmp_path, monkeypatch, policy):
    entered, release, created, continue_start = (threading.Event() for _ in range(4))
    calls, admissions = [], []

    # LLM: 真实首个运行持有claim，记录哪个运行真正进入业务步骤。
    # 函数用途: 控制忙碌窗口并拦截第二次业务执行。
    def analyze(spec, context, skills):
        calls.append(context.run_inputs['label'])
        entered.set()
        assert release.wait(10)
        return {'value': 7}

    engine = WorkflowEngine(tmp_path, analysis_handler=analyze)
    engine.concurrency_admission_observer = admissions.append
    create = engine.store.create_run

    # LLM: 不替换真实持久写，仅在第二个运行提交后等待取消完成。
    # 函数用途: 固定已有claim时的新运行启动窗口。
    def publish(record, *args, **kwargs):
        create(record, *args, **kwargs)
        if record.workflow_run_id == 'later':
            created.set()
            assert continue_start.wait(10)

    monkeypatch.setattr(engine.store, 'create_run', publish)
    with ThreadPoolExecutor(2) as pool:
        holder = pool.submit(engine.run_workflow, definition(), {'label': 'holder'},
                             workflow_run_id='holder', concurrency_policy=policy)
        try:
            assert entered.wait(5)
            later = pool.submit(engine.run_workflow, definition(), {'label': 'later'},
                                workflow_run_id='later', concurrency_policy=policy)
            assert created.wait(5)
            assert engine.cancel_workflow_run('later', expected_revision=engine.store.load_run('later').revision,
                                              operation_key='cancel') == []
            continue_start.set()
            try:
                result = later.result(5)
            except ValueError as exc:
                result = exc
            assert not isinstance(result, Exception), repr(result)
            assert result.status == 'cancelled' and calls == ['holder']
            assert admissions == []
        finally:
            release.set()
            continue_start.set()
        assert holder.result(5).status == 'succeeded'


# LLM: 无法确认终态的保存或回读失败，不能吞掉最初执行错误或伪称可恢复。
# 函数用途: 覆盖I/O、非终态CAS及回读失败的原错误链。
@pytest.mark.parametrize('scenario', ['io_error', 'cas_nonterminal', 'read_error'])
def test_pause_save_failure_keeps_execution_failure_and_original_cause(tmp_path, monkeypatch, scenario):
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda *args: {'value': 1})
    current = engine.run_workflow(definition(), workflow_run_id='unfinished')
    current = replace(current, status='running')
    engine.store.save_run(current, resuming=True)
    original_load = engine.store.load_run
    execution_error = RuntimeError('simulated execution error')
    reads, notices = [], []

    # LLM: 固定同一个异常对象供原始cause身份核验。
    # 函数用途: 触发工作流执行失败。
    def fail_steps(*args, **kwargs):
        raise execution_error

    # LLM: 保存出口按场景失败，不修改真实持久状态。
    # 函数用途: 区分I/O失败与需要回读的修订冲突。
    def fail_save(*args, **kwargs):
        if scenario == 'io_error':
            raise OSError('simulated I/O error')
        raise ValueError('WORKFLOW_RUN_REVISION_CONFLICT')

    # LLM: 真实回读仅在指定场景失败，计数防止无条件吞错或重试。
    # 函数用途: 验证是否及何时核对持久终态。
    def reread(*args, **kwargs):
        reads.append(1)
        if scenario == 'read_error':
            raise OSError('simulated read error')
        return original_load(*args, **kwargs)

    engine._notify_breakpoint = lambda row: notices.append(row.status)
    engine._record_summary = lambda row: None
    monkeypatch.setattr(engine, '_drive_steps', fail_steps)
    monkeypatch.setattr(engine.store, 'save_run', fail_save)
    monkeypatch.setattr(engine.store, 'load_run', reread)
    with pytest.raises(WorkflowExecutionError) as caught:
        engine._drive(current, definition(), 0)
    assert caught.value.__cause__ is execution_error
    assert caught.value.record.status == 'paused_for_resume'
    assert caught.value.record.breakpoint['can_resume'] is False
    assert notices == ['paused_for_resume']
    assert len(reads) == (0 if scenario == 'io_error' else 1)
    assert original_load('unfinished').status == 'running'


# LLM: 保留真实dispatch和worker，仅固定worker持锁先于启动线程回执复核的交错；不依赖sleep。
# 函数用途: 验证已入队且已开始的运行不因回执阶段竞争执行锁而误报启动失败。
def test_queued_worker_starts_before_admission_receipt_check(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from agent_py_agent.agent.workflows import engine as engine_module

    first_entered, release_first, queued_entered, release_queued = (threading.Event() for _ in range(4))
    calls, after_dispatch = [], []

    # LLM: 两个真实执行都停在分析端口，取消及账本逻辑不被替换。
    # 函数用途: 记录各运行恰好执行一次并等待精确放行。
    def analyze(spec, context, skills):
        label = context.run_inputs['label']
        calls.append(label)
        (first_entered if label == 'first' else queued_entered).set()
        assert (release_first if label == 'first' else release_queued).wait(10)
        return {'value': 7}

    engine = WorkflowEngine(tmp_path, analysis_handler=analyze)
    dispatch, load, execution_lock = engine_module.dispatch, engine.store.load_run, engine.store.execution_lock

    # LLM: 实际票据和线程仍由W3创建，返回时确保排队worker已经取得执行锁。
    # 函数用途: 固定旧回执检查必遇busy的顺序。
    def ordered_dispatch(*args, **kwargs):
        result = dispatch(*args, **kwargs)
        if isinstance(result, dict) and result.get('status') == 'queued':
            release_first.set()
            assert queued_entered.wait(5)
            after_dispatch.append(threading.get_ident())
        return result

    # LLM: 只读load允许等待已有执行者；调用前放行，避免测试自身形成等待环。
    # 函数用途: 让修复后的只读回查返回真实持久记录。
    def load_after_dispatch(run_id):
        if run_id == 'queued' and threading.get_ident() in after_dispatch:
            release_queued.set()
        return load(run_id)

    # LLM: 真实非阻塞锁先执行；旧代码若争锁必先失败，再释放worker，错误不得被夹具掩盖。
    # 函数用途: 在失败出口解除测试等待，保留原始busy结果。
    @contextmanager
    def observe_execution_lock(run_id):
        try:
            with execution_lock(run_id):
                yield
        finally:
            if run_id == 'queued' and threading.get_ident() in after_dispatch:
                release_queued.set()

    monkeypatch.setattr(engine_module, 'dispatch', ordered_dispatch)
    monkeypatch.setattr(engine.store, 'load_run', load_after_dispatch)
    monkeypatch.setattr(engine.store, 'execution_lock', observe_execution_lock)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(engine.run_workflow, definition(), {'label': 'first'},
                            workflow_run_id='first', concurrency_policy='queue')
        try:
            assert first_entered.wait(5)
            queued = pool.submit(engine.run_workflow, definition(), {'label': 'queued'},
                                 workflow_run_id='queued', concurrency_policy='queue')
            try:
                result = queued.result(5)
            except (RuntimeError, ValueError) as exc:
                result = exc
            assert not isinstance(result, Exception), repr(result)
            assert getattr(result, 'status', None) == 'succeeded' or result.get('status') == 'queued'
        finally:
            release_first.set()
            release_queued.set()
        assert first.result(5).status == 'succeeded'
    assert engine.store.load_run('queued').status == 'succeeded'
    assert calls == ['first', 'queued']
