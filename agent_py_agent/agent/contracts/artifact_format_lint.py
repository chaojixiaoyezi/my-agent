# LLM: Artifact format lint is the unified entrypoint over existing artifact validators.
# 模块用途: 收口 JSON/Markdown/HTML/PDF/DOCX/XLSX/CSV 等格式检查，避免 closeout 到处直接找专项门。

from __future__ import annotations

from pathlib import Path
from typing import Any

from .artifact_acceptance import ArtifactAcceptanceRequest, validate_artifact
from .artifact_acceptance_models import ArtifactAcceptanceReport


# LLM: lint_artifact_format reuses existing validators and preserves their structured finding shape.
# 函数用途: 统一从 path/workspace/contract 进入格式 lint，内部仍复用已有 artifact acceptance 能力。
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
