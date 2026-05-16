# LLM: Child context helpers keep hierarchy_scheduler focused on scheduling, not context inheritance.
# 模块用途: 决定 runner 创建 child 时使用 child 专属 context 还是父级默认 context。

from __future__ import annotations

from typing import Any


# LLM: child_context_manifest preserves child-specific repair refs without losing the parent fallback.
# 函数用途: child spec 携带 context_manifest/required_read_paths 时优先写入 child task。
def child_context_manifest(parent: Any, spec: Any):
    if getattr(spec, "context_manifest", {}):
        return spec.context_manifest
    return getattr(parent, "context_manifest", None)


# LLM: child_context_packs preserves repair_contract packs for runner-created repair children.
# 函数用途: child spec 有 context_packs 时优先使用，避免 repair_contract 在 schedule parser 边界丢失。
def child_context_packs(parent: Any, spec: Any):
    if getattr(spec, "context_packs", []):
        return spec.context_packs
    return getattr(parent, "context_packs", None)


__all__ = ["child_context_manifest", "child_context_packs"]
