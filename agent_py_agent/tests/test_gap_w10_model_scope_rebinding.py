"""同 Agent 的线程绑定恢复与采用清理；只使用隔离目录和本地替身。"""
from __future__ import annotations

import sys
import threading
from contextvars import copy_context
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.orchestration.background import dispatch
from agent_py_agent.agent.gateway_model_adoption import GatewayModelAdoption
from agent_py_agent.agent.gateway_model_observation import GatewayModelObservation
from agent_py_agent.agent.settings.model_profiles import (
    execute_model_profile_operation,
    selected_model_config,
)
from agent_py_agent.agent.settings.model_scope import (
    active_thread_model_name,
    current_model_selection,
    model_dependencies_scope,
    prepare_model_dependencies,
    selected_model_scope,
)
from agent_py_agent.agent.settings.thread_model_selection import execute_local_model_operation
from agent_py_agent.tests.test_model_profiles import add
from agent_py_agent.tests.test_thread_model_selection import host_with_store


@pytest.fixture
def bound_host(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_py_agent.agent.backends.get_backend", lambda _kind, cfg: SimpleNamespace(
        model_name=cfg.model_name, name=cfg.model_backend))
    host = host_with_store(tmp_path)
    explicit = add(host, model_name="explicit-A", model_context_window_tokens=400_000)[0]
    other = add(host, model_name="other-B", model_context_window_tokens=500_000)[0]
    execute_model_profile_operation(host, "set_default", {"profile_id": other})
    a = execute_local_model_operation(host, "session-a", "select", {"profile_id": explicit})["thread_id"]
    b = execute_local_model_operation(host, "session-b", "select", {"profile_id": other})["thread_id"]
    return host, a, b, explicit, other


@pytest.mark.parametrize("exception", [False, True])
def test_h3_nested_different_thread_rebinds_and_restores_token(bound_host, exception):
    host, a, b, explicit, other = bound_host
    defaults = host.config, host.backend, host.prompts
    with selected_model_scope(host, thread_id=a):
        outer = host.config, host.backend, host.prompts
        selection = current_model_selection(host)
        try:
            with selected_model_scope(host, thread_id=b):
                assert host.config.model_name == "other-B"
                assert current_model_selection(host).thread_id == b
                assert current_model_selection(host).profile_id == other
                assert host.prompts.config is host.config
                assert active_thread_model_name(host, b) == "other-B"
                if exception:
                    raise InterruptedError("取消内层")
        except InterruptedError:
            assert exception
        assert all(actual is expected for actual, expected in zip((host.config, host.backend, host.prompts), outer))
        assert current_model_selection(host) is selection
        assert selection.profile_id == explicit
        assert host.config.model_context_window_tokens == 400_000
        assert active_thread_model_name(host, b) == ""
    assert all(actual is expected for actual, expected in zip((host.config, host.backend, host.prompts), defaults))
    assert current_model_selection(host) is None


def test_same_thread_reentrant_scope_keeps_frozen_objects(bound_host):
    host, a, _b, _explicit, other = bound_host
    with selected_model_scope(host, thread_id=a):
        frozen = host.config, host.backend, host.prompts, current_model_selection(host)
        execute_local_model_operation(host, "session-a", "select", {"profile_id": other})
        with selected_model_scope(host, thread_id=a):
            assert all(actual is expected for actual, expected in zip(
                (host.config, host.backend, host.prompts, current_model_selection(host)), frozen))
        with selected_model_scope(host):
            assert host.config is frozen[0]
    with selected_model_scope(host, thread_id=a):
        assert host.config.model_name == "other-B"


def test_explicit_thread_rebinds_over_unlabelled_inherited_scope(bound_host):
    host, a, _b, explicit, _other = bound_host
    with selected_model_scope(host, inherited=True, thread_id="detached-child"):
        inherited = host.config
        assert current_model_selection(host) is None
        with selected_model_scope(host, thread_id=a):
            assert host.config.model_name == "explicit-A"
            assert current_model_selection(host).profile_id == explicit
        assert host.config is inherited
        assert current_model_selection(host) is None


def test_inherited_same_thread_is_noop_but_not_a_different_thread(bound_host):
    host, a, b, _explicit, _other = bound_host
    with selected_model_scope(host, thread_id=a):
        outer = host.config
        with selected_model_scope(host, inherited=True, thread_id=b):
            inner = host.config
            assert current_model_selection(host) is None
            with selected_model_scope(host, inherited=True, thread_id=b):
                assert host.config is inner
            with selected_model_scope(host, thread_id=a):
                assert host.config is not inner
                assert current_model_selection(host).thread_id == a
            assert host.config is inner
        assert host.config is outer
        assert current_model_selection(host).thread_id == a


@pytest.mark.parametrize("exit_kind", ["close", "reject", "observation_no_send", "observation_error"])
def test_h2_candidate_cleanup_keeps_request_binding(bound_host, exit_kind):
    host, a, _b, _explicit, other = bound_host
    params = SimpleNamespace(request_id="request", context_scope="conversation",
                             task_attributes={"conversation_thread_id": a})
    observer = GatewayModelObservation(SimpleNamespace(agent=host, request_id="request"), None, {})
    observer.writer = SimpleNamespace(record_adoption=lambda _result: None)
    adoption = GatewayModelAdoption(observer, host.conversation_store.threads.require(a), None, None, None)
    observer.adoption = adoption
    defaults = host.config, host.backend, host.prompts
    with selected_model_scope(host, thread_id=a):
        request_objects = host.config, host.backend, host.prompts, current_model_selection(host)
        dependencies = prepare_model_dependencies(host, selected_model_config(host, profile_id=other))
        adoption.candidate, adoption.candidate_params = dependencies, params
        try:
            with observer.scope():
                adoption.stack.enter_context(model_dependencies_scope(host, dependencies, thread_id=a, active=True))
                assert host.config.model_name == "other-B"
                if exit_kind == "close":
                    adoption.stack.close()
                elif exit_kind == "reject":
                    adoption.reject(host, params)
                elif exit_kind == "observation_error":
                    raise InterruptedError("发送前压缩退出")
        except InterruptedError:
            assert exit_kind == "observation_error"
        assert all(actual is expected for actual, expected in zip(
            (host.config, host.backend, host.prompts, current_model_selection(host)), request_objects))
        assert host.config.model_context_window_tokens == 400_000
        assert active_thread_model_name(host, a) == "explicit-A"
        with selected_model_scope(host, thread_id=a):
            assert host.config is request_objects[0]
        assert not adoption.submitted
    assert all(actual is expected for actual, expected in zip((host.config, host.backend, host.prompts), defaults))


@pytest.mark.parametrize("outcome", ["commit", "reject", "no_send"])
def test_gateway_candidate_lifecycle_restores_outer_request(tmp_path, monkeypatch, outcome):
    from contextlib import contextmanager

    from agent_py_agent.agent.gateway_parts import request_execution
    from agent_py_agent.agent.llm_scale import hot_path
    from agent_py_agent.tests.test_gateway_model_adoption import (
        actual_request,
        before_final_check,
        fake_http,
    )
    from agent_py_agent.tests.test_gateway_model_observation import install_backend

    monkeypatch.setenv("LLM_MAX_INFLIGHT", "2")
    monkeypatch.setenv("LLM_ADMISSION_WAIT_SECONDS", "0")
    hot_path.reset_hot_path_admission_for_test()
    fixture = actual_request(tmp_path, original_window=400_000)
    install_backend(monkeypatch, fixture)
    business, _probes = fake_http(monkeypatch, fixture)
    original_scope = GatewayModelObservation.scope
    after = []

    @contextmanager
    def observed_scope(self):
        try:
            with original_scope(self):
                yield
        finally:
            selection = current_model_selection(fixture.agent)
            after.append((fixture.agent.config.model_name, fixture.agent.config.model_context_window_tokens,
                          selection.thread_id, selection.profile_id))

    monkeypatch.setattr(GatewayModelObservation, "scope", observed_scope)
    if outcome == "reject":
        before_final_check(monkeypatch, lambda _adoption, state: state.messages.append(
            {"role": "user", "content": "最后核对窗口改变"}))
    elif outcome == "no_send":
        original_select = GatewayModelAdoption.select

        def abort_after_prepare(self, agent, params, prompt):
            original_select(self, agent, params, prompt)
            assert self.candidate is not None
            raise InterruptedError("候选已准备但未发送")

        monkeypatch.setattr(GatewayModelAdoption, "select", abort_after_prepare)
    try:
        if outcome == "no_send":
            with pytest.raises(InterruptedError):
                request_execution._run_gateway_ask(fixture.context)
        else:
            request_execution._run_gateway_ask(fixture.context)
        assert after == [("original-model", 400_000, fixture.thread_id, fixture.original)]
        expected_models = [] if outcome == "no_send" else ["candidate-large" if outcome == "commit" else "original-model"]
        assert [wire["model"] for wire, _thread in business] == expected_models
        assert current_model_selection(fixture.agent) is None
    finally:
        hot_path.reset_hot_path_admission_for_test()


def test_dispatch_explicitly_isolates_inherited_context(bound_host, monkeypatch):
    host, a, _b, _explicit, _other = bound_host
    real_thread = threading.Thread
    workers, seen = [], []

    # 强制模拟继承开启的实际线程，不把 GIL 版本默认空上下文当作隔离证明。
    def inheriting_thread(*, target, args, **kwargs):
        inherited = copy_context()
        thread = real_thread(target=inherited.run, args=(target, *args), **kwargs)
        workers.append(thread)
        return thread

    monkeypatch.setattr(dispatch.threading, "Thread", inheriting_thread)
    monkeypatch.setattr(dispatch, "_background_dispatch_worker", lambda request: seen.append(
        (request.agent.config.model_name, current_model_selection(request.agent))))
    request = SimpleNamespace(launch_id="scope-test", run_ids=["child"], agent=host)
    try:
        with selected_model_scope(host, thread_id=a):
            result = dispatch._start_inprocess_dispatch(host, request)
            for worker in workers:
                worker.join(timeout=5)
                assert not worker.is_alive()
            assert result["acceptance_status"] == "accepted"
            assert seen == [("deployment-model", None)]
            assert host.config.model_name == "explicit-A"
    finally:
        for worker in workers:
            worker.join(timeout=5)


@pytest.mark.skipif(not hasattr(sys.flags, "thread_inherit_context"), reason="运行时未提供线程继承旗标")
def test_native_thread_context_flag_keeps_dispatch_empty(bound_host, monkeypatch):
    host, a, _b, _explicit, _other = bound_host
    seen, done = [], threading.Event()

    def worker(request):
        try:
            seen.append((request.agent.config.model_name, current_model_selection(request.agent)))
        finally:
            done.set()

    monkeypatch.setattr(dispatch, "_background_dispatch_worker", worker)
    request = SimpleNamespace(launch_id="native-context", run_ids=["child"], agent=host)
    with selected_model_scope(host, thread_id=a):
        dispatch._start_inprocess_dispatch(host, request)
        assert done.wait(5)
        assert seen == [("deployment-model", None)]


def test_process_reaper_does_not_retain_request_binding(bound_host, monkeypatch):
    host, a, _b, _explicit, _other = bound_host
    real_thread = threading.Thread
    workers, seen = [], []

    def inheriting_thread(*, target, args, **kwargs):
        inherited = copy_context()
        thread = real_thread(target=inherited.run, args=(target, *args), **kwargs)
        workers.append(thread)
        return thread

    monkeypatch.setattr(dispatch.threading, "Thread", inheriting_thread)
    monkeypatch.setattr(dispatch, "_reap_background_process", lambda agent, _launch, _process: seen.append(
        (agent.config.model_name, current_model_selection(agent))))
    request = SimpleNamespace(launch_id="reaper-context")
    with selected_model_scope(host, thread_id=a):
        dispatch._start_background_process_reaper(host, request, SimpleNamespace(wait=lambda: 0))
        for worker in workers:
            worker.join(timeout=5)
            assert not worker.is_alive()
        assert seen == [("deployment-model", None)]


def test_import_origin_is_the_owner_snapshot():
    from pathlib import Path

    from agent_py_agent.agent.settings import model_scope

    assert Path(model_scope.__file__).resolve() == Path(__file__).resolve().parents[1] / "agent/settings/model_scope.py"
