
from __future__ import annotations

import json
import re
from pathlib import Path, PureWindowsPath
from typing import Any

from ..contracts.delivery_contract_doctor import validate_delivery_contract
from ..contracts.recovery import RecoveryAction

SCHEMA_VERSION = "delivery_contract.v1"
MATERIALIZER_SCHEMA_VERSION = "delivery_requirement_materializer.v1"
SOURCE_COVERAGE_UNDECLARED = "DELIVERY_MATERIALIZER_SOURCE_COVERAGE_UNDECLARED"


def build_delivery_requirement_materializer_prompt(user_prompt: str, *, repair_feedback: str = "") -> str:
    prompt = (
        "请把下面的用户需求转换成一个最小 delivery_contract.v1 JSON 对象。\n"
        "只输出 JSON，不要解释，不要写具体执行步骤模板，不要替用户编造来源。\n"
        "artifacts 只表示用户要求创建、修改、过程中写入或最终交付的文件产物；用户要求读取、参考、搜索、对比的文件路径"
        "不是产物，不要写入 artifacts。子代理 work/agents/... 内部状态和 final_report.md 不是用户产物。\n"
        "产物可以只声明 artifact_id、kind、required、allowed_output_roots；只有用户明确说把结果保存到某个文件时，"
        "才写 artifacts[].preferred_path；如果只给输出目录，才写 allowed_output_roots。\n"
        "kind 只在用户明确文件格式或后缀时写；如果用户只说 CAD图纸、文档、视频、图像这类大类，"
        "请写 artifacts[].kind_label，并用 artifacts[].artifact_intent.acceptable_extensions 给出可接受后缀；"
        "不要让系统去找 .cad、.document 这类假后缀。\n"
        "如果用户要求表格列，请写入 artifacts[].validation_contract.required_columns；事实型数字、排名、时间窗"
        "请写入 delivery_quality_contract.metric_contracts。\n"
        "用户用普通自然语言描述的报告维度、分析角度和对比口径只作为内容意图，不要写入"
        "validation_contract.required_sections；只有外部结构化合同已经显式给出这些字段时才保留。\n"
        "分析型字段请放入 artifacts[].llm_generated_fields，例如解释、理由、建议、结论、判断、摘要这类需要模型撰写的列；"
        "不要把它们映射到来源 API 的普通 description 字段。\n"
        "如果外部调用方显式需要把覆盖范围结构化，可以写 target_coverage_contract，里面只放目标清单和覆盖口径；"
        "它是进度账本，不是执行模板。不要仅凭普通自然语言把覆盖要求升级成硬性验收门。\n"
        "可选字段包括 artifacts、delivery_quality_contract、fact_evidence_contract、target_coverage_contract；"
        "只有外部系统显式给出时才保留 bootstrap_contract。\n"
    )
    if repair_feedback.strip():
        prompt += (
            "合同医生反馈：\n"
            f"{repair_feedback.strip()}\n"
            "请基于反馈重新输出完整 JSON；不要只重复上次 artifacts。\n"
        )
    return prompt + "用户需求：\n" + f"{user_prompt}"


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
    _derive_user_requested_output_artifacts(contract, user_prompt)
    _derive_prompt_directory_coverage_contract(contract, user_prompt, workspace_root)
    _derive_prompt_report_artifact_contract(contract, user_prompt)
    _normalize_target_coverage_contract(contract, user_prompt, workspace_root)
    _preserve_explicit_bootstrap_contract(contract)
    _attach_source_contract_repair_diagnostics(contract, user_prompt)
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


_PATH_SEGMENT = r"[^\\/\s，。；;：:、)）\]】\"'<>`]+"
_PATH_RE = re.compile(
    rf"(?P<path>(?:(?:[A-Za-z]:[\\/])|(?:\\\\{_PATH_SEGMENT}[\\/]{_PATH_SEGMENT}[\\/])|(?:~[\\/])|/)?"
    rf"(?:{_PATH_SEGMENT}[\\/])*{_PATH_SEGMENT}\.[A-Za-z0-9]{{1,12}})"
)
_ABSOLUTE_OR_HOME_PATH_RE = re.compile(
    rf"(?<![A-Za-z0-9_.~-])(?P<path>(?:[A-Za-z]:[\\/]|~[\\/]|/)(?:{_PATH_SEGMENT}[\\/])*{_PATH_SEGMENT})"
)


def delivery_contract_from_user_requested_outputs(
    user_prompt: str,
    *,
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    artifacts = [_user_requested_output_artifact(path) for path in _structural_output_path_candidates(user_prompt)]
    if not artifacts:
        return {}
    contract = {"schema_version": SCHEMA_VERSION, "artifacts": artifacts}
    _derive_prompt_directory_coverage_contract(contract, user_prompt, workspace_root)
    _attach_source_contract_repair_diagnostics(contract, user_prompt)
    doctor = validate_delivery_contract(contract, workspace_root=workspace_root)
    return dict(doctor.normalized_contract or contract)


def delivery_contract_from_user_prompt_structure(
    user_prompt: str,
    *,
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    contract = delivery_contract_from_user_requested_outputs(user_prompt, workspace_root=workspace_root)
    if not contract:
        contract = {"schema_version": SCHEMA_VERSION, "artifacts": []}
        _derive_prompt_directory_coverage_contract(contract, user_prompt, workspace_root)
        _derive_prompt_report_artifact_contract(contract, user_prompt)
        if not isinstance(contract.get("target_coverage_contract"), dict):
            return {}
    doctor = validate_delivery_contract(contract, workspace_root=workspace_root)
    return dict(doctor.normalized_contract or contract)


def materializer_repair_feedback(contract: dict[str, Any]) -> str:
    doctor = contract.get("_contract_doctor")
    if not isinstance(doctor, dict) or not doctor.get("should_rematerialize"):
        return ""
    findings = doctor.get("findings")
    actions = doctor.get("repair_actions")
    payload = {
        "findings": findings if isinstance(findings, list) else [],
        "repair_actions": actions if isinstance(actions, list) else [],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _attach_source_contract_repair_diagnostics(contract: dict[str, Any], user_prompt: str) -> None:
    if not _artifact_payload(contract):
        return
    if _has_source_modeling_contract(contract):
        return
    source_paths = _user_referenced_source_paths(user_prompt, contract.get("artifacts"))
    if not source_paths:
        return
    existing = contract.get("_contract_doctor")
    findings = []
    repair_actions = []
    if isinstance(existing, dict):
        findings = list(existing.get("findings") or []) if isinstance(existing.get("findings"), list) else []
        repair_actions = list(existing.get("repair_actions") or []) if isinstance(existing.get("repair_actions"), list) else []
    finding = _finding(
        SOURCE_COVERAGE_UNDECLARED,
        "source_paths",
        severity="warning",
        value=", ".join(source_paths[:12]),
    )
    findings.append(finding)
    repair_actions.append({
        "code": "DELIVERY_CONTRACT_REMATERIALIZATION_REQUIRED",
        "category": "contract",
        "retryable": True,
        "recommended_action": RecoveryAction.REPAIR_EFFECTIVE_CONTRACT.value,
        "source_paths": source_paths[:50],
        "finding_codes": [SOURCE_COVERAGE_UNDECLARED],
    })
    contract["_contract_doctor"] = _doctor_payload({
        "schema_version": "delivery_contract_doctor.v1",
        "ok": True,
        "findings": findings,
        "repair_actions": repair_actions,
        "should_rematerialize": True,
    })


def _has_source_modeling_contract(contract: dict[str, Any]) -> bool:
    return isinstance(contract.get("target_coverage_contract"), dict)


def _user_referenced_source_paths(user_prompt: str, artifacts: object = None) -> list[str]:
    output_paths = {*_declared_artifact_paths(artifacts), *_structural_output_path_candidates(user_prompt)}
    paths: list[str] = []
    for path in _path_candidates(str(user_prompt or "")):
        if path in output_paths:
            continue
        if _looks_like_output_path(path):
            continue
        paths.append(path)
    return list(dict.fromkeys(paths))


def _path_candidates(text: str) -> list[str]:
    paths = [match.group("path") for match in _PATH_RE.finditer(text)]
    paths.extend(match.group("path") for match in _ABSOLUTE_OR_HOME_PATH_RE.finditer(text))
    return list(dict.fromkeys(paths))


def _looks_like_output_path(path: str) -> bool:
    pure = _pure_path(path)
    name = pure.name.lower()
    parent = pure.parent.name.lower()
    output_markers = {"output", "outputs", "result", "results", "report", "reports", "dist", "build", "lab_outputs"}
    if any(marker in parent or marker in name for marker in output_markers):
        return True
    parts = [part.lower() for part in pure.parts if part not in ("/", "\\")]
    if parts and not _is_absolute_or_home_path(path) and parts[0] in output_markers:
        return True
    return any(part in output_markers for part in parts[-4:-1])


def _looks_like_user_work_artifact_path(path: str) -> bool:
    parts = [part.lower() for part in _pure_path(path).parts]
    if "work" not in parts or not _path_suffix(path):
        return False
    work_index = parts.index("work")
    tail = parts[work_index + 1 :]
    if not tail:
        return False
    return tail[0] != "agents"


def _looks_like_internal_agent_work_path(path: str) -> bool:
    parts = [part.lower() for part in _pure_path(path).parts]
    if "work" not in parts:
        return False
    work_index = parts.index("work")
    tail = parts[work_index + 1 :]
    return bool(tail and tail[0] == "agents")


def _derive_user_requested_output_artifacts(contract: dict[str, Any], user_prompt: str) -> None:
    paths = _structural_output_path_candidates(user_prompt)
    if not paths:
        return
    artifacts = contract.get("artifacts")
    if not isinstance(artifacts, list):
        artifacts = []
        contract["artifacts"] = artifacts
    for path in paths:
        if _artifact_path_already_declared(artifacts, path):
            continue
        if _correct_existing_artifact_to_prompt_output_path(artifacts, path, prompt_paths=paths):
            continue
        if _promote_output_path_to_existing_artifact(artifacts, path):
            continue
        artifacts.append(_user_requested_output_artifact(path))


def _structural_output_path_candidates(user_prompt: str) -> list[str]:
    paths: list[str] = []
    for match in _PATH_RE.finditer(str(user_prompt or "")):
        path = match.group("path")
        if _looks_like_internal_agent_work_path(path):
            continue
        if _looks_like_output_path(path) or _looks_like_user_work_artifact_path(path):
            paths.append(path)
    return _drop_shadowed_basename_paths(list(dict.fromkeys(paths)))


def _drop_shadowed_basename_paths(paths: list[str]) -> list[str]:
    result: list[str] = []
    for path in paths:
        if _is_shadowed_basename_path(path, paths):
            continue
        result.append(path)
    return result


def _is_shadowed_basename_path(path: str, paths: list[str]) -> bool:
    if _output_parent(path) != ".":
        return False
    name = _path_name(path)
    if not name:
        return False
    return any(other != path and _path_name(other) == name and _output_parent(other) != "." for other in paths)


def _declared_artifact_paths(artifacts: object) -> list[str]:
    if not isinstance(artifacts, list):
        return []
    paths: list[str] = []
    for artifact in artifacts:
        paths.extend(_declared_paths_from_artifact(artifact))
    return list(dict.fromkeys(paths))


def _declared_paths_from_artifact(artifact: object) -> list[str]:
    if not isinstance(artifact, dict):
        return []
    return [
        value
        for key in ("preferred_path", "path")
        if (value := str(artifact.get(key) or "").strip())
    ]


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
    artifact.setdefault("allowed_output_roots", [_output_parent(path)])
    if not artifact.get("kind"):
        kind = _path_suffix(path)
        if kind:
            artifact["kind"] = kind
    return True


def _correct_existing_artifact_to_prompt_output_path(
    artifacts: list[object],
    path: str,
    *,
    prompt_paths: list[str],
) -> bool:
    candidates = [
        item
        for item in artifacts
        if isinstance(item, dict)
        and _artifact_can_take_prompt_path(item, path, prompt_paths=prompt_paths)
    ]
    if len(candidates) != 1:
        return False
    artifact = candidates[0]
    artifact["preferred_path"] = path
    artifact.pop("path", None)
    artifact["allowed_output_roots"] = [_output_parent(path)]
    if not artifact.get("kind"):
        kind = _path_suffix(path)
        if kind:
            artifact["kind"] = kind
    return True


def _artifact_can_take_prompt_path(
    artifact: dict[str, Any],
    path: str,
    *,
    prompt_paths: list[str],
) -> bool:
    if _artifact_declares_input_role(artifact):
        return False
    existing = str(artifact.get("preferred_path") or artifact.get("path") or "").strip()
    if not existing or existing == path or existing in set(prompt_paths):
        return False
    if _path_name(existing) != _path_name(path):
        return False
    existing_suffix = _path_suffix(existing)
    prompt_suffix = _path_suffix(path)
    if existing_suffix and prompt_suffix and existing_suffix != prompt_suffix:
        return False
    kind = str(artifact.get("kind") or "").strip().lower().lstrip(".")
    return not kind or not prompt_suffix or kind == prompt_suffix


def _user_requested_output_artifact(path: str) -> dict[str, Any]:
    suffix = _path_suffix(path)
    artifact: dict[str, Any] = {
        "artifact_id": _artifact_id_from_output_path(path),
        "preferred_path": path,
        "allowed_output_roots": [_output_parent(path)],
        "required": True,
    }
    if suffix:
        artifact["kind"] = suffix
    return artifact


def _derive_prompt_report_artifact_contract(contract: dict[str, Any], user_prompt: str) -> None:
    artifacts = contract.get("artifacts")
    if isinstance(artifacts, list):
        if any(_is_report_artifact(artifact) for artifact in artifacts):
            return
    if not _prompt_requests_final_report(user_prompt):
        return
    if not isinstance(artifacts, list):
        artifacts = []
        contract["artifacts"] = artifacts
    artifacts.append(
        {
            "artifact_id": "final_report",
            "kind": "md",
            "allowed_output_roots": ["output"],
            "required": True,
        }
    )


def _is_report_artifact(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    kind = str(value.get("kind") or "").strip().lower().lstrip(".")
    if kind in {"md", "markdown", "txt", "docx", "pdf", "html"}:
        return True
    path = str(value.get("preferred_path") or value.get("path") or "").strip().lower()
    return path.endswith((".md", ".markdown", ".txt", ".docx", ".pdf", ".html", ".htm"))


def _prompt_requests_final_report(user_prompt: str) -> bool:
    text = str(user_prompt or "")
    return bool(re.search(r"(?:最后|最终|生成|输出|写(?:成|出)?|整理(?:成)?)\S{0,20}报告", text))


def _artifact_id_from_output_path(path: str) -> str:
    raw = _path_name(path) or "artifact"
    text = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_").lower()
    return f"user_requested_{text or 'artifact'}"


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
        "artifact_role",
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
        "artifact_role",
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
    roles = [
        str(item.get(key) or "").strip().lower()
        for key in ("artifact_role", "role")
        if str(item.get(key) or "").strip()
    ]
    intent = item.get("artifact_intent")
    if isinstance(intent, dict):
        roles.extend(
            str(intent.get(key) or "").strip().lower()
            for key in ("artifact_role", "role")
            if str(intent.get(key) or "").strip()
        )
    return any(role in _INPUT_ARTIFACT_ROLES for role in roles)


_INPUT_ARTIFACT_ROLES = frozenset({"input", "source", "reference", "read_only", "evidence", "lookup", "search"})


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
    suffix = _path_suffix(raw)
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
    if not _path_suffix(root):
        return
    artifact["preferred_path"] = root
    artifact["allowed_output_roots"] = [_output_parent(root)]


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
        field = str(metric.get("field") or "").strip()
        if field:
            metric["field"] = field
        normalized.append(metric)
    quality["metric_contracts"] = normalized
    contract["delivery_quality_contract"] = quality


def _normalize_target_coverage_contract(
    contract: dict[str, Any],
    user_prompt: str,
    workspace_root: Path | None,
) -> None:
    coverage = contract.get("target_coverage_contract")
    if not isinstance(coverage, dict):
        return
    if isinstance(coverage.get("target_items"), list):
        return
    projects = _string_items(coverage.get("target_projects") or coverage.get("projects"))
    if not projects:
        return
    base = _target_coverage_base_path(coverage, user_prompt, contract.get("artifacts"), workspace_root)
    coverage["target_items"] = [_project_coverage_item(project, base) for project in projects]
    coverage.setdefault("scope_label", "source projects")
    coverage.setdefault("enforcement", "required")
    contract["target_coverage_contract"] = coverage


def _derive_prompt_directory_coverage_contract(
    contract: dict[str, Any],
    user_prompt: str,
    workspace_root: Path | None,
) -> None:
    coverage = contract.get("target_coverage_contract")
    if isinstance(coverage, dict) and isinstance(coverage.get("target_items"), list):
        return
    if isinstance(coverage, dict) and (coverage.get("target_projects") or coverage.get("projects")):
        return
    items: list[dict[str, Any]] = []
    for root in _source_directories_from_prompt(user_prompt, contract.get("artifacts"), workspace_root):
        items.extend(_prompt_directory_coverage_items(root, user_prompt, has_existing=bool(items)))
    if not items:
        return
    contract["target_coverage_contract"] = {
        "scope_label": "source directories",
        "enforcement": "required",
        "target_items": items,
    }


def _prompt_directory_coverage_items(root: Path, user_prompt: str, *, has_existing: bool) -> list[dict[str, Any]]:
    children = _mentioned_child_directories(root, user_prompt) or _project_child_directories(root)
    if children:
        return [_project_coverage_item(child.name, str(root)) for child in children]
    return [] if has_existing else [_project_coverage_item(root.name, str(root.parent))]


def _source_directories_from_prompt(
    user_prompt: str,
    artifacts: object,
    workspace_root: Path | None,
) -> list[Path]:
    roots: list[Path] = []
    for raw in _user_referenced_source_paths(user_prompt, artifacts):
        path = _resolve_source_path(raw, workspace_root)
        if path is not None and path.is_dir() and path not in roots:
            roots.append(path)
    return roots


def _resolve_source_path(raw: str, workspace_root: Path | None) -> Path | None:
    if not raw:
        return None
    try:
        path = Path(raw).expanduser()
        if not path.is_absolute() and workspace_root is not None:
            path = Path(workspace_root).expanduser() / path
        return path.resolve(strict=False)
    except OSError:
        return None


def _mentioned_child_directories(root: Path, user_prompt: str) -> list[Path]:
    try:
        children = _sorted_directories(root)
    except OSError:
        return []
    excluded = _excluded_child_directory_names(children)
    return [
        child
        for child in children
        if child.name in user_prompt
        and child.name not in excluded
        and not _looks_like_output_path(child.name)
        and not _looks_like_non_source_child_dir(child.name)
    ]


def _project_child_directories(root: Path) -> list[Path]:
    try:
        children = _sorted_directories(root)
    except OSError:
        return []
    candidates = [
        child
        for child in children
        if not _looks_like_output_path(child.name)
        and not _looks_like_non_source_child_dir(child.name)
        and _looks_like_project_root(child)
    ]
    return candidates if len(candidates) >= 2 else []


def _sorted_directories(root: Path) -> list[Path]:
    return sorted((item for item in root.iterdir() if item.is_dir()), key=lambda item: (item.name.casefold(), item.name))


def _looks_like_project_root(path: Path) -> bool:
    marker_names = {
        ".git",
        "README",
        "README.md",
        "README.rst",
        "pyproject.toml",
        "setup.py",
        "requirements.txt",
        "package.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "Cargo.toml",
        "go.mod",
        "pom.xml",
        "build.gradle",
        "Makefile",
    }
    try:
        names = {item.name for item in path.iterdir()}
    except OSError:
        return False
    if marker_names.intersection(names):
        return True
    return any((path / name).is_dir() for name in ("src", "lib", "app", "packages"))


def _excluded_child_directory_names(children: list[Path]) -> set[str]:
    names = {child.name for child in children}
    return {name for name in names if _looks_like_non_source_child_dir(name)}


def _looks_like_non_source_child_dir(name: str) -> bool:
    lowered = name.lower()
    if lowered.startswith(("_backup", ".agent")):
        return True
    return lowered in {
        "all_agent_total_code_module_reports",
        "data",
        "local_store",
        "memory",
        "memory_archive",
        "notes",
        "output",
        "outputs",
        "subagents",
        "tmp",
        "笔记",
    }


def _target_coverage_base_path(
    coverage: dict[str, Any],
    user_prompt: str,
    artifacts: object,
    workspace_root: Path | None,
) -> str:
    candidates = [
        *_user_referenced_source_paths(user_prompt, artifacts),
        str(coverage.get("base_path") or "").strip(),
        str(workspace_root or "").strip(),
    ]
    for candidate in candidates:
        if _path_is_existing_dir(candidate):
            return candidate
    return next((candidate for candidate in candidates if candidate), "")


def _path_is_existing_dir(value: str) -> bool:
    if not value:
        return False
    try:
        return Path(value).expanduser().is_dir()
    except OSError:
        return False


def _project_coverage_item(project: str, base: str) -> dict[str, Any]:
    source_ref = str((Path(base).expanduser() / project).resolve(strict=False)) if base else project
    return {
        "target_id": project,
        "label": project,
        "source_ref": source_ref,
        "coverage_kind": "source_file_under_dir",
        "min_read_count": 2,
    }


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
    if _is_windows_absolute_path(raw) and not candidate.is_absolute():
        return candidate
    if candidate.is_absolute() or workspace_root is None:
        return candidate.resolve(strict=False)
    return (Path(workspace_root).resolve(strict=False) / candidate).resolve(strict=False)


def _output_parent(path: str) -> str:
    parent = _pure_path(path).parent
    text = str(parent)
    return text if text != "." else "."


def _path_suffix(path: str) -> str:
    return _pure_path(path).suffix.lower().lstrip(".")


def _path_name(path: str) -> str:
    return _pure_path(path).name


def _pure_path(path: str):
    text = str(path).strip()
    return PureWindowsPath(text) if _is_windows_path(text) else Path(text)


def _is_windows_path(text: str) -> bool:
    return _is_windows_absolute_path(text) or "\\" in text


def _is_absolute_or_home_path(text: str) -> bool:
    return str(text or "").startswith(("/", "~")) or _is_windows_absolute_path(str(text or ""))


def _is_windows_absolute_path(text: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", text) or re.match(r"^\\\\[^\\/]+[\\/][^\\/]+", text))


def _finding(code: str, location: str, *, severity: str = "warning", value: str = "") -> dict[str, object]:
    return {
        "code": code,
        "severity": severity,
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
    "delivery_contract_from_user_prompt_structure",
    "materialized_delivery_contract",
    "materializer_repair_feedback",
]
