
# LLM: 路径解析必须持续限制在工作区根内，避免读越界。
# 模块用途: 工作区内文件列举、读取和文本搜索工具实现。

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..path_access_policy import PathAccessPolicy
from ..path_recovery_hints import suggest_workspace_typo_target
from ._filesystem_helpers import (
    _normalized_workspace_roots,
    _required_path,
)
from .filesystem_artifact_guard import tool_output_artifact_typo_hint
from .filesystem_read_file import execute_read_file
from .models import BaseTool, ToolExecutionResult, ToolSpec

_COMMON_FILE_DISCOVERY_IGNORES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".venv",
        "venv",
    }
)


# LLM: FileSystemTool 属于 工具系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: FileSystemTool 数据模型，集中保存 工具系统 的结构化状态。
class FileSystemTool(BaseTool):

    # LLM: FileSystemTool.__init__ 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 初始化 FileSystemTool 的依赖、配置和运行期字段。
    def __init__(
        self,
        workspace_root: Path,
        workspace_roots: list[Path] | None = None,
        access_options: FileSystemAccessOptions | None = None,
    ):
        access = access_options or FileSystemAccessOptions()
        self.workspace_root = workspace_root.resolve()
        self.workspace_roots = _normalized_workspace_roots(self.workspace_root, workspace_roots)
        self.path_access_policy = PathAccessPolicy.from_values(
            mode=access.path_access_mode,
            dangerous_roots=access.path_dangerous_roots,
        )

    # LLM: FileSystemTool.resolve_path 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 解析 resolve_path 并确认结果仍在允许边界内。
    def resolve_path(self, raw_path: str | Path) -> Path:

        raw_text = _required_path(raw_path)
        candidate = Path(raw_text)
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        try:
            candidate = candidate.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise ValueError("路径解析失败，请检查路径是否有效。") from exc
        decision = self.path_access_policy.check(candidate)
        if decision.allowed:
            return candidate
        hint = _workspace_typo_error(raw_text, self.workspace_root, self.workspace_roots)
        if hint:
            raise ValueError(hint)
        raise ValueError(decision.message or "路径访问被拒绝。")

    # LLM: FileSystemTool.display_path 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 把内部路径转换成调用方可读的展示路径。
    def display_path(self, path: Path) -> str:

        for root in self.workspace_roots:
            if root == self.workspace_root:
                continue
            try:
                path.relative_to(root)
            except ValueError:
                continue
            return str(path).replace("\\", "/")
        try:
            return str(path.relative_to(self.workspace_root)).replace("\\", "/")
        except ValueError:
            return str(path).replace("\\", "/")


# LLM: _workspace_typo_error gives models a precise retry path for near-miss workspace paths.
# 函数用途: 当读/列文件路径只是工作区前缀拼错时，返回机器可读的 suggested_target 提示。
def _workspace_typo_error(raw_path: str, workspace_root: Path, workspace_roots: list[Path]) -> str:
    suggested = suggest_workspace_typo_target(raw_path, workspace_roots)
    if not suggested:
        return ""
    artifact_hint = tool_output_artifact_typo_hint(raw_path, workspace_root, suggested)
    if artifact_hint:
        return artifact_hint
    return (
        "路径疑似拼写错误，已拒绝访问。"
        f" suspected_path_typo=true target={raw_path} workspace_root={workspace_root}"
        f" suggested_target={suggested}。"
        " 这是路径拼写错误，不是权限缺口；请使用 suggested_target 重试。"
    )


@dataclass(frozen=True)
class FileSystemAccessOptions:
    path_access_mode: str = "normal"
    path_dangerous_roots: list[str] | None = None


def filesystem_access_options(
    *,
    path_access_mode: str = "normal",
    path_dangerous_roots: list[str] | None = None,
) -> FileSystemAccessOptions:
    roots = list(path_dangerous_roots) if isinstance(path_dangerous_roots, list) else None
    return FileSystemAccessOptions(path_access_mode=str(path_access_mode or "normal"), path_dangerous_roots=roots)


# LLM: ReadFileTool 属于 工具系统 的稳定结构；调整字段或继承关系前先核对序列化、导入和测试。
# 类用途: ReadFileTool 数据模型，集中保存 工具系统 的结构化状态。
class ReadFileTool(FileSystemTool):

    # LLM: ReadFileTool.__init__ 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 初始化 ReadFileTool 的依赖、配置和运行期字段。
    def __init__(
        self,
        workspace_root: Path,
        max_chars: int,
        workspace_roots: list[Path] | None = None,
        access_options: FileSystemAccessOptions | None = None,
    ):
        super().__init__(
            workspace_root,
            workspace_roots,
            access_options,
        )
        self.max_chars = max_chars
        self.spec = ToolSpec(
            name="read_file",
            category="filesystem",
            effect="read_only",
            description="读取文本文件内容；普通文件、大工具输出路径和历史产物路径都优先用这个入口。",
            use_cases=[
                "查看某个 Python 文件、配置文件或 Markdown 文档",
                "定位报错后，按行阅读相关代码",
                "读取工具返回的大输出保存路径或 tool-output artifact 包装路径",
            ],
            avoid_when=[
                "只想知道关键字在哪些文件出现过时，先用 search_text 更省",
            ],
            keywords=["读文件", "查看文件", "代码", "配置", "文档", "cat", "open file"],
            parameters={
                "path": "要读取的文件路径",
                "start_line": "起始行号，可选",
                "end_line": "结束行号，可选",
            },
            parameter_details={
                "path": "相对工作区的文本文件路径，或系统返回的安全大输出路径；必须是文件而不是目录。",
                "start_line": "从第几行开始读，默认从第 1 行开始。",
                "end_line": "读到第几行结束，包含该行；不传时默认读到文件结尾。",
            },
            examples=[
                '{"tool": "read_file", "path": "agent_py_agent/agent/core.py"}',
                '{"tool": "read_file", "path": "agent_py_agent/agent/core.py", "start_line": 1, "end_line": 120}',
            ],
        )

    # LLM: ReadFileTool.execute 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
    # 函数用途: 执行 ReadFileTool 的主流程并返回 ToolExecutionResult。
    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        return execute_read_file(self, params, self.max_chars)
