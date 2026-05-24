# LLM: Artifact capabilities keep artifact handling open-world while still giving known formats strong validators.
# 模块用途: 将产物后缀、MIME、默认 validator 归一到可扩展 capability；未知格式走通用兜底而不是直接拒绝。

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

_KNOWN_BUILDERS = {
    "xlsx": "data_to_workbook",
    "pdf": "markdown_to_pdf",
}


# LLM: artifact_capability derives a best-effort capability without treating unknown suffixes as invalid.
# 函数用途: 已知格式返回专门 validator/builder；未知格式保留 kind/MIME 并交给通用验收兜底。
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
