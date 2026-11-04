"""监控并发、非阻塞准入及重启稳定错峰的确定性交错测试。"""
import hashlib
from concurrent.futures import Future
from contextlib import contextmanager
from threading import Event, local

from agent_py_agent.agent.scheduler import schedule
from agent_py_agent.agent.scheduler.monitor_pool import MonitorPool


# LLM: 时间用纯输入，无真实sleep；摘要公式必须与跨进程口径一致。
# 函数用途: 核验抖动的稳定、边界及分散。
def test_jitter_is_exact_stable_hash_offset():
    jitter = getattr(schedule, 'monitor_jitter_seconds', None)
    assert callable(jitter)
    offsets = [jitter('owner', f'job{i}', window_seconds=30) for i in range(20)]
    assert offsets == [int(hashlib.sha256(f'owner:job{i}'.encode()).hexdigest(), 16) % 30 for i in range(20)]
    assert len(set(offsets)) > 1 and all(0 <= offset < 30 for offset in offsets)
    assert offsets == [jitter('owner', f'job{i}', window_seconds=30) for i in range(20)]
    assert jitter('owner', 'job', window_seconds=0) == 0
    assert schedule.compute_next_run({'kind': 'every', 'every_seconds': 600, 'anchor_at': 1000, 'timezone': 'UTC'},
        after=999, owner_id='owner', monitor_id='job') == 1000 + jitter('owner', 'job')


# LLM: Event控制业务持有时刻，排队和跳过不依赖机器速度。
# 函数用途: 验证运行数和队列有界、同监控重入跳过及满载不等待。
def test_pool_bound_and_duplicate_trigger_skip_without_blocking(tmp_path):
    started, release, dispatched = Event(), Event(), Event()
    pool = MonitorPool(max_workers=1, queue_limit=1)

    # LLM: 只有测试显式释放后才结束，不使用时间猜测并发状态。
    # 函数用途: 保持首个业务运行在途。
    def blocking():
        started.set()
        assert release.wait(5)
        return {'status': 'succeeded'}

    try:
        first = pool.submit(tmp_path, 'job1', 'run1', blocking)
        assert isinstance(first, Future)
        assert started.wait(3)
        duplicate = pool.submit(tmp_path, 'job1', 'run2', lambda: {'status': 'unexpected'})
        queued = pool.submit(tmp_path, 'job2', 'run3', lambda: {'status': 'succeeded'})
        full = pool.submit(tmp_path, 'job3', 'run4', lambda: {'status': 'unexpected'})
        dispatched.set()
        assert dispatched.is_set() and full.done() and duplicate.done()
        assert duplicate.result()['error_code'] == full.result()['error_code'] == 'WORKFLOW_RUN_SKIPPED_ALREADY_RUNNING'
        assert full.result()['reason'] == 'queue_full'
        assert not queued.done()
        release.set()
        assert first.result(timeout=3)['status'] == queued.result(timeout=3)['status'] == 'succeeded'
    finally:
        release.set()
        pool.shutdown()


# LLM: ContextVar复制不能冒充thread-local，必须实际进入参数绑定作用域。
# 函数用途: 验证每个worker看到自己的run参数且结束后不串身份。
def test_pool_workers_bind_explicit_thread_local_run_params(tmp_path):
    state = local()
    pool = MonitorPool(max_workers=1, queue_limit=2)

    # LLM: 与宿主current_prompt_scope同样保存和恢复原线程参数。
    # 函数用途: 给测试worker绑定一个准确run身份。
    @contextmanager
    def bind(params):
        previous = getattr(state, 'params', None)
        state.params = params
        try:
            yield
        finally:
            state.params = previous

    try:
        results = [pool.submit(tmp_path, f'job{i}', f'run{i}', lambda: state.params,
            run_params=f'run{i}', bind_run_params=bind) for i in range(2)]
        assert all(isinstance(result, Future) for result in results)
        assert [result.result(timeout=3) for result in results] == ['run0', 'run1']
        assert getattr(state, 'params', None) is None
    finally:
        pool.shutdown()


# LLM: 原周期身份不改写，派发和due索引必须使用同一偏移，否则会丢唤醒或提前执行。
# 函数用途: 验证真实调度预约后，错峰门不阻塞并最终发布原Wake。
def test_dispatch_and_due_index_share_jitter_deadline(tmp_path):
    from agent_py_agent.agent.scheduler.due_index import _earliest_due_at
    from agent_py_agent.agent.scheduler.service import SchedulerService
    from agent_py_agent.tests.test_plan_w8_schedule_target import asset_case

    case = asset_case(tmp_path)
    service = SchedulerService(case.agent.scheduler_repository, conversation_store=case.agent.conversation_store)
    offset = schedule.monitor_jitter_seconds(case.agent.scheduler_repository.owner['id'], case.job['job_id'])
    assert getattr(service, 'monitor_jitter_window_seconds', None) == 30
    case.agent.scheduler_repository.reserve_due_runs(now=1000)
    assert service.enqueue_ready_runs(now=1000 + offset - 0.5) == []
    runs = case.agent.scheduler_repository.queued_runs(now=1000 + offset)
    assert len(runs) == 1 and runs[0]['scheduled_for'] == 1000
    store = {'owner': case.agent.scheduler_repository.owner, 'jobs': {case.job['job_id']: case.job},
             'runs': {runs[0]['run_id']: runs[0]}}
    assert _earliest_due_at(store, now=1000) == 1000 + offset
    assert len(service.enqueue_ready_runs(now=1000 + offset)) == 1
    service.monitor_pool.shutdown()
