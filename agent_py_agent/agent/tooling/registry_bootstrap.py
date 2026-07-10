
from __future__ import annotations

import json
from typing import Any

from ._filesystem_edit import EditFileTool
from ._filesystem_find import FindFilesTool
from ._filesystem_list import ListFilesTool
from ._filesystem_patch import ApplyPatchTool
from ._filesystem_read import ReadFileTool, filesystem_access_options
from ._filesystem_search import SearchTextTool
from ._filesystem_write import WriteFileTool, WriteFileToolOptions
from .artifact import ReadArtifactTool
from .browser_tools import browser_tools
from .capabilities_tool import ListCapabilitiesTool
from .controlled_exec import ControlledExecTool
from .models import (
    BaseTool,
    HybridToolRetriever,
    KeywordToolSearchProvider,
    ToolExecutionResult,
    ToolSpec,
    VectorToolSearchProvider,
)
from .process_tools import KillProcessTool, ListProcessesTool, ProcessStatusTool
from .shell import ShellTool, ShellToolOptions
from .vision_tools import AnalyzeImageTool, VisionModelConfig
from .web import WebFetchTool
from .web_search import WebSearchTool


class SubmitForAcceptanceTool(BaseTool):
    """A model-visible, task-agnostic way to submit the current work for acceptance."""

    spec = ToolSpec(
        name="submit_for_acceptance",
        category="delivery",
        description="通用交付提交工具：当你认为用户要求的工作已经完成时，用它请求系统验收。",
        use_cases=[
            "已经生成或更新完用户要求的交付物，需要系统检查是否合格",
            "已按返工单修复产物，需要重新提交验收",
        ],
        avoid_when=[
            "还在搜索、读取、分析、写草稿或没有写出目标产物时不要调用",
            "output_dir 里还混着明显的草稿、日志、子代理分报告或临时材料且没有最终索引时不要调用",
        ],
        keywords=["submit", "acceptance", "final", "done", "验收", "提交", "交付", "完成"],
        parameters={"note": "可选。简短说明你认为可以验收的内容；系统不会把 note 当作通过依据。"},
        parameter_schema={"note": {"type": "string"}},
        parameter_details={
            "note": (
                "可选字符串。只作为交接说明，真正验收只读取产物、工具记录、合同和运行事实。"
                "提交前请确保 output_dir 面向用户是清爽的：最终产物保留，中间材料挪到 work_dir 或由最终产物索引。"
            ),
        },
        examples=[
            '{"tool": "submit_for_acceptance", "note": "主要产物已经写入 output/，请系统验收。"}'
        ],
        effect="read_only",
        default_mode="real",
        requires_idempotency=False,
        requires_approval=False,
    )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        note = str(params.get("note") or "").strip()
        payload = {
            "submission": "acceptance_requested",
            "note": note,
            "message_zh": "已提交系统验收；是否通过以后续机器验收结果为准。",
        }
        return ToolExecutionResult(
            self.spec.name,
            True,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            result_envelope=payload,
        )


def build_tool_retriever(params: Any) -> HybridToolRetriever:
    return HybridToolRetriever(
        [
            KeywordToolSearchProvider(),
            VectorToolSearchProvider(enabled=params.vector_search_enabled),
        ]
    )


def register_base_tools(registry: Any, params: Any) -> None:
    _register_filesystem_tools(registry, params)
    _register_network_tools(registry, params)
    _register_vision_tools(registry, params)
    registry.register(SubmitForAcceptanceTool())
    registry.register(ListCapabilitiesTool())


def _register_vision_tools(registry: Any, params: Any) -> None:
    """注册看图工具 analyze_image(短板6 视觉理解)。

    视觉是可选加法:vision_config 为 None 时给一个空 VisionModelConfig,工具照常注册,
    但调用时返回 TOOL_UNAVAILABLE 带配置指引——没配视觉模型对现有流程零影响、不崩。
    """
    vision_config = getattr(params, "vision_config", None) or VisionModelConfig()
    registry.register(AnalyzeImageTool(vision_config))


def _register_filesystem_tools(registry: Any, params: Any) -> None:
    workspace_roots = registry.workspace_roots
    access_options = filesystem_access_options(
        path_access_mode=params.path_access_mode,
        path_dangerous_roots=params.path_dangerous_roots,
        owner_scope_root=params.owner_scope_root,
    )
    registry.register(ListFilesTool(registry.workspace_root, params.max_entries, workspace_roots, access_options))
    registry.register(FindFilesTool(registry.workspace_root, params.max_matches, workspace_roots, access_options))
    registry.register(ReadFileTool(registry.workspace_root, params.max_chars, workspace_roots, access_options))
    registry.register(SearchTextTool(registry.workspace_root, params.max_matches, workspace_roots, access_options))
    registry.register(
        ReadArtifactTool(
            getattr(params, "artifact_root", None) or registry.workspace_root,
            artifact_read_budget_window_seconds=params.artifact_read_budget_window_seconds,
            artifact_read_budget_max_chars=params.artifact_read_budget_max_chars,
            default_read_chars=params.artifact_default_read_chars,
        )
    )
    registry.register(
        WriteFileTool(
            registry.workspace_root,
            workspace_roots,
            WriteFileToolOptions(
                max_inline_content_chars=params.tool_write_inline_max_chars,
                access_options=access_options,
                runtime_fact_roots=_runtime_fact_roots(registry, params),
            ),
        )
    )
    registry.register(ApplyPatchTool(registry.workspace_root, workspace_roots, access_options))
    registry.register(EditFileTool(registry.workspace_root, workspace_roots, access_options))


def _register_network_tools(registry: Any, params: Any) -> None:
    registry.register(WebSearchTool(max_results=params.max_matches, timeout=params.http_timeout))
    registry.register(WebFetchTool(max_chars=params.web_max_chars, timeout=params.http_timeout))
    registry.register(
        ShellTool(
            registry.workspace_root,
            options=ShellToolOptions(
                workspace_roots=registry.workspace_roots,
                path_access_mode=params.path_access_mode,
                path_dangerous_roots=params.path_dangerous_roots,
                owner_scope_root=params.owner_scope_root,
                access_mode=params.access_mode,
                default_timeout=params.shell_tool_timeout,
                max_output_chars=params.shell_tool_output_max_chars,
            ),
        )
    )
    # 后台进程管理:管住 run_command(run_in_background=true) 起的后台进程
    # (注册表 + 列表/查状态/杀进程组),让模型不再只剩日志文件管不了进程。
    registry.register(ListProcessesTool())
    registry.register(ProcessStatusTool())
    registry.register(KillProcessTool())
    # 浏览器自动化:补 web_fetch 抓不到的 JS 渲染/SPA/需点击填表的动态页面
    # (惰性启动 headless Chromium,导航走 SSRF 防护,a11y 快照给无视觉模型用)。
    for browser_tool in browser_tools():
        registry.register(browser_tool)
    # controlled_exec is an internal tool used by capability grants.
    # flows, but ToolRegistry hides it from the default model-facing catalog.
    registry.register(ControlledExecTool())


def _runtime_fact_roots(registry: Any, params: Any) -> list[Any]:
    roots = [*(getattr(params, "runtime_fact_roots", None) or []), getattr(params, "artifact_root", None), registry.workspace_root]
    result: list[Any] = []
    seen: set[str] = set()
    for root in roots:
        if not root:
            continue
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        result.append(root)
    return result


__all__ = ["build_tool_retriever", "register_base_tools"]
