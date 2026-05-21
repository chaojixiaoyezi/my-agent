# LLM: Staging helpers keep delivery recovery generic across JSON, Markdown, workbook, and PDF builders.
# 模块用途: 读取 staging_contract 的结构化 source/output/checkpoint 字段，并判断 builder 前置阶段是否可用。

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..contracts.staged_checkpoint_acceptance import json_checkpoint_status
from .main_agent_delivery_closeout_artifacts import _artifact_path


# LLM: StagingPrerequisiteRequest bundles builder prerequisite inputs.
# 类用途: 避免 staging_prerequisites_ready 的参数继续扩散。
@dataclass(frozen=True)
class StagingPrerequisiteRequest:
    staging: dict[str, Any]
    workspace_root: Path
    output_ref: str
    source_ref: str
    required_columns: list[str]
    required_sheets_min: int = 0


# LLM: staging_checkpoint_refs normalizes staging checkpoint refs.
# 函数用途: 提取非空字符串 checkpoint 引用，供恢复动作按顺序检查。
def staging_checkpoint_refs(staging: dict[str, Any]) -> list[str]:
    refs = staging.get("checkpoint_refs")
    if not isinstance(refs, list):
        return []
    return [text for ref in refs if (text := str(ref or "").strip())]


# LLM: staging_source_ref reads source refs by structured key aliases.
# 函数用途: 支持 JSON、Markdown 和未来通用 source_ref，不从任务描述里猜产物路径。
def staging_source_ref(staging: dict[str, Any]) -> str:
    for key in ("source_json_ref", "source_markdown_ref", "source_ref"):
        if value := str(staging.get(key) or "").strip():
            return value
    return ""


# LLM: staging_output_ref reads builder output refs by structured key aliases.
# 函数用途: 支持 workbook/pdf/output_ref，缺省时回退到 artifact preferred_path。
def staging_output_ref(staging: dict[str, Any], artifact: dict[str, Any]) -> str:
    for key in ("workbook_ref", "pdf_ref", "output_ref"):
        if value := str(staging.get(key) or "").strip():
            return value
    return str(artifact.get("preferred_path") or artifact.get("path") or "").strip()


# LLM: staging_prerequisites_ready keeps builder actions behind declared staged inputs.
# 函数用途: 检查 output_ref 之前的 checkpoint_refs 都已存在且质量可用。
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


# LLM: staged_input_ready validates one staged input by file kind.
# 函数用途: JSON checkpoint 走结构化行/列检查；非 JSON checkpoint 至少要求存在且非空。
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
