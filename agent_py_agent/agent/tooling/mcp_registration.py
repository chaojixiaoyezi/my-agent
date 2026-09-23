
# LLM: MCP 代理冻结连接并逐次复查权限；完整业务失败与传输未知分开落原账，联测操作结算、共享视图和激活撤销。
# 模块用途: 将远端工具接到原执行链，保留完整失败回执，阻止旧代理追随重连，未知清理不改报成功。
from __future__ import annotations

"""把 MCP server 发现的工具动态注册进 my-agent 的 ToolRegistry。

职责（对接 ``mcp_client.MCPStdioClient`` 与 ``registry.ToolRegistry``）：
1. 读 config 的 ``mcp_servers`` 段，逐个连接 server（起子进程 + 握手 + tools/list）。
2. 把每个发现的 MCP 工具包成一个 ``BaseTool``，名字加 ``mcp__<server>__<tool>`` 前缀防冲突，
   并把 MCP ``inputSchema`` 规范化为唯一 ``ToolModelSpec.input_schema``。
3. 模型调用该工具时，handler 把 arguments 转发给 server 的 ``tools/call``，结果转成
   ``ToolHandlerOutcome``。

严格约束落地：
- 惰性/可选：``mcp_servers`` 为空（默认）时 ``register_mcp_servers`` 直接返回，不起任何子进程。
- 不破坏现有工具：只新增 ``mcp__*`` 前缀工具；某 server 连不上只记日志跳过，不影响其他工具。
- server 异常不崩主流程：连接异常被捕获转日志；调用异常转结构化 ``ToolHandlerOutcome`` 错误。
- 凭证脱敏：复用 ``mcp_client.sanitize_credentials`` / ``redact_env_for_log``。

effect 边界：MCP 工具属于外部执行边界，未声明 effect 时一律按 ``dangerous`` 进入统一
Tool Gateway。部署者只能通过 ``mcp_servers.<server>.tool_effects`` 逐工具显式声明
``read_only``/``mutating``/``dangerous``；MCP server 自报 metadata 不具授权效力。这样未知
工具不会再伪装成只读绕过幂等与审批绑定。

目录披露边界：普通 MCP 默认使用 ``catalog_category=mcp``，继续经 ``tool_search`` 渐进披露；
部署者可为需要首轮直接可见的受控服务声明独立分类。分类只影响模型目录，不改变 owner、effect、审批或执行权限。
"""

import json
import logging
import re
from typing import Any

from ..common.log_redaction import redact_sensitive_value
from .input_schema import canonicalize_tool_input_schema
from .mcp_client import (
    MCPError,
    MCPServerConfig,
    MCPStdioClient,
    MCPToolInfo,
    redact_env_for_log,
    sanitize_credentials,
)
from .mcp_transport import MCPTransport
from .models import (
    BaseTool,
    ConcurrencyPolicy,
    EffectResolverPolicy,
    IdempotencyPolicy,
    OutputPolicy,
    ResourceScopePolicy,
    ToolAvailability,
    ToolHandlerOutcome,
    ToolInvocationContext,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)
from .process_session_cleanup import ProcessSessionCleanupError

logger = logging.getLogger(__name__)

# 工具名前缀：mcp__<server>__<tool>。双下划线分隔，server/tool 各自做字符清洗，
# 避免与内置工具名冲突，也避免 server 名含特殊字符破坏渲染/解析。
_NAME_PREFIX = "mcp"
_NAME_SEP = "__"

# 工具名只允许 [a-z0-9_]（与内置工具命名风格一致，对齐 native tool_use 名校验）。
_NAME_SANITIZE = re.compile(r"[^a-z0-9_]+")

def sanitize_name_component(text: str) -> str:
    """把 server / tool 名清洗成 ``[a-z0-9_]`` 片段，供拼工具名用。"""
    lowered = str(text or "").strip().lower()
    cleaned = _NAME_SANITIZE.sub("_", lowered).strip("_")
    return cleaned or "x"


def mcp_tool_name(server_name: str, tool_name: str) -> str:
    """拼出带前缀的 my-agent 工具名：``mcp__<server>__<tool>``。"""
    return _NAME_SEP.join(
        (_NAME_PREFIX, sanitize_name_component(server_name), sanitize_name_component(tool_name))
    )


# LLM: 目录参数只是完整 MCP Schema 的展示投影，不能再成为执行或 provider 校验事实源。
# 函数用途: 从 MCP properties 提取字段说明，供普通工具目录显示。
def mcp_schema_parameters(input_schema: dict[str, Any]) -> dict[str, str]:
    parameters: dict[str, str] = {}
    if not isinstance(input_schema, dict):
        return parameters
    properties = input_schema.get("properties")
    if not isinstance(properties, dict):
        return parameters
    for raw_name, prop in properties.items():
        name = str(raw_name)
        if isinstance(prop, dict):
            description = str(prop.get("description") or "")
        else:
            description = ""
        parameters[name] = description
    return parameters


# LLM: Schema 只在固定连接存活时暴露，执行走统一 effect/approval 门；完整 isError 只证明本次失败，不证明外部变更回滚。
# 类用途: 把远端工具映射为受控代理，分别报告业务失败和无法确认结果的通信异常。
class MCPProxyTool(BaseTool):
    """一个把调用转发给某 MCP server ``tools/call`` 的代理工具。

    ``execute`` 把模型传来的 dict 参数（去掉内部 ``tool`` 字段）转发给 server，
    再把结果归一化成 ``ToolHandlerOutcome``。server 自报的工具错（isError=true）
    映射成 ``error_code=TOOL_EXECUTION_FAILED`` 与确定失败事实；客户端层异常按其
    ``MCPError.code`` 映射成 my-agent 错误码。
    """

    # LLM: 可选 transport 是发现时的原实例，不能执行时重取新连接；本次调用权限不能保存在共享 proxy 上。
    # 函数用途: 绑定工具声明、策略与原服务连接，不发请求或启动进程。
    def __init__(
        self,
        client: MCPStdioClient,
        remote_tool: str,
        model_spec: ToolModelSpec,
        runtime_policy: ToolRuntimePolicy,
        *,
        transport: MCPTransport | None = None,
    ):
        self.client = client
        self.remote_tool = remote_tool
        self.model_spec = model_spec
        self.runtime_policy = runtime_policy
        self.transport = transport

    # LLM: 只读原连接，不自动重启或跟随新的 current；目录可见不代替真正发送前的激活和任务权限检查。
    # 函数用途: 隐藏已断开的原代理，防止新连接让旧工具声明重新可用。
    def availability(self) -> ToolAvailability:
        if self.client.is_running():
            try:
                if self.transport is None or self.client.connection() is self.transport:
                    return ToolAvailability.ready()
            except MCPError:
                pass
        return ToolAvailability.unavailable("MCP stdio server 当前未运行")

    # LLM: 无上下文入口保留原 BaseTool 接口；正式执行应由 executor 使用 execute_scoped 注入本次权限。
    # 函数用途: 执行直接调用，复用同一结果处理链，不生成权限或任务身份。
    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        return self._execute(params, None)

    # LLM: 原执行权限按调用传到排队后发送处；保留 canonical 块和托管清理异常事实，不能存入共享实例或改写为清理成功。
    # 函数用途: 沿原 executor 上下文调用固定工具连接，返回内容或结构化失败。
    def execute_scoped(self, params: dict[str, Any], context: ToolInvocationContext) -> ToolHandlerOutcome:
        return self._execute(params, context)

    # LLM: 普通 MCP 不因服务自述能力获得宿主路径；只有受控插件子类可投影已声明支持的逐次上下文。
    # 函数用途: 保持普通外部 MCP 的请求不携带宿主元数据。
    def _request_meta(self, context: ToolInvocationContext | None) -> dict[str, object] | None:
        return None

    # LLM: 两入口共用执行链；完整 CallToolResult 的 isError 按失败结算，不声称未执行；传输与清理异常仍保留未知。
    # 函数用途: 向固定连接发送本次参数和权限，保留可读失败回执，使原操作可查询且不阻塞后续独立调用。
    def _execute(self, params: dict[str, Any], context: ToolInvocationContext | None) -> ToolHandlerOutcome:
        arguments = {
            key: value
            for key, value in (params or {}).items()
            if key != "tool" and not key.startswith("__")
        }
        try:
            options = {}
            if self.transport is not None:
                options["transport"] = self.transport
            if context is not None and context.execution_authority_check is not None:
                options["authority_check"] = context.execution_authority_check
            request_meta = self._request_meta(context)
            if request_meta is not None:
                options["request_meta"] = request_meta
            result = self.client.call_tool(self.remote_tool, arguments, **options)
        except ProcessSessionCleanupError as exc:
            return self._error_result("插件原进程清理尚未确认", "TOOL_EXECUTION_FAILED",
                                      details={"process_cleanup": exc.report})
        except MCPError as exc:
            return self._error_result(str(exc), _ERROR_CODE_MAP.get(exc.code, "TOOL_EXECUTION_FAILED"),
                                      effect_outcome=exc.effect_outcome)
        except Exception as exc:  # 兜底：任何意外异常都转结构化错误，绝不向上崩主循环。
            logger.exception("MCP 工具 %s 调用出现未预期异常", self.model_spec.name)
            return self._error_result(
                sanitize_credentials(f"MCP 工具调用异常：{type(exc).__name__}: {exc}"),
                "TOOL_EXECUTION_FAILED",
            )

        content = sanitize_credentials(str(result.get("content") or ""))
        payload: dict[str, Any] = {"result": content}
        failed = bool(result.get("isError"))
        if failed:
            payload["error"] = content or "MCP 工具返回错误"
        if isinstance(result.get("content_blocks"), list):
            payload["content"] = redact_sensitive_value(result["content_blocks"])
        if result.get("structuredContent") is not None:
            payload["structuredContent"] = redact_sensitive_value(
                result["structuredContent"]
            )
        return ToolHandlerOutcome(
            self.model_spec.name,
            not failed,
            json.dumps(payload, ensure_ascii=False),
            error_code="TOOL_EXECUTION_FAILED" if failed else "",
            effect_outcome="failed" if failed else "",
        )

    # LLM: 发送事实只接受 transport 的结构化结论；未发送拒绝不得记 UNKNOWN，已启动 writer 和清理异常不能改成未发生。
    # 函数用途: 返回 MCP 失败、精确清理信息与原操作账所需的执行边界，不暴露配置或完整进程记录。
    def _error_result(self, message: str, error_code: str, *, details: dict | None = None,
                      effect_outcome: str = "") -> ToolHandlerOutcome:
        return ToolHandlerOutcome(
            self.model_spec.name,
            False,
            json.dumps({"error": message, **(details or {})}, ensure_ascii=False),
            error_code=error_code,
            effect_outcome=effect_outcome,
        )


# MCPError.code → my-agent 错误码。让模型拿到正确的可重试/恢复语义：
# 超时/断连可重试；配置/协议错不应原样空转重试。
_ERROR_CODE_MAP = {
    "MCP_CANCELLED": "CANCELLED",
    "MCP_TIMEOUT": "TOOL_TIMEOUT",
    "MCP_CONNECTION_CLOSED": "TOOL_EXECUTION_FAILED",
    "MCP_PROTOCOL_ERROR": "TOOL_EXECUTION_FAILED",
    "MCP_SERVER_START_FAILED": "TOOL_UNAVAILABLE",
    "MCP_CONFIG_INVALID": "TOOL_UNAVAILABLE",
    "PLUGIN_ACTIVATION_UNAVAILABLE": "TOOL_UNAVAILABLE",
    "MCP_ERROR": "TOOL_EXECUTION_FAILED",
}


# LLM: Schema 先编译，effect 来自宿主声明，transport 固定发现实例；非法声明不能降级透传或改绑新连接。
# 函数用途: 构造进入统一权限和工具执行链的代理，参数与效果继续以原 canonical 声明为准。
def build_proxy_tool(
    client: MCPStdioClient,
    server_name: str,
    info: MCPToolInfo,
    *,
    effect: str = "dangerous",
    catalog_category: str = "mcp",
    transport: MCPTransport | None = None,
) -> MCPProxyTool:
    raw_schema = info.input_schema if isinstance(info.input_schema, dict) else {}
    canonical_schema = canonicalize_tool_input_schema(raw_schema)
    upstream_description = info.description or f"工具 {info.name}"
    description = (
        f"管理员配置的外部 MCP 服务 '{server_name}' 提供的 '{info.name}' 能力。"
        f"{upstream_description}"
    )
    model_spec = ToolModelSpec(
        name=mcp_tool_name(server_name, info.name),
        description=sanitize_credentials(description),
        input_schema=canonical_schema,
        hints=ToolModelHints(
            category=catalog_category,
            use_cases=tuple(_mcp_use_cases(server_name, info.name, effect)),
            avoid_when=("该外部能力与当前任务无关时不要调用",),
            keywords=tuple(_mcp_keywords(server_name, info.name, effect)),
        ),
    )
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy(effect),
        idempotency_policy=IdempotencyPolicy(
            "operation" if effect in {"mutating", "dangerous"} else ""
        ),
        concurrency_policy=ConcurrencyPolicy(
            "parallel_safe" if effect == "read_only" else "serial"
        ),
        resource_scopes=ResourceScopePolicy(
            mode="declared",
            static_scopes=(f"mcp:{server_name}:{info.name}",),
        ),
        output_policy=OutputPolicy(trust="external_data"),
    )
    return MCPProxyTool(client, info.name, model_spec, runtime_policy, transport=transport)


def _mcp_use_cases(server_name: str, tool_name: str, effect: str) -> list[str]:
    cases = [f"调用管理员已接入的外部 MCP 服务 '{server_name}' 提供的 {tool_name} 能力"]
    if effect == "read_only":
        cases.append("通过已接入的外部服务读取或查询信息，不产生修改")
    elif effect == "mutating":
        cases.append("通过已接入的外部服务执行明确授权的修改")
    return cases


def _mcp_keywords(server_name: str, tool_name: str, effect: str) -> list[str]:
    keywords = ["mcp", "外部工具", "外部服务", "已接入", server_name, tool_name]
    if effect == "read_only":
        keywords.extend(["读取", "查询", "只读"])
    elif effect == "mutating":
        keywords.extend(["修改", "写入"])
    return keywords


def parse_mcp_servers(raw: object) -> list[MCPServerConfig]:
    """从 config 的 ``mcp_servers`` 值解析出 server 配置列表。

    接受 ``{name: {command,args,env,...}}`` 映射；非法条目记日志跳过（不影响其他 server）。
    返回空列表表示「没有要连的 server」（惰性零开销路径）。
    """
    if not raw or not isinstance(raw, dict):
        return []
    configs: list[MCPServerConfig] = []
    for name, cfg in raw.items():
        try:
            configs.append(MCPServerConfig.from_mapping(str(name), cfg))
        except MCPError as exc:
            logger.warning("跳过非法 MCP server 配置 '%s'：%s", name, exc)
    return configs


# LLM: 初次发现与恢复共用固定 transport 发布；未返回句柄的启动失败由 client 自行清理，不终结重试资格。
# 函数用途: 启动声明的 MCP 服务并注册工具，保留暂时失败的客户端供后续运行恢复。
def register_mcp_servers(registry: Any, mcp_servers: object) -> list[MCPStdioClient]:
    """连接所有配置的 MCP server，把已发现的工具注册进 ``registry``。

    返回每个合法配置对应的 ``MCPStdioClient``，包括本次启动失败的 client。失败连接没有
    工具可见，但必须保留同一个结构化配置，供后续 run 边界按退避策略重新连接；否则一次
    短暂启动故障会永久删掉该 server，直到整个 Agent 进程重启。调用方负责统一 ``stop()``。
    若 ``mcp_servers`` 为空则直接返回空列表，**不启动任何子进程**（零开销）。
    """
    configs = parse_mcp_servers(mcp_servers)
    if not configs:
        return []

    clients: list[MCPStdioClient] = []
    for config in configs:
        client = MCPStdioClient(config)
        clients.append(client)
        transport = None
        try:
            transport = client.start()
            registered = refresh_registered_mcp_client(registry, client, transport=transport)
        except MCPError as exc:
            logger.warning(
                "MCP server '%s' 连接失败，跳过（command=%s env=%s）：%s",
                config.name, config.command, redact_env_for_log(config.env), exc,
            )
            if transport is not None:
                disconnect_failed_mcp_transport(client, transport)
            continue
        except Exception as exc:  # 兜底：连接路径任何意外异常都不许崩主流程。
            logger.exception("MCP server '%s' 连接出现未预期异常，跳过", config.name)
            if transport is not None:
                disconnect_failed_mcp_transport(client, transport)
            continue

        logger.info("MCP server '%s' 已连接，注册 %d 个工具", config.name, registered)
    return clients


# LLM: 错误收尾只能清理原 transport；异常不能中断其他服务装配，未知仍由原客户端保留，不新建替代连接。
# 函数用途: 隔离单个 MCP 服务的清理失败，保持核心和其他服务可用。
def disconnect_failed_mcp_transport(client: MCPStdioClient, transport: MCPTransport) -> None:
    try:
        receipt = client.disconnect(transport=transport)
        if not receipt.confirmed:
            logger.warning("MCP server '%s' 原连接清理未确认", client.config.name)
    except Exception as exc:
        logger.warning("MCP server '%s' 原连接清理异常：%s", client.config.name, type(exc).__name__)


# LLM: 所有分页来自固定连接；发布在客户端生命周期锁内复核该连接，纯目录替换不能反向获取 prepare 锁。
# 函数用途: 刷新一个 MCP 服务的工具目录；停用或重连已使候选失效时拒绝发布。
def refresh_registered_mcp_client(
    registry: Any,
    client: MCPStdioClient,
    *,
    transport: MCPTransport,
) -> int:
    tools = client.list_tools(transport=transport)
    current = dict(getattr(registry, "tools", {}) or {})
    old_names = {
        name
        for name, tool in current.items()
        if isinstance(tool, MCPProxyTool) and tool.client is client
    }
    replacement = {name: tool for name, tool in current.items() if name not in old_names}
    registered = 0
    for info in tools:
        try:
            proxy = build_proxy_tool(
                client,
                client.config.name,
                info,
                effect=client.config.effect_for_tool(info.name),
                catalog_category=client.config.catalog_category,
                transport=transport,
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "MCP 工具 Schema 无法安全执行，跳过 server '%s' 的 '%s'：%s",
                client.config.name,
                info.name,
                exc,
            )
            continue
        if proxy.model_spec.name in replacement:
            logger.warning(
                "MCP 工具名冲突，跳过 server '%s' 的 '%s'（已存在 %s）",
                client.config.name,
                info.name,
                proxy.model_spec.name,
            )
            continue
        replacement[proxy.model_spec.name] = proxy
        registered += 1
    # LLM: 回调只提交已构建好的内存目录，不调用插件、发请求或获取注册准备锁。
    # 函数用途: 在连接仍有效的最后边界，一次替换目录。
    def publish() -> int:
        registry.tools = replacement
        return registered

    return client.publish_tools(transport, publish)
