from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ContractFixtureResult:
    ok: bool
    error_codes: tuple[str, ...]
    errors: tuple[str, ...]


def verify_contract_fixture(
    run_dir: Path,
    contract: dict[str, object],
    *,
    tool_trace: list[dict[str, object]],
    final_status: str,
) -> ContractFixtureResult:
    errors: list[str] = []
    error_codes: list[str] = []
    artifact_conditions = _artifact_conditions(run_dir, contract, errors, error_codes)
    tool_conditions = _tool_conditions(contract, tool_trace, errors, error_codes)
    conditions = {**artifact_conditions, **tool_conditions}
    if str(final_status).upper() == "SUCCEEDED" and not _succeeded_allowed(contract, conditions):
        error_codes.append("FINAL_STATUS_REJECTED")
        errors.append("final status SUCCEEDED is not allowed by fixture contract")
    return ContractFixtureResult(
        ok=not error_codes,
        error_codes=tuple(error_codes),
        errors=tuple(errors),
    )


def _artifact_conditions(
    run_dir: Path,
    contract: dict[str, object],
    errors: list[str],
    error_codes: list[str],
) -> dict[str, bool]:
    exists = True
    non_empty = True
    sections_present = True
    for artifact in _required_artifacts(contract):
        path = run_dir / str(artifact.get("path") or "")
        if not path.exists():
            exists = False
            non_empty = False
            sections_present = False
            error_codes.append("ARTIFACT_MISSING")
            errors.append(f"missing artifact: {path}")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        min_size = _positive_int(artifact.get("min_size"), default=1)
        if path.stat().st_size < min_size:
            non_empty = False
            error_codes.append("ARTIFACT_TOO_SMALL")
            errors.append(f"artifact too small: {path}")
        for section in _string_list(artifact.get("required_sections")):
            if section not in text:
                sections_present = False
                error_codes.append("REQUIRED_SECTION_MISSING")
                errors.append(f"missing section {section}: {path}")
    return {
        "artifact_exists": exists,
        "artifact_non_empty": non_empty,
        "required_sections_present": sections_present,
    }


def _tool_conditions(
    contract: dict[str, object],
    tool_trace: list[dict[str, object]],
    errors: list[str],
    error_codes: list[str],
) -> dict[str, bool]:
    required_calls = _string_list(_tools_contract(contract).get("required_calls"))
    if not tool_trace:
        error_codes.append("TOOL_TRACE_EMPTY")
        errors.append("tool trace is empty")
    called = {str(item.get("tool") or "") for item in tool_trace if isinstance(item, dict)}
    missing = [name for name in required_calls if name not in called]
    for name in missing:
        error_codes.append("REQUIRED_TOOL_CALL_MISSING")
        errors.append(f"required tool call missing: {name}")
    return {"required_tool_calls_present": not missing and bool(tool_trace or not required_calls)}


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
