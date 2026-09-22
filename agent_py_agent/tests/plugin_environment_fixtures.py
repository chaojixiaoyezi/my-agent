# LLM: 仅组件测试用临时 RuntimeDB 领取原操作，不启动模型或伪造产品 TUI 验收；进程仍走产品实现。
# 模块用途: 给环境准备测试提供真实运行、原 claim 和持久资源声明。

import time
from types import SimpleNamespace

from agent_py_agent.agent.local_storage.tool_operations import (
    ToolOperationClaimRequest,
    new_tool_operation_holder,
)
from agent_py_agent.agent.plugin_environment import prepare_plugin_environment
from agent_py_agent.agent.plugin_environment_plan import plan_plugin_environment
from agent_py_agent.agent.plugin_environment_process import PluginEnvironmentOperation
from agent_py_agent.agent.runtime_db.host_commands import HostCommandRequest
from agent_py_agent.agent.runtime_db.managed_operation_store import ManagedOperationStore
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.tooling.runtime_contracts import tool_arguments_hash


# LLM: 测试输入和 owner 都归 tmp_path；复读同请求保持同一个原 attempt，不新建代次。
# 函数用途: 为低层故障测试准备原资源权威，不创建安装表或环境候选。
def environment_operation(owner, package, request_id="prepare"):
    repo = RuntimeRepository(runtime_db_path(owner.home_dir))
    args_hash = tool_arguments_hash({})
    request = HostCommandRequest(owner.owner_id, "test-actor", "test", "test-thread", request_id,
                                 "plugin_prepare_component", args_hash.removeprefix("sha256:"))
    binding = repo.register_host_command(request)
    installation = SimpleNamespace(manifest=package.manifest, package_sha256=package.sha256,
                                    revision=1, settings_revision=0)
    plan = plan_plugin_environment(installation, request.operation_id)
    if binding.created:
        repo.create_attempt(binding.agent_run_id, reuse_pending=True, reject_running=True,
                            expected_pending_attempt_id=binding.attempt_id)
        ManagedOperationStore(repo).claim_tool_operation(ToolOperationClaimRequest(
            owner_id=owner.owner_id, run_id=binding.run_id, task_id=binding.task_id,
            operation_id=request.operation_id, tool=request.command_name, args_hash=args_hash,
            idempotency_key=request.operation_id, idempotency_scope="operation", idempotency_namespace="component",
            holder=new_tool_operation_holder(), lease_expires_at=time.time() + 300,
            resource_scopes=(plan.resource_scope,), attempt_id=binding.attempt_id,
        ))
    return PluginEnvironmentOperation.bind(owner, repo, binding, plan)


# LLM: 组件仅替换调用方组装，不替换环境和进程实现；重复调用会碰到同一候选而失败，不覆盖旧结果。
# 函数用途: 将已有环境测试接到真实的原操作资源绑定。
def prepare_environment(owner, package, request_id, **kwargs):
    return prepare_plugin_environment(owner, package, environment_operation(owner, package, request_id), **kwargs)
