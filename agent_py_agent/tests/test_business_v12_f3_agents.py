"""真实thread/task/guidance/wake；只把未交付W1空间端口换成冻结合同fake。"""
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import ModuleType, SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
from agent_py_agent.agent.conversation.host_actor import HostChannelActor
from agent_py_agent.agent.conversation.session_tasks import SessionTaskDraft, SessionTaskUpdate


# LLM: pytest fixture为领域及W11测试创建临时真实Store和来源线程，替换模型选择、空间服务与上下文解析模块，并令模型执行直接失败；不代表真实空间授权联调。
# 函数用途: 提供能验证线程、任务、guidance和wake持久行为的离线宿主及可信通道Actor。
@pytest.fixture
def business_host(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations")
    source = store.threads.create(owner_id="owner", canonical_user_id="local-agent", cwd=str(tmp_path))
    host = SimpleNamespace(conversation_store=store, home_paths=SimpleNamespace(
        owner_id="owner", owner_home_dir=tmp_path, owner_kind="main", owner_provider="local"),
        config=SimpleNamespace(), run=lambda *a, **k: pytest.fail("identity operation must not call model"))
    actor = HostChannelActor("owner", source.thread_id, "entry", "local", "local", "user")
    import agent_py_agent.agent.settings.model_profiles as profiles
    import agent_py_agent.agent.settings.thread_model_selection as selection
    monkeypatch.setattr(selection, "default_model_profile_id", lambda h: "model-A")
    monkeypatch.setattr(profiles, "selected_model_config", lambda h, **k: SimpleNamespace(model_name="fake"))
    monkeypatch.setattr(selection, "selected_model_config", profiles.selected_model_config)
    spaces = ModuleType("agent_py_agent.agent.conversation.business_spaces")

    class Spaces:
        def __init__(self, host):
            self.host = host

        def get(self, space_id, *, actor):
            assert actor.owner_id == "owner"
            if space_id == "denied":
                raise PermissionError("denied")
            return {"space": {"space_id": space_id, "state": "active", "revision": 1}}

    spaces.BusinessSpaceService = Spaces
    monkeypatch.setitem(sys.modules, spaces.__name__, spaces)
    context = ModuleType("agent_py_agent.agent.conversation.business_context")

    # LLM: 仅供本fixture替换的上下文模块调用；按传入意图构造DTO，unassigned决定空间修订为空，不读存储或执行真实空间/资产授权。
    # 函数用途: 为测试任务返回固定格式上下文，兼容明确的无归属意图。
    def resolve_business_context(*, actor, task_id, requested_space_id, project_ref, binding_id,
                                 requested_method_refs=(), requested_asset_version_refs=(), unassigned=False, host=None):
        return BusinessExecutionContext(actor.owner_id, requested_space_id, project_ref, task_id, None if unassigned else 1,
                                        binding_id, requested_method_refs, requested_asset_version_refs, "business-execution-context.v1")

    context.resolve_business_context = resolve_business_context
    monkeypatch.setitem(sys.modules, context.__name__, context)
    return host, actor


def _service(business_host):
    from agent_py_agent.agent.conversation.background_agents import BackgroundAgentService
    host, actor = business_host
    return BackgroundAgentService(host), host, actor


def test_background_identity_canonical_and_concurrent_replay(business_host):
    service, host, actor = _service(business_host)
    request = {"space_id": "space-A", "name": "worker", "purpose": "build"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(pool.map(lambda _: service.create(request, actor=actor, idempotency_key="create"), range(4)))
    identities = {reply["agent"]["agent_id"] for reply in replies}
    assert len(identities) == 1
    view = replies[0]["agent"]
    thread = host.conversation_store.threads.require(view["thread_id"])
    assert view["agent_id"] == thread.thread_id
    assert view["managed_by_thread_id"] == actor.source_thread_id
    assert thread.model_profile_id == "model-A"
    assert not thread.channel_bindings and not thread.task_ids
    assert "parent_run_id" not in thread.metadata
    from agent_py_agent.agent.conversation.background_agents import BackgroundAgentService
    assert BackgroundAgentService(host).create(request, actor=actor, idempotency_key="create")["agent"] == view
    with pytest.raises((ValueError, RuntimeError), match="IDEMPOTENCY_CONFLICT"):
        service.create({**request, "space_id": "space-B"}, actor=actor, idempotency_key="create")


def test_controller_is_stable_not_the_source_chat(business_host):
    service, host, actor = _service(business_host)
    first = service.ensure_controller(actor=actor)
    second = service.ensure_controller(actor=actor)
    assert first["created"] is True and second["created"] is False
    assert first["agent_id"] == second["agent_id"] != actor.source_thread_id
    assert service.get_controller(actor=actor)["available"] is True
    assert "business_controller" not in host.conversation_store.threads.require(actor.source_thread_id).metadata


def test_identity_suspend_resume_retire_restore_and_model_isolation(business_host):
    service, host, actor = _service(business_host)
    view = service.create({"space_id": "space-A", "name": "worker"}, actor=actor, idempotency_key="create")["agent"]
    identifier = view["agent_id"]
    suspended = service.transition(identifier, "suspend", actor=actor, expected_revision=view["revision"], idempotency_key="suspend")
    assert suspended["agent"]["identity_state"] == "suspended"
    resumed = service.transition(identifier, "resume", actor=actor, expected_revision=suspended["agent"]["revision"], idempotency_key="resume")
    assert resumed["agent"]["identity_state"] == "available"
    selected = service.set_model(identifier, "model-B", actor=actor, expected_revision=1, idempotency_key="model")
    assert selected["selection_revision"] == 2 and selected["adoption_status"] == "applied"
    assert host.conversation_store.threads.require(actor.source_thread_id).model_profile_id != "model-B"
    retired = service.transition(identifier, "retire", actor=actor, expected_revision=resumed["agent"]["revision"], idempotency_key="retire", disposition="drain")
    assert retired["agent"]["identity_state"] == "retired"
    restored = service.transition(identifier, "restore", actor=actor, expected_revision=retired["agent"]["revision"], idempotency_key="restore")
    assert restored["agent"]["identity_state"] == "available"
    assert restored["agent"]["thread_id"] == identifier
    assert host.conversation_store.threads.require(identifier).model_profile_id == "model-B"


def test_retirement_retains_pending_delivery_and_task_responsibility(business_host):
    service, host, actor = _service(business_host)
    view = service.create({"space_id": "space-A", "name": "worker"}, actor=actor, idempotency_key="create")["agent"]
    store = host.conversation_store
    task = store.session_tasks.create(SessionTaskDraft(actor.source_thread_id, view["agent_id"], "work"))
    retired = service.transition(view["agent_id"], "retire", actor=actor, expected_revision=view["revision"], idempotency_key="retire", disposition="drain")
    assert retired["agent"]["identity_state"] == "retiring"
    assert task.task_id in retired["outstanding_refs"]
    with pytest.raises((ValueError, RuntimeError), match="AGENT_NOT_AVAILABLE"):
        service.assign({"agent_id": view["agent_id"], "space_id": "space-A", "instruction": "another"}, actor=actor, idempotency_key="assign")
    store.session_tasks.advance(task.task_id, SessionTaskUpdate(status="cancelled"))
    from agent_py_agent.agent.conversation.background_team_lifecycle import (
        reconcile_background_identity,
    )
    assert reconcile_background_identity(host, view["agent_id"]).metadata["background_agent"]["identity_state"] == "retired"


def test_assign_uses_one_original_task_body_wake_and_frozen_space(business_host):
    service, host, actor = _service(business_host)
    view = service.create({"space_id": "space-A", "name": "worker"}, actor=actor, idempotency_key="create")["agent"]
    request = {"agent_id": view["agent_id"], "space_id": "space-A", "instruction": "work"}
    reply = service.assign(request, actor=actor, idempotency_key="assign")
    again = service.assign(request, actor=actor, idempotency_key="assign")
    assert again["task_id"] == reply["task_id"]
    task = host.conversation_store.session_tasks.require(reply["task_id"])
    assert task.metadata["business_context"]["task_id"] == task.task_id
    assert task.metadata["business_context"]["space_id"] == "space-A"
    assert task.body_guidance_id
    wakes = [w for w in host.conversation_store.wakes.pending(limit=100) if w.metadata.get("session_task_id") == task.task_id]
    assert len(wakes) == 1
    with pytest.raises((ValueError, RuntimeError), match="SPACE_MISMATCH"):
        service.assign({**request, "space_id": "space-B"}, actor=actor, idempotency_key="different")


def test_business_service_rejects_cross_owner_and_schema_injection(business_host):
    service, host, actor = _service(business_host)
    with pytest.raises((PermissionError, ValueError)):
        service.create({"space_id": "space-A", "name": "bad"}, actor=replace(actor, owner_id="other"), idempotency_key="cross")
    from agent_py_agent.agent.plugin_host.contracts import HostAPIError
    with pytest.raises(HostAPIError):
        service.create({"space_id": "space-A", "name": "bad", "owner_id": "owner"}, actor=actor, idempotency_key="inject")
    assert host.conversation_store.session_tasks.list_report(limit=0) == ([], [])


def test_business_action_adapter_uses_frozen_schema_and_original_stores(business_host):
    from agent_py_agent.agent.conversation.background_agents import BackgroundAgentService
    from agent_py_agent.agent.conversation.session_messaging import queue_owned_background_message
    from agent_py_agent.agent.plugin_host.business_action_specs import business_action_spec
    from agent_py_agent.agent.plugin_host.schemas import validate_object
    host, actor = business_host
    service = BackgroundAgentService(host)
    create_spec = business_action_spec("background_agents.create")
    create_params = validate_object({"space_id": "space-A", "name": "worker"}, create_spec.params_schema)
    create = create_spec.validate_result(service.create(create_params, actor=actor, idempotency_key="adapter-create"))
    identifier = create["agent"]["agent_id"]
    for action, request, invoke in (
        ("background_agents.get", {"agent_id": identifier}, lambda params: service.get(params["agent_id"], actor=actor)),
        ("background_agents.list", {}, lambda params: service.list(params, actor=actor)),
        ("background_agents.model.get", {"agent_id": identifier}, lambda params: service.get_model(params["agent_id"], actor=actor)),
    ):
        spec = business_action_spec(action)
        result = invoke(validate_object(request, spec.params_schema))
        spec.validate_result(result)
    message_spec = business_action_spec("background_agents.messages.send")
    message_params = validate_object(
        {"agent_id": identifier, "text": "extra requirement", "input_kind": "guidance"},
        message_spec.params_schema,
    )
    message = message_spec.validate_result(queue_owned_background_message(
        host, actor=actor, target=service._thread(message_params["agent_id"], actor),
        text=message_params["text"], input_kind=message_params["input_kind"],
        operation_key=service._operation_key(actor, "message"),
    ))
    assert message["target_thread_id"] == identifier and message["receipt_state"] == "accepted"
    submit_spec = business_action_spec("tasks.submit")
    submit_params = validate_object({"space_id": "space-A", "instruction": "work"}, submit_spec.params_schema)
    reply = submit_spec.validate_result(service.submit(submit_params, actor=actor, idempotency_key="submit"))
    assert host.conversation_store.session_tasks.require(reply["task_id"]).metadata["business_context"]["space_id"] == "space-A"


def test_suspended_identity_admission_preserves_queued_source_without_model(business_host):
    from agent_py_agent.agent.conversation.background_execution import BackgroundCapacityDeferred
    from agent_py_agent.agent.conversation.runtime import BackgroundMainAgentRuntime
    service, host, actor = _service(business_host)
    view = service.create({"space_id": "space-A", "name": "worker"}, actor=actor, idempotency_key="create")["agent"]
    service.transition(view["agent_id"], "suspend", actor=actor, expected_revision=view["revision"], idempotency_key="suspend")
    runtime = BackgroundMainAgentRuntime(agent=host, store=host.conversation_store, channels=SimpleNamespace())
    with pytest.raises(BackgroundCapacityDeferred):
        runtime.run_once({"thread_id": view["agent_id"], "task_id": "", "reason": "session_task"})
    assert host.conversation_store.session_tasks.list_report(limit=0) == ([], [])


def test_follow_up_cannot_create_task_after_suspension(business_host):
    service, host, actor = _service(business_host)
    view = service.create({"space_id": "space-A", "name": "worker"}, actor=actor, idempotency_key="create")["agent"]
    assignment = service.assign({"agent_id": view["agent_id"], "space_id": "space-A", "instruction": "first"},
                                actor=actor, idempotency_key="first")
    service.transition(view["agent_id"], "suspend", actor=actor, expected_revision=view["revision"], idempotency_key="suspend")
    with pytest.raises(ValueError, match="AGENT_NOT_AVAILABLE"):
        service.follow_up({"task_id": assignment["task_id"], "instruction": "next"}, actor=actor, idempotency_key="next")
    assert len(host.conversation_store.session_tasks.list_report(limit=0)[0]) == 1


def test_retirement_preserves_open_capability_and_unreadable_subtree(business_host):
    from agent_py_agent.agent.subagents.model_capabilities import CapabilityRequest
    service, host, actor = _service(business_host)
    view = service.create({"space_id": "space-A", "name": "worker"}, actor=actor, idempotency_key="create")["agent"]
    child = SimpleNamespace(id="child", status="DONE", attributes={"conversation_thread_id": view["agent_id"]},
                            capability_requests=[CapabilityRequest("request", "child", "need", "write")])
    host.subagents = SimpleNamespace(list_runs=lambda: [child], list_runs_report=lambda: SimpleNamespace(runs=[child], load_errors=[{"error": "bad other child"}]))
    retired = service.transition(view["agent_id"], "retire", actor=actor, expected_revision=view["revision"],
                                  idempotency_key="retire", disposition="drain")
    assert retired["agent"]["identity_state"] == "retiring"
    assert "request" in retired["outstanding_refs"] and "subagents:unreadable" in retired["outstanding_refs"]


def test_suspended_messages_are_durable_and_resume_republishes_original_input(business_host):
    from agent_py_agent.agent.conversation.session_messaging import queue_owned_background_message
    service, host, actor = _service(business_host)
    view = service.create({"space_id": "space-A", "name": "worker"}, actor=actor, idempotency_key="create")["agent"]
    suspend = service.transition(view["agent_id"], "suspend", actor=actor, expected_revision=view["revision"], idempotency_key="suspend")
    request = {"agent_id": view["agent_id"], "text": "wait for resume", "input_kind": "guidance"}
    queue_owned_background_message(
        host, actor=actor, target=service._thread(view["agent_id"], actor),
        text=request["text"], input_kind=request["input_kind"],
        operation_key=service._operation_key(actor, "input"),
    )
    assert host.conversation_store.wakes.pending(limit=0) == []
    service.transition(view["agent_id"], "resume", actor=actor, expected_revision=suspend["agent"]["revision"], idempotency_key="resume")
    pending = host.conversation_store.guidance.pending("thread", view["agent_id"], limit=0)
    assert len(pending) == 1
    wakes = host.conversation_store.wakes.pending(limit=0)
    assert len(wakes) == 1 and wakes[0].metadata["message_dedupe_key"] == pending[0].metadata["message_dedupe_key"]


def test_execution_cancel_closes_exact_authority_and_retains_exit_responsibility(business_host, tmp_path):
    from agent_py_agent.agent.conversation.background_agents import cancel_business_execution
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    service, host, actor = _service(business_host)
    submitted = service.submit({"space_id": "space-A", "instruction": "work"}, actor=actor, idempotency_key="submit")
    task, context = service._owned_task(submitted["task_id"], actor)
    repo = RuntimeRepository(tmp_path / "runtime.db")
    record = repo.record_run_creation(owner_id=actor.owner_id, conversation_task_id="runtime-root", run_id="exact-execution",
        thread_id=task.target_thread_id, attempt_status="running",
        task_run_metadata={"business_context": context.to_dict(), "session_task_ref": task.task_id})
    host.conversation_store.session_tasks.bind_execution(task.task_id, task_run_id=record["task_run_id"], target_thread_id=task.target_thread_id)
    def missing(run_id):
        raise FileNotFoundError(run_id)
    host.subagents = SimpleNamespace(runtime_db=repo, load=missing)
    result = cancel_business_execution(
        host, service, {"run_id": "exact-execution", "attempt_id": record["attempt_id"], "include_descendants": True}, actor=actor)
    assert repo.get_agent_run(record["agent_run_id"])["status"] == "cancelled"
    assert result["state"] == "pending" and result["unconfirmed_refs"] == ["exact-execution"]
    assert host.conversation_store.session_tasks.require(task.task_id).metadata["cancellation_control"]["outstanding_refs"] == result["unconfirmed_refs"]
