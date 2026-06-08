
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from ..common.value_parsing import sequence_strings
from .artifact_acceptance_models import ArtifactFinding
from .artifact_collection_evidence import item_evidence_findings
from .artifact_collection_mapping import mapping_findings
from .artifact_structured_contracts import positive_int


def collection_contract_findings(
    validation_contract: dict[str, object] | None,
    workspace_root: Path,
) -> list[ArtifactFinding]:
    contract = _collection_contract(validation_contract or {})
    if not contract:
        return []
    source_ref = str(contract.get("source_json_ref") or _staging_source_ref(validation_contract or "")).strip()
    if not source_ref:
        return [_finding("COLLECTION_SOURCE_REF_MISSING", "collection_contract.source_json_ref is required.")]
    source_path = _workspace_path(source_ref, workspace_root)
    if not _inside_workspace(source_path, workspace_root):
        return [_finding("COLLECTION_SOURCE_OUTSIDE_WORKSPACE", "collection source is outside workspace_root.", source_ref)]
    value, parse_finding = _read_json(source_path)
    if parse_finding is not None:
        return [parse_finding]
    return [
        *_group_findings(value, contract, source_ref),
        *_item_findings(value, contract, source_ref),
        *_completion_evidence_findings(value, contract, source_ref),
        *_source_claim_count_findings(value, contract, source_ref),
        *item_evidence_findings(value, contract, validation_contract or {}, source_ref),
        *mapping_findings(_all_items(value, contract), contract, source_ref, workspace_root),
    ]


def collection_contract_finding_dicts(
    validation_contract: dict[str, object] | None,
    workspace_root: Path,
) -> list[dict[str, object]]:
    return [item.to_dict() for item in collection_contract_findings(validation_contract, workspace_root)]


@dataclass(frozen=True)
class _ItemFindingContext:
    contract: dict[str, object]
    source_ref: str
    required_fields: list[str]


def _collection_contract(validation_contract: dict[str, object]) -> dict[str, object]:
    contract = validation_contract.get("collection_contract")
    return dict(contract) if isinstance(contract, dict) else {}


def _staging_source_ref(validation_contract: dict[str, object] | object) -> str:
    staging = validation_contract.get("staging_contract") if isinstance(validation_contract, dict) else None
    if not isinstance(staging, dict):
        return ""
    return str(staging.get("source_json_ref") or "")


def _read_json(path: Path) -> tuple[object, ArtifactFinding | None]:
    if not path.exists():
        return None, _finding("COLLECTION_SOURCE_MISSING", "collection source JSON does not exist.", str(path))
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, json.JSONDecodeError) as exc:
        return None, _finding("COLLECTION_SOURCE_INVALID", f"collection source JSON is invalid: {exc}", str(path))


def _group_findings(value: object, contract: dict[str, object], source_ref: str) -> list[ArtifactFinding]:
    groups = _groups(value, contract)
    min_groups = positive_int(contract.get("min_groups"))
    findings = _min_group_findings(groups, min_groups, source_ref)
    min_items = positive_int(contract.get("min_items_per_group"))
    if min_items:
        findings.extend(_group_item_count_findings(groups, contract, source_ref, min_items))
    return findings


def _min_group_findings(groups: list[object], min_groups: int, source_ref: str) -> list[ArtifactFinding]:
    if not min_groups or len(groups) >= min_groups:
        return []
    return [
        _finding(
            "COLLECTION_TOO_FEW_GROUPS",
            f"collection has {len(groups)} groups, expected at least {min_groups}.",
            source_ref,
            str(len(groups)),
        )
    ]


def _group_item_count_findings(
    groups: list[object],
    contract: dict[str, object],
    source_ref: str,
    min_items: int,
) -> list[ArtifactFinding]:
    return [
        _finding(
            "COLLECTION_GROUP_TOO_FEW_ITEMS",
            f"collection group has {item_count} items, expected at least {min_items}.",
            _group_location(source_ref, group, index),
            str(item_count),
        )
        for index, group in enumerate(groups)
        if (item_count := len(_items_from_group(group, contract))) < min_items
    ]


def _item_findings(value: object, contract: dict[str, object], source_ref: str) -> list[ArtifactFinding]:
    items = _all_items(value, contract)
    min_total = positive_int(contract.get("min_items_total"))
    findings = _min_item_findings(items, min_total, source_ref)
    required_fields = sequence_strings(contract.get("required_item_fields"))
    if required_fields:
        findings.extend(_item_shape_and_field_findings(items, contract, source_ref, required_fields))
    return findings


def _min_item_findings(items: list[object], min_total: int, source_ref: str) -> list[ArtifactFinding]:
    if not min_total or len(items) >= min_total:
        return []
    return [
        _finding(
            "COLLECTION_TOO_FEW_ITEMS",
            f"collection has {len(items)} items, expected at least {min_total}.",
            source_ref,
            str(len(items)),
        )
    ]


def _item_shape_and_field_findings(
    items: list[object],
    contract: dict[str, object],
    source_ref: str,
    required_fields: list[str],
) -> list[ArtifactFinding]:
    context = _ItemFindingContext(contract=contract, source_ref=source_ref, required_fields=required_fields)
    findings: list[ArtifactFinding] = []
    for index, item in enumerate(items):
        findings.extend(_single_item_findings(item, index, context))
    return findings


def _single_item_findings(
    item: object,
    index: int,
    context: _ItemFindingContext,
) -> list[ArtifactFinding]:
    if not isinstance(item, dict):
        return [_finding("COLLECTION_ITEM_SHAPE_INVALID", "collection item must be an object.", context.source_ref, str(index))]
    findings = _required_item_field_findings(item, context.source_ref, index, context.required_fields)
    findings.extend(_required_item_value_findings(item, context.contract, context.source_ref, index))
    findings.extend(_item_date_bound_findings(item, context.contract, context.source_ref, index))
    return findings


def _required_item_field_findings(
    item: dict[str, object],
    source_ref: str,
    index: int,
    required_fields: list[str],
) -> list[ArtifactFinding]:
    missing = [field for field in required_fields if not _has_value(item.get(field))]
    findings: list[ArtifactFinding] = []
    if missing:
        findings.append(
            _finding(
                "COLLECTION_ITEM_REQUIRED_FIELD_MISSING",
                "collection item is missing required fields.",
                f"{source_ref}#{index}",
                ",".join(missing),
            )
        )
    findings.extend(_placeholder_item_field_findings(item, source_ref, index, required_fields))
    return findings


def _placeholder_item_field_findings(
    item: dict[str, object],
    source_ref: str,
    index: int,
    required_fields: list[str],
) -> list[ArtifactFinding]:
    findings: list[ArtifactFinding] = []
    for field in required_fields:
        value = item.get(field)
        if not _is_placeholder_value(value):
            continue
        findings.append(
            _finding(
                "COLLECTION_ITEM_PLACEHOLDER_VALUE",
                "collection item required field still contains a placeholder value.",
                f"{source_ref}#{index}:{field}",
                _compact_json({"field": field, "value": value}),
            )
        )
    return findings


def _required_item_value_findings(
    item: dict[str, object],
    contract: dict[str, object],
    source_ref: str,
    index: int,
) -> list[ArtifactFinding]:
    rules = contract.get("required_item_values")
    if not isinstance(rules, dict):
        return []
    findings: list[ArtifactFinding] = []
    for path, expected in sorted(rules.items()):
        path_text = str(path).strip()
        if not path_text:
            continue
        actual = _lookup_path(item, path_text)
        if actual != expected:
            findings.append(
                _finding(
                    "COLLECTION_ITEM_VALUE_MISMATCH",
                    "collection item field value does not match the required machine contract.",
                    f"{source_ref}#{index}:{path_text}",
                    _compact_json({"expected": expected, "actual": actual}),
                )
            )
    return findings


def _item_date_bound_findings(
    item: dict[str, object],
    contract: dict[str, object],
    source_ref: str,
    index: int,
) -> list[ArtifactFinding]:
    bounds = contract.get("item_date_bounds")
    if not isinstance(bounds, dict):
        return []
    field = str(bounds.get("field") or "date").strip()
    if not field:
        return []
    raw_value = _lookup_path(item, field)
    actual = _parse_iso_date(raw_value)
    if actual is None:
        return [
            _finding(
                "COLLECTION_ITEM_DATE_INVALID",
                "collection item date cannot be parsed as ISO date.",
                f"{source_ref}#{index}:{field}",
                _compact_json({"actual": raw_value}),
            )
        ]
    findings: list[ArtifactFinding] = []
    min_date = _parse_iso_date(bounds.get("min"))
    max_date = _parse_iso_date(bounds.get("max"))
    if min_date is not None and actual < min_date:
        findings.append(
            _finding(
                "COLLECTION_ITEM_DATE_BEFORE_MIN",
                "collection item date is before the declared minimum date.",
                f"{source_ref}#{index}:{field}",
                _compact_json({"min": min_date.isoformat(), "actual": actual.isoformat()}),
            )
        )
    if max_date is not None and actual > max_date:
        findings.append(
            _finding(
                "COLLECTION_ITEM_DATE_AFTER_MAX",
                "collection item date is after the declared maximum date.",
                f"{source_ref}#{index}:{field}",
                _compact_json({"max": max_date.isoformat(), "actual": actual.isoformat()}),
            )
        )
    return findings


def _parse_iso_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) >= 10:
        text = text[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _completion_evidence_findings(value: object, contract: dict[str, object], source_ref: str) -> list[ArtifactFinding]:
    if not bool(contract.get("require_completion_evidence")):
        return []
    evidence_path = str(contract.get("completion_evidence_path") or "completion_evidence")
    evidence = _lookup_path(value, evidence_path)
    if _has_structured_data(evidence):
        return []
    return [
        _finding(
            "COLLECTION_COMPLETENESS_EVIDENCE_MISSING",
            "collection completeness evidence is required.",
            f"{source_ref}:{evidence_path}",
        )
    ]


def _source_claim_count_findings(value: object, contract: dict[str, object], source_ref: str) -> list[ArtifactFinding]:
    findings: list[ArtifactFinding] = []
    min_sources = positive_int(contract.get("min_source_refs"))
    min_claims = positive_int(contract.get("min_claims"))
    source_refs = _lookup_path(value, "source_refs")
    claims = _lookup_path(value, "claims")
    if min_sources and (not isinstance(source_refs, list) or len(source_refs) < min_sources):
        findings.append(_finding("COLLECTION_TOO_FEW_SOURCE_REFS", "collection has too few source refs.", source_ref, str(len(source_refs) if isinstance(source_refs, list) else 0)))
    if min_claims and (not isinstance(claims, list) or len(claims) < min_claims):
        findings.append(_finding("COLLECTION_TOO_FEW_CLAIMS", "collection has too few evidence claims.", source_ref, str(len(claims) if isinstance(claims, list) else 0)))
    return findings


def _groups(value: object, contract: dict[str, object]) -> list[object]:
    groups_path = str(contract.get("groups_path") or "").strip()
    if not groups_path and _items_path_is_missing(value, contract):
        groups_path = "sheets"
    if not groups_path:
        return []
    groups = _lookup_path(value, groups_path)
    return list(groups) if isinstance(groups, list) else []


def _all_items(value: object, contract: dict[str, object]) -> list[object]:
    groups = _groups(value, contract)
    if groups:
        return [item for group in groups for item in _items_from_group(group, contract)]
    items_path = str(contract.get("items_path") or "rows")
    items = _lookup_path(value, items_path)
    if isinstance(items, list):
        return list(items)
    return list(value) if isinstance(value, list) else []


def _items_from_group(group: object, contract: dict[str, object]) -> list[object]:
    items_path = str(contract.get("items_path") or "rows")
    items = _lookup_path(group, items_path)
    if isinstance(items, list):
        return list(items)
    return list(group) if isinstance(group, list) else []


def _items_path_is_missing(value: object, contract: dict[str, object]) -> bool:
    items_path = str(contract.get("items_path") or "rows")
    return not isinstance(_lookup_path(value, items_path), list)


def _lookup_path(value: object, path: str) -> object:
    current = value
    for part in [item for item in path.split(".") if item]:
        if isinstance(current, dict):
            current = current.get(part)
            continue
        return None
    return current


def _workspace_path(ref: str, workspace_root: Path) -> Path:
    path = Path(ref)
    return path if path.is_absolute() else (workspace_root / path).resolve()


def _inside_workspace(path: Path, workspace_root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(workspace_root.resolve(strict=False))
        return True
    except ValueError:
        return False


def _has_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip()) and not _is_placeholder_value(value)
    return True


def _is_placeholder_value(value: object) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False
    upper = text.upper()
    if upper.startswith("__FILL_") and upper.endswith("__"):
        return True
    if upper in {"__FILL__", "__TODO__", "PLACEHOLDER", "TODO", "TBD", "TO_BE_FILLED"}:
        return True
    return upper.startswith("{") and upper.endswith("}") and len(upper) <= 80


def _has_structured_data(value: object) -> bool:
    if isinstance(value, dict):
        return any(_has_structured_data(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_structured_data(item) for item in value)
    return _has_value(value)


def _group_location(source_ref: str, group: object, index: int) -> str:
    if isinstance(group, dict) and group.get("name"):
        return f"{source_ref}:{group.get('name')}"
    return f"{source_ref}#{index}"


def _finding(code: str, message: str, location: str = "", value: str = "") -> ArtifactFinding:
    return ArtifactFinding(code=code, severity="hard", message=message, location=location, value=value)


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

__all__ = ["collection_contract_finding_dicts", "collection_contract_findings"]
