
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


@dataclass(frozen=True)
class RuntimeMemorySchemaOptions:
    """Bundle metadata for stamping runtime memory schema v2 records."""

    name: str
    version: int = RUNTIME_MEMORY_SCHEMA_VERSION
    reserved_keys: tuple[str, ...] = RESERVED_FIELD_KEYS


def runtime_memory_schema_payload(options: RuntimeMemorySchemaOptions) -> dict[str, Any]:
    return {
        "name": options.name,
        "version": options.version,
        "reserved_keys": list(options.reserved_keys),
    }


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
