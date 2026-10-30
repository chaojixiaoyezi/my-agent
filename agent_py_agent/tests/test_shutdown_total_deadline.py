# LLM: 直接验证队列停止链的真实线程及总预算，不用自然语言或睡眠推断完成；夹具只写tmp_path。
# 模块用途: 防止多个线程各耗一次timeout以及活线程被丢弃后误报成功。
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.ingress_queue import IngressQueue, QueueConfig
from agent_py_agent.agent.queue_worker import WorkerPool
from agent_py_agent.agent.storage_backend import StorageBackend


# LLM: 显式钉住当前工作树，不依赖cwd隐式导入、旧editable包或关闭PYTHONSAFEPATH；只改变自有测试子进程环境。
# 函数用途: 让信号测试子进程实际运行本工作树的Worker组件。
def _worker_subprocess_environment():
    root = Path(__file__).resolve().parents[2]
    return {**os.environ, 'PYTHONPATH': str(root) + os.pathsep + str(root / 'agent_py_agent')}


# LLM: 假线程精确消耗传入join预算，时钟由调用显式推进，不代替真实在途测试。
# 类用途: 确认整个池只消耗一份停止预算。
class BudgetThread:
    # LLM: 保存共享时钟和join观察，不创建线程或写文件。
    # 函数用途: 准备确定性的预算观察对象。
    def __init__(self, clock, waits):
        self.clock = clock
        self.waits = waits

    # LLM: 消耗本次传入的最大等待，线程保持alive以模拟未完成任务。
    # 函数用途: 精确记录各线程拿到的剩余预算。
    def join(self, timeout=None):
        self.waits.append(timeout)
        self.clock[0] += timeout

    # LLM: 测试期间一直在途，停止不能丢弃其引用。
    # 函数用途: 模拟尚未完成的线程。
    def is_alive(self):
        return True


# LLM: 只替换时钟和join，断言总预算与保留活线程；不改队列领取或完成逻辑。
# 函数用途: 复现逐线程timeout导致总停止时间成倍增加的问题。
def test_pool_shutdown_uses_one_deadline_and_retains_unfinished(monkeypatch):
    clock = [10.0]
    waits = []
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    pool = WorkerPool(None, lambda payload: None, workers=2)
    threads = [BudgetThread(clock, waits), BudgetThread(clock, waits)]
    pool._threads = threads.copy()
    report = pool.stop(timeout=3.0)
    assert sum(waits) == pytest.approx(3.0)
    assert pool._threads == threads
    assert report.drained is False and report.unfinished == 2
    assert report.deadline_monotonic == 13.0


# LLM: 真实SQLite队列与在途handler通过Event协调；超时后心跳和claim不得提前完成或遗失。
# 函数用途: 验证超时如实返回unfinished，随后能再次等待至真正complete。
def test_pool_timeout_is_not_completion_and_can_finish_later(tmp_path):
    queue = IngressQueue(StorageBackend.for_path(tmp_path / 'queue.db'), QueueConfig(lane_cap=50))
    queue.ensure_schema()
    queue.enqueue('drain-event', 'drain-lane', {'value': 1})
    entered = threading.Event()
    release = threading.Event()

    # LLM: 在显式释放前保持handler在途，finally保证即使断言失败也不残留工作线程。
    # 函数用途: 构造受控未完成任务。
    def handler(payload):
        entered.set()
        assert release.wait(5)

    pool = WorkerPool(queue, handler, workers=1)
    pool.start()
    try:
        assert entered.wait(3)
        report = pool.stop(timeout=0)
        assert report.drained is False and report.unfinished == 1
        assert queue.stats().get('claimed') == 1
        assert any(thread.is_alive() for thread in pool._threads)
    finally:
        release.set()
        final = pool.stop(timeout=3)
    assert final.drained is True and final.unfinished == 0
    assert queue.stats().get('completed') == 1
    assert pool._threads == []


# LLM: 进程层必须把同一deadline传给池和reaper，后者不能再拿一份完整timeout。
# 函数用途: 防止池耗尽预算后维护线程继续延长停止时间。
def test_worker_entry_shares_deadline_with_reaper(monkeypatch):
    from agent_py_agent.agent import worker_entry
    drain_components = getattr(worker_entry, 'drain_worker_components', None)
    assert callable(drain_components), 'worker entry must expose structured total-deadline drain'
    clock, waits = [10.0], []
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    pool = WorkerPool(None, lambda payload: None, workers=1)
    pool._threads = [BudgetThread(clock, waits)]
    reaper = BudgetThread(clock, waits)
    reaper.stop = lambda: None
    result = drain_components(pool, reaper, timeout=3.0)
    assert sum(waits) == pytest.approx(3.0)
    assert result.deadline_monotonic == 13.0
    assert result.drained is False and result.unfinished == 2


# LLM: 真实队列模拟终结确认失败，业务成功不能被转换成fail或被线程死亡洗成drained。
# 函数用途: 守住完成确认异常/未命中时的未知任务责任。
@pytest.mark.parametrize('ack', ['raise', 'false'])
def test_queue_worker_unconfirmed_complete_never_drains(tmp_path, monkeypatch, ack):
    from agent_py_agent.agent.queue_worker import QueueWorker
    queue = IngressQueue(StorageBackend.for_path(tmp_path / 'ack.db'), QueueConfig(lane_cap=50))
    queue.ensure_schema()
    queue.enqueue('ack-event', 'ack-lane', {})
    fail_calls = []

    # LLM: 只注入原队列确认口故障，不改领取/持久状态以保留真实claimed证据。
    # 函数用途: 模拟完成确认故障或失去claim。
    def complete(token):
        if ack == 'raise':
            raise OSError('ack unavailable')
        return False

    monkeypatch.setattr(queue, 'complete', complete)
    monkeypatch.setattr(queue, 'fail', lambda *args, **kwargs: fail_calls.append(1))
    pool = WorkerPool(queue, lambda payload: None, workers=1)
    assert isinstance(pool._pool[0], QueueWorker)
    pool._pool[0].run_once()
    report = pool.stop(timeout=0)
    assert not fail_calls
    assert report.drained is False and report.unfinished == 1
    assert report.in_flight[0]['message_id'] > 0
    assert report.in_flight[0]['phase'] == 'settlement_unknown'
    assert queue.stats().get('claimed') == 1


# LLM: 原fail写入也失败时不能把已死消费者当成已确认终态，异常输出不保存业务正文。
# 函数用途: 验证失败确认丢失后仍报告未完成责任。
def test_queue_worker_unconfirmed_failure_never_drains(tmp_path, monkeypatch):
    queue = IngressQueue(StorageBackend.for_path(tmp_path / 'fail.db'), QueueConfig(lane_cap=50))
    queue.ensure_schema()
    queue.enqueue('fail-event', 'fail-lane', {})

    # LLM: handler异常是业务失败，后续fail异常则是独立的落账未知。
    # 函数用途: 构造处理和持久失败两个不同观察点。
    def fail(*args, **kwargs):
        raise OSError('durable fail unavailable')

    monkeypatch.setattr(queue, 'fail', fail)
    pool = WorkerPool(queue, fail, workers=1)
    try:
        pool._pool[0].run_once()
    except OSError:
        pass
    report = pool.stop(timeout=0)
    assert report.drained is False and report.unfinished == 1
    assert report.in_flight[0]['phase'] == 'settlement_unknown'


# LLM: stop与新领取的准入共享短同步边界，停后直接run_once也不能领取任务。
# 函数用途: 防止外层循环检查后的恢复执行偷偷领取下一条消息。
def test_queue_worker_stopped_consumer_does_not_claim(tmp_path):
    from agent_py_agent.agent.queue_worker import QueueWorker
    queue = IngressQueue(StorageBackend.for_path(tmp_path / 'stopped.db'), QueueConfig(lane_cap=50))
    queue.ensure_schema()
    queue.enqueue('stop-event', 'stop-lane', {})
    worker = QueueWorker(queue, lambda payload: None)
    worker.stop()
    assert worker.run_once() is False
    assert queue.stats().get('pending') == 1


# LLM: 仅启动本测试自有SQLite子进程，真实SIGTERM和30秒handler；不调用生产服务、sandbox门或网络。
# 函数用途: 验证部署110秒预算内的30秒任务先complete再退出，并保留超时后的原租约恢复边界。
def test_graceful_sigterm_thirty_second_handler_completes(tmp_path):
    import json
    import subprocess
    import sys
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path

    code = '''
import json, sys, time
from agent_py_agent.agent.storage_backend import StorageBackend
from agent_py_agent.agent.ingress_queue import IngressQueue, QueueConfig
from agent_py_agent.agent.queue_worker import WorkerPool, StaleReaper
from agent_py_agent.agent.graceful import DrainState, install_sigterm_drain
from agent_py_agent.agent.worker_entry import drain_worker_components
q=IngressQueue(StorageBackend.for_path(sys.argv[1]),QueueConfig(lane_cap=50))
q.ensure_schema();q.enqueue('thirty-second-event','lane',{})
# LLM: 真实业务时长用于验证停止顺序，不以sleep推测是否完成。
# 函数用途: 模拟30秒在途处理并输出可观察的开始边界。
def handler(payload):
    print('HANDLER_STARTED',flush=True)
    time.sleep(30)
pool=WorkerPool(q,handler,workers=1,lease_seconds=2)
reaper=StaleReaper(q,interval=0.2)
drain=DrainState();install_sigterm_drain(drain)
pool.start();reaper.start();drain.wait()
result=drain_worker_components(pool,reaper,timeout=110)
print(json.dumps({'drain':result.to_dict(),'queue':q.stats()}),flush=True)
raise SystemExit(0 if result.drained else 1)
'''
    process = subprocess.Popen([sys.executable, '-B', '-c', code, str(tmp_path / 'thirty.db')],
        cwd=Path(__file__).resolve().parents[2], env=_worker_subprocess_environment(),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    readers = ThreadPoolExecutor(max_workers=1)
    try:
        assert readers.submit(process.stdout.readline).result(timeout=15).strip() == 'HANDLER_STARTED'
        started = time.monotonic()
        process.terminate()
        output, _ = process.communicate(timeout=45)
        assert process.returncode == 0, output
        final = json.loads(output.strip().splitlines()[-1])
        assert final['drain']['status'] == 'drained'
        assert final['drain']['unfinished'] == 0
        assert final['queue'].get('completed') == 1
        assert time.monotonic() - started >= 29
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        readers.shutdown(wait=True)


# LLM: 自有子进程真实超grace非零退出，原SQLite claim保留；只用原recover_stale/worker重处理，不伪造终态。
# 函数用途: 验证超时进程退出后仍能按原租约协议恢复并完成任务。
def test_graceful_timeout_process_keeps_claim_for_native_recovery(tmp_path):
    import json
    import subprocess
    import sys
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path

    path = tmp_path / 'timeout.db'
    code = '''
import json, sys, threading
from agent_py_agent.agent.storage_backend import StorageBackend
from agent_py_agent.agent.ingress_queue import IngressQueue, QueueConfig
from agent_py_agent.agent.queue_worker import WorkerPool, StaleReaper
from agent_py_agent.agent.graceful import DrainState, install_sigterm_drain
from agent_py_agent.agent.worker_entry import drain_worker_components
q=IngressQueue(StorageBackend.for_path(sys.argv[1]),QueueConfig(lane_cap=50))
q.ensure_schema();q.enqueue('timeout-event','lane',{'value':7})
original=q.heartbeat
# LLM: 包裹真实续租返回，仅成功后记录，不以调用过函数冒充续租确认。
# 函数用途: 在停止等待期间观察真实心跳是否继续。
def heartbeat(*args,**kwargs):
    confirmed=original(*args,**kwargs)
    if confirmed: print('LEASE_RENEWED',flush=True)
    return confirmed
q.heartbeat=heartbeat
# LLM: 通过Event阻塞真实在途任务，主线程停止返回时仍不能完成。
# 函数用途: 构造超过本次总宽限的处理。
def handler(payload):
    print('HANDLER_STARTED',flush=True)
    threading.Event().wait(60)
pool=WorkerPool(q,handler,workers=1,lease_seconds=2)
reaper=StaleReaper(q,interval=0.2)
drain=DrainState();install_sigterm_drain(drain)
pool.start();reaper.start();drain.wait()
result=drain_worker_components(pool,reaper,timeout=2.2)
print(json.dumps(result.to_dict()),flush=True)
raise SystemExit(0 if result.drained else 1)
'''
    process = subprocess.Popen([sys.executable, '-B', '-c', code, str(path)],
        cwd=Path(__file__).resolve().parents[2], env=_worker_subprocess_environment(),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    readers = ThreadPoolExecutor(max_workers=1)
    try:
        assert readers.submit(process.stdout.readline).result(timeout=15).strip() == 'HANDLER_STARTED'
        process.terminate()
        output, _ = process.communicate(timeout=15)
        assert process.returncode == 1, output
        assert 'LEASE_RENEWED' in output
        report = json.loads(output.strip().splitlines()[-1])
        assert report['status'] == 'unfinished' and report['unfinished'] == 1
        assert report['in_flight'][0]['message_id'] > 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        readers.shutdown(wait=True)
    queue = IngressQueue(StorageBackend.for_path(path), QueueConfig(lane_cap=50))
    assert queue.stats().get('claimed') == 1
    deadline = time.monotonic() + 5
    while queue.recover_stale() == 0:
        assert time.monotonic() < deadline, 'expired original claim must become recoverable'
        threading.Event().wait(0.05)
    from agent_py_agent.agent.queue_worker import QueueWorker
    seen = []
    assert QueueWorker(queue, seen.append).run_once() is True
    assert seen == [{'value': 7}]
    assert queue.stats().get('completed') == 1


# LLM: 真实SQLite领取后只注入原确认/claim故障；不调用stop/wait也须立即有脱敏结构化诊断。
# 函数用途: 防止后台消费者把异常只留在pending而静默退出。
@pytest.mark.parametrize('stage', ['claim', 'complete', 'false_ack'])
def test_queue_worker_pending_failure_is_logged_without_wait(tmp_path, monkeypatch, caplog, stage):
    import logging

    from agent_py_agent.agent.queue_worker import QueueWorker
    queue = IngressQueue(StorageBackend.for_path(tmp_path / 'diagnostic.db'), QueueConfig(lane_cap=50))
    queue.ensure_schema()
    queue.enqueue('diagnostic-event', 'lane', {'secret': 'must-not-log-payload'})

    # LLM: 异常正文故意含敏感样本，诊断只能保留类型和阶段而非正文/trace。
    # 函数用途: 模拟数据库操作失败或确认未命中。
    def failed(*args, **kwargs):
        if stage == 'false_ack':
            return False
        raise OSError('must-not-log-error')

    monkeypatch.setattr(queue, 'claim' if stage == 'claim' else 'complete', failed)
    worker = QueueWorker(queue, lambda payload: None)
    with caplog.at_level(logging.ERROR):
        worker.run_once()
    records = [record for record in caplog.records if getattr(record, 'event', '') == 'queue_worker_settlement_unknown']
    assert len(records) == 1 and records[0].phase == 'settlement_unknown'
    assert records[0].error_type == ('UnconfirmedAcknowledgment' if stage == 'false_ack' else 'OSError')
    assert 'must-not-log' not in caplog.text and records[0].exc_info is None
    assert worker.in_flight['phase'] == 'settlement_unknown'
