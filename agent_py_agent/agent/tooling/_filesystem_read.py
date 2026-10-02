

# LLM: 文件工具逐项复用唯一 PathAccessPolicy；宿主注入的语法反馈开关不扩大权限，联查三写入口和 owner 边界测试。
# 模块用途: 为内置文件工具装配配置、解析路径和检查读写边界，不拥有插件或任务状态。
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, ClassVar

from ..path_access_policy import (
    PATH_SCOPE_FULL,
    PATH_SCOPE_NORMAL,
    PATH_SCOPE_OWNER_WALL,
    PathAccessDecision,
    PathAccessPolicy,
    agent_home_root_for_owner,
)
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
# 无 owner 墙的 normal 模式仍拦危险目录与凭据文件（PathAccessPolicy.check）；读、列两个工具的说明共用这一句。
UNWALLED_NORMAL_EXCEPTIONS_NOTE = "危险目录和凭据文件除外（PATH_DANGEROUS_ROOT_BLOCKED / PATH_CREDENTIAL_FILE_BLOCKED）。"
_READ_FILE_DESCRIPTION_BASE = (
    "读取普通文本文件内容；已外置 tool-output 的包装文件只能用 read_artifact，不能用本工具。默认一次返回整个文件——"
    "理解或复刻一个源码文件时直接读全文，不要自己分段反复读同一个文件；只有文件极大（几十万字符以上）或只需确认某几行时"
    "才用 start_line/end_line/max_chars。"
)
_READ_FILE_FULL_NOTE = (
    "可直接读任意绝对路径，包括 workspace 外、用户在任务里指定的输入目录/文件，无需 shell 或额外授权——"
    "不要为读取输入文件提 capability_request。"
)
# 说明末句按本 run 的读取范围给出，三句与 PathAccessPolicy.check 的三种判定一一对应（full 句即原说明）。
_READ_FILE_SCOPE_NOTES = {
    PATH_SCOPE_OWNER_WALL: (
        "只能读当前用户自己的数据目录、shared 公共区和本任务明确授权的外部工作目录；其它路径会在授权阶段被拒，"
        "返回 PATH_OWNER_SCOPE_BLOCKED 等 PATH_*_BLOCKED 错误码，换写法重试同一位置不会成功——需要墙外的文件时如实说明缺哪个文件。"
    ),
    PATH_SCOPE_FULL: _READ_FILE_FULL_NOTE,
    PATH_SCOPE_NORMAL: _READ_FILE_FULL_NOTE + UNWALLED_NORMAL_EXCEPTIONS_NOTE,
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


# LLM: 写工具把它统一转成 WRITE_FORBIDDEN；只有 access_code 非空（目前仅 H3 宿主托管文件拒写：PATH_HOST_CONFIG_WRITE_BLOCKED、
#   PATH_HOST_STATE_WRITE_BLOCKED）时改报这个码，让模型和宿主都能按结构化码区分。access_code 只由 resolve_write_path 从
#   PathAccessDecision.code 写入。
# 类用途: 表示写目标不在本次结构化写入范围内，或是宿主托管文件。
class WriteScopeError(ValueError):
    """A mutating file target is outside this invocation's structured write roots."""

    # 函数用途: 保存拒绝原因和可选的结构化拒绝码（空表示沿用 WRITE_FORBIDDEN）。
    def __init__(self, message: str, access_code: str = "") -> None:
        super().__init__(message)
        self.access_code = str(access_code or "").strip()


# LLM: Preserve the distinction between an invalid path argument and a policy denial so mutating tools fail closed with WRITE_FORBIDDEN.
# 类用途: 标记路径已成功解析、但被统一访问策略拒绝，供写工具转换成明确的权限错误。
# LLM: 路径被拒时携带 PathAccessDecision.code，供上层区分权限拒绝与参数错误；消息文字不参与判断。
# 类用途: 表示"路径解析成功但被访问策略拒绝"，并把策略给出的稳定错误码带上去。
class PathAccessError(ValueError):
    """A resolved path was rejected by PathAccessPolicy."""

    # LLM: access_code 只由 resolve_path 从 PathAccessDecision.code 写入；读取方不得从消息文字反推。
    # 函数用途: 保存策略给出的错误码（可为空，空表示策略没给码）。
    def __init__(self, message: str, access_code: str = "") -> None:
        super().__init__(message)
        self.access_code = str(access_code or "").strip()


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


# LLM: 工作区和已授权外部根由 registry 注入；路径决定共用 PathAccessPolicy，语法观察只读宿主开关，不从模型参数生成权限。
# 类用途: 为内置读写工具提供路径解析、权限核对、诊断配置和输出路径展示。
class FileSystemTool(BaseTool):

    # 说明前半段与三种读取范围的末句；没有声明的文件工具说明不随范围变化。
    path_scope_description_base: ClassVar[str] = ""
    path_scope_notes: ClassVar[Mapping[str, str] | None] = None

    # LLM: 这里只固定宿主配置，不读取文件或创建每调用诊断状态；联查 registry 装配和关闭零反馈测试。
    # 函数用途: 初始化文件访问策略、配额和默认关闭的语法反馈开关。
    def __init__(
        self,
        workspace_root: Path,
        workspace_roots: list[Path] | None = None,
        access_options: FileSystemAccessOptions | None = None,
    ):
        access = access_options or FileSystemAccessOptions()
        self.enable_file_syntax_diagnostics = access.enable_file_syntax_diagnostics is True
        self.workspace_root = workspace_root.resolve()
        self.workspace_roots = _normalized_workspace_roots(self.workspace_root, workspace_roots)
        # LLM: 只有宿主从结构化 write_boundary 下发的"墙外已授权根"才允许穿过 owner 墙；
        #   这个字段默认空，普通调用方（含测试里直接改 workspace_roots 的场景）拿不到它，
        #   因此单纯篡改 workspace_roots 不会放宽 owner 隔离。
        # 人类: 这是 owner 墙的逃生口白名单，只能由 registry 逐次调用组装，不要从模型参数或
        #   workspace_roots 反推。
        self.granted_external_roots: tuple[Path, ...] = ()
        # 数据根取宿主解析出的 owner home（H3 二审：不能只看环境变量 MY_AGENT_HOME）。
        self.path_access_policy = PathAccessPolicy.from_values(
            mode=access.path_access_mode,
            dangerous_roots=access.path_dangerous_roots,
            owner_scope_root=access.owner_scope_root,
            agent_home_root=agent_home_root_for_owner(access.owner_scope_root or access.protected_persona_root),
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

    # LLM: 由 ToolRegistry 冻结快照时按本 run 的读取范围调用；只换说明末句，input_schema 原样，replace 会复核 schema_hash。
    #   regime 只来自 path_access_policy.path_scope_regime 的结构化结果，不读模型参数或自然语言。
    # 函数用途: 给出与本 run 路径门一致的工具说明；没有声明范围说明的工具原样返回自己的说明。
    def model_spec_for_path_scope(self, regime: str) -> ToolModelSpec:
        if not self.path_scope_notes:
            return self.model_spec
        return replace(self.model_spec, description=self.path_scope_description_base + self.path_scope_notes[regime])

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
        self._raise_for_typo_hint(raw_text, candidate)
        raise PathAccessError(decision.message or "路径访问被拒绝。", decision.code)

    # LLM: 路径已经落在某个已知工作区根下时，它本身不可能写错前缀（根名就是在这条路径里匹配到的），
    #   所谓的"拼写提示"只会给出同一个路径、诱导调用方原地重试并盖住真实的权限拒绝（2026-09-28 集成者裁定）。
    #   只有不在任何已知根下、且建议目标确实不同于原路径时，才保留拼写提示。
    #   实测（2026-09-28）：本方法的拼写分支只在**三者同时成立**时才真正抛出——
    #   ① check_path_access 已拒绝该路径（在根内、或文件不存在时策略放行，都到不了这里）；
    #   ② 该路径不在任何 workspace_root 之下；
    #   ③ suggest_workspace_typo_target 给出的建议非空且不同于原路径。
    #   所以它覆盖的是"把工作区根的**位置**写错"（根名出现在路径中段），不是"文件名拼错"。
    # 函数用途: 命中"真拼写"场景时抛出对应异常；根内路径与拿不到建议的路径直接返回，交给权限拒绝处理。
    def _raise_for_typo_hint(self, raw_text: str, candidate: Path) -> None:
        if any(_path_is_under(candidate, root) for root in self.workspace_roots):
            return
        hint = _workspace_typo_error(raw_text, self.workspace_root, self.workspace_roots)
        if not hint:
            return
        if tool_output_artifact_typo_hint(
            raw_text,
            self.workspace_root,
            suggest_workspace_typo_target(raw_text, self.workspace_roots),
        ):
            raise ToolOutputArtifactRedirectError(hint)
        raise ValueError(hint)

    # LLM: 只消费 registry 注入的墙外授权，裁决复用原路径策略；workspace_roots 不能自行产生权限，联测 owner/exact 读取边界。
    # 函数用途: 使用核心与插件共用的路径裁决，检查目标是否获准读取。
    def check_path_access(self, resolved: Path) -> PathAccessDecision:
        return self.path_access_policy.check_with_external_roots(resolved, self.granted_external_roots)

    # LLM: owner-scoped 的写操作只能落在当前结构化 workspace_roots；registry 会把
    #   本轮明确授权的外部输出根临时加入该列表。读操作仍走 resolve_path 的既有策略。
    #   H3：任何模式下宿主托管文件（配置、运行状态）都拒写（拒写码随 WriteScopeError.access_code 上报），在墙外授权复核之后、
    #   工作区范围之前判定，所以 Full Access 管理员与声明了自家根的隔离 owner 同样被拒。
    # 人类: 这是文件写工具统一硬门，防止模型用绝对路径写进全局 service-cwd，也防止直接改宿主配置和宿主账本。
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
        host_decision = self.path_access_policy.host_write_decision(candidate)
        if not host_decision.allowed:
            raise WriteScopeError(host_decision.message, host_decision.code)
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


# LLM: 配置仅由宿主装配；语法观察开关不属于可由模型设置的权限参数，默认关闭须与 AgentConfig 一致。
# 类用途: 传递统一文件访问约束和可选的本地语法反馈配置。
@dataclass(frozen=True)
class FileSystemAccessOptions:
    path_access_mode: str = "normal"
    path_dangerous_roots: list[str] | None = None
    owner_scope_root: str = ""  # 多用户隔离:per-user owner home;空=不隔离
    protected_persona_root: str = ""  # 当前 owner 人格根；不随 admin 文件访问豁免而消失
    owner_quota_max_bytes: int = 0
    owner_quota_policy_available: bool = True
    enable_file_syntax_diagnostics: bool = False


def _path_is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# LLM: 归一化宿主文件配置；诊断只认布尔 True，不把字符串或自然语言当作启用授权。
# 函数用途: 构建三种文件修改工具共用的配置，不读写文件。
def filesystem_access_options(
    *,
    path_access_mode: str = "normal",
    path_dangerous_roots: list[str] | None = None,
    owner_scope_root: str = "",
    protected_persona_root: str = "",
    owner_quota_max_bytes: int = 0,
    owner_quota_policy_available: bool = True,
    enable_file_syntax_diagnostics: bool = False,
) -> FileSystemAccessOptions:
    roots = list(path_dangerous_roots) if isinstance(path_dangerous_roots, list) else None
    return FileSystemAccessOptions(
        path_access_mode=str(path_access_mode or "normal"),
        path_dangerous_roots=roots,
        owner_scope_root=str(owner_scope_root or ""),
        protected_persona_root=str(protected_persona_root or ""),
        owner_quota_max_bytes=max(0, int(owner_quota_max_bytes or 0)),
        owner_quota_policy_available=bool(owner_quota_policy_available),
        enable_file_syntax_diagnostics=enable_file_syntax_diagnostics is True,
    )


# LLM: This surface reads user/workspace text only; registered tool-output wrappers belong to the
# logical read_artifact surface so path rebasing cannot become a second recovery protocol.
# 类用途: 提供普通文本文件读取能力，不直接读取底座保存的工具输出包装文件。
class ReadFileTool(FileSystemTool):

    path_scope_description_base = _READ_FILE_DESCRIPTION_BASE
    path_scope_notes = _READ_FILE_SCOPE_NOTES
    model_spec = ToolModelSpec(
        name="read_file",
        description=_READ_FILE_DESCRIPTION_BASE + _READ_FILE_FULL_NOTE,
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
