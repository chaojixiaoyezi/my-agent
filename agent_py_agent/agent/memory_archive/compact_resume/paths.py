
from __future__ import annotations

from typing import Any

from ...common.value_parsing import dedupe_strings


def recommended_compact_resume_paths(
    metadata: dict[str, Any], artifacts: dict[str, Any], fail_safe_checkpoints: list[dict[str, Any]]
) -> list[str]:
    refs = metadata.get("refs", {}) if isinstance(metadata.get("refs"), dict) else {}
    paths = [str(value) for value in refs.values() if value]
    paths.extend(str(item.get("path", "") or "") for item in fail_safe_checkpoints)
    paths.extend(_context_bundle_paths(metadata))
    paths.extend(_source_paths(artifacts["restore_refs"]))
    return dedupe_strings(paths)


def _source_paths(restore_refs: dict[str, Any]) -> list[str]:
    source_refs = restore_refs.get("source_refs", {}) if isinstance(restore_refs, dict) else {}
    return [str(item.get("path", "")) for group in source_refs.values() for item in group if item.get("path")]


def _context_bundle_paths(metadata: dict[str, Any]) -> list[str]:
    payload = metadata.get("main_context_bundle", {})
    if not isinstance(payload, dict):
        return []
    ref = str(payload.get("ref", "") or "")
    return [ref] if ref else []


__all__ = ["recommended_compact_resume_paths"]
