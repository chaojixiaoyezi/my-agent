# LLM: 显式业务命令只组合原 HostCommand、MCP 和 ToolExecutor；固定安装/目录/参数，不能重选版本或绕过审批。
# 模块用途: 将一个已解析的插件动作接到普通工具执行链，结束后只关闭本次建立的连接。

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


# LLM: select 只在原 pending 真正获得执行权后调用；重放不读新目录、不启动服务，取消只关闭本调用的 MCP。
# 函数用途: 等待原审批并调用固定工具，业务结果和连接清理结果分开返回。
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

        try:
            result = execute_host_command(repo, request, prepare, request_permission=request_permission)
        finally:
            if client is not None:
                try:
                    cleanup = {"confirmed": client.stop().confirmed}
                except Exception:  # noqa: BLE001 原资源账保留清理未知，不能改写业务结果或泄露异常正文
                    cleanup = {"confirmed": False}
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
