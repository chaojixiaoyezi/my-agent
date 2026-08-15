
from __future__ import annotations

import json
from dataclasses import dataclass

from ..common.value_parsing import sequence_strings
from .artifact_acceptance_models import ArtifactFinding
from .artifact_collection_source_refs import source_ref_is_audited, source_refs_by_id


@dataclass(frozen=True)
class _EvidenceScope:
    source_ref: str
    validation_contract: dict[str, object]
    sources_by_id: dict[str, dict[str, object]]
    claims: list[dict[str, object]]


def item_evidence_findings(
    value: object,
    contract: dict[str, object],
    validation_contract: dict[str, object],
    source_ref: str,
) -> list[ArtifactFinding]:
    required_fields = _required_item_evidence_fields(contract, validation_contract)
    if not required_fields:
        return []
    scope = _EvidenceScope(
        source_ref=source_ref,
        validation_contract=validation_contract,
        sources_by_id=source_refs_by_id(value, _lookup_path),
        claims=_claim_records(value),
    )
    findings: list[ArtifactFinding] = []
    for context in _item_contexts(value, contract):
        item = context["item"]
        if isinstance(item, dict):
            findings.extend(_one_item_evidence_findings(item, context, required_fields, scope))
    return findings


def _one_item_evidence_findings(
    item: dict[str, object],
    context: dict[str, object],
    required_fields: list[str],
    scope: _EvidenceScope,
) -> list[ArtifactFinding]:
    findings: list[ArtifactFinding] = []
    for field in required_fields:
        if not _has_value(item.get(field)):
            continue
        finding = _field_evidence_finding(field, item, context, scope)
        if finding is not None:
            findings.append(finding)
    return findings


def _field_evidence_finding(
    field: str,
    item: dict[str, object],
    context: dict[str, object],
    scope: _EvidenceScope,
) -> ArtifactFinding | None:
    source_ids = _item_field_source_ids(item, field)
    if source_ids:
        missing = [source_id for source_id in source_ids if source_id not in scope.sources_by_id]
        if missing:
            return _finding(
                "COLLECTION_ITEM_EVIDENCE_SOURCE_MISSING",
                "collection item field references unknown source ids.",
                _item_location(scope.source_ref, context, field),
                _compact_json({"field": field, "missing_source_ids": missing}),
            )
        unaudited = [source_id for source_id in source_ids if not source_ref_is_audited(scope.sources_by_id[source_id])]
        if unaudited:
            return _finding(
                "COLLECTION_ITEM_EVIDENCE_SOURCE_UNAUDITED",
                "collection item field source lacks tool/audit binding.",
                _item_location(scope.source_ref, context, field),
                _compact_json({"field": field, "source_ids": unaudited}),
            )
        return None
    if _has_scoped_claim(field, item.get(field), context, scope):
        return None
    return _finding(
        "COLLECTION_ITEM_EVIDENCE_FIELD_MISSING",
        "collection item field lacks row-scoped machine evidence.",
        _item_location(scope.source_ref, context, field),
        field,
    )


def _required_item_evidence_fields(
    contract: dict[str, object],
    validation_contract: dict[str, object],
) -> list[str]:
    explicit = sequence_strings(contract.get("required_item_evidence_fields"))
    if explicit:
        return explicit
    evidence = validation_contract.get("evidence_contract")
    if not isinstance(evidence, dict) or contract.get("require_item_evidence") is False:
        return []
    required = sequence_strings(evidence.get("required_fields"))
    if required and (bool(contract.get("require_item_evidence")) or _has_collection_items_contract(contract)):
        return required
    return []


def _has_collection_items_contract(contract: dict[str, object]) -> bool:
    return bool(
        str(contract.get("groups_path") or "").strip()
        or str(contract.get("items_path") or "").strip()
        or sequence_strings(contract.get("required_item_fields"))
    )


def _item_contexts(value: object, contract: dict[str, object]) -> list[dict[str, object]]:
    groups = _groups(value, contract)
    if groups:
        return [context for group_index, group in enumerate(groups) for context in _group_item_contexts(group, group_index, contract)]
    return _flat_item_contexts(value, contract)


def _group_item_contexts(group: object, group_index: int, contract: dict[str, object]) -> list[dict[str, object]]:
    group_path = str(contract.get("groups_path") or "groups")
    items_path = str(contract.get("items_path") or "rows")
    return [
        {
            "group_index": group_index,
            "group_name": _group_name(group),
            "item": item,
            "item_index": item_index,
            "item_path": f"{group_path}[{group_index}].{items_path}[{item_index}]",
        }
        for item_index, item in enumerate(_items_from_group(group, contract))
    ]


def _flat_item_contexts(value: object, contract: dict[str, object]) -> list[dict[str, object]]:
    items_path = str(contract.get("items_path") or "rows")
    items = _lookup_path(value, items_path)
    item_list = list(items) if isinstance(items, list) else list(value) if isinstance(value, list) else []
    return [
        {"group_index": -1, "group_name": "", "item": item, "item_index": index, "item_path": f"{items_path}[{index}]"}
        for index, item in enumerate(item_list)
    ]


def _groups(value: object, contract: dict[str, object]) -> list[object]:
    groups_path = str(contract.get("groups_path") or "").strip()
    if not groups_path and _items_path_is_missing(value, contract):
        groups_path = "sheets"
    if not groups_path:
        return []
    groups = _lookup_path(value, groups_path)
    return list(groups) if isinstance(groups, list) else []


def _items_from_group(group: object, contract: dict[str, object]) -> list[object]:
    items_path = str(contract.get("items_path") or "rows")
    items = _lookup_path(group, items_path)
    if isinstance(items, list):
        return list(items)
    return list(group) if isinstance(group, list) else []


def _items_path_is_missing(value: object, contract: dict[str, object]) -> bool:
    items_path = str(contract.get("items_path") or "rows")
    return not isinstance(_lookup_path(value, items_path), list)


def _claim_records(value: object) -> list[dict[str, object]]:
    claims = _lookup_path(value, "claims")
    return [item for item in claims if isinstance(item, dict)] if isinstance(claims, list) else []


def _item_field_source_ids(item: dict[str, object], field: str) -> list[str]:
    for holder in _source_mapping_holders(item):
        if values := _holder_field_source_ids(holder, field):
            return values
    return _string_refs(item.get("source_ids"))


def _holder_field_source_ids(holder: dict[str, object], field: str) -> list[str]:
    for mapping_key in ("field_source_ids", "source_ids_by_field", "evidence_source_ids", "evidence_refs_by_field"):
        if values := _source_ids_from_mapping(holder.get(mapping_key), field):
            return values
    return []


def _source_mapping_holders(item: dict[str, object]) -> list[dict[str, object]]:
    evidence = item.get("evidence")
    return [item, evidence] if isinstance(evidence, dict) else [item]


def _source_ids_from_mapping(value: object, field: str) -> list[str]:
    if not isinstance(value, dict):
        return []
    return _string_refs(value.get(field))


def _has_scoped_claim(
    field: str,
    value: object,
    context: dict[str, object],
    scope: _EvidenceScope,
) -> bool:
    probe = (field, value, context)
    return any(_claim_covers_item_field(probe, claim, scope) for claim in scope.claims)


def _claim_covers_item_field(
    probe: tuple[str, object, dict[str, object]],
    claim: dict[str, object],
    scope: _EvidenceScope,
) -> bool:
    field, value, context = probe
    if str(claim.get("field") or "") != field or not _same_value(claim.get("value"), value):
        return False
    source_ids = _string_refs(claim.get("source_ids"))
    if not source_ids or any(source_id not in scope.sources_by_id for source_id in source_ids):
        return False
    if any(not source_ref_is_audited(scope.sources_by_id[source_id]) for source_id in source_ids):
        return False
    if _requires_verified(scope.validation_contract) and str(claim.get("verification_status") or "VERIFIED") != "VERIFIED":
        return False
    return _claim_matches_item_scope(claim, context)


def _claim_matches_item_scope(claim: dict[str, object], context: dict[str, object]) -> bool:
    if str(claim.get("item_path") or "") == str(context.get("item_path") or ""):
        return True
    if _item_key_matches(claim.get("item_key"), context.get("item")):
        return True
    return _index_scope_matches(claim, context)


def _item_key_matches(item_key: object, item: object) -> bool:
    if not isinstance(item_key, dict) or not isinstance(item, dict) or not item_key:
        return False
    return all(_same_value(item.get(str(key)), val) for key, val in item_key.items())


def _index_scope_matches(claim: dict[str, object], context: dict[str, object]) -> bool:
    if "item_index" not in claim:
        return False
    try:
        if int(str(claim.get("item_index"))) != int(str(context.get("item_index"))):
            return False
        group_index = int(str(context.get("group_index") or -1))
        return group_index < 0 or _group_scope_matches(claim, context, group_index)
    except ValueError:
        return False


def _group_scope_matches(claim: dict[str, object], context: dict[str, object], group_index: int) -> bool:
    if claim.get("group_index") is not None:
        try:
            return int(str(claim.get("group_index"))) == group_index
        except ValueError:
            return False
    group_name = str(claim.get("group_name") or "")
    return bool(group_name and group_name == str(context.get("group_name") or ""))


def _lookup_path(value: object, path: str) -> object:
    current = value
    for part in [item for item in path.split(".") if item]:
        if isinstance(current, dict):
            current = current.get(part)
            continue
        return None
    return current


def _has_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _requires_verified(validation_contract: dict[str, object]) -> bool:
    evidence = validation_contract.get("evidence_contract")
    return not isinstance(evidence, dict) or bool(evidence.get("require_verified", True))


def _string_refs(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := str(item).strip())]


def _same_value(left: object, right: object) -> bool:
    return left == right or str(left) == str(right)


def _group_name(group: object) -> str:
    return str(group.get("name") or "") if isinstance(group, dict) else ""


def _item_location(source_ref: str, context: dict[str, object], field: str) -> str:
    return f"{source_ref}:{context.get('item_path')}:{field}"


def _finding(code: str, message: str, location: str = "", value: str = "") -> ArtifactFinding:
    return ArtifactFinding(code=code, severity="hard", message=message, location=location, value=value)


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

__all__ = ["item_evidence_findings"]
