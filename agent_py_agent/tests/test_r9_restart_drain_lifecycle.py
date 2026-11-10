"""排空取消须先于不可逆关闭；进程退出不能替代同代责任核收。"""
import os
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.concurrency import restart_gate
from agent_py_agent.agent.conversation.background_shutdown import (
    BackgroundShutdown,
    BackgroundSlice,
)
from agent_py_agent.agent.gateway_parts import restart_service as service
from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.cli import gateway_restart_handover as handover


@pytest.fixture(autouse=True)
def gates():
    service.reset_restart_drain_state()
    yield
    service.reset_restart_drain_state()


def context(tmp_path, monkeypatch):
    paths = gateway_paths_from_root(tmp_path / 'gateway')
    paths.root.mkdir(parents=True)
    identity = {'pid': os.getpid(), 'start_time': 'original', 'host_id': 'test'}
    config = SimpleNamespace(gateway_restart_turn_wait_seconds=0, gateway_restart_drain_timeout_seconds=.01,
                             gateway_restart_force_on_drain_timeout=False)
    result = SimpleNamespace(paths=paths, process_identity=identity, process_started_at=1.,
                             background_shutdown=BackgroundShutdown(),
                             agent=SimpleNamespace(config=config, home_paths=SimpleNamespace(root=tmp_path)))
    monkeypatch.setattr(handover, 'log_gateway_event', lambda *a: None)
    return result


@pytest.mark.parametrize('kind', ['future', 'finished_unknown', 'unreadable'])
def test_unsettled_background_cancels_before_irreversible_cleanup(tmp_path, monkeypatch, kind):
    ctx = context(tmp_path, monkeypatch)
    background = ctx.background_shutdown
    future = Future()
    if kind == 'future':
        background.bind(SimpleNamespace(_inflight={'exact': future}))
    elif kind == 'finished_unknown':
        item = BackgroundSlice(background, None)
        item.finished = True
        item.claims.append(SimpleNamespace(claim_id='original-claim'))
        monkeypatch.setattr(item, '_claim_blocker', lambda claim: 'background_operation_unsettled')
        background.slices.add(item)
    else:
        def unreadable():
            raise OSError('private source must not be printed')
        monkeypatch.setattr(background, 'blockers', unreadable)
    assert background.active_slice_count() == 0
    service.submit_restart_request(ctx.paths, target_pid=os.getpid(), requester={}, reason='',
                                   cooldown_seconds=0, operation='stop_for_update')
    assert handover.drain_for_requested_restart(ctx, ctx.process_identity) is None
    state = read_json_file(ctx.paths.state)['restart_drain']
    assert state['phase'] == 'cancelled' and state['service_cleanup_started'] is False
    assert state['reason'] == 'background_unsettled' and state['unresolved_operation_refs']
    assert not background.stopping and not future.done()
    assert not restart_gate.tool_gate_closed()
    assert not service.restart_completed_path(ctx.paths).exists()
    # 原服务仍可接下一片；并未调用 stop/interrupt 或清旧未结责任。
    if kind != 'finished_unknown':
        with background.observe(None):
            pass


@pytest.mark.parametrize('proof', ['clean', 'missing', 'incomplete', 'wrong_generation'])
def test_update_requires_exact_cleanup_proof_even_after_process_exit(tmp_path, monkeypatch, capsys, proof):
    ctx = context(tmp_path, monkeypatch)
    pid = ctx.process_identity['pid']
    monkeypatch.setattr(handover, 'build_process_identity', lambda p: dict(ctx.process_identity))
    monkeypatch.setattr(handover, 'is_pid_alive', lambda p: False)
    original = handover.submit_restart_request
    def submit(*args, **kwargs):
        report = original(*args, **kwargs)
        if proof != 'missing':
            marker = service.mark_restart_drained(ctx.paths, report['request'], old_pid=pid,
                old_process_started_at=1., active_turns_at_exit=0,
                old_process_identity=ctx.process_identity,
                cleanup_result={'status': 'clean_drain' if proof != 'incomplete' else 'incomplete',
                                'handover_allowed': proof != 'incomplete'})
            if proof == 'wrong_generation':
                marker['old_process_identity'] = {**ctx.process_identity, 'start_time': 'reused'}
                write_json_file_atomic(service.restart_completed_path(ctx.paths), marker)
        return report
    monkeypatch.setattr(handover, 'submit_restart_request', submit)
    assert handover.safe_stop_for_update(ctx.agent, ctx.paths, pid) == (0 if proof == 'clean' else 2)
    output = capsys.readouterr()
    if proof != 'clean':
        assert 'my-agent gateway start' in output.err
        assert '原实例保持运行' not in output.err


def test_irreversible_failure_is_not_reported_as_service_preserved(tmp_path, monkeypatch, capsys):
    ctx = context(tmp_path, monkeypatch)
    request = service.submit_restart_request(ctx.paths, target_pid=os.getpid(), requester={}, reason='',
                                             cooldown_seconds=0)['request']
    marker = service.mark_restart_drained(ctx.paths, request, old_pid=os.getpid(), old_process_started_at=1.,
        active_turns_at_exit=0, old_process_identity=ctx.process_identity, cleanup_result={'status': 'pending'})
    write_json_file_atomic(ctx.paths.state, {'pid': os.getpid(), 'process_identity': ctx.process_identity, 'started_at': 1.})
    # 最后核收才到达的真实未完成 future；最终硬门不能靠前置检查豁免。
    cleanup = {'drain_complete': False, 'alive_threads': [],
               'background_blockers': [{'reason': 'background_future_unsettled', 'ref': 'original_future'}]}
    result = handover.finalize_restart_cleanup(ctx, marker, cleanup)
    assert result['handover_allowed'] is False
    state = read_json_file(ctx.paths.state)
    assert state['restart_drain']['phase'] == 'cleanup_failed'
    assert service.consume_restart_marker(ctx.paths, old_pid_alive=lambda p: False) is None
    assert handover._wait_for_cli_restart(ctx.paths, os.getpid(), request['request_id'], 1) == 2
    output = capsys.readouterr().err
    assert '不可逆' in output and 'my-agent gateway start' in output
    assert '照常运行' not in output


def test_cancelled_future_drain_keeps_service_then_exact_completion_allows_retry(tmp_path, monkeypatch):
    import threading

    ctx = context(tmp_path, monkeypatch)
    future = Future()
    release = threading.Event()
    worker = threading.Thread(target=lambda: (release.wait(5), future.set_result(None)))
    ctx.background_shutdown.bind(SimpleNamespace(_inflight={'original': future}))
    worker.start()
    try:
        service.submit_restart_request(ctx.paths, target_pid=os.getpid(), requester={}, reason='',
                                       cooldown_seconds=0, operation='stop_for_update')
        assert handover.drain_for_requested_restart(ctx, ctx.process_identity) is None
        assert worker.is_alive() and not ctx.background_shutdown.stopping
        release.set()
        worker.join(5)
        assert not worker.is_alive() and future.done()
        service.submit_restart_request(ctx.paths, target_pid=os.getpid(), requester={}, reason='',
                                       cooldown_seconds=0, operation='stop_for_update')
        report = handover.drain_for_requested_restart(ctx, ctx.process_identity)
        assert report['restart']['operation'] == 'stop_for_update'
        assert read_json_file(service.restart_completed_path(ctx.paths))['cleanup_result'] == {'status': 'pending'}
        # preflight 仅准许进入 cleanup，绝非已签最终 clean。
        assert service.consume_restart_marker(ctx.paths, old_pid_alive=lambda p: False) is None
    finally:
        release.set()
        worker.join(5)
