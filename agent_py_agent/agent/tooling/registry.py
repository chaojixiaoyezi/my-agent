
from __future__ import annotations

"""coordinates tool registration, prompt rendering, call parsing, authorization, and execution.

这个文件是工具系统的"前台服务台"。
它不亲自实现读文件或发 HTTP，而是登记这些工具、给模型渲染工具菜单、解析模型发来的工具调用，
再按授权和写入边界把请求分发给真正的工具。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..log_analysis.capabilities import SECURITY_TOOL_NAMES
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
from .registry_catalog import CatalogRenderConfig, render_catalog_entries
from .registry_execution import (
    ExecuteRegistryCallParams,
    allowed_tool_set,
    execute_registry_call,
    parse_registry_tool_calls,
    security_tools_visible,
)
from .registry_list_tools import ListToolsTool
from .registry_payload_normalize import ToolPayloadNormalizeLimits
from .registry_prompt import render_tool_catalog_section

_allowed_tool_set = allowed_tool_set
_DEFAULT_HIDDEN_TOOL_NAMES = frozenset({"controlled_exec"})


def _agent_config_int(key: str) -> int:
    return default_config_int(key)


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
    access_mode: str = "workspace-write"
    shell_tool_timeout: int = 30
    shell_tool_output_max_chars: int = 12_000
    catalog_mode: str = "compact"
    catalog_offset: int = 0
    catalog_categories: list[str] | None = None
    catalog_include_examples: bool = False
    catalog_entry_max_chars: int = 700
    catalog_show_truncated_notice: bool = True
    tool_detail_max_chars: int = 0
    tool_write_inline_max_chars: int = MAX_INLINE_WRITE_CONTENT_CHARS
    expose_security_tools: bool = False
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

class ToolRegistry:

    def __init__(
        self,
        params: ToolRegistryParams,
    ):
        self.workspace_root = params.workspace_root.resolve()
        self.workspace_roots = params.workspace_roots or [self.workspace_root]
        self.path_access_mode = params.path_access_mode
        self.path_dangerous_roots = params.path_dangerous_roots or []
        self.tools: dict[str, BaseTool] = {}
        self.default_hidden_tool_names = set(_DEFAULT_HIDDEN_TOOL_NAMES)
        self.disabled_tool_names = {str(item).strip() for item in params.disabled_tools if str(item).strip()}
        self.expose_security_tools = params.expose_security_tools
        self.security_tool_names = set(SECURITY_TOOL_NAMES)
        self.catalog_limit = params.catalog_limit
        self.catalog_mode = params.catalog_mode
        self.catalog_offset = max(0, params.catalog_offset)
        self.catalog_categories = [item.strip() for item in params.catalog_categories or [] if item.strip()]
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

    def register(self, tool: BaseTool) -> None:

        self.tools[tool.spec.name] = tool

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
        if not security_tools_visible(
            self.expose_security_tools,
            allowed=allowed,
            granted_capabilities=granted_capabilities,
        ):
            specs = [spec for spec in specs if spec.name not in self.security_tool_names]
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
    ) -> str:

        specs = self.specs(
            allowed_tools=allowed_tools,
            granted_capabilities=granted_capabilities,
            include_orchestration=True,
        )
        entries = render_catalog_entries(specs, self._catalog_render_config())
        return render_tool_catalog_section(
            entries,
            tool_content_transport_protocol(self._write_inline_max_chars()),
        )

    def _catalog_render_config(self) -> CatalogRenderConfig:
        return CatalogRenderConfig(
            mode=self.catalog_mode,
            offset=self.catalog_offset,
            limit=self.catalog_limit,
            categories=self.catalog_categories,
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
                expose_security_tools=self.expose_security_tools,
                security_tool_names=self.security_tool_names,
                default_hidden_tool_names=self.default_hidden_tool_names,
                allowed_tools=allowed_tools,
                granted_capabilities=granted_capabilities,
                disabled_tools=list(self.disabled_tool_names),
                write_boundary=write_boundary,
                payload_limits=self.payload_limits,
                runtime_guard_policy=self.runtime_guard_policy,
            )
        )
