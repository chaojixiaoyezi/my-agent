from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ContractFixtureResult:
    ok: bool
    error_codes: tuple[str, ...]
    errors: tuple[str, ...]
    findings: tuple[dict[str, object], ...]
    recommended_action: str


def verify_contract_fixture(
    run_dir: Path,
    contract: dict[str, object],
    *,
    tool_trace: list[dict[str, object]],
    final_status: str,
) -> ContractFixtureResult:
    errors: list[str] = []
    error_codes: list[str] = []
    findings: list[dict[str, object]] = []
    artifact_conditions = _artifact_conditions(run_dir, contract, errors, error_codes, findings)
    tool_conditions = _tool_conditions(contract, tool_trace, errors, error_codes, findings)
    conditions = {**artifact_conditions, **tool_conditions}
    if str(final_status).upper() == "UNKNOWN":
        error_codes.append("FINAL_STATUS_MISSING")
        errors.append("final status is missing")
        findings.append(_finding("FINAL_STATUS_MISSING", "final_status", "final status is missing"))
    if str(final_status).upper() == "SUCCEEDED" and not _succeeded_allowed(contract, conditions):
        error_codes.append("FINAL_STATUS_REJECTED")
        errors.append("final status SUCCEEDED is not allowed by fixture contract")
        findings.append(_finding("FINAL_STATUS_REJECTED", "final_status", "SUCCEEDED is not allowed"))
    return ContractFixtureResult(
        ok=not error_codes,
        error_codes=tuple(error_codes),
        errors=tuple(errors),
        findings=tuple(findings),
        recommended_action="none" if not error_codes else "repair_then_reverify",
    )


def _artifact_conditions(
    run_dir: Path,
    contract: dict[str, object],
    errors: list[str],
    error_codes: list[str],
    findings: list[dict[str, object]],
) -> dict[str, bool]:
    exists = True
    non_empty = True
    sections_present = True
    json_requirements_present = True
    for artifact in _required_artifacts(contract):
        path = run_dir / str(artifact.get("path") or "")
        if not path.exists():
            exists = False
            non_empty = False
            sections_present = False
            json_requirements_present = False
            error_codes.append("ARTIFACT_MISSING")
            errors.append(f"missing artifact: {path}")
            findings.append(_finding("ARTIFACT_MISSING", str(path), "required artifact is missing"))
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        min_size = _positive_int(artifact.get("min_size"), default=1)
        if path.stat().st_size < min_size:
            non_empty = False
            error_codes.append("ARTIFACT_TOO_SMALL")
            errors.append(f"artifact too small: {path}")
            findings.append(_finding("ARTIFACT_TOO_SMALL", str(path), "artifact is smaller than min_size"))
        for section in _string_list(artifact.get("required_sections")):
            if section not in text:
                sections_present = False
                error_codes.append("REQUIRED_SECTION_MISSING")
                errors.append(f"missing section {section}: {path}")
                findings.append(_finding("REQUIRED_SECTION_MISSING", str(path), section))
        json_requirements_present = _json_conditions(
            artifact,
            path,
            errors,
            error_codes,
        ) and json_requirements_present
    return {
        "artifact_exists": exists,
        "artifact_non_empty": non_empty,
        "json_requirements_present": json_requirements_present,
        "required_sections_present": sections_present,
    }


def _tool_conditions(
    contract: dict[str, object],
    tool_trace: list[dict[str, object]],
    errors: list[str],
    error_codes: list[str],
    findings: list[dict[str, object]],
) -> dict[str, bool]:
    required_calls = _string_list(_tools_contract(contract).get("required_calls"))
    required_successful_calls = _string_list(_tools_contract(contract).get("required_successful_calls"))
    if not tool_trace:
        error_codes.append("TOOL_TRACE_EMPTY")
        errors.append("tool trace is empty")
        findings.append(_finding("TOOL_TRACE_EMPTY", "tool_trace", "tool trace is empty"))
    called = {str(item.get("tool") or "") for item in tool_trace if isinstance(item, dict)}
    missing = [name for name in required_calls if name not in called]
    for name in missing:
        error_codes.append("REQUIRED_TOOL_CALL_MISSING")
        errors.append(f"required tool call missing: {name}")
        findings.append(_finding("REQUIRED_TOOL_CALL_MISSING", "tool_trace", name))
    successful = {
        str(item.get("tool") or "")
        for item in tool_trace
        if isinstance(item, dict) and bool(_tool_result(item).get("ok"))
    }
    missing_success = [name for name in required_successful_calls if name not in successful]
    for name in missing_success:
        error_codes.append("REQUIRED_SUCCESSFUL_TOOL_CALL_MISSING")
        errors.append(f"required successful tool call missing: {name}")
    return {
        "required_successful_tool_calls_present": not missing_success and bool(tool_trace or not required_successful_calls),
        "required_tool_calls_present": not missing and bool(tool_trace or not required_calls),
    }


def _json_conditions(
    artifact: dict[str, object],
    path: Path,
    errors: list[str],
    error_codes: list[str],
) -> bool:
    requirements = artifact.get("json_requirements")
    if not isinstance(requirements, dict):
        return True
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        error_codes.append("ARTIFACT_JSON_INVALID")
        errors.append(f"artifact json invalid: {path}")
        return False
    ok = True
    for key in _string_list(requirements.get("required_keys")):
        if _lookup_json_path(payload, key) is _MISSING:
            ok = False
            error_codes.append("REQUIRED_JSON_KEY_MISSING")
            errors.append(f"missing json key {key}: {path}")
    for key in _string_list(requirements.get("required_non_empty_paths")):
        value = _lookup_json_path(payload, key)
        if value is _MISSING or not _value_is_non_empty(value):
            ok = False
            error_codes.append("REQUIRED_JSON_COLLECTION_EMPTY")
            errors.append(f"json path empty {key}: {path}")
    return ok


def _succeeded_allowed(contract: dict[str, object], conditions: dict[str, bool]) -> bool:
    required = _string_list(_final_status_contract(contract).get("allow_succeeded_only_if"))
    return all(bool(conditions.get(condition)) for condition in required)


def _required_artifacts(contract: dict[str, object]) -> list[dict[str, object]]:
    artifacts = contract.get("artifacts")
    required = artifacts.get("required") if isinstance(artifacts, dict) else None
    return [dict(item) for item in required] if isinstance(required, list) else []


def _tools_contract(contract: dict[str, object]) -> dict[str, object]:
    tools = contract.get("tools")
    return dict(tools) if isinstance(tools, dict) else {}


def _final_status_contract(contract: dict[str, object]) -> dict[str, object]:
    final_status = contract.get("final_status")
    return dict(final_status) if isinstance(final_status, dict) else {}


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for raw in value if (item := str(raw).strip())]


def _positive_int(value: object, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, parsed)


def _finding(code: str, location: str, message: str) -> dict[str, object]:
    return {"code": code, "location": location, "message": message, "severity": "hard"}


def _tool_result(item: dict[str, object]) -> dict[str, object]:
    result = item.get("result")
    return dict(result) if isinstance(result, dict) else {}


_MISSING = object()


def _lookup_json_path(payload: object, path: str) -> object:
    current = payload
    for part in _split_json_path(path):
        if isinstance(part, int):
            if not isinstance(current, list) or part >= len(current):
                return _MISSING
            current = current[part]
            continue
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _split_json_path(path: str) -> list[object]:
    tokens: list[object] = []
    for chunk in str(path).split("."):
        raw = chunk.strip()
        if not raw:
            continue
        while "[" in raw and raw.endswith("]"):
            key, _, rest = raw.partition("[")
            if key:
                tokens.append(key)
            index = rest[:-1]
            try:
                tokens.append(int(index))
            except ValueError:
                return []
            raw = ""
        if raw:
            tokens.append(raw)
    return tokens


def _value_is_non_empty(value: object) -> bool:
    if isinstance(value, (list, tuple, dict, str, bytes)):
        return len(value) > 0
    return value not in {None, False}
