
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .delivery_contract_fields import string_items
from .recovery_actions import RecoveryAction

SCHEMA_VERSION = "delivery_contract.v1"
DOCTOR_SCHEMA_VERSION = "delivery_contract_doctor.v1"
_ARTIFACT_PATH_KEYS = ("preferred_path", "path")
_ROOT_LIST_KEYS = ("allowed_output_roots", "search_roots", "artifact_roots")
_EXTENSION_KEYS = (
    "preferred_extension",
    "preferred_extensions",
    "acceptable_extension",
    "acceptable_extensions",
    "accepted_extension",
    "accepted_extensions",
    "extension",
    "extensions",
    "file_extension",
    "file_extensions",
)


@dataclass(frozen=True)
class ContractFinding:
    code: str
    severity: str
    location: str
    message: str
    value: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "location": self.location,
            "message": self.message,
            "value": self.value,
        }


@dataclass(frozen=True)
class ContractDoctorReport:
    ok: bool
    findings: list[ContractFinding] = field(default_factory=list)
    normalized_contract: dict[str, Any] = field(default_factory=dict)
    repair_actions: list[dict[str, object]] = field(default_factory=list)
    should_rematerialize: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": DOCTOR_SCHEMA_VERSION,
            "ok": self.ok,
            "findings": [finding.to_dict() for finding in self.findings],
            "normalized_contract": self.normalized_contract,
            "repair_actions": self.repair_actions,
            "should_rematerialize": self.should_rematerialize,
        }


def validate_delivery_contract(payload: object, *, workspace_root: Path | None = None) -> ContractDoctorReport:
    if not isinstance(payload, dict):
        findings = [_finding("DELIVERY_CONTRACT_NOT_OBJECT", "hard", "$", value=type(payload).__name__)]
        return _report({}, findings)

    normalized: dict[str, Any] = dict(payload)
    findings: list[ContractFinding] = []
    _check_schema_version(payload, findings)
    artifacts = payload.get("artifacts")
    if artifacts is None:
        if _allows_no_artifact_delivery(payload):
            artifacts = []
            normalized["artifacts"] = []
        else:
            findings.append(_finding("DELIVERY_CONTRACT_ARTIFACTS_MISSING", "hard", "artifacts"))
            return _report(normalized, findings)
    if not isinstance(artifacts, list):
        findings.append(_finding("DELIVERY_CONTRACT_ARTIFACTS_NOT_LIST", "hard", "artifacts", value=type(artifacts).__name__))
        return _report(normalized, findings)

    normalized_artifacts: list[dict[str, Any]] = []
    for index, artifact in enumerate(artifacts):
        normalized_artifact, artifact_findings = _validate_artifact_contract(artifact, index, workspace_root)
        findings.extend(artifact_findings)
        if normalized_artifact is not None:
            normalized_artifacts.append(normalized_artifact)
    normalized["schema_version"] = SCHEMA_VERSION
    normalized["artifacts"] = normalized_artifacts
    findings.extend(_validate_optional_dict(payload, "delivery_quality_contract"))
    findings.extend(_validate_optional_dict(payload, "fact_evidence_contract"))
    findings.extend(_validate_optional_dict(payload, "target_coverage_contract"))
    findings.extend(_validate_optional_dict(payload, "bootstrap_contract"))
    return _report(normalized, findings)


def _allows_no_artifact_delivery(payload: dict[str, Any]) -> bool:
    for key in ("requires_artifact", "artifact_required", "requires_disk_artifact", "disk_artifact_required"):
        if payload.get(key) is False:
            return True
    mode = str(payload.get("delivery_mode") or payload.get("output_mode") or "").strip().lower()
    return mode in {"message", "answer", "summary", "no_artifact", "no-artifact", "none"}


def validate_recovery_action(payload: object) -> ContractDoctorReport:
    if not isinstance(payload, dict):
        return _report({}, [_finding("RECOVERY_ACTION_NOT_OBJECT", "hard", "$", value=type(payload).__name__)])
    findings: list[ContractFinding] = []
    if not str(payload.get("code") or "").strip():
        findings.append(_finding("RECOVERY_ACTION_CODE_REQUIRED", "hard", "code"))
    if not str(payload.get("recommended_action") or "").strip():
        findings.append(_finding("RECOVERY_ACTION_RECOMMENDED_ACTION_REQUIRED", "hard", "recommended_action"))
    if "retryable" in payload and not isinstance(payload.get("retryable"), bool):
        findings.append(_finding("RECOVERY_ACTION_RETRYABLE_NOT_BOOL", "hard", "retryable", value=type(payload.get("retryable")).__name__))
    if "repair_targets" in payload and not _is_nonempty_string_list(payload.get("repair_targets")):
        findings.append(_finding("RECOVERY_ACTION_REPAIR_TARGETS_INVALID", "hard", "repair_targets"))
    return _report(dict(payload), findings)


def _check_schema_version(payload: dict[str, Any], findings: list[ContractFinding]) -> None:
    version = str(payload.get("schema_version") or SCHEMA_VERSION).strip()
    if version and version != SCHEMA_VERSION:
        findings.append(_finding("DELIVERY_CONTRACT_SCHEMA_VERSION_MISMATCH", "warning", "schema_version", value=version))


def _validate_artifact_contract(
    artifact: object,
    index: int,
    workspace_root: Path | None,
) -> tuple[dict[str, Any] | None, list[ContractFinding]]:
    location = f"artifacts[{index}]"
    if not isinstance(artifact, dict):
        return None, [_finding("DELIVERY_CONTRACT_ARTIFACT_NOT_OBJECT", "hard", location, value=type(artifact).__name__)]
    normalized = dict(artifact)
    findings: list[ContractFinding] = []
    if "artifact_id" in artifact and not str(artifact.get("artifact_id") or "").strip():
        findings.append(_finding("DELIVERY_CONTRACT_ARTIFACT_ID_EMPTY", "hard", f"{location}.artifact_id"))
    kind = str(artifact.get("kind") or "").strip().lower()
    if kind:
        normalized["kind"] = kind
    if not _has_explicit_path(artifact) and not kind and not _has_extension_intent(artifact):
        findings.append(_finding("DELIVERY_CONTRACT_ARTIFACT_TARGET_UNDECLARED", "hard", location))
    findings.extend(_validate_root_lists(artifact, location))
    findings.extend(_validate_extension_fields(artifact, location))
    findings.extend(_validate_artifact_intent(artifact.get("artifact_intent"), location))
    validation_contract, validation_findings = _validate_validation_contract(artifact.get("validation_contract"), location)
    findings.extend(validation_findings)
    if validation_contract is not None:
        normalized["validation_contract"] = validation_contract
    findings.extend(_path_findings(artifact, workspace_root, location))
    return normalized, findings


def _validate_validation_contract(value: object, artifact_location: str) -> tuple[dict[str, Any] | None, list[ContractFinding]]:
    if value is None:
        return None, []
    if not isinstance(value, dict):
        return None, [_finding("VALIDATION_CONTRACT_NOT_OBJECT", "hard", f"{artifact_location}.validation_contract", value=type(value).__name__)]
    normalized = dict(value)
    findings: list[ContractFinding] = []
    if "min_size" in value and not _non_negative_int(value.get("min_size")):
        findings.append(_finding("VALIDATION_CONTRACT_MIN_SIZE_INVALID", "hard", f"{artifact_location}.validation_contract.min_size"))
    for key in ("required_sections", "required_files", "required_sheets", "required_columns"):
        if key not in value:
            continue
        items = string_items(value.get(key), allow_named_dict=key == "required_columns")
        if not items:
            findings.append(_finding("VALIDATION_CONTRACT_STRING_LIST_INVALID", "hard", f"{artifact_location}.validation_contract.{key}"))
            continue
        normalized[key] = items
    for key in ("collection_contract", "staging_contract", "quality_contract", "evidence_contract"):
        if key in value and not isinstance(value.get(key), dict):
            findings.append(_finding("VALIDATION_CONTRACT_NESTED_CONTRACT_INVALID", "hard", f"{artifact_location}.validation_contract.{key}"))
    for key in _EXTENSION_KEYS:
        if key in value and not _extension_value(value.get(key)):
            findings.append(_finding("VALIDATION_CONTRACT_EXTENSION_INVALID", "hard", f"{artifact_location}.validation_contract.{key}"))
    return normalized, findings


def _validate_root_lists(artifact: dict[str, Any], location: str) -> list[ContractFinding]:
    findings: list[ContractFinding] = []
    for key in _ROOT_LIST_KEYS:
        if key in artifact and not _is_nonempty_string_list(artifact.get(key)):
            findings.append(_finding("DELIVERY_CONTRACT_ROOTS_INVALID", "hard", f"{location}.{key}"))
    return findings


def _validate_extension_fields(artifact: dict[str, Any], location: str) -> list[ContractFinding]:
    findings: list[ContractFinding] = []
    for key in _EXTENSION_KEYS:
        if key in artifact and not _extension_value(artifact.get(key)):
            findings.append(_finding("DELIVERY_CONTRACT_EXTENSION_INVALID", "hard", f"{location}.{key}"))
    return findings


def _validate_artifact_intent(value: object, location: str) -> list[ContractFinding]:
    if value is None:
        return []
    if not isinstance(value, dict):
        return [_finding("DELIVERY_CONTRACT_ARTIFACT_INTENT_INVALID", "hard", f"{location}.artifact_intent", value=type(value).__name__)]
    findings: list[ContractFinding] = []
    for key in _EXTENSION_KEYS:
        if key in value and not _extension_value(value.get(key)):
            findings.append(_finding("DELIVERY_CONTRACT_ARTIFACT_INTENT_EXTENSION_INVALID", "hard", f"{location}.artifact_intent.{key}"))
    return findings


def _validate_optional_dict(payload: dict[str, Any], key: str) -> list[ContractFinding]:
    if key not in payload or isinstance(payload.get(key), dict):
        return []
    return [_finding("DELIVERY_CONTRACT_OPTIONAL_MAP_INVALID", "hard", key, value=type(payload.get(key)).__name__)]


def _path_findings(artifact: dict[str, Any], workspace_root: Path | None, location: str) -> list[ContractFinding]:
    findings: list[ContractFinding] = []
    for key in _ARTIFACT_PATH_KEYS:
        raw = str(artifact.get(key) or "").strip()
        if raw and not _path_resolves(raw, workspace_root):
            findings.append(_finding("DELIVERY_CONTRACT_ARTIFACT_PATH_INVALID", "hard", f"{location}.{key}", value=raw))
    return findings


def _path_resolves(raw: str, workspace_root: Path | None) -> bool:
    candidate = Path(raw).expanduser()
    try:
        target = candidate if candidate.is_absolute() or workspace_root is None else Path(workspace_root).resolve(strict=False) / candidate
        target.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _report(normalized: dict[str, Any], findings: list[ContractFinding]) -> ContractDoctorReport:
    hard_findings = [finding for finding in findings if finding.severity == "hard"]
    should_rematerialize = bool(hard_findings)
    repair_actions = [_rematerialize_action(findings)] if should_rematerialize else []
    return ContractDoctorReport(
        ok=not hard_findings,
        findings=findings,
        normalized_contract=normalized,
        repair_actions=repair_actions,
        should_rematerialize=should_rematerialize,
    )


def _rematerialize_action(findings: list[ContractFinding]) -> dict[str, object]:
    return {
        "code": "DELIVERY_CONTRACT_REMATERIALIZATION_REQUIRED",
        "category": "contract",
        "retryable": True,
        "recommended_action": RecoveryAction.REPAIR_EFFECTIVE_CONTRACT.value,
        "finding_codes": [finding.code for finding in findings],
    }


def _has_explicit_path(artifact: dict[str, Any]) -> bool:
    return any(str(artifact.get(key) or "").strip() for key in _ARTIFACT_PATH_KEYS)


def _has_extension_intent(artifact: dict[str, Any]) -> bool:
    if any(_extension_value(artifact.get(key)) for key in _EXTENSION_KEYS if key in artifact):
        return True
    intent = artifact.get("artifact_intent")
    return isinstance(intent, dict) and any(_extension_value(intent.get(key)) for key in _EXTENSION_KEYS if key in intent)


def _is_nonempty_string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)


def _extension_value(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return _is_nonempty_string_list(value)


def _non_negative_int(value: object) -> bool:
    return isinstance(value, int) and value >= 0


def _finding(code: str, severity: str, location: str, *, value: str = "") -> ContractFinding:
    return ContractFinding(
        code=code,
        severity=severity,
        location=location,
        message=code.lower(),
        value=value,
    )


__all__ = [
    "ContractDoctorReport",
    "ContractFinding",
    "DOCTOR_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "validate_delivery_contract",
    "validate_recovery_action",
]
