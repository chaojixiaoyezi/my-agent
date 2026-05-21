# LLM: Delivery progress roots are derived from structured contracts instead of task prose.
# 模块用途: 计算 no-progress 指纹应该观察的工作目录，包含默认工作根和合同声明的产物路径。

from __future__ import annotations

from pathlib import Path
from typing import Any

from .main_agent_delivery_closeout_artifacts import _artifact_path


# LLM: work_progress_roots combines default work roots with contract-declared artifact roots.
# 函数用途: 让 no-progress 判断跟随 delivery_contract 的真实产物路径，而不是只看固定 outputs/ 目录。
def work_progress_roots(workspace_root: Path, contract: dict[str, Any]) -> list[Path]:
    roots = [workspace_root / "outputs", workspace_root / "scripts", workspace_root / "data"]
    roots.extend(_contract_progress_paths(workspace_root, contract))
    return _dedupe_paths(roots)


# LLM: _contract_progress_paths extracts machine-declared output paths from artifacts, bootstrap targets and staging refs.
# 函数用途: 支持 lab_outputs、PDF/XLSX staging、代码目录等任意合同产物根参与进展指纹。
def _contract_progress_paths(workspace_root: Path, contract: dict[str, Any]) -> list[Path]:
    return [
        *_artifact_progress_paths(workspace_root, contract.get("artifacts")),
        *_bootstrap_progress_paths(workspace_root, contract.get("bootstrap_contract")),
    ]


# LLM: _artifact_progress_paths keeps each artifact and its staging refs visible to no-progress detection.
# 函数用途: 从 artifacts 数组中提取 preferred_path/path 和 validation_contract.staging_contract。
def _artifact_progress_paths(workspace_root: Path, artifacts: object) -> list[Path]:
    paths: list[Path] = []
    for item in _dict_items(artifacts):
        paths.extend(_resolved_contract_paths(workspace_root, item, ("preferred_path", "path")))
        paths.extend(_validation_progress_paths(workspace_root, item.get("validation_contract")))
    return paths


# LLM: _validation_progress_paths extracts staged source/output refs from validation contracts.
# 函数用途: staging_contract 不是 dict 时返回空列表，保持合同版本兼容。
def _validation_progress_paths(workspace_root: Path, validation: object) -> list[Path]:
    if not isinstance(validation, dict):
        return []
    return _staging_progress_paths(workspace_root, validation.get("staging_contract"))


# LLM: _bootstrap_progress_paths tracks materialization targets declared by the bootstrap contract.
# 函数用途: 多文件任务还没写完所有 bootstrap target 时，进展判断不会误看固定目录。
def _bootstrap_progress_paths(workspace_root: Path, bootstrap: object) -> list[Path]:
    if not isinstance(bootstrap, dict):
        return []
    paths: list[Path] = []
    for target in _dict_items(bootstrap.get("materialization_targets")):
        paths.extend(_resolved_contract_paths(workspace_root, target, ("workspace_relative_path", "path")))
    return paths


# LLM: _staging_progress_paths keeps staged source/output refs visible to no-progress detection.
# 函数用途: 从 staging_contract 中提取 source、workbook、PDF 和 checkpoint refs。
def _staging_progress_paths(workspace_root: Path, staging: object) -> list[Path]:
    if not isinstance(staging, dict):
        return []
    paths = _resolved_contract_paths(
        workspace_root,
        staging,
        ("source_json_ref", "workbook_ref", "pdf_ref", "output_ref"),
    )
    paths.extend(_checkpoint_progress_paths(workspace_root, staging.get("checkpoint_refs")))
    return paths


# LLM: _checkpoint_progress_paths normalizes checkpoint refs without adding nested control flow.
# 函数用途: checkpoint_refs 不是字符串列表时忽略坏项。
def _checkpoint_progress_paths(workspace_root: Path, refs: object) -> list[Path]:
    if not isinstance(refs, list):
        return []
    return [
        path
        for ref in refs
        if isinstance(ref, str)
        for path in [_contract_path(workspace_root, ref)]
        if path is not None
    ]


# LLM: _resolved_contract_paths turns structured refs into bounded workspace paths.
# 函数用途: 对多个路径字段做统一解析，越界或空值忽略。
def _resolved_contract_paths(workspace_root: Path, payload: dict[str, Any], keys: tuple[str, ...]) -> list[Path]:
    return [
        path
        for key in keys
        for value in [payload.get(key)]
        if isinstance(value, str)
        for path in [_contract_path(workspace_root, value)]
        if path is not None
    ]


# LLM: _contract_path reuses workspace-bounded artifact path semantics for progress roots.
# 函数用途: 把合同路径落到 workspace 内；工作区外路径不参与进展指纹。
def _contract_path(workspace_root: Path, raw_path: str) -> Path | None:
    return _artifact_path(raw_path, workspace_root)


# LLM: _dict_items narrows list entries to dicts.
# 函数用途: 让调用方用平铺循环处理结构化列表，避免层层嵌套判断。
def _dict_items(value: object) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


# LLM: _dedupe_paths preserves root order while removing duplicate path refs.
# 函数用途: 避免同一产物根被 artifacts/bootstrap/staging 重复扫描。
def _dedupe_paths(paths: list[Path]) -> list[Path]:
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        marker = str(path)
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append(path)
    return deduped


__all__ = ["work_progress_roots"]
