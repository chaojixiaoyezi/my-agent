# LLM: 真实受管Agent与原账号/认证/治理存储；每个关口只观察是否在新效果前拒绝，替身仅记录本应未发生的下游调用。
# 模块用途: 验证受管原代次的只收窄绑定，以及停用/修订/安全/治理变化在各实际效果关口的拒绝与已接纳结果的保留。
from __future__ import annotations

import copy
import json
import pickle
from contextlib import contextmanager
from dataclasses import fields, replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.backends import BaseBackend, ModelResponse
from agent_py_agent.agent.common.managed_owner_generation import (
    ManagedOwnerGeneration,
    ManagedOwnerGenerationError,
    bind_managed_owner_generation,
    current_managed_owner_generation,
    require_agent_managed_owner_generation,
    require_managed_owner_generation,
)
from agent_py_agent.agent.gateway_parts.managed_owner_runtime import prepare_managed_owner_runtime
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.local_user_lifecycle import LocalUserLifecycle
from agent_py_agent.agent.user_space.local_user_store import LocalUserStore
from agent_py_agent.agent.user_space.managed_user_governance import ManagedUserGovernanceService
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

ALICE = OwnerIdentity.local_user("alice")
CHANGES = {"disable": "USER_DISABLED", "revision": "USER_REVISION_CHANGED",
           "security": "MANAGED_OWNER_SECURITY_CHANGED", "policy": "MANAGED_OWNER_POLICY_CHANGED"}


@pytest.fixture
def host(tmp_path):
    root = (tmp_path / "host").resolve()
    lifecycle = LocalUserLifecycle(LocalUserStore(root))
    lifecycle.create("alice", idempotency_key="managed_alice_0001")
    agent = prepare_managed_owner_runtime(root, ALICE, host_options={})
    return SimpleNamespace(root=root, lifecycle=lifecycle, agent=agent, generation=agent._managed_owner_generation,
                           home=LocalUserStore(root).owner_home("alice"), tmp=tmp_path)


# LLM: 四类变化均走原管理员/认证入口；revision经停用再启用，账号仍启用但修订已前进。
# 函数用途: 对受管用户施加一种权威变化并返回预期拒绝码。
def _change(host, kind):
    if kind in {"disable", "revision"}:
        host.lifecycle.disable("alice", idempotency_key="disable_alice_0001")
    if kind == "revision":
        host.lifecycle.enable("alice", idempotency_key="enable_alice_00001")
    if kind == "security":
        from agent_py_agent.agent.user_space.totp_sso_auth import TotpSSOAuthService

        TotpSSOAuthService(host.root).update_sso_config({"protocol": "oidc", "issuer_url": "https://idp.example.test",
                                                         "client_id": "managed-client",
                                                         "redirect_uri": "https://host.example.test/callback"})
    if kind == "policy":
        ManagedUserGovernanceService(host.root).set_policy("alice", {"persona_mode": "admin_locked"}, expected_revision=0)
    return CHANGES[kind]


def _code(call):
    with pytest.raises(ManagedOwnerGenerationError) as caught:
        call()
    return caught.value.code


class _Tool:
    def __init__(self, effect):
        from agent_py_agent.agent.tooling.models import BaseTool, ToolHandlerOutcome
        from agent_py_agent.tests._tool_runtime_harness import (
            make_test_model_spec,
            make_test_runtime_policy,
        )

        outer = self

        class Tool(BaseTool):
            model_spec = make_test_model_spec(f"managed_{effect}_tool", description="test-only managed tool")
            runtime_policy = make_test_runtime_policy(effect)

            def execute(self, params):
                outer.executions += 1
                return ToolHandlerOutcome(self.model_spec.name, True, "executed")

        self.executions, self.tool = 0, Tool()

    def run(self, root, *, write_boundary=None):
        from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call

        return execute_canonical_test_call(root, tools={self.tool.model_spec.name: self.tool},
                                           tool_name=self.tool.model_spec.name, arguments={},
                                           write_boundary=write_boundary)


class _RecordingBackend(BaseBackend):
    name = "recording"

    def __init__(self, on_first_call, tool_input=None):
        self.calls, self.on_first_call, self.tool_input = 0, on_first_call, tool_input

    def probe_tool_capability(self):
        from agent_py_agent.agent.backends.base import ProviderToolCapability, _utc_now_iso

        return ProviderToolCapability(provider="recording", endpoint="local://recording", model="", stream=False,
                                      native_supported=True, evidence="test_backend_declares_native_tools",
                                      observed_at=_utc_now_iso())

    # LLM: 第一次调用期间施加权威变化，模拟供应商等待中途的停用/改密/改策略；不发网络。
    # 函数用途: 返回已接纳的模型结果，可选附带一个原生工具调用。
    def generate(self, prompt, on_chunk=None, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            self.on_first_call()
            if self.tool_input is not None:
                return ModelResponse(text="", backend=self.name, tool_use_blocks=[
                    {"id": "call-1", "name": "write_file", "input": self.tool_input}])
        return ModelResponse(text="managed work finished", backend=self.name)


def test_generation_is_object_bound_and_cannot_be_rebuilt(host):
    generation = host.generation
    assert require_managed_owner_generation("unbound") is None and current_managed_owner_generation() is None
    with pytest.raises(ValueError):
        require_managed_owner_generation("")
    copies = [copy.copy(generation), copy.deepcopy(generation), pickle.loads(pickle.dumps(generation)),
              replace(generation), ManagedOwnerGeneration(**{item.name: getattr(generation, item.name) for item in fields(generation)})]
    for forged in copies:
        assert forged is not generation and forged.key == generation.key
        assert _code(lambda: bind_managed_owner_generation(forged).__enter__()) == "MANAGED_OWNER_GENERATION_INVALID"
        assert _code(lambda: require_agent_managed_owner_generation(
            SimpleNamespace(_managed_owner_generation=forged), "forged")) == "MANAGED_OWNER_GENERATION_INVALID"
    serialized = json.loads(json.dumps({item.name: str(getattr(generation, item.name)) for item in fields(generation)}))
    assert _code(lambda: bind_managed_owner_generation(serialized).__enter__()) == "MANAGED_OWNER_GENERATION_INVALID"
    other = prepare_managed_owner_runtime(host.root, ALICE, host_options={})._managed_owner_generation
    with bind_managed_owner_generation(generation) as bound:
        assert bound is generation and require_managed_owner_generation("bound") is generation
        with bind_managed_owner_generation(generation):
            assert current_managed_owner_generation() is generation
        assert _code(lambda: bind_managed_owner_generation(other).__enter__()) == "MANAGED_OWNER_GENERATION_CHANGED"
    assert current_managed_owner_generation() is None


@pytest.mark.parametrize("kind", sorted(CHANGES))
def test_authority_change_denies_each_new_effect_barrier(host, monkeypatch, kind):
    from agent_py_agent.agent.agent_core import tool_model_generation
    from agent_py_agent.agent.agent_core.runner import worker as runner_worker
    from agent_py_agent.agent.agent_core.subagent import model_selection
    from agent_py_agent.agent.assets.descriptor import AssetDescriptor
    from agent_py_agent.agent.assets.script_store import ScriptStore
    from agent_py_agent.agent.backends import gateway_helpers
    from agent_py_agent.agent.backends.provider_headers import provider_runtime_scope
    from agent_py_agent.agent.gateway_parts.request_entry_admission import require_request_execution
    from agent_py_agent.agent.workflows import script_runner
    from agent_py_agent.agent.workflows.script_runner import ScriptRunner

    agent, generation = host.agent, host.generation
    params = SimpleNamespace(task_attributes={}, thread_id="managed-barrier")
    ScriptStore(host.home.home_dir).save_script(AssetDescriptor("hello", "script", "Hello"),
                                                "import json\nprint(json.dumps({'ran': True}))\n")
    launched = []
    original_execute = script_runner._execute
    monkeypatch.setattr(script_runner, "_execute", lambda *args: launched.append(1) or original_execute(*args))
    runner = ScriptRunner(host.home.home_dir, use_sandbox=False, strict_sandbox=False, restrict_to_roots=True)
    tool = _Tool("read_only")
    with bind_managed_owner_generation(generation):
        child = runner_worker.RunSubagentWorkerParams(config=AgentConfig(), root=host.tmp, run_id="run-managed",
                                                      instruction="child work", dry_run=False, max_cards=1,
                                                      probe=False, retry_reason="")
        with provider_runtime_scope(agent, params):
            assert require_request_execution(agent=agent) is None
        assert runner.run_script("hello").ok and launched == [1]
        assert tool.run(host.tmp).result.handler_executed is True and tool.executions == 1
    assert child.managed_owner_generation is generation
    assert runner_worker.RunSubagentWorkerParams(config=AgentConfig(), root=host.tmp, run_id="r", instruction="x",
                                                 dry_run=False, max_cards=1, probe=False,
                                                 retry_reason="").managed_owner_generation is None

    expected = _change(host, kind)
    side_effects = []
    monkeypatch.setattr(model_selection, "mark_subagent_business_request_submitted", lambda *a, **k: side_effects.append("model"))
    monkeypatch.setattr(gateway_helpers, "_bounded_connect_timeout", lambda *a, **k: side_effects.append("connect"))
    monkeypatch.setattr(runner_worker, "_build_worker_agent", lambda *a, **k: side_effects.append("child"))

    @contextmanager
    def provider_scope():
        with provider_runtime_scope(agent, params):
            yield

    codes = {
        "provider_runtime": _code(lambda: provider_scope().__enter__()),
        "model_generation": _code(lambda: tool_model_generation._mark_call_before_send(
            None, "prompt", SimpleNamespace(agent=agent, params=None, call_id="call-1"))),
        "request_admission": _code(lambda: require_request_execution(agent=agent)),
    }
    with bind_managed_owner_generation(generation):
        codes["gateway_provider_call"] = _code(lambda: gateway_helpers._gateway_urlopen_unlimited(object(), object()))
        script = runner.run_script("hello")
        codes["native_script_launch"] = script.error_code
        execution = tool.run(host.tmp)
        codes["tool_invocation"] = execution.result.reported_error_code
        assert execution.result.error_code == "TOOL_EXECUTION_DENIED"
        assert execution.result.handler_executed is False and tool.executions == 1
    refusal = runner_worker._run_subagent_worker(child)
    codes["child_worker"] = refusal.runner_last_error
    assert (refusal.ok, refusal.status) == (False, "PENDING")
    assert codes == dict.fromkeys(codes, expected)
    assert side_effects == [] and launched == [1] and not script.ok
    assert script.effect_outcome == "not_started"


@pytest.mark.parametrize("kind", ["disable", "policy"])
def test_authority_change_during_waits_blocks_the_next_effect(host, monkeypatch, kind):
    from agent_py_agent.agent.conversation import auxiliary_model_call
    from agent_py_agent.agent.llm_scale import hot_path

    expected = CHANGES[kind]

    @contextmanager
    def slot_wait(*_args, **_kwargs):
        _change(host, kind)
        yield

    monkeypatch.setattr(hot_path, "global_llm_admission_slot", slot_wait)
    generated = []
    request = SimpleNamespace(agent=host.agent, purpose="managed-test", task_attributes={}, thread_id="aux")
    call = SimpleNamespace(ledger=None, call_id="aux-1", timeouts=None)
    assert _code(lambda: auxiliary_model_call._invoke_auxiliary_generate(
        request, lambda *a, **k: generated.append(1), call)) == expected
    assert generated == []


def test_authority_change_during_approval_wait_denies_before_handler(host):
    tool = _Tool("dangerous")
    with bind_managed_owner_generation(host.generation):
        first = tool.run(host.tmp)
        assert first.result.status == "approval_required" and first.decision.approval_request
        expected = _change(host, "security")
        binding = {**dict(first.decision.approval_request), "approval_id": "approval-managed", "status": "APPROVED"}
        second = tool.run(host.tmp, write_boundary={"approved_actions": [binding]})
    assert second.decision.evidence["approval_applied"] is True
    assert (second.result.error_code, second.result.reported_error_code) == ("TOOL_EXECUTION_DENIED", expected)
    assert second.result.handler_executed is False
    assert "approved" not in second.states and tool.executions == 0


def _scheduler(agent):
    from agent_py_agent.agent.conversation.runtime import (
        BackgroundMainAgentRuntime,
        BackgroundMainAgentScheduler,
    )

    runtime = BackgroundMainAgentRuntime(agent=agent, store=agent.conversation_store, channels=agent.delivery_service)
    return BackgroundMainAgentScheduler({"runtime": runtime, "store": agent.conversation_store})


@pytest.mark.parametrize("kind", sorted(CHANGES))
def test_change_during_model_wait_blocks_tool_and_next_model_call(host, kind):
    target = host.home.workspace_dir / "managed-output.txt"
    agent = host.agent
    backend = agent.backend = _RecordingBackend(lambda: _change(host, kind),
                                                tool_input={"path": str(target), "content": "should not exist"})
    dispatched = ManagedUserGovernanceService(host.root).dispatch_task("alice", "Write the report", title="Report")
    with pytest.raises(ManagedOwnerGenerationError) as caught:
        _scheduler(agent).tick_thread(dispatched["thread_id"])
    assert caught.value.code == CHANGES[kind]
    assert backend.calls == 1 and not target.exists()
    store = agent.conversation_store
    assert store.tasks.load(dispatched["task_id"]).status == "active"
    assert [wake.root_task_id for wake in store.wakes.pending()] == [dispatched["task_id"]]


def test_admitted_result_and_selected_model_survive_later_change(host):
    from agent_py_agent.agent.gateway_parts.managed_owner_runtime import ManagedOwnerPool
    from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation

    agent = host.agent
    dispatched = ManagedUserGovernanceService(host.root).dispatch_task("alice", "Finish quietly", title="Quiet")
    profile_id = str(uuid4())
    execute_model_profile_operation(agent, "add", {"profile_id": profile_id, "profile": {
        "model_backend": "anthropic_compatible", "model_name": "thread-model", "api_base": "https://example.test/anthropic",
        "api_key": "thread-secret", "model_context_window_tokens": 96000}})
    execute_model_profile_operation(agent, "select", {"profile_id": profile_id}, thread_id=dispatched["thread_id"])
    assert require_agent_managed_owner_generation(agent, "after_thread_selection") is host.generation
    assert agent.conversation_store.threads.require(dispatched["thread_id"]).model_profile_id == profile_id

    other = ManagedUserGovernanceService(host.root).dispatch_task("alice", "Commit this result", title="Commit")
    backend = agent.backend = _RecordingBackend(lambda: _change(host, "policy"))
    reports = _scheduler(agent).tick_thread(other["thread_id"])
    store = agent.conversation_store
    assert backend.calls == 1 and [report.task_status for report in reports] == ["completed"]
    assert store.tasks.load(other["task_id"]).status == "completed"
    assert [m.content for m in store.messages.recent(other["thread_id"]) if m.role == "assistant" and m.content] == [
        "managed work finished"]
    pool = ManagedOwnerPool(host.root, host_options={}, max_agents=2)
    with pool.acquire_pinned(ALICE) as fresh:
        assert fresh._managed_owner_generation is not host.generation
    assert pool.release_idle(0) == [fresh] and pool.gauges() == {"cached": 0, "building": 0, "pinned": 0}
