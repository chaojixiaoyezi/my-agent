"""验证原控制Thread、readiness交错和真实临时双泵；不代签原生W/native验收。"""
import threading
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.request_execution_ports import RequestExecutionContextUnavailable
from agent_py_agent.agent.gateway_parts.multiprocess_control_poller import ControlPollingLane


def test_independent_poll_lane_is_joined_after_stop():
    entered, release, stop = threading.Event(), threading.Event(), threading.Event()
    threads = []

    # LLM: 此替身只有同步poll观察，不造控制回执、owner、权限或完成状态。
    # 函数用途: 保持原轮询线程活到测试明确释放，验证close真的join该线程。
    def poll():
        threads.append(threading.current_thread())
        entered.set()
        assert release.wait(3)

    lane = ControlPollingLane(SimpleNamespace(poll=poll), stop)
    try:
        lane.start()
        assert entered.wait(3)
        assert threads == [lane._thread] and lane._thread is not threading.current_thread()
        assert not lane._thread.daemon
        stop.set()
    finally:
        release.set()
        lane.close()
    assert not lane._thread.is_alive()


def test_typed_poll_failure_wakes_worker_and_keeps_original_exception():
    stop = threading.Event()
    original = RequestExecutionContextUnavailable("synthetic_source_expired")
    calls = []

    # LLM: 原typed对象在真实线程里抛出，不模拟side effect或request unknown提交。
    # 函数用途: 核轮询异常唤醒主循环并由同一close传播，不能静默重试。
    def poll():
        calls.append(True)
        raise original

    lane = ControlPollingLane(SimpleNamespace(poll=poll), stop)
    lane.start()
    assert stop.wait(3)
    with pytest.raises(RequestExecutionContextUnavailable) as caught:
        lane.close()
    assert caught.value is original and calls == [True]
    assert not lane._thread.is_alive()


def test_already_stopping_worker_never_enters_poll():
    stop = threading.Event()
    stop.set()
    lane = ControlPollingLane(SimpleNamespace(poll=lambda: pytest.fail("stopped worker polled")), stop)
    lane.start()
    lane.close()
    assert not lane._thread.is_alive()


def test_constructor_preserves_thread_before_any_start_attempt(monkeypatch):
    def forbidden_start(_thread):
        pytest.fail("construction started the original thread")

    monkeypatch.setattr(threading.Thread, "start", forbidden_start)
    stop = threading.Event()
    lane = ControlPollingLane(SimpleNamespace(poll=lambda: None), stop)
    assert lane._thread.ident is None
    assert not stop.is_set()
    lane.close()


def test_started_thread_is_retained_when_start_ack_raises(monkeypatch):
    entered, release, stop = threading.Event(), threading.Event(), threading.Event()
    real_start = threading.Thread.start
    original = RuntimeError("synthetic_start_ack_error")

    def poll():
        entered.set()
        assert release.wait(3)

    def start_then_raise(thread):
        real_start(thread)
        assert entered.wait(3)
        raise original

    lane = ControlPollingLane(SimpleNamespace(poll=poll), stop)
    thread = lane._thread
    monkeypatch.setattr(threading.Thread, "start", start_then_raise)
    try:
        with pytest.raises(RuntimeError) as caught:
            lane.start()
        assert caught.value is original
        assert stop.is_set() and lane._thread is thread and thread.is_alive()
        with pytest.raises(RuntimeError):
            lane.start()
    finally:
        release.set()
        lane.close()
    assert lane._thread is thread and not thread.is_alive()


def test_unknown_birth_is_not_retried_or_reported_as_joined(monkeypatch):
    original = RuntimeError("synthetic_before_birth_error")
    calls = []

    def start_without_ack(thread):
        calls.append(thread)
        raise original

    stop = threading.Event()
    lane = ControlPollingLane(SimpleNamespace(poll=lambda: None), stop)
    thread = lane._thread
    monkeypatch.setattr(threading.Thread, "start", start_without_ack)
    try:
        with pytest.raises(RuntimeError) as caught:
            lane.start()
        assert caught.value is original and stop.is_set()
        with pytest.raises(RuntimeError):
            lane.start()
        with pytest.raises(RuntimeError, match="cannot join thread before it is started"):
            lane.close()
        assert calls == [thread] and lane._thread is thread
    finally:
        stop.set()
        if thread.ident is not None:
            thread.join(3)
            assert not thread.is_alive()


def test_worker_root_keeps_lane_for_finally_after_start_ack_error(monkeypatch):
    from agent_py_agent.agent.gateway_parts import multiprocess_worker as worker

    entered, release = threading.Event(), threading.Event()
    real_start, real_close = threading.Thread.start, ControlPollingLane.close
    original = RuntimeError("synthetic_root_start_ack_error")
    threads, closed, claim_stops, pump_closes, notices = [], [], [], [], []
    partition = object()
    launch = SimpleNamespace(partition=partition, parent_pid=1, instance_id="")

    def poll():
        entered.set()
        assert release.wait(3)

    def start_then_raise(thread):
        threads.append(thread)
        real_start(thread)
        assert entered.wait(3)
        raise original

    def close_original(lane):
        closed.append(lane._thread)
        assert lane.worker_stop.is_set()
        release.set()
        real_close(lane)

    pump = SimpleNamespace(controls=SimpleNamespace(poll=poll), inflight=0,
                           stop_claiming=lambda: claim_stops.append(True),
                           close=lambda: pump_closes.append(True))
    monkeypatch.setattr(worker, "_install_stop_handlers", lambda _stop: {})
    monkeypatch.setattr(worker, "_restore_stop_handlers", lambda _previous: None)
    monkeypatch.setattr(worker, "_arm_parent_exit", lambda _pid: "")
    monkeypatch.setattr(worker, "prepare_worker_readiness", lambda _launch: {"ready": True})
    monkeypatch.setattr(worker, "current_worker_partition", lambda: partition)
    monkeypatch.setattr(worker, "create_request_pump", lambda *_args: pump)
    monkeypatch.setattr(worker, "_publish_heartbeat", lambda _launch, row, _notice: notices.append(row))
    monkeypatch.setattr(worker, "_worker_execution_loop", lambda *_args: pytest.fail("failed start reached request loop"))
    monkeypatch.setattr(ControlPollingLane, "close", close_original)
    monkeypatch.setattr(threading.Thread, "start", start_then_raise)
    try:
        with pytest.raises(RuntimeError) as caught:
            worker.run_worker(launch)
        assert caught.value is original
    finally:
        release.set()
        for thread in threads:
            thread.join(3)
            assert not thread.is_alive()
    assert len(threads) == 1 and closed == threads
    assert claim_stops == [True] and pump_closes == [True]
    assert notices[-1]["reason"] == "worker_runtime_failed"


# LLM: 真实线程仅选择交错，原异常收集后由主测试断言，不能在线程中静默失败。
# 函数用途: 在可join的线程里运行一个真实调用并保留返回值/原异常。
def _background_action(action):
    done, results, errors = threading.Event(), [], []

    # LLM: 只捕获调用事实与原异常，finally必定通知等待者。
    # 函数用途: 执行一次调用并保留可供主线程断言的结果。
    def run():
        try:
            results.append(action())
        except BaseException as error:
            errors.append(error)
        finally:
            done.set()

    thread = threading.Thread(target=run)
    thread.start()
    return thread, done, results, errors


# LLM: 准备控制、authority、操作锁、心跳写盘和Future均真实；只在原authority前用Event停住旧poll。
# 函数用途: 不健康发布必须等旧poll到安全边界，并保留已认领Future责任。
def test_unhealthy_publication_waits_for_existing_poll_boundary(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.control_commands import ConversationControlResult
    from agent_py_agent.agent.gateway_parts import control_operation_service as ops
    from agent_py_agent.agent.gateway_parts import multiprocess_control_worker as controls
    from agent_py_agent.agent.gateway_parts import multiprocess_worker as worker
    from agent_py_agent.agent.gateway_parts.multiprocess_worker_dispatch import WorkerRequestPump
    from agent_py_agent.tests.test_mp03_worker_survival import fd_count, readiness_case

    case = readiness_case(tmp_path, monkeypatch)
    pump = WorkerRequestPump(case.frozen, case.pool, 1)
    entered, release_poll, attempting_pause = threading.Event(), threading.Event(), threading.Event()
    effect_entered, release_effect, published = threading.Event(), threading.Event(), threading.Event()
    authority, pause = controls.control_worker_authority, pump.controls.pause_admission
    threads, effects = [], []
    before = fd_count()

    # LLM: Event只停在原认领校验之前，释放后原authorizer仍执行。
    # 函数用途: 选择旧poll持锁尚未完成的竞争窗口。
    def checked_authority(frozen, *, claiming):
        if claiming:
            entered.set()
            assert release_poll.wait(5)
        return authority(frozen, claiming=claiming)

    # LLM: 记录暂停尝试后仍调用原同步方法，不抢先设置暂停字段。
    # 函数用途: 通知主线程刷新已开始等待旧poll。
    def pausing():
        attempting_pause.set()
        pause()

    # LLM: 只替代领域返回；原控制认领、Future、心跳及关闭链均保留。
    # 函数用途: 保持一个已认领控制在途，以核对暂停不丢责任。
    def effect(_agent, _paths, command, _scope):
        effects.append(command.kind)
        effect_entered.set()
        assert release_effect.wait(5)
        return ConversationControlResult(command.kind, True, 'completed before pause acknowledgment')

    monkeypatch.setattr(controls, 'control_worker_authority', checked_authority)
    monkeypatch.setattr(pump.controls, 'pause_admission', pausing)
    monkeypatch.setattr(ops, 'execute_gateway_conversation_control', effect)
    try:
        poll = _background_action(pump.controls.poll)
        threads.append(poll[0])
        assert entered.wait(5)
        case.paths.state.write_text('{', encoding='utf-8')
        refresh = _background_action(lambda: worker._refresh_due_heartbeat(
            case.frozen.launch, 0, lambda _row: published.set(), pump=pump, report={}))
        threads.append(refresh[0])
        assert attempting_pause.wait(5)
        assert not published.is_set() and not refresh[1].is_set()
        release_poll.set()
        assert poll[1].wait(5) and refresh[1].wait(5) and effect_entered.wait(5)
        assert poll[2] == [1] and poll[3] == [] and refresh[3] == []
        assert published.is_set() and refresh[2][0][1] is False
        retained = dict(pump.controls._active)
        assert len(retained) == 1 and all(not future.done() for future in retained)
        assert pump.controls.poll() == 0
        assert pump.controls._active == retained and pump.inflight == 1
        assert effects == ['steer']
        release_effect.set()
    finally:
        release_poll.set()
        release_effect.set()
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
        pump.close()
    assert ops.read_gateway_control_operation(case.paths, case.receipt.operation_id).state == 'completed'
    assert pump.inflight == 0 and fd_count() - before == 0


# LLM: 真实writer在健康写前及写后通知两端被Event暂停；同一controls在两窗口都只能核收，不能提前认领。
# 函数用途: 证明健康读/登记/发表完成之前不恢复准入，且发表和unpause之间也没有错误认领。
def test_healthy_publication_precedes_unpause(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation.control_commands import ConversationControlResult
    from agent_py_agent.agent.gateway_parts import control_operation_service as ops
    from agent_py_agent.agent.gateway_parts import multiprocess_control_worker as controls
    from agent_py_agent.agent.gateway_parts import multiprocess_worker as worker
    from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
    from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap_ready import WorkerReadyError
    from agent_py_agent.agent.gateway_parts.multiprocess_worker_dispatch import WorkerRequestPump
    from agent_py_agent.tests.test_mp03_worker_survival import fd_count, readiness_case

    case = readiness_case(tmp_path, monkeypatch)
    pump = WorkerRequestPump(case.frozen, case.pool, 1)
    writing, allow_write, published, allow_resume = (threading.Event() for _ in range(4))
    original_write, effects, thread = worker.write_json_file_atomic, [], None
    before = fd_count()
    heartbeat = case.paths.root / 'workers' / f'w{case.frozen.launch.partition.index}.json'

    # LLM: Event释放后才执行原心跳writer，不伪造写成功。
    # 函数用途: 固定健康心跳实际发布前的竞争窗口。
    def write(path, row):
        writing.set()
        assert allow_write.wait(5)
        original_write(path, row)

    # LLM: 回调发生在原写盘之后；只阻塞恢复，不改变已发布内容。
    # 函数用途: 固定健康发表已完成但尚未unpause的窗口。
    def notice(_row):
        published.set()
        assert allow_resume.wait(5)

    # LLM: 仅记录真实泵交付的命令，不代写认领或终态。
    # 函数用途: 验证恢复后唯一控制才被执行。
    def effect(_agent, _paths, command, _scope):
        effects.append(command.kind)
        return ConversationControlResult(command.kind, True, 'healthy publication observed')

    monkeypatch.setattr(ops, 'execute_gateway_conversation_control', effect)
    try:
        case.paths.state.write_text('{', encoding='utf-8')
        assert worker._refresh_due_heartbeat(case.frozen.launch, 0, None, pump=pump)[1] is False
        unhealthy = heartbeat.read_bytes()
        write_json_file_atomic(case.paths.state, case.coordinator)
        monkeypatch.setattr(worker, 'write_json_file_atomic', write)
        refresh = _background_action(lambda: worker._refresh_due_heartbeat(
            case.frozen.launch, 0, notice, pump=pump))
        thread = refresh[0]
        assert writing.wait(5)
        assert heartbeat.read_bytes() == unhealthy and pump.controls.poll() == 0
        with pytest.raises(WorkerReadyError, match='worker_unhealthy'):
            controls.control_worker_authority(case.frozen, claiming=True)
        allow_write.set()
        assert published.wait(5)
        assert read_json_file(heartbeat)['ready'] is True
        assert pump.controls.poll() == 0 and effects == []
        assert ops.read_gateway_control_operation(case.paths, case.receipt.operation_id).state == 'prepared'
        allow_resume.set()
        assert refresh[1].wait(5) and refresh[3] == [] and refresh[2][0][1] is True
        assert pump.poll() == 1
    finally:
        allow_write.set()
        allow_resume.set()
        if thread is not None:
            thread.join(5)
            assert not thread.is_alive()
        pump.close()
    assert effects == ['steer']
    assert ops.read_gateway_control_operation(case.paths, case.receipt.operation_id).state == 'completed'
    assert fd_count() - before == 0


# LLM: 故障只发生在真实健康发表边界或真实registry读取；不替换authorizer，不以失败写盘解除暂停。
# 函数用途: 验证写失败/代次替换保留原错误、旧心跳及暂停，清理后FD仍归零。
@pytest.mark.parametrize('failure', ['heartbeat_write', 'generation_replaced'])
def test_failed_healthy_refresh_does_not_reopen_admission(tmp_path, monkeypatch, failure):
    from agent_py_agent.agent.gateway_parts import control_operation_service as ops
    from agent_py_agent.agent.gateway_parts import multiprocess_worker as worker
    from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
    from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap_ready import WorkerReadyError
    from agent_py_agent.agent.gateway_parts.multiprocess_worker_dispatch import WorkerRequestPump
    from agent_py_agent.tests.test_mp03_worker_survival import fd_count, readiness_case

    case = readiness_case(tmp_path, monkeypatch)
    pump = WorkerRequestPump(case.frozen, case.pool, 1)
    before = fd_count()
    original, cause = OSError('healthy-heartbeat-write-failed'), RuntimeError('retained publication cause')

    # LLM: 只在实际writer边界注入同一个异常及cause，不能伪装发表成功。
    # 函数用途: 复现健康心跳写失败。
    def failing_write(_path, _row):
        raise original from cause

    try:
        case.paths.state.write_text('{', encoding='utf-8')
        assert worker._refresh_due_heartbeat(case.frozen.launch, 0, None, pump=pump)[1] is False
        heartbeat = case.paths.root / 'workers' / f'w{case.frozen.launch.partition.index}.json'
        unhealthy = heartbeat.read_bytes()
        write_json_file_atomic(case.paths.state, case.coordinator)
        if failure == 'heartbeat_write':
            monkeypatch.setattr(worker, 'write_json_file_atomic', failing_write)
            expected = OSError
        else:
            registry_path = case.paths.root / 'workers/registry.json'
            registry = read_json_file(registry_path)
            write_json_file_atomic(registry_path, {**registry, 'c_run_id': 'replacement-generation'})
            expected = WorkerReadyError
        with pytest.raises(expected) as caught:
            worker._refresh_due_heartbeat(case.frozen.launch, 0, None, pump=pump)
        if failure == 'heartbeat_write':
            assert caught.value is original and caught.value.__cause__ is cause
        else:
            assert caught.value.reason == 'registry_generation_mismatch'
        assert heartbeat.read_bytes() == unhealthy and pump.controls.poll() == 0
        assert pump.controls._admission_paused and pump.inflight == 0
        assert ops.read_gateway_control_operation(case.paths, case.receipt.operation_id).state == 'prepared'
    finally:
        pump.close()
    assert fd_count() - before == 0
