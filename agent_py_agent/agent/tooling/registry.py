# LLM: 这是 Agent 调工具的主入口，新增能力需保持返回格式稳定。
# 模块用途: 工具注册表，负责规格展示、相关工具推荐、调用解析和执行。

from __future__ import annotations

"""coordinates tool registration, prompt rendering, call parsing, authorization, and execution.

给人看的解释：
这个文件是工具系统的"前台服务台"。
它不亲自实现读文件或发 HTTP，而是登记这些工具、给模型渲染工具菜单、解析模型发来的工具调用，
再按授权和写入边界把请求分发给真正的工具。
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..log_analysis.capabilities import SECURITY_TOOL_NAMES
from .artifact import ReadArtifactTool
from .controlled_exec import ControlledExecTool
from .filesystem import (
    AppendFileTool,
    ListFilesTool,
    ReadFileTool,
    ReplaceInFileTool,
    SearchTextTool,
    WriteFileTool,
)
from .models import (
    BaseTool,
    HybridToolRetriever,
    KeywordToolSearchProvider,
    ToolExecutionResult,
    ToolSpec,
    VectorToolSearchProvider,
)
from .registry_execution import (
    ExecuteRegistryCallParams,
    allowed_tool_set,
    execute_registry_call,
    parse_registry_tool_calls,
    security_tools_visible,
)
from .shell import ShellTool
from .web import FetchUrlTool, HttpRequestTool

_allowed_tool_set = allowed_tool_set


# LLM: ToolRegistryParams 属于 工具系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: 工具注册表参数包，集中保存工作区边界和工具输出上限。
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
    shell_tool_timeout: int = 30
    expose_security_tools: bool = False


# LLM: ToolRegistry 属于 工具系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: ToolRegistry 数据模型，集中保存 工具系统 的结构化状态。
class ToolRegistry:

    # LLM: ToolRegistry.__init__ 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 初始化 ToolRegistry 的依赖、配置和运行期字段。
    def __init__(
        self,
        params: ToolRegistryParams,
    ):
        self.workspace_root = params.workspace_root.resolve()
        self.workspace_roots = params.workspace_roots or [self.workspace_root]
        self.tools: dict[str, BaseTool] = {}
        self.expose_security_tools = params.expose_security_tools
        self.security_tool_names = set(SECURITY_TOOL_NAMES)
        self.catalog_limit = params.catalog_limit
        self.retrieval_limit = params.retrieval_limit
        self.retriever = HybridToolRetriever(
            [
                KeywordToolSearchProvider(),
                VectorToolSearchProvider(enabled=params.vector_search_enabled),
            ]
        )

        workspace_roots = self.workspace_roots
        self.register(ListFilesTool(self.workspace_root, params.max_entries, workspace_roots))
        self.register(ReadFileTool(self.workspace_root, params.max_chars, workspace_roots))
        self.register(SearchTextTool(self.workspace_root, params.max_matches, workspace_roots))
        self.register(ReadArtifactTool(self.workspace_root))
        self.register(WriteFileTool(self.workspace_root, workspace_roots))
        self.register(AppendFileTool(self.workspace_root, workspace_roots))
        self.register(ReplaceInFileTool(self.workspace_root, workspace_roots))
        self.register(FetchUrlTool(max_chars=params.web_max_chars, timeout=params.http_timeout))
        self.register(HttpRequestTool(max_chars=params.web_max_chars, timeout=params.http_timeout))
        self.register(ShellTool(self.workspace_root, default_timeout=params.shell_tool_timeout))
        self.register(ControlledExecTool())
        from ..log_analysis.tools import (
            SecurityHuntIpTool,
            SecurityQueryTool,
            SecurityTraceCaseTool,
        )

        self.register(SecurityQueryTool(self.workspace_root))
        self.register(SecurityHuntIpTool(self.workspace_root))
        self.register(SecurityTraceCaseTool(self.workspace_root))

    # LLM: ToolRegistry.register 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 工具系统 中的 register 步骤，并保持调用方依赖的数据形状。
    def register(self, tool: BaseTool) -> None:

        self.tools[tool.spec.name] = tool

    # LLM: ToolRegistry.specs 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 工具系统 中的 specs 步骤，并保持调用方依赖的数据形状。
    def specs(
        self,
        *,
        allowed_tools: list[str] | None = None,
        granted_capabilities: list[str] | None = None,
        include_orchestration: bool = False,
    ) -> list[ToolSpec]:

        allowed = allowed_tool_set(allowed_tools)
        specs = [tool.spec for tool in self.tools.values()]
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

    # LLM: ToolRegistry.render_catalog_section 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 把 render_catalog_section 转成人或模型可读的展示文本。
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
        entries = [spec.render_catalog_entry() for spec in specs[: self.catalog_limit]]
        if not entries:
            entries = ["- none：当前执行上下文没有授权任何工具；缺能力时请上抛 capability_request。"]
        return (
            "# Tools\n"
            "当你需要看文件、改代码、查网页或测接口时，可以调用工具。\n"
            "工具调用格式必须严格写成：\n"
            "[TOOL_CALL]\n"
            '{"tool": "tool_name", "actual_parameter_name": "actual_value"}\n'
            "[/TOOL_CALL]\n"
            "必须把工具参数直接放在同一个 JSON 对象里；不要写 param_name 包裹参数。\n"
            "必须使用 Tool Catalog 里该工具自己的参数名；不要把 path 当作所有工具的默认参数。\n"
            "可以连续写多个 [TOOL_CALL] 块。拿到工具结果后，再输出最终答案，不要把工具调用块留在最后回复里。\n\n"
            "# Tool Catalog\n"
            + "\n".join(entries)
        )

    # LLM: ToolRegistry.find_relevant_specs 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 工具系统 中的 find_relevant_specs 步骤，并保持调用方依赖的数据形状。
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

    # LLM: ToolRegistry.render_recommended_tools_section 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 把 render_recommended_tools_section 转成人或模型可读的展示文本。
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
            blocks.append(f"{spec.render_detail_entry()}\n推荐理由：{reason_text}")
        return "# Recommended Tools\n" + "\n\n".join(blocks)

    # LLM: ToolRegistry.parse_tool_calls 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 解析 parse_tool_calls 数据结构。
    def parse_tool_calls(self, text: str) -> list[dict[str, Any]]:
        return parse_registry_tool_calls(text)

    # LLM: ToolRegistry.execute_call 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 完成 工具系统 中的 execute_call 步骤，并保持调用方依赖的数据形状。
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
            expose_security_tools=self.expose_security_tools,
            security_tool_names=self.security_tool_names,
            allowed_tools=allowed_tools,
            granted_capabilities=granted_capabilities,
            write_boundary=write_boundary,
            )
        )
