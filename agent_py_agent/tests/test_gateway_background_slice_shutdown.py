"""真实后台claim/core与fake provider的宿主停止接线；不启动Gateway或网络。"""
import threading
import time
from types import SimpleNamespace

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.concurrency.interrupt import interrupt_by_name, is_interrupted
from agent_py_agent.agent.conversation.run_claim import conversation_claim_interrupt_name
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli.gateway_loops import _BackgroundMainSupervisor
from agent_py_agent.cli.models import GatewayRunContext


# LLM: 只合成当前provider等待与协作中断，不访问网络；晚退分支用于证明逻辑future不等于物理退出。
# 类用途: 让真实主链进入可控等待，测试结束只释放自己创建的等待。
class _WaitingBackend:
    name = 'waiting-local-fake'

    def __init__(self, *, cooperative=True):
        self.started = threading.Event()
        self.exited = threading.Event()
        self.release = threading.Event()
        self.cooperative = cooperative

    def generate(self, prompt, on_chunk=None, **kwargs):
        self.started.set()
        try:
            if self.cooperative:
                while not self.release.wait(.02):
                    if is_interrupted():
                        raise InterruptedError('合成provider准确中断')
            else:
                assert self.release.wait(15)
            return ModelResponse(text='已完成当前执行片。', backend=self.name)
        finally:
            self.exited.set()


# LLM: 用真实owner/会话/source/claim/core，只替代provider；调用原候选提交方法，不手工绑定停止目标。
# 函数用途: 提交一片真实后台任务并等待fake模型进入，便于观察停机是否走本片finally。
def _running(tmp_path, *, cooperative=True, start=True):
    agent = SimpleAgent(AgentConfig(model_backend='echo', enable_tools=False,
        my_agent_home=str(tmp_path/'home'), background_owner_workers=1), tmp_path)
    backend = _WaitingBackend(cooperative=cooperative)
    agent.backend = backend
    store = agent.conversation_store
    thread = store.threads.get_or_create({'canonical_user_id':'local', 'channel':'cli_chat',
        'channel_conversation_id':'shutdown-one', 'channel_user_id':'local'})
    store.tasks.bind({'thread_id':thread.thread_id, 'task_id':'original-work',
        'goal':'继续原有任务', 'status':'active'})
    store.tasks.select_workspace_task({'thread_id':thread.thread_id, 'task_id':'original-work'})
    observed = store.observations.append({'thread_id':thread.thread_id,
        'event_type':'manual_wake', 'summary':'继续', 'requires_main_agent':True})
    store.wakes.raise_signal({'thread_id':thread.thread_id, 'observation':observed,
        'task_id':'original-work', 'reason':'manual_wake'})
    context = GatewayRunContext(agent=agent, paths=SimpleNamespace(), config_path=tmp_path/'config.yaml')
    supervisor = _BackgroundMainSupervisor(context)
    future = None
    if start:
        supervisor._submit_thread_candidates([('base',supervisor._base_scheduler,[thread.thread_id])],available=1)
        future = supervisor._inflight[('base',thread.thread_id)]
        assert backend.started.wait(3)
    return SimpleNamespace(agent=agent,store=store,thread=thread,backend=backend,
        context=context,supervisor=supervisor,future=future)


# LLM: 失败收尾仅中断测试创建的准确claim并释放fake等待，不启动第二次工作或修改durable状态。
# 函数用途: 保证红例也不遗留测试线程。
def _release(ctx):
    claim = ctx.store.claims.load(ctx.thread.thread_id)
    interrupt_by_name(conversation_claim_interrupt_name(claim.get('claim_id','')))
    ctx.backend.release.set()
    ctx.future.result(timeout=4)
    ctx.supervisor.shutdown()


def test_normal_shutdown_interrupts_original_background_claim_and_settles_attempt(tmp_path):
    ctx = _running(tmp_path)
    try:
        claim = ctx.store.claims.load(ctx.thread.thread_id)
        binding = claim['runtime_authority']
        assert ctx.agent.subagents.runtime_db.get_attempt(binding['attempt_id'])['status'] == 'running'
        ctx.supervisor.shutdown()
        assert ctx.backend.exited.wait(.5), '循环退出不代表原后台provider已中断/退出'
        ctx.future.result(timeout=.5)
        final_claim = ctx.store.claims.load(ctx.thread.thread_id)
        assert final_claim['claim_id'] == claim['claim_id']
        assert final_claim['status'] == 'cancelled'
        attempt = ctx.agent.subagents.runtime_db.get_attempt(binding['attempt_id'])
        assert attempt['status'] == 'cancelled' and attempt['ended_at'] > 0
        assert ctx.agent.subagents.runtime_db.get_agent_run_exec_lock(binding['agent_run_id']) is None
        assert ctx.store.tasks.list(ctx.thread.thread_id)[0].status == 'active'
        assert ctx.store.wakes.pending(), '停服务不能消费原业务来源'
    finally:
        _release(ctx)


def test_cleanup_refuses_drained_when_background_future_is_still_running(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core import tool_model_generation
    from agent_py_agent.cli import gateway_process
    from agent_py_agent.tests.test_gateway_model_call_shutdown_settlement import _cleanup_request
    # 用户停止走取消宽限，不是墙钟超时宽限；保留Future未退出及真实claim未结清断言。
    monkeypatch.setattr(tool_model_generation, "_MODEL_CANCEL_DRAIN_SECONDS", 5.0)
    ctx = _running(tmp_path, cooperative=False)
    try:
        request, _events = _cleanup_request(monkeypatch, [], ctx.agent)
        request.context.background_shutdown = getattr(ctx.context, 'background_shutdown', None)
        ctx.supervisor.shutdown()
        assert not ctx.future.done()
        report = gateway_process._cmd_gateway_run_cleanup(request)
        assert report['drain_complete'] is False, '仅三条loop退出不能签后台执行已排空'
        assert report['background_blockers']
        assert ctx.store.claims.load(ctx.thread.thread_id)['status'] == 'running'
    finally:
        _release(ctx)


def test_finished_future_does_not_hide_late_original_model_worker(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core import tool_model_generation
    from agent_py_agent.cli import gateway_process
    from agent_py_agent.tests.test_gateway_model_call_shutdown_settlement import _cleanup_request

    monkeypatch.setattr(tool_model_generation, '_MODEL_CANCEL_DRAIN_SECONDS', 0)
    ctx = _running(tmp_path, cooperative=False)
    try:
        ctx.supervisor.shutdown()
        assert ctx.future.done() and not ctx.backend.exited.is_set()
        request, _events = _cleanup_request(monkeypatch, [], ctx.agent)
        request.context.background_shutdown = ctx.context.background_shutdown
        report = gateway_process._cmd_gateway_run_cleanup(request)
        assert report['drain_complete'] is False
        assert any(row['reason']=='background_model_worker_unsettled' for row in report['background_blockers'])
        ctx.backend.release.set()
        assert ctx.backend.exited.wait(1)
        deadline = time.monotonic()+1
        while ctx.context.background_shutdown.blockers() and time.monotonic()<deadline:
            time.sleep(.01)
        assert ctx.context.background_shutdown.blockers() == []
    finally:
        _release(ctx)


def test_one_host_shutdown_does_not_interrupt_another_owner_job(tmp_path):
    one = _running(tmp_path/'one')
    two = _running(tmp_path/'two')
    try:
        one.supervisor.shutdown()
        assert one.backend.exited.is_set() and one.future.done()
        assert not two.backend.exited.is_set() and not two.future.done()
        assert two.store.claims.load(two.thread.thread_id)['status'] == 'running'
        assert two.store.tasks.list(two.thread.thread_id)[0].status == 'active'
    finally:
        _release(one)
        _release(two)


def test_shutdown_preserves_independent_managed_collector(tmp_path):
    import subprocess
    import sys

    from agent_py_agent.agent.tooling.process_registry import capture_process_birth_token
    from agent_py_agent.agent.tooling.process_session_store import (
        ProcessSessionStore,
        process_session_store_root,
    )
    from agent_py_agent.tests.test_gateway_background_sessions_shutdown import _record

    ctx = _running(tmp_path/'host')
    process = subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)'])
    try:
        owner = ctx.agent.home_paths.owner_home_dir
        root = process_session_store_root(tmp_path/'collector',str(owner))
        store = ProcessSessionStore(root)
        token = capture_process_birth_token(process.pid)
        assert token
        store.write(_record('bg-independent-collector',process.pid,token,status='running',owner_home=str(owner)))
        before = store.load('bg-independent-collector').record
        ctx.supervisor.shutdown()
        assert ctx.future.done() and ctx.backend.exited.is_set()
        assert process.poll() is None
        assert store.load('bg-independent-collector').record == before
    finally:
        process.terminate()
        process.wait(timeout=3)
        _release(ctx)


from agent_py_agent.tests.test_background_tool_approval import background

_FIXTURES = (background,)


def test_future_waiting_input_is_not_active_execution_or_cancelled_by_stop(background, monkeypatch):
    from agent_py_agent.agent.conversation.input_dependencies import (
        arm_input_dependency,
        create_input_dependency,
        input_dependency_path,
    )
    from agent_py_agent.agent.conversation.tool_approval_scope import tool_approval_scope
    from agent_py_agent.cli import gateway_process
    from agent_py_agent.tests.test_background_tool_approval import _bind_background_authority
    from agent_py_agent.tests.test_gateway_model_call_shutdown_settlement import _cleanup_request

    params = _bind_background_authority(background)
    scope = tool_approval_scope(background.agent, background.task_id)
    row = create_input_dependency(background.agent,scope,input_id='future-normal-input',
        provider='synthetic',expires_at=time.time()+3600)
    arm_input_dependency(background.agent,row['input_id'])
    path = input_dependency_path(background.agent,row['input_id'])
    before = path.read_bytes()
    repo = background.agent.subagents.runtime_db
    run = repo.agent_run_for_run_id(params.run_id)
    assert repo.settle_agent_attempt(agent_run_id=run['agent_run_id'],attempt_id=params.attempt_id)['settled']
    background.store.claims.finish({'thread_id':background.thread.thread_id,'task_id':background.task_id,
        'claim_id':background.claim['claim_id'],'status':'finished'})
    context = GatewayRunContext(agent=background.agent,paths=SimpleNamespace(),config_path=path.parent/'unused.yaml')
    supervisor = _BackgroundMainSupervisor(context)
    supervisor.shutdown()
    request, _events = _cleanup_request(monkeypatch,[],background.agent)
    request.context.background_shutdown = context.background_shutdown
    report = gateway_process._cmd_gateway_run_cleanup(request)
    assert report['drain_complete'] is True and report['background_blockers'] == []
    assert path.read_bytes() == before
    assert background.store.tasks.load(background.task_id).status == 'active'
    assert repo.get_attempt(params.attempt_id)['status'] == 'done'


def test_queued_background_job_is_cancelled_without_starting_or_creating_claim(tmp_path):
    ctx = _running(tmp_path,start=False)
    release = threading.Event()
    executor = ctx.supervisor._get_executor()
    occupying = executor.submit(release.wait,3)
    try:
        ctx.supervisor._submit_thread_candidates([('base',ctx.supervisor._base_scheduler,[ctx.thread.thread_id])],available=1)
        future = ctx.supervisor._inflight[('base',ctx.thread.thread_id)]
        ctx.supervisor.shutdown()
        assert future.cancelled() and not ctx.backend.started.is_set()
        assert ctx.store.claims.load(ctx.thread.thread_id) == {}
        ctx.supervisor._submit_thread_candidates([('base',ctx.supervisor._base_scheduler,[ctx.thread.thread_id])],available=1)
        assert len(ctx.supervisor._inflight) == 1
    finally:
        release.set()
        occupying.result(timeout=3)


def test_stop_between_claim_acquisition_and_registration_is_not_lost(tmp_path,monkeypatch):
    ctx = _running(tmp_path,start=False)
    original = ctx.store.claims.acquire
    acquired = []

    def acquire(request):
        claim = original(request)
        if claim is not None:
            acquired.append(claim['claim_id'])
            ctx.context.background_shutdown.stop()
        return claim

    monkeypatch.setattr(ctx.store.claims,'acquire',acquire)
    ctx.supervisor._submit_thread_candidates([('base',ctx.supervisor._base_scheduler,[ctx.thread.thread_id])],available=1)
    future = ctx.supervisor._inflight[('base',ctx.thread.thread_id)]
    future.result(timeout=3)
    ctx.supervisor.shutdown()
    assert len(acquired) == 1 and not ctx.backend.started.is_set()
    claim = ctx.store.claims.load(ctx.thread.thread_id)
    assert claim['claim_id'] == acquired[0] and claim['status'] == 'cancelled'
    assert ctx.context.background_shutdown.blockers() == []


import pytest


@pytest.mark.parametrize('fault', ['claim_running','claim_identity','attempt_unknown','operation_unknown','exec_lock','task_status_unknown','task_binding'])
def test_readonly_closeout_never_converts_unknown_or_unlocks_another_fact(background,fault):
    from agent_py_agent.agent.conversation.background_shutdown import (
        BackgroundShutdown,
        BackgroundSlice,
    )
    from agent_py_agent.agent.conversation.claim_execution_binding import ClaimExecutionBinding
    from agent_py_agent.tests.test_background_tool_approval import _bind_background_authority

    params = _bind_background_authority(background)
    repo = background.agent.subagents.runtime_db
    run = repo.agent_run_for_run_id(params.run_id)
    operation = repo.create_tool_operation(agent_run_id=run['agent_run_id'],attempt_id=params.attempt_id,
        operation_type='synthetic_effect') if fault=='operation_unknown' else None
    assert repo.settle_agent_attempt(agent_run_id=run['agent_run_id'],attempt_id=params.attempt_id)['settled']
    background.store.claims.finish({'thread_id':background.thread.thread_id,'task_id':background.task_id,
        'claim_id':background.claim['claim_id'],'status':'finished'})
    with repo.transaction() as conn:
        if fault=='task_status_unknown':
            conn.execute("UPDATE task_runs SET status='unknown' WHERE task_run_id=?",(run['task_run_id'],))
        if fault=='attempt_unknown':
            conn.execute("UPDATE agent_attempts SET status='unknown' WHERE attempt_id=?",(params.attempt_id,))
        if operation is not None:
            conn.execute("UPDATE tool_operations SET status='UNKNOWN',handler_started_at=1 WHERE operation_id=?",(operation['operation_id'],))
        if fault=='exec_lock':
            conn.execute("INSERT INTO resource_locks(lock_id,canonical_scope,holder_instance,pid,start_token,attempt_id,attempt_generation,workspace_epoch,tool_operation_generation,lease_expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                ('held-lock','attempt-exec:'+run['agent_run_id'],'test',1,'test',params.attempt_id,1,1,0,9999999999,1,1))
    if fault=='task_binding':
        from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic
        path = background.store.claims.storage.background_claim_path(background.thread.thread_id)
        update_json_file_atomic(path,lambda row:{**row,'runtime_authority':{**row['runtime_authority'],'task_id':'other-task'}})
    if fault.startswith('claim_'):
        from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic
        path = background.store.claims.storage.background_claim_path(background.thread.thread_id)
        update_json_file_atomic(path,lambda row:{**row, **({'status':'running'} if fault=='claim_running' else {'claim_id':'other-claim'})})
    manager = BackgroundShutdown()
    item = BackgroundSlice(manager,repo)
    item.claims.append(ClaimExecutionBinding(background.store.claims,background.thread.thread_id,
        background.claim['claim_id'],background.thread.thread_id))
    item.finished = True
    before_attempt = dict(repo.get_attempt(params.attempt_id))
    before_lock = repo.get_agent_run_exec_lock(run['agent_run_id'])
    assert item.blockers()
    assert dict(repo.get_attempt(params.attempt_id)) == before_attempt
    after_lock = repo.get_agent_run_exec_lock(run['agent_run_id'])
    assert (dict(before_lock) if before_lock is not None else None) == (dict(after_lock) if after_lock is not None else None)
    if operation is not None:
        assert repo.unsettled_attempt_operations(params.attempt_id)[0]['status'] == 'UNKNOWN'


def test_completed_physical_observation_is_retired_before_next_job():
    from agent_py_agent.agent.conversation.background_shutdown import (
        BackgroundShutdown,
        start_background_model_worker,
    )
    manager = BackgroundShutdown()
    release = threading.Event()
    worker = threading.Thread(target=release.wait,daemon=True)
    try:
        with manager.observe(None):
            start_background_model_worker(worker)
        assert len(manager.slices) == 1
        release.set()
        worker.join(timeout=1)
        assert not worker.is_alive()
        with manager.observe(None):
            assert len(manager.slices) == 1, '下一片只保当前观察，不无限保留已退的旧线程引用'
        assert manager.slices == set()
    finally:
        release.set()
        worker.join(timeout=1)


@pytest.fixture
def reconfigurable_monitor_pool(monkeypatch):
    from agent_py_agent.agent.scheduler import monitor_pool

    monkeypatch.setattr(monitor_pool, '_SHARED_POOL', None)
    try:
        yield monitor_pool.shared_monitor_pool
    finally:
        if monitor_pool._SHARED_POOL is not None:
            monitor_pool._SHARED_POOL.shutdown()


def test_different_agent_limits_reconfigure_the_same_borrowed_monitor_pool(tmp_path, reconfigurable_monitor_pool):
    from agent_py_agent.agent.owner_scoped_pool import _close_agent_mcp_clients

    agents = []
    try:
        for index, (workers, inflight) in enumerate(((1, 3), (3, 5), (3, 4), (2, 3))):
            root = tmp_path / str(index)
            agent = SimpleAgent(AgentConfig(model_backend='echo', enable_plugins=False,
                my_agent_home=str(root / 'home'), background_owner_workers=workers,
                gateway_global_inflight_limit=inflight), root)
            agents.append(agent)
            pool = agent.scheduler_service.monitor_pool
            assert pool is agents[0].scheduler_service.monitor_pool
            assert pool._executor._max_workers == workers
            assert pool._capacity == inflight
        _close_agent_mcp_clients(agents[-1])
        borrowed = agents[0].scheduler_service.monitor_pool
        assert borrowed.submit(tmp_path, 'after-resize', 'run', lambda: 'done').result(timeout=10) == 'done'
        borrowed.shutdown()
        replacement = reconfigurable_monitor_pool(max_workers=1, queue_limit=0)
        assert replacement is not borrowed and not replacement._closed
        assert replacement.submit(tmp_path, 'replacement', 'run', lambda: 'done').result(timeout=10) == 'done'
    finally:
        for agent in agents:
            _close_agent_mcp_clients(agent)


def test_monitor_growth_preserves_running_and_queued_jobs(tmp_path, reconfigurable_monitor_pool):
    pool = reconfigurable_monitor_pool(max_workers=1, queue_limit=2)
    entered = [threading.Event() for _ in range(3)]
    release = [threading.Event() for _ in range(3)]

    def callback(index):
        entered[index].set()
        assert release[index].wait(10)
        return index

    try:
        first = pool.submit(tmp_path, 'first', 'run1', lambda: callback(0))
        assert entered[0].wait(10)
        queued = pool.submit(tmp_path, 'queued', 'run2', lambda: callback(1))
        assert not queued.running()
        assert reconfigurable_monitor_pool(max_workers=2, queue_limit=1) is pool
        third = pool.submit(tmp_path, 'third', 'run3', lambda: callback(2))
        assert entered[1].wait(10)
        assert not first.done() and not entered[2].is_set()
        assert pool.submit(tmp_path, 'full', 'run4', lambda: None).result()['reason'] == 'queue_full'
        release[1].set()
        assert queued.result(timeout=10) == 1
        assert entered[2].wait(10) and not first.done()
        release[0].set()
        release[2].set()
        assert first.result(timeout=10) == 0 and third.result(timeout=10) == 2
        assert len(pool._executor._threads) == 2
    finally:
        for event in release:
            event.set()


def test_monitor_shrink_keeps_accepted_jobs_and_bounds_new_execution(tmp_path, reconfigurable_monitor_pool):
    pool = reconfigurable_monitor_pool(max_workers=3, queue_limit=1)
    previous = pool._executor
    entered = [threading.Event() for _ in range(4)]
    release = [threading.Event() for _ in range(4)]

    def callback(index):
        entered[index].set()
        assert release[index].wait(10)
        return index

    try:
        running = [pool.submit(tmp_path, str(index), str(index), lambda index=index: callback(index))
                   for index in range(3)]
        assert all(event.wait(10) for event in entered[:3])
        queued = pool.submit(tmp_path, 'queued', 'queued', lambda: callback(3))
        assert reconfigurable_monitor_pool(max_workers=1, queue_limit=1) is pool
        assert pool._capacity == 2 and len(pool._pending) == 4
        assert pool.submit(tmp_path, '0', 'duplicate', lambda: None).result()['reason'] == 'already_running'
        assert pool.submit(tmp_path, 'full', 'full', lambda: None).result()['reason'] == 'queue_full'
        release[0].set()
        release[1].set()
        assert [future.result(timeout=10) for future in running[:2]] == [0, 1]
        # FIFO 队列后的探针已执行，证明旧 worker 已接走排队任务，但执行门仍等待缩容。
        previous.submit(lambda: None).result(timeout=10)
        assert queued.running() and not entered[3].is_set()
        release[2].set()
        assert running[2].result(timeout=10) == 2
        assert entered[3].wait(10)
        returned = threading.Event()
        queued.add_done_callback(lambda completed: returned.set())
        release[3].set()
        assert queued.result(timeout=10) == 3
        # Future 唤醒可早于 done callback，等池锁下最后一份占位归还再检查回收。
        assert returned.wait(10) and not pool._pending
        assert pool._executor is not previous and previous._shutdown
        for worker in previous._threads:
            worker.join(timeout=10)
            assert not worker.is_alive()
        assert pool.submit(tmp_path, 'reuse', 'reuse', lambda: 'done').result(timeout=10) == 'done'
        assert len(pool._executor._threads) == 1
    finally:
        for event in release:
            event.set()


def test_monitor_queue_reduction_keeps_cancellation_and_capacity_return(tmp_path, reconfigurable_monitor_pool):
    pool = reconfigurable_monitor_pool(max_workers=1, queue_limit=2)
    entered, release = threading.Event(), threading.Event()

    def blocking():
        entered.set()
        assert release.wait(10)
        return 'held'

    try:
        running = pool.submit(tmp_path, 'held', 'held', blocking)
        assert entered.wait(10)
        first = pool.submit(tmp_path, 'first', 'first', lambda: pytest.fail('已取消的监控不能执行'))
        second = pool.submit(tmp_path, 'second', 'second', lambda: pytest.fail('已取消的监控不能执行'))
        assert reconfigurable_monitor_pool(max_workers=1, queue_limit=1) is pool
        assert first.cancel() and second.cancel()
        replacement = pool.submit(tmp_path, 'first', 'replacement', lambda: 'replacement')
        assert not replacement.done() and not running.done()
        assert pool.submit(tmp_path, 'full', 'full', lambda: None).result()['reason'] == 'queue_full'
        release.set()
        assert running.result(timeout=10) == 'held'
        assert replacement.result(timeout=10) == 'replacement'
        assert first.cancelled() and second.cancelled()
    finally:
        release.set()


@pytest.mark.parametrize('fails', [False, True])
def test_monitor_resize_preserves_job_context_without_retaining_request(tmp_path, reconfigurable_monitor_pool, fails):
    import gc
    import weakref
    from contextvars import ContextVar

    request = ContextVar('res2-request', default=None)

    class Payload:
        pass

    def callback():
        assert request.get() is not None
        if fails:
            raise ValueError('synthetic failure')
        return request.get().index

    pool = None
    for index, (workers, queue) in enumerate(((1, 1), (3, 1), (1, 0), (2, 2), (1, 1))):
        item = Payload()
        item.index = index
        reference = weakref.ref(item)
        token = request.set(item)
        try:
            current = reconfigurable_monitor_pool(max_workers=workers, queue_limit=queue)
            assert pool is None or current is pool
            pool = current
            future = pool.submit(tmp_path, 'same', str(index), callback)
        finally:
            request.reset(token)
        del item
        if fails:
            with pytest.raises(ValueError, match='synthetic failure'):
                future.result(timeout=10)
        else:
            assert future.result(timeout=10) == index
        del future
        assert pool._executor.submit(request.get).result(timeout=10) is None
        gc.collect()
        assert reference() is None


def test_concurrent_monitor_configuration_changes_keep_one_live_pool(tmp_path, reconfigurable_monitor_pool):
    from concurrent.futures import ThreadPoolExecutor

    def submit(index):
        pool = reconfigurable_monitor_pool(max_workers=1 + index % 3, queue_limit=index % 2)
        future = pool.submit(tmp_path, str(index), str(index), lambda: index)
        return pool, future

    with ThreadPoolExecutor(max_workers=8) as callers:
        results = list(callers.map(submit, range(32)))
    pool = results[0][0]
    for current, future in results:
        assert current is pool
        result = future.result(timeout=10)
        assert isinstance(result, int) or result['reason'] == 'queue_full'
    assert not pool._closed and len(pool._executor._threads) <= 3
