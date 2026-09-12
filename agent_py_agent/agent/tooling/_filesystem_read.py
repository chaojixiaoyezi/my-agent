

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..path_access_policy import PathAccessDecision, PathAccessPolicy
from ..path_recovery_hints import suggest_workspace_typo_target
from ..user_space.owner_quota import (
    OwnerQuotaChange,
    OwnerQuotaEnforcer,
    OwnerQuotaExceeded,
    OwnerQuotaUnavailable,
)
from ._filesystem_helpers import (
    _normalized_workspace_roots,
    _required_path,
)
from .filesystem_artifact_guard import (
    ToolOutputArtifactRedirectError,
    tool_output_artifact_typo_hint,
)
from .filesystem_read_file import execute_read_file
from .models import (
    BaseTool,
    ConcurrencyPolicy,
    EffectResolverPolicy,
    OutputPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)

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
    "path": "相对工作区的普通文本文件路径；已外置 tool-output 必须改用 read_artifact。",
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


def owner_quota_error_result(tool_name: str, exc: BaseException) -> ToolHandlerOutcome:
    if isinstance(exc, OwnerQuotaExceeded):
        projection = exc.projection
        return ToolHandlerOutcome(
            tool_name,
            False,
            str(exc),
            error_code="OWNER_DISK_QUOTA_EXCEEDED",
            result_envelope={
                "owner_quota": {
                    "used_bytes": projection.used_bytes,
                    "projected_bytes": projection.projected_bytes,
                    "max_bytes": projection.max_bytes,
                }
            },
        )
    return ToolHandlerOutcome(
        tool_name,
        False,
        "当前无法可靠读取 owner 配额或磁盘使用量；系统已拒绝本次写入。",
        error_code="OWNER_QUOTA_UNAVAILABLE",
    )


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
        # LLM: 只有宿主从结构化 write_boundary 下发的"墙外已授权根"才允许穿过 owner 墙；
        #   这个字段默认空，普通调用方（含测试里直接改 workspace_roots 的场景）拿不到它，
        #   因此单纯篡改 workspace_roots 不会放宽 owner 隔离。
        # 人类: 这是 owner 墙的逃生口白名单，只能由 registry 逐次调用组装，不要从模型参数或
        #   workspace_roots 反推。
        self.granted_external_roots: tuple[Path, ...] = ()
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
        self.owner_quota = (
            OwnerQuotaEnforcer(
                access.owner_scope_root,
                max_bytes=access.owner_quota_max_bytes,
                policy_available=access.owner_quota_policy_available,
            )
            if str(access.owner_scope_root or "").strip() and access.owner_quota_max_bytes > 0
            else None
        )

    def quota_changes(self, changes: list[OwnerQuotaChange]):
        if self.owner_quota is None:
            from contextlib import nullcontext

            return nullcontext(None)
        return self.owner_quota.reserve(changes)

    # LLM: PathAccessPolicy is the hard owner wall. Per-turn workspace roots may narrow paths but
    # never authorize a path outside owner home; only an admin Full Access registry has no scope.
    # 函数用途: 把相对路径落到当前工具目录，并先经过 owner/full-access 统一权限裁决。
    def resolve_path(self, raw_path: str | Path) -> Path:

        raw_text = _required_path(raw_path)
        candidate = Path(raw_text).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        try:
            candidate = candidate.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise ValueError("路径解析失败，请检查路径是否有效。") from exc
        decision = self.check_path_access(candidate)
        if decision.allowed:
            return candidate
        hint = _workspace_typo_error(raw_text, self.workspace_root, self.workspace_roots)
        if hint:
            if tool_output_artifact_typo_hint(
                raw_text,
                self.workspace_root,
                suggest_workspace_typo_target(raw_text, self.workspace_roots),
            ):
                raise ToolOutputArtifactRedirectError(hint)
            raise ValueError(hint)
        raise PathAccessError(decision.message or "路径访问被拒绝。")

    # LLM: owner 墙的逃生口必须和中央路径门 (`contracts/gates/path_url_command._path_finding`)
    #   一致：只有目标确实落在**宿主逐次下发的墙外已授权根**(granted_external_roots) 里才放行，
    #   危险目录、凭据文件名这些与 owner 无关的硬拦继续生效。少了这一步，子代理在用户显式声明的
    #   项目目录里会先被 handler 自己的 owner 墙判死，中央门和写边界门放行也没用
    #   （2026-09-11 真机：write_file 报 WRITE_FORBIDDEN、list_files 报 TOOL_INVALID_ARGUMENTS，
    #   文件始终不落盘）。
    # 函数用途: 统一 owner 墙裁决，并在宿主已授权的墙外工作根内按"无 owner 墙"策略复核。
    def check_path_access(self, resolved: Path) -> PathAccessDecision:
        decision = self.path_access_policy.check(resolved)
        if decision.allowed or decision.code != "PATH_OWNER_SCOPE_BLOCKED":
            return decision
        if not any(_path_is_under(resolved, root) for root in self.granted_external_roots):
            return decision
        return PathAccessPolicy.from_values(
            mode=self.path_access_policy.mode,
            dangerous_roots=self.path_access_policy.dangerous_roots,
        ).check(resolved)

    # LLM: owner-scoped 的写操作只能落在当前结构化 workspace_roots；registry 会把
    #   本轮明确授权的外部输出根临时加入该列表。读操作仍走 resolve_path 的既有策略。
    # 人类: 这是文件写工具统一硬门，防止模型用绝对路径写进全局 service-cwd。
    def resolve_write_path(self, raw_path: str | Path) -> Path:
        """解析写路径，并在多用户模式下强制命中本轮已授权工作区。"""
        try:
            candidate = self.resolve_path(raw_path)
        except PathAccessError as exc:
            # LLM: GUIDE-01(2026-08-15 轻量运行时 对照真机): 拦截消息必须带具体可用路径——
            # D1 任务被 PATH_OWNER_SCOPE_BLOCKED 拦后模型首次假完成(声称写了但无产物),
            # 同模型在无限制的 轻量运行时 上直接成功; 差异=提示只说"改用工作区路径"没给路径。
            # 现在把 workspace_roots + owner home 直接列出, 模型无需探索即知可写位置。
            allowed = list(self.workspace_roots)
            if self.path_access_policy.owner_scope_root:
                allowed.append(self.path_access_policy.owner_scope_root)
            raise WriteScopeError(
                f"{exc} 可用的写入位置: {'、'.join(str(p) for p in allowed) or '（无）'}"
            ) from exc
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
    owner_quota_max_bytes: int = 0
    owner_quota_policy_available: bool = True


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
    owner_quota_max_bytes: int = 0,
    owner_quota_policy_available: bool = True,
) -> FileSystemAccessOptions:
    roots = list(path_dangerous_roots) if isinstance(path_dangerous_roots, list) else None
    return FileSystemAccessOptions(
        path_access_mode=str(path_access_mode or "normal"),
        path_dangerous_roots=roots,
        owner_scope_root=str(owner_scope_root or ""),
        protected_persona_root=str(protected_persona_root or ""),
        owner_quota_max_bytes=max(0, int(owner_quota_max_bytes or 0)),
        owner_quota_policy_available=bool(owner_quota_policy_available),
    )


# LLM: This surface reads user/workspace text only; registered tool-output wrappers belong to the
# logical read_artifact surface so path rebasing cannot become a second recovery protocol.
# 类用途: 提供普通文本文件读取能力，不直接读取底座保存的工具输出包装文件。
class ReadFileTool(FileSystemTool):

    model_spec = ToolModelSpec(
        name="read_file",
        description="读取普通文本文件内容；已外置 tool-output 的包装文件只能用 read_artifact，不能用本工具。默认一次返回整个文件——理解或复刻一个源码文件时直接读全文，不要自己分段反复读同一个文件；只有文件极大（几十万字符以上）或只需确认某几行时才用 start_line/end_line/max_chars。可直接读任意绝对路径，包括 workspace 外、用户在任务里指定的输入目录/文件，无需 shell 或额外授权——不要为读取输入文件提 capability_request。",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": _READ_FILE_PARAMETER_DETAILS["path"]},
                "start_line": {
                    "type": "integer",
                    "minimum": 1,
                    "description": _READ_FILE_PARAMETER_DETAILS["start_line"],
                },
                "end_line": {
                    "type": "integer",
                    "minimum": 1,
                    "description": _READ_FILE_PARAMETER_DETAILS["end_line"],
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": _READ_FILE_PARAMETER_DETAILS["offset"],
                },
                "max_chars": {
                    "type": "integer",
                    "minimum": 1,
                    "description": _READ_FILE_PARAMETER_DETAILS["max_chars"],
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="filesystem",
            use_cases=tuple(_READ_FILE_USE_CASES),
            avoid_when=("只想知道关键字在哪些文件出现过时，先用 search_text 更省",),
            keywords=("读文件", "查看文件", "代码", "配置", "文档", "cat", "open file"),
            examples=tuple(_READ_FILE_EXAMPLES),
        ),
    )
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("read_only"),
        concurrency_policy=ConcurrencyPolicy("parallel_safe"),
        resource_scopes=ResourceScopePolicy(parameter_names=("path",)),
        output_policy=OutputPolicy(redaction="source_code"),
        promotes_task=True,
    )

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
    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        return execute_read_file(self, params, self.max_chars)
