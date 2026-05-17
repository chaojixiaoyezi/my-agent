# LLM: Compact context-bundle refs bridge main-agent run cards into compact/resume packages.
# 模块用途: 读取和摘要主代理 context bundle，让 compact apply/resume 使用结构化任务卡而不是猜范围。

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# LLM: load_main_context_bundle_ref is tolerant so old compact packages remain compatible.
# 函数用途: 读取主代理 context bundle 引用；缺失、损坏或未传入时返回可序列化摘要而不抛异常。
def load_main_context_bundle_ref(workspace: str | Path, ref: str | Path | None) -> dict[str, Any]:
    if not ref:
        return _empty_payload("")
    path = _resolve_ref(workspace, ref)
    if not path.exists():
        return {**_empty_payload(str(path)), "error": "missing_context_bundle"}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {**_empty_payload(str(path)), "error": "invalid_context_bundle"}
    if not isinstance(payload, dict):
        return {**_empty_payload(str(path)), "error": "context_bundle_not_object"}
    return _summary_payload(path, payload)


# LLM: main_context_bundle_source_refs converts the context bundle card into restore source refs.
# 函数用途: 生成 restore_refs.source_refs.context_bundles 的条目，供 resume 推荐读取和自检定位。
def main_context_bundle_source_refs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    ref = str(payload.get("ref", "") or "")
    if not ref:
        return []
    scope = payload.get("scope", {}) if isinstance(payload.get("scope"), dict) else {}
    return [{
        "path": ref,
        "loaded": bool(payload.get("loaded")),
        "schema": str(payload.get("schema", "") or ""),
        "request_id": str(scope.get("request_id", "") or ""),
        "run_id": str(scope.get("run_id", "") or ""),
        "task_id": str(scope.get("task_id", "") or ""),
        "size_bytes": _safe_size(Path(ref)),
        "reserved": {},
    }]


# LLM: compact_context_bundle_summary strips the bundle to fields safe for prompts and continue packets.
# 函数用途: 返回给 handoff、continue packet 和 CLI 的短摘要，不展开任何 artifact 或记忆正文。
def compact_context_bundle_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "ref": str(payload.get("ref", "") or ""),
        "loaded": bool(payload.get("loaded")),
        "schema": str(payload.get("schema", "") or ""),
        "scope": dict(payload.get("scope", {}) if isinstance(payload.get("scope"), dict) else {}),
        "workspace_refs": dict(
            payload.get("workspace_refs", {}) if isinstance(payload.get("workspace_refs"), dict) else {}
        ),
        "task": dict(payload.get("task", {}) if isinstance(payload.get("task"), dict) else {}),
        "error": str(payload.get("error", "") or ""),
    }


# LLM: main_context_bundle_recommended_paths returns read hints without forcing body reads.
# 函数用途: 从 metadata/restore refs 的 context bundle 摘要中取推荐读取路径。
def main_context_bundle_recommended_paths(payload: dict[str, Any]) -> list[str]:
    ref = str(payload.get("ref", "") or "")
    return [ref] if ref else []


# LLM: _summary_payload keeps only stable top-level sections from main_context_bundle.v1.
# 函数用途: 将原始 context bundle 缩成 compact/resume 可嵌入的短摘要。
def _summary_payload(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "ref": str(path),
        "loaded": True,
        "schema": str(payload.get("schema", "") or ""),
        "version": int(payload.get("version", 0) or 0),
        "identity": _dict_section(payload, "identity"),
        "scope": _dict_section(payload, "scope"),
        "workspace_refs": _dict_section(payload, "workspace_refs"),
        "task": _dict_section(payload, "task"),
        "memory_refs": _dict_section(payload, "memory_refs"),
        "recovery_refs": _dict_section(payload, "recovery_refs"),
        "error": "",
        "reserved": {},
    }


# LLM: _empty_payload keeps compact schemas stable when no main context bundle exists.
# 函数用途: 构造缺省 context bundle 摘要，旧 compact 包和非主代理包可继续工作。
def _empty_payload(ref: str) -> dict[str, Any]:
    return {
        "ref": ref,
        "loaded": False,
        "schema": "",
        "version": 0,
        "identity": {},
        "scope": {},
        "workspace_refs": {},
        "task": {},
        "memory_refs": {},
        "recovery_refs": {},
        "error": "",
        "reserved": {},
    }


# LLM: _dict_section avoids leaking unexpected non-object JSON into compact payloads.
# 函数用途: 安全读取指定字典字段；非 dict 字段统一当空对象处理。
def _dict_section(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key, {})
    return dict(value) if isinstance(value, dict) else {}


# LLM: _resolve_ref supports both absolute refs and workspace-relative debug refs.
# 函数用途: 解析 context bundle 路径；相对路径按 compact workspace 处理。
def _resolve_ref(workspace: str | Path, ref: str | Path) -> Path:
    path = Path(ref).expanduser()
    return path if path.is_absolute() else Path(workspace) / path


# LLM: _safe_size keeps missing refs diagnostic-only.
# 函数用途: 读取文件大小；缺失或不可访问时返回 0。
def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


__all__ = [
    "compact_context_bundle_summary",
    "load_main_context_bundle_ref",
    "main_context_bundle_recommended_paths",
    "main_context_bundle_source_refs",
]
