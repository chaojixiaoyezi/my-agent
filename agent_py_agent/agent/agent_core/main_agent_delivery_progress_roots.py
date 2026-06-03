
from __future__ import annotations

from pathlib import Path
from typing import Any

from .delivery_closeout.artifacts import _artifact_path


def work_progress_roots(workspace_root: Path, contract: dict[str, Any]) -> list[Path]:
    roots = [workspace_root / "outputs", workspace_root / "scripts", workspace_root / "data"]
    roots.extend(_contract_progress_paths(workspace_root, contract))
    return _dedupe_paths(roots)


def _contract_progress_paths(workspace_root: Path, contract: dict[str, Any]) -> list[Path]:
    return [
        *_artifact_progress_paths(workspace_root, contract.get("artifacts")),
        *_bootstrap_progress_paths(workspace_root, contract.get("bootstrap_contract")),
    ]


def _artifact_progress_paths(workspace_root: Path, artifacts: object) -> list[Path]:
    paths: list[Path] = []
    for item in _dict_items(artifacts):
        paths.extend(_resolved_contract_paths(workspace_root, item, ("preferred_path", "path")))
        paths.extend(_validation_progress_paths(workspace_root, item.get("validation_contract")))
    return paths


def _validation_progress_paths(workspace_root: Path, validation: object) -> list[Path]:
    if not isinstance(validation, dict):
        return []
    return _staging_progress_paths(workspace_root, validation.get("staging_contract"))


def _bootstrap_progress_paths(workspace_root: Path, bootstrap: object) -> list[Path]:
    if not isinstance(bootstrap, dict):
        return []
    paths: list[Path] = []
    for target in _dict_items(bootstrap.get("materialization_targets")):
        paths.extend(_resolved_contract_paths(workspace_root, target, ("workspace_relative_path", "path")))
    return paths


def _staging_progress_paths(workspace_root: Path, staging: object) -> list[Path]:
    if not isinstance(staging, dict):
        return []
    paths = _resolved_contract_paths(
        workspace_root,
        staging,
        _staging_ref_keys(staging),
    )
    paths.extend(_checkpoint_progress_paths(workspace_root, staging.get("checkpoint_refs")))
    return paths


def _staging_ref_keys(staging: dict[str, Any]) -> tuple[str, ...]:
    keys = [
        str(staging.get("source_ref_key") or "").strip(),
        str(staging.get("input_ref_key") or "").strip(),
        str(staging.get("output_ref_key") or "").strip(),
        "source_json_ref",
        "source_markdown_ref",
        "source_ref",
        "input_ref",
        "workbook_ref",
        "pdf_ref",
        "output_ref",
        "artifact_ref",
    ]
    return tuple(dict.fromkeys(key for key in keys if key))


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


def _resolved_contract_paths(workspace_root: Path, payload: dict[str, Any], keys: tuple[str, ...]) -> list[Path]:
    return [
        path
        for key in keys
        for value in [payload.get(key)]
        if isinstance(value, str)
        for path in [_contract_path(workspace_root, value)]
        if path is not None
    ]


def _contract_path(workspace_root: Path, raw_path: str) -> Path | None:
    return _artifact_path(raw_path, workspace_root)


def _dict_items(value: object) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


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
