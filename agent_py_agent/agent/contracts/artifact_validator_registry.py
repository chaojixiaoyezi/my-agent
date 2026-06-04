
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .artifact_acceptance_models import (
    ArtifactAcceptanceReport,
    ArtifactAcceptanceRequest,
    kind_for_path,
)
from .artifact_capabilities import artifact_capability

ArtifactValidator = Callable[[ArtifactAcceptanceRequest], ArtifactAcceptanceReport]


def validator_name_from_contract(validation_contract: dict[str, object] | None) -> str:
    value = (validation_contract or {}).get("validator")
    return str(value or "").strip().lower()


def resolve_artifact_validator(
    request: ArtifactAcceptanceRequest,
    *,
    named_validators: dict[str, ArtifactValidator],
    kind_validators: dict[str, ArtifactValidator],
    default_validator: ArtifactValidator,
) -> ArtifactValidator:
    validator_name = validator_name_from_contract(request.validation_contract)
    if validator_name and validator_name in named_validators:
        return named_validators[validator_name]
    validation = request.validation_contract or {}
    capability = artifact_capability(
        Path(request.path),
        declared_kind=str(validation.get("artifact_kind") or validation.get("kind") or ""),
        declared_mime=str(validation.get("mime_type") or validation.get("mime") or ""),
    )
    artifact_kind = capability.validator_key or capability.kind or kind_for_path(Path(request.path))
    return kind_validators.get(artifact_kind, default_validator)


__all__ = [
    "ArtifactValidator",
    "resolve_artifact_validator",
    "validator_name_from_contract",
]
