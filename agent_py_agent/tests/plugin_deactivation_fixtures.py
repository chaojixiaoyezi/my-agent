# LLM: 只替换启用调用方，真实 RuntimeDB、ToolExecutor、托管进程和 MCP 不替换；不证明包环境或完整启用目录匹配。
# 模块用途: 为停用组件测试创建有真实原管理身份的两类资源，所有测试资源在 finally 精确清理。

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_activation_record import PluginActivation
from agent_py_agent.agent.plugin_activation_ref import PluginActivationRef
from agent_py_agent.agent.plugin_deactivation import PLUGIN_ENABLE_TOOL
from agent_py_agent.agent.plugin_environment_plan import plan_plugin_environment
from agent_py_agent.agent.plugin_environment_process import PluginEnvironmentOperation
from agent_py_agent.agent.runtime_db.host_command_execution import execute_host_command
from agent_py_agent.agent.runtime_db.host_commands import HostCommandRequest
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.tooling.background_process_launch import start_background_process
from agent_py_agent.agent.tooling.executor import ToolExecutorRequest
from agent_py_agent.agent.tooling.mcp_client import MCPStdioClient
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
from agent_py_agent.agent.tooling.process_session_cleanup import stop_process_session
from agent_py_agent.agent.tooling.process_session_store import ProcessSessionStore
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall, tool_arguments_hash
from agent_py_agent.tests.test_mcp_client import _ECHO_SERVER, _config
from agent_py_agent.tests.test_plugin_activation import publication


# LLM: 假启用工具仅用于构造撤销的生产运行链；准备计划先经真实 claim，原 handler 才能持久化激活和创建进程。
# 类用途: 同时启动管理任务资源和共享 MCP，为停用边界提供有原始证据的对象。
class ActivationSetupTool(BaseTool):
    # LLM: 原 request/plan 从测试宿主冻结，回调只用于控制测试交错，不添加产品测试接口。
    # 函数用途: 构造本次原操作和固定资源声明。
    def __init__(self, state, binding, plan):
        self.state, self.binding, self.plan = state, binding, plan
        self.model_spec = ToolModelSpec(PLUGIN_ENABLE_TOOL, "激活清理测试组件", {
            "type": "object", "properties": {}, "additionalProperties": False,
        })
        self.runtime_policy = ToolRuntimePolicy(
            effect_resolver=EffectResolverPolicy("mutating"), approval_policy=ApprovalPolicy("never"),
            idempotency_policy=IdempotencyPolicy("operation"), mutates_workspace=False,
            resource_scopes=ResourceScopePolicy("declared", static_scopes=(plan.resource_scope,)),
        )

    # LLM: 此发布只有合同夹具含义，不做实际包安装；进程通过原产品入口，失败时 fixture 保留句柄以清理。
    # 函数用途: 建立测试目标，并可暂停在原 handler 内观察并发停用。
    def execute(self, params):
        state = self.state
        service, owner = state["service"], state["service"].context.owner
        operation = PluginEnvironmentOperation.bind(owner, state["repo"], self.binding, self.plan)
        prepared = service.installations.change_activation(PluginActivationRequest(
            self.plan.operation_id, self.plan.installation_revision, PluginActivation(self.plan, "preparing"),
        ))
        state["binding"], state["operation"] = self.binding, operation
        if state["preparation"]:
            path = state["path"]
            request = operation.launch_request([sys.executable, "-c", "import time; time.sleep(25)"],
                path, {}, time.monotonic() + 30, path / "preparation.log")
            state["prepared_process"] = start_background_process(request)
        entry = prepared.installation
        state["reference"] = PluginActivationRef.from_owner(owner, entry.manifest.plugin_id, entry.activation_id)
        client = MCPStdioClient(_config(state["script"], cwd=str(state["path"])), activation=state["reference"])
        state["client"] = client
        client.start()
        if state["phase"] == "active":
            entry = service.installations.change_activation(publication(prepared)).installation
        state["entry"] = entry
        state["ready"].set()
        if state["running"]:
            assert state["release"].wait(20)
        return ToolHandlerOutcome(PLUGIN_ENABLE_TOOL, True, "组件准备完成")


# LLM: 原执行器只执行一次假业务，真实持久状态和 OS 资源可供各测试读回；结束后只收本次句柄，不扫描用户任务。
# 函数用途: 给测试提供已完成或仍运行的原启用操作，以及精确清理的 finally。
@contextmanager
def activation_component(service, path, *, phase="active", preparation=True, running=False, script=_ECHO_SERVER):
    repo = RuntimeRepository(runtime_db_path(service.context.owner.home_dir))
    state = dict(service=service, path=path, repo=repo, phase=phase, preparation=preparation,
                 running=running, script=script, ready=threading.Event(), release=threading.Event())
    request = HostCommandRequest(service.context.owner.owner_id, "fixture", "test", "fixture-thread", "enable",
                                 PLUGIN_ENABLE_TOOL, tool_arguments_hash({}).removeprefix("sha256:"))

    # LLM: 计划先于 claim 构造，原执行器负责把相同资源声明落盘；不能在 handler 外创建候选进程。
    # 函数用途: 将测试启用工具送入现成宿主命令链。
    def prepare(binding):
        entry = service.installations.snapshot()[0]
        plan = plan_plugin_environment(entry, binding.request.operation_id)
        tool = ActivationSetupTool(state, binding, plan)
        runtime = ToolRuntime(tool.model_spec, tool.runtime_policy, tool, exposure=ToolExposure(model_visible=False))
        names = frozenset({PLUGIN_ENABLE_TOOL})
        snapshot = ToolRuntimeSnapshot(binding.run_id, (runtime,), names, (), names)
        call = ToolCall(request.request_id, PLUGIN_ENABLE_TOOL, {}, "native", tool.model_spec.schema_hash,
                        binding.run_id, request.request_id, binding.attempt_id, operation_id=request.operation_id)
        return ToolExecutorRequest(call, snapshot, path, operation_owner_id=service.context.owner.owner_id)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute_host_command, repo, request, prepare)
        try:
            assert state["ready"].wait(10), future.result(timeout=1)
            if not running:
                assert future.result(timeout=10)["state"] == "succeeded"
            yield state
        finally:
            state["release"].set()
            future.result(timeout=10)
            if "client" in state:
                state["client"].stop()
            hosted = state.get("prepared_process")
            if hosted:
                stop_process_session(ProcessSessionStore(hosted.store_root), hosted.record, host_process=hosted.process)
                hosted.process.wait(timeout=3)
