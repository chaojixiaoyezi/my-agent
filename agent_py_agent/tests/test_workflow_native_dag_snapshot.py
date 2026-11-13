"""原生DAG治理读取原子冻结定义，不反向等待父执行锁；执行权仍互斥。"""
import multiprocessing
import os
from threading import Event, Thread

import pytest

from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from agent_py_agent.tests.test_wf_07_08_foreach_and_dag import _flow


def test_definition_reader_survives_concurrent_atomic_save(tmp_path, monkeypatch):
    from agent_py_agent.agent.common import nofollow_fs

    supports_dir_fd = nofollow_fs._supports_dir_fd()
    if not supports_dir_fd:
        pytest.skip('此交错针对支持 dir_fd 的文件系统')
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda *args: {'value': 1})
    run = engine.run_workflow(_flow([{'step_id': 'one', 'mode': 'llm_skill_analysis'}]))
    original = engine.store.load_workflow_snapshot(run.workflow_run_id)
    original_open = os.open
    published = False

    def open_during_save(name, flags, *args, **kwargs):
        nonlocal published
        if (not published and name == 'run.json' and kwargs.get('dir_fd') is not None
                and flags & os.O_ACCMODE == os.O_RDONLY):
            published = True
            engine.store.save_run(run)
        return original_open(name, flags, *args, **kwargs)

    # os.supports_dir_fd 按函数对象识别能力；插入调度点后仍须走本机实际的 no-follow 读链。
    monkeypatch.setattr(os, 'supports_dir_fd', os.supports_dir_fd | {open_during_save})
    monkeypatch.setattr(os, 'open', open_during_save)
    assert engine.store.load_workflow_snapshot(run.workflow_run_id) == original
    assert published


def test_atomic_definition_reader_does_not_wait_for_execution_lock(tmp_path):
    definition = _flow([{'step_id': 'one', 'mode': 'llm_skill_analysis'}])
    engine = WorkflowEngine(tmp_path, analysis_handler=lambda *args: {'value': 1})
    run = engine.run_workflow(definition)
    started, finished = Event(), Event()
    read = {}

    def query():
        started.set()
        read['snapshot'] = WorkflowRunStore(tmp_path).load_workflow_snapshot(run.workflow_run_id)
        finished.set()

    with engine.store.execution_lock(run.workflow_run_id):
        reader = Thread(target=query)
        reader.start()
        assert started.wait(2)
        progressed = finished.wait(2)
    reader.join(3)
    assert progressed, '只读单记录冻结定义不能等待正在执行的运行锁'
    assert read['snapshot']['descriptor']['id'] == 'dag-flow'
    # 返回的是从canonical原子文件解码的新对象，不是可变的共享授权快照。
    read['snapshot']['descriptor']['metadata']['workflow_review_policy'] = {'steps': []}
    assert 'workflow_review_policy' not in engine.store.load_workflow_snapshot(run.workflow_run_id)['descriptor']['metadata']


def _native_dag_worker(home, entered, release, cancel, results):
    from agent_py_agent.agent.common.cancellation import ToolCancelled
    from agent_py_agent.agent.workflows.native_governance import NativeWorkflowGovernance

    called = []
    store = WorkflowRunStore(home)
    governance = NativeWorkflowGovernance(None, store, owner_home=home,
        owner_id='alice', author_provider=lambda: 'original-author')

    def analyze(spec, *_):
        called.append(spec['step_id'])
        if spec['step_id'] == 'first':
            entered.set()
            if not release.wait(8):
                raise RuntimeError('test release timeout')
            if cancel:
                raise ToolCancelled('original cancellation')
        return {'value': 7}

    engine = WorkflowEngine(home, analysis_handler=analyze, run_authorizer=governance.require_effect)
    definition = _flow([
        {'step_id': 'first', 'mode': 'llm_skill_analysis'},
        {'step_id': 'reviewed', 'mode': 'llm_skill_analysis', 'depends_on': ['first']},
    ], descriptor={'metadata': {'workflow_review_policy': {'steps': ['reviewed'], 'minimum_reviews': 1}}})
    try:
        run = engine.run_workflow(definition, workflow_run_id='native-dag-run')
        results.put({'status': run.status, 'called': called,
                     'review': run.steps[1].escalation_result.get('pending_review'),
                     'error_code': run.steps[1].error_code})
    except ToolCancelled:
        results.put({'status': store.load_run('native-dag-run').status, 'called': called, 'cancelled': True})
    except BaseException as error:
        results.put({'exception': type(error).__name__, 'message': str(error)})


@pytest.mark.parametrize('cancel', [False, True])
def test_real_native_dag_governance_keeps_execution_mutex_and_cancellation(tmp_path, cancel):
    # 子进程仅隔离回归死锁；不改锁实现或继承父线程的held集合，失败也能回收测试执行体。
    context = multiprocessing.get_context('spawn')
    entered, release, results = context.Event(), context.Event(), context.Queue()
    process = context.Process(target=_native_dag_worker,
        args=(str(tmp_path), entered, release, cancel, results))
    process.start()
    try:
        assert entered.wait(5), 'DAG worker在原生governance读冻结定义时被父执行锁卡住'
        store = WorkflowRunStore(tmp_path)
        snapshot = store.load_workflow_snapshot('native-dag-run')
        assert snapshot['descriptor']['metadata']['workflow_review_policy']['steps'] == ['reviewed']
        with pytest.raises(RuntimeError, match='工作流正在执行'):
            with store.execution_lock('native-dag-run'):
                pytest.fail('第二执行体不得进入')
        release.set()
        process.join(5)
        assert not process.is_alive(), '原DAG/治理/取消未收口'
        result = results.get(timeout=2)
        assert result['called'] == ['first'], result
        if cancel:
            # 原DAG把ToolCancelled保存为暂停断点并继续传播；不是伪造已撤销全部副作用。
            assert result['status'] == 'paused_for_resume' and result['cancelled']
        else:
            assert result['status'] == 'failed' and result['error_code'] == 'WORKFLOW_REVIEW_REQUIRED', result
            assert result['review']['owner_id'] == 'alice'
            assert result['review']['workflow_run_id'] == 'native-dag-run'
            assert result['review']['step_id'] == 'reviewed'
        record = store.load_run('native-dag-run')
        assert record.status == result['status']
    finally:
        release.set()
        if process.is_alive():
            process.terminate()
        process.join(3)
        results.close()
        results.join_thread()


@pytest.mark.parametrize('changed', ['workflow_run_id', 'workflow_snapshot'])
def test_atomic_definition_reader_rejects_wrong_identity(tmp_path, changed):
    import json

    engine = WorkflowEngine(tmp_path, analysis_handler=lambda *args: {'value': 1})
    run = engine.run_workflow(_flow([{'step_id': 'one', 'mode': 'llm_skill_analysis'}]))
    path = engine.store.run_dir(run.workflow_run_id) / 'run.json'
    data = json.loads(path.read_text())
    if changed == 'workflow_run_id':
        data[changed] = 'different-run'
    else:
        data[changed]['descriptor']['id'] = 'different-definition'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='身份'):
        engine.store.load_workflow_snapshot(run.workflow_run_id)
