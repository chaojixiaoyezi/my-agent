# LLM: Delivery contract doctor validates machine contracts before runtime gates consume them.
# 模块用途: 对 delivery_contract / recovery_action 做轻量 schema 校验，输出结构化 findings 和返工动作。

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


# LLM: ContractFinding is the stable machine finding shape for contract doctor checks.
# 类用途: 保存合同校验 code/severity/location/value，供 closeout、测试套件和返工循环消费。
@dataclass(frozen=True)
class ContractFinding:
    code: str
    severity: str
    location: str
    message: str
    value: str = ""

    # LLM: to_dict serializes one finding without exposing dataclass internals.
    # 函数用途: 把合同 finding 转成 JSON 友好的 dict。
    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "location": self.location,
            "message": self.message,
            "value": self.value,
        }


# LLM: ContractDoctorReport is the reusable report shape for schema and doctor gates.
# 类用途: 汇总合同是否可运行、校验 findings、规范化合同和建议返工动作。
@dataclass(frozen=True)
class ContractDoctorReport:
    ok: bool
    findings: list[ContractFinding] = field(default_factory=list)
    normalized_contract: dict[str, Any] = field(default_factory=dict)
    repair_actions: list[dict[str, object]] = field(default_factory=list)
    should_rematerialize: bool = False

    # LLM: to_dict keeps doctor reports stable for prompt injection and JSON snapshots.
    # 函数用途: 输出结构化 doctor 报告，不依赖自然语言错误文本。
    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": DOCTOR_SCHEMA_VERSION,
            "ok": self.ok,
            "findings": [finding.to_dict() for finding in self.findings],
            "normalized_contract": self.normalized_contract,
            "repair_actions": self.repair_actions,
            "should_rematerialize": self.should_rematerialize,
        }


# LLM: validate_delivery_contract checks the delivery contract itself, not the artifact contents.
# 函数用途: 在运行/验收前校验合同结构、路径边界和基础字段类型。
def validate_delivery_contract(payload: object, *, workspace_root: Path | None = None) -> ContractDoctorReport:
    if not isinstance(payload, dict):
        findings = [_finding("DELIVERY_CONTRACT_NOT_OBJECT", "hard", "$", value=type(payload).__name__)]
        return _report({}, findings)

    normalized: dict[str, Any] = dict(payload)
    findings: list[ContractFinding] = []
    _check_schema_version(payload, findings)
    artifacts = payload.get("artifacts")
    if artifacts is None:
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
    findings.extend(_validate_optional_dict(payload, "bootstrap_contract"))
    return _report(normalized, findings)


# LLM: validate_recovery_action checks repair actions before they re-enter the tool loop.
# 函数用途: 校验恢复动作必须包含 code 和 recommended_action 等机器字段。
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
    if "repair_targets" in payload and not _string_list(payload.get("repair_targets")):
        findings.append(_finding("RECOVERY_ACTION_REPAIR_TARGETS_INVALID", "hard", "repair_targets"))
    return _report(dict(payload), findings)


# LLM: _check_schema_version observes version drift without breaking old readable contracts.
# 函数用途: schema_version 不匹配时给 warning，仍允许后续字段校验继续运行。
def _check_schema_version(payload: dict[str, Any], findings: list[ContractFinding]) -> None:
    version = str(payload.get("schema_version") or SCHEMA_VERSION).strip()
    if version and version != SCHEMA_VERSION:
        findings.append(_finding("DELIVERY_CONTRACT_SCHEMA_VERSION_MISMATCH", "warning", "schema_version", value=version))


# LLM: _validate_artifact_contract checks one artifact's locator and validation fields.
# 函数用途: 校验 artifact 结构、开放世界定位字段和路径边界。
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
    findings.extend(_validate_validation_contract(artifact.get("validation_contract"), location))
    findings.extend(_path_findings(artifact, workspace_root, location))
    return normalized, findings


# LLM: _validate_validation_contract checks generic artifact validation options.
# 函数用途: 对 validation_contract 做轻量类型校验，避免字段明显写错才到验收阶段报错。
def _validate_validation_contract(value: object, artifact_location: str) -> list[ContractFinding]:
    if value is None:
        return []
    if not isinstance(value, dict):
        return [_finding("VALIDATION_CONTRACT_NOT_OBJECT", "hard", f"{artifact_location}.validation_contract", value=type(value).__name__)]
    findings: list[ContractFinding] = []
    if "min_size" in value and not _non_negative_int(value.get("min_size")):
        findings.append(_finding("VALIDATION_CONTRACT_MIN_SIZE_INVALID", "hard", f"{artifact_location}.validation_contract.min_size"))
    for key in ("required_sections", "required_files", "required_sheets", "required_columns"):
        if key in value and not _string_list(value.get(key)):
            findings.append(_finding("VALIDATION_CONTRACT_STRING_LIST_INVALID", "hard", f"{artifact_location}.validation_contract.{key}"))
    for key in ("collection_contract", "staging_contract", "quality_contract", "evidence_contract"):
        if key in value and not isinstance(value.get(key), dict):
            findings.append(_finding("VALIDATION_CONTRACT_NESTED_CONTRACT_INVALID", "hard", f"{artifact_location}.validation_contract.{key}"))
    for key in _EXTENSION_KEYS:
        if key in value and not _extension_value(value.get(key)):
            findings.append(_finding("VALIDATION_CONTRACT_EXTENSION_INVALID", "hard", f"{artifact_location}.validation_contract.{key}"))
    return findings


# LLM: _validate_root_lists keeps locator roots structured and bounded.
# 函数用途: 校验 allowed/search/artifact roots 必须是非空字符串列表。
def _validate_root_lists(artifact: dict[str, Any], location: str) -> list[ContractFinding]:
    findings: list[ContractFinding] = []
    for key in _ROOT_LIST_KEYS:
        if key in artifact and not _string_list(artifact.get(key)):
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


# LLM: _validate_optional_dict checks optional top-level contract maps.
# 函数用途: delivery_quality_contract/bootstrap_contract 如果存在必须是对象。
def _validate_optional_dict(payload: dict[str, Any], key: str) -> list[ContractFinding]:
    if key not in payload or isinstance(payload.get(key), dict):
        return []
    return [_finding("DELIVERY_CONTRACT_OPTIONAL_MAP_INVALID", "hard", key, value=type(payload.get(key)).__name__)]


# LLM: _path_findings bounds explicit artifact paths to the workspace when available.
# 函数用途: 检查 path/preferred_path 不能越过当前工作区。
def _path_findings(artifact: dict[str, Any], workspace_root: Path | None, location: str) -> list[ContractFinding]:
    if workspace_root is None:
        return []
    root = Path(workspace_root).resolve(strict=False)
    findings: list[ContractFinding] = []
    for key in _ARTIFACT_PATH_KEYS:
        raw = str(artifact.get(key) or "").strip()
        if not raw:
            continue
        candidate = Path(raw).expanduser()
        path = candidate.resolve(strict=False) if candidate.is_absolute() else (root / candidate).resolve(strict=False)
        try:
            path.relative_to(root)
        except ValueError:
            findings.append(_finding("DELIVERY_CONTRACT_ARTIFACT_PATH_OUTSIDE_WORKSPACE", "hard", f"{location}.{key}", value=raw))
    return findings


# LLM: _report derives ok and rematerialization hints from machine findings.
# 函数用途: 生成 doctor 报告和统一返工动作。
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


# LLM: _rematerialize_action is the generic repair action for malformed delivery contracts.
# 函数用途: 告诉上层重新生成结构化合同，而不是让模型靠自然语言猜坏字段。
def _rematerialize_action(findings: list[ContractFinding]) -> dict[str, object]:
    return {
        "code": "DELIVERY_CONTRACT_REMATERIALIZATION_REQUIRED",
        "category": "contract",
        "retryable": True,
        "recommended_action": "rematerialize_delivery_contract",
        "finding_codes": [finding.code for finding in findings],
    }


# LLM: _has_explicit_path distinguishes locator-backed artifacts from direct paths.
# 函数用途: 判断 artifact 是否声明了 path/preferred_path，开放世界 kind 可作为另一种合法定位方式。
def _has_explicit_path(artifact: dict[str, Any]) -> bool:
    return any(str(artifact.get(key) or "").strip() for key in _ARTIFACT_PATH_KEYS)


def _has_extension_intent(artifact: dict[str, Any]) -> bool:
    if any(_extension_value(artifact.get(key)) for key in _EXTENSION_KEYS if key in artifact):
        return True
    intent = artifact.get("artifact_intent")
    return isinstance(intent, dict) and any(_extension_value(intent.get(key)) for key in _EXTENSION_KEYS if key in intent)


# LLM: _string_list validates schema fields without a closed enum of allowed values.
# 函数用途: 检查列表中的每个值都是非空字符串，供 roots/sections/extensions 等字段复用。
def _string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)


# LLM: _extension_value accepts explicit single or multiple artifact extensions.
# 函数用途: 支持未知文件类型由合同显式声明扩展名，避免格式映射表成为封闭硬门。
def _extension_value(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return _string_list(value)


# LLM: _non_negative_int validates numeric thresholds used by artifact validators.
# 函数用途: 检查 min_size 等阈值是非负整数，避免坏合同到运行期才爆错。
def _non_negative_int(value: object) -> bool:
    return isinstance(value, int) and value >= 0


# LLM: _finding creates one stable delivery-contract doctor finding.
# 函数用途: 统一 finding 的 code/severity/location/message/value 结构，供报告和返工动作消费。
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
