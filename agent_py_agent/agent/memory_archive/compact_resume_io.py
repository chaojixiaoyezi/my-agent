# LLM: Compact resume IO helpers read only declared apply artifacts and never mutate memory files.
# 模块用途: 为 compact resume 解析 apply 引用、读取 metadata 和关联产物，保持主恢复流程薄编排。

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# LLM: resolve_compact_metadata_path accepts either an apply id or a direct compact apply artifact path.
# 函数用途: 将用户传入的 apply_id/metadata/apply_bundle 路径解析成 metadata JSON 路径。
def resolve_compact_metadata_path(workspace: Path, apply_ref: str) -> Path:
    candidate = Path(apply_ref).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    if candidate.exists():
        return _metadata_path_from_existing(candidate)
    indexed = _metadata_path_from_global_ledger(workspace, apply_ref)
    if indexed is not None and indexed.exists():
        return indexed
    return workspace / "memory_archive" / "compact_applies" / f"{apply_ref}.json"


# LLM: read_compact_apply_artifacts loads only refs declared by compact apply metadata.
# 函数用途: 读取 metadata 指向的 apply bundle、restore refs、work state、self-check 和 compact context。
def read_compact_apply_artifacts(metadata: dict[str, Any]) -> dict[str, Any]:
    refs = metadata.get("refs", {}) if isinstance(metadata.get("refs"), dict) else {}
    # 函数用途: main_context_bundle 是恢复任务卡引用，缺失时返回空对象保持旧 apply 包兼容。
    return {
        "apply_bundle": _read_json_path(refs.get("apply_bundle")),
        "restore_refs": _read_json_path(refs.get("restore_refs")),
        "work_state": _read_json_path(refs.get("work_state_snapshot")),
        "compaction_state": _read_json_path(refs.get("compaction_state")),
        "handoff_summary": _read_text_path(refs.get("handoff_summary")),
        "self_check": _read_json_path(refs.get("post_compact_self_check")),
        "compact_context": _read_text_path(refs.get("compact_context")),
        "main_context_bundle": _read_json_path(refs.get("main_context_bundle")),
    }


# LLM: read_json_object is the strict local JSON reader for compact resume artifacts.
# 函数用途: 读取 JSON 对象；文件缺失、损坏或不是对象时返回空 dict。
def read_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


# LLM: _metadata_path_from_existing normalizes direct paths without reading unrelated files.
# 函数用途: 当用户传入 apply_bundle/self_check/work_state 等路径时，回推同一 apply 的 metadata 路径。
def _metadata_path_from_existing(path: Path) -> Path:
    name = path.name
    for suffix in (
        ".apply_bundle.json",
        ".restore_refs.json",
        ".work_state_snapshot.json",
        ".self_check.json",
        ".self_check_failed.json",
    ):
        if name.endswith(suffix):
            return path.with_name(name[: -len(suffix)] + ".json")
    return path


# LLM: _metadata_path_from_global_ledger lets compact resume find run-local apply files by apply_id.
# 函数用途: compact 产物已按 run 分目录存放；这里用全局索引把短 apply_id 解析回 metadata 路径。
def _metadata_path_from_global_ledger(workspace: Path, apply_ref: str) -> Path | None:
    target = str(apply_ref or "").strip()
    if not target:
        return None
    ledger = workspace / "memory_archive" / "compact_applies" / "ledger.jsonl"
    for record in _read_jsonl_dicts(ledger):
        if str(record.get("apply_id") or record.get("event_id") or "") != target:
            continue
        refs = record.get("refs", {}) if isinstance(record.get("refs"), dict) else {}
        path = Path(str(refs.get("metadata") or ""))
        if path.exists():
            return path
    return None


def _read_jsonl_dicts(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


# LLM: _read_json_path reads optional JSON refs and returns an empty object on missing/invalid files.
# 函数用途: 安全读取 compact apply 产物 JSON，缺失时让 consistency report 阻断而不是崩溃。
def _read_json_path(value: object) -> dict[str, Any]:
    return read_json_object(Path(str(value))) if value else {}


# LLM: _read_text_path reads small compact context markdown refs.
# 函数用途: 读取 compact context 文本，缺失时返回空字符串并交给一致性检查处理。
def _read_text_path(value: object) -> str:
    if not value:
        return ""
    try:
        return Path(str(value)).read_text(encoding="utf-8")
    except OSError:
        return ""


__all__ = ["read_compact_apply_artifacts", "read_json_object", "resolve_compact_metadata_path"]
