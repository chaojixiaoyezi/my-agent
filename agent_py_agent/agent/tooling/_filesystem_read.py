

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

_READ_FILE_USE_CASES = [
    "查看某个 Python 文件、配置文件或 Markdown 文档",
    "定位报错后，按行阅读相关代码",
    "读取工具返回的大输出保存路径或 tool-output artifact 包装路径",
    "读取超大单行文本时，用 offset/max_chars 分段继续",
    "大文件已用 search_text 定位章节/锚点后，读取锚点附近源片段作为事实证据",
]
_READ_FILE_PARAMETERS = {
    "path": "要读取的文件路径",
    "start_line": "起始行号，可选",
    "end_line": "结束行号，可选",
    "offset": "字符偏移，可选；用于超大单行或按字符分块读取",
    "max_chars": "本次最多返回多少字符，可选；不会超过系统默认上限",
}
_READ_FILE_PARAMETER_DETAILS = {
    "path": "相对工作区的文本文件路径，或系统返回的安全大输出路径；必须是文件而不是目录。",
    "start_line": "从第几行开始读，默认从第 1 行开始。",
    "end_line": "读到第几行结束，包含该行；不传时默认读到文件结尾。",
    "offset": "当文件是一整行大文本或返回 next_offset 时，下一次传入 offset 继续读。",
    "max_chars": "字符窗口大小；适合大文件分块阅读、摘要、再继续。",
}
_READ_FILE_EXAMPLES = [
    '{"tool": "read_file", "path": "agent_py_agent/agent/core.py"}',
    '{"tool": "read_file", "path": "agent_py_agent/agent/core.py", "start_line": 1, "end_line": 120}',
    '{"tool": "read_file", "path": "large.log", "offset": 50000, "max_chars": 100000}',
    '{"tool": "read_file", "path": "field_journal.txt", "start_line": 2053, "end_line": 2058}',
]


class WriteScopeError(ValueError):
    """A mutating file target is outside this invocation's structured write roots."""


# LLM: Preserve the distinction between an invalid path argument and a policy denial so mutating tools fail closed with WRITE_FORBIDDEN.
# 类用途: 标记路径已成功解析、但被统一访问策略拒绝，供写工具转换成明确的权限错误。
class PathAccessError(ValueError):
    """A resolved path was rejected by PathAccessPolicy."""


class FileSystemTool(BaseTool):

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
            owner_scope_root=access.owner_scope_root,
        )
        self.protected_persona_root = (
            Path(access.protected_persona_root).expanduser().resolve(strict=False)
            if str(access.protected_persona_root or "").strip()
            else None
        )

    def resolve_path(self, raw_path: str | Path) -> Path:

        raw_text = _required_path(raw_path)
        candidate = Path(raw_text).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        try:
            candidate = candidate.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise ValueError("路径解析失败，请检查路径是否有效。") from exc
        decision = self.path_access_policy.check(candidate)
        if decision.allowed:
            return candidate
        # owner 默认只见自己 home + shared；但 capability/delivery contract 可以把
        # 一个 owner 外目录结构化加入本轮 workspace_roots。只放行这一种明确授权，
        # 凭据文件和其他 owner 边界的专用拒绝码仍不可绕过。
        if decision.code == "PATH_OWNER_SCOPE_BLOCKED" and any(
            _path_is_under(candidate, root) for root in self.workspace_roots
        ):
            return candidate
        hint = _workspace_typo_error(raw_text, self.workspace_root, self.workspace_roots)
        if hint:
            raise ValueError(hint)
        raise PathAccessError(decision.message or "路径访问被拒绝。")

    # LLM: owner-scoped 的写操作只能落在当前结构化 workspace_roots；registry 会把
    #   本轮明确授权的外部输出根临时加入该列表。读操作仍走 resolve_path 的既有策略。
    # 人类: 这是文件写工具统一硬门，防止模型用绝对路径写进全局 service-cwd。
    def resolve_write_path(self, raw_path: str | Path) -> Path:
        """解析写路径，并在多用户模式下强制命中本轮已授权工作区。"""
        try:
            candidate = self.resolve_path(raw_path)
        except PathAccessError as exc:
            raise WriteScopeError(str(exc)) from exc
        if self.path_access_policy.owner_scope_root is None:
            return candidate
        if any(_path_is_under(candidate, root) for root in self.workspace_roots):
            return candidate
        raise WriteScopeError(
            "写入被阻止: 多用户 owner 只能写当前任务工作区或结构化授权的输出目录。"
            f" target={candidate} workspace_roots={','.join(str(root) for root in self.workspace_roots)}"
        )

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
    owner_scope_root: str = ""  # 多用户隔离:per-user owner home;空=不隔离
    protected_persona_root: str = ""  # 当前 owner 人格根；不随 admin 文件访问豁免而消失


def _path_is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def filesystem_access_options(
    *,
    path_access_mode: str = "normal",
    path_dangerous_roots: list[str] | None = None,
    owner_scope_root: str = "",
    protected_persona_root: str = "",
) -> FileSystemAccessOptions:
    roots = list(path_dangerous_roots) if isinstance(path_dangerous_roots, list) else None
    return FileSystemAccessOptions(
        path_access_mode=str(path_access_mode or "normal"),
        path_dangerous_roots=roots,
        owner_scope_root=str(owner_scope_root or ""),
        protected_persona_root=str(protected_persona_root or ""),
    )


class ReadFileTool(FileSystemTool):

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
            description="读取文本文件内容；普通文件、大工具输出路径和历史产物路径都优先用这个入口。可直接读任意绝对路径，包括 workspace 外、用户在任务里指定的输入目录/文件，无需 shell 或额外授权——不要为读取输入文件提 capability_request。",
            use_cases=_READ_FILE_USE_CASES,
            avoid_when=[
                "只想知道关键字在哪些文件出现过时，先用 search_text 更省",
            ],
            keywords=["读文件", "查看文件", "代码", "配置", "文档", "cat", "open file"],
            parameters=_READ_FILE_PARAMETERS,
            parameter_details=_READ_FILE_PARAMETER_DETAILS,
            parameter_schema={
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
                "offset": {"type": "integer", "minimum": 0},
                "max_chars": {"type": "integer", "minimum": 1},
            },
            required_parameters=["path"],
            examples=_READ_FILE_EXAMPLES,
        )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        return execute_read_file(self, params, self.max_chars)
