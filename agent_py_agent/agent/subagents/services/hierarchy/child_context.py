
from __future__ import annotations

from typing import Any


def child_context_manifest(parent: Any, spec: Any):
    if getattr(spec, "context_manifest", {}):
        return spec.context_manifest
    return getattr(parent, "context_manifest", None)


def child_context_packs(parent: Any, spec: Any):
    if getattr(spec, "context_packs", []):
        return spec.context_packs
    return getattr(parent, "context_packs", None)


__all__ = ["child_context_manifest", "child_context_packs"]
