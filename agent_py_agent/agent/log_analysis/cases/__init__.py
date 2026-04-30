from __future__ import annotations

from typing import Any


def __getattr__(name: str) -> Any:
    if name == "LocalEvidenceStore":
        from .evidence import LocalEvidenceStore

        return LocalEvidenceStore
    raise AttributeError(name)

__all__ = ["LocalEvidenceStore"]
