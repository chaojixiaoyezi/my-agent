"""真实隔离 Curator 与活心跳：join 中断不能交回或退役执行权。"""
import threading

import pytest

from agent_py_agent.agent.memory_store import curator as curator_module
from agent_py_agent.agent.memory_store import curator_lease
from agent_py_agent.tests.test_agents_cleanup_review33 import _setup
from agent_py_agent.tests.test_agents_phase0_guard import _curator


class _JoinProbe:
    def __init__(self, curator, join_error, primary):
        self.curator, self.join_error, self.primary = curator, join_error, primary
        self.entered, self.unblock = threading.Event(), threading.Event()
        self.threads = []
        self.real_renew, self.real_join = curator_lease.renew_curator_lease, threading.Thread.join

    def renew(self, store, lease_id, seconds):
        if threading.current_thread().name == "curator-lease-heartbeat":
            self.threads.append(threading.current_thread())
            self.entered.set()
            assert self.unblock.wait(10)
        return self.real_renew(store, lease_id, seconds)

    def execute(self, context):
        assert self.entered.wait(5)
        if self.primary is not None:
            raise self.primary
        return self.curator._result(status="succeeded", reason=context.reason)

    def join(self, thread, *args, **kwargs):
        if thread.name == "curator-lease-heartbeat":
            assert thread.is_alive()
            raise self.join_error
        return self.real_join(thread, *args, **kwargs)

    def close(self):
        self.unblock.set()
        for thread in self.threads:
            self.real_join(thread, 5)
            assert not thread.is_alive()


@pytest.mark.parametrize("join_type", [KeyboardInterrupt, RuntimeError])
@pytest.mark.parametrize("body_fails", [False, True])
def test_curator_public_run_preserves_primary_and_executing_when_join_unconfirmed(tmp_path, monkeypatch, join_type, body_fails):
    env = _setup(tmp_path)
    curator = _curator(tmp_path, env.conversations, env.service.candidates)
    join_error = join_type("synthetic join interruption")
    primary = RuntimeError("synthetic body failure") if body_fails else None
    probe = _JoinProbe(curator, join_error, primary)
    monkeypatch.setattr(curator_module, "_lease_seconds", lambda _config: 0)
    monkeypatch.setattr(curator_lease, "renew_curator_lease", probe.renew)
    monkeypatch.setattr(threading.Thread, "join", lambda thread, *args, **kwargs: probe.join(thread, *args, **kwargs))
    monkeypatch.setattr(curator, "_execute", probe.execute)
    try:
        with pytest.raises(BaseException) as caught:
            curator.run(reason="admin")
        assert caught.value is (primary if body_fails else join_error)
        assert caught.value.lease_heartbeat_stop_error_class == join_type.__name__
        assert probe.threads and all(thread.is_alive() for thread in probe.threads)
        assert curator.state_store.load().active_lease.get("phase") == "executing"
    finally:
        probe.close()


@pytest.mark.parametrize("body_fails", [False, True])
def test_stop_signal_interruption_keeps_original_and_never_releases(tmp_path, monkeypatch, body_fails):
    from types import SimpleNamespace

    from agent_py_agent.agent.memory_store import agents_migration as migration
    from agent_py_agent.tests.test_agents_cleanup_review33 import _apply

    env = _setup(tmp_path)
    original = curator_lease._LeaseHeartbeat
    workers = []
    body_error, stop_error = RuntimeError("body error"), KeyboardInterrupt("stop request interruption")
    run = migration._run_cleanup

    def raise_stop():
        raise stop_error

    def heartbeat(store, lease_id, seconds):
        worker = original(store, lease_id, seconds)
        real_stop = worker.stop
        workers.append((worker, real_stop))
        worker.stop = SimpleNamespace(wait=real_stop.wait, set=raise_stop)
        return worker

    def body(context):
        result = run(context)
        if body_fails:
            raise body_error
        return result

    monkeypatch.setattr(curator_lease, "_LeaseHeartbeat", heartbeat)
    monkeypatch.setattr(migration, "_run_cleanup", body)
    try:
        with pytest.raises(BaseException) as caught:
            _apply(env)
        assert caught.value is (body_error if body_fails else stop_error)
        assert caught.value.lease_heartbeat_stop_error_class == "KeyboardInterrupt"
        assert workers and all(w.thread.is_alive() and not s.is_set() for w, s in workers)
        assert env.store.load().active_lease.get("phase") == "executing"
    finally:
        for worker, real_stop in workers:
            real_stop.set()
            worker.thread.join(5)
            assert not worker.thread.is_alive()
