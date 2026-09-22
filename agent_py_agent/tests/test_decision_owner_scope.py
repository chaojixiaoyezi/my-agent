"""用户级后台决策复用原配置/账本，不能冒用或创建会话身份。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.call_runtime import model_call_summary
from agent_py_agent.agent.conversation import decision_model_call as calls
from agent_py_agent.agent.conversation import decision_service as service
from agent_py_agent.tests.test_decision_model_profiles import decision
from agent_py_agent.tests.test_decision_protocol import questions
from agent_py_agent.tests.test_decision_service import successful
from agent_py_agent.tests.test_decision_service_http import server as server
from agent_py_agent.tests.test_decision_settings import host_at, patch


# LLM: 只构造原后台run及临时owner配置，材料里的历史thread不获得调用身份。
# 函数用途: 为后台身份合同生成不带ConversationThread的宿主请求。
def configured(tmp_path, url="http://127.0.0.1:1"):
    host = host_at(tmp_path)
    key, _ = decision(host, api_base=url)
    patch(host, {"enabled": True, "profile_id": key, "points.curator.mode": "apply"})
    params = SimpleNamespace(request_id="", run_id="curator-run", task_id="", thread_id="", task_attributes={})
    return host, params


# LLM: 调用真实可选服务，消费者仍只读取响应，不写记忆或控制游标。
# 函数用途: 对同一个后台阶段请求结构化建议。
def invoke(host, params, stage):
    return service.decide(host, params, stage, point="curator", state={"history_thread": "untrusted-thread"},
        questions=questions(), candidates_revision="batch-digest")


def test_owner_background_http_uses_original_run_without_thread(tmp_path, server, monkeypatch):
    host, params = configured(tmp_path, server.url)
    monkeypatch.setattr(host.conversation_store.threads, "load", lambda *_: pytest.fail("后台不能读取材料中的会话"))
    stage = service.begin_decision_stage(host, params, operation_id=params.run_id, scope="owner_background")
    outcome = invoke(host, params, stage)
    assert outcome.status == "success" and outcome.may_apply
    assert outcome.response.binding.thread_id == "" and outcome.response.binding.run_id == params.run_id
    summary = model_call_summary(host, run_id=params.run_id)
    assert summary["purpose_breakdown"]["decision"]["physical_model_attempt_count"] == 1
    assert summary["provider_http_attempt_count"] == 1
    record = host._model_call_ledger.records()[0]
    assert record.metadata["thread_id"] == "" and record.run_id == params.run_id
    assert not getattr(params, "live_archive_state", None)


@pytest.mark.parametrize("scope,attrs,run_id", [
    ("thread", {}, "curator-run"),
    ("owner_background", {}, ""),
    ("owner_background", {"conversation_thread_id": "history-thread"}, "curator-run"),
    ("invalid", {}, "curator-run"),
])
def test_background_scope_requires_explicit_run_and_no_thread(tmp_path, scope, attrs, run_id):
    host, params = configured(tmp_path)
    params.task_attributes, params.run_id = attrs, run_id
    stage = service.begin_decision_stage(host, params, operation_id="batch", scope=scope)
    assert stage.error_code == "invalid_identity"
    assert not invoke(host, params, stage).may_apply
    assert not hasattr(host, "_model_call_ledger")


def test_background_cannot_relabel_active_runner(tmp_path):
    host, params = configured(tmp_path)
    host._current_task_attributes = {"conversation_thread_id": "active-thread"}
    stage = service.begin_decision_stage(host, params, operation_id="batch", scope="owner_background")
    assert stage.error_code == "invalid_identity"


def test_background_rejects_direct_thread_identity_too(tmp_path):
    host, params = configured(tmp_path)
    params.thread_id = "historical-thread"
    stage = service.begin_decision_stage(host, params, operation_id="batch", scope="owner_background")
    assert stage.error_code == "invalid_identity"


def test_background_budget_is_frozen_and_caller_can_shorten(tmp_path, monkeypatch):
    host, params = configured(tmp_path)
    patch(host, {"stage_timeout_seconds": 100, "background_timeout_seconds": 7})
    now = [100.0]
    monkeypatch.setattr(service, "time", SimpleNamespace(monotonic=lambda: now[0]))
    stage = service.begin_decision_stage(host, params, operation_id="batch", scope="owner_background", caller_deadline=105)
    assert stage.deadline == 105
    patch(host, {"background_timeout_seconds": 90})
    captured = []

    def call(*args, **kwargs):
        captured.append(kwargs["deadline"])
        return successful(*args, **kwargs)

    monkeypatch.setattr(calls, "invoke_decision_model_call", call)
    now[0] = 104
    assert invoke(host, params, stage).status == "success"
    assert captured == [105]
    now[0] = 105
    assert invoke(host, params, stage).status == "deadline" and captured == [105]


def test_background_does_not_inherit_unrelated_thread_overrides(tmp_path, monkeypatch):
    host, params = configured(tmp_path)
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    patch(host, {"enabled": False}, scope="thread", thread_id=thread.thread_id)
    monkeypatch.setattr(calls, "invoke_decision_model_call", successful)
    stage = service.begin_decision_stage(host, params, operation_id="batch", scope="owner_background")
    assert invoke(host, params, stage).may_apply
    patch(host, {"enabled": False})
    assert invoke(host, params, stage).status == "off"
