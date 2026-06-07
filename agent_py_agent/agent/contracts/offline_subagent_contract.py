
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..common.value_parsing import sequence_strings
from ..common.value_parsing import text_value as _text
from ..subagents.models import TaskStatus
from .contract_validation_recovery import recovery_for_findings

SUCCESS_STATUSES = {TaskStatus.DONE.value}
ACTIVE_PARENT_CLOSEOUT_STATUSES = {"VERIFYING", TaskStatus.DONE.value}


@dataclass(frozen=True)
class OfflineSubagentValidation:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, object], ...]
    recovery: dict[str, object] | None = None


def validate_subagent_contract(contract: dict[str, Any]) -> OfflineSubagentValidation:
    findings: list[dict[str, object]] = []
    parent = _section(contract.get("parent"))
    children = _record_list(contract.get("children"))
    children_by_id = {_text(child.get("run_id")): child for child in children if _text(child.get("run_id"))}
    _validate_explicit_limits(parent, children, _section(contract.get("limits")), findings)
    _validate_required_children(parent, children_by_id, findings)
    _validate_successful_children(children, findings)
    _validate_child_exports(children, findings)
    return OfflineSubagentValidation(
        ok=not findings,
        error_codes=tuple(dict.fromkeys(_text(item.get("code")) for item in findings)),
        findings=tuple(findings),
        recovery=recovery_for_findings("offline_subagent", findings),
    )


def _validate_explicit_limits(
    parent: dict[str, Any],
    children: tuple[dict[str, Any], ...],
    limits: dict[str, Any],
    findings: list[dict[str, object]],
) -> None:
    _validate_explicit_depth_limit(children, _optional_int(limits.get("max_depth")), findings)
    _validate_explicit_child_limit(parent, children, _optional_int(limits.get("max_children")), findings)


def _validate_explicit_depth_limit(
    children: tuple[dict[str, Any], ...],
    max_depth: int | None,
    findings: list[dict[str, object]],
) -> None:
    if max_depth is None:
        return
    for child in children:
        if _child_depth_exceeds(child, max_depth):
            findings.append(
                _finding(
                    "SUBAGENT_DEPTH_EXCEEDED",
                    {
                        "run_id": _text(child.get("run_id")),
                        "depth": _optional_int(child.get("depth")),
                        "max_depth": max_depth,
                    },
                )
            )


def _validate_explicit_child_limit(
    parent: dict[str, Any],
    children: tuple[dict[str, Any], ...],
    max_children: int | None,
    findings: list[dict[str, object]],
) -> None:
    if max_children is None:
        return
    parent_id = _text(parent.get("run_id"))
    child_count = sum(1 for child in children if _text(child.get("parent_run_id")) == parent_id)
    if child_count > max_children:
        findings.append(
            _finding(
                "SUBAGENT_CHILD_LIMIT_EXCEEDED",
                {
                    "parent_run_id": parent_id,
                    "child_count": child_count,
                    "max_children": max_children,
                },
            )
        )


def _child_depth_exceeds(child: dict[str, Any], max_depth: int) -> bool:
    depth = _optional_int(child.get("depth"))
    return depth is not None and depth > max_depth


def _validate_required_children(
    parent: dict[str, Any],
    children_by_id: dict[str, dict[str, Any]],
    findings: list[dict[str, object]],
) -> None:
    if _status(parent.get("status")) not in ACTIVE_PARENT_CLOSEOUT_STATUSES:
        return
    for child_id in sequence_strings(parent.get("required_child_run_ids")):
        child = children_by_id.get(child_id)
        if child is None:
            findings.append(_finding("CHILD_MISSING", {"child_run_id": child_id}))
            continue
        if _status(child.get("status")) == TaskStatus.TIMEOUT.value:
            findings.append(_finding("CHILD_TIMEOUT", {"child_run_id": child_id}))
            continue
        if _status(child.get("status")) not in SUCCESS_STATUSES:
            findings.append(
                _finding(
                    "CHILD_NOT_SUCCESSFUL",
                    {
                        "child_run_id": child_id,
                        "child_status": _status(child.get("status")),
                    },
                )
            )


def _validate_successful_children(
    children: tuple[dict[str, Any], ...],
    findings: list[dict[str, object]],
) -> None:
    for child in children:
        if _status(child.get("status")) not in SUCCESS_STATUSES:
            continue
        child_id = _text(child.get("run_id"))
        if not sequence_strings(child.get("artifact_refs")):
            findings.append(_finding("CHILD_ARTIFACT_MISSING", {"child_run_id": child_id}))
        acceptance = _section(child.get("acceptance"))
        if acceptance.get("ok") is not True or not sequence_strings(acceptance.get("evidence_refs")):
            findings.append(_finding("CHILD_ACCEPTANCE_MISSING", {"child_run_id": child_id}))


def _validate_child_exports(
    children: tuple[dict[str, Any], ...],
    findings: list[dict[str, object]],
) -> None:
    for child in children:
        export = _section(child.get("export"))
        if not export:
            continue
        if _text(export.get("kind")).lower() != "refs_only":
            findings.append(
                _finding(
                    "CHILD_CONTEXT_EXPORT_NOT_REFS_ONLY",
                    {
                        "child_run_id": _text(child.get("run_id")),
                        "export_kind": _text(export.get("kind")),
                    },
                )
            )


def _record_list(value: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


def _section(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _finding(code: str, extra: dict[str, object] | None = None) -> dict[str, object]:
    return {"code": code, **(extra or {})}


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _status(value: object) -> str:
    return _text(value).upper()


__all__ = ["OfflineSubagentValidation", "validate_subagent_contract"]
