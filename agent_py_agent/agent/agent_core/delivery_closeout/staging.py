
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...contracts.staged_checkpoint import json_checkpoint_status
from .artifacts import _artifact_path


@dataclass(frozen=True)
class StagingPrerequisiteRequest:
    staging: dict[str, Any]
    workspace_root: Path
    output_ref: str
    source_ref: str
    required_columns: list[str]
    required_sheets_min: int = 0


def staging_checkpoint_refs(staging: dict[str, Any]) -> list[str]:
    refs = staging.get("checkpoint_refs")
    if not isinstance(refs, list):
        return []
    return [text for ref in refs if (text := str(ref or "").strip())]


def staging_source_ref(staging: dict[str, Any]) -> str:
    keys = [
        str(staging.get("source_ref_key") or "").strip(),
        str(staging.get("input_ref_key") or "").strip(),
        "source_json_ref",
        "source_markdown_ref",
        "source_ref",
        "input_ref",
    ]
    for key in keys:
        if not key:
            continue
        if value := str(staging.get(key) or "").strip():
            return value
    return ""


def staging_output_ref(staging: dict[str, Any], artifact: dict[str, Any]) -> str:
    keys = [
        str(staging.get("output_ref_key") or "").strip(),
        "workbook_ref",
        "pdf_ref",
        "output_ref",
        "artifact_ref",
    ]
    for key in keys:
        if not key:
            continue
        if value := str(staging.get(key) or "").strip():
            return value
    return str(artifact.get("preferred_path") or artifact.get("path") or "").strip()


def staging_prerequisites_ready(request: StagingPrerequisiteRequest) -> bool:
    for ref_text in staging_checkpoint_refs(request.staging):
        if ref_text == request.output_ref:
            break
        columns = request.required_columns if ref_text == request.source_ref else []
        sheets_min = request.required_sheets_min if ref_text == request.source_ref else 0
        if not staged_input_ready(
            ref_text,
            request.workspace_root,
            required_columns=columns,
            required_sheets_min=sheets_min,
        ):
            return False
    return True


def staged_input_ready(
    ref_text: str,
    workspace_root: Path,
    *,
    required_columns: list[str] | None = None,
    required_sheets_min: int = 0,
) -> bool:
    path = _artifact_path(ref_text, workspace_root)
    if path is None or not path.exists():
        return False
    if path.suffix.lower() == ".json":
        return (
            json_checkpoint_status(
                path,
                required_columns=required_columns or [],
                required_sheets_min=required_sheets_min,
            ).get("code")
            == "OK"
        )
    return path.stat().st_size > 0


__all__ = [
    "StagingPrerequisiteRequest",
    "staged_input_ready",
    "staging_checkpoint_refs",
    "staging_output_ref",
    "staging_prerequisites_ready",
    "staging_source_ref",
]
