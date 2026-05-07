# LLM: Runtime memory schema helpers; keep schema versioning explicit and shared by all lightweight indexes.
# 模块用途: 定义 runtime memory v2 的版本号、schema payload 和 reserved 字段形状。

from __future__ import annotations

"""schema v2 helpers for runtime memory records.

Human version:
Runtime memory writes several small JSON/JSONL records. These helpers keep the
top-level schema version and the reserved extension block consistent, so future
fields can be added without guessing which record shape is safe to extend.
"""

from dataclasses import dataclass
from typing import Any

RUNTIME_MEMORY_SCHEMA_VERSION = 2
RESERVED_FIELD_KEYS = ("extensions", "compat", "future")


# LLM: RuntimeMemorySchemaOptions is the bundle for schema metadata, avoiding loose name/version pairs.
# 类用途: 描述一个 runtime memory 记录族的 schema 名称、版本和允许保留扩展字段。
@dataclass(frozen=True)
class RuntimeMemorySchemaOptions:
    """Bundle metadata for stamping runtime memory schema v2 records."""

    name: str
    version: int = RUNTIME_MEMORY_SCHEMA_VERSION
    reserved_keys: tuple[str, ...] = RESERVED_FIELD_KEYS


# LLM: runtime_memory_schema_payload returns the small schema block stored beside version.
# 函数用途: 生成机器可读 schema 描述，说明记录名、版本和 reserved 中允许的扩展槽。
def runtime_memory_schema_payload(options: RuntimeMemorySchemaOptions) -> dict[str, Any]:
    return {
        "name": options.name,
        "version": options.version,
        "reserved_keys": list(options.reserved_keys),
    }


# LLM: runtime_memory_reserved_fields creates the stable v2 reserved block; keep new fields inside its slots.
# 函数用途: 生成统一 reserved 字段，预留 extensions、compat、future 三类后续扩展空间。
def runtime_memory_reserved_fields(options: RuntimeMemorySchemaOptions) -> dict[str, Any]:
    return {
        "schema_name": options.name,
        "schema_version": options.version,
        "extensions": {},
        "compat": {},
        "future": {},
    }


__all__ = [
    "RESERVED_FIELD_KEYS",
    "RUNTIME_MEMORY_SCHEMA_VERSION",
    "RuntimeMemorySchemaOptions",
    "runtime_memory_reserved_fields",
    "runtime_memory_schema_payload",
]
