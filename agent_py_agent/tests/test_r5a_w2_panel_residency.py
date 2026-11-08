"""面板展示可重建缓存有界，合并输入及 worker 不延长请求上下文寿命。"""
from __future__ import annotations

import gc
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common import cache_capacity
from agent_py_agent.agent.plugin_channel import PluginChannelRevoked
from agent_py_agent.agent.plugin_display.service import (
    MAX_REQUESTED_PANEL_COUNT,
    DisplayWiring,
    PanelQuery,
    PluginDisplayService,
)
from agent_py_agent.tests.test_plugin_display_service import _Harness, _installation


@pytest.fixture(autouse=True)
def small_capacity(monkeypatch):
    monkeypatch.setattr(cache_capacity, "_SLOTS", dict(cache_capacity._SLOTS))
    _configure(2, 2)


def _configure(owners, sessions):
    cache_capacity.configure_from_config(SimpleNamespace(
        gateway_owner_inflight_limit=owners, gateway_user_inflight_limit=sessions,
        background_threads_per_owner=0, background_admin_threads_per_owner=0,
    ))


@pytest.fixture
def harness():
    h = _Harness()
    try:
        yield h
    finally:
        h.service.close()


def _query(h, thread, *, owner="owner", phase="tool"):
    activity = {"main_activity": {"phase": phase}} if phase else {}
    return h.service.panels(PanelQuery(
        SimpleNamespace(identity=owner), owner, thread, activity, (("pet", "line"),),
    ))[0]


@pytest.mark.parametrize("owners,sessions", [(0, 0), (2, 3)])
def test_two_thousand_session_results_obey_derived_capacity(harness, monkeypatch, owners, sessions):
    _configure(owners, sessions)
    limit = cache_capacity.resident_owners(1) * cache_capacity.owner_sessions(1) * MAX_REQUESTED_PANEL_COUNT
    # 合法文本描述超过 8KiB，避免只用空结果掩盖真实驻留成本；不启动插件子进程。
    monkeypatch.setattr(harness.service, "_render", lambda *_: {"lines": ["文" * 200] * 16})
    for index in range(2000):
        assert _query(harness, str(index))["state"] == "loading"
        harness.executor.run_all()
        assert len(harness.service._results) <= limit
    assert len(harness.service._results) == limit
    assert _query(harness, "1999")["state"] == "ready"
    assert _query(harness, "0")["state"] == "loading"
    harness.executor.run_all()
    assert _query(harness, "0")["state"] == "ready"


def test_result_hits_promote_lru_and_eviction_requires_real_rerender(harness):
    limit = cache_capacity.resident_owners(1) * cache_capacity.owner_sessions(1) * MAX_REQUESTED_PANEL_COUNT
    for index in range(limit):
        _query(harness, str(index))
        harness.executor.run_all()
    calls = len(harness.plugins[0].calls)
    assert _query(harness, "0")["state"] == "ready"
    assert not harness.executor.jobs and len(harness.plugins[0].calls) == calls
    _query(harness, "new")
    harness.executor.run_all()
    assert _query(harness, "0")["state"] == "ready"
    assert _query(harness, "1")["state"] == "loading"
    assert len(harness.plugins[0].calls) == calls + 1
    harness.executor.run_all()
    assert _query(harness, "1")["state"] == "ready"
    assert len(harness.plugins[0].calls) == calls + 2


@pytest.mark.parametrize("owners,sessions", [(0, 0), (2, 3)])
def test_pending_is_bounded_and_coalescing_promotes_latest_input(harness, owners, sessions):
    _configure(owners, sessions)
    limit = cache_capacity.owner_sessions(1) * MAX_REQUESTED_PANEL_COUNT
    for index in range(2000):
        assert _query(harness, str(index))["state"] == "loading"
        assert len(harness.service._slots[("owner", "act-1")].pending) <= limit
    slot = harness.service._slots[("owner", "act-1")]
    assert slot.running and len(slot.pending) == limit and len(harness.executor.jobs) == 1
    hot, cold = str(2000 - limit), str(2001 - limit)
    _query(harness, hot, phase="waiting_permission")
    _query(harness, "new")
    assert (hot, "line") in slot.pending and (cold, "line") not in slot.pending
    harness.executor.run_all()
    assert not slot.running and not slot.pending
    assert len(harness.plugins[0].calls) == limit
    assert _query(harness, hot, phase="waiting_permission")["display"]["lines"] == ["state=waiting"]


def test_identical_session_and_activation_do_not_share_owner_results(harness):
    _query(harness, "same", owner="first", phase="tool")
    _query(harness, "same", owner="second", phase=None)
    harness.executor.run_all()
    assert _query(harness, "same", owner="first")["display"]["lines"] == ["state=working"]
    assert _query(harness, "same", owner="second", phase=None)["display"]["lines"] == ["state=idle"]
    assert len(harness.plugins) == 2
    assert set(harness.service._connections) == {("first", "act-1"), ("second", "act-1")}


def test_eviction_does_not_bypass_error_backoff_or_activation_fence(harness):
    _query(harness, "hot")
    harness.executor.run_all()
    harness.plugins[0].fail = ValueError("bad display")
    _query(harness, "hot", phase="waiting_permission")
    harness.executor.run_all()
    calls = len(harness.plugins[0].calls)
    assert _query(harness, "hot", phase="waiting_permission")["state"] == "error"
    assert not harness.executor.jobs and len(harness.plugins[0].calls) == calls
    harness.now += 6
    harness.plugins[0].fail = None
    _query(harness, "hot", phase="waiting_permission")
    harness.executor.run_all()
    assert _query(harness, "hot", phase="waiting_permission")["state"] == "ready"
    harness.rows = [_installation("act-2")]
    assert _query(harness, "hot")["state"] == "loading"
    assert all(key[1] == "act-2" for key in harness.service._results)
    harness.executor.run_all()
    assert _query(harness, "hot")["state"] == "ready"
    harness.rows = []
    assert _query(harness, "hot")["state"] == "unavailable"
    assert not harness.service._results and not harness.service._slots


def test_each_coalesced_job_uses_its_own_context(harness, monkeypatch):
    identity = ContextVar("panel-request", default=None)
    observed = []
    render = harness.service._render

    def record(connection, job, slot):
        observed.append((job.thread_key, identity.get()))
        return render(connection, job, slot)

    monkeypatch.setattr(harness.service, "_render", record)
    for thread, value in (("first", "old"), ("second", "second-run"), ("first", "latest-run")):
        token = identity.set(value)
        try:
            _query(harness, thread)
        finally:
            identity.reset(token)
    token = identity.set("draining-thread")
    try:
        harness.executor.run_all()
        assert identity.get() == "draining-thread"
    finally:
        identity.reset(token)
    assert observed == [("second", "second-run"), ("first", "latest-run")]


class _Request:
    pass


def test_evicted_and_consumed_pending_contexts_are_released(harness):
    request = ContextVar("pending-request", default=None)
    references = []
    limit = cache_capacity.owner_sessions(1) * MAX_REQUESTED_PANEL_COUNT
    for index in range(limit + 1):
        item = _Request()
        references.append(weakref.ref(item))
        token = request.set(item)
        try:
            _query(harness, str(index))
        finally:
            request.reset(token)
        del item
    gc.collect()
    assert references[0]() is None and references[-1]() is not None
    harness.executor.run_all()
    gc.collect()
    assert all(reference() is None for reference in references)


@pytest.mark.parametrize("outcome", ["ready", "error", "revoked"])
def test_finished_worker_does_not_retain_request_context(monkeypatch, outcome):
    executor = ThreadPoolExecutor(max_workers=1)
    service = PluginDisplayService(wiring=DisplayWiring(
        installations=lambda _: (_installation(),), executor=executor,
    ))
    request = ContextVar("worker-request", default=None)
    entered, release = threading.Event(), threading.Event()
    observed = []

    def render(*_):
        observed.append(request.get() is not None)
        entered.set()
        assert release.wait(timeout=10)
        if outcome == "error":
            raise ValueError("bad display")
        if outcome == "revoked":
            raise PluginChannelRevoked("revoked")
        return {"lines": ["ready"]}

    monkeypatch.setattr(service, "_render", render)
    item = _Request()
    reference = weakref.ref(item)
    token = request.set(item)
    try:
        service.panels(PanelQuery(object(), "owner", "thread", {}, (("pet", "line"),)))
    finally:
        request.reset(token)
    del item
    try:
        assert entered.wait(timeout=10)
        release.set()
        assert executor.submit(lambda: request.get()).result(timeout=10) is None
        gc.collect()
        assert observed == [True] and reference() is None
    finally:
        release.set()
        service.close()
        executor.shutdown(wait=True)


def test_pending_eviction_keeps_the_executing_request(monkeypatch):
    executor = ThreadPoolExecutor(max_workers=1)
    service = PluginDisplayService(wiring=DisplayWiring(
        installations=lambda _: (_installation(),), executor=executor,
    ))
    entered, release = threading.Event(), threading.Event()
    rendered = []

    def render(_connection, job, _slot):
        if job.thread_key == "executing":
            entered.set()
            assert release.wait(timeout=10)
        rendered.append(job.thread_key)
        return {"lines": [job.thread_key]}

    monkeypatch.setattr(service, "_render", render)

    def query(thread):
        return service.panels(PanelQuery(object(), "owner", thread, {}, (("pet", "line"),)))[0]

    try:
        assert query("executing")["state"] == "loading"
        assert entered.wait(timeout=10)
        limit = cache_capacity.owner_sessions(1) * MAX_REQUESTED_PANEL_COUNT
        for index in range(limit * 2):
            query(str(index))
        assert len(service._slots[("owner", "act-1")].pending) == limit
        release.set()
        executor.submit(lambda: None).result(timeout=10)
        assert rendered == ["executing", *(str(index) for index in range(limit, limit * 2))]
        assert query("executing")["state"] == "ready"
    finally:
        release.set()
        service.close()
        executor.shutdown(wait=True)
