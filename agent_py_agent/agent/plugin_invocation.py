# LLM: 显式业务命令组合原 HostCommand/MCP/ToolExecutor；本次连接释放归原执行区间，联测拒绝、重复与收尾，不换绑版本。
# 模块用途: 将插件动作接到普通工具执行链，在原运行收口前关闭本次连接，业务与清理结果分别保留。

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .common.cancellation import CancellationToken, bind_cancellation_token
from .plugin_installation import PluginInstallation
from .plugin_runtime import PluginMCPClient
from .runtime_db.host_command_execution import execute_host_command
from .runtime_db.host_commands import HostCommandBinding, HostCommandRequest
from .runtime_db.repository import RuntimeRepository
from .tooling.executor import ToolExecutorRequest
from .tooling.models import ToolExposure, ToolRuntime, ToolRuntimeSnapshot
from .tooling.runtime_contracts import ToolCall, tool_arguments_hash

if TYPE_CHECKING:
    from .plugin_management import PluginManagementContext


# LLM: 安装取唯一 Store 的同一快照；arguments 只含公共参数绑定器生成的工具值，不包含宿主控制字段。
# 类用途: 保存本次命令选中的固定插件及工具输入。
@dataclass(frozen=True)
class PluginInvocation:
    installation: PluginInstallation
    tool_name: str
    arguments: dict


# LLM: 摘要不保存私有路径或命令正文；目录版本绑定安装/激活/schema，权限变化不能借原请求号扩大执行。
# 函数用途: 把宿主选择单独绑定到原请求，避免将管理字段塞入插件实际参数。
def plugin_invocation_context(context: PluginManagementContext, text: str, revision: str) -> str:
    policy = context.path_policy
    values = {"text": text, "catalog_revision": revision, "workspace": str(context.workspace),
              "permission": {"mode": policy.mode, "owner_root": str(policy.owner_scope_root or ""),
                             "dangerous_roots": [str(path) for path in policy.dangerous_roots],
                             "business_allowed": context.business_allowed,
                             "disabled_tools": sorted(context.disabled_tools), "approval_mode": context.approval_mode}}
    return tool_arguments_hash(values).removeprefix("sha256:")


# LLM: select/释放仅归原 pending 获得者；释放调用返回前不发布运行终态，清理未知独立保留，重放不启动或清理服务。
# 函数用途: 等待原审批并调用固定工具，在同一执行区间释放连接，分别返回业务及清理结果。
def execute_plugin_invocation(
    context: PluginManagementContext, repo: RuntimeRepository, request: HostCommandRequest,
    select: Callable[[], PluginInvocation], *, request_permission: Callable | None = None,
    cancellation_token: CancellationToken | None = None,
) -> dict:
    client = None
    cleanup = None
    token = cancellation_token or CancellationToken()
    with ExitStack() as callbacks:
        # LLM: 原执行身份已经领取；先核对完整选择和参数再启动连接，实际发送仍经过原执行权与激活门。
        # 函数用途: 为唯一执行器构造本请求的固定 MCP 代理和权限快照。
        def prepare(binding: HostCommandBinding) -> ToolExecutorRequest:
            nonlocal client
            selected = select()
            if (selected.tool_name != request.command_name
                    or tool_arguments_hash(selected.arguments) != "sha256:" + request.input_digest):
                raise ValueError("原插件调用选择已变化")
            token.raise_if_cancelled()
            client = PluginMCPClient(context.owner, selected.installation)
            client.require_settled_previous_resources()
            callbacks.enter_context(token.register_callback(client.stop))
            with bind_cancellation_token(token):
                transport = client.start()
                tools = client.discover_tools(transport)
                client.publish_discovered_tools(transport, tools)
            tool = next(tool for tool in tools if tool.model_spec.name == request.command_name)
            return _prepare_invocation(context, binding, selected.arguments, tool, token)

        # LLM: 原宿主执行作用域调用一次；清理未知留在原资源账，不篡改已持久化的业务结果。
        # 函数用途: 在 executor 退出前关闭本次连接，并保存独立清理回执。
        def release_execution():
            nonlocal cleanup
            if client is not None:
                try:
                    cleanup = {"confirmed": client.stop().confirmed}
                except Exception:  # noqa: BLE001 原资源账保留清理未知，不能改写业务结果或泄露异常正文
                    cleanup = {"confirmed": False}
        result = execute_host_command(repo, request, prepare, request_permission=request_permission,
                                      release_execution=release_execution)
    return {**result, "connection_cleanup": cleanup} if cleanup is not None else result


# LLM: 工具只降低模型展示要求，危险效果/原 schema/资源声明不变；审批模式仅取可信 owner 配置。
# 函数用途: 将已验证的同连接工具、实际参数、运行身份和取消信号接到原执行器。
def _prepare_invocation(context: PluginManagementContext, binding: HostCommandBinding,
                        arguments: dict, tool, token: CancellationToken) -> ToolExecutorRequest:
    request = binding.request
    runtime = ToolRuntime(tool.model_spec, tool.runtime_policy, tool, exposure=ToolExposure(model_visible=False),
                          availability=tool.availability())
    names = frozenset({request.command_name})
    snapshot = ToolRuntimeSnapshot(binding.run_id, (runtime,), names, (), names)
    call = ToolCall(request.request_id, request.command_name, arguments, "native", tool.model_spec.schema_hash,
                    binding.run_id, request.request_id, binding.attempt_id, operation_id=request.operation_id)
    policy = context.path_policy
    return ToolExecutorRequest(
        call, snapshot, context.workspace, path_access_mode=policy.mode,
        path_dangerous_roots=tuple(str(path) for path in policy.dangerous_roots),
        owner_scope_root=str(policy.owner_scope_root or ""), operation_owner_id=context.owner.owner_id,
        approval_mode=context.approval_mode, cancellation_token=token,
        write_boundary={"canonical_owner_home_root": str(context.owner.home_dir)},
    )
