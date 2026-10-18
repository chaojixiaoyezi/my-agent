"""Worker正式组合根接入P4/P5；模型替身不当真实供应商验收。"""
import os
import threading

from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
from agent_py_agent.agent.gateway_parts import multiprocess_worker as worker
from agent_py_agent.agent.gateway_parts import request_worker
from agent_py_agent.agent.gateway_parts.io import read_json_file, write_json_file_atomic
from agent_py_agent.agent.gateway_parts.multiprocess_worker_dispatch import WorkerRequestPump
from agent_py_agent.cli.gateway_worker import dispatch_worker_entry
from agent_py_agent.tests.test_gateway_multiprocess_worker_dispatch import (  # noqa: F401
    enqueue,
    setup,
)


def test_cli_keeps_original_config_for_worker_construction(setup, monkeypatch, tmp_path):  # noqa: F811
    launch = setup.frozen.launch
    monkeypatch.setattr(mode, '_WORKER_PARTITION', None)
    for key, value in mode.worker_environment(launch, {}).items():
        monkeypatch.setenv(key, value)
    seen = {}
    def run(received, notice, **options):
        seen.update(options)
        assert received == launch
        return 0
    monkeypatch.setattr(worker, 'run_worker', run)
    config = tmp_path / 'worker.yaml'
    assert dispatch_worker_entry(['--config', str(config), 'gateway', 'worker',
        '--index', str(launch.partition.index), '--of', '2', '--run', 'c-p5',
        '--gateway-root', str(launch.gateway_root), '--owner-root', str(launch.owner_root),
        '--parent-pid', str(launch.parent_pid)]) == 0
    assert seen.get('config_path') == str(config)


def test_worker_entry_consumes_pending_and_reports_actual_inflight(setup, monkeypatch):  # noqa: F811
    path = enqueue(setup, setup.request)
    stop = threading.Event()
    entered, release = threading.Event(), threading.Event()
    pump = WorkerRequestPump(setup.frozen, setup.pool, 1)
    created, notices = [], []
    def factory(launch, config_path):
        created.append(launch)
        return pump
    def provider(context):
        entered.set()
        assert release.wait(5)
        return {'id': context.request_id, 'ok': True, 'status': 'done'}
    def notice(payload):
        notices.append(payload)
        if payload['inflight'] == 1:
            write_json_file_atomic(setup.frozen.launch.gateway_root / 'workers' / 'control.json',
                                  {'c_run_id': 'c-p5', 'drain': True})
            release.set()
        if payload.get('loop_health') == 'draining' and pump.inflight == 0:
            stop.set()
    monkeypatch.setattr(worker, 'create_request_pump', factory, raising=False)
    monkeypatch.setattr(worker, '_install_stop_handlers', lambda _: {})
    from types import SimpleNamespace
    monkeypatch.setattr(worker, 'threading', SimpleNamespace(Event=lambda: stop))
    monkeypatch.setattr(worker, '_arm_parent_exit', lambda _: '')
    monkeypatch.setattr(worker.os, 'getppid', lambda: setup.frozen.launch.parent_pid)
    monkeypatch.setattr(worker, 'prepare_worker_readiness', lambda _: {'ready': True})
    monkeypatch.setattr(worker, 'WORKER_HEARTBEAT_SECONDS', 0.01)
    monkeypatch.setattr(worker, 'WORKER_PARENT_POLL_SECONDS', 0.01)
    monkeypatch.setattr(request_worker, '_process_claimed_gateway_request', provider)
    timer = threading.Timer(3, stop.set)
    timer.start()
    try:
        assert worker.run_worker(setup.frozen.launch, notice) == 0
    finally:
        release.set()
        timer.cancel()
        timer.join()
        pump.close()
    assert created == [setup.frozen.launch]
    assert entered.is_set()
    assert any(row['inflight'] == 1 for row in notices)
    assert read_json_file(setup.paths.terminal / path.name)['status'] == 'done'
    assert not any(row.get('loop_health') == 'drained' for row in notices)


def test_heartbeat_keeps_observed_inflight_count(setup):  # noqa: F811
    from agent_py_agent.agent.gateway_parts.multiprocess_state import worker_heartbeat
    heartbeat = worker_heartbeat(setup.frozen.launch, {'ready': True, 'inflight': 2}, 123.0)
    assert heartbeat['inflight'] == 2
    assert heartbeat['pid'] == os.getpid()
