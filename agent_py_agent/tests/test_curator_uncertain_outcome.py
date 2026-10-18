"""原 Curator state/claim/backend 的未结出口；不把临时合同代作 P3 终态或原生排空。"""
from __future__ import annotations

import json
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError
from agent_py_agent.agent.common.request_execution_ports import (
    RequestExecutionContextUnavailable,
    RequestExecutionOutcomeUncertainError,
)
from agent_py_agent.agent.memory_store import curator_backend, curator_lease
from agent_py_agent.agent.memory_store.curator import _run_context
from agent_py_agent.agent.memory_store.curator_models import CuratorModelRoute, MemoryCuratorConfig
from agent_py_agent.agent.memory_store.curator_state import CuratorLeaseRequest
from agent_py_agent.tests.test_curator_original_transport import _original_execution
from agent_py_agent.tests.test_gateway_claim_transition_scope import actual as actual
from agent_py_agent.tests.test_gateway_claim_transition_scope import c_actual as c_actual
from agent_py_agent.tests.test_gateway_claim_transition_scope import context as context
from agent_py_agent.tests.test_memory_curator_v2 import _conversation, _service


# LLM: 使用真实 Curator 仓库与原 claim 的准确业务对象；只替换无网络模型，组件指针不签发新权限。
# 函数用途: 建立可走 public run 的隔离服务，保原 pending 代次和真实消息游标。
def _case(tmp_path, backend, agent=None):
    root = tmp_path / "curator-data"
    store, thread, message = _conversation(root)
    owner_id = "" if agent is None else agent.home_paths.owner_id
    service = _service(root, backend, store, owner_id=owner_id,
                       config=MemoryCuratorConfig(timeout_seconds=1, max_retries=0, max_input_chars=40000))
    if agent is not None:
        agent.memory_curator = service
    service.request("admin")
    return service, thread, message


# LLM: 门前取消只改原 claimed 文件的结构字段；P3 快照不动，不编造 claim 或从正文推断取消。
# 函数用途: 模拟真实取消写者到达，供原后台运输门拒绝后续模型提交。
def _cancel(selected, claim):
    selected["request"].update(cancel_requested=True, turn_phase="closing")
    claim.path.write_text(json.dumps(selected["request"]), encoding="utf-8")


# LLM: 只核原状态，不将 lease 活性当 P3 权限；心跳时间可以前进，但游标/pending/正式输出不能被未知推进。
# 函数用途: 检查未结运行仍由同一原租约持有，且没有失败提交或候选写入。
def _assert_retained(service, pending_before):
    state = service.state_store.load()
    assert state.active_lease.get("phase") == "executing", state.to_dict()
    assert state.active_lease.get("lease_id")
    assert state.pending_reasons == pending_before.pending_reasons
    assert state.pending_reason_generations == pending_before.pending_reason_generations
    assert state.per_thread_cursors == pending_before.per_thread_cursors
    assert state.last_processed_audit_event_id == pending_before.last_processed_audit_event_id
    assert state.last_failure_code == pending_before.last_failure_code
    assert service.run_log.list() == []
    assert service.candidate_service.list() == []


@pytest.mark.parametrize("kind", ["worker", "coordinator"])
def test_real_transport_unknown_never_commits_failure_or_clears_original_lease(tmp_path, request, kind):
    from agent_py_agent.agent.gateway_parts import multiprocess_background_execution as bridge
    from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    reached = []
    backend = SimpleNamespace(generate_structured=lambda *_a, **_k: reached.append("provider"))
    with _original_execution(request, kind) as (selected, claim), bridge.background_request_scope(selected):
        service, _thread, _message = _case(tmp_path, backend, selected["agent"])
        pending = service.state_store.load()
        paths = gateway_paths_from_root(claim.path.parent.parent.parent)
        original_p3 = reservation_file(paths, claim.request_id).read_bytes()

        def cancel_after_acquire(batch, _run_id, _deadline):
            _cancel(selected, claim)
            return batch, ()

        service.dependencies = replace(service.dependencies, annotate_batch=cancel_after_acquire)
        caught = None
        try:
            service.run(reason="admin")
        except RequestExecutionOutcomeUncertainError as error:
            caught = error
        assert caught is not None, service.state_store.load().to_dict()
        assert caught.request_id == claim.request_id and caught.execution_attempt_id == claim.execution_attempt_id
        assert caught.lease_epoch == claim.lease_epoch and caught.lease_owner == claim.lease_owner
        assert caught.operation_entered is False and reached == []
        _assert_retained(service, pending)
        assert reservation_file(paths, claim.request_id).read_bytes() == original_p3
        assert not (paths.terminal / claim.path.name).exists()


@pytest.mark.parametrize("kind", ["worker", "coordinator"])
def test_real_worker_exit_unknown_never_commits_failure_or_retires_lease(tmp_path, request, kind, monkeypatch):
    from agent_py_agent.agent.backends import bounded_call as bounded
    from agent_py_agent.agent.gateway_parts import multiprocess_background_execution as bridge

    reached, workers = [], []
    release = threading.Event()
    original_run = threading.Thread.run

    def physical_tail(worker):
        result = original_run(worker)
        if worker.name.startswith("bounded-call:"):
            workers.append(worker)
            assert release.wait(30)
        return result

    monkeypatch.setattr(threading.Thread, "run", physical_tail)
    backend = SimpleNamespace(generate_structured=lambda *_a, **_k: reached.append("provider") or "unused")
    try:
        with _original_execution(request, kind) as (selected, _claim), bridge.background_request_scope(selected):
            service, _thread, _message = _case(tmp_path, backend, selected["agent"])
            pending = service.state_store.load()
            caught = None
            try:
                service.run(reason="admin")
            except BoundedCallExitUnconfirmedError as error:
                caught = error
            assert caught is not None, service.state_store.load().to_dict()
            assert not hasattr(caught, "operation_entered") and caught.automatic_replay is False
            assert caught.reason == "worker_exit_unconfirmed" and reached == ["provider"]
            assert len(workers) == 1 and workers[0].is_alive()
            assert any(row.worker is workers[0] and row.retained() for row in bounded.bounded_call_drain_snapshot())
            _assert_retained(service, pending)
    finally:
        release.set()
        for worker in workers:
            worker.join(2)
        assert all(not worker.is_alive() for worker in workers)
        with bounded._CALL_LOCK:
            bounded._reap_calls_locked()


@pytest.mark.parametrize("kind", ["worker", "coordinator"])
def test_actual_short_gate_denial_before_acquire_preserves_pending_and_error(tmp_path, request, kind, monkeypatch):
    from agent_py_agent.agent.common.request_execution_ports import gateway_short_operation
    from agent_py_agent.agent.gateway_parts import multiprocess_background_execution as bridge

    with _original_execution(request, kind) as (selected, claim), bridge.background_request_scope(selected):
        service, _thread, _message = _case(tmp_path, SimpleNamespace(), selected["agent"])
        original = service.committer.recover_and_acquire
        state_before = service.state_store.path.read_bytes()
        failures = []

        def guarded_acquire(lease_request):
            try:
                return gateway_short_operation("curator_acquire", lambda: original(lease_request))
            except RequestExecutionOutcomeUncertainError as error:
                failures.append(error)
                raise

        monkeypatch.setattr(service.committer, "recover_and_acquire", guarded_acquire)
        _cancel(selected, claim)
        with pytest.raises(RequestExecutionOutcomeUncertainError) as caught:
            service._run_once(reason="admin")
        assert len(failures) == 1 and caught.value is failures[0]
        assert service.state_store.path.read_bytes() == state_before
        assert service.run_log.list() == []


# LLM: 多继承只测试 canonical 类型不能被 schema 分类抢走；异常对象不授身份，也不进入真实 claim。
# 类用途: 模拟同时具有 ValueError 形状的真实短门未结子类。
class _SchemaShapedTransportUnknown(RequestExecutionOutcomeUncertainError, ValueError):
    pass


# LLM: 保持独立物理退出错误身份；ValueError 形状不能让它获得默认模型 fallback 资格。
# 类用途: 验证物理未知不能被普通解析错误分类吞掉。
class _SchemaShapedExitUnknown(BoundedCallExitUnconfirmedError, ValueError):
    pass


# LLM: 错误只是合成事实，不是权限；用原 canonical 构造器保留同一对象供直接出口回归。
# 函数用途: 生成两类未结异常，包含能触发旧 schema fallback 分类的子类。
def _unknown(kind, *, schema_shaped=False):
    if kind == "missing":
        return RequestExecutionContextUnavailable("synthetic missing original source")
    if kind == "transport":
        cls = _SchemaShapedTransportUnknown if schema_shaped else RequestExecutionOutcomeUncertainError
        return cls({"id": "request", "execution_attempt_id": "attempt", "lease_epoch": 1,
                    "lease_owner": "lease"}, "submit")
    cls = _SchemaShapedExitUnknown if schema_shaped else BoundedCallExitUnconfirmedError
    return cls("bounded-call:fixture", "worker_exit_unconfirmed")


@pytest.mark.parametrize("kind", ["transport", "exit"])
def test_typed_unknown_cannot_use_default_model_schema_fallback(tmp_path, kind, monkeypatch):
    error = _unknown(kind, schema_shaped=True)
    calls = []

    selected_backend, fallback = object(), object()
    service, _thread, _message = _case(tmp_path, selected_backend)

    # 这里只隔离route层的类型门；上方真实claim/worker用例另行覆盖完整backend，不用本替身宣称P3通过。
    def extract(backend, _context, _batch):
        calls.append("selected" if backend is selected_backend else "fallback")
        if backend is selected_backend:
            raise error
        return "fallback-result"

    monkeypatch.setattr(service, "_extract_with", extract)
    state, lease = service.committer.recover_and_acquire(CuratorLeaseRequest("admin", "fixture", 120, None))
    context_value = _run_context("admin", state, lease)
    context_value.route.update(backend=service.backend, is_default=False,
                               default=CuratorModelRoute("default", fallback, "fake", "default", is_default=True))
    batch = service._collect(state, include_formal=True)
    with pytest.raises(type(error)) as caught:
        service._extract_for_route(context_value, batch)
    assert caught.value is error and calls == ["selected"]
    assert "failure_warnings" not in context_value.route and "fallback_warnings" not in context_value.route


@pytest.mark.parametrize("kind", ["transport", "exit", "missing"])
def test_interruption_guard_keeps_executing_for_original_typed_unknown(tmp_path, kind):
    error = _unknown(kind)
    service, _thread, _message = _case(tmp_path, SimpleNamespace())
    _state, lease = service.committer.recover_and_acquire(CuratorLeaseRequest("admin", "fixture", 120, None))
    before = service.state_store.path.read_bytes()
    with pytest.raises(type(error)) as caught:
        with curator_lease.curator_lease_interruption_guard(service.state_store, lease["lease_id"]):
            raise error
    assert caught.value is error and service.state_store.path.read_bytes() == before


def test_missing_original_context_before_acquire_is_not_an_unleased_failure(tmp_path, monkeypatch):
    service, _thread, _message = _case(tmp_path, SimpleNamespace())
    error = RequestExecutionContextUnavailable("synthetic missing original source")
    before = service.state_store.path.read_bytes()

    # 只隔离缺端口的类型出口；真实claim/P3链由前六例承担，本例不伪造请求身份。
    def denied(_request):
        raise error

    monkeypatch.setattr(service.committer, "recover_and_acquire", denied)
    with pytest.raises(RequestExecutionContextUnavailable) as caught:
        service._run_once(reason="admin")
    assert caught.value is error and service.state_store.path.read_bytes() == before
    assert service.run_log.list() == []


def test_error_code_spoof_keeps_original_ordinary_failure_and_lease_cleanup(tmp_path):
    calls = []
    error = RuntimeError("synthetic ordinary provider failure")
    error.error_code = RequestExecutionOutcomeUncertainError.error_code

    def generate(*_args, **_kwargs):
        calls.append("provider")
        raise error

    service, _thread, _message = _case(tmp_path, SimpleNamespace(generate_structured=generate))
    result = service.run(reason="admin")
    assert result.status == "failed" and result.failure_code == "CURATOR_MODEL_FAILED"
    assert calls == ["provider"] and service.state_store.load().active_lease == {}
    assert len(service.run_log.list()) == 1 and service.state_store.load().per_thread_cursors == {}


@pytest.mark.parametrize("kind", ["worker", "coordinator"])
@pytest.mark.parametrize("failure_kind", ["transport", "exit"])
def test_backend_canonical_subclass_reaches_real_service_without_schema_reclassification(
    tmp_path, request, kind, failure_kind,
):
    from agent_py_agent.agent.gateway_parts import multiprocess_background_execution as bridge

    error = _unknown(failure_kind, schema_shaped=True)
    calls = []

    # 唯一替身是无网络provider错误；claim/state/bounded/backend重试和service出口均为实际产品链。
    def generate(*_args, **_kwargs):
        calls.append(threading.current_thread())
        raise error

    with _original_execution(request, kind) as (selected, _claim), bridge.background_request_scope(selected):
        service, _thread, _message = _case(tmp_path, SimpleNamespace(generate_structured=generate), selected["agent"])
        pending = service.state_store.load()
        with pytest.raises(type(error)) as caught:
            service.run(reason="admin")
        assert caught.value is error and len(calls) == 1 and calls[0] is not threading.current_thread()
        _assert_retained(service, pending)
