"""可信宿主可替换 worker 类型；授权、执行轮和资源收口仍由原 runner 负责。"""
from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError, fields, replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runner import dispatch, gate
from agent_py_agent.agent.agent_core.runner import worker as runner
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.authorization_gate import AuthorizationError
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.manager_runner_result_payload import RecordRunnerResultParams
from agent_py_agent.agent.subagents.models import SubAgentRunnerResult
from agent_py_agent.agent.subagents.runner_start import reserve_runner_start


# LLM: Build only the internal DTO; preserve legacy positional defaults for compatibility tests.
# 函数用途: 给各个测试提供最小的宿主启动参数。
def _params(root, **overrides):
    values = dict(config=AgentConfig(), root=root, run_id="worker-test", instruction="work",
                  dry_run=False, max_cards=2, probe=False, retry_reason="")
    return runner.RunSubagentWorkerParams(**{**values, **overrides})


# LLM: Check the field contract, immutability and the entire old positional signature.
# 函数用途: 验证类型选项不会改变旧调用方的参数解释和对象比较。
def test_worker_type_is_optional_keyword_only_and_not_value_identity(tmp_path):
    params = _params(tmp_path)
    assert "worker_agent_type" in {item.name for item in fields(params)}
    field = next(item for item in fields(params) if item.name == "worker_agent_type")
    parameter = inspect.signature(runner.RunSubagentWorkerParams).parameters[field.name]
    assert field.default is None and field.kw_only and not field.repr and not field.compare
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    injected = replace(params, worker_agent_type=SimpleAgent)
    assert params == injected and repr(params) == repr(injected)
    assert replace(injected, instruction="next").worker_agent_type is SimpleAgent
    assert field.name not in params.__match_args__
    with pytest.raises(FrozenInstanceError):
        params.worker_agent_type = SimpleAgent
    # 旧调用方的全部位置参数仍按原顺序解释，新增字段不能吃掉第十六个位置参数。
    positional = [getattr(params, item.name) for item in fields(params) if not item.kw_only]
    assert runner.RunSubagentWorkerParams(*positional) == params
    with pytest.raises(TypeError):
        runner.RunSubagentWorkerParams(*positional, SimpleAgent)


# LLM: Isolate type selection while keeping the real outer cleanup finally block.
# 函数用途: 验证默认类型及显式类型都保留宿主清理。
@pytest.mark.parametrize("selection", ["omitted", "none", "custom"])
def test_common_entry_selects_type_without_replacing_outer_cleanup(tmp_path, monkeypatch, selection):
    class CustomWorker:
        pass

    options = {} if selection == "omitted" else {"worker_agent_type": None if selection == "none" else CustomWorker}
    params = _params(tmp_path, dry_run=True, **options)
    closed, built = [], []
    worker = SimpleNamespace(tools=SimpleNamespace(close_mcp_clients=lambda: closed.append(True)))
    result = object()

    # LLM: Capture the chosen type without constructing a model or external client.
    # 函数用途: 保存共同入口实际选择的类型和参数。
    def build(agent_type, received):
        built.append((agent_type, received))
        return worker

    monkeypatch.setattr(runner, "_build_worker_agent", build)
    monkeypatch.setattr(runner, "_run_bound_subagent_worker", lambda *_: result)
    assert runner._run_subagent_worker(params) is result
    assert built == [(CustomWorker if selection == "custom" else SimpleAgent, params)]
    assert closed == [True]


# LLM: Use a real local manager and protocol-compatible worker; replace only model selection.
# 函数用途: 创建不调用模型的隔离宿主和替身实现。
@pytest.fixture
def host(tmp_path, monkeypatch):
    manager = SubAgentManager(tmp_path / "runs", workspace_root=tmp_path, owner_id="local/main",
                              execution_mode="local_unmanaged")
    state = SimpleNamespace(config=AgentConfig(), root=tmp_path, subagents=manager, local_store=None,
                            created=[], calls=[], closed=[], failure=None)

    class ProtocolWorker:
        # LLM: Provide the existing builder contract without inheriting SimpleAgent.
        # 函数用途: 记录构造次数并提供宿主需要的资源。
        def __init__(self, config, root, *, defer_mcp_connect=False):
            self.config, self.root, self.subagents = config, root, manager
            self.memory = SimpleNamespace()
            self.tools = SimpleNamespace(close_mcp_clients=lambda: state.closed.append(self))
            self.deferred = defer_mcp_connect
            state.created.append(self)

        # LLM: Inspect real lease/attempt state before recording through the real result gate.
        # 函数用途: 用真实任务管理器完成一次受宿主管理的工作片。
        def run_subagent(self, *, params):
            task = manager.load(params.run_id)
            session = task.attributes["runner_session"]
            assert session["status"] == "running"
            assert session["attempt_id"] == task.runner_active_attempt_id == params.attempt_id
            state.calls.append(params)
            if state.failure is not None:
                raise state.failure
            return manager.runner_result.record_runner_result(RecordRunnerResultParams(
                run_id=params.run_id, attempt_id=params.attempt_id, dry_run=False, ok=True,
                status="DONE", verification_status="VERIFIED", message="custom worker completed"))

    state.worker_type = ProtocolWorker
    # 模型选择不是本 seam 的被测对象；授权、执行轮、租约、结果和清理均保留真实实现。
    monkeypatch.setattr("agent_py_agent.agent.settings.model_profiles.inherited_model_config",
                        lambda agent, _task: agent.config)
    return state


# LLM: Reserve a real exact attempt instead of forging an active task or lease.
# 函数用途: 为注入实现准备真实的预留执行轮。
def _reserved_params(host, **overrides):
    task = host.subagents.create_run(goal="worker type seam")
    attempt = reserve_runner_start(host.subagents, task.id, launch_id="iface-w5")
    return _params(host.root, config=host.config, run_id=task.id, expected_attempt_id=attempt,
                   worker_agent_type=host.worker_type, **overrides)


# LLM: Cover both ordinary and timeout-thread execution without simulating lease closeout.
# 函数用途: 核对注入实现仍走原执行轮、存储、租约和清理。
@pytest.mark.parametrize("timeout", [0.0, 10.0])
def test_protocol_worker_uses_real_attempt_lease_closeout_and_cleanup(host, timeout):
    store, backend = object(), object()
    params = _reserved_params(host, timeout_seconds=timeout, local_store=store, backend_override=backend)
    result = runner._run_subagent_worker(params)
    task = host.subagents.load(params.run_id)
    assert result.ok and result.status == task.status == "DONE"
    assert len(host.created) == 1 and len(host.calls) == 1
    worker = host.created[0]
    assert worker.deferred and worker.backend is backend
    assert worker.local_store is worker.memory.local_store is host.subagents.local_store is store
    assert host.calls[0].attempt_id == params.expected_attempt_id
    assert task.runner_active_attempt_id == ""
    assert task.attributes["runner_session"]["status"] == "completed"
    assert task.attributes["runner_session"]["ended_at"] > 0
    assert host.closed == [worker]


# LLM: A business exception must pass through host failure recording and final cleanup.
# 函数用途: 验证失败工作片也会收好租约和工具。
def test_protocol_worker_business_failure_still_closes_lease_and_tools(host):
    host.failure = OSError("controlled worker failure")
    params = _reserved_params(host)
    result = runner._run_subagent_worker(params)
    task = host.subagents.load(params.run_id)
    assert not result.ok and result.status == task.status == "FAILED"
    assert "controlled worker failure" in result.message
    assert task.attributes["runner_session"]["status"] == "failed"
    assert task.attributes["runner_session"]["ended_at"] > 0
    assert host.closed == host.created and len(host.calls) == 1


# LLM: A stale attempt must not activate or overwrite the original reservation.
# 函数用途: 验证替换实现不能冒用别的执行轮。
def test_injected_worker_cannot_activate_a_different_attempt(host):
    params = _reserved_params(host)
    result = runner._run_subagent_worker(replace(params, expected_attempt_id="wrong-attempt"))
    task = host.subagents.load(params.run_id)
    assert not result.ok and result.status == "CANCELLED"
    assert task.status == "PLANNING" and not task.runner_active_attempt_id
    assert task.attributes["background_start"]["attempt_id"] == params.expected_attempt_id
    assert "runner_session" not in task.attributes and host.calls == []
    assert host.closed == host.created


# LLM: Keep the actual dispatch authorization gate and canonical manager for denial checks.
# 函数用途: 核对跨用户、非法路径和缺失任务都会被拒绝。
@pytest.mark.parametrize("violation", ["owner", "path", "missing"])
def test_injected_protocol_cannot_bypass_dispatch_authorization(host, violation):
    task = host.subagents.create_run(goal="denied", owner="other/owner" if violation == "owner" else "local/main")
    run_id = {"owner": task.id, "path": "../../escape", "missing": "run-missing"}[violation]
    params = _params(host.root, config=host.config, run_id=run_id, worker_agent_type=host.worker_type)
    error = FileNotFoundError if violation == "missing" else AuthorizationError
    with pytest.raises(error):
        runner._run_subagent_worker(params)
    unchanged = host.subagents.load(task.id)
    assert unchanged.status == "PLANNING" and not unchanged.runner_active_attempt_id
    assert "runner_session" not in unchanged.attributes and host.calls == []
    assert len(host.created) == 1 and host.closed == host.created


# LLM: Shutdown admission must refuse before any injected constructor or attempt activation.
# 函数用途: 验证宿主关门后不再创建替换实现。
def test_injected_worker_is_not_constructed_after_host_admission_closes(host, monkeypatch):
    monkeypatch.setattr("agent_py_agent.agent.contracts.model_call_ledger.model_call_admission_closure",
                        lambda: object())
    result = runner._run_subagent_worker(_reserved_params(host))
    assert not result.ok and result.status == "PENDING"
    assert result.runner_last_error == "MODEL_CALL_ADMISSION_CLOSED"
    assert host.created == host.calls == host.closed == []


# LLM: Observe the cancellation hook identity without replacing lease failure or tool cleanup.
# 函数用途: 核对业务执行前按原执行轮检查取消。
def test_injected_worker_cancellation_uses_exact_attempt_before_business(host, monkeypatch):
    params = _reserved_params(host)
    observed = []

    # LLM: Report cancellation for the exact manager/run/attempt received from the host.
    # 函数用途: 保存取消检查收到的完整身份。
    def cancelled(manager, run_id, attempt_id):
        observed.append((manager, run_id, attempt_id))
        return True

    monkeypatch.setattr(runner, "runner_attempt_cancelled", cancelled)
    with pytest.raises(InterruptedError, match="原子代理执行轮已停止"):
        runner._run_subagent_worker(params)
    assert observed == [(host.subagents, params.run_id, params.expected_attempt_id)]
    assert host.calls == [] and host.closed == host.created
    assert host.subagents.load(params.run_id).attributes["runner_session"]["status"] == "failed"


# LLM: Inject at trusted DTO construction, never via model tool arguments or task attributes.
# 函数用途: 验证单个和线程池入口汇入同一类型接缝。
@pytest.mark.parametrize("mode", ["single", "parallel"])
def test_single_and_parallel_entries_share_the_injected_worker_seam(host, monkeypatch, mode):
    params = _reserved_params(host)
    dto_type = runner.RunSubagentWorkerParams
    monkeypatch.setattr(dispatch, "RunSubagentWorkerParams",
                        lambda *args, **kwargs: dto_type(*args, **kwargs, worker_agent_type=host.worker_type))
    if mode == "single":
        result = gate.run_single_runner(gate.SingleRunnerParams(
            host, params.run_id, 0, "instruction", True, 2, False, "", params.expected_attempt_id))
    else:
        result, after = gate.run_concurrent_runners(gate.ConcurrentRunnerParams(
            agent=host, pending_jobs=[(params.run_id, host.subagents.load(params.run_id), "")],
            runner_concurrency=1, runner_timeout_seconds=0, instruction="instruction", start_runners=True,
            max_cards=2, probe=False, expected_attempt_ids={params.run_id: params.expected_attempt_id}))[params.run_id]
        assert after.status == "DONE"
    assert result.ok and result.status == "DONE"
    assert host.calls[0].attempt_id == params.expected_attempt_id
    assert len(host.created) == 1 and host.closed == host.created


# LLM: A native subclass must retain real bootstrap validation before constructor side effects.
# 函数用途: 验证原生子类不能跳过启动身份核对。
def test_native_subclass_still_checks_bootstrap_before_construction(host, monkeypatch):
    from agent_py_agent.agent.gateway_parts import multiprocess_worker_agent
    from agent_py_agent.agent.gateway_parts.multiprocess_bootstrap import WorkerBootstrapError

    class NativeWorker(host.worker_type, SimpleAgent):
        pass

    monkeypatch.setattr(multiprocess_worker_agent, "current_worker_partition", lambda: object())
    params = replace(_reserved_params(host), worker_agent_type=NativeWorker)
    with pytest.raises(WorkerBootstrapError) as error:
        runner._run_subagent_worker(params)
    assert error.value.error_code == "WORKER_BOOTSTRAP_INVALID"
    assert error.value.reason == "worker_bootstrap_required" and error.value.field == "mode"
    assert host.created == host.calls == host.closed == []


# LLM: Constructor failures must propagate once, not be mistaken for a legacy signature.
# 函数用途: 验证构造器内部异常不会触发重复构造。
def test_constructor_internal_typeerror_is_not_retried(host):
    attempts = []

    class BrokenWorker:
        # LLM: Raise inside a supported signature to expose unsafe retry behavior.
        # 函数用途: 制造一次可识别的构造内部失败。
        def __init__(self, config, root, *, defer_mcp_connect=False):
            attempts.append((config, root, defer_mcp_connect))
            raise TypeError("constructor internal failure")

    params = replace(_reserved_params(host), worker_agent_type=BrokenWorker)
    with pytest.raises(TypeError, match="constructor internal failure"):
        runner._run_subagent_worker(params)
    assert attempts == [(params.config, params.root, True)]
    assert host.calls == []


# LLM: Only transport admission and orphan selection are stubbed; the worker host remains real.
# 函数用途: 核对已经过运输准入的孤儿续跑也消费同一个类型接缝。
def test_orphan_resume_uses_common_injected_type_after_transport_admission(host, monkeypatch):
    from agent_py_agent.agent.agent_core.orchestration.dispatch import (
        capability_auto_sweep as sweep,
    )
    from agent_py_agent.agent.agent_core.orchestration.dispatch import (
        conversation_lifecycle_gate as lifecycle,
    )
    from agent_py_agent.agent.agent_core.orchestration.dispatch import (
        orphan_request,
    )

    task = host.subagents.create_run(goal="orphan worker type seam")
    dto_type = runner.RunSubagentWorkerParams
    admitted = []
    host.runtime_guard_policy = None
    monkeypatch.setattr(runner, "RunSubagentWorkerParams",
                        lambda *args, **kwargs: dto_type(*args, **kwargs, worker_agent_type=host.worker_type))
    monkeypatch.setattr(orphan_request, "_require_original_child_ref",
                        lambda transport, agent: admitted.append((transport, agent)))
    monkeypatch.setattr(orphan_request, "background_short_operation", lambda _name, operation: operation())
    monkeypatch.setattr(sweep, "supervise_stalled_orphans", lambda _agent: {})
    monkeypatch.setattr(sweep, "_is_stalled_dispatchable_orphan", lambda *_args: True)
    monkeypatch.setattr(lifecycle, "conversation_lifecycle_decisions", lambda *_args: {})
    result = orphan_request.execute_orphan_source(host, {"source_id": task.id})
    assert admitted == [(None, host)]
    assert result["ok"] and result["status"] == "DONE" and result["run_id"] == task.id
    # 孤儿入口不传排队预留ID；仍由宿主原子创建新执行轮，而非复用先前失效的启动记录。
    session = host.subagents.load(task.id).attributes["runner_session"]
    assert host.calls[0].attempt_id and session["attempt_id"] == host.calls[0].attempt_id
    assert session["status"] == "completed" and session["ended_at"] > 0
    assert len(host.created) == 1 and host.closed == host.created


# LLM: Simulate an authority revocation at the real pre-construction generation hook.
# 函数用途: 核对受管授权失效时不会创建替换实现或激活执行轮。
def test_injected_type_cannot_bypass_managed_generation_refusal(host, monkeypatch):
    from agent_py_agent.agent.common import managed_owner_generation as generation

    params = _reserved_params(host)

    # LLM: Represent a typed authority refusal, not a fabricated successful generation.
    # 函数用途: 在原授权检查位置返回受管账号拒绝。
    def refuse(phase):
        raise generation.ManagedOwnerGenerationError("OWNER_REVOKED", 403, phase)

    monkeypatch.setattr(generation, "require_managed_owner_generation", refuse)
    result = runner._run_subagent_worker(params)
    assert not result.ok and result.status == "PENDING" and result.runner_last_error == "OWNER_REVOKED"
    assert host.created == host.calls == host.closed == []
    task = host.subagents.load(params.run_id)
    assert task.attributes["background_start"]["attempt_id"] == params.expected_attempt_id
    assert not task.runner_active_attempt_id and "runner_session" not in task.attributes


# LLM: Inspect actual model schemas for both media modes; do not expose the trusted Python seam.
# 函数用途: 验证模型派工参数仍拒绝实现类型或工厂字段。
@pytest.mark.parametrize("input_media", [False, True])
def test_worker_type_is_not_exposed_in_model_dispatch_schema(input_media):
    from agent_py_agent.agent.agent_core.orchestration.tool_specs import (
        build_create_subagents_model_spec,
    )

    schema = build_create_subagents_model_spec(input_media=input_media).input_schema
    item = schema["properties"]["items"]["items"]
    assert schema["required"] == ["items"] and "goal" in item["required"]
    assert schema["additionalProperties"] is item["additionalProperties"] is False
    private = {"worker_agent_type", "provider", "factory", "worker_factory", "agent_factory"}
    assert not private.intersection(schema["properties"])
    assert not private.intersection(item["properties"])


# LLM: A protocol instance exists before overlay resolution; rejected assembly must close it.
# 函数用途: 核对构造后配置解析失败不会漏掉MCP清理。
def test_injected_context_is_closed_if_effective_config_resolution_fails(host, monkeypatch):
    params = _reserved_params(host)

    # LLM: Fail only the overlay dependency, leaving builder ownership and cleanup real.
    # 函数用途: 在真实构造成功后制造配置解析异常。
    def fail_overlay(*_args, **_kwargs):
        raise ValueError("controlled overlay failure")

    monkeypatch.setattr(runner, "apply_task_runtime_config_overlay", fail_overlay)
    with pytest.raises(ValueError, match="controlled overlay failure"):
        runner._run_subagent_worker(params)
    assert len(host.created) == 1 and host.closed == host.created and host.calls == []


# LLM: On rebuild, close the obsolete context once and the failed final worker once.
# 函数用途: 核对最终配置重建失败时两份资源都恰好清理一次。
def test_injected_rebuild_failure_closes_each_constructed_worker_once(host, monkeypatch):
    params = _reserved_params(host)
    attach = runner._attach_worker_runtime
    monkeypatch.setattr("agent_py_agent.agent.settings.model_profiles.inherited_model_config",
                        lambda agent, _task: replace(agent.config))

    # LLM: Preserve the first attachment and fail after the replacement is actually constructed.
    # 函数用途: 在第二个worker安装运行资源时制造异常。
    def fail_final_attach(worker, received):
        if len(host.created) == 2:
            raise ValueError("controlled attachment failure")
        attach(worker, received)

    monkeypatch.setattr(runner, "_attach_worker_runtime", fail_final_attach)
    with pytest.raises(ValueError, match="controlled attachment failure"):
        runner._run_subagent_worker(params)
    assert len(host.created) == 2 and host.closed == host.created and host.calls == []


# LLM: Use a genuinely issued in-process generation with a controlled unchanged authority reader.
# 函数用途: 核对构造成功后的代次属性安装失败也会清理一次。
def test_injected_worker_generation_attachment_failure_is_closed(host, monkeypatch):
    from agent_py_agent.agent.common import managed_owner_generation as generation

    monkeypatch.setattr(generation, "_current_authority", lambda *_args: (1, "test-security", 1))
    issued = generation.issue_managed_owner_generation(host.root, "synthetic", config_digest="test-config")
    assert generation.is_issued_managed_owner_generation(issued)

    class RejectGenerationWorker(host.worker_type):
        # LLM: Reject only this host attachment, after normal construction has returned resources.
        # 函数用途: 模拟注入实现不接受代次属性的情况。
        def __setattr__(self, name, value):
            if name == "_managed_owner_generation":
                raise ValueError("controlled generation attachment failure")
            super().__setattr__(name, value)

    params = replace(_reserved_params(host), worker_agent_type=RejectGenerationWorker,
                     managed_owner_generation=issued)
    with pytest.raises(ValueError, match="controlled generation attachment failure"):
        runner._run_subagent_worker(params)
    assert len(host.created) == 1 and host.closed == host.created and host.calls == []
    task = host.subagents.load(params.run_id)
    assert not task.runner_active_attempt_id and "runner_session" not in task.attributes
