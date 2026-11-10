"""可逆原片收口：不中止持久工作，超时保持旧服务准入与未知门。"""
import os
from dataclasses import replace

from agent_py_agent.agent.concurrency import restart_gate
from agent_py_agent.agent.gateway_parts import restart_service
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.cli import gateway_restart_handover as handover
from agent_py_agent.tests.test_gateway_background_slice_shutdown import _release, _running


def _request(ctx, monkeypatch):
    ctx.context = replace(ctx.context, paths=gateway_paths(ctx.agent),
                          process_identity={'pid': os.getpid(), 'start_time': 'isolated'})
    ctx.context.paths.root.mkdir(parents=True, exist_ok=True)
    ctx.agent.config.gateway_restart_turn_wait_seconds = 0
    ctx.agent.config.gateway_restart_drain_timeout_seconds = .1
    monkeypatch.setattr(handover, 'log_gateway_event', lambda *a: None)
    restart_service.submit_restart_request(ctx.context.paths, target_pid=os.getpid(), requester={}, reason='',
        cooldown_seconds=0, operation='stop_for_update')


def test_restart_parks_original_background_claim_before_irreversible_cleanup(tmp_path, monkeypatch):
    ctx = _running(tmp_path)
    try:
        _request(ctx, monkeypatch)
        original = ctx.store.claims.load(ctx.thread.thread_id)
        report = handover.drain_for_requested_restart(ctx.context, ctx.context.process_identity)
        assert report is not None
        ctx.future.result(timeout=1)
        current = ctx.store.claims.load(ctx.thread.thread_id)
        assert current['claim_id'] == original['claim_id'] and current['status'] == 'cancelled'
        assert ctx.store.tasks.link_report(ctx.thread.thread_id, 'original-work')[0].status == 'active'
        assert ctx.store.wakes.pending()
        assert not ctx.context.background_shutdown.stopping
        assert ctx.context.background_shutdown.blockers() == []
    finally:
        restart_service.reset_restart_drain_state()
        _release(ctx)


def test_uncooperative_park_cancels_upgrade_and_reopens_only_new_slice_admission(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import background_shutdown
    monkeypatch.setattr(background_shutdown, 'SHUTDOWN_WAIT_SECONDS', .05)
    ctx = _running(tmp_path, cooperative=False)
    try:
        _request(ctx, monkeypatch)
        assert handover.drain_for_requested_restart(ctx.context, ctx.context.process_identity) is None
        assert not ctx.context.background_shutdown.stopping and not restart_gate.tool_gate_closed()
        assert not ctx.future.done()
        with ctx.context.background_shutdown.observe(None):
            pass
        assert ctx.store.tasks.link_report(ctx.thread.thread_id, 'original-work')[0].status == 'active'
        assert ctx.store.wakes.pending()
    finally:
        restart_service.reset_restart_drain_state()
        _release(ctx)


def test_late_claim_after_cancelled_park_still_consumes_original_slice_exit_request(monkeypatch):
    import threading

    import pytest

    from agent_py_agent.agent.conversation import background_shutdown as shutdown
    monkeypatch.setattr(shutdown, 'SHUTDOWN_WAIT_SECONDS', .03)
    control = shutdown.BackgroundShutdown()
    entered, release = threading.Event(), threading.Event()
    rejected = []
    def original():
        with control.observe(None):
            entered.set()
            assert release.wait(5)
            try:
                shutdown.check_background_admission()
            except InterruptedError:
                rejected.append(True)
    worker = threading.Thread(target=original)
    worker.start()
    try:
        assert entered.wait(5)
        assert control.pause_for_restart()
        assert not control.restart_paused and not control.stopping
        # 旧片的退出意图不撤销，后继新片的准入已恢复。
        with control.observe(None):
            shutdown.check_background_admission()
        release.set()
        worker.join(5)
        assert not worker.is_alive() and rejected == [True]
        assert control.blockers() == []
        control.stop()
        control.resume_after_restart_cancel()
        with pytest.raises(InterruptedError):
            with control.observe(None):
                pass
    finally:
        release.set()
        worker.join(5)


def test_finished_unknown_refuses_park_without_interrupting_other_original_work(monkeypatch):
    from agent_py_agent.agent.conversation import background_shutdown as shutdown
    control = shutdown.BackgroundShutdown()
    item = shutdown.BackgroundSlice(control, None)
    item.finished = True
    control.slices.add(item)
    unknown = {'reason': 'background_operation_unsettled', 'ref': 'exact-original-claim'}
    monkeypatch.setattr(item, 'blockers', lambda: [unknown])
    monkeypatch.setattr(item, 'interrupt', lambda: (_ for _ in ()).throw(AssertionError('UNKNOWN cannot be interrupted away')))
    assert control.pause_for_restart() == [unknown]
    assert not control.restart_paused and not control.stopping
    assert item in control.slices
