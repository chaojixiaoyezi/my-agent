# LLM: Binary signature checks keep generic artifact acceptance from trusting non-empty corrupt files.
# 模块用途: 对 PNG/ZIP/GZIP 这类常见二进制产物做轻量 magic-bytes 校验。

from __future__ import annotations

from pathlib import Path

from .artifact_acceptance_models import ArtifactFinding, kind_for_path


# LLM: binary_signature_finding validates known binary suffixes with magic bytes.
# 函数用途: 返回二进制签名错误 finding；未知后缀返回 None 交给通用非空检查。
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
