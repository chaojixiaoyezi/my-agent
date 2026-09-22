"""真实临时 RuntimeDB/托管进程的开发集成；不替代产品 TUI 或模型验收。"""

import json
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.plugin_environment import prepare_plugin_environment
from agent_py_agent.agent.plugin_environment_plan import plan_plugin_environment
from agent_py_agent.agent.plugin_environment_process import (
    EnvironmentPreparationError,
    PluginEnvironmentOperation,
)
from agent_py_agent.agent.runtime_db.host_command_execution import (
    execute_host_command,
    query_host_command,
)
from agent_py_agent.agent.runtime_db.host_commands import HostCommandRequest
from agent_py_agent.agent.runtime_db.operations import RuntimeConflictError
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.tooling.executor import ToolExecutorRequest
from agent_py_agent.agent.tooling.models import (
    ApprovalPolicy,
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ResourceScopePolicy,
    ToolExposure,
    ToolHandlerOutcome,
    ToolModelSpec,
    ToolRuntime,
    ToolRuntimePolicy,
    ToolRuntimeSnapshot,
)
from agent_py_agent.agent.tooling.process_session_store import (
    ProcessSessionStore,
    process_session_store_root,
)
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall, tool_arguments_hash
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
from agent_py_agent.tests.plugin_environment_fixtures import environment_operation
from agent_py_agent.tests.plugin_wheel_fixtures import make_wheel, package_wheels


@pytest.mark.parametrize("fault", [
    "scope", "missing_lock", "lock_holder", "lock_generation", "lock_epoch", "lease",
    "op_holder", "holder_birth", "lock_birth", "op_generation", "op_epoch", "op_args", "op_unknown", "op_settled", "attempt_closed", "owner",
])
def test_preparation_rejects_changed_original_claim_before_any_candidate(tmp_path, fault):
    owner, package = resolve_owner_home(tmp_path), package_wheels(make_wheel())
    operation = environment_operation(owner, package)
    repo = RuntimeRepository(runtime_db_path(owner.home_dir))
    with repo._runtime_connection() as conn:
        raw = conn.execute("SELECT outcome_json FROM tool_operations").fetchone()[0]
        payload = json.loads(raw)
        if fault == "scope":
            payload["resource_scopes"] = []
        elif fault == "op_holder":
            payload["holder"]["holder_id"] = "other-holder"
        elif fault == "holder_birth":
            payload["holder"]["process_start_token"] = "other-instance"
        elif fault == "op_epoch":
            payload["workspace_epoch"] += 1
        elif fault == "op_args":
            payload["args_hash"] = "sha256:" + "0" * 64
        conn.execute("UPDATE tool_operations SET outcome_json = ?", (json.dumps(payload),))
        mutations = {
            "missing_lock": "DELETE FROM resource_locks",
            "lock_holder": "UPDATE resource_locks SET holder_instance = 'other-holder'",
            "lock_birth": "UPDATE resource_locks SET start_token = 'other-instance'",
            "lock_generation": "UPDATE resource_locks SET tool_operation_generation = 999",
            "lock_epoch": "UPDATE resource_locks SET workspace_epoch = 999",
            "lease": "UPDATE resource_locks SET lease_expires_at = 0",
            "op_generation": "UPDATE tool_operations SET tool_operation_generation = 999",
            "op_unknown": "UPDATE tool_operations SET status = 'UNKNOWN'",
            "op_settled": "UPDATE tool_operations SET settled_at = 1",
            "attempt_closed": "UPDATE agent_attempts SET status = 'cancelled'",
            "owner": "UPDATE tasks SET owner_id = 'another-owner'",
        }
        if fault in mutations:
            conn.execute(mutations[fault])
        conn.commit()
    with pytest.raises((RuntimeConflictError, RuntimeError)):
        prepare_plugin_environment(owner, package, operation)
    assert not owner.plugins_dir.exists()


def test_plan_is_durable_before_prepare_and_no_path_or_settings_value_is_exposed(tmp_path, monkeypatch):
    owner, package = resolve_owner_home(tmp_path), package_wheels(make_wheel())
    operation = environment_operation(owner, package)
    repo = RuntimeRepository(runtime_db_path(owner.home_dir))
    with repo._runtime_connection() as conn:
        payload = json.loads(conn.execute("SELECT outcome_json FROM tool_operations").fetchone()[0])
    assert payload["resource_scopes"] == [operation.plan.resource_scope]
    frozen = json.loads(payload["resource_scopes"][0].removeprefix("logical:plugin_environment:"))
    assert frozen["environment_ref"] == operation.plan.environment_ref
    assert frozen["package_sha256"] == package.sha256 and frozen["installation_revision"] == 1
    assert str(tmp_path) not in operation.plan.resource_scope
    monkeypatch.setattr("agent_py_agent.agent.plugin_environment.interpreter_fingerprint", lambda *_: "f" * 64)
    with pytest.raises(EnvironmentPreparationError) as error:
        prepare_plugin_environment(owner, package, operation)
    assert error.value.reason == "environment_interpreter_changed"
    assert not owner.plugins_dir.exists()


def test_old_frozen_claim_does_not_adopt_new_generation(tmp_path):
    operation = environment_operation(resolve_owner_home(tmp_path), package_wheels(make_wheel()))
    altered = replace(operation, claim=replace(operation.claim, generation=operation.claim.generation + 1))
    with pytest.raises(RuntimeConflictError):
        altered.authorize()
    operation.authorize()


# LLM: 这个假业务工具只用于原宿主链集成，真实环境/进程实现不替换；没有产品命令或模型入口。
# 类用途: 验证资源声明先持久化，随后才能运行环境准备，重复请求不得重进 handler。
class PreparationTool(BaseTool):
    # LLM: 工具计划由测试宿主 prepare 冻结，不由参数提供；内部工具仍必须通过原执行器。
    # 函数用途: 组装本片准备器所需的原 operation 与资源声明。
    def __init__(self, owner, repo, binding, package, plan, calls):
        self.owner, self.repo, self.binding, self.package, self.plan = owner, repo, binding, package, plan
        self.calls = calls
        self.model_spec = ToolModelSpec(binding.request.command_name, "环境准备组件", {
            "type": "object", "properties": {}, "additionalProperties": False,
        })
        self.runtime_policy = ToolRuntimePolicy(
            effect_resolver=EffectResolverPolicy("mutating"), approval_policy=ApprovalPolicy("never"),
            idempotency_policy=IdempotencyPolicy("operation"), mutates_workspace=False,
            resource_scopes=ResourceScopePolicy("declared", static_scopes=(plan.resource_scope,)),
        )

    # LLM: bind 核对原执行器确已领取声明；成功回执只表示环境完成，不发布插件贡献。
    # 函数用途: 在原 handler 内执行真实准备，给宿主查询留下原结果。
    def execute(self, params):
        self.calls.append(self.plan.environment_ref)
        operation = PluginEnvironmentOperation.bind(self.owner, self.repo, self.binding, self.plan)
        result = prepare_plugin_environment(self.owner, self.package, operation)
        return ToolHandlerOutcome(self.model_spec.name, True, "环境组件完成", result_envelope={"environment": asdict(result)})


def test_host_executor_claim_precedes_real_preparation_and_replay_does_not_launch_again(tmp_path):
    owner, package = resolve_owner_home(tmp_path), package_wheels(make_wheel())
    repo, calls = RuntimeRepository(runtime_db_path(owner.home_dir)), []
    request = HostCommandRequest(owner.owner_id, "actor", "chat", "thread", "prepare",
                                 "plugin_prepare_component", tool_arguments_hash({}).removeprefix("sha256:"))
    installation = SimpleNamespace(manifest=package.manifest, package_sha256=package.sha256,
                                    revision=1, settings_revision=0)

    def prepare(binding):
        plan = plan_plugin_environment(installation, binding.request.operation_id)
        tool = PreparationTool(owner, repo, binding, package, plan, calls)
        runtime = ToolRuntime(tool.model_spec, tool.runtime_policy, tool, exposure=ToolExposure(model_visible=False))
        names = frozenset({request.command_name})
        snapshot = ToolRuntimeSnapshot(binding.run_id, (runtime,), names, (), names)
        call = ToolCall(request.request_id, request.command_name, {}, "native", tool.model_spec.schema_hash,
                        binding.run_id, request.request_id, binding.attempt_id, operation_id=request.operation_id)
        return ToolExecutorRequest(call, snapshot, owner.home_dir, operation_owner_id=owner.owner_id)

    result = execute_host_command(repo, request, prepare)
    assert result["state"] == "succeeded", result
    assert len(calls) == 1
    assert query_host_command(repo, request)["result"] == result["result"]
    assert execute_host_command(repo, request, lambda _: pytest.fail("原成功请求不可再准备"))["state"] == "succeeded"
    records, errors = ProcessSessionStore(process_session_store_root(owner.home_dir, owner.home_dir)).list_records()
    assert not errors and len(records) == 3
    binding = repo.find_host_command(request)
    assert all(row["status"] == "exited" and row["exit_code"] == 0 for row in records)
    assert all(row["execution_scope"]["attempt_id"] == binding.attempt_id for row in records)
    assert all(row["completion_target"] == {} for row in records)
    with repo._runtime_connection() as conn:
        assert conn.execute("SELECT count(*) FROM tool_operations").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM resource_locks").fetchone()[0] == 0
    assert not (owner.plugins_dir / "installations.json").exists()
