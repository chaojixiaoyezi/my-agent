from __future__ import annotations

"""coordinates tool registration, prompt rendering, call parsing, authorization, and execution.

这个文件是工具系统的"前台服务台"。
它不亲自实现读文件或发 HTTP，而是登记这些工具、给模型渲染工具菜单、解析模型发来的工具调用，
再按授权和写入边界把请求分发给真正的工具。
"""

import json
from collections.abc import Callable
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
    ToolAvailability,
    ToolExecutionResult,
    ToolInvocationContext,
    ToolRuntimeSnapshot,
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
    # 渐进式披露:这些 category 的工具不进初始模型 schema，只在目录末尾留折叠清单。
    # 模型通过 tool_search 加载命中工具；list_tools 仍可查看完整注册表。默认空=全量直出。
    deferred_categories: list[str] = field(default_factory=list)


# LLM: 该不可变参数对象是 ToolRegistry 装配的单一配置载体；新增字段要同步 core 装配、bootstrap 消费和 registry 测试。
# 类用途: 汇总工作区、权限、工具上限和可选后端配置，避免每类工具各自读取一套全局配置。
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
    owner_scope_root: str = (
        ""  # 多用户隔离 0 层:per-user agent 的 owner home;空=不隔离(单租户/主代理)
    )
    owner_type: str = "main_agent"
    protected_persona_root: str = ""  # SOUL/AGENTS 单一受控写入口使用；admin bypass 也不清空
    owner_quota_max_bytes: int = 0
    owner_quota_policy_available: bool = True
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
    lsp_servers: dict[str, Any] | None = None
    # 视觉理解(短板6)：辅助视觉模型配置(VisionModelConfig)。默认 None = 未配视觉模型,
    # analyze_image 注册但调用时返回 TOOL_UNAVAILABLE(可选加法,零默认影响)。
    vision_config: Any | None = None
    # 真实语义工具检索的 embedding provider；未配置时 vector 通道明确显示 unconfigured。
    tool_embedder: Any | None = None
    # list_capabilities 只读运行时实际配置，不再把“代码里有适配器”虚报成“已经配置可用”。
    capability_config: Any | None = None
    # 通道状态和当前绑定来自 composition root 注入的唯一 registry/ConversationStore 事实。
    channel_registry: Any | None = None
    channel_binding_provider: Callable[[], Any | None] | None = None
    skill_snapshot_provider: Callable[[], Any] | None = None
    memory_snapshot_provider: Callable[[], dict[str, object]] | None = None
    persona_snapshot_provider: Callable[[], dict[str, object]] | None = None
    scheduler_snapshot_provider: Callable[[], dict[str, object]] | None = None


# LLM: list_tools 只能描述调用它的请求快照，不能退回进程级注册表或猜测 owner 类型。
# 类用途: 把当前请求真正可见且可执行的工具清单渲染成机器可读 manifest。
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

    # LLM: 直接调用仅供本地兼容和单测；真实 Tool Gateway 会走 execute_scoped 复用请求快照。
    # 函数用途: 用当前 registry 的默认权限与可用性生成一次自洽工具清单。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        _ = params
        return self._execute_snapshot(self.registry.runtime_snapshot())

    # LLM: 请求快照已经完成 owner、allowlist 与 availability 交集，目录工具不得重新放宽。
    # 函数用途: 在统一执行入口中列出本轮实际可见工具。
    def execute_scoped(
        self,
        params: dict[str, Any],
        context: ToolInvocationContext,
    ) -> ToolExecutionResult:
        _ = params
        return self._execute_snapshot(context.runtime_snapshot)

    # LLM: 完整归档与紧凑 prompt 输出必须来自同一份 specs，防止两份清单漂移。
    # 函数用途: 把指定运行快照渲染为完整 manifest 和有界模型视图。
    def _execute_snapshot(self, snapshot: ToolRuntimeSnapshot) -> ToolExecutionResult:
        payload = tool_manifest_payload(
            list(snapshot.specs),
            owner_type=snapshot.owner_type,
        )
        payload["tool_failure_taxonomy"] = payload["failure_taxonomy"]
        payload["tool_retrieval"] = self.registry.retriever.status()
        live_payload = _live_tool_manifest_payload(payload)
        return ToolExecutionResult(
            "list_tools",
            True,
            json.dumps(payload, ensure_ascii=False),
            result_envelope={
                "tool_output_policy": {
                    "preserve_prompt_output": True,
                    "live_prompt_output": json.dumps(live_payload, ensure_ascii=False),
                }
            },
        )


# LLM: tool_search 只能在请求快照中检索，搜索结果永远不能扩大 allowed_tools 或恢复不可用工具。
# 类用途: 从本轮已授权且已就绪的 deferred 工具中检索下一回合可展开的 Schema。
class ToolSearchTool(BaseTool):
    """会话运行时 discovery for tools that are registered but not initially exposed."""

    def __init__(self, registry: Any):
        self.registry = registry
        self.spec = ToolSpec(
            name="tool_search",
            category="system",
            effect="read_only",
            description=(
                "搜索尚未在当前模型回合直接展开的工具，并把命中的工具定义加载到下一次模型调用。"
                "当任务需要子代理、/goal 生命周期或跨代理协作，而当前工具列表里没有对应工具时使用。"
            ),
            use_cases=[
                "需要一种当前未直接提供的工具能力",
                "需要创建/管理子代理、读取持续目标或发起跨代理协作",
            ],
            avoid_when=["目标工具已经出现在当前工具列表中时，直接调用目标工具"],
            keywords=["tool search", "工具搜索", "发现工具", "子代理", "goal", "协作"],
            parameters={
                "query": "必填。描述需要的工具能力或工具名。",
                "limit": "可选。最多返回多少个工具，默认 8，范围 1-20。",
            },
            parameter_schema={
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            required_parameters=["query"],
            examples=['{"tool":"tool_search","query":"创建并管理子代理","limit":8}'],
        )

    # LLM: 直接调用使用 registry 默认快照；真实调用由 execute_scoped 固定到本轮快照。
    # 函数用途: 为兼容调用者执行一次默认范围的 deferred 工具搜索。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        return self._execute_snapshot(params, self.registry.runtime_snapshot())

    # LLM: 受限子代理只能搜索父级下发的快照子集，不能看到未授权工具名或说明。
    # 函数用途: 使用统一请求快照完成工具搜索并返回可加载名称。
    def execute_scoped(
        self,
        params: dict[str, Any],
        context: ToolInvocationContext,
    ) -> ToolExecutionResult:
        return self._execute_snapshot(params, context.runtime_snapshot)

    # LLM: 搜索算法仍复用唯一 retriever；这里只注入运行快照，不维护第二套目录。
    # 函数用途: 校验查询参数并在指定快照中检索 deferred specs。
    def _execute_snapshot(
        self,
        params: dict[str, Any],
        snapshot: ToolRuntimeSnapshot,
    ) -> ToolExecutionResult:
        query = str(params.get("query") or "").strip()
        if not query:
            return ToolExecutionResult(
                "tool_search",
                False,
                json.dumps({"error": "query 不能为空"}, ensure_ascii=False),
                error_code="TOOL_INVALID_ARGUMENTS",
            )
        try:
            limit = int(params.get("limit") or 8)
        except (TypeError, ValueError):
            limit = 0
        if limit < 1 or limit > 20:
            return ToolExecutionResult(
                "tool_search",
                False,
                json.dumps({"error": "limit 必须在 1-20 之间"}, ensure_ascii=False),
                error_code="TOOL_INVALID_ARGUMENTS",
            )
        specs = self.registry.search_deferred_specs(
            query,
            limit=limit,
            runtime_snapshot=snapshot,
        )
        names = [spec.name for spec in specs]
        payload = {
            "schema_name": "tool_search_output",
            "schema_version": 1,
            "tools": [
                {
                    "name": spec.name,
                    "category": spec.category,
                    "description": spec.description,
                    "parameters": spec.parameters,
                    "required_parameters": list(spec.required_parameters),
                }
                for spec in specs
            ],
            "loaded_for_next_model_call": names,
        }
        return ToolExecutionResult(
            "tool_search",
            True,
            json.dumps(payload, ensure_ascii=False),
            result_envelope={
                "tool_search": {
                    "loaded_tool_names": names,
                }
            },
        )


def _live_tool_manifest_payload(payload: dict[str, object]) -> dict[str, object]:
    """Keep the model-facing discovery page bounded while preserving the full manifest."""
    tools = payload.get("tools")
    compact_tools = []
    for item in tools if isinstance(tools, list) else []:
        if not isinstance(item, dict):
            continue
        compact_tools.append(
            {
                "name": str(item.get("name") or ""),
                "category": str(item.get("category") or ""),
                "effect": str(item.get("effect") or ""),
                "requires_approval": item.get("requires_approval") is True,
                "parameters": list(item.get("parameters") or []),
            }
        )
    return {
        "schema_name": "tool_manifest_live_prompt",
        "schema_version": 1,
        "visible_tool_count": len(compact_tools),
        "permission_mode": str(payload.get("permission_mode") or ""),
        "tools": compact_tools,
        "tool_retrieval": payload.get("tool_retrieval") or {},
        "full_manifest": "完整参数、示例和失败合同已保存在本次工具输出归档中。",
    }


# LLM: Registry 是工具事实窄腰；注册、可见、搜索、Schema 与执行必须从同一请求快照派生。
# 类用途: 组合进程级工具实现，并为每个 Agent run 生成权限与可用性的不可变交集。
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
        self.owner_type = str(params.owner_type or "main_agent").strip() or "main_agent"
        self.tools: dict[str, BaseTool] = {}
        self.default_hidden_tool_names = set(_DEFAULT_HIDDEN_TOOL_NAMES)
        self.disabled_tool_names = {
            str(item).strip() for item in params.disabled_tools if str(item).strip()
        }
        self.catalog_limit = params.catalog_limit
        self.catalog_mode = params.catalog_mode
        self.catalog_offset = max(0, params.catalog_offset)
        self.catalog_categories = [
            item.strip() for item in params.catalog_categories or [] if item.strip()
        ]
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
        self.register(ToolSearchTool(self))
        self.register(ListToolsTool(self))
        # MCP 客户端(短板6)：连接配置的外部 MCP server，把其工具动态注册成 mcp__* 前缀工具。
        # mcp_servers 为空时此调用零开销返回(不起任何子进程)；任一 server 连不上只记日志跳过。
        self._mcp_clients = _connect_mcp_servers(self, params.mcp_servers)

    # LLM: 注册表只存进程级实现；是否能给某个请求使用由 runtime_snapshot 决定。
    # 函数用途: 按稳定工具名登记一个实现，后注册的同名实现显式覆盖旧值。
    def register(self, tool: BaseTool) -> None:
        self.tools[tool.spec.name] = tool

    # LLM: 快照先做硬权限交集，再做无副作用可用性检查；检查异常按不可用 fail-closed。
    # 函数用途: 固定一次 Agent run 可见、可搜、可调用的工具事实，供全部工具表面复用。
    def runtime_snapshot(
        self,
        *,
        allowed_tools: list[str] | None = None,
    ) -> ToolRuntimeSnapshot:
        allowed = allowed_tool_set(allowed_tools)
        available_specs: list[ToolSpec] = []
        unavailable: list[tuple[str, str, str]] = []
        for name, tool in self.tools.items():
            if name in self.disabled_tool_names:
                continue
            if allowed is None and name in self.default_hidden_tool_names:
                continue
            if allowed is not None and name not in allowed:
                continue
            availability = _safe_tool_availability(tool)
            if not availability.available:
                unavailable.append(
                    (
                        name,
                        availability.error_code or "TOOL_UNAVAILABLE",
                        availability.reason,
                    )
                )
                continue
            available_specs.append(tool.spec)
        return ToolRuntimeSnapshot(
            specs=tuple(available_specs),
            available_tool_names=frozenset(spec.name for spec in available_specs),
            unavailable_tools=tuple(unavailable),
            allowed_tools=frozenset(allowed) if allowed is not None else None,
            owner_type=self.owner_type,
        )

    def close_mcp_clients(self) -> None:
        """关闭所有已连接的 MCP server 子进程(进程生命周期收尾)。幂等。"""
        for client in getattr(self, "_mcp_clients", ()) or ():
            try:
                client.stop()
            except Exception:  # 关闭尽力而为，单个失败不阻断其余清理。
                pass
        self._mcp_clients = []
        lsp_manager = getattr(self, "_lsp_manager", None)
        if lsp_manager is not None:
            lsp_manager.close_all()

    # LLM: specs 只是 runtime_snapshot 的投影；它不再维护独立的授权或可用性判断。
    # 函数用途: 返回当前快照中的工具说明，并按调用者需要隐藏编排类工具。
    def specs(
        self,
        *,
        allowed_tools: list[str] | None = None,
        include_orchestration: bool = False,
        runtime_snapshot: ToolRuntimeSnapshot | None = None,
    ) -> list[ToolSpec]:
        snapshot = runtime_snapshot or self.runtime_snapshot(allowed_tools=allowed_tools)
        specs = list(snapshot.specs)
        allowed = allowed_tool_set(allowed_tools)
        if allowed is not None:
            specs = [spec for spec in specs if spec.name in allowed]
        if not include_orchestration:
            specs = [spec for spec in specs if spec.category != "orchestration"]
        return specs

    # LLM: 原生 Schema 只能在请求快照中选择 direct/deferred，不得重新扫描注册表。
    # 函数用途: 返回本轮直接工具与已由真实 tool_search 加载的 deferred 工具。
    def model_visible_specs(
        self,
        *,
        allowed_tools: list[str] | None = None,
        loaded_tool_names: set[str] | None = None,
        runtime_snapshot: ToolRuntimeSnapshot | None = None,
    ) -> list[ToolSpec]:
        """Return direct tools plus deferred tools loaded by a real tool_search result.

        An explicit ``allowed_tools`` profile is already a structured runtime
        selection (background/runner turns), so it remains fully visible.  Only
        the unrestricted foreground surface uses progressive disclosure.
        """

        specs = self.specs(
            allowed_tools=allowed_tools,
            include_orchestration=True,
            runtime_snapshot=runtime_snapshot,
        )
        if allowed_tools is not None or not self.catalog_deferred_categories:
            return specs
        deferred = set(self.catalog_deferred_categories)
        loaded = {str(item).strip() for item in loaded_tool_names or set() if str(item).strip()}
        return [
            spec
            for spec in specs
            if spec.category not in deferred or spec.name in loaded
        ]

    # LLM: deferred 搜索只能缩小快照，检索分数或模型文字都不能创建新权限。
    # 函数用途: 在当前请求快照的 deferred 类别中返回相关工具说明。
    def search_deferred_specs(
        self,
        query: str,
        *,
        limit: int = 8,
        allowed_tools: list[str] | None = None,
        runtime_snapshot: ToolRuntimeSnapshot | None = None,
    ) -> list[ToolSpec]:
        """Search only structurally deferred specs; searching never grants authority."""

        specs = self.specs(
            allowed_tools=allowed_tools,
            include_orchestration=True,
            runtime_snapshot=runtime_snapshot,
        )
        deferred = set(self.catalog_deferred_categories)
        searchable = [spec for spec in specs if spec.category in deferred]
        if not searchable:
            return []
        hits = self.retriever.search(str(query or ""), searchable, max(1, min(20, int(limit))))
        by_name = {spec.name: spec for spec in searchable}
        return [by_name[hit.name] for hit in hits if hit.name in by_name]

    # LLM: 文本协议目录与原生 Schema 共享同一快照，协议差异只影响渲染形式。
    # 函数用途: 把本轮工具快照渲染成有界文本目录。
    def render_catalog_section(
        self,
        *,
        allowed_tools: list[str] | None = None,
        tool_protocol: str = "text",
        runtime_snapshot: ToolRuntimeSnapshot | None = None,
    ) -> str:

        specs = self.specs(
            allowed_tools=allowed_tools,
            include_orchestration=True,
            runtime_snapshot=runtime_snapshot,
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

    # LLM: 推荐检索只能消费快照 specs，不能把进程级工具重新带回提示词。
    # 函数用途: 在本轮工具范围内查找与查询相关的工具说明。
    def find_relevant_specs(
        self,
        query: str,
        *,
        allowed_tools: list[str] | None = None,
        runtime_snapshot: ToolRuntimeSnapshot | None = None,
    ) -> list[ToolSpec]:

        specs = self.specs(
            allowed_tools=allowed_tools,
            include_orchestration=True,
            runtime_snapshot=runtime_snapshot,
        )
        hits = self.retriever.search(query, specs, self.retrieval_limit)
        if not hits:
            return []
        by_name = {spec.name: spec for spec in specs}
        return [by_name[hit.name] for hit in hits if hit.name in by_name]

    # LLM: Recommended Tools 只解释快照内的选择，绝不能成为旁路授权或可用性列表。
    # 函数用途: 为当前用户请求渲染少量相关且本轮可用的工具建议。
    def render_recommended_tools_section(
        self,
        query: str,
        *,
        allowed_tools: list[str] | None = None,
        runtime_snapshot: ToolRuntimeSnapshot | None = None,
    ) -> str:

        specs = self.model_visible_specs(
            allowed_tools=allowed_tools,
            runtime_snapshot=runtime_snapshot,
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
            blocks.append(
                f"{spec.render_recommended_entry(max_chars=self.tool_detail_max_chars)}\n推荐理由：{reason_text}"
            )
        return "# Recommended Tools\n" + "\n\n".join(blocks)

    def parse_tool_calls(self, text: str) -> list[dict[str, Any]]:
        return parse_registry_tool_calls(text, payload_limits=self.payload_limits)

    # LLM: 调用入口复用 run 快照；外部若误传不同 allowlist 就重建更窄快照，绝不信任不匹配状态。
    # 函数用途: 在统一授权、可用性复检和副作用门下执行一个结构化工具调用。
    def execute_call(
        self,
        payload: object,
        *,
        allowed_tools: list[str] | None = None,
        write_boundary: dict[str, object] | None = None,
        runtime_snapshot: ToolRuntimeSnapshot | None = None,
    ) -> ToolExecutionResult:
        expected_allowed = allowed_tool_set(allowed_tools)
        expected_snapshot_allowed = (
            frozenset(expected_allowed) if expected_allowed is not None else None
        )
        snapshot = runtime_snapshot
        if snapshot is None or snapshot.allowed_tools != expected_snapshot_allowed:
            snapshot = self.runtime_snapshot(allowed_tools=allowed_tools)
        return execute_registry_call(
            ExecuteRegistryCallParams(
                payload=payload,
                tools=self.tools,
                workspace_root=self.workspace_root,
                workspace_roots=self.workspace_roots,
                path_access_mode=self.path_access_mode,
                path_dangerous_roots=self.path_dangerous_roots,
                owner_scope_root=self.owner_scope_root,
                default_hidden_tool_names=self.default_hidden_tool_names,
                allowed_tools=allowed_tools,
                disabled_tools=list(self.disabled_tool_names),
                write_boundary=write_boundary,
                payload_limits=self.payload_limits,
                runtime_guard_policy=self.runtime_guard_policy,
                runtime_snapshot=snapshot,
                owner_type=self.owner_type,
            )
        )


# LLM: readiness check 发生在已授权候选集内；实现异常不得让工具进入 Schema 或中断整个 Agent。
# 函数用途: 安全调用工具的无副作用 availability 合同，并把异常归一为不可用。
def _safe_tool_availability(tool: BaseTool) -> ToolAvailability:
    try:
        return tool.availability()
    except Exception as exc:
        return ToolAvailability.unavailable(
            f"availability check failed: {type(exc).__name__}"
        )


def _connect_mcp_servers(registry: ToolRegistry, mcp_servers: dict[str, Any] | None) -> list[Any]:
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
        _tool_call_protocol(tool_protocol) + "\n\n" + content_transport_protocol + "\n\n"
        "# Tool Catalog\n" + "\n".join(entries)
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
        return ["- retrieval_only：工具目录精简隐藏，请依赖 Recommended Tools；缺少工具时用 tool_search 加载。"]
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
    specs: list[ToolSpec],
    deferred_categories: list[str],
) -> tuple[list[ToolSpec], list[ToolSpec]]:
    """渐进式披露:把 deferred category 的工具从主目录分出去(只在末尾留折叠清单)。"""
    if not deferred_categories:
        return specs, []
    deferred_set = set(deferred_categories)
    primary = [spec for spec in specs if spec.category not in deferred_set]
    deferred = [spec for spec in specs if spec.category in deferred_set]
    return primary, deferred


def _render_deferred_notice(specs: list[ToolSpec]) -> str:
    """折叠清单:只列被 defer 工具的名字，按 会话运行时 方式用 tool_search 再加载。"""
    if not specs:
        return ""
    names = ", ".join(sorted(spec.name for spec in specs))
    return (
        f"- ⊞ 另有 {len(specs)} 个工具已注册但未直接展开；需要时先用 tool_search 搜索并加载，"
        f"也可用 list_tools 查看完整清单：{names}"
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
