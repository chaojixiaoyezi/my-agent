
from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path

from .artifact_acceptance_models import kind_for_path


@dataclass(frozen=True)
class ArtifactCapability:
    kind: str
    extension: str
    mime_type: str
    validator_key: str = ""
    builder_key: str = ""
    known: bool = False

    @property
    def binary_like(self) -> bool:
        return bool(self.mime_type and not self.mime_type.startswith(("text/", "application/json", "application/xml")))


_KNOWN_VALIDATORS = {
    "csv": "csv",
    "docx": "docx",
    "htm": "html",
    "html": "html",
    "json": "json",
    "markdown": "md",
    "md": "md",
    "pdf": "pdf",
    "txt": "txt",
    "xlsx": "xlsx",
}

_KNOWN_BUILDERS: dict[str, str] = {}


def artifact_capability(path: str | Path, *, declared_kind: str = "", declared_mime: str = "") -> ArtifactCapability:
    artifact_path = Path(path)
    kind = _normalize_kind(declared_kind) or kind_for_path(artifact_path)
    extension = artifact_path.suffix.lower().lstrip(".")
    mime_type = declared_mime.strip() or _mime_for_path(artifact_path)
    validator_key = _KNOWN_VALIDATORS.get(kind) or _KNOWN_VALIDATORS.get(extension, "")
    builder_key = _KNOWN_BUILDERS.get(kind) or _KNOWN_BUILDERS.get(extension, "")
    return ArtifactCapability(
        kind=kind,
        extension=extension,
        mime_type=mime_type,
        validator_key=validator_key,
        builder_key=builder_key,
        known=bool(validator_key or builder_key),
    )


def _normalize_kind(value: str) -> str:
    return value.strip().lower().lstrip(".")


def _mime_for_path(path: Path) -> str:
    guessed, _encoding = mimetypes.guess_type(str(path))
    return guessed or ""


__all__ = [
    "ArtifactCapability",
    "artifact_capability",
]
