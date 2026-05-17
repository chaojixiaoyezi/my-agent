# LLM: Compact resume path helpers keep source-ref recommendation logic out of the resume orchestrator.
# 模块用途: 根据 compact metadata、restore refs 和 fail-safe checkpoint 生成手动恢复时的推荐读取路径。

from __future__ import annotations

from typing import Any


# LLM: recommended_compact_resume_paths points humans/models back to facts before continuing work.
# 函数用途: 返回手动恢复时必须优先读取的 compact 产物、fail-safe checkpoint 和原始事实源路径。
def recommended_compact_resume_paths(
    metadata: dict[str, Any], artifacts: dict[str, Any], fail_safe_checkpoints: list[dict[str, Any]]
) -> list[str]:
    refs = metadata.get("refs", {}) if isinstance(metadata.get("refs"), dict) else {}
    paths = [str(value) for value in refs.values() if value]
    paths.extend(str(item.get("path", "") or "") for item in fail_safe_checkpoints)
    paths.extend(_context_bundle_paths(metadata))
    paths.extend(_source_paths(artifacts["restore_refs"]))
    return _dedupe(paths)


# LLM: _source_paths extracts original fact-source paths from restore refs.
# 函数用途: 收集 restore refs 中的 archive、snapshot 和 token ledger 路径，供推荐读取。
def _source_paths(restore_refs: dict[str, Any]) -> list[str]:
    source_refs = restore_refs.get("source_refs", {}) if isinstance(restore_refs, dict) else {}
    return [str(item.get("path", "")) for group in source_refs.values() for item in group if item.get("path")]


# LLM: _context_bundle_paths makes the root run card an early resume read hint.
# 函数用途: 从 metadata.main_context_bundle 提取 context bundle 路径；旧 apply 包没有该字段时返回空。
def _context_bundle_paths(metadata: dict[str, Any]) -> list[str]:
    payload = metadata.get("main_context_bundle", {})
    if not isinstance(payload, dict):
        return []
    ref = str(payload.get("ref", "") or "")
    return [ref] if ref else []


# LLM: _dedupe preserves read order while removing duplicate recommended paths.
# 函数用途: 对推荐读取路径去重，保持 compact 产物优先、源文件随后。
def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


__all__ = ["recommended_compact_resume_paths"]
