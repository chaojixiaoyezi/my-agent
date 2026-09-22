"""通用有界调用与原模型准入组合；不冒充尚未接通的产品决策服务。"""
from __future__ import annotations

import threading
import time

import pytest

from agent_py_agent.agent.backends import bounded_call
from agent_py_agent.agent.backends.errors import ProviderTransientError
from agent_py_agent.agent.llm_scale import hot_path


@pytest.fixture(autouse=True)
def _admission(monkeypatch):
    monkeypatch.setenv("LLM_MAX_INFLIGHT", "2")
    monkeypatch.setenv("LLM_ADMISSION_WAIT_SECONDS", "0")
    hot_path.reset_hot_path_admission_for_test()
    yield
    hot_path.reset_hot_path_admission_for_test()


def test_timed_out_worker_keeps_admission_and_normal_call_can_continue():
    release = threading.Event()
    entered = threading.Event()
    resource = ("decision", "owner-one", "profile-one", "revision-one")
    worker = None

    def operation():
        nonlocal worker
        worker = threading.current_thread()
        with hot_path.global_llm_admission_slot(optional=True):
            entered.set()
            assert release.wait(3)
            return "迟到的建议"

    def optional_operation():
        with hot_path.global_llm_admission_slot(optional=True):
            pytest.fail("第二个决策挤占了普通模型名额")

    try:
        with pytest.raises(bounded_call.BoundedCallStillRunningError):
            bounded_call.call_with_deadline(operation, deadline=time.monotonic() + 0.1,
                                            resource_key=resource, optional=True)
        assert entered.is_set() and worker is not None and worker.is_alive()
        assert hot_path._limiter().in_flight() == 1
        with pytest.raises(bounded_call.BoundedCallBusyError) as caught:
            bounded_call.call_with_deadline(operation, deadline=time.monotonic() + 1,
                                            resource_key=resource, optional=True)
        assert caught.value.reason == "resource_busy"
        with pytest.raises(ProviderTransientError):
            bounded_call.call_with_deadline(optional_operation, deadline=time.monotonic() + 1,
                                            resource_key=("decision", "owner-two"), optional=True)
        with hot_path.global_llm_admission_slot():
            assert hot_path._limiter().in_flight() == 2
    finally:
        release.set()
        if worker is not None:
            worker.join(3)
    assert not worker.is_alive()
    assert hot_path._limiter().in_flight() == 0
    assert bounded_call.call_with_deadline(lambda: "新请求", deadline=time.monotonic() + 1,
                                          resource_key=resource, optional=True) == "新请求"


def test_admission_failure_does_not_run_optional_business(monkeypatch):
    monkeypatch.setenv("LLM_MAX_INFLIGHT", "1")
    executed = []

    def operation():
        with hot_path.global_llm_admission_slot(optional=True):
            executed.append(True)

    with pytest.raises(ProviderTransientError):
        bounded_call.call_with_deadline(operation, deadline=time.monotonic() + 1,
                                        resource_key=("decision", "single-capacity"), optional=True)
    assert not executed
    with hot_path.global_llm_admission_slot():
        assert hot_path._limiter().in_flight() == 1
