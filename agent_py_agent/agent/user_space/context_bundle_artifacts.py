# LLM: Context bundle artifact updates attach post-tool refs without copying artifact bodies.
# 模块用途: 在主代理工具循环结束后，把同 scope 的工具输出 artifact refs 补回 context bundle。

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..memory_archive.compact_tool_output_refs import tool_output_source_refs


# LLM: MainContextBundleArtifactUpdateRequest bundles the post-run artifact update scope.
# 类用途: 保存 context bundle 路径、工作区和 request/run/task 范围；调用后才会改写 bundle 文件。
@dataclass(frozen=True)
class MainContextBundleArtifactUpdateRequest:
    context_bundle_path: str
    workspace_root: str | Path
    request_id: str = ""
    run_id: str = ""
    task_id: str = ""


# LLM: update_main_context_bundle_artifacts appends refs-only tool artifacts to the saved bundle.
# 函数用途: 工具循环完成后把同 scope 的 tool-output refs 写回 context bundle；不读取 artifact 正文。
def update_main_context_bundle_artifacts(request: MainContextBundleArtifactUpdateRequest) -> dict[str, Any]:
    path = Path(request.context_bundle_path)
    payload = _read_bundle(path)
    if not payload:
        return {"ok": False, "status": "missing_or_invalid_context_bundle", "artifact_count": 0}
    scope = _scope(request)
    refs = tool_output_source_refs(request.workspace_root, scope) if _has_scope(scope) else []
    if not refs:
        return {"ok": True, "status": "no_matching_tool_output_artifacts", "artifact_count": 0}
    payload["artifact_refs"] = _merged_artifact_refs(payload.get("artifact_refs"), refs)
    _write_bundle(path, payload)
    _rewrite_latest_if_needed(path)
    return {"ok": True, "status": "updated", "artifact_count": len(refs)}


# LLM: _merged_artifact_refs preserves existing request refs and adds tool-output refs by path.
# 函数用途: 合并 context bundle 中已有产物和本轮工具输出产物，按 path/ref 去重。
def _merged_artifact_refs(current: object, refs: list[dict[str, Any]]) -> dict[str, Any]:
    payload = current if isinstance(current, dict) else {}
    items = [item for item in payload.get("items", []) if isinstance(item, dict)]
    seen = {str(item.get("ref") or item.get("path") or "") for item in items}
    for ref in refs:
        path = str(ref.get("path") or "")
        if not path or path in seen:
            continue
        seen.add(path)
        items.append({
            "ref": path,
            "kind": "tool_output",
            "source": "tool_output_index",
            "tool": str(ref.get("tool") or ""),
            "call_id": str(ref.get("call_id") or ""),
            "scoped_call_id": str(ref.get("scoped_call_id") or ""),
            "sha256": str(ref.get("sha256") or ""),
            "size_bytes": int(ref.get("size_bytes", 0) or 0),
            "reserved": {},
        })
    return {
        "items": items,
        "collection_phase": "post_tool_loop",
        "body_policy": "refs_only_read_explicitly",
        "reserved": dict(payload.get("reserved", {}) if isinstance(payload.get("reserved"), dict) else {}),
    }


# LLM: _scope keeps artifact update limited to explicit run/request/task identifiers.
# 函数用途: 构造 tool-output index 查询 scope；全部为空时禁止扫描全量 index。
def _scope(request: MainContextBundleArtifactUpdateRequest) -> dict[str, str]:
    return {
        "request_id": str(request.request_id or "").strip(),
        "run_id": str(request.run_id or "").strip(),
        "task_id": str(request.task_id or "").strip(),
    }


# LLM: _has_scope prevents unscoped runs from importing all historical tool outputs.
# 函数用途: 只有 request/run/task 至少一个字段存在时才允许查询 tool output index。
def _has_scope(scope: dict[str, str]) -> bool:
    return any(scope.values())


# LLM: _read_bundle is a tolerant JSON object reader for bundle update.
# 函数用途: 读取 context bundle JSON；坏文件返回空对象而不是让 finalization 崩溃。
def _read_bundle(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


# LLM: _write_bundle preserves deterministic JSON formatting.
# 函数用途: 写回更新后的 context bundle JSON，不处理 Markdown 正文。
def _write_bundle(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


# LLM: _rewrite_latest_if_needed keeps same-day latest mirror consistent with the source bundle.
# 函数用途: 如果当前 bundle 就在 context_bundles 日期目录下，同步 latest_context_bundle.json。
def _rewrite_latest_if_needed(path: Path) -> None:
    latest = path.parent / "latest_context_bundle.json"
    if latest.exists():
        latest.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")


__all__ = ["MainContextBundleArtifactUpdateRequest", "update_main_context_bundle_artifacts"]
