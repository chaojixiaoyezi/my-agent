
from __future__ import annotations

"""coordinates tool registration, prompt rendering, call parsing, authorization, and execution.

这个文件是工具系统的"前台服务台"。
它不亲自实现读文件或发 HTTP，而是登记这些工具、给模型渲染工具菜单、解析模型发来的工具调用，
再按授权和写入边界把请求分发给真正的工具。
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..contracts.tool_manifest_contract import tool_manifest_payload
from ..settings.defaults import default_config_int
from .artifact import ReadArtifactTool
from .content_transport_policy import (
    MAX_INLINE_WRITE_CONTENT_CHARS,
    tool_content_transport_protocol,
)
from .models import (
    BaseTool,
    ToolExecutionResult,
    ToolSpec,
)
from .registry_bootstrap import build_tool_retriever, register_base_tools
from .registry_execution import (
    ExecuteRegistryCallParams,
    allowed_tool_set,
    execute_registry_call,
    parse_registry_tool_calls,
)
from .registry_payload_normalize import ToolPayloadNormalizeLimits

_allowed_tool_set = allowed_tool_set
_DEFAULT_HIDDEN_TOOL_NAMES = frozenset({"controlled_exec"})


def _agent_config_int(key: str) -> int:
    return default_config_int(key)


@dataclass(frozen=True)
class CatalogRenderConfig:
    mode: str
    offset: int
    limit: int
    categories: list[str]
    include_examples: bool
    entry_max_chars: int
    show_truncated_notice: bool
    detail_max_chars: int
    # 渐进式披露:这些 category 的工具不进主目录全量渲染,只在末尾留一行折叠清单(名字)。
    # 仍可被 vector 推荐区按任务拉出、被 list_tools 查到、按名直接调用。默认空=老行为(全量)。
    deferred_categories: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ToolRegistryParams:
    workspace_root: Path
    max_chars: int
    max_entries: int
    max_matches: int
    web_max_chars: int
    http_timeout: int
    catalog_limit: int
    retrieval_limit: int
    vector_search_enabled: bool
    workspace_roots: list[Path] | None = None
    path_access_mode: str = "normal"
    path_dangerous_roots: list[str] | None = None
    owner_scope_root: str = ""  # 多用户隔离 0 层:per-user agent 的 owner home;空=不隔离(单租户/主代理)
    access_mode: str = "workspace-write"
    shell_tool_timeout: int = 30
    shell_tool_output_max_chars: int = 12_000
    catalog_mode: str = "compact"
    catalog_offset: int = 0
    catalog_categories: list[str] | None = None
    catalog_deferred_categories: list[str] | None = None
    catalog_include_examples: bool = False
    catalog_entry_max_chars: int = 700
    catalog_show_truncated_notice: bool = True
    tool_detail_max_chars: int = 0
    tool_write_inline_max_chars: int = MAX_INLINE_WRITE_CONTENT_CHARS
    artifact_read_budget_window_seconds: int = field(
        default_factory=lambda: _agent_config_int("tool_artifact_read_budget_window_seconds")
    )
    artifact_read_budget_max_chars: int = field(
        default_factory=lambda: _agent_config_int("tool_artifact_read_budget_max_chars")
    )
    artifact_default_read_chars: int = field(
        default_factory=lambda: _agent_config_int("memory_artifact_default_read_chars")
    )
    payload_limits: ToolPayloadNormalizeLimits | None = None
    disabled_tools: list[str] = field(default_factory=list)
    artifact_root: Path | None = None
    runtime_fact_roots: list[Path] | None = None
    runtime_guard_policy: object | None = None
    # MCP 客户端(短板6)：要连接的外部 MCP server 声明。默认空 = 不连、不起子进程(零开销)。
    mcp_servers: dict[str, Any] | None = None
    # 视觉理解(短板6)：辅助视觉模型配置(VisionModelConfig)。默认 None = 未配视觉模型,
    # analyze_image 注册但调用时返回 TOOL_UNAVAILABLE(可选加法,零默认影响)。
    vision_config: Any | None = None


class ListToolsTool(BaseTool):

    def __init__(self, registry: Any):
        self.registry = registry
        self.spec = ToolSpec(
            name="list_tools",
            category="system",
            effect="read_only",
            description="列出当前执行上下文可见的工具清单。",
            use_cases=[
                "不确定当前有哪些工具时，先查询机器可读工具清单",
                "需要确认 run_command、write_file、apply_patch 等工具是否可用",
            ],
            avoid_when=[
                "已经知道要用哪个工具时，直接调用目标工具",
            ],
            keywords=["list_tools", "tools", "工具清单", "tool manifest", "available tools"],
            parameters={},
            examples=['{"tool": "list_tools"}'],
        )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        specs = self.registry.specs(include_orchestration=True)
        payload = tool_manifest_payload(specs, owner_type="main_agent")
        payload["tool_failure_taxonomy"] = payload["failure_taxonomy"]
        return ToolExecutionResult(
            "list_tools",
            True,
            json.dumps(payload, ensure_ascii=False),
            result_envelope={"tool_output_policy": {"preserve_prompt_output": True}},
        )


class ToolRegistry:

    def __init__(
        self,
        params: ToolRegistryParams,
    ):
        self.workspace_root = params.workspace_root.resolve()
        self.workspace_roots = params.workspace_roots or [self.workspace_root]
        self.path_access_mode = params.path_access_mode
        self.path_dangerous_roots = params.path_dangerous_roots or []
        self.owner_scope_root = params.owner_scope_root
        self.tools: dict[str, BaseTool] = {}
        self.default_hidden_tool_names = set(_DEFAULT_HIDDEN_TOOL_NAMES)
        self.disabled_tool_names = {str(item).strip() for item in params.disabled_tools if str(item).strip()}
        self.catalog_limit = params.catalog_limit
        self.catalog_mode = params.catalog_mode
        self.catalog_offset = max(0, params.catalog_offset)
        self.catalog_categories = [item.strip() for item in params.catalog_categories or [] if item.strip()]
        self.catalog_deferred_categories = [
            item.strip() for item in params.catalog_deferred_categories or [] if item.strip()
        ]
        self.catalog_include_examples = params.catalog_include_examples
        self.catalog_entry_max_chars = max(0, params.catalog_entry_max_chars)
        self.catalog_show_truncated_notice = params.catalog_show_truncated_notice
        self.tool_detail_max_chars = max(0, params.tool_detail_max_chars)
        self.payload_limits = params.payload_limits
        self.runtime_guard_policy = params.runtime_guard_policy
        self.retrieval_limit = params.retrieval_limit
        self.retriever = build_tool_retriever(params)
        register_base_tools(self, params)
        self.register(ListToolsTool(self))
        # MCP 客户端(短板6)：连接配置的外部 MCP server，把其工具动态注册成 mcp__* 前缀工具。
        # mcp_servers 为空时此调用零开销返回(不起任何子进程)；任一 server 连不上只记日志跳过。
        self._mcp_clients = _connect_mcp_servers(self, params.mcp_servers)

    def register(self, tool: BaseTool) -> None:

        self.tools[tool.spec.name] = tool

    def close_mcp_clients(self) -> None:
        """关闭所有已连接的 MCP server 子进程(进程生命周期收尾)。幂等。"""
        for client in getattr(self, "_mcp_clients", ()) or ():
            try:
                client.stop()
            except Exception:  # 关闭尽力而为，单个失败不阻断其余清理。
                pass
        self._mcp_clients = []

    def specs(
        self,
        *,
        allowed_tools: list[str] | None = None,
        granted_capabilities: list[str] | None = None,
        include_orchestration: bool = False,
    ) -> list[ToolSpec]:

        allowed = allowed_tool_set(allowed_tools)
        specs = [tool.spec for tool in self.tools.values()]
        if self.disabled_tool_names:
            specs = [spec for spec in specs if spec.name not in self.disabled_tool_names]
        if allowed is None:
            specs = [spec for spec in specs if spec.name not in self.default_hidden_tool_names]
        if not include_orchestration:
            specs = [spec for spec in specs if spec.category != "orchestration"]
        if allowed is None:
            return specs
        return [spec for spec in specs if spec.name in allowed]

    def render_catalog_section(
        self,
        *,
        allowed_tools: list[str] | None = None,
        granted_capabilities: list[str] | None = None,
        tool_protocol: str = "text",
    ) -> str:

        specs = self.specs(
            allowed_tools=allowed_tools,
            granted_capabilities=granted_capabilities,
            include_orchestration=True,
        )
        entries = render_catalog_entries(specs, self._catalog_render_config())
        return _render_tool_catalog_section(
            entries,
            tool_content_transport_protocol(self._write_inline_max_chars()),
            tool_protocol=tool_protocol,
        )

    def _catalog_render_config(self) -> CatalogRenderConfig:
        return CatalogRenderConfig(
            mode=self.catalog_mode,
            offset=self.catalog_offset,
            limit=self.catalog_limit,
            categories=self.catalog_categories,
            deferred_categories=self.catalog_deferred_categories,
            include_examples=self.catalog_include_examples,
            entry_max_chars=self.catalog_entry_max_chars,
            show_truncated_notice=self.catalog_show_truncated_notice,
            detail_max_chars=self.tool_detail_max_chars,
        )

    def _write_inline_max_chars(self) -> int:
        tool = self.tools.get("write_file")
        value = getattr(tool, "max_inline_content_chars", MAX_INLINE_WRITE_CONTENT_CHARS)
        try:
            return int(value)
        except (TypeError, ValueError):
            return MAX_INLINE_WRITE_CONTENT_CHARS

    def find_relevant_specs(
        self,
        query: str,
        *,
        allowed_tools: list[str] | None = None,
        granted_capabilities: list[str] | None = None,
    ) -> list[ToolSpec]:

        specs = self.specs(
            allowed_tools=allowed_tools,
            granted_capabilities=granted_capabilities,
            include_orchestration=True,
        )
        hits = self.retriever.search(query, specs, self.retrieval_limit)
        if not hits:
            return []
        by_name = {spec.name: spec for spec in specs}
        return [by_name[hit.name] for hit in hits if hit.name in by_name]

    def render_recommended_tools_section(
        self,
        query: str,
        *,
        allowed_tools: list[str] | None = None,
        granted_capabilities: list[str] | None = None,
    ) -> str:

        specs = self.specs(
            allowed_tools=allowed_tools,
            granted_capabilities=granted_capabilities,
            include_orchestration=True,
        )
        if not specs:
            return (
                "# Recommended Tools\n"
                "当前执行上下文没有授权工具。若缺少能力，请提交 capability_request。"
            )

        hits = self.retriever.search(query, specs, self.retrieval_limit)
        if not hits:
            return (
                "# Recommended Tools\n"
                "当前没有明显高相关的工具命中。若要动手操作，请先根据 Tool Catalog 选最接近的工具。"
            )

        by_name = {spec.name: spec for spec in specs}
        blocks: list[str] = []
        for hit in hits:
            spec = by_name[hit.name]
            reason_text = "；".join(hit.reasons) or "与当前任务相关"
            blocks.append(f"{spec.render_recommended_entry(max_chars=self.tool_detail_max_chars)}\n推荐理由：{reason_text}")
        return "# Recommended Tools\n" + "\n\n".join(blocks)

    def parse_tool_calls(self, text: str) -> list[dict[str, Any]]:
        return parse_registry_tool_calls(text, payload_limits=self.payload_limits)

    def execute_call(
        self,
        payload: object,
        *,
        allowed_tools: list[str] | None = None,
        granted_capabilities: list[str] | None = None,
        write_boundary: dict[str, object] | None = None,
    ) -> ToolExecutionResult:
        return execute_registry_call(
            ExecuteRegistryCallParams(
                payload=payload,
                tools=self.tools,
                workspace_root=self.workspace_root,
                workspace_roots=self.workspace_roots,
                path_access_mode=self.path_access_mode,
                path_dangerous_roots=self.path_dangerous_roots,
                default_hidden_tool_names=self.default_hidden_tool_names,
                allowed_tools=allowed_tools,
                granted_capabilities=granted_capabilities,
                disabled_tools=list(self.disabled_tool_names),
                write_boundary=write_boundary,
                payload_limits=self.payload_limits,
                runtime_guard_policy=self.runtime_guard_policy,
            )
        )


def _connect_mcp_servers(registry: "ToolRegistry", mcp_servers: dict[str, Any] | None) -> list[Any]:
    """惰性连接 MCP server 并注册其工具；返回已连接 client 列表(供 close 清理)。

    惰性 import ``mcp_registration``：mcp_servers 为空(默认)时连模块都不导入，零开销；
    且把 MCP 子系统与核心 registry 解耦。整个连接过程被 try 兜底——MCP 是可选加法，
    任何异常都不许阻断 registry 构造(主流程)。
    """
    if not mcp_servers:
        return []
    try:
        from .mcp_registration import register_mcp_servers

        return register_mcp_servers(registry, mcp_servers)
    except Exception:  # 兜底：连接子系统整体异常也不崩主流程。
        import logging

        logging.getLogger(__name__).exception("MCP server 连接子系统初始化失败，已跳过")
        return []


def _render_tool_catalog_section(
    entries: list[str],
    content_transport_protocol: str,
    *,
    tool_protocol: str = "text",
) -> str:
    if not entries:
        entries = ["- none：当前执行上下文没有授权任何工具；缺能力时请上抛 capability_request。"]
    return (
        _tool_call_protocol(tool_protocol)
        + "\n\n"
        + content_transport_protocol
        + "\n\n"
        "# Tool Catalog\n"
        + "\n".join(entries)
    )


def _tool_call_protocol(tool_protocol: str = "text") -> str:
    if str(tool_protocol or "").strip().lower() == "native":
        return _native_tool_call_protocol()
    return (
        "# Tools\n"
        "当你需要看文件、改代码、查网页或测接口时，可以调用工具。\n"
        "工具调用格式必须严格写成：\n"
        "[TOOL_CALL]\n"
        '{"tool": "read_file", "path": "README.md"}\n'
        "[/TOOL_CALL]\n"
        "必须把工具参数直接放在同一个 JSON 对象里；不要写 param_name、args、arguments 或其他包裹参数。\n"
        "必须使用 Tool Catalog 里该工具自己的参数名；不要把 path 当作所有工具的默认参数。\n"
        "可以连续写多个 [TOOL_CALL] 块。拿到工具结果后，再输出最终答案，不要把工具调用块留在最后回复里。"
    )


def _native_tool_call_protocol() -> str:
    # native 协议下模型直接用结构化 tool_use 调工具，不需要教它写 [TOOL_CALL] 文本格式。
    # 明确禁止退回文本协议：弱模型有训练惯性，偶尔会在正文里写 [TOOL_CALL]{...} 文本而非发起
    # 结构化调用，导致解析失败(TOOL_CALL_JSON_INVALID/UNCLOSED)。这条正向约束是低风险缓解，
    # 治本需历史 messages 完全结构化(对标 长期助手，红线区大工程，单独立项)。
    return (
        "# Tools\n"
        "当你需要看文件、改代码、查网页或测接口时，可以调用工具。\n"
        "本会话已启用原生工具调用：直接发起结构化工具调用即可，参数按下方 Tool Catalog 中各工具的参数名填写。\n"
        "重要：必须用原生工具调用机制发起调用；不要把工具调用写成正文里的文本 JSON 块，那样不会被执行。\n"
        "拿到工具结果后再输出最终答案。"
    )


def render_catalog_entries(specs: list[ToolSpec], config: CatalogRenderConfig) -> list[str]:
    filtered = _filter_catalog_specs(specs, config.categories)
    if config.mode == "off":
        return ["- disabled：tool_catalog_mode=off，当前 prompt 不注入工具目录。"]
    if config.mode == "retrieval_only":
        return ["- retrieval_only：工具目录精简隐藏，请依赖 Recommended Tools 或显式工具名调用。"]
    primary, deferred = _split_deferred_specs(filtered, config.deferred_categories)
    page = primary[config.offset : config.offset + max(0, config.limit)]
    entries = [_render_catalog_spec(spec, config) for spec in page]
    if config.show_truncated_notice:
        notice = _catalog_page_notice(config, total=len(primary), returned=len(page))
        if notice:
            entries.append(notice)
    deferred_notice = _render_deferred_notice(deferred)
    if deferred_notice:
        entries.append(deferred_notice)
    return entries


def _split_deferred_specs(
    specs: list[ToolSpec], deferred_categories: list[str],
) -> tuple[list[ToolSpec], list[ToolSpec]]:
    """渐进式披露:把 deferred category 的工具从主目录分出去(只在末尾留折叠清单)。"""
    if not deferred_categories:
        return specs, []
    deferred_set = set(deferred_categories)
    primary = [spec for spec in specs if spec.category not in deferred_set]
    deferred = [spec for spec in specs if spec.category in deferred_set]
    return primary, deferred


def _render_deferred_notice(specs: list[ToolSpec]) -> str:
    """折叠清单:只列被 defer 工具的名字(省 ~95% token)。模型可按名直接调用,
    或等 vector 推荐区按任务把完整 spec 拉出来,或用 list_tools 查全清单。"""
    if not specs:
        return ""
    names = ", ".join(sorted(spec.name for spec in specs))
    return (
        f"- ⊞ 另有 {len(specs)} 个垂直领域工具未在此展开（做相关任务时会自动出现在 "
        f"Recommended Tools，也可直接按工具名调用，或用 list_tools 查看完整说明）：{names}"
    )


def _filter_catalog_specs(specs: list[ToolSpec], categories: list[str]) -> list[ToolSpec]:
    if not categories:
        return specs
    allowed_categories = set(categories)
    return [spec for spec in specs if spec.category in allowed_categories]


def _render_catalog_spec(spec: ToolSpec, config: CatalogRenderConfig) -> str:
    if config.mode == "full":
        return spec.render_detail_entry(max_chars=config.detail_max_chars)
    return spec.render_catalog_entry(
        include_examples=config.include_examples,
        max_chars=config.entry_max_chars,
    )


def _catalog_page_notice(config: CatalogRenderConfig, *, total: int, returned: int) -> str:
    next_offset = config.offset + returned
    if total <= next_offset:
        return ""
    return (
        f"- more_tools：工具目录已分页，next_offset={next_offset} limit={config.limit} total={total}。"
        " 如需更多工具，请调大 tool_catalog_limit 或 tool_catalog_offset。"
    )
