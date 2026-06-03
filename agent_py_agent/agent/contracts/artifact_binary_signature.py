
from __future__ import annotations

from pathlib import Path

from .artifact_acceptance_models import ArtifactFinding, kind_for_path


def binary_signature_finding(path: Path) -> ArtifactFinding | None:
    expected = {
        "png": (b"\x89PNG\r\n\x1a\n",),
        "zip": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
        "gz": (b"\x1f\x8b",),
        "gzip": (b"\x1f\x8b",),
    }.get(kind_for_path(path))
    if expected is None:
        return None
    data = path.read_bytes()[:8]
    if any(data.startswith(signature) for signature in expected):
        return None
    return ArtifactFinding(
        code="ARTIFACT_INVALID_SIGNATURE",
        severity="hard",
        message="Artifact bytes do not match the expected file signature.",
        location=str(path),
        value=kind_for_path(path),
    )


__all__ = ["binary_signature_finding"]
