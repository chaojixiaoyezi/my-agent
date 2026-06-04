
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..contracts.delivery_contract_doctor import validate_delivery_contract

SCHEMA_VERSION = "delivery_contract.v1"
MATERIALIZER_SCHEMA_VERSION = "delivery_requirement_materializer.v1"


def build_delivery_requirement_materializer_prompt(user_prompt: str) -> str:
    return (
        "请把下面的用户需求转换成一个最小 delivery_contract.v1 JSON 对象。\n"
        "只输出 JSON，不要解释，不要写具体执行步骤模板，不要替用户编造来源。\n"
        "artifacts 只表示用户要求创建、修改或最终交付的产物；用户要求读取、参考、搜索、对比的文件路径"
        "不是产物，不要写入 artifacts。\n"
        "产物可以只声明 artifact_id、kind、required、allowed_output_roots；只有用户明确说把结果保存到某个文件时，"
        "才写 artifacts[].preferred_path；如果只给输出目录，才写 allowed_output_roots。\n"
        "kind 只在用户明确文件格式或后缀时写；如果用户只说 CAD图纸、文档、视频、图像这类大类，"
        "请写 artifacts[].kind_label，并用 artifacts[].artifact_intent.acceptable_extensions 给出可接受后缀；"
        "不要让系统去找 .cad、.document 这类假后缀。\n"
        "如果用户要求表格列，请写入 artifacts[].validation_contract.required_columns；事实型数字、排名、时间窗"
        "请写入 delivery_quality_contract.metric_contracts。\n"
        "分析型字段请放入 artifacts[].llm_generated_fields，例如解释、理由、建议、结论、判断、摘要这类需要模型撰写的列；"
        "不要把它们映射到来源 API 的普通 description 字段。\n"
        "如果用户要求覆盖一组目标、时间段、名单、分片或来源范围，可以写 target_coverage_contract，里面只放目标清单"
        "和覆盖口径；它是进度账本，不是执行模板。\n"
        "可选字段包括 artifacts、delivery_quality_contract、fact_evidence_contract、target_coverage_contract；"
        "只有外部系统显式给出时才保留 bootstrap_contract。\n"
        "用户需求：\n"
        f"{user_prompt}"
    )


def materialized_delivery_contract(
    payload: object,
    *,
    workspace_root: Path | None = None,
    user_prompt: str = "",
) -> dict[str, Any]:
    value = _payload_object(payload)
    source_doctor = validate_delivery_contract(value, workspace_root=workspace_root)
    artifacts, findings = _artifact_contracts(_artifact_payload(value), workspace_root)
    contract: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "artifacts": artifacts,
    }
    for key in ("delivery_quality_contract", "fact_evidence_contract", "target_coverage_contract", "bootstrap_contract"):
        if isinstance(value.get(key), dict):
            contract[key] = dict(value[key])
    _normalize_delivery_quality_contract(contract)
    _derive_fact_evidence_contract(contract)
    _derive_full_source_read_coverage_contract(contract, user_prompt)
    _derive_user_requested_output_artifacts(contract, user_prompt)
    _preserve_explicit_bootstrap_contract(contract)
    if findings:
        contract["_preflight_findings"] = findings
    doctor = validate_delivery_contract(contract, workspace_root=workspace_root)
    if doctor.normalized_contract:
        contract = dict(doctor.normalized_contract)
    doctor_findings = list(doctor.findings)
    if not _artifact_payload(contract):
        doctor_findings = [*source_doctor.findings, *doctor_findings]
    if doctor_findings:
        contract["_contract_doctor"] = _doctor_payload(
            {
                **doctor.to_dict(),
                "ok": not any(finding.severity == "hard" for finding in doctor_findings),
                "findings": [finding.to_dict() for finding in doctor_findings],
                "repair_actions": source_doctor.repair_actions or doctor.repair_actions,
                "should_rematerialize": source_doctor.should_rematerialize or doctor.should_rematerialize,
            }
        )
    return contract


_COMPLETE_SOURCE_READ_MARKERS = (
    "完整读完",
    "完整读取",
    "全部读完",
    "全文读完",
    "从头到尾",
    "按顺序慢慢读",
    "不要只抽样",
    "不要只搜",
)
_PATH_RE = re.compile(r"(?P<path>(?:[A-Za-z0-9_.~-]+/)*[A-Za-z0-9_.~-]+\.[A-Za-z0-9]{1,12})")
_OUTPUT_INTENT_MARKERS = ("写到", "输出到", "保存到", "放到", "存到", "最终把", "最后写", "最终写")
_OUTPUT_PATH_MARKERS = ("写到", "输出到", "保存到", "放到", "存到", "最后写", "最终写")


def _derive_full_source_read_coverage_contract(contract: dict[str, Any], user_prompt: str) -> None:
    source_paths = _complete_read_source_paths(user_prompt)
    if not source_paths:
        return
    coverage = contract.get("target_coverage_contract")
    if isinstance(coverage, dict):
        _merge_full_source_read_targets(coverage, source_paths)
        return
    contract["target_coverage_contract"] = {
        "scope_label": "完整读取源文件",
        "enforcement": "required",
        "coverage_requirement": "full_source_read",
        "target_items": [_full_source_read_target(path) for path in source_paths],
    }


def _complete_read_source_paths(user_prompt: str) -> list[str]:
    paths: list[str] = []
    for line in str(user_prompt or "").splitlines():
        if not _line_requests_complete_read(line):
            continue
        paths.extend(_path_candidates(line))
    return list(dict.fromkeys(paths))


def _derive_user_requested_output_artifacts(contract: dict[str, Any], user_prompt: str) -> None:
    paths = _user_requested_output_paths(user_prompt)
    if not paths:
        return
    artifacts = contract.get("artifacts")
    if not isinstance(artifacts, list):
        artifacts = []
        contract["artifacts"] = artifacts
    for path in paths:
        if _artifact_path_already_declared(artifacts, path):
            continue
        if _promote_output_path_to_existing_artifact(artifacts, path):
            continue
        artifacts.append(_user_requested_output_artifact(path))


def _user_requested_output_paths(user_prompt: str) -> list[str]:
    paths: list[str] = []
    for line in str(user_prompt or "").splitlines():
        paths.extend(_output_path_candidates(line))
    return list(dict.fromkeys(paths))


def _output_path_candidates(line: str) -> list[str]:
    candidates: list[str] = []
    for marker in _OUTPUT_PATH_MARKERS:
        index = line.find(marker)
        if index < 0:
            continue
        candidates.extend(match.group("path") for match in _PATH_RE.finditer(line[index + len(marker) :]))
    return candidates


def _artifact_path_already_declared(artifacts: list[object], path: str) -> bool:
    return any(
        isinstance(item, dict)
        and str(item.get("preferred_path") or item.get("path") or "").strip() == path
        for item in artifacts
    )


def _promote_output_path_to_existing_artifact(artifacts: list[object], path: str) -> bool:
    candidates = [
        item
        for item in artifacts
        if isinstance(item, dict)
        and not str(item.get("preferred_path") or item.get("path") or "").strip()
        and not _artifact_declares_input_role(item)
    ]
    if len(candidates) != 1:
        return False
    artifact = candidates[0]
    artifact["preferred_path"] = path
    artifact.setdefault("allowed_output_roots", [str(Path(path).parent) if str(Path(path).parent) != "." else "."])
    if not artifact.get("kind"):
        kind = Path(path).suffix.lower().lstrip(".")
        if kind:
            artifact["kind"] = kind
    return True


def _user_requested_output_artifact(path: str) -> dict[str, Any]:
    suffix = Path(path).suffix.lower().lstrip(".")
    artifact: dict[str, Any] = {
        "artifact_id": _artifact_id_from_output_path(path),
        "preferred_path": path,
        "allowed_output_roots": [str(Path(path).parent) if str(Path(path).parent) != "." else "."],
        "required": True,
    }
    if suffix:
        artifact["kind"] = suffix
    return artifact


def _artifact_id_from_output_path(path: str) -> str:
    raw = Path(path).name or "artifact"
    text = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_").lower()
    return f"user_requested_{text or 'artifact'}"


def _line_requests_complete_read(line: str) -> bool:
    return any(marker in line for marker in _COMPLETE_SOURCE_READ_MARKERS)


def _path_candidates(text: str) -> list[str]:
    return [match.group("path") for match in _PATH_RE.finditer(_source_intent_segment(text))]


def _source_intent_segment(text: str) -> str:
    end = len(text)
    for marker in _OUTPUT_INTENT_MARKERS:
        index = text.find(marker)
        if index >= 0:
            end = min(end, index)
    return text[:end]


def _merge_full_source_read_targets(coverage: dict[str, Any], paths: list[str]) -> None:
    items = coverage.get("target_items")
    if not isinstance(items, list):
        items = []
        coverage["target_items"] = items
    existing = {str(item.get("source_ref") or item.get("target_id") or "") for item in items if isinstance(item, dict)}
    for path in paths:
        if path not in existing:
            items.append(_full_source_read_target(path))
    coverage.setdefault("coverage_requirement", "full_source_read")
    coverage.setdefault("enforcement", "required")


def _full_source_read_target(path: str) -> dict[str, str]:
    return {
        "target_id": path,
        "label": f"完整读取 {path}",
        "source_ref": path,
        "coverage_kind": "full_source_read",
    }


def _artifact_payload(value: dict[str, Any]) -> object:
    if "artifacts" in value:
        return value.get("artifacts")
    if any(key in value for key in ("artifact_id", "kind", "path", "preferred_path", "allowed_output_roots")):
        return [value]
    return None


def _payload_object(payload: object) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        parsed = _loads_payload_object(payload)
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _loads_payload_object(text: str) -> object:
    for candidate in _json_candidates(text):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return {}


def _json_candidates(text: str) -> list[str]:
    stripped = text.strip()
    candidates = [stripped]
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            candidates.append("\n".join(lines[1:-1]).strip())
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        candidates.append(stripped[start : end + 1])
    return candidates


def _artifact_contracts(value: object, workspace_root: Path | None) -> tuple[list[dict[str, Any]], list[dict[str, object]]]:
    artifacts: list[dict[str, Any]] = []
    findings: list[dict[str, object]] = []
    if not isinstance(value, list):
        return artifacts, findings
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            findings.append(_finding("DELIVERY_MATERIALIZER_ARTIFACT_INVALID", f"artifacts[{index}]"))
            continue
        if _artifact_declares_input_role(item):
            findings.append(_finding("DELIVERY_MATERIALIZER_INPUT_NOT_ARTIFACT", f"artifacts[{index}]"))
            continue
        normalized = _artifact_contract(item)
        if _is_internal_work_artifact(normalized):
            continue
        path_finding = _path_finding(normalized, workspace_root, index)
        if path_finding:
            findings.append(path_finding)
            continue
        artifacts.append(normalized)
    return artifacts, findings


def _artifact_contract(item: dict[str, Any]) -> dict[str, Any]:
    allowed_keys = {
        "allowed_output_roots",
        "acceptable_extension",
        "acceptable_extensions",
        "accepted_extension",
        "accepted_extensions",
        "artifact_intent",
        "artifact_id",
        "content_type",
        "extension",
        "extensions",
        "file_extension",
        "file_extensions",
        "kind",
        "kind_label",
        "mime_type",
        "path",
        "preferred_extension",
        "preferred_extensions",
        "preferred_path",
        "purpose",
        "required",
        "role",
        "search_roots",
        "validation_contract",
        "llm_generated_fields",
    }
    result = {key: item[key] for key in allowed_keys if key in item}
    _normalize_kind_field(result)
    _normalize_artifact_intent_fields(result)
    if "required" not in result:
        result["required"] = True
    kind = _kind_text(result.pop("kind", ""))
    if kind:
        result["kind"] = kind
    else:
        kind = _kind_from_artifact_path(result)
        if kind:
            result["kind"] = kind
    if isinstance(result.get("allowed_output_roots"), list):
        result["allowed_output_roots"] = [str(value).strip() for value in result["allowed_output_roots"] if str(value).strip()]
        _promote_file_root_to_preferred_path(result)
    return result


def _normalize_kind_field(artifact: dict[str, Any]) -> None:
    raw_kind = artifact.get("kind")
    if isinstance(raw_kind, dict):
        intent = artifact.get("artifact_intent")
        merged = dict(raw_kind)
        if isinstance(intent, dict):
            merged.update(intent)
        artifact["artifact_intent"] = merged
        artifact.pop("kind", None)
    elif raw_kind is not None and not isinstance(raw_kind, str):
        artifact.pop("kind", None)


def _kind_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower().lstrip(".")


def _normalize_artifact_intent_fields(artifact: dict[str, Any]) -> None:
    intent = artifact.get("artifact_intent")
    if isinstance(intent, dict):
        artifact["artifact_intent"] = _artifact_intent(intent)
    else:
        artifact.pop("artifact_intent", None)
    _promote_intent_extensions(artifact)


def _artifact_intent(intent: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "acceptable_extension",
        "acceptable_extensions",
        "accepted_extension",
        "accepted_extensions",
        "content_type",
        "extension",
        "extensions",
        "file_extension",
        "file_extensions",
        "kind_label",
        "mime_type",
        "preferred_extension",
        "preferred_extensions",
        "role",
    }
    result = {key: intent[key] for key in allowed if key in intent}
    for key in ("acceptable_extensions", "accepted_extensions", "extensions", "file_extensions", "preferred_extensions"):
        if key in result:
            result[key] = _string_items(result[key])
    for key in ("acceptable_extension", "accepted_extension", "extension", "file_extension", "preferred_extension"):
        if key in result:
            value = str(result[key]).strip()
            if value:
                result[key] = value
            else:
                result.pop(key, None)
    return result


def _promote_intent_extensions(artifact: dict[str, Any]) -> None:
    if any(key in artifact for key in ("extension", "extensions", "file_extension", "file_extensions")):
        return
    extensions = _extension_values_from_artifact(artifact)
    if extensions:
        artifact["file_extensions"] = extensions


def _extension_values_from_artifact(artifact: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("preferred_extension", "preferred_extensions", "acceptable_extension", "acceptable_extensions", "accepted_extension", "accepted_extensions"):
        values.extend(_string_items(artifact.get(key)))
    intent = artifact.get("artifact_intent")
    if isinstance(intent, dict):
        for key in ("preferred_extension", "preferred_extensions", "acceptable_extension", "acceptable_extensions", "accepted_extension", "accepted_extensions"):
            values.extend(_string_items(intent.get(key)))
    return list(dict.fromkeys(value for value in values if value))


def _string_items(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _artifact_declares_input_role(item: dict[str, Any]) -> bool:
    role = " ".join(
        str(item.get(key) or "").strip().lower()
        for key in ("artifact_role", "role", "purpose", "usage")
    )
    if not role:
        return False
    input_markers = (
        "input",
        "source",
        "reference",
        "read_only",
        "readonly",
        "evidence",
        "lookup",
        "search",
    )
    return any(marker in role for marker in input_markers)


def _is_internal_work_artifact(artifact: dict[str, Any]) -> bool:
    if str(artifact.get("preferred_path") or artifact.get("path") or "").strip():
        return False
    if artifact.get("kind") or _extension_values_from_artifact(artifact):
        return False
    roots = artifact.get("allowed_output_roots")
    if not isinstance(roots, list) or not roots:
        return False
    return all(_is_work_root(root) for root in roots)


def _is_work_root(value: object) -> bool:
    text = str(value or "").strip().rstrip("/\\")
    if not text:
        return False
    return text == "work" or Path(text).name == "work"


def _kind_from_artifact_path(artifact: dict[str, Any]) -> str:
    raw = str(artifact.get("preferred_path") or artifact.get("path") or "").strip()
    if not raw:
        roots = artifact.get("allowed_output_roots")
        raw = str(roots[0]).strip() if isinstance(roots, list) and len(roots) == 1 else ""
    suffix = Path(raw).suffix.lower().lstrip(".")
    if not suffix:
        return ""
    return suffix


def _promote_file_root_to_preferred_path(artifact: dict[str, Any]) -> None:
    if artifact.get("preferred_path") or artifact.get("path"):
        return
    roots = artifact.get("allowed_output_roots")
    if not isinstance(roots, list) or len(roots) != 1:
        return
    root = str(roots[0]).strip()
    if not Path(root).suffix:
        return
    artifact["preferred_path"] = root
    artifact["allowed_output_roots"] = [str(Path(root).parent)]


def _derive_fact_evidence_contract(contract: dict[str, Any]) -> None:
    if isinstance(contract.get("fact_evidence_contract"), dict):
        return
    quality = contract.get("delivery_quality_contract")
    if not isinstance(quality, dict):
        return
    evidence_contract = _derived_evidence_contract(quality)
    if not evidence_contract:
        return
    contract["fact_evidence_contract"] = {
        "evidence_contract": evidence_contract,
        "require_tool_backed_sources": True,
    }


def _derived_evidence_contract(quality: dict[str, Any]) -> dict[str, Any]:
    evidence = dict(quality.get("evidence_contract")) if isinstance(quality.get("evidence_contract"), dict) else {}
    required_fields = _merged_required_fields(evidence.get("required_fields"), quality.get("metric_contracts"))
    if not required_fields:
        return {}
    evidence["required_fields"] = required_fields
    evidence.setdefault("require_verified", True)
    if "allowed_value_types" not in evidence:
        evidence["allowed_value_types"] = ["exact"]
    return evidence


def _merged_required_fields(raw_fields: object, metric_contracts: object) -> list[str]:
    fields = [str(item).strip() for item in raw_fields if str(item).strip()] if isinstance(raw_fields, list) else []
    if isinstance(metric_contracts, list):
        fields.extend(str(item.get("field") or "").strip() for item in metric_contracts if isinstance(item, dict))
    return sorted({item for item in fields if item})


def _normalize_delivery_quality_contract(contract: dict[str, Any]) -> None:
    quality = contract.get("delivery_quality_contract")
    if not isinstance(quality, dict):
        return
    metrics = quality.get("metric_contracts")
    if not isinstance(metrics, list):
        return
    normalized: list[dict[str, Any]] = []
    for item in metrics:
        if not isinstance(item, dict):
            continue
        metric = dict(item)
        field = str(metric.get("field") or metric.get("name") or metric.get("column") or "").strip()
        if field:
            metric["field"] = field
        normalized.append(metric)
    quality["metric_contracts"] = normalized
    contract["delivery_quality_contract"] = quality


def _preserve_explicit_bootstrap_contract(contract: dict[str, Any]) -> None:
    bootstrap = contract.get("bootstrap_contract")
    if not isinstance(bootstrap, dict):
        return
    if "enforcement" not in bootstrap:
        bootstrap["enforcement"] = "soft"
    contract["bootstrap_contract"] = bootstrap


def _path_finding(item: dict[str, Any], workspace_root: Path | None, index: int) -> dict[str, object] | None:
    for key in ("preferred_path", "path"):
        raw = str(item.get(key) or "").strip()
        if not raw:
            continue
        try:
            _resolve_artifact_path(raw, workspace_root)
        except (OSError, RuntimeError, ValueError):
            return _finding(
                "DELIVERY_MATERIALIZER_ARTIFACT_PATH_INVALID",
                f"artifacts[{index}].{key}",
                value=raw,
            )
    return None


def _resolve_artifact_path(raw: str, workspace_root: Path | None) -> Path:
    candidate = Path(raw).expanduser()
    if candidate.is_absolute() or workspace_root is None:
        return candidate.resolve(strict=False)
    return (Path(workspace_root).resolve(strict=False) / candidate).resolve(strict=False)


def _finding(code: str, location: str, *, value: str = "") -> dict[str, object]:
    return {
        "code": code,
        "severity": "warning",
        "location": location,
        "message": code.lower(),
        "value": value,
    }


def _doctor_payload(report: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": report.get("schema_version", ""),
        "ok": report.get("ok", False),
        "findings": report.get("findings", []),
        "repair_actions": report.get("repair_actions", []),
        "should_rematerialize": report.get("should_rematerialize", False),
    }


__all__ = [
    "MATERIALIZER_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "build_delivery_requirement_materializer_prompt",
    "materialized_delivery_contract",
]
