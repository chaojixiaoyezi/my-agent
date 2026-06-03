
from __future__ import annotations

from pathlib import Path
from typing import Any

from .artifact_acceptance import ArtifactAcceptanceRequest, validate_artifact
from .artifact_acceptance_models import ArtifactAcceptanceReport


def lint_artifact_format(
    *,
    path: Path,
    workspace_root: Path | None = None,
    validation_contract: dict[str, Any] | None = None,
) -> ArtifactAcceptanceReport:
    return validate_artifact(
        ArtifactAcceptanceRequest(
            path=path,
            workspace_root=workspace_root,
            validation_contract=validation_contract or {},
        )
    )


__all__ = ["lint_artifact_format"]
