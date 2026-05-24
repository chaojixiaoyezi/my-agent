# LLM: Artifact validator registry turns format-specific checks into pluggable generic validators.
# 模块用途: 统一根据 validation_contract 和 artifact kind 选择验收器，避免合同层写死专项分支。

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


# LLM: validator_name_from_contract reads only machine fields from validation_contract.
# 函数用途: 从结构化合同里拿 validator 名称；缺失时返回空字符串，不读自然语言说明。
def validator_name_from_contract(validation_contract: dict[str, object] | None) -> str:
    value = (validation_contract or {}).get("validator")
    return str(value or "").strip().lower()


# LLM: resolve_artifact_validator selects one validator from registry without branching on prompt prose.
# 函数用途: 优先按 validation_contract.validator 选验收器，找不到再按 artifact kind 选默认验收器。
def resolve_artifact_validator(
    request: ArtifactAcceptanceRequest,
    *,
    named_validators: dict[str, ArtifactValidator],
    kind_validators: dict[str, ArtifactValidator],
    fallback: ArtifactValidator,
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
    return kind_validators.get(artifact_kind, fallback)


__all__ = [
    "ArtifactValidator",
    "resolve_artifact_validator",
    "validator_name_from_contract",
]
