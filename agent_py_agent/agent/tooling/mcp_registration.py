
from __future__ import annotations

"""把 MCP server 发现的工具动态注册进 my-agent 的 ToolRegistry。

职责（对接 ``mcp_client.MCPStdioClient`` 与 ``registry.ToolRegistry``）：
1. 读 config 的 ``mcp_servers`` 段，逐个连接 server（起子进程 + 握手 + tools/list）。
2. 把每个发现的 MCP 工具包成一个 ``BaseTool``，名字加 ``mcp__<server>__<tool>`` 前缀防冲突，
   并把 MCP ``inputSchema``（JSON Schema）转成 my-agent 的 ``parameters`` + ``parameter_schema``
   （对齐原生 tool_use 的精确类型）。
3. 模型调用该工具时，handler 把 arguments 转发给 server 的 ``tools/call``，结果转成
   ``ToolExecutionResult``。

严格约束落地：
- 惰性/可选：``mcp_servers`` 为空（默认）时 ``register_mcp_servers`` 直接返回，不起任何子进程。
- 不破坏现有工具：只新增 ``mcp__*`` 前缀工具；某 server 连不上只记日志跳过，不影响其他工具。
- server 异常不崩主流程：连接异常被捕获转日志；调用异常转结构化 ``ToolExecutionResult`` 错误。
- 凭证脱敏：复用 ``mcp_client.sanitize_credentials`` / ``redact_env_for_log``。

effect 边界：MCP 工具属于外部执行边界，未声明 effect 时一律按 ``dangerous`` 进入统一
Tool Gateway。部署者只能通过 ``mcp_servers.<server>.tool_effects`` 逐工具显式声明
``read_only``/``mutating``/``dangerous``；MCP server 自报 metadata 不具授权效力。这样未知
工具不会再伪装成只读绕过幂等与审批绑定。
"""

import json
import logging
import re
from typing import Any

from .mcp_client import (
    MCPError,
    MCPServerConfig,
    MCPStdioClient,
    MCPToolInfo,
    redact_env_for_log,
    sanitize_credentials,
)
from .models import BaseTool, ToolAvailability, ToolExecutionResult, ToolSpec

logger = logging.getLogger(__name__)

# 工具名前缀：mcp__<server>__<tool>。双下划线分隔，server/tool 各自做字符清洗，
# 避免与内置工具名冲突，也避免 server 名含特殊字符破坏渲染/解析。
_NAME_PREFIX = "mcp"
_NAME_SEP = "__"

# 工具名只允许 [a-z0-9_]（与内置工具命名风格一致，对齐 native tool_use 名校验）。
_NAME_SANITIZE = re.compile(r"[^a-z0-9_]+")

# MCP inputSchema 里我们透传给 parameter_schema 的精确类型键（其余键忽略，保持保守）。
_SCHEMA_PASSTHROUGH_KEYS = ("type", "enum", "items", "properties", "format")


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


def input_schema_to_parameters(
    input_schema: dict[str, Any],
) -> tuple[dict[str, str], dict[str, Any], list[str]]:
    """把 MCP ``inputSchema``（JSON Schema）转成 my-agent 的三件套。

    返回 ``(parameters, parameter_schema, required)``：
    - ``parameters``：``{参数名: 中文/英文描述}``，渲染工具目录用。
    - ``parameter_schema``：``{参数名: {精确 JSON Schema 片段}}``，对齐 native tool_use 精确类型，
      消除全 string 弱推导导致的参数校验误拦。
    - ``required``：必填参数名列表（映射到 ToolSpec.required_parameters）。

    保守处理：inputSchema 缺失 / 非 object / 无 properties 时返回空三件套（工具仍可注册，
    只是没有参数声明，调用时把任意 arguments 透传给 server）。
    """
    parameters: dict[str, str] = {}
    parameter_schema: dict[str, Any] = {}
    if not isinstance(input_schema, dict):
        return parameters, parameter_schema, []
    properties = input_schema.get("properties")
    if not isinstance(properties, dict):
        return parameters, parameter_schema, []
    for raw_name, prop in properties.items():
        name = str(raw_name)
        if isinstance(prop, dict):
            description = str(prop.get("description") or "")
            precise = {k: prop[k] for k in _SCHEMA_PASSTHROUGH_KEYS if k in prop}
            if precise:
                parameter_schema[name] = precise
        else:
            description = ""
        parameters[name] = description
    required_raw = input_schema.get("required")
    required: list[str] = []
    if isinstance(required_raw, (list, tuple)):
        required = [str(item) for item in required_raw if str(item) in parameters]
    return parameters, parameter_schema, required


# LLM: MCP proxy 的 Schema 只在它绑定的已握手子进程仍存活时暴露，执行仍走统一 effect/approval 门。
# 类用途: 把一个已发现 MCP remote tool 映射为 my-agent 的受控本地工具代理。
class MCPProxyTool(BaseTool):
    """一个把调用转发给某 MCP server ``tools/call`` 的代理工具。

    ``execute`` 把模型传来的 dict 参数（去掉内部 ``tool`` 字段）转发给 server，
    再把结果归一化成 ``ToolExecutionResult``。server 自报的工具错（isError=true）
    映射成 ``error_code=TOOL_EXECUTION_FAILED``（可重试语义）；客户端层异常按其
    ``MCPError.code`` 映射成 my-agent 错误码。
    """

    def __init__(self, client: MCPStdioClient, remote_tool: str, spec: ToolSpec):
        self.client = client
        self.remote_tool = remote_tool
        self.spec = spec

    # LLM: 只读取既有 stdio 进程状态，不自动重启或重新发现工具，防止列表查询产生子进程副作用。
    # 函数用途: MCP server 掉线后立即从后续请求快照中隐藏对应 proxy。
    def availability(self) -> ToolAvailability:
        if self.client.is_running():
            return ToolAvailability.ready()
        return ToolAvailability.unavailable("MCP stdio server 当前未运行")

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        arguments = {k: v for k, v in (params or {}).items() if k != "tool"}
        try:
            result = self.client.call_tool(self.remote_tool, arguments)
        except MCPError as exc:
            return self._error_result(str(exc), _ERROR_CODE_MAP.get(exc.code, "TOOL_EXECUTION_FAILED"))
        except Exception as exc:  # 兜底：任何意外异常都转结构化错误，绝不向上崩主循环。
            logger.exception("MCP 工具 %s 调用出现未预期异常", self.spec.name)
            return self._error_result(
                sanitize_credentials(f"MCP 工具调用异常：{type(exc).__name__}: {exc}"),
                "TOOL_EXECUTION_FAILED",
            )

        content = sanitize_credentials(str(result.get("content") or ""))
        if result.get("isError"):
            # server 把工具自身的失败放在 content 里（isError=true）。如实回传 + 标错误码，
            # 让模型知道是工具执行失败（可调参重试）而非框架错误。
            return self._error_result(content or "MCP 工具返回错误", "TOOL_EXECUTION_FAILED")

        payload: dict[str, Any] = {"result": content}
        if result.get("structuredContent") is not None:
            payload["structuredContent"] = result["structuredContent"]
        return ToolExecutionResult(
            self.spec.name,
            True,
            json.dumps(payload, ensure_ascii=False),
        )

    def _error_result(self, message: str, error_code: str) -> ToolExecutionResult:
        return ToolExecutionResult(
            self.spec.name,
            False,
            json.dumps({"error": message}, ensure_ascii=False),
            error_code=error_code,
        )


# MCPError.code → my-agent 错误码。让模型拿到正确的可重试/恢复语义：
# 超时/断连可重试；配置/协议错不应原样空转重试。
_ERROR_CODE_MAP = {
    "MCP_TIMEOUT": "TOOL_TIMEOUT",
    "MCP_CONNECTION_CLOSED": "TOOL_EXECUTION_FAILED",
    "MCP_PROTOCOL_ERROR": "TOOL_EXECUTION_FAILED",
    "MCP_SERVER_START_FAILED": "TOOL_UNAVAILABLE",
    "MCP_CONFIG_INVALID": "TOOL_UNAVAILABLE",
    "MCP_ERROR": "TOOL_EXECUTION_FAILED",
}


def build_proxy_tool(
    client: MCPStdioClient,
    server_name: str,
    info: MCPToolInfo,
    *,
    effect: str = "dangerous",
) -> MCPProxyTool:
    """从一个发现的 MCP 工具构造可注册的 ``MCPProxyTool``（含转换好的 ToolSpec）。"""
    parameters, parameter_schema, required = input_schema_to_parameters(info.input_schema)
    upstream_description = info.description or f"工具 {info.name}"
    description = (
        f"管理员配置的外部 MCP 服务 '{server_name}' 提供的 '{info.name}' 能力。"
        f"{upstream_description}"
    )
    spec = ToolSpec(
        name=mcp_tool_name(server_name, info.name),
        category="mcp",
        description=sanitize_credentials(description),
        use_cases=_mcp_use_cases(server_name, info.name, effect),
        avoid_when=["该外部能力与当前任务无关时不要调用"],
        keywords=_mcp_keywords(server_name, info.name, effect),
        parameters=parameters,
        parameter_schema=parameter_schema,
        required_parameters=required,
        effect=effect,
        default_mode="read_only" if effect == "read_only" else "real",
        requires_idempotency=effect in {"mutating", "dangerous"},
        requires_approval=effect == "dangerous",
    )
    return MCPProxyTool(client, info.name, spec)


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
        try:
            client.start()
            tools = client.list_tools()
        except MCPError as exc:
            logger.warning(
                "MCP server '%s' 连接失败，跳过（command=%s env=%s）：%s",
                config.name, config.command, redact_env_for_log(config.env), exc,
            )
            client.stop()
            continue
        except Exception as exc:  # 兜底：连接路径任何意外异常都不许崩主流程。
            logger.exception("MCP server '%s' 连接出现未预期异常，跳过", config.name)
            client.stop()
            continue

        registered = _register_discovered_tools(registry, client, config, tools)
        logger.info(
            "MCP server '%s' 已连接，注册 %d 个工具：%s",
            config.name, registered, _tool_names_preview(tools),
        )
    return clients


def refresh_registered_mcp_client(
    registry: Any,
    client: MCPStdioClient,
) -> int:
    """Refresh one reconnected server and atomically publish its exact tool catalog."""
    tools = client.list_tools()
    current = dict(getattr(registry, "tools", {}) or {})
    old_names = {
        name
        for name, tool in current.items()
        if isinstance(tool, MCPProxyTool) and tool.client is client
    }
    replacement = {name: tool for name, tool in current.items() if name not in old_names}
    registered = 0
    for info in tools:
        proxy = build_proxy_tool(
            client,
            client.config.name,
            info,
            effect=client.config.effect_for_tool(info.name),
        )
        if proxy.spec.name in replacement:
            logger.warning(
                "MCP 工具名冲突，跳过 server '%s' 的 '%s'（已存在 %s）",
                client.config.name,
                info.name,
                proxy.spec.name,
            )
            continue
        replacement[proxy.spec.name] = proxy
        registered += 1
    registry.tools = replacement
    return registered


def _register_discovered_tools(
    registry: Any,
    client: MCPStdioClient,
    config: MCPServerConfig,
    tools: list[MCPToolInfo],
) -> int:
    """把一个 server 发现的工具逐个包成代理工具注册进 registry；返回注册数。"""
    count = 0
    existing = getattr(registry, "tools", {})
    for info in tools:
        proxy = build_proxy_tool(
            client,
            config.name,
            info,
            effect=config.effect_for_tool(info.name),
        )
        if proxy.spec.name in existing:
            # 极端情况下两个 server 清洗后撞名：保留先到者，跳过后者并告警。
            logger.warning(
                "MCP 工具名冲突，跳过 server '%s' 的 '%s'（已存在 %s）",
                config.name, info.name, proxy.spec.name,
            )
            continue
        registry.register(proxy)
        count += 1
    return count


def _tool_names_preview(tools: list[MCPToolInfo]) -> str:
    names = [info.name for info in tools[:10]]
    suffix = " ..." if len(tools) > 10 else ""
    return ", ".join(names) + suffix if names else "(无)"


__all__ = [
    "MCPProxyTool",
    "build_proxy_tool",
    "input_schema_to_parameters",
    "mcp_tool_name",
    "parse_mcp_servers",
    "refresh_registered_mcp_client",
    "register_mcp_servers",
    "sanitize_name_component",
]
