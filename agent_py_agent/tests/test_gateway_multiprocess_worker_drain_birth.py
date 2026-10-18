"""原W三池真实Thread出生责任，不把标准库清表当已退出。"""
import threading

import pytest

from agent_py_agent.agent.gateway_parts.multiprocess_worker_dispatch import WorkerRequestPump
from agent_py_agent.agent.gateway_parts.multiprocess_worker_drain import (
    worker_pump_drain_assessment,
)
from agent_py_agent.tests.test_gateway_multiprocess_control_worker import prepared_case


@pytest.mark.parametrize('index', [0, 1, 2])
def test_original_worker_pool_clear_keeps_actual_held_thread(tmp_path, monkeypatch, index):
    _paths, _receipt, _scope, frozen, pool, _owners = prepared_case(tmp_path, monkeypatch)
    pump = WorkerRequestPump(frozen, pool, 1)
    executor = (pump._executor, pump.controls._executor, pump.controls._stop_executor)[index]
    entered, release = threading.Event(), threading.Event()

    def hold():
        entered.set()
        release.wait(5)

    future = executor.submit(hold)
    try:
        assert entered.wait(3)
        with executor._shutdown_lock:
            original = tuple(executor._threads)
            executor._threads.clear()
        assert len(original) == 1 and original[0].is_alive()
        pump.stop_claiming()
        assessment = worker_pump_drain_assessment(pump)
        assert original[0].is_alive() and not future.done()
        assert any(b.item_ref == f'original-pool/{index}' and b.reason in {'running', 'unknown'}
                   for b in assessment.blockers), assessment
        release.set()
        future.result(3)
        original[0].join(3)
        after = worker_pump_drain_assessment(pump)
        assert not any(b.item_ref == f'original-pool/{index}' for b in after.blockers)
    finally:
        release.set()
        with executor._shutdown_lock:
            executor._threads.update(original)
        pump.close()
