# LLM: Create target roots resolve structured artifact refs into product write directories.
# 模块用途: 从 repair/context/read refs 推导安全的产物写入根，避免 create/schedule 把修复写到错误目录。

from __future__ import annotations

from pathlib import Path

from .parameters import _string_list

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


# LLM: normalized_write_root turns file targets into their parent directory.
# 函数用途: create_subagents 从自然语言或 extra_write_roots 收到 `/.../index.html` 时，持久化目录写入根而不是文件根。
def normalized_write_root(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    path = Path(text).expanduser()
    if path.suffix.lower() in _PRODUCT_TARGET_SUFFIXES:
        path = path.parent
    return str(path)


# LLM: context_target_write_roots prefers structured target refs over broad workspace defaults.
# 函数用途: 修复/生成任务带 required_read_paths 或 repair_contract 目标产物时，授权目标文件所在目录，避免写偏到项目根。
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


# LLM: agent_workspace_roots mirrors write-guard root normalization without importing that preflight module.
# 函数用途: 读取 agent.subagents.workspace_roots；缺省时只允许 workspace_root 自身。
def agent_workspace_roots(agent: object, root: Path) -> list[Path]:
    raw_roots = getattr(getattr(agent, "subagents", None), "workspace_roots", None)
    if not isinstance(raw_roots, list):
        return [root]
    roots: list[Path] = []
    for raw in [root, *raw_roots]:
        if isinstance(raw, str | Path):
            roots.append(Path(raw).expanduser().resolve(strict=False))
    return roots or [root]


# LLM: is_relative_to keeps pathlib compatibility in one tiny helper.
# 函数用途: 判断 path 是否位于 root 下，用于 workspace_root 默认授权校验。
def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# LLM: _context_target_refs collects machine refs from model params without reading file bodies.
# 函数用途: 合并 required_read_paths、context_manifest 和 repair_contract.target_artifact_refs，供写入根收窄使用。
def _context_target_refs(params: dict[str, object]) -> list[str]:
    refs = _string_list(params.get("required_read_paths"))
    manifest = params.get("context_manifest")
    if isinstance(manifest, dict):
        refs.extend(_string_list(manifest.get("required_read_paths")))
    contract = params.get("repair_contract")
    if isinstance(contract, dict):
        refs.extend(_string_list(contract.get("target_artifact_refs")))
    packs = params.get("context_packs")
    for pack in packs if isinstance(packs, list) else []:
        if isinstance(pack, dict) and isinstance(pack.get("contract"), dict):
            refs.extend(_string_list(pack["contract"].get("target_artifact_refs")))
    return refs


# LLM: _workspace_product_file_path filters context refs to concrete workspace product files.
# 函数用途: 忽略 .my_agent 内部报告和非产物后缀，只返回 workspace 内可写目标文件路径。
def _workspace_product_file_path(value: str, workspace_root: Path, workspace_roots: list[Path]) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    path = candidate if candidate.is_absolute() else workspace_root / candidate
    resolved = path.resolve(strict=False)
    if resolved.suffix.lower() not in _PRODUCT_TARGET_SUFFIXES or _is_agent_internal_path(resolved):
        return None
    return resolved if any(is_relative_to(resolved, root) for root in workspace_roots) else None


# LLM: _is_agent_internal_path keeps reports/task refs from becoming product write roots.
# 函数用途: required_read_paths 里常有 .my_agent 报告；这些只能读，不能当成产物目录授权。
def _is_agent_internal_path(path: Path) -> bool:
    return any(part in {".my_agent", ".my-agent"} for part in path.parts)
