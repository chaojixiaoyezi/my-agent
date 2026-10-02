# LLM: 部署者在 mcp_servers.<server> 下的 tool_approvals / tool_observations 是宿主侧逐工具声明（和插件 manifest v5 同形），不是
#   server 自报，不授予权限：审批只能等于或严于 ApprovalPolicy 默认的 dangerous（never 等于免审 dangerous 工具，配置非法）；
#   观察声明按 plugin_observation.validate_observation_declaration 的同一套规则核对。核对只按本次发现到的工具算：坏项抛
#   MCPDeclarationError（整个服务拒绝发布，带结构化 reasons），声明了但本次没发现的工具只记 notice。发布结果记成
#   MCPPublication 挂在客户端上，供状态投影读取。改动须同步 mcp_client.MCPServerConfig.from_mapping、
#   mcp_registration.refresh_registered_mcp_client、computer_use_profile 的声明表与 test_mcp_observation_binding。
# 模块用途: 把 YAML 里的逐工具声明变成结构化对象，并在工具目录发布前核对它们和真实工具对不对得上。
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..plugin_observation import (
    ObservationDeclarationError,
    PluginToolObservation,
    PluginToolObservationRef,
    validate_observation_declaration,
)
from .input_schema import canonicalize_tool_input_schema
from .mcp_protocol import MCPError

# 允许的逐工具审批模式：等于或严于默认 dangerous；never 会免去 dangerous 工具的审批，属于放宽安全边界，不接受
VALID_TOOL_APPROVALS = frozenset({"dangerous", "mutating", "always"})
# 声明与发现到的工具对不上时整服务拒绝的状态码；notice 里"声明了但没发现"的原因码
DECLARATION_INVALID_CODE = "MCP_DECLARATION_INVALID"
DECLARED_TOOL_NOT_DISCOVERED = "declared_tool_not_discovered"
_OBSERVATION_KINDS = ("observation", "observation_ref")


# LLM: reasons 是结构化列表 [{"tool", "code"}]，code 来自 ObservationDeclarationError.code 或 observation_ref_unpaired；
#   message 不含配置值。
# 类用途: 逐工具声明与发现到的工具对不上时的整服务拒绝。
class MCPDeclarationError(MCPError):
    def __init__(self, server_name: str, reasons: list[dict[str, str]]) -> None:
        super().__init__(f"MCP server '{server_name}' 的逐工具声明与发现到的工具不一致", code=DECLARATION_INVALID_CODE)
        self.reasons = tuple(reasons)


# LLM: status ∈ published / rejected / unavailable；rejected 带 code 与 reasons，published 带 tool_count 与 notices。
#   这是一次发布尝试的结构化结果，不是权限，也不替代 is_running。
# 类用途: 一个 MCP 服务最近一次目录发布的结果事实。
@dataclass(frozen=True)
class MCPPublication:
    status: str
    tool_count: int = 0
    code: str = ""
    reasons: tuple[dict[str, str], ...] = ()
    notices: tuple[dict[str, str], ...] = ()

    # 函数用途: 状态投影用的纯 JSON 形状。
    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "tool_count": self.tool_count, "code": self.code,
                "reasons": [dict(item) for item in self.reasons], "notices": [dict(item) for item in self.notices]}


# 类用途: 核对通过后，一个发现到的工具的宿主声明：effect、审批模式与可选的观察/观察引用。
@dataclass(frozen=True)
class ResolvedToolDeclaration:
    effect: str
    approval: str
    observation: PluginToolObservation | None = None
    observation_ref: PluginToolObservationRef | None = None


# 类用途: 整个服务核对后的声明结果：逐工具声明加"声明了但没发现"的提醒。
@dataclass(frozen=True)
class ResolvedServerDeclarations:
    tools: dict[str, ResolvedToolDeclaration]
    notices: tuple[dict[str, str], ...] = ()

    # 函数用途: 某个 target_kind 下"远端动作工具名 → 宿主注册名"的映射，供观察工具校验候选 actions。
    def action_tools(self, target_kind: str, host_name) -> dict[str, str]:
        return {name: host_name(name) for name, item in self.tools.items()
                if item.observation_ref is not None and item.observation_ref.target_kind == target_kind}


# 函数用途: 解析 tool_approvals 映射；模式不在允许集合（含 never）就报配置非法。
def parse_tool_approvals(value: object, *, server_name: str) -> dict[str, str]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise MCPError(f"MCP server '{server_name}' 的 tool_approvals 必须是映射", code="MCP_CONFIG_INVALID")
    approvals = {}
    for tool_name, mode in value.items():
        normalized = str(mode or "").strip().lower()
        if normalized not in VALID_TOOL_APPROVALS:
            raise MCPError(
                f"MCP server '{server_name}' 的 tool_approvals.{tool_name} 必须是 dangerous、mutating 或 always"
                "（never 会放宽审批边界，不接受）",
                code="MCP_CONFIG_INVALID",
            )
        approvals[str(tool_name)] = normalized
    return approvals


# 函数用途: 解析 tool_observations 映射：每个工具恰好一项 observation 或 observation_ref，字段交给 v5 数据类校验。
def parse_tool_observations(value: object, *, server_name: str) -> dict[str, PluginToolObservation | PluginToolObservationRef]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise MCPError(f"MCP server '{server_name}' 的 tool_observations 必须是映射", code="MCP_CONFIG_INVALID")
    return {str(tool_name): _parse_tool_observation(raw, server_name=server_name, tool_name=str(tool_name))
            for tool_name, raw in value.items()}


# 函数用途: 解析单个工具的观察声明项。
def _parse_tool_observation(raw: object, *, server_name: str, tool_name: str) -> PluginToolObservation | PluginToolObservationRef:
    kinds = [kind for kind in _OBSERVATION_KINDS if isinstance(raw, dict) and kind in raw]
    if not isinstance(raw, dict) or len(kinds) != 1 or set(raw) != {kinds[0]} or not isinstance(raw[kinds[0]], dict):
        raise MCPError(
            f"MCP server '{server_name}' 的 tool_observations.{tool_name} 必须恰好包含 observation 或 observation_ref 一项",
            code="MCP_CONFIG_INVALID",
        )
    factory = PluginToolObservation if kinds[0] == "observation" else PluginToolObservationRef
    try:
        return factory(**raw[kinds[0]])
    except (TypeError, ValueError) as exc:
        raise MCPError(
            f"MCP server '{server_name}' 的 tool_observations.{tool_name} 无效：{exc}", code="MCP_CONFIG_INVALID",
        ) from exc


# LLM: 只按发现到的工具核对：每个声明了观察的工具按 effect / 输入 schema 走共用规则，observation_ref 必须有同 target_kind 的已发现
#   观察工具配对；任一坏项收集成 reasons 后整服务抛 MCPDeclarationError。声明了但没发现的工具不算坏项，只进 notices。
# 函数用途: 把配置里的逐工具声明和本次 tools/list 的结果对上，得到每个工具的 effect / 审批 / 观察声明。
def resolve_server_declarations(config: Any, tools: list[Any]) -> ResolvedServerDeclarations:
    resolved: dict[str, ResolvedToolDeclaration] = {}
    reasons: list[dict[str, str]] = []
    for info in tools:
        declared = config.tool_observations.get(info.name)
        observation = declared if isinstance(declared, PluginToolObservation) else None
        observation_ref = declared if isinstance(declared, PluginToolObservationRef) else None
        effect = config.effect_for_tool(info.name)
        code = _declaration_issue(observation, observation_ref, effect=effect, raw_schema=info.input_schema)
        if code:
            reasons.append({"tool": info.name, "code": code})
        resolved[info.name] = ResolvedToolDeclaration(effect, config.approval_for_tool(info.name), observation, observation_ref)
    observed_kinds = {item.observation.target_kind for item in resolved.values() if item.observation is not None}
    reasons.extend({"tool": name, "code": "observation_ref_unpaired"} for name, item in resolved.items()
                   if item.observation_ref is not None and item.observation_ref.target_kind not in observed_kinds)
    if reasons:
        raise MCPDeclarationError(config.name, reasons)
    notices = tuple({"tool": name, "code": DECLARED_TOOL_NOT_DISCOVERED} for name in config.tool_observations if name not in resolved)
    return ResolvedServerDeclarations(resolved, notices)


# 函数用途: 一个工具的观察声明是否合规；返回结构化原因码，合规返回空串。没有声明的工具不做任何核对。
def _declaration_issue(observation: object, observation_ref: object, *, effect: str, raw_schema: object) -> str:
    if observation is None and observation_ref is None:
        return ""
    try:
        schema = canonicalize_tool_input_schema(raw_schema if isinstance(raw_schema, dict) else {})
        validate_observation_declaration(observation, observation_ref, effect=effect, input_schema=schema)
    except ObservationDeclarationError as exc:
        return exc.code
    except (TypeError, ValueError):
        return "observation_ref_param"
    return ""


__all__ = [
    "DECLARATION_INVALID_CODE", "DECLARED_TOOL_NOT_DISCOVERED", "MCPDeclarationError", "MCPPublication",
    "ResolvedServerDeclarations", "ResolvedToolDeclaration", "VALID_TOOL_APPROVALS", "parse_tool_approvals",
    "parse_tool_observations", "resolve_server_declarations",
]
