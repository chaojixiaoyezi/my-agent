
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ...common.value_parsing import TOOL_TEXT_LIST_OPTIONS, string_list
from ..runner.ref_fields import params_input_refs, params_output_refs

_PRODUCT_TARGET_SUFFIXES = {
    ".html",
    ".htm",
    ".css",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".jsx",
    ".py",
    ".md",
    ".json",
    ".yaml",
    ".yml",
    ".txt",
    ".csv",
    ".xlsx",
    ".xls",
    ".pdf",
}


@dataclass(frozen=True)
class _TaskOutputRootInput:
    output_ref: str
    workspace_root: Path
    workspace_roots: list[Path]
    input_paths: list[Path]


def normalized_write_root(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    path = Path(text).expanduser()
    if _path_has_file_suffix(path):
        path = path.parent
    return str(path)


def context_target_write_roots(agent: object, params: dict[str, object]) -> list[str]:
    raw_root = getattr(getattr(agent, "subagents", None), "workspace_root", None)
    if not isinstance(raw_root, str | Path):
        return []
    workspace_root = Path(raw_root).expanduser().resolve(strict=False)
    roots = agent_workspace_roots(agent, workspace_root)
    result: list[str] = []
    for value in _context_target_refs(params):
        path = _workspace_product_file_path(value, workspace_root, roots)
        if path is None:
            continue
        root = str(path.parent)
        if root not in result:
            result.append(root)
    return result


# 不要求输入文件已存在，也不依赖文件类型枚举，避免模型只能写进子代理私有目录。
def structured_output_write_roots(agent: object, params: dict[str, object]) -> list[str]:
    raw_root = getattr(getattr(agent, "subagents", None), "workspace_root", None)
    if not isinstance(raw_root, str | Path):
        return []
    workspace_root = Path(raw_root).expanduser().resolve(strict=False)
    roots = agent_workspace_roots(agent, workspace_root)
    result: list[str] = []
    for value in params_output_refs(params):
        path = _workspace_product_file_path(value, workspace_root, roots)
        if path is None:
            continue
        root = str(path.parent)
        if root not in result:
            result.append(root)
    return result


# 允许子代理写到同一任务目录的输出根；不从 goal/prompt 自然语言猜授权。
def structured_task_output_write_roots(agent: object, params: dict[str, object]) -> list[str]:
    raw_root = getattr(getattr(agent, "subagents", None), "workspace_root", None)
    if not isinstance(raw_root, str | Path):
        return []
    workspace_root = Path(raw_root).expanduser().resolve(strict=False)
    workspace_roots = agent_workspace_roots(agent, workspace_root)
    input_paths = _existing_absolute_paths(params_input_refs(params), workspace_root)
    if not input_paths:
        return []
    roots: list[str] = []
    for output_ref in params_output_refs(params):
        _append_task_output_root(roots, _TaskOutputRootInput(output_ref, workspace_root, workspace_roots, input_paths))
    return roots


def _append_task_output_root(roots: list[str], request: _TaskOutputRootInput) -> None:
    output_path = _absolute_ref_path(request.output_ref, request.workspace_root)
    if output_path is None or any(is_relative_to(output_path, root) for root in request.workspace_roots):
        return
    if not _output_matches_existing_task_input(output_path, request.input_paths):
        return
    root = str(output_path.parent)
    if root not in roots:
        roots.append(root)


def agent_workspace_roots(agent: object, root: Path) -> list[Path]:
    raw_roots = getattr(getattr(agent, "subagents", None), "workspace_roots", None)
    if not isinstance(raw_roots, list):
        return [root]
    roots: list[Path] = []
    for raw in [root, *raw_roots]:
        if isinstance(raw, str | Path):
            roots.append(Path(raw).expanduser().resolve(strict=False))
    return roots or [root]


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _context_target_refs(params: dict[str, object]) -> list[str]:
    refs = string_list(params.get("required_read_paths"), TOOL_TEXT_LIST_OPTIONS)
    manifest = params.get("context_manifest")
    if isinstance(manifest, dict):
        refs.extend(string_list(manifest.get("required_read_paths"), TOOL_TEXT_LIST_OPTIONS))
    contract = params.get("repair_contract")
    if isinstance(contract, dict):
        refs.extend(string_list(contract.get("target_artifact_refs"), TOOL_TEXT_LIST_OPTIONS))
    packs = params.get("context_packs")
    for pack in packs if isinstance(packs, list) else []:
        if isinstance(pack, dict) and isinstance(pack.get("contract"), dict):
            refs.extend(string_list(pack["contract"].get("target_artifact_refs"), TOOL_TEXT_LIST_OPTIONS))
    return refs


def _workspace_product_file_path(value: str, workspace_root: Path, workspace_roots: list[Path]) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    path = candidate if candidate.is_absolute() else workspace_root / candidate
    resolved = path.resolve(strict=False)
    if not _path_has_file_suffix(resolved) or _is_agent_internal_path(resolved):
        return None
    return resolved if any(is_relative_to(resolved, root) for root in workspace_roots) else None


def _is_agent_internal_path(path: Path) -> bool:
    return any(part in {".my_agent", ".my-agent"} for part in path.parts)


def _path_has_file_suffix(path: Path) -> bool:
    suffix = path.suffix.lower()
    if not suffix:
        return False
    if suffix in _PRODUCT_TARGET_SUFFIXES:
        return True
    return len(suffix) > 1 and suffix[1:].replace(".", "").replace("_", "").replace("+", "").replace("-", "").isalnum()


def _existing_absolute_paths(refs: list[str], workspace_root: Path) -> list[Path]:
    paths: list[Path] = []
    for ref in refs:
        path = _absolute_ref_path(ref, workspace_root)
        if path is not None and path.exists() and path not in paths:
            paths.append(path)
    return paths


def _absolute_ref_path(ref: str, workspace_root: Path) -> Path | None:
    text = str(ref or "").strip()
    if not text or "://" in text:
        return None
    candidate = Path(text).expanduser()
    path = candidate if candidate.is_absolute() else workspace_root / candidate
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _output_matches_existing_task_input(output_path: Path, input_paths: list[Path]) -> bool:
    for input_path in input_paths:
        common = _common_parent(input_path, output_path)
        if common is not None and _safe_external_task_root(common):
            return True
    return False


def _common_parent(left: Path, right: Path) -> Path | None:
    try:
        return Path(os.path.commonpath([str(left), str(right)])).resolve(strict=False)
    except (OSError, ValueError):
        return None


def _safe_external_task_root(path: Path) -> bool:
    root = path.resolve(strict=False)
    home = Path.home().resolve(strict=False)
    if root == root.parent or root == home:
        return False
    return len(root.parts) >= 3
