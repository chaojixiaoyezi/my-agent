from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .contract_fixture_tool_checks import tool_conditions as _tool_conditions


@dataclass(frozen=True)
class ContractFixtureResult:
    ok: bool
    error_codes: tuple[str, ...]
    errors: tuple[str, ...]
    findings: tuple[dict[str, object], ...]
    recommended_action: str


@dataclass(frozen=True)
class FixtureRunFacts:
    tool_trace: tuple[dict[str, object], ...] = ()
    final_status: str = "UNKNOWN"
    runtime_issues: tuple[dict[str, object], ...] = ()


@dataclass
class _FixtureEvaluation:
    errors: list[str]
    error_codes: list[str]
    findings: list[dict[str, object]]

    def add(self, code: str, location: str, message: str) -> None:
        self.error_codes.append(code)
        self.errors.append(message)
        self.findings.append(_finding(code, location, message))


def verify_contract_fixture(
    run_dir: Path,
    contract: dict[str, object],
    facts: FixtureRunFacts,
) -> ContractFixtureResult:
    evaluation = _FixtureEvaluation(errors=[], error_codes=[], findings=[])
    artifact_conditions = _artifact_conditions(run_dir, contract, evaluation)
    tool_conditions = _tool_conditions(contract, facts.tool_trace, evaluation)
    runtime_conditions = _runtime_conditions(contract, facts.runtime_issues, evaluation)
    conditions = {**artifact_conditions, **tool_conditions, **runtime_conditions}
    final_status = str(facts.final_status).upper()
    runtime_conflicts = _runtime_success_conflicts(contract, facts.runtime_issues)
    if final_status == "UNKNOWN":
        evaluation.add("FINAL_STATUS_MISSING", "final_status", "final status is missing")
    if final_status == "SUCCEEDED" and runtime_conflicts:
        evaluation.add(
            "RUNTIME_ISSUE_SUCCESS_CONFLICT",
            "runtime_issues",
            f"runtime issues conflict with success: {','.join(runtime_conflicts)}",
        )
    if final_status == "SUCCEEDED" and not _succeeded_allowed(contract, conditions):
        evaluation.add(
            "FINAL_STATUS_REJECTED",
            "final_status",
            "final status SUCCEEDED is not allowed by fixture contract",
        )
    return ContractFixtureResult(
        ok=not evaluation.error_codes,
        error_codes=tuple(evaluation.error_codes),
        errors=tuple(evaluation.errors),
        findings=tuple(evaluation.findings),
        recommended_action="none" if not evaluation.error_codes else "repair_then_reverify",
    )


def _artifact_conditions(
    run_dir: Path,
    contract: dict[str, object],
    evaluation: _FixtureEvaluation,
) -> dict[str, bool]:
    exists = True
    non_empty = True
    sections_present = True
    json_requirements_present = True
    evidence_source_present = True
    path_inside_run_dir = True
    for artifact in _required_artifacts(contract):
        artifact_state = _evaluate_artifact(run_dir, artifact, evaluation)
        exists = exists and artifact_state["artifact_exists"]
        non_empty = non_empty and artifact_state["artifact_non_empty"]
        sections_present = sections_present and artifact_state["required_sections_present"]
        json_requirements_present = json_requirements_present and artifact_state["json_requirements_present"]
        evidence_source_present = evidence_source_present and artifact_state["evidence_source_present"]
        path_inside_run_dir = path_inside_run_dir and artifact_state["artifact_path_inside_run_dir"]
    return {
        "artifact_exists": exists,
        "artifact_non_empty": non_empty,
        "artifact_path_inside_run_dir": path_inside_run_dir,
        "evidence_source_present": evidence_source_present,
        "json_requirements_present": json_requirements_present,
        "required_sections_present": sections_present,
    }


def _evaluate_artifact(
    run_dir: Path,
    artifact: dict[str, object],
    evaluation: _FixtureEvaluation,
) -> dict[str, bool]:
    path = _artifact_path(run_dir, artifact, evaluation)
    if path is None:
        return {
            "artifact_exists": False,
            "artifact_non_empty": False,
            "artifact_path_inside_run_dir": False,
            "evidence_source_present": False,
            "json_requirements_present": False,
            "required_sections_present": False,
        }
    if not path.exists():
        evaluation.add("ARTIFACT_MISSING", str(path), "required artifact is missing")
        return {
            "artifact_exists": False,
            "artifact_non_empty": False,
            "artifact_path_inside_run_dir": True,
            "evidence_source_present": False,
            "json_requirements_present": False,
            "required_sections_present": False,
        }
    text = path.read_text(encoding="utf-8", errors="replace")
    non_empty = _artifact_meets_min_size(artifact, path, evaluation)
    sections_present = _artifact_has_required_sections(artifact, path, text, evaluation)
    return {
        "artifact_exists": True,
        "artifact_non_empty": non_empty,
        "artifact_path_inside_run_dir": True,
        "evidence_source_present": _evidence_source_conditions(artifact, path, evaluation),
        "json_requirements_present": _json_conditions(artifact, path, evaluation),
        "required_sections_present": sections_present,
    }


def _artifact_meets_min_size(
    artifact: dict[str, object],
    path: Path,
    evaluation: _FixtureEvaluation,
) -> bool:
    min_size = _positive_int(artifact.get("min_size"), default=1)
    if path.stat().st_size >= min_size:
        return True
    evaluation.add("ARTIFACT_TOO_SMALL", str(path), "artifact is smaller than min_size")
    return False


def _artifact_has_required_sections(
    artifact: dict[str, object],
    path: Path,
    text: str,
    evaluation: _FixtureEvaluation,
) -> bool:
    ok = True
    for section in _string_list(artifact.get("required_sections")):
        if section in text:
            continue
        ok = False
        evaluation.add("REQUIRED_SECTION_MISSING", str(path), section)
    return ok


def _json_conditions(
    artifact: dict[str, object],
    path: Path,
    evaluation: _FixtureEvaluation,
) -> bool:
    requirements = artifact.get("json_requirements")
    if not isinstance(requirements, dict):
        return True
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        evaluation.add("ARTIFACT_JSON_INVALID", str(path), f"artifact json invalid: {path}")
        return False
    ok = True
    for key in _string_list(requirements.get("required_keys")):
        if _lookup_json_path(payload, key) is _MISSING:
            ok = False
            evaluation.add("REQUIRED_JSON_KEY_MISSING", str(path), f"missing json key {key}: {path}")
    for key in _string_list(requirements.get("required_non_empty_paths")):
        value = _lookup_json_path(payload, key)
        if value is _MISSING or not _value_is_non_empty(value):
            ok = False
            evaluation.add("REQUIRED_JSON_COLLECTION_EMPTY", str(path), f"json path empty {key}: {path}")
    return ok


def _evidence_source_conditions(
    artifact: dict[str, object],
    path: Path,
    evaluation: _FixtureEvaluation,
) -> bool:
    requirements = artifact.get("evidence")
    if not isinstance(requirements, dict):
        return True
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        evaluation.add("ARTIFACT_JSON_INVALID", str(path), f"artifact json invalid: {path}")
        return False
    evidence = _lookup_json_path(payload, str(requirements.get("json_path") or "evidence"))
    if evidence is _MISSING or not _value_is_non_empty(evidence):
        evaluation.add("EVIDENCE_MISSING", str(path), "structured evidence is missing")
        return False
    if not bool(requirements.get("require_source")):
        return True
    ok = True
    for index, item in enumerate(_evidence_items(evidence)):
        if _evidence_item_has_source(item):
            continue
        ok = False
        evaluation.add("EVIDENCE_SOURCE_MISSING", f"{path}:evidence[{index}]", "evidence item needs source ref")
    return ok


def _succeeded_allowed(contract: dict[str, object], conditions: dict[str, bool]) -> bool:
    required = _string_list(_final_status_contract(contract).get("allow_succeeded_only_if"))
    return all(bool(conditions.get(condition)) for condition in required)


def _runtime_conditions(
    contract: dict[str, object],
    runtime_issues: list[dict[str, object]],
    evaluation: _FixtureEvaluation,
) -> dict[str, bool]:
    required = _string_list(_runtime_contract(contract).get("required_issue_codes"))
    present = _runtime_issue_codes(runtime_issues)
    missing = [code for code in required if code not in present]
    for code in missing:
        evaluation.add(
            "REQUIRED_RUNTIME_ISSUE_MISSING",
            "runtime_issues",
            f"required runtime issue missing: {code}",
        )
    return {
        "required_runtime_issue_codes_present": not missing,
        "runtime_success_allowed": not bool(_runtime_success_conflicts(contract, runtime_issues)),
    }


def _runtime_success_conflicts(contract: dict[str, object], runtime_issues: list[dict[str, object]]) -> list[str]:
    forbidden = set(_string_list(_runtime_contract(contract).get("forbid_succeeded_when_issue_codes_present")))
    if not forbidden:
        return []
    present = _runtime_issue_codes(runtime_issues)
    return sorted(code for code in present if code in forbidden)


def _required_artifacts(contract: dict[str, object]) -> list[dict[str, object]]:
    artifacts = contract.get("artifacts")
    required = artifacts.get("required") if isinstance(artifacts, dict) else None
    return [dict(item) for item in required] if isinstance(required, list) else []


def _runtime_contract(contract: dict[str, object]) -> dict[str, object]:
    runtime = contract.get("runtime")
    return dict(runtime) if isinstance(runtime, dict) else {}


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


def _runtime_issue_codes(items: list[dict[str, object]]) -> set[str]:
    return {
        code
        for item in items
        if isinstance(item, dict) and (code := str(item.get("code") or "").strip())
    }


def _artifact_path(
    run_dir: Path,
    artifact: dict[str, object],
    evaluation: _FixtureEvaluation,
) -> Path | None:
    raw_path = str(artifact.get("path") or "").strip()
    path = Path(raw_path)
    root = run_dir.resolve()
    candidate = path if path.is_absolute() else run_dir / path
    resolved = candidate.resolve()
    if not _is_relative_to(resolved, root):
        evaluation.add("ARTIFACT_PATH_OUTSIDE_RUN_DIR", raw_path or str(candidate), "artifact path escapes run dir")
        return None
    return resolved


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _evidence_items(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


def _evidence_item_has_source(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    return any(_value_is_non_empty(item.get(key)) for key in _EVIDENCE_SOURCE_KEYS)


_EVIDENCE_SOURCE_KEYS = (
    "artifact_ref",
    "source",
    "source_ref",
    "tool_call_id",
    "trace_ref",
)


_MISSING = object()


def _lookup_json_path(payload: object, path: str) -> object:
    current = payload
    for part in _split_json_path(path):
        current = _lookup_json_step(current, part)
        if current is _MISSING:
            return _MISSING
    return current


def _lookup_json_step(current: object, part: object) -> object:
    if isinstance(part, int):
        if not isinstance(current, list) or part >= len(current):
            return _MISSING
        return current[part]
    if not isinstance(current, dict) or part not in current:
        return _MISSING
    return current[part]


def _split_json_path(path: str) -> list[object]:
    tokens: list[object] = []
    for chunk in str(path).split("."):
        chunk_tokens = _split_json_chunk(chunk.strip())
        if chunk_tokens is _MISSING:
            return []
        tokens.extend(chunk_tokens)
    return tokens


def _split_json_chunk(raw: str) -> list[object] | object:
    if not raw:
        return []
    tokens: list[object] = []
    remainder = raw
    while "[" in remainder and remainder.endswith("]"):
        key, _, rest = remainder.partition("[")
        if key:
            tokens.append(key)
        index = _parse_json_index(rest[:-1])
        if index is _MISSING:
            return _MISSING
        tokens.append(index)
        remainder = ""
    if remainder:
        tokens.append(remainder)
    return tokens


def _parse_json_index(raw: str) -> int | object:
    try:
        return int(raw)
    except ValueError:
        return _MISSING


def _value_is_non_empty(value: object) -> bool:
    if isinstance(value, (list, tuple, dict, str, bytes)):
        return len(value) > 0
    return value not in {None, False}
