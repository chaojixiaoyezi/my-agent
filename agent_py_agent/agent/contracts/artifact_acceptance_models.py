
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from ..action_protocol_core import ArtifactRef
from .contract_validation_recovery import recovery_for_findings


@dataclass(frozen=True)
class ArtifactAcceptanceRequest:
    path: Path
    workspace_root: Path | None = None
    validation_contract: dict[str, object] | None = None


@dataclass(frozen=True)
class ArtifactFinding:
    code: str
    severity: str
    message: str
    location: str = ""
    value: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "location": self.location,
            "value": self.value,
        }


def advisory_artifact_finding(finding: ArtifactFinding) -> ArtifactFinding:
    """Return a non-blocking copy of a quality or coverage finding."""

    return ArtifactFinding(
        code=finding.code,
        severity="warning",
        message=finding.message,
        location=finding.location,
        value=finding.value,
    )


def advisory_artifact_findings(findings: list[ArtifactFinding]) -> list[ArtifactFinding]:
    """Downgrade subjective/content contract findings to closeout warnings."""

    return [advisory_artifact_finding(item) for item in findings]


@dataclass(frozen=True)
class ArtifactAcceptanceReport:
    ok: bool
    artifact_ref: str
    artifact_kind: str = "generic"
    findings: list[ArtifactFinding] = field(default_factory=list)
    recovery: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        payload = {
            "ok": self.ok,
            "artifact_ref": self.artifact_ref,
            "artifact_ref_payload": artifact_ref_payload(self.artifact_ref, self.artifact_kind).to_dict(),
            "artifact_kind": self.artifact_kind,
            "findings": [item.to_dict() for item in self.findings],
        }
        recovery = self.recovery or recovery_for_findings("artifact_acceptance", payload["findings"])
        if recovery is not None:
            payload["recovery"] = recovery
        return payload


def artifact_ref_payload(path: str | Path, kind: str = "") -> ArtifactRef:
    artifact_path = Path(path)
    digest = _artifact_hash(artifact_path)
    suffix_kind = kind or kind_for_path(artifact_path)
    return ArtifactRef(
        artifact_id=_artifact_id(artifact_path, digest),
        path=str(artifact_path),
        kind=suffix_kind,
        hash=digest,
        reserved={"size_bytes": _artifact_size(artifact_path)},
    )


def kind_for_path(path: Path) -> str:
    return path.suffix.lower().lstrip(".") or "generic"


def _artifact_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _artifact_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _artifact_id(path: Path, digest: str) -> str:
    seed = f"{path.resolve(strict=False)}:{digest}"
    return f"artifact:{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:16]}"


__all__ = [
    "ArtifactAcceptanceReport",
    "ArtifactAcceptanceRequest",
    "ArtifactFinding",
    "advisory_artifact_finding",
    "advisory_artifact_findings",
    "artifact_ref_payload",
    "kind_for_path",
]
