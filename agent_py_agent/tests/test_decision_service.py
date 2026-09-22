"""决策外层策略的身份、固定阶段、冷却及设置撤销合同；不访问真实供应商。"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.decision_protocol import DecisionInputError
from agent_py_agent.agent.backends.errors import (
    ProviderConfigurationError,
    ProviderQuotaExhaustedError,
    ProviderTransientError,
)
from agent_py_agent.agent.backends.typesafe_decision_wire import parse_typesafe_response
from agent_py_agent.agent.common.json_io import locked_json_path
from agent_py_agent.agent.concurrency.interrupt import InterruptHandle
from agent_py_agent.agent.conversation import decision_model_call as calls
from agent_py_agent.agent.conversation import decision_policy as policy
from agent_py_agent.agent.conversation import decision_service as service
from agent_py_agent.agent.llm_scale.concurrency import ConcurrencyTimeout
from agent_py_agent.agent.settings.decision_settings import (
    execute_decision_settings_operation as settings,
)
from agent_py_agent.agent.settings.model_profiles import (
    execute_model_profile_operation,
    model_profiles_path,
    read_model_profiles,
)
from agent_py_agent.agent.tooling.cancellation import ToolCancelled
from agent_py_agent.tests.test_decision_model_profiles import decision
from agent_py_agent.tests.test_decision_protocol import questions, response
from agent_py_agent.tests.test_decision_settings import host_at, patch


@pytest.fixture
def prepared(tmp_path):
    host = host_at(tmp_path)
    key, _ = decision(host)
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    params = SimpleNamespace(request_id="req", run_id="run", task_id="task", task_attributes={"conversation_thread_id": thread.thread_id})
    patch(host, {"enabled": True, "profile_id": key, "points.recall.mode": "apply"})
    with policy._LOCK:
        policy._FAILURES.clear()
    return host, params, key


# LLM: 只替换原invoke边界，不构造旁路账本；返回真实协议解析结果供外层绑定复查。
# 函数用途: 用协议fixture模拟成功响应，适用于策略合同测试。
def successful(_agent, _params, request, backend, **_kwargs):
    return parse_typesafe_response(request, backend.model_name, response())


@pytest.mark.parametrize("change", [{"enabled": False}, {"points.recall.mode": "observe"}, {"timeout_seconds": 10}])
def test_consumer_revalidates_settings_after_candidate_refresh(prepared, monkeypatch, change):
    host, params, _ = prepared
    monkeypatch.setattr(calls, "invoke_decision_model_call", successful)
    stage = service.begin_decision_stage(host, params, operation_id="consumer-batch")
    outcome = decide(host, params, stage)
    assert service.decision_outcome_is_current(host, params, stage, outcome)
    patch(host, change)
    assert not service.decision_outcome_is_current(host, params, stage, outcome)


def test_consumer_uses_original_call_deadline_and_propagates_user_stop(prepared, monkeypatch):
    host, params, _ = prepared
    monkeypatch.setattr(calls, "invoke_decision_model_call", successful)
    stage = service.begin_decision_stage(host, params, operation_id="consumer-batch")
    outcome = decide(host, params, stage)
    monkeypatch.setattr(service, "time", SimpleNamespace(monotonic=lambda: outcome.deadline))
    assert not service.decision_outcome_is_current(host, params, stage, outcome)

    def stop():
        raise InterruptedError("user stopped")

    monkeypatch.setattr(service, "_check_interrupted", stop)
    with pytest.raises(InterruptedError):
        service.decision_outcome_is_current(host, params, stage, outcome)


# LLM: 每次测试显式创建原业务stage；多次调用复用stage的测试自行持有，不在helper内重置。
# 函数用途: 对已建阶段执行一次固定接入点判断。
def decide(host, params, stage, **kwargs):
    return service.decide(host, params, stage, **{"point": "recall", "state": {"fact": 1}, "questions": questions(), "candidates_revision": "candidates-1", **kwargs})


def test_off_never_builds_backend_or_request(prepared, monkeypatch):
    host, params, _ = prepared
    patch(host, {"enabled": False})
    stage = service.begin_decision_stage(host, params, operation_id="batch")
    monkeypatch.setattr(service, "_backend", lambda *_: pytest.fail("off cannot construct backend"))
    monkeypatch.setattr(service, "DecisionRequest", lambda *_: pytest.fail("off cannot construct request"))
    assert decide(host, params, stage, state=object()).status == "off"


@pytest.mark.parametrize("mode,may_apply", [("observe", False), ("apply", True)])
def test_success_mode_and_partial_answers_keep_consumer_authority(prepared, monkeypatch, mode, may_apply):
    host, params, _ = prepared
    patch(host, {"points.recall.mode": mode})
    def partial(*args, **kwargs):
        result = successful(*args, **kwargs)
        return replace(result, answers=tuple(replace(answer, error_code="invalid_answer") for answer in result.answers))
    monkeypatch.setattr(calls, "invoke_decision_model_call", partial)
    result = decide(host, params, service.begin_decision_stage(host, params, operation_id="batch"))
    assert (result.status, result.mode, result.may_apply) == ("success", mode, may_apply)
    assert result.retain_original and all(answer.error_code for answer in result.response.answers)


def test_stage_budget_and_single_call_deadline_do_not_restart(prepared, monkeypatch):
    host, params, _ = prepared
    now = [100.0]
    monkeypatch.setattr(service, "time", SimpleNamespace(monotonic=lambda: now[0]))
    stage = service.begin_decision_stage(host, params, operation_id="batch")
    assert stage.deadline == 104
    patch(host, {"stage_timeout_seconds": 50, "points.recall.timeout_seconds": 10})
    captured = []
    def call(*args, **kwargs):
        captured.append(kwargs["deadline"])
        return successful(*args, **kwargs)
    monkeypatch.setattr(calls, "invoke_decision_model_call", call)
    now[0] = 103
    assert decide(host, params, stage, caller_deadline=103.5).status == "success"
    assert captured == [103.5]
    now[0] = 104
    assert decide(host, params, stage).status == "deadline" and len(captured) == 1
    assert stage.deadline == 104


def test_stage_start_precedes_settings_read_and_corruption_keeps_baseline(prepared, monkeypatch):
    host, params, _ = prepared
    now = [100.0]
    monkeypatch.setattr(service, "time", SimpleNamespace(monotonic=lambda: now[0]))
    original = service.execute_decision_settings_operation
    def delayed(*args, **kwargs):
        now[0] = 110
        return original(*args, **kwargs)
    monkeypatch.setattr(service, "execute_decision_settings_operation", delayed)
    stage = service.begin_decision_stage(host, params, operation_id="batch")
    assert stage.started_at == 100 and stage.deadline == 104
    assert decide(host, params, stage).status == "deadline"
    path = model_profiles_path(host.home_paths)
    path.write_text("broken json")
    stage = service.begin_decision_stage(host, params, operation_id="next")
    assert decide(host, params, stage).status == "configuration_required"
    assert path.read_text() == "broken json"


@pytest.mark.parametrize("lock_scope", ["owner", "thread"])
def test_runtime_reads_do_not_wait_for_original_file_locks(prepared, lock_scope):
    host, params, _ = prepared
    thread_id = params.task_attributes["conversation_thread_id"]
    path = model_profiles_path(host.home_paths) if lock_scope == "owner" else host.conversation_store.threads.storage.thread_path(thread_id)
    entered, release = threading.Event(), threading.Event()
    def holder():
        with locked_json_path(path):
            entered.set()
            assert release.wait(2)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(holder)
        assert entered.wait(2)
        try:
            started = time.monotonic()
            stage = service.begin_decision_stage(host, params, operation_id="batch")
            assert time.monotonic() - started < 0.5
            assert decide(host, params, stage).reason == "settings_busy"
        finally:
            release.set()
        future.result()
    assert not service.begin_decision_stage(host, params, operation_id="next").error_code


@pytest.mark.parametrize("change", ["thread", "run", "task"])
def test_stage_cannot_be_reused_for_another_identity(prepared, change):
    host, params, _ = prepared
    stage = service.begin_decision_stage(host, params, operation_id="batch")
    if change == "thread":
        params.task_attributes = {"conversation_thread_id": "other"}
    else:
        setattr(params, change + "_id", "other")
    result = decide(host, params, stage)
    assert result.status == "stale" and not result.may_apply


@pytest.mark.parametrize("field", ["binding", "input_digest", "requested_model"])
def test_mismatched_response_is_not_usable(prepared, monkeypatch, field):
    host, params, _ = prepared
    def mismatch(*args, **kwargs):
        result = successful(*args, **kwargs)
        value = replace(result.binding, operation_id="other") if field == "binding" else "other"
        return replace(result, **{field: value})
    monkeypatch.setattr(calls, "invoke_decision_model_call", mismatch)
    result = decide(host, params, service.begin_decision_stage(host, params, operation_id="batch"))
    assert result.status == "stale" and result.reason == "response_binding_mismatch"


@pytest.mark.parametrize("change", ["disabled", "credentials", "timing"])
def test_changes_during_call_discard_old_response(prepared, monkeypatch, change):
    host, params, key = prepared
    handles = []
    def change_while_calling(*args, **kwargs):
        handles.append(kwargs["interrupt_handle"])
        if change == "disabled":
            patch(host, {"enabled": False})
        elif change == "timing":
            patch(host, {"timeout_seconds": 8})
        else:
            data = read_model_profiles(model_profiles_path(host.home_paths))
            execute_model_profile_operation(host, "save_provider", {"provider_id": data["profiles"][key]["provider_id"], "editing": True, "provider": {"api_key": "new-secret"}})
        return successful(*args, **kwargs)
    monkeypatch.setattr(calls, "invoke_decision_model_call", change_while_calling)
    result = decide(host, params, service.begin_decision_stage(host, params, operation_id="batch"))
    assert result.status == "stale" and not result.may_apply
    assert handles[0].cancelled is (change == "disabled")


def test_final_validation_time_is_inside_deadline(prepared, monkeypatch):
    host, params, _ = prepared
    now = [100.0]
    monkeypatch.setattr(service, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(calls, "invoke_decision_model_call", successful)
    original = service._stale
    checks = []
    def slow_validation(*args):
        checks.append(1)
        result = original(*args)
        if len(checks) == 2:
            now[0] = 105
        return result
    monkeypatch.setattr(service, "_stale", slow_validation)
    result = decide(host, params, service.begin_decision_stage(host, params, operation_id="batch"))
    assert result.status == "deadline" and result.reason == "late_validation"


@pytest.mark.parametrize("error,expected,delay", [(ProviderConfigurationError("credential-secret"), "configuration_required", 0), (ProviderTransientError("temporary"), "cooldown", 30), (ProviderQuotaExhaustedError("quota"), "cooldown", 300)])
def test_typed_connection_cooldown_and_explicit_retry(prepared, monkeypatch, error, expected, delay):
    host, params, _ = prepared
    calls_seen = []
    def failure(*args, **kwargs):
        calls_seen.append(1)
        raise error
    monkeypatch.setattr(calls, "invoke_decision_model_call", failure)
    stage = service.begin_decision_stage(host, params, operation_id="batch")
    decide(host, params, stage)
    result = decide(host, params, stage)
    assert result.status == expected and len(calls_seen) == 1
    if delay:
        assert delay - 1 <= result.retry_after_seconds <= delay
    assert "credential-secret" not in repr(policy._FAILURES)
    monkeypatch.setattr(calls, "invoke_decision_model_call", successful)
    assert decide(host, params, stage, explicit_retry=True).status == "success"


@pytest.mark.parametrize("error", [DecisionInputError("bad local question"), RuntimeError("local"), BlockingIOError("settings busy")])
def test_local_errors_do_not_poison_connection(prepared, monkeypatch, error):
    host, params, _ = prepared
    def failure(*_args, **_kwargs):
        raise error
    monkeypatch.setattr(calls, "invoke_decision_model_call", failure)
    stage = service.begin_decision_stage(host, params, operation_id="batch")
    assert decide(host, params, stage).status == "error"
    assert not policy._FAILURES
    monkeypatch.setattr(calls, "invoke_decision_model_call", successful)
    assert decide(host, params, stage).status == "success"


def test_admission_cause_is_structured_and_not_a_provider_cooldown(prepared, monkeypatch):
    host, params, _ = prepared
    def busy(*_args, **_kwargs):
        raise ProviderTransientError("arbitrary text") from ConcurrencyTimeout("arbitrary text")
    monkeypatch.setattr(calls, "invoke_decision_model_call", busy)
    result = decide(host, params, service.begin_decision_stage(host, params, operation_id="batch"))
    assert result.reason == "admission_busy" and not policy._FAILURES


@pytest.mark.parametrize("error", [InterruptedError("stop"), ToolCancelled("stop"), KeyboardInterrupt()])
def test_user_interruptions_are_never_swallowed(prepared, monkeypatch, error):
    host, params, _ = prepared
    def stopped(*_args, **_kwargs):
        raise error
    monkeypatch.setattr(calls, "invoke_decision_model_call", stopped)
    with pytest.raises(type(error)):
        decide(host, params, service.begin_decision_stage(host, params, operation_id="batch"))


def test_stable_resource_key_across_new_stages(prepared, monkeypatch):
    host, params, _ = prepared
    keys = []
    def captured(*args, **kwargs):
        keys.append(kwargs["resource_key"])
        return successful(*args, **kwargs)
    monkeypatch.setattr(calls, "invoke_decision_model_call", captured)
    for operation in ("first", "next"):
        assert decide(host, params, service.begin_decision_stage(host, params, operation_id=operation)).status == "success"
    assert keys[0] == keys[1]
