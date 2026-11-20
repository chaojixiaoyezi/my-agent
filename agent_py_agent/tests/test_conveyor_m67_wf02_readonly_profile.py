"""WF-02 的原派活路径、效果限制和完成回执。"""
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.call_runtime import model_request_token_budget
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.backends.request_scope import (
    provider_request_budget,
    provider_request_timeout,
)
from agent_py_agent.agent.common.cancellation import ToolCancelled
from agent_py_agent.agent.conversation.authority import CONVERSATION_SESSION_TASK_ID_ATTR
from agent_py_agent.agent.conversation.background_execution import _run_background_model_attempt
from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
from agent_py_agent.agent.conversation.host_actor import HostChannelActor
from agent_py_agent.agent.conversation.session_tasks import SessionTaskUpdate
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts.business_api import (
    BusinessServices,
    _BusinessBackgroundService,
    bind_business_context,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.executor import ToolExecutor, ToolExecutorRequest
from agent_py_agent.agent.tooling.models import (
    EffectResolverPolicy,
    IdempotencyPolicy,
    ToolRuntimePolicy,
)
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.workflows.agent_steps import (
    WorkflowAgentSteps,
    build_default_workflow_agent_steps,
)
from agent_py_agent.agent.workflows.analysis_isolation import (
    AGENT_TASK_PROFILE_KEY,
    ReadOnlyAgentTaskProfile,
    WorkflowIsolationUnavailable,
    resolve_analysis_isolation,
)
from agent_py_agent.agent.workflows.business_scope import WorkflowScopeService
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from agent_py_agent.tests.test_tool_runtime_unification import _call, _CountingTool, _snapshot


def test_available_agent_task_port_resolves_the_read_only_profile():
    class Background:
        def submit(self, request, *, actor, idempotency_key, execution_profile=None):
            raise AssertionError("解析隔离不应提交任务")

    port = WorkflowAgentSteps(
        Background(),
        actor_provider=lambda _run: SimpleNamespace(owner_id="local/main"),
        completion_reader=lambda *_: None,
        cancel_handler=lambda *_: None,
    )
    result = resolve_analysis_isolation(
        {"analysis_isolation": "lightweight_subagent"}, isolation_executor=port,
    )
    assert result["mode"] == "agent_task"
    assert result["profile"] == "read_only"
    with pytest.raises(WorkflowIsolationUnavailable) as caught:
        resolve_analysis_isolation(
            {"analysis_isolation": "lightweight_subagent", "ephemeral_debug": True}, isolation_executor=port,
        )
    assert caught.value.effect_outcome == "not_started"


@pytest.mark.parametrize("executor", [None, lambda: None])
def test_isolation_unavailable_without_a_capable_host(executor):
    with pytest.raises(WorkflowIsolationUnavailable) as caught:
        resolve_analysis_isolation({"analysis_isolation": "lightweight_subagent"}, isolation_executor=executor)
    assert caught.value.error_code == "WORKFLOW_ISOLATION_UNAVAILABLE"


@pytest.fixture(params=["profile", "analysis_isolation"])
def flow(tmp_path, request):
    home = replace(home_paths(tmp_path / "isolated-home"), owner_id="local/main",
                   owner_provider="local", owner_kind="main")
    store = ConversationStore(home.owner_home_dir / "conversations")
    host = SimpleNamespace(home_paths=home, conversation_store=store,
                           config=AgentConfig(business_spaces_enabled=True, model_backend="echo"),
                           _current_run_params=RunParams(task_attributes={"token_budget": 128}))
    services = BusinessServices(host)
    owner = home.owner_id
    origin = store.threads.create(owner_id=owner, owner_home=str(services.owner_home))
    actor = HostChannelActor(owner, origin.thread_id, "source-request", "local", "local", "human")
    native = bind_business_context(host, actor=actor, authorize=lambda *_: True)
    caller = services.actor(native)
    background = _BusinessBackgroundService(services, native)
    port = build_default_workflow_agent_steps(host, background_service=background, actor_provider=lambda _: actor)
    assert port.supports_read_only_profile()
    space = services.spaces.create({"name": "只读工作流"}, actor=caller, idempotency_key="m67-space")
    context = BusinessExecutionContext(owner, space["space_id"], None, "parent-task",
                                       space["revision"], None, (), (), "business-context.v1")
    api = WorkflowScopeService(home.owner_home_dir, owner_id=owner, authorize=lambda *_: True,
        context_provider=lambda *_: context, agent_steps=port, engine_factory=WorkflowEngine)
    step = {"step_id": "inspect", "name": "核验记录", "mode": "agent_task", "timeout_seconds": 90,
            "config": {"instruction": "核验业务记录"}}
    if request.param == "profile":
        step["profile"] = "read_only"
    else:
        step["config"]["analysis_isolation"] = "lightweight_subagent"
    definition = {
        "descriptor": {"id": "readonly-flow", "kind": "workflow", "name": "读取", "access_targets": [],
                       "data_sources": [], "credential_refs": [], "io_contract": {}},
        "steps": [step],
    }
    api.execute("workflows.create", {"definition": definition}, actor=caller, idempotency_key="m67-definition")
    started = time.time()
    with provider_request_budget(45):
        receipt = api.execute("workflows.run", {"workflow_ref": "readonly-flow"}, actor=caller,
                              idempotency_key="m67-run")
    runs = WorkflowRunStore(home.owner_home_dir)
    run = runs.load_run(receipt["workflow_run_id"])
    assert run.status == "waiting_for_agent", (run.steps[0].error_code, run.steps[0].error_message, receipt)
    task = store.session_tasks.require(run.steps[0].escalation_result["task_id"])
    profile = task.metadata[AGENT_TASK_PROFILE_KEY]
    assert profile == run.steps[0].escalation_result[AGENT_TASK_PROFILE_KEY]
    assert profile["space_id"] == space["space_id"]
    assert started < profile["deadline_at"] < started + 60
    assert profile["token_budget"] == 128
    params = RunParams(run_id=task.task_id, task_id=task.task_id, business_context=context,
        task_attributes={CONVERSATION_SESSION_TASK_ID_ATTR: task.task_id,
                         "conversation_thread_id": task.target_thread_id})
    return SimpleNamespace(host=host, api=api, caller=caller, runs=runs, run=run, port=port,
                           tasks=store.session_tasks, task=task, params=params)


def test_profile_routes_reads_and_rejects_writes_before_the_handler(tmp_path, flow):
    tool = _CountingTool()
    tool.runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("read_only", by_parameter=(("value", (("save", "mutating"),)),)),
        idempotency_policy=IdempotencyPolicy("operation"),
    )
    calls = []

    def model(_prompt, *, params):
        assert params.save is False and params.write_boundary["effect_policy"] == "read_only"
        assert params.business_context.space_id == flow.task.metadata["business_context"]["space_id"]
        assert 0 < provider_request_timeout(100) < 60
        flow.host._current_run_params = params
        assert model_request_token_budget(flow.host, input_tokens=12).output_tokens <= 128
        for value in ("inspect", "save"):
            call = replace(_call(tool, {"value": value}), run_id=flow.task.task_id, operation_id="", idempotency_key="")
            calls.append(ToolExecutor().execute(ToolExecutorRequest(
                call=call, runtime_snapshot=_snapshot(tool, run_id=flow.task.task_id), workspace_root=tmp_path,
                write_boundary=params.write_boundary, approval_mode="auto", operation_store_required=False,
            )))
        return SimpleNamespace(response="读取成功")

    flow.host.run = model
    _run_background_model_attempt(flow.host, "核验", flow.params)
    assert calls[0].result.ok
    assert calls[1].result.error_code == "INVESTIGATION_READ_ONLY_REJECTED"
    assert calls[1].result.effect_outcome == "not_started"
    assert calls[1].decision.status == "deny"
    assert tool.executions == 1
    flow.tasks.bind_turn(flow.task.task_id, turn_id=flow.task.task_id)
    flow.tasks.advance(flow.task.task_id, SessionTaskUpdate(status="done", summary="已核验"))
    completed = flow.port.completion(flow.run, flow.run.steps[0])
    assert completed["status"] == "completed" and completed["output"]["summary"] == "已核验"
    updated = flow.api.execute("workflow_runs.resume", {"workflow_run_id": flow.run.workflow_run_id},
        actor=flow.caller, idempotency_key="m67-resume", expected_revision=flow.run.revision)
    assert updated["state"] == "completed"
    resumed = flow.runs.load_run(flow.run.workflow_run_id)
    assert resumed.status == "succeeded" and resumed.steps[0].output_snapshot["summary"] == "已核验"


def test_expired_profile_is_settled_before_model_dispatch(flow, monkeypatch):
    monkeypatch.setattr(ReadOnlyAgentTaskProfile, "remaining_seconds", lambda _: 0)
    flow.host.run = lambda *_a, **_k: pytest.fail("过期任务启动了模型")
    with pytest.raises(TimeoutError, match="期限"):
        _run_background_model_attempt(flow.host, "核验", flow.params)
    receipt = flow.port.completion(flow.run, flow.run.steps[0])
    assert receipt["status"] == "failed" and receipt["error_code"] == "TOOL_TIMEOUT"


def test_cancel_uses_original_task_and_blocks_late_model_dispatch(flow):
    flow.host.run = lambda *_a, **_k: pytest.fail("已取消任务启动了模型")
    result = flow.api.execute("workflow_runs.cancel", {"workflow_run_id": flow.run.workflow_run_id},
        actor=flow.caller, idempotency_key="m67-cancel", expected_revision=flow.run.revision)
    assert result["state"] == "completed"
    assert flow.runs.load_run(flow.run.workflow_run_id).status == "cancelled"
    with pytest.raises(ToolCancelled):
        _run_background_model_attempt(flow.host, "核验", flow.params)
    assert flow.tasks.require(flow.task.task_id).status == "cancelled"


def test_profile_binding_refuses_an_unrelated_turn(flow):
    flow.host.run = lambda *_a, **_k: pytest.fail("无关回合启动了模型")
    wrong_turn = replace(flow.params, run_id="another-turn", task_id="another-task")
    with pytest.raises(PermissionError, match="WORKFLOW_AGENT_DISPATCH_CONTEXT_MISMATCH"):
        _run_background_model_attempt(flow.host, "核验", wrong_turn)
