"""原 user_config 的脱敏决策目录与显式连接测试；只用临时目录、本地 HTTP 和原工具执行器。"""
import json

import pytest

from agent_py_agent.agent.runtime_context import (
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent_py_agent.agent.settings import decision_probe, model_profiles, thread_model_selection
from agent_py_agent.agent.settings.shared_model_catalog import set_shared_profile
from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool
from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call
from agent_py_agent.tests.test_decision_model_operations import setup_probe
from agent_py_agent.tests.test_decision_model_profiles import decision
from agent_py_agent.tests.test_decision_service_http import server as server
from agent_py_agent.tests.test_decision_settings import host_at
from agent_py_agent.tests.test_model_profiles import add
from agent_py_agent.tests.test_shared_model_catalog import admin_host


@pytest.fixture
def configured(tmp_path):
    host = host_at(tmp_path)
    key, _ = decision(host)
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    previous = set_current_subagent_context(host, run_id="current-run", task_attributes={"agent_thread_id": thread.thread_id})
    try:
        yield host, key, thread, UserConfigTool(host)
    finally:
        restore_current_subagent_context(host, previous)


def test_directory_lists_only_current_private_and_shared_decision_profiles(configured, tmp_path, monkeypatch):
    host, key, thread, tool = configured
    generation, _ = add(host)
    foreign, _ = decision(host_at(tmp_path, owner="bob"), model_name="foreign-private")
    admin = admin_host(tmp_path / "config")
    shared, _ = decision(admin, model_name="shared-decision")
    hidden, _ = decision(admin, model_name="admin-private")
    set_shared_profile(admin, shared, True)
    original = model_profiles.model_profiles_path(host.home_paths).read_bytes()
    monkeypatch.setattr(decision_probe, "probe_decision_model", lambda *_a, **_kw: pytest.fail("读取不能触发联网测试"))
    monkeypatch.setattr(thread_model_selection, "thread_model_profile_id", lambda *_a, **_kw: pytest.fail("目录不得初始化普通模型选择"))
    outcome = tool.execute({"action": "decision_models"})
    assert outcome.ok
    report = json.loads(outcome.output)
    ids = {row["id"] for row in report["profiles"]}
    assert ids == {key, "shared:" + shared}
    assert not ids.intersection({generation, foreign, hidden, "default"})
    assert all(row["capability"] == "decision" for row in report["profiles"])
    assert report["scope_resolution"] == {"source": "current_runner_context", "effective": {"thread_id": thread.thread_id, "scope": "owner"}}
    assert outcome.result_envelope["decision_report"] == report
    assert "only-private-secret" not in outcome.output and "deployment-secret" not in outcome.output
    assert model_profiles.model_profiles_path(host.home_paths).read_bytes() == original
    assert not hasattr(host, "_model_call_ledger")


@pytest.mark.parametrize("operation", ["decision_models", "decision_probe"])
@pytest.mark.parametrize("extra", ["owner_id", "owner_provider", "thread_id", "run_id", "task_id", "scope", "api_key", "api_base", "custom_headers"])
def test_new_actions_reject_extra_identity_and_connection_fields_before_dispatch(configured, monkeypatch, operation, extra):
    _host, key, _thread, tool = configured
    monkeypatch.setattr(model_profiles, "execute_model_profile_operation", lambda *_a, **_kw: pytest.fail("参数非法不得转交操作"))
    params = {"action": operation, extra: "forbidden-secret"}
    if operation == "decision_probe":
        params.update(profile_id=key, timeout_seconds=1)
    result = tool.execute(params)
    assert not result.ok and result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert result.effect_outcome == "not_started" and "forbidden-secret" not in result.output


@pytest.mark.parametrize("seconds", [True, False, 0, -1, None, "1", float("nan"), float("inf"), -float("inf"), 10**1000])
def test_probe_requires_finite_positive_explicit_budget(configured, monkeypatch, seconds):
    _host, key, _thread, tool = configured
    monkeypatch.setattr(model_profiles, "execute_model_profile_operation", lambda *_a, **_kw: pytest.fail("预算非法不得开始操作"))
    result = tool.execute({"action": "decision_probe", "profile_id": key, "timeout_seconds": seconds})
    assert not result.ok and result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert result.effect_outcome == "not_started"


@pytest.mark.parametrize("missing", ["profile_id", "timeout_seconds"])
def test_probe_has_no_implicit_profile_or_timeout_default(configured, monkeypatch, missing):
    _host, key, _thread, tool = configured
    monkeypatch.setattr(model_profiles, "execute_model_profile_operation", lambda *_a, **_kw: pytest.fail("缺显式参数不得探测"))
    params = {"action": "decision_probe", "profile_id": key, "timeout_seconds": 1}
    params.pop(missing)
    assert tool.execute(params).error_code == "TOOL_INVALID_ARGUMENTS"


@pytest.mark.parametrize("profile_id", ["", "default", "jev-test", "../../other", None])
def test_probe_only_accepts_explicit_saved_reference(configured, monkeypatch, profile_id):
    _host, _key, _thread, tool = configured
    monkeypatch.setattr(model_profiles, "execute_model_profile_operation", lambda *_a, **_kw: pytest.fail("引用非法不得探测"))
    assert tool.execute({"action": "decision_probe", "profile_id": profile_id, "timeout_seconds": 1}).error_code == "TOOL_INVALID_ARGUMENTS"


def test_probe_requires_current_thread_but_owner_directory_can_be_read_without_one(tmp_path, monkeypatch):
    host = host_at(tmp_path)
    key, _ = decision(host)
    tool = UserConfigTool(host)
    assert tool.execute({"action": "decision_models"}).ok
    monkeypatch.setattr(model_profiles, "execute_model_profile_operation", lambda *_a, **_kw: pytest.fail("无可信会话不能探测"))
    result = tool.execute({"action": "decision_probe", "profile_id": key, "timeout_seconds": 1})
    assert not result.ok and result.error_code == "TOOL_PERMISSION_DENIED"
    assert result.effect_outcome == "not_started"
    for action in ("decision_models", "decision_probe"):
        assert UserConfigTool().execute({"action": action}).error_code == "TOOL_PERMISSION_DENIED"


def test_probe_passes_only_runner_thread_and_exact_original_operation_payload(configured, monkeypatch):
    host, key, thread, tool = configured
    host._current_task_attributes = {"conversation_thread_id": "stale-host-thread"}
    seen = []

    def operation(context, action, payload, *, thread_id):
        seen.append((context, action, payload, thread_id))
        return {"ok": True, "model_name": "jev-test", "elapsed_seconds": 0.01, "usage": {"input_tokens": 0}, "error_type": "", "message": "测试完成"}

    monkeypatch.setattr(model_profiles, "execute_model_profile_operation", operation)
    outcome = tool.execute({"action": "decision_probe", "profile_id": key, "timeout_seconds": 0.5})
    assert outcome.ok
    assert seen == [(host, "decision_probe", {"profile_id": key, "timeout_seconds": 0.5}, thread.thread_id)]
    report = json.loads(outcome.output)
    assert report["usage"]["input_tokens"] == 0
    assert report["scope_resolution"] == {"source": "current_runner_context", "effective": {"thread_id": thread.thread_id, "scope": "thread"}}


@pytest.mark.parametrize("error", [InterruptedError("停止"), KeyboardInterrupt()])
def test_user_stop_is_never_reported_as_probe_failure(configured, monkeypatch, error):
    _host, key, _thread, tool = configured

    def stopped(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(model_profiles, "execute_model_profile_operation", stopped)
    with pytest.raises(type(error)):
        tool.execute({"action": "decision_probe", "profile_id": key, "timeout_seconds": 1})


def test_explicit_probe_runs_original_http_and_records_usage_for_current_thread(tmp_path, server, monkeypatch):
    host, key, thread = setup_probe(tmp_path, server, monkeypatch, usage={"input_tokens": 0})
    previous = set_current_subagent_context(host, run_id="active-run", task_attributes={"conversation_thread_id": thread.thread_id})
    try:
        tool = UserConfigTool(host)
        execution = execute_canonical_test_call(tmp_path, tools={"user_config": tool}, tool_name="user_config",
            arguments={"action": "decision_probe", "profile_id": key, "timeout_seconds": 5})
    finally:
        restore_current_subagent_context(host, previous)
    assert execution.result.status == "succeeded", execution.result
    assert len(server.requests) == 1 and server.requests[0][0] == "/v1/systemone"
    assert server.requests[0][1]["state"] == {"purpose": "explicit_connection_test", "value": "ready"}
    totals = host.conversation_store.model_usage.summary(thread.thread_id)
    assert totals["event_count"] == 1 and totals["purpose_breakdown"]["decision"]["physical_model_attempt_count"] == 1
    assert host._model_call_ledger.records()[0].metadata["thread_id"] == thread.thread_id


def test_real_probe_failure_keeps_false_tool_status_and_original_safe_report(tmp_path, server, monkeypatch):
    host, key, thread = setup_probe(tmp_path, server, monkeypatch)
    from agent_py_agent.tests import test_decision_service_http as http_fixture

    monkeypatch.setattr(http_fixture, "response", lambda: {"model": "jev-test", "answers": {"connection": {
        "type": "choice", "choice": "unknown", "confidence": 1.0, "probabilities": {"ready": 0.0, "unknown": 1.0}}}})
    previous = set_current_subagent_context(host, run_id="active-run", task_attributes={"agent_thread_id": thread.thread_id})
    try:
        outcome = UserConfigTool(host).execute({"action": "decision_probe", "profile_id": key, "timeout_seconds": 5})
    finally:
        restore_current_subagent_context(host, previous)
    assert not outcome.ok and outcome.error_code == "TOOL_EXECUTION_FAILED"
    assert outcome.reported_error_code == "DECISION_PROBE_FAILED"
    report = json.loads(outcome.output)
    assert report["ok"] is False and report["error_type"] == "DecisionProbeAnswerInvalid", report
    assert outcome.result_envelope["decision_report"] == report
    assert report["usage"]["input_tokens"] is None
    assert "only-private-secret" not in outcome.output and len(server.requests) == 1


def test_foreign_runner_thread_cannot_probe_current_owner_connection(tmp_path, server, monkeypatch):
    host, key, _thread = setup_probe(tmp_path, server, monkeypatch)
    foreign = host.conversation_store.threads.get_or_create({"canonical_user_id": "bob", "owner_id": "bob"})
    previous = set_current_subagent_context(host, run_id="active-run", task_attributes={"agent_thread_id": foreign.thread_id})
    try:
        outcome = UserConfigTool(host).execute({"action": "decision_probe", "profile_id": key, "timeout_seconds": 1})
    finally:
        restore_current_subagent_context(host, previous)
    assert not outcome.ok and not server.requests
    assert json.loads(outcome.output)["error_type"] == "DecisionSettingsAccessError"


def test_original_tool_gate_can_block_probe_before_handler(configured, tmp_path, monkeypatch):
    _host, key, _thread, tool = configured
    monkeypatch.setattr(model_profiles, "execute_model_profile_operation", lambda *_a, **_kw: pytest.fail("权限拒绝不得进入模型操作"))
    execution = execute_canonical_test_call(tmp_path, tools={"user_config": tool}, tool_name="user_config", allowed_tools=[],
        arguments={"action": "decision_probe", "profile_id": key, "timeout_seconds": 1})
    assert execution.result.status != "succeeded"
