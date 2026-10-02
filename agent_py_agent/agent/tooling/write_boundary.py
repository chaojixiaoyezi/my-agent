
from __future__ import annotations

"""enforces subagent write scopes before mutating filesystem tools run.

这个文件是一道真正的写入门禁。
prompt 里说'只能写这个目录'只是提醒，真正防止越界写文件的是这里的路径检查。
它会检查允许目录、禁止目录、锁定文件，确保子代理不能改不该改的地方。
"""

# LLM: 本模块是文件写工具的结构化路径裁决层；路径规则按最具体条目优先，且不能从 prompt 推导权限。
# 模块用途: 在真正写文件前统一核对允许目录、禁止目录和锁定文件，避免子代理越界或被矛盾祖先规则误伤。

from pathlib import Path
from typing import Any

from ..path_access_policy import HOST_FILE_DENIAL_CODES, PathAccessPolicy, data_root_from_boundary

# LLM: Every file-mutation capability snapshot and write-scope consumer must
# reuse this canonical order/set; duplicating partial lists previously hid
# edit_file from child agents while the executor already authorized it.
# 常量用途: 统一文件写工具的展示顺序与成员集合，避免主代理、子代理和权限门各维护一份后漂移。
WRITE_TOOL_ORDER = ("write_file", "edit_file", "apply_patch")
WRITE_TOOL_NAMES = frozenset(WRITE_TOOL_ORDER)
# 写边界路径最多 4096 字符：超长路径拒绝，防异常输入。
_MAX_BOUNDARY_PATH_CHARS = 4096
_INTERNAL_OUTPUT_JSON_NAME = "output.json"
# 正在运行的 my-agent 安装目录（宿主结构化事实，见 agent_core/runtime_write_guards）。它不属于写入范围键：
# 只额外拒绝落在安装目录里的写入，不会把原本不限范围的调用变成“没有 allowed_write_roots 就全拒”。
RUNTIME_INSTALL_ROOTS_KEY = "runtime_install_roots"
_WRITE_SCOPE_BOUNDARY_KEYS = frozenset(
    {
        "allowed_write_roots",
        "capability_write_roots",
        "forbidden_write_roots",
        "locked_files",
        "output_json",
        "product_write_roots",
        "task_dir",
    }
)


# LLM: 写边界的拒绝结果：仍是给模型看的消息字符串（原调用方按 str 用、按空串判放行不变），另带结构化拒绝码 code。路径门拒在
#   宿主托管文件上时是原码（path_access_policy.HOST_FILE_DENIAL_CODES），其余拒绝是 WRITE_FORBIDDEN。调用方只读 code，
#   不从消息文字反推（registry_invoke._write_boundary_denied、action_policy._task_boundary_decision）。
# 类用途: 写边界拒绝消息，附带要上报的错误码。
class WriteBoundaryDenial(str):
    code: str

    # 函数用途: 用消息和结构化码构造拒绝结果。
    def __new__(cls, message: str, code: str = "WRITE_FORBIDDEN") -> WriteBoundaryDenial:
        denial = super().__new__(cls, message)
        denial.code = code
        return denial


# LLM: 只读结构化 code；不是 WriteBoundaryDenial（旧调用方自己拼的字符串）时按 WRITE_FORBIDDEN。
# 函数用途: 取写边界拒绝结果要上报的错误码。
def write_boundary_error_code(error: str) -> str:
    return getattr(error, "code", "") or "WRITE_FORBIDDEN"


def _path_text(raw_path: object, *, label: str = "path") -> str:
    if raw_path is None:
        raise ValueError(f"{label} 参数缺失")
    if not isinstance(raw_path, (str, Path)):
        raise ValueError(f"{label} 参数必须是字符串路径")
    text = str(raw_path).strip()
    if not text:
        raise ValueError(f"{label} 不能为空")
    if len(text) > _MAX_BOUNDARY_PATH_CHARS:
        raise ValueError(f"{label} 过长，最多 {_MAX_BOUNDARY_PATH_CHARS} 个字符")
    if any(ord(char) < 32 for char in text):
        raise ValueError(f"{label} 包含不支持的控制字符")
    # Normalize path separators to forward slashes for cross-platform consistency
    text = text.replace("\\", "/")
    return text


# LLM: Dangerous-path policy applies to every filesystem mutation; the narrower task boundary
# is optional, but once any structured scope field exists, missing/empty allowed roots fail closed.
#   路径门用 PathAccessPolicy.check_write（含 H3 宿主配置拒写），与插件写入上下文 workspace_write_context 保持同一裁决。
# 函数用途: 在文件写入前先守全局危险路径和宿主配置，再按当前任务的结构化允许、禁止和锁定范围裁决。
def validate_write_boundary(
    tool_name: str,
    params: dict[str, Any],
    *,
    workspace_root: Path,
    workspace_roots: list[Path] | None = None,
    path_access_mode: str = "normal",
    path_dangerous_roots: list[str] | None = None,
    write_boundary: dict[str, object] | None,
) -> str:

    if tool_name not in WRITE_TOOL_NAMES or write_boundary is None:
        return ""

    if not isinstance(params, dict):
        return "写入被阻止: 工具参数必须是 JSON 对象。"
    enforces_scope = _boundary_enforces_write_scope(write_boundary)

    raw_paths = declared_write_paths(tool_name, params)
    if not raw_paths:
        return ""

    roots = _normalized_workspace_roots(workspace_root, workspace_roots)
    # 数据根取写边界里宿主解析出的 owner home（H3 二审：与文件工具同一来源，不能只看环境变量）。
    path_policy = PathAccessPolicy.from_values(
        mode=path_access_mode,
        dangerous_roots=path_dangerous_roots,
        agent_home_root=data_root_from_boundary(write_boundary),
    )
    allowed_roots = _boundary_paths(write_boundary.get("allowed_write_roots"), workspace_root, roots)
    for raw_path in raw_paths:
        try:
            target = _resolve_boundary_path(raw_path, workspace_root, roots)
        except ValueError as exc:
            return f"写入被阻止: {exc}"
        access_decision = path_policy.check_write(target)
        if not access_decision.allowed:
            code = access_decision.code if access_decision.code in HOST_FILE_DENIAL_CODES else "WRITE_FORBIDDEN"
            return WriteBoundaryDenial(f"写入被阻止: {access_decision.message}", code)
        install_error = _runtime_install_error(target, write_boundary, workspace_root)
        if install_error:
            return install_error
        if not enforces_scope:
            continue
        allowed_error = _allowed_boundary_error(
            target,
            allowed_roots,
            workspace_root,
            scope_declared=_boundary_enforces_write_scope(write_boundary),
        )
        if allowed_error:
            return allowed_error
        internal_output_error = _internal_output_json_error(target, write_boundary, workspace_root, roots)
        if internal_output_error:
            return internal_output_error
        forbidden_error = _forbidden_boundary_error(target, allowed_roots, write_boundary, workspace_root)
        if forbidden_error:
            return forbidden_error
        locked_error = _locked_boundary_error(target, write_boundary, workspace_root)
        if locked_error:
            return locked_error
    return ""


# LLM: Runtime audit metadata alone must not invent a workspace allowlist, while any genuine
# path-boundary field activates fail-closed write-scope enforcement.
# 函数用途: 区分“只有运行账本”与“已经声明路径围栏”的边界对象。
def _boundary_enforces_write_scope(write_boundary: dict[str, object]) -> bool:
    if not write_boundary:
        return False
    return any(key in write_boundary for key in _WRITE_SCOPE_BOUNDARY_KEYS)


def declared_write_paths(tool_name: str, params: dict[str, Any]) -> list[str]:
    """Return only paths explicitly declared by a filesystem mutation call."""

    if tool_name == "write_file":
        raw_path = params.get("path")
        return [str(raw_path)] if raw_path is not None else []
    if tool_name == "edit_file":
        raw_path = params.get("path")
        return [str(raw_path)] if raw_path is not None else []
    if tool_name == "apply_patch":
        patch = params.get("patch")
        if not isinstance(patch, str):
            return []
        return _patch_declared_paths(patch)
    return []


def _patch_declared_paths(patch: str) -> list[str]:
    paths: list[str] = []
    for raw in patch.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        path = _patch_declared_path(raw)
        if path:
            paths.append(path)
    return [path for path in paths if path]


def _patch_declared_path(raw: str) -> str:
    prefixes = (
        "*** Add File: ",
        "*** Update File: ",
        "*** Delete File: ",
        "*** Move to: ",
    )
    for prefix in prefixes:
        if raw.startswith(prefix):
            return raw.removeprefix(prefix).strip()
    return ""


def _internal_output_json_error(
    target: Path,
    write_boundary: dict[str, object],
    workspace_root: Path,
    workspace_roots: list[Path],
) -> str:
    output_refs = _boundary_paths([write_boundary.get("output_json")], workspace_root, workspace_roots)
    if not output_refs or target == output_refs[0]:
        return ""
    if target.name != _INTERNAL_OUTPUT_JSON_NAME:
        return ""
    product_roots = _boundary_paths(write_boundary.get("product_write_roots"), workspace_root, workspace_roots)
    if not any(_is_relative_to(target, root) for root in product_roots):
        return ""
    return (
        "内部结果文件写入被阻止: output.json 是子代理 runner 的收口/验收文件，"
        "不能写进用户产物目录，避免污染 deliverables。"
        f" target={_display_path(target, workspace_root)} "
        f"execution_context.output_json={output_refs[0]}"
    )


def _allowed_boundary_error(
    target: Path,
    allowed_roots: list[Path],
    workspace_root: Path,
    *,
    scope_declared: bool,
) -> str:
    if not allowed_roots:
        return (
            "写入被阻止: 当前任务没有有效的 allowed_write_roots，按安全默认拒绝写入。"
            if scope_declared
            else ""
        )
    if any(_is_relative_to(target, root) for root in allowed_roots):
        return ""
    allowed = ", ".join(_display_path(root, workspace_root) for root in allowed_roots[:5])
    suffix = "" if len(allowed_roots) <= 5 else f" 等 {len(allowed_roots)} 个"
    return (
        "写入被阻止: 目标路径不在 allowed_write_roots 内。"
        f" target={_display_path(target, workspace_root)} allowed={allowed}{suffix}"
    )


def _forbidden_boundary_error(
    target: Path,
    allowed_roots: list[Path],
    write_boundary: dict[str, object],
    workspace_root: Path,
) -> str:
    forbidden_roots = _boundary_paths(write_boundary.get("forbidden_write_roots"), workspace_root)
    for root in forbidden_roots:
        if _forbidden_root_blocks_target(target, root, allowed_roots):
            return (
                "写入被阻止: 目标路径落在 forbidden_write_roots 内。"
                f" target={_display_path(target, workspace_root)} forbidden={_display_path(root, workspace_root)}"
            )
    return ""


# LLM: 路径优先级对齐 会话运行时 FileSystemSandboxPolicy：命中目标的最具体条目生效，
# 同层冲突时 deny 胜出；宽泛家目录保护不能吞掉更窄的结构化交付授权。
# 函数用途: 比较命中目标的允许/禁止目录层级；更窄的明确授权可穿过祖先保护，同层或更窄的禁止规则仍拦截。
def _forbidden_root_blocks_target(target: Path, root: Path, allowed_roots: list[Path]) -> bool:
    if not _is_relative_to(target, root):
        return False
    matching_allowed = [aroot for aroot in allowed_roots if _is_relative_to(target, aroot)]
    if not matching_allowed:
        return True
    allowed_specificity = max(len(aroot.parts) for aroot in matching_allowed)
    return len(root.parts) >= allowed_specificity


# LLM: 安装目录保护与写入范围无关、也不受“更具体的允许目录胜出”影响：命中就拒，Full Access 同样生效。
# 函数用途: 拒绝改写正在运行的 my-agent 安装目录，提示去开发工作树改代码。
def _runtime_install_error(target: Path, write_boundary: dict[str, object], workspace_root: Path) -> str:
    for root in _boundary_paths(write_boundary.get(RUNTIME_INSTALL_ROOTS_KEY), workspace_root):
        if _is_relative_to(target, root):
            return (
                "写入被阻止: 目标位于正在运行的 my-agent 安装目录，部署会整体替换它，不能直接修改。"
                f" target={_display_path(target, workspace_root)}；改自身代码请在配置的开发工作树里改。"
            )
    return ""


def _locked_boundary_error(target: Path, write_boundary: dict[str, object], workspace_root: Path) -> str:
    locked_paths = _boundary_paths(write_boundary.get("locked_files"), workspace_root)
    for locked in locked_paths:
        if target == locked or _is_relative_to(target, locked):
            return (
                "写入被阻止: 目标路径已被 locked_files 锁定。"
                f" target={_display_path(target, workspace_root)} locked={_display_path(locked, workspace_root)}"
            )
    return ""


def resolved_write_roots(
    write_boundary: dict[str, Any] | None,
    workspace_root: Path,
) -> list[Path]:
    """权威归一化 allowed_write_roots（与写边界校验同一解析器）。

    seq 248 #6：shell 类工具按此锁定 workspace-wide 写根——bwrap 沙箱允许写
    全部 allowed_write_roots，锁不能只覆盖 working_dir（只锁 cwd 的假安全）。
    """
    if not write_boundary:
        return []
    return _boundary_paths(
        write_boundary.get("allowed_write_roots"), workspace_root
    )


def _boundary_paths(
    raw_paths: object,
    workspace_root: Path,
    workspace_roots: list[Path] | None = None,
) -> list[Path]:
    if not isinstance(raw_paths, list):
        return []
    paths: list[Path] = []
    for raw in raw_paths:
        try:
            paths.append(_resolve_boundary_path(raw, workspace_root, workspace_roots))
        except ValueError:
            continue
    return paths


def _resolve_boundary_path(
    raw_path: object,
    workspace_root: Path,
    workspace_roots: list[Path] | None = None,
) -> Path:
    text = _path_text(raw_path)
    root = workspace_root.resolve(strict=False)
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError("路径解析失败，请检查路径是否有效。") from exc
    return resolved


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _normalized_workspace_roots(primary: Path, roots: list[Path] | None) -> list[Path]:
    resolved: list[Path] = []
    for raw in [primary, *(roots or [])]:
        path = Path(raw).resolve(strict=False)
        if path not in resolved:
            resolved.append(path)
    return resolved


def _display_path(path: Path, workspace_root: Path) -> str:
    try:
        return str(path.relative_to(workspace_root)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")
