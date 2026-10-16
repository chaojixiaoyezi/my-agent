from __future__ import annotations

import hashlib
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, nullcontext
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.background import dispatch
from agent_py_agent.agent.agent_core.runner import worker as runner
from agent_py_agent.agent.backends import gateway_helpers as http
from agent_py_agent.agent.backends import request_scope as scope
from agent_py_agent.agent.gateway_parts import request_worker
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests.test_provider_queue import wait_until
from agent_py_agent.tests.test_provider_queue_transport import opener, request


@pytest.mark.parametrize("entry", ["dispatch", "timeout", "interruptible"])
@pytest.mark.parametrize("failure", [False, True])
def test_resolved_owner_reaches_real_thread_transport_and_resets(monkeypatch, tmp_path, entry, failure):
    req = request()
    main_ident = threading.get_ident()
    agent, expected = _resolved_owner_agent(monkeypatch)
    sent = _provider_entry_transport(monkeypatch, agent, failure)
    traces = _trace_entry_threads(monkeypatch)
    params = runner.RunSubagentWorkerParams(agent.config, tmp_path, "child", "", False, 1, False, "", timeout_seconds=2)
    held = scope.acquire_provider_request(req)
    with ExitStack() as stack:
        pool = stack.enter_context(ThreadPoolExecutor(max_workers=1))
        stack.callback(held.release)
        job = pool.submit(_execute_provider_entry, (entry, failure), agent, params)
        _assert_queued_owner(req, expected)
        held.release()
        job.result(timeout=3)
        _join_entry_threads(traces.created)
    assert sent == [(expected, sent[0][1])]
    assert sent[0][1] != main_ident
    assert traces.resets and all(value == "" for value in traces.resets)
    assert scope._OWNER.get() == ""
    assert scope.provider_endpoint_counts(req) == (0, 0)


def _resolved_owner_agent(monkeypatch):
    config = AgentConfig(gateway_per_user_owner_scoping=True)
    agent = SimpleNamespace(config=config, home_paths=SimpleNamespace(
        owner_provider="local", owner_kind="main", owner_id="main"),
        subagents=SimpleNamespace(load=lambda _: SimpleNamespace(attributes={})))
    # 原通道用户不是最终 owner：模拟已经绑定管理员的私聊，走真实解析函数返回 base owner。
    monkeypatch.setattr(request_worker, "admin_channel_identity_for_request", lambda *a: object())
    resolved = request_worker._resolve_request_agent(agent, {"channel": "feishu", "user_id": "raw-channel-user"})
    assert resolved is agent
    expected = hashlib.sha256(json.dumps(("local", "main", "main"), ensure_ascii=True).encode()).hexdigest()
    return resolved, expected


def _provider_entry_transport(monkeypatch, agent, failure):
    sent = []

    def send(*a, **k):
        sent.append((scope._OWNER.get(), threading.get_ident()))
        if failure:
            raise ValueError("fixture transport failure")
        return io.BytesIO(b'{"ok":true}')

    opener(monkeypatch, send)
    agent.dispatch_subagents = lambda *a, **k: http.post_json(request())
    agent.run_subagent = lambda *a, **k: http.post_json(request())
    monkeypatch.setattr(dispatch, "mark_background_start", lambda *a, **k: [])
    monkeypatch.setattr(runner, "runner_attempt_cancelled", lambda *a: False)
    return sent


def _trace_entry_threads(monkeypatch):
    traces = SimpleNamespace(created=[], resets=[])
    original_thread = threading.Thread

    def traced_thread(*a, **kw):
        target = kw["target"]

        def traced(*args, **kwargs):
            try:
                return target(*args, **kwargs)
            finally:
                traces.resets.append(scope._OWNER.get())

        kw["target"] = traced
        thread = original_thread(*a, **kw)
        traces.created.append(thread)
        return thread

    monkeypatch.setattr(dispatch.threading, "Thread", traced_thread)
    return traces


def _execute_provider_entry(case, agent, params):
    entry, failure = case
    expected = pytest.raises(ValueError) if failure and entry != "dispatch" else nullcontext()
    with scope.provider_owner_scope("previous-owner"):
        with expected:
            _call_provider_entry(entry, agent, params)
        assert scope._OWNER.get() == "previous-owner"


def _call_provider_entry(entry, agent, params):
    if entry == "dispatch":
        dispatch._start_inprocess_dispatch(agent, dispatch._BackgroundDispatchRequest(
            agent, ["child"], "owner-test", None, None, SimpleNamespace()))
    elif entry == "timeout":
        runner._run_subagent_worker_with_timeout(agent, params, attempt_id="attempt")
    else:
        runner._run_subagent_worker_interruptibly(agent, params, attempt_id="attempt")


def _assert_queued_owner(req, expected):
    wait_until(lambda: scope.provider_endpoint_counts(req) == (1, 1))
    with scope._LOCK:
        assert list(scope._INFLIGHT[scope._endpoint_key(req)].owners) == [expected]


def _join_entry_threads(threads):
    for thread in threads:
        if thread.name.startswith("ThreadPoolExecutor"):
            continue
        thread.join(timeout=2)
        assert not thread.is_alive()
