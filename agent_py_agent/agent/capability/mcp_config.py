from __future__ import annotations

# LLM: MCP config parsing converts raw startup settings into typed runtime bundles.
# 模块用途: 把 AgentConfig 里的 MCP 描述、stdio server 和授权字段转换成 registry 可消费对象。
import shlex
from dataclasses import dataclass
from typing import Any

from .grants import CapabilityGrantScope
from .mcp import McpToolDescriptor
from .mcp_runtime import McpStdioServerSpec, StdioMcpExecutor


# LLM: McpRegistryConfig is the typed bridge from AgentConfig into ToolRegistryParams.
# 类用途: 保存 MCP tool 描述、executor 和 capability grant scope，避免长期传 dict。
@dataclass(frozen=True)
class McpRegistryConfig:
    descriptors: list[McpToolDescriptor]
    executor: StdioMcpExecutor | None
    grant_scope: CapabilityGrantScope | None


# LLM: mcp_registry_config_from_agent_config parses only the MCP slice of AgentConfig.
# 函数用途: 从配置对象读取 MCP 原始字段，并转换成 registry 初始化需要的类型。
def mcp_registry_config_from_agent_config(config: object) -> McpRegistryConfig:
    descriptors = [_descriptor_from_raw(item) for item in _raw_list(config, "mcp_tool_descriptors")]
    servers = [_server_spec_from_raw(item) for item in _raw_list(config, "mcp_stdio_servers")]
    executor = StdioMcpExecutor(servers) if servers else None
    if executor is not None and bool(getattr(config, "mcp_auto_discover_tools", True)):
        descriptors = _merge_discovered_descriptors(descriptors, executor, servers)
    grant_scope = _grant_scope_from_config(config)
    return McpRegistryConfig(
        descriptors=descriptors,
        executor=executor,
        grant_scope=grant_scope,
    )


# LLM: _descriptor_from_raw normalizes one config entry into an McpToolDescriptor.
# 函数用途: 校验 MCP tool 配置的 server/name，并复制可搜索描述字段。
def _descriptor_from_raw(raw: object) -> McpToolDescriptor:
    if not isinstance(raw, dict):
        raise ValueError("mcp_tool_descriptors entries must be objects")
    server = str(raw.get("server") or "").strip()
    name = str(raw.get("name") or "").strip()
    if not server or not name:
        raise ValueError("MCP tool descriptor requires server and name")
    return McpToolDescriptor(
        server=server,
        name=name,
        description=str(raw.get("description") or name),
        capabilities=_string_list(raw.get("capabilities")),
        when_to_use=_string_list(raw.get("when_to_use")),
        not_when_to_use=_string_list(raw.get("not_when_to_use")),
        keywords=_string_list(raw.get("keywords")),
        risk_level=str(raw.get("risk_level") or "medium"),
        source=str(raw.get("source") or "mcp"),
        input_schema=raw.get("input_schema") if isinstance(raw.get("input_schema"), dict) else {},
    )


# LLM: _server_spec_from_raw normalizes one config entry into an stdio server spec.
# 函数用途: 校验 MCP server 配置，并把 command/env/timeout 转成 typed server spec。
def _server_spec_from_raw(raw: object) -> McpStdioServerSpec:
    if not isinstance(raw, dict):
        raise ValueError("mcp_stdio_servers entries must be objects")
    name = str(raw.get("name") or "").strip()
    command = _command(raw.get("command"))
    if not name or not command:
        raise ValueError("MCP stdio server requires name and command")
    env = raw.get("env")
    return McpStdioServerSpec(
        name=name,
        command=command,
        cwd=str(raw.get("cwd") or ""),
        env={str(key): str(value) for key, value in env.items()} if isinstance(env, dict) else {},
        startup_timeout_seconds=_float(raw.get("startup_timeout_seconds"), default=10.0),
        request_timeout_seconds=_float(raw.get("request_timeout_seconds"), default=30.0),
    )


# LLM: _grant_scope_from_config builds an optional visible/executable capability scope.
# 函数用途: 读取 capability_grant_* 配置字段，全部为空时返回 None 表示不额外过滤。
def _grant_scope_from_config(config: object) -> CapabilityGrantScope | None:
    tools = _raw_string_list(config, "capability_grant_tools")
    skills = _raw_string_list(config, "capability_grant_skills")
    mcp_tools = _raw_string_list(config, "capability_grant_mcp_tools")
    if not tools and not skills and not mcp_tools:
        return None
    return CapabilityGrantScope(tools=tools, skills=skills, mcp_tools=mcp_tools)


# LLM: _raw_list accepts list-valued config fields and treats missing values as empty.
# 函数用途: 读取配置对象上的列表字段，并过滤非列表输入。
def _raw_list(config: object, name: str) -> list[object]:
    value = getattr(config, name, [])
    return list(value) if isinstance(value, list) else []


# LLM: _raw_string_list returns normalized string list config values.
# 函数用途: 读取配置对象上的字符串列表字段，去掉空白项。
def _raw_string_list(config: object, name: str) -> list[str]:
    return _string_list(getattr(config, name, []))


# LLM: _string_list converts config list-like values to non-empty strings.
# 函数用途: 把配置字段规范化成字符串列表。
def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


# LLM: _command supports both shell-like strings and explicit argv arrays at the config boundary.
# 函数用途: 把 MCP server command 转换为 subprocess 需要的 argv 列表。
def _command(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str):
        return shlex.split(value)
    return []


# LLM: _float parses optional timeout fields without leaking ValueError to config callers.
# 函数用途: 把配置里的超时字段转换为 float，失败时使用默认值。
def _float(value: object, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# LLM: _merge_discovered_descriptors adds tools/list metadata without overwriting explicit descriptors.
# 函数用途: 从 MCP server 自动发现 tool schema，并和手写 descriptor 合并。
def _merge_discovered_descriptors(
    descriptors: list[McpToolDescriptor],
    executor: StdioMcpExecutor,
    servers: list[McpStdioServerSpec],
) -> list[McpToolDescriptor]:
    merged = {(item.server, item.name): item for item in descriptors}
    for server in servers:
        for raw_tool in executor.list_tools(server.name):
            descriptor = _descriptor_from_discovered_tool(server.name, raw_tool)
            merged.setdefault((descriptor.server, descriptor.name), descriptor)
    return list(merged.values())


# LLM: _descriptor_from_discovered_tool converts MCP tools/list entries into routable descriptors.
# 函数用途: 把 MCP tools/list 的 name/description/inputSchema 转成 McpToolDescriptor。
def _descriptor_from_discovered_tool(server: str, raw_tool: dict[str, Any]) -> McpToolDescriptor:
    name = str(raw_tool.get("name") or "").strip()
    if not name:
        raise ValueError(f"MCP server {server!r} returned tool without name")
    input_schema = raw_tool.get("inputSchema")
    return McpToolDescriptor(
        server=server,
        name=name,
        description=str(raw_tool.get("description") or name),
        capabilities=["mcp", server, name],
        keywords=[server, name],
        risk_level="medium",
        source="mcp_discovered",
        input_schema=input_schema if isinstance(input_schema, dict) else {},
    )
