# LLM: 这是写工具的安全闸口，路径归一和错误解释必须保持保守。
# 模块用途: 子代理写入边界校验，阻止文件工具越过授权路径。

from __future__ import annotations

"""enforces subagent write scopes before mutating filesystem tools run.

给人看的解释：
这个文件是一道真正的写入门禁。
prompt 里说'只能写这个目录'只是提醒，真正防止越界写文件的是这里的路径检查。
它会检查允许目录、禁止目录、锁定文件，确保子代理不能改不该改的地方。
"""

from pathlib import Path
from typing import Any

from ..path_access_policy import PathAccessPolicy

WRITE_TOOL_NAMES = {"write_file", "apply_patch"}
_MAX_BOUNDARY_PATH_CHARS = 4096
_PRODUCT_WRITE_DELEGATE_POLICY = "delegate"
_INTERNAL_OUTPUT_JSON_NAME = "output.json"
_WRITE_SCOPE_BOUNDARY_KEYS = frozenset(
    {
        "allowed_write_roots",
        "forbidden_write_roots",
        "locked_files",
        "output_json",
        "product_write_roots",
        "task_dir",
    }
)
_REPORT_ARTIFACT_SUFFIXES = (".md", ".txt", ".json", ".jsonl")
_REPORT_ARTIFACT_NAME_MARKERS = (
    "acceptance",
    "audit",
    "bug",
    "check",
    "finding",
    "handoff",
    "report",
    "review",
    "status",
    "summary",
    "test",
    "验收",
    "报告",
    "测试",
)


# LLM: _path_text 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 工具系统 中的 path_text 步骤，并保持调用方依赖的数据形状。
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


# LLM: validate_write_boundary 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 工具系统 中的 validate_write_boundary 步骤，并保持调用方依赖的数据形状。
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
    if not _boundary_enforces_write_scope(write_boundary):
        return ""

    raw_paths = _tool_write_paths(tool_name, params)
    if not raw_paths:
        return ""

    roots = _normalized_workspace_roots(workspace_root, workspace_roots)
    path_policy = PathAccessPolicy.from_values(
        mode=path_access_mode,
        dangerous_roots=path_dangerous_roots,
    )
    allowed_roots = _boundary_paths(write_boundary.get("allowed_write_roots"), workspace_root, roots)
    for raw_path in raw_paths:
        try:
            target = _resolve_boundary_path(raw_path, workspace_root, roots)
        except ValueError as exc:
            return f"写入被阻止: {exc}"
        access_decision = path_policy.check(target)
        if not access_decision.allowed:
            return f"写入被阻止: {access_decision.message}"
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


def _boundary_enforces_write_scope(write_boundary: dict[str, object]) -> bool:
    if not write_boundary:
        return True
    return any(key in write_boundary for key in _WRITE_SCOPE_BOUNDARY_KEYS)


def _tool_write_paths(tool_name: str, params: dict[str, Any]) -> list[str]:
    if tool_name == "write_file":
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


# LLM: _patch_declared_path extracts one filesystem path from a patch control line.
# 函数用途: 让 write boundary 能在 apply_patch 真执行前检查全部声明路径。
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


# LLM: _internal_output_json_error keeps runner bookkeeping out of user deliverable roots.
# 函数用途: 阻止子代理把内部收口 output.json 写进产品目录；真实 task.output_json 仍然允许写。
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


# LLM: _product_write_policy_error separates inherited authority from direct business writes.
# 函数用途: 上层 coordinator/tester/reviewer 可以拥有产物目录权限用于检查和救援，但默认不能直接写业务产物。
def _product_write_policy_error(
    target: Path,
    write_boundary: dict[str, object],
    workspace_root: Path,
    workspace_roots: list[Path],
) -> str:
    policy = str(write_boundary.get("product_write_policy") or "").strip().lower()
    if policy != _PRODUCT_WRITE_DELEGATE_POLICY:
        return ""
    product_roots = _boundary_paths(write_boundary.get("product_write_roots"), workspace_root, workspace_roots)
    if not any(_is_relative_to(target, root) for root in product_roots):
        return ""
    if _is_report_artifact_target(target):
        return ""
    role = str(write_boundary.get("role") or "coordinator").strip() or "coordinator"
    return (
        "业务产物写入被阻止: 当前角色拥有上层覆盖权限用于检查、接管和救援，"
        "但默认不能直接写最终业务产物。"
        f" role={role} target={_display_path(target, workspace_root)} "
        "请创建或调度 worker/writer/leaf_worker 处理该产物；"
        "当前角色可以把 test_report/acceptance_report/status 等报告证据写到授权目录，"
        "但不能写 index.html 等业务正文。"
    )


# LLM: _is_report_artifact_target lets QA/acceptance roles leave visible evidence in deliverables.
# 函数用途: 识别报告、测试、验收、状态、发现等交接证据文件；这些不是业务正文产物。
def _is_report_artifact_target(target: Path) -> bool:
    name = target.name.lower()
    if name == _INTERNAL_OUTPUT_JSON_NAME:
        return False
    if not name.endswith(_REPORT_ARTIFACT_SUFFIXES):
        return False
    return any(marker in name for marker in _REPORT_ARTIFACT_NAME_MARKERS)


# LLM: _forbidden_boundary_error 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 工具系统 中的 forbidden_boundary_error 步骤，并保持调用方依赖的数据形状。
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


# LLM: _forbidden_root_blocks_target 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 工具系统 中的 forbidden_root_blocks_target 步骤，并保持调用方依赖的数据形状。
def _forbidden_root_blocks_target(target: Path, root: Path, allowed_roots: list[Path]) -> bool:
    if not _is_relative_to(target, root):
        return False
    return any(_is_relative_to(root, aroot) for aroot in allowed_roots)


# LLM: _locked_boundary_error 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 工具系统 中的 locked_boundary_error 步骤，并保持调用方依赖的数据形状。
def _locked_boundary_error(target: Path, write_boundary: dict[str, object], workspace_root: Path) -> str:
    locked_paths = _boundary_paths(write_boundary.get("locked_files"), workspace_root)
    for locked in locked_paths:
        if target == locked or _is_relative_to(target, locked):
            return (
                "写入被阻止: 目标路径已被 locked_files 锁定。"
                f" target={_display_path(target, workspace_root)} locked={_display_path(locked, workspace_root)}"
            )
    return ""


# LLM: _boundary_paths 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 完成 工具系统 中的 boundary_paths 步骤，并保持调用方依赖的数据形状。
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


# LLM: _resolve_boundary_path 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 解析 resolve_boundary_path 并确认结果仍在允许边界内。
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


# LLM: _is_relative_to 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 判断 is_relative_to 是否满足安全或状态条件。
def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# LLM: _normalized_workspace_roots 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 解析并去重工作区根目录，保留第一个主工作区。
def _normalized_workspace_roots(primary: Path, roots: list[Path] | None) -> list[Path]:
    resolved: list[Path] = []
    for raw in [primary, *(roots or [])]:
        path = Path(raw).resolve(strict=False)
        if path not in resolved:
            resolved.append(path)
    return resolved


# LLM: _display_path 属于 工具系统 的调用边界；改行为前先核对直接调用方和错误路径。
# 函数用途: 把内部路径转换成调用方可读的展示路径。
def _display_path(path: Path, workspace_root: Path) -> str:
    try:
        return str(path.relative_to(workspace_root)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")
