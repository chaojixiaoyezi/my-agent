# LLM: 使用真实 bounded worker、原 ContextVar 和 canonical typed 错误验证 Curator 运输；不造 C/W claim 或实际授权，不连接模型或网络。
# 模块用途: 保留 Count0 原行为与 F2 归属，证明未结运输异常不能被普通重试或 schema 包装吞掉。
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends import request_scope
from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError
from agent_py_agent.agent.backends.provider_headers import (
    ProbeAccountingScope,
    current_probe_accounting,
    current_provider_session,
    probe_accounting_scope,
    provider_session_scope,
)
from agent_py_agent.agent.common.request_execution_ports import (
    RequestExecutionOutcomeUncertainError,
)
from agent_py_agent.agent.memory_store import curator_backend
from agent_py_agent.agent.memory_store.curator_backend import CuratorModelCallError
from agent_py_agent.agent.memory_store.curator_inputs import CuratorInputBatch, CuratorMessageInput
from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorConfig
from agent_py_agent.tests.test_gateway_claim_transition_scope import (
    actual as actual,
)
from agent_py_agent.tests.test_gateway_claim_transition_scope import (
    c_actual as c_actual,
)
from agent_py_agent.tests.test_gateway_claim_transition_scope import (
    context as context,
)


# LLM: 输入保持正式 Curator 类型和来源字段；只用合成材料，不从正文建立执行身份。
# 函数用途: 构造可完整进入提取循环的最小批次，避免空输入或伪对象掩盖重试。
def _batch() -> CuratorInputBatch:
    return CuratorInputBatch(messages=(CuratorMessageInput(
        message_id="message-test", thread_id="thread-test", role="user", channel="internal",
        created_at=1.0, full_content="合成材料", content_preview="合成材料", content_hash="sha256:test",
        metadata={"session_id": "session-test", "request_id": "request-test", "task_id": "task-test", "run_id": "run-test"},
    ),), audit_events=())


# LLM: 只声明 canonical 未结异常的合成身份；它不是执行许可，也不进入任何实际 scope。
# 函数用途: 生成可检查对象身份和字段不变的原类型异常。
def _uncertain() -> RequestExecutionOutcomeUncertainError:
    return RequestExecutionOutcomeUncertainError({
        "id": "original-request", "execution_attempt_id": "original-attempt", "lease_epoch": 3,
        "lease_owner": "original-owner",
    }, "submit")


# LLM: 只替换模型运输返回边界，不替换 bounded、预算或重试循环；没有网络和候选写入。
# 函数用途: 记录真实 worker 调用次数，按同一异常对象返回失败事实。
class _FailureBackend:
    # LLM: 只保留合成异常对象，不安装任何执行 scope 或权限。
    # 函数用途: 准备失败运输及线程调用记录。
    def __init__(self, failure):
        self.failure = failure
        self.calls = []

    # LLM: 此方法是唯一模型替身边界，错误保持原对象；其余 bounded 和 Curator 全走真实入口。
    # 函数用途: 记录一次真实 worker 中的请求并抛出预定异常，不做网络 I/O。
    def generate_structured(self, prompt, *, response_schema):
        self.calls.append((threading.get_ident(), prompt, response_schema))
        raise self.failure


def test_canonical_uncertain_error_keeps_same_object_and_never_retries():
    error = _uncertain()
    backend = _FailureBackend(error)
    with pytest.raises(RequestExecutionOutcomeUncertainError) as caught:
        curator_backend.extract_with_retries(backend, MemoryCuratorConfig(max_retries=2), _batch())
    assert caught.value is error
    assert len(backend.calls) == 1 and backend.calls[0][0] != threading.get_ident()
    assert error.request_id == "original-request" and error.execution_attempt_id == "original-attempt"
    assert error.lease_epoch == 3 and error.lease_owner == "original-owner"
    assert error.operation_entered is False and error.phase == "submit"
    attempts = curator_backend.last_model_attempts()
    assert len(attempts) == 1 and attempts[0].outcome == "RequestExecutionOutcomeUncertainError"
    assert attempts[0].shrunk is False


def test_error_code_spoof_is_still_an_ordinary_provider_failure():
    error = RuntimeError("synthetic-provider-failure")
    error.error_code = RequestExecutionOutcomeUncertainError.error_code
    error.operation_entered = False
    backend = _FailureBackend(error)
    with pytest.raises(RuntimeError) as caught:
        curator_backend.extract_with_retries(backend, MemoryCuratorConfig(max_retries=2), _batch())
    assert caught.value is error and len(backend.calls) == 3
    assert len({item[1] for item in backend.calls}) == 1
    assert len(curator_backend.last_model_attempts()) == 3
    assert all(not attempt.shrunk for attempt in curator_backend.last_model_attempts())


def test_bounded_exit_unconfirmed_keeps_independent_type_and_never_retries():
    error = BoundedCallExitUnconfirmedError("bounded-call:synthetic", "worker_exit_unconfirmed")
    backend = _FailureBackend(error)
    with pytest.raises(BoundedCallExitUnconfirmedError) as caught:
        curator_backend.extract_with_retries(backend, MemoryCuratorConfig(max_retries=2), _batch())
    assert caught.value is error and len(backend.calls) == 1
    assert not isinstance(error, TimeoutError)
    assert not hasattr(error, "operation_entered")
    assert error.automatic_replay is False and error.reason == "worker_exit_unconfirmed"
    attempts = curator_backend.last_model_attempts()
    assert len(attempts) == 1 and attempts[0].outcome == "BoundedCallExitUnconfirmedError"
    assert attempts[0].shrunk is False


def test_provider_value_error_keeps_original_stage_wrapper_and_retry_count():
    error = ValueError("synthetic-provider-value")
    backend = _FailureBackend(error)
    with pytest.raises(CuratorModelCallError) as caught:
        curator_backend.extract_with_retries(backend, MemoryCuratorConfig(max_retries=2), _batch())
    assert caught.value.__cause__ is error and len(backend.calls) == 3
    assert len({item[1] for item in backend.calls}) == 1
    assert [attempt.outcome for attempt in curator_backend.last_model_attempts()] == ["ValueError"] * 3


def test_count_zero_worker_preserves_original_f2_owner_budget_and_probe_scope():
    seen = []
    owner = ("local", "user", "synthetic-owner")
    request = SimpleNamespace(
        api_base="http://synthetic.invalid/v1", provider_max_concurrent_requests=1,
    )
    accounting = ProbeAccountingScope(object(), "request-test", "run-test")

    def generate(prompt, *, response_schema):
        lease = request_scope.acquire_provider_request(request)
        try:
            seen.append({
                "thread": threading.get_ident(), "prompt": prompt, "schema": response_schema,
                "session": current_provider_session(), "owner": lease.ticket.owner,
                "budget": request_scope.provider_request_timeout(240),
                "accounting": current_probe_accounting(),
                "counts": request_scope.provider_endpoint_counts(request),
            })
            return "fake-response"
        finally:
            lease.release()

    backend = SimpleNamespace(generate_structured=generate)
    with provider_session_scope(owner, "thread-test"), probe_accounting_scope(accounting):
        original_session = current_provider_session()
        original_owner = request_scope._OWNER.get()
        assert curator_backend.call_backend_with_timeout(
            backend, prompt="合成提取", response_schema={"type": "object"}, timeout_seconds=5,
        ) == "fake-response"
        assert current_provider_session() == original_session
        assert request_scope.provider_request_timeout(240) == 240
    assert len(seen) == 1 and seen[0]["thread"] != threading.get_ident()
    assert seen[0]["prompt"] == "合成提取" and seen[0]["schema"] == {"type": "object"}
    assert seen[0]["session"] == original_session and seen[0]["owner"] == original_owner
    assert 0 < seen[0]["budget"] <= 5
    assert seen[0]["accounting"] is accounting and seen[0]["counts"] == (1, 0)
    assert request_scope.provider_endpoint_counts(request) == (0, 0)
    assert current_provider_session() == "" and current_probe_accounting() is None


def test_original_transport_rejection_happens_inside_worker_before_provider(monkeypatch):
    from agent_py_agent.agent.gateway_parts import multiprocess_background_execution as bridge

    error = _uncertain()
    checked = []
    reached = []

    def reject():
        checked.append((threading.get_ident(), request_scope.provider_request_timeout(240)))
        raise error

    monkeypatch.setattr(bridge, "require_background_model_admission", reject)
    backend = SimpleNamespace(generate_structured=lambda *args, **kwargs: reached.append("provider"))
    with pytest.raises(RequestExecutionOutcomeUncertainError) as caught:
        curator_backend.extract_with_retries(backend, MemoryCuratorConfig(max_retries=2), _batch())
    assert caught.value is error
    assert len(checked) == 1 and checked[0][0] != threading.get_ident()
    attempts = curator_backend.last_model_attempts()
    assert reached == [] and len(attempts) == 1
    assert 0 < checked[0][1] <= attempts[0].granted_seconds


def test_concurrent_count_zero_invocations_do_not_mix_f2_owner_or_session():
    rendezvous = threading.Barrier(2)
    request = SimpleNamespace(api_base="http://synthetic.invalid/v1", provider_max_concurrent_requests=2)

    def invoke(owner_id):
        seen = []

        def generate(prompt, *, response_schema):
            lease = request_scope.acquire_provider_request(request)
            try:
                rendezvous.wait(timeout=2)
                seen.append((lease.ticket.owner, current_provider_session(), request_scope.provider_request_timeout(240)))
                return "fake-response"
            finally:
                lease.release()

        with provider_session_scope(("local", "user", owner_id), "thread-" + owner_id):
            expected_owner, expected_session = request_scope._OWNER.get(), current_provider_session()
            assert curator_backend.call_backend_with_timeout(
                SimpleNamespace(generate_structured=generate), prompt="合成材料", response_schema={}, timeout_seconds=5,
            ) == "fake-response"
        assert len(seen) == 1
        assert seen[0][:2] == (expected_owner, expected_session)
        assert 0 < seen[0][2] <= 5
        assert current_provider_session() == "" and request_scope._OWNER.get() == ""
        return seen[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        alice, bob = [future.result(timeout=6) for future in (
            pool.submit(invoke, "alice"), pool.submit(invoke, "bob"),
        )]
    assert alice[0] != bob[0] and alice[1] != bob[1]
    assert request_scope.provider_endpoint_counts(request) == (0, 0)


# LLM: 输入只复用作者冻结夹具经真实 P3/P5/lease/claim 产生的原对象；不手造 binding、scope 或宿主身份。
# 函数用途: 在临时真实 C/W 认领上安装原执行区间，供实际 bounded 运输合同共用。
@contextmanager
def _original_execution(request, kind):
    from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_execution import (
        coordinator_execution_scope,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_execution import worker_execution_scope

    if kind == "worker":
        binding, payload, agent = request.getfixturevalue("actual")
        claim = binding.claim
        scope = worker_execution_scope(binding, agent)
    else:
        gate, claim, agent = request.getfixturevalue("c_actual")
        payload = json.loads(claim.path.read_bytes())
        scope = coordinator_execution_scope(gate, claim, agent)
    selected = {"agent": agent, "request": payload, "request_path": claim.path, "request_id": claim.request_id}
    with scope:
        yield selected, claim


@pytest.mark.parametrize("kind", ["worker", "coordinator"])
def test_real_claim_scope_reaches_original_gate_inside_actual_bounded_worker(request, kind):
    from agent_py_agent.agent.gateway_parts import multiprocess_background_execution as bridge
    from agent_py_agent.agent.gateway_parts.multiprocess_execution import current_worker_execution

    seen = []
    response = object()
    with _original_execution(request, kind) as (selected, _claim), bridge.background_request_scope(selected):
        original = current_worker_execution()

        def generate(prompt, *, response_schema):
            background = bridge.current_background_request()
            assert current_worker_execution() is original
            assert background.context is selected and background.lifetime.active
            assert background.lifetime._active_window is None and bridge._BACKGROUND_TRANSACTION.get() is None
            seen.append(threading.current_thread())
            return response

        assert curator_backend.call_backend_with_timeout(
            SimpleNamespace(generate_structured=generate), prompt="合成后台", response_schema={}, timeout_seconds=5,
        ) is response
    assert len(seen) == 1 and seen[0] is not threading.current_thread() and not seen[0].is_alive()


@pytest.mark.parametrize("kind", ["worker", "coordinator"])
def test_real_claim_cancel_blocks_second_provider_without_releasing_reservation(request, kind):
    from agent_py_agent.agent.gateway_parts import multiprocess_background_execution as bridge
    from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    calls = []
    backend = SimpleNamespace(generate_structured=lambda *args, **kwargs: calls.append("provider") or "fake-response")
    with _original_execution(request, kind) as (selected, claim), bridge.background_request_scope(selected):
        paths = gateway_paths_from_root(claim.path.parent.parent.parent)
        reservation = reservation_file(paths, claim.request_id)
        original_p3 = reservation.read_bytes()
        assert curator_backend.call_backend_with_timeout(
            backend, prompt="有效后台", response_schema={}, timeout_seconds=5,
        ) == "fake-response"
        selected["request"].update(cancel_requested=True, turn_phase="closing")
        claim.path.write_text(json.dumps(selected["request"]), encoding="utf-8")
        with pytest.raises(RequestExecutionOutcomeUncertainError) as caught:
            curator_backend.extract_with_retries(backend, MemoryCuratorConfig(max_retries=2), _batch())
        assert caught.value.request_id == claim.request_id
        assert caught.value.execution_attempt_id == claim.execution_attempt_id
        assert caught.value.lease_epoch == claim.lease_epoch and caught.value.lease_owner == claim.lease_owner
        assert caught.value.operation_entered is False and caught.value.phase == "submit"
        assert len(curator_backend.last_model_attempts()) == 1
        assert reservation.read_bytes() == original_p3
        assert json.loads(claim.path.read_bytes())["status"] == "processing"
        assert not (paths.terminal / claim.path.name).exists()
    assert calls == ["provider"]


@pytest.mark.parametrize("kind", ["worker", "coordinator"])
def test_real_claim_waits_for_original_worker_exit_and_keeps_independent_unknown(request, kind, monkeypatch):
    from agent_py_agent.agent.backends import bounded_call as bounded
    from agent_py_agent.agent.gateway_parts import multiprocess_background_execution as bridge

    tail, release = threading.Event(), threading.Event()
    original_run = threading.Thread.run
    workers = []

    def run(worker):
        value = original_run(worker)
        if worker.name.startswith("bounded-call:"):
            workers.append(worker)
            tail.set()
            assert release.wait(3)
        return value

    monkeypatch.setattr(threading.Thread, "run", run)
    try:
        with _original_execution(request, kind) as (selected, _claim), bridge.background_request_scope(selected):
            backend = SimpleNamespace(generate_structured=lambda *args, **kwargs: "fake-response")
            with pytest.raises(BoundedCallExitUnconfirmedError) as caught:
                curator_backend.call_backend_with_timeout(
                    backend, prompt="合成后台", response_schema={}, timeout_seconds=0.15,
                )
            assert tail.is_set() and len(workers) == 1 and workers[0].is_alive()
            assert caught.value.reason == "worker_exit_unconfirmed"
            assert not isinstance(caught.value, TimeoutError)
            assert not isinstance(caught.value, RequestExecutionOutcomeUncertainError)
            assert not hasattr(caught.value, "operation_entered")
            assert caught.value.automatic_replay is False
            rows = bounded.bounded_call_drain_snapshot()
            assert len(rows) == 1 and rows[0].worker is workers[0] and rows[0].retained()
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=1)
        assert all(not worker.is_alive() for worker in workers)
        with bounded._CALL_LOCK:
            bounded._reap_calls_locked()


# LLM: 保持 common 原构造器和实际类型身份；附带 ValueError 形状不授予普通 provider 包装或重试资格。
# 类用途: 核验原短门未结的多继承子类仍同对象传播。
class _ValueShapedTransportUnknown(RequestExecutionOutcomeUncertainError, ValueError):
    pass


# LLM: 保持独立退出错误及无 operation_entered 事实；多继承不能把已进入的物理责任改签成普通解析失败。
# 类用途: 核验原线程退出未知的 ValueError 子类不会丢失 canonical 身份。
class _ValueShapedExitUnknown(BoundedCallExitUnconfirmedError, ValueError):
    pass


@pytest.mark.parametrize("kind", ["transport", "exit"])
def test_schema_shaped_canonical_error_keeps_identity_at_final_wrapper(kind):
    if kind == "transport":
        error = _ValueShapedTransportUnknown({
            "id": "original-request", "execution_attempt_id": "original-attempt", "lease_epoch": 3,
            "lease_owner": "original-owner",
        }, "submit")
    else:
        error = _ValueShapedExitUnknown("bounded-call:synthetic", "worker_exit_unconfirmed")
    assert isinstance(error, ValueError)
    backend = _FailureBackend(error)
    with pytest.raises(type(error)) as caught:
        curator_backend.extract_with_retries(backend, MemoryCuratorConfig(max_retries=2), _batch())
    assert caught.value is error and len(backend.calls) == 1
    assert not isinstance(caught.value, CuratorModelCallError)
    attempts = curator_backend.last_model_attempts()
    assert len(attempts) == 1 and attempts[0].outcome == type(error).__name__
    assert attempts[0].shrunk is False
    if kind == "transport":
        assert error.operation_entered is False and error.phase == "submit"
        assert error.request_id == "original-request" and error.execution_attempt_id == "original-attempt"
        assert error.lease_epoch == 3 and error.lease_owner == "original-owner"
    else:
        assert error.reason == "worker_exit_unconfirmed" and error.automatic_replay is False
        assert not hasattr(error, "operation_entered") and not isinstance(error, TimeoutError)
