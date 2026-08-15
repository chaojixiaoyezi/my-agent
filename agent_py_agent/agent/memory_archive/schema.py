
from __future__ import annotations

"""schema v2 helpers for runtime memory records.

Human version:
Runtime memory writes several small JSON/JSONL records. These helpers stamp
the current schema name and version on each record.
"""

from dataclasses import dataclass
from typing import Any

RUNTIME_MEMORY_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class RuntimeMemorySchemaOptions:
    """Bundle metadata for stamping runtime memory schema v2 records."""

    name: str
    version: int = RUNTIME_MEMORY_SCHEMA_VERSION


def runtime_memory_schema_payload(options: RuntimeMemorySchemaOptions) -> dict[str, Any]:
    return {
        "name": options.name,
        "version": options.version,
    }


__all__ = [
    "RUNTIME_MEMORY_SCHEMA_VERSION",
    "RuntimeMemorySchemaOptions",
    "runtime_memory_schema_payload",
]
