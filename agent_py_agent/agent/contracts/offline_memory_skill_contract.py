
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..common.value_parsing import sequence_strings
from ..common.value_parsing import text_value as _text
from .contract_validation_recovery import recovery_for_findings

SECRET_FIELD_NAMES = {
    "api_key",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
}
CURRENT_FACT_USAGES = {"current_fact", "authoritative_fact", "current_context"}


@dataclass(frozen=True)
class OfflineMemorySkillValidation:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, object], ...]
    recovery: dict[str, object] | None = None


def validate_memory_skill_contract(contract: dict[str, Any]) -> OfflineMemorySkillValidation:
    findings: list[dict[str, object]] = []
    _validate_memory_writes(_record_list(contract.get("memory_writes")), findings)
    _validate_memory_uses(
        _record_list(contract.get("memory_records")),
        _record_list(contract.get("memory_uses")),
        findings,
    )
    _validate_memory_retrievals(_record_list(contract.get("memory_retrievals")), findings)
    _validate_skill_manifests(
        _record_list(contract.get("skill_manifests")),
        _section(contract.get("tool_manifest")),
        findings,
    )
    return OfflineMemorySkillValidation(
        ok=not findings,
        error_codes=tuple(dict.fromkeys(_text(item.get("code")) for item in findings)),
        findings=tuple(findings),
        recovery=recovery_for_findings("offline_memory_skill", findings),
    )


def _validate_memory_writes(records: tuple[dict[str, Any], ...], findings: list[dict[str, object]]) -> None:
    for record in records:
        memory_ref = _text(record.get("memory_ref"))
        if not _valid_scope(_section(record.get("scope"))):
            findings.append(_finding("MEMORY_SCOPE_MISSING", {"memory_ref": memory_ref}))
        if not _valid_validation(_section(record.get("validation"))):
            findings.append(_finding("MEMORY_VALIDATION_MISSING", {"memory_ref": memory_ref}))
        for field_path in _secret_field_paths(record):
            findings.append(
                _finding(
                    "MEMORY_SECRET_FIELD",
                    {"memory_ref": memory_ref, "field_path": field_path},
                )
            )


def _validate_memory_uses(
    records: tuple[dict[str, Any], ...],
    uses: tuple[dict[str, Any], ...],
    findings: list[dict[str, object]],
) -> None:
    records_by_ref = {_text(record.get("memory_ref")): record for record in records if _text(record.get("memory_ref"))}
    for use in uses:
        memory_ref = _text(use.get("memory_ref"))
        usage = _text(use.get("usage")).lower()
        record = records_by_ref.get(memory_ref, {})
        if usage in CURRENT_FACT_USAGES and record.get("is_stale") is True:
            findings.append(_finding("MEMORY_FACT_STALE", {"memory_ref": memory_ref, "usage": usage}))


def _validate_memory_retrievals(
    retrievals: tuple[dict[str, Any], ...],
    findings: list[dict[str, object]],
) -> None:
    for retrieval in retrievals:
        query_ref = _text(retrieval.get("query_ref"))
        selected = set(sequence_strings(retrieval.get("selected_refs")))
        expected = set(sequence_strings(retrieval.get("expected_refs")))
        irrelevant = set(sequence_strings(retrieval.get("irrelevant_refs")))
        missing = sorted(expected - selected)
        noisy = sorted(selected & irrelevant)
        if missing:
            findings.append(_finding("MEMORY_RECALL_EXPECTED_MISSING", {"query_ref": query_ref, "missing_refs": missing}))
        if noisy:
            findings.append(_finding("MEMORY_RECALL_IRRELEVANT_SELECTED", {"query_ref": query_ref, "irrelevant_refs": noisy}))


def _validate_skill_manifests(
    manifests: tuple[dict[str, Any], ...],
    tool_manifest: dict[str, Any],
    findings: list[dict[str, object]],
) -> None:
    visible_tools = set(sequence_strings(tool_manifest.get("visible_tools")))
    for manifest in manifests:
        skill_id = _text(manifest.get("skill_id") or manifest.get("name"))
        if not _section_non_empty(manifest.get("trigger")):
            findings.append(_finding("SKILL_TRIGGER_MISSING", {"skill_id": skill_id}))
        if not _section_non_empty(manifest.get("input_contract")):
            findings.append(_finding("SKILL_INPUT_CONTRACT_MISSING", {"skill_id": skill_id}))
        _validate_skill_safety(manifest, skill_id, findings)
        _validate_skill_tools(manifest, visible_tools, skill_id, findings)


def _validate_skill_safety(
    manifest: dict[str, Any],
    skill_id: str,
    findings: list[dict[str, object]],
) -> None:
    safety_scan = _section(manifest.get("safety_scan"))
    finding_codes = sequence_strings(safety_scan.get("finding_codes"))
    if safety_scan.get("ok") is not True or finding_codes:
        findings.append(
            _finding(
                "SKILL_SAFETY_FINDING",
                {"skill_id": skill_id, "finding_codes": finding_codes},
            )
        )


def _validate_skill_tools(
    manifest: dict[str, Any],
    visible_tools: set[str],
    skill_id: str,
    findings: list[dict[str, object]],
) -> None:
    allowed_tools = sequence_strings(manifest.get("allowed_tools"))
    missing_tools = [tool for tool in allowed_tools if tool not in visible_tools]
    if missing_tools:
        findings.append(
            _finding(
                "SKILL_TOOL_NOT_VISIBLE",
                {"skill_id": skill_id, "missing_tools": missing_tools},
            )
        )


def _valid_scope(scope: dict[str, Any]) -> bool:
    return bool(_text(scope.get("namespace")) and _text(scope.get("owner_type")))


def _valid_validation(validation: dict[str, Any]) -> bool:
    return validation.get("ok") is True and bool(sequence_strings(validation.get("evidence_refs")))


def _secret_field_paths(value: object, prefix: str = "") -> tuple[str, ...]:
    paths: list[str] = []
    stack: list[tuple[str, object]] = [(prefix, value)]
    while stack:
        current_prefix, current = stack.pop()
        if isinstance(current, dict):
            paths.extend(_dict_secret_field_paths(current_prefix, current, stack))
            continue
        if isinstance(current, (list, tuple)):
            stack.extend(_indexed_children(current_prefix, current))
    return tuple(paths)


def _dict_secret_field_paths(
    prefix: str,
    value: dict[object, object],
    stack: list[tuple[str, object]],
) -> list[str]:
    paths: list[str] = []
    for key, child in value.items():
        key_text = _text(key)
        path = f"{prefix}.{key_text}" if prefix else key_text
        if key_text.lower() in SECRET_FIELD_NAMES:
            paths.append(path)
        stack.append((path, child))
    return paths


def _indexed_children(prefix: str, value: list[object] | tuple[object, ...]) -> list[tuple[str, object]]:
    return [
        (f"{prefix}[{index}]" if prefix else f"[{index}]", child)
        for index, child in enumerate(value)
    ]


def _record_list(value: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


def _section_non_empty(value: object) -> bool:
    section = _section(value)
    return bool(section) and any(_present(item) for item in section.values())


def _present(value: object) -> bool:
    if isinstance(value, (list, tuple, set)):
        return bool(sequence_strings(value))
    if isinstance(value, dict):
        return _section_non_empty(value)
    return bool(_text(value))


def _finding(code: str, extra: dict[str, object] | None = None) -> dict[str, object]:
    return {"code": code, **(extra or {})}


def _section(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


__all__ = ["OfflineMemorySkillValidation", "validate_memory_skill_contract"]
