"""原有界线程错误出处只读证明；不调用旧Context，不创建第二调用账或权限。"""
from __future__ import annotations

import contextvars
import copy
import threading
import time
from contextlib import contextmanager

import pytest

from agent_py_agent.agent.backends import bounded_call as bounded


# LLM: 故障只保真实Thread在原run返回后的尾部；最终join实际线程并沿原_CALLS回收，不修改退出观察。
# 函数用途: 制造有结果但仍未物理退出的原调用，保存其原error/Call供来源正反例核对。
@contextmanager
def held_call(monkeypatch, *, operation=lambda: "actual-result", key="origin-call"):
    tail, release = threading.Event(), threading.Event()
    original_run = threading.Thread.run
    seen = {}

    def run(worker):
        result = original_run(worker)
        if worker.name.startswith("bounded-call:") and not seen:
            seen["worker"] = worker
            tail.set()
            assert release.wait(3)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "run", run)
        try:
            with pytest.raises(bounded.BoundedCallExitUnconfirmedError) as caught:
                bounded.call_with_deadline(operation, deadline=time.monotonic() + .08,
                                           resource_key=key, wait_for_exit=True)
            assert tail.is_set() and seen["worker"].is_alive()
            call = next(c for c in bounded.bounded_call_drain_snapshot() if c.call_id == caught.value.call_id)
            yield caught.value, call
        finally:
            release.set()
            worker = seen.get("worker")
            if worker is not None:
                worker.join(2)
                assert not worker.is_alive()
            with bounded._CALL_LOCK:
                bounded._reap_calls_locked()


def test_only_original_self_issued_error_can_read_same_captured_context(monkeypatch):
    marker = contextvars.ContextVar("bounded-origin-marker")
    value = object()
    marker.set(value)
    with held_call(monkeypatch) as (error, call):
        origin = bounded.bounded_call_error_origin(error)
        assert type(origin) is contextvars.Context and origin.get(marker) is value
        assert call.retained() and bounded.bounded_call_drain_snapshot() == (call,)
        marker.set(object())
        assert bounded.bounded_call_error_origin(error) is origin and origin.get(marker) is value
    assert bounded.bounded_call_error_origin(error) is None


@pytest.mark.parametrize("fault", ["error_copy", "same_id", "call_copy", "context_copy", "reason", "id"])
def test_copied_or_forged_origin_cannot_claim_actual_call(monkeypatch, fault):
    with held_call(monkeypatch) as (error, call):
        tested = error
        if fault == "error_copy":
            tested = type(error).__new__(type(error))
            tested.__dict__.update(error.__dict__)
        elif fault == "same_id":
            tested = bounded.BoundedCallExitUnconfirmedError(error.call_id, error.reason)
        elif fault == "call_copy":
            tested._call_origin = copy.copy(call)
        elif fault == "context_copy":
            call.context = call.context.copy()
        elif fault == "reason":
            tested.reason = "worker_identity_changed"
        else:
            tested.call_id = "bounded-call:foreign"
        assert bounded.bounded_call_error_origin(tested) is None


def test_callback_old_error_is_never_rebound_to_new_call(monkeypatch):
    with held_call(monkeypatch, key="old-origin") as (old, old_call):
        old_origin = bounded.bounded_call_error_origin(old)

        def fail():
            raise old

        # 新调用实际线程正常退出；finally不能把业务抛来的旧错误盖成新调用出处。
        with pytest.raises(bounded.BoundedCallExitUnconfirmedError) as caught:
            bounded.call_with_deadline(fail, deadline=time.monotonic() + 1,
                                       resource_key="new-origin", wait_for_exit=True)
        assert caught.value is old
        assert bounded.bounded_call_error_origin(old) is old_origin
        assert old._call_origin is old_call
    assert bounded.bounded_call_error_origin(old) is None
