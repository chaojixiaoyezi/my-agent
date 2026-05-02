from __future__ import annotations

"""LLM: coordinates tool registration, prompt rendering, call parsing, authorization, and execution.

给人看的解释：
这个文件是工具系统的“前台服务台”。
它不亲自实现读文件或发 HTTP，而是登记这些工具、给模型渲染工具菜单、解析模型发来的工具调用，
再按授权和写入边界把请求分发给真正的工具。
"""

import json
from pathlib import Path
from typing import Any

from .filesystem import (
    AppendFileTool,
    ListFilesTool,
    ReadFileTool,
    ReplaceInFileTool,
    SearchTextTool,
    WriteFileTool,
)
from .shell import ShellTool
from ..log_analysis.capabilities import SECURITY_TOOL_NAMES, has_security_tool_capability
from .models import (
    BaseTool,
    HybridToolRetriever,
    KeywordToolSearchProvider,
    ToolExecutionResult,
    ToolSpec,
    VectorToolSearchProvider,
)
from .parser import parse_xmlish_tool_calls
from .web import FetchUrlTool, HttpRequestTool
from .write_boundary import validate_write_boundary

_MAX_TOOL_PAYLOAD_FIELDS = 64
_MAX_TOOL_FIELD_NAME_CHARS = 128
_MAX_TOOL_NAME_CHARS = 128
_MAX_PARSE_ERROR_RAW_CHARS = 1000
_MAX_EXCEPTION_MESSAGE_CHARS = 500


class ToolRegistry:
    """工具注册表。

    它负责四件事：
    - 统一登记有哪些工具
    - 生成“常驻目录”和“候选详情”两层工具说明
    - 解析模型发出的工具调用
    - 执行实际工具
    """

    def __init__(
        self,
        workspace_root: Path,
        *,
        max_chars: int,
        max_entries: int,
        max_matches: int,
        web_max_chars: int,
        http_timeout: int,
        catalog_limit: int,
        retrieval_limit: int,
        vector_search_enabled: bool,
        shell_tool_timeout: int = 30,
        expose_security_tools: bool = False,
    ):
        self.workspace_root = workspace_root.resolve()
        self.tools: dict[str, BaseTool] = {}
        self.expose_security_tools = expose_security_tools
        self.security_tool_names = set(SECURITY_TOOL_NAMES)
        self.catalog_limit = catalog_limit
        self.retrieval_limit = retrieval_limit
        self.retriever = HybridToolRetriever(
            [
                KeywordToolSearchProvider(),
                VectorToolSearchProvider(enabled=vector_search_enabled),
            ]
        )

        self.register(ListFilesTool(self.workspace_root, max_entries))
        self.register(ReadFileTool(self.workspace_root, max_chars))
        self.register(SearchTextTool(self.workspace_root, max_matches))
        self.register(WriteFileTool(self.workspace_root))
        self.register(AppendFileTool(self.workspace_root))
        self.register(ReplaceInFileTool(self.workspace_root))
        self.register(FetchUrlTool(max_chars=web_max_chars, timeout=http_timeout))
        self.register(HttpRequestTool(max_chars=web_max_chars, timeout=http_timeout))
        self.register(ShellTool(self.workspace_root, default_timeout=shell_tool_timeout))
        from ..log_analysis.tools import SecurityHuntIpTool, SecurityQueryTool, SecurityTraceCaseTool

        self.register(SecurityQueryTool(self.workspace_root))
        self.register(SecurityHuntIpTool(self.workspace_root))
        self.register(SecurityTraceCaseTool(self.workspace_root))

    def register(self, tool: BaseTool) -> None:
        """注册一个工具。"""

        self.tools[tool.spec.name] = tool

    def specs(
        self,
        *,
        allowed_tools: list[str] | None = None,
        granted_capabilities: list[str] | None = None,
        include_orchestration: bool = False,
    ) -> list[ToolSpec]:
        """按注册顺序返回所有工具说明。"""

        allowed = _allowed_tool_set(allowed_tools)
        specs = [tool.spec for tool in self.tools.values()]
        if not _security_tools_visible(
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
        """生成常驻 prompt 的工具目录。

        这里给的是中等详细度版本：
        模型能知道每个工具大概做什么、什么时候用、要传哪些关键参数，
        但不会把每个参数的长篇说明全塞进去。
        """

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
            '{"tool": "tool_name", "path": "example"}\n'
            "[/TOOL_CALL]\n"
            "可以连续写多个 [TOOL_CALL] 块。拿到工具结果后，再输出最终答案，不要把工具调用块留在最后回复里。\n\n"
            "# Tool Catalog\n"
            + "\n".join(entries)
        )

    def find_relevant_specs(
        self,
        query: str,
        *,
        allowed_tools: list[str] | None = None,
        granted_capabilities: list[str] | None = None,
    ) -> list[ToolSpec]:
        """根据当前任务挑出最相关的少量工具。"""

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
        """生成当前任务的候选工具详情区块。"""

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

    def parse_tool_calls(self, text: str) -> list[dict[str, Any]]:
        """从模型输出里提取工具调用块。"""

        calls: list[tuple[int, dict[str, Any]]] = []
        start_markers = ["[TOOL_CALL]", "[SUBAGENT_CALL]"]
        end_markers = ["[/TOOL_CALL]", "[/SUBAGENT_CALL]"]
        cursor = 0
        while True:
            starts = [
                (pos, marker)
                for marker in start_markers
                for pos in [text.find(marker, cursor)]
                if pos != -1
            ]
            if not starts:
                break
            start, marker_start = min(starts, key=lambda item: item[0])
            ends = [
                (pos, marker)
                for marker in end_markers
                for pos in [text.find(marker, start + len(marker_start))]
                if pos != -1
            ]
            if not ends:
                break
            end, marker_end = min(ends, key=lambda item: item[0])
            raw = text[start + len(marker_start) : end].strip().strip("`")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                calls.append(
                    (
                        start,
                        _parse_error_payload(f"工具调用 JSON 解析失败: {exc}", raw),
                    )
                )
            else:
                if not isinstance(payload, dict):
                    calls.append((start, _parse_error_payload("工具调用必须是 JSON 对象", raw)))
                else:
                    calls.append((start, payload))
            cursor = end + len(marker_end)
        calls.extend(parse_xmlish_tool_calls(text))
        calls.sort(key=lambda item: item[0])
        return [payload for _, payload in calls]

    def execute_call(
        self,
        payload: object,
        *,
        allowed_tools: list[str] | None = None,
        granted_capabilities: list[str] | None = None,
        write_boundary: dict[str, object] | None = None,
    ) -> ToolExecutionResult:
        """执行单个工具调用。"""

        normalized_payload, payload_error = _normalize_tool_payload(payload)
        if payload_error:
            return ToolExecutionResult("unknown", False, payload_error)
        assert normalized_payload is not None
        payload = normalized_payload

        if payload.get("tool") == "__parse_error__":
            return ToolExecutionResult("__parse_error__", False, str(payload.get("error") or "工具调用解析失败"))

        try:
            tool_name = _tool_name(payload.get("tool"))
        except ValueError as exc:
            return ToolExecutionResult("unknown", False, str(exc))

        allowed = _allowed_tool_set(allowed_tools)
        if allowed is not None and tool_name not in allowed:
            return ToolExecutionResult(tool_name, False, f"工具未授权: {tool_name}")

        if tool_name in self.security_tool_names and not _security_tool_call_authorized(
            tool_name,
            self.expose_security_tools,
            allowed=allowed,
            granted_capabilities=granted_capabilities,
        ):
            return ToolExecutionResult(tool_name, False, f"tool not authorized: {tool_name}")

        tool = self.tools.get(tool_name)
        if tool is None:
            return ToolExecutionResult(tool_name, False, f"未知工具: {tool_name}")

        params = {key: value for key, value in payload.items() if key != "tool"}
        boundary_error = validate_write_boundary(
            tool_name,
            params,
            workspace_root=self.workspace_root,
            write_boundary=write_boundary,
        )
        if boundary_error:
            return ToolExecutionResult(tool_name, False, boundary_error)

        try:
            return tool.execute(params)
        except Exception as exc:
            return ToolExecutionResult(tool_name, False, _format_tool_exception(exc))



def _allowed_tool_set(allowed_tools: list[str] | None) -> set[str] | None:
    """把工具 allowlist 规范成集合；None 表示不限制。"""

    if allowed_tools is None:
        return None
    return {str(item) for item in allowed_tools if str(item).strip()}


def _security_tools_visible(
    expose_security_tools: bool,
    *,
    allowed: set[str] | None,
    granted_capabilities: list[str] | None,
) -> bool:
    return bool(
        expose_security_tools
        or (allowed is not None and bool(allowed.intersection(SECURITY_TOOL_NAMES)))
        or has_security_tool_capability(granted_capabilities)
    )


def _security_tool_call_authorized(
    tool_name: str,
    expose_security_tools: bool,
    *,
    allowed: set[str] | None,
    granted_capabilities: list[str] | None,
) -> bool:
    return bool(
        expose_security_tools
        or (allowed is not None and tool_name in allowed)
        or has_security_tool_capability(granted_capabilities)
    )


def _parse_error_payload(error: str, raw: str) -> dict[str, str]:
    return {
        "tool": "__parse_error__",
        "error": error,
        "raw": _truncate(raw, _MAX_PARSE_ERROR_RAW_CHARS),
    }


def _normalize_tool_payload(payload: object) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(payload, dict):
        return None, "工具调用必须是 JSON 对象"
    if len(payload) > _MAX_TOOL_PAYLOAD_FIELDS:
        return None, f"工具调用字段过多，最多 {_MAX_TOOL_PAYLOAD_FIELDS} 个字段"

    normalized: dict[str, Any] = {}
    for key, value in payload.items():
        key_text = str(key)
        if not key_text:
            return None, "工具调用包含空参数名"
        if len(key_text) > _MAX_TOOL_FIELD_NAME_CHARS:
            return None, f"工具调用参数名过长，最多 {_MAX_TOOL_FIELD_NAME_CHARS} 个字符"
        if any(ord(char) < 32 for char in key_text):
            return None, "工具调用参数名包含不支持的控制字符"
        normalized[key_text] = value
    return normalized, ""


def _tool_name(value: object) -> str:
    if value is None:
        raise ValueError("工具调用缺少 tool 字段")
    if not isinstance(value, (str, int, float, bool)):
        raise ValueError("tool 字段必须是字符串工具名")
    name = str(value).strip()
    if not name:
        raise ValueError("工具调用缺少 tool 字段")
    if len(name) > _MAX_TOOL_NAME_CHARS:
        raise ValueError(f"tool 字段过长，最多 {_MAX_TOOL_NAME_CHARS} 个字符")
    if any(ord(char) < 32 for char in name):
        raise ValueError("tool 字段包含不支持的控制字符")
    return name


def _format_tool_exception(exc: Exception) -> str:
    if isinstance(exc, ValueError):
        message = _truncate(str(exc), _MAX_EXCEPTION_MESSAGE_CHARS)
        return f"工具执行失败: {message or exc.__class__.__name__}"
    if isinstance(exc, (OSError, UnicodeError)):
        return f"工具执行失败: {exc.__class__.__name__}；请检查路径、权限或文件编码。"
    return f"工具执行失败: {exc.__class__.__name__}；请检查参数后重试。"


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... 已截断"


WRITE_TOOL_NAMES = {"write_file", "append_file", "replace_in_file"}
