# LLM: Row evidence contracts bind structured collection rows to source refs.
# 模块用途: 校验表格/资料清单的每个关键字段都有机器来源，避免全局 claim 冒充逐行证据。

from __future__ import annotations

import json
from dataclasses import dataclass

from .artifact_acceptance_models import ArtifactFinding
from .artifact_structured_contracts import string_list


# LLM: _EvidenceScope 是 agent_py_agent/agent/contracts/artifact_collection_evidence.py 的结构化数据载体；修改字段时同步检查调用方和合同测试。
# 类用途: 保存 EvidenceScope 相关的机器事实，供合同、验收或恢复流程复用。
@dataclass(frozen=True)
class _EvidenceScope:
    source_ref: str
    validation_contract: dict[str, object]
    sources_by_id: dict[str, dict[str, object]]
    claims: list[dict[str, object]]


# LLM: item_evidence_findings validates row-scoped evidence without task-specific keywords.
# 函数用途: 根据 collection_contract/evidence_contract 检查每行字段 source_ids 或 scoped claims。
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
        sources_by_id=_source_refs_by_id(value),
        claims=_claim_records(value),
    )
    findings: list[ArtifactFinding] = []
    for context in _item_contexts(value, contract):
        item = context["item"]
        if isinstance(item, dict):
            findings.extend(_one_item_evidence_findings(item, context, required_fields, scope))
    return findings


# LLM: _one_item_evidence_findings 是 agent_py_agent/agent/contracts/artifact_collection_evidence.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 one item evidence findings 相关的结构化数据、路径或 finding，供当前合同链路调用。
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


# LLM: _field_evidence_finding 是 agent_py_agent/agent/contracts/artifact_collection_evidence.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 field evidence finding 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _field_evidence_finding(
    field: str,
    item: dict[str, object],
    context: dict[str, object],
    scope: _EvidenceScope,
) -> ArtifactFinding | None:
    source_ids = _item_field_source_ids(item, field)
    if source_ids:
        missing = [source_id for source_id in source_ids if source_id not in scope.sources_by_id]
        return (
            _finding(
                "COLLECTION_ITEM_EVIDENCE_SOURCE_MISSING",
                "collection item field references unknown source ids.",
                _item_location(scope.source_ref, context, field),
                _compact_json({"field": field, "missing_source_ids": missing}),
            )
            if missing
            else None
        )
    if _has_scoped_claim(field, item.get(field), context, scope):
        return None
    return _finding(
        "COLLECTION_ITEM_EVIDENCE_FIELD_MISSING",
        "collection item field lacks row-scoped machine evidence.",
        _item_location(scope.source_ref, context, field),
        field,
    )


# LLM: _required_item_evidence_fields 是 agent_py_agent/agent/contracts/artifact_collection_evidence.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 required item evidence fields 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _required_item_evidence_fields(
    contract: dict[str, object],
    validation_contract: dict[str, object],
) -> list[str]:
    explicit = string_list(contract.get("required_item_evidence_fields"))
    if explicit:
        return explicit
    evidence = validation_contract.get("evidence_contract")
    if not isinstance(evidence, dict) or contract.get("require_item_evidence") is False:
        return []
    required = string_list(evidence.get("required_fields"))
    if required and (bool(contract.get("require_item_evidence")) or _has_collection_items_contract(contract)):
        return required
    return []


# LLM: _has_collection_items_contract 是 agent_py_agent/agent/contracts/artifact_collection_evidence.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 has collection items contract 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _has_collection_items_contract(contract: dict[str, object]) -> bool:
    return bool(
        str(contract.get("groups_path") or "").strip()
        or str(contract.get("items_path") or "").strip()
        or string_list(contract.get("required_item_fields"))
    )


# LLM: _item_contexts 是 agent_py_agent/agent/contracts/artifact_collection_evidence.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 item contexts 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _item_contexts(value: object, contract: dict[str, object]) -> list[dict[str, object]]:
    groups = _groups(value, contract)
    if groups:
        return [context for group_index, group in enumerate(groups) for context in _group_item_contexts(group, group_index, contract)]
    return _flat_item_contexts(value, contract)


# LLM: _group_item_contexts 是 agent_py_agent/agent/contracts/artifact_collection_evidence.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 group item contexts 相关的结构化数据、路径或 finding，供当前合同链路调用。
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


# LLM: _flat_item_contexts 是 agent_py_agent/agent/contracts/artifact_collection_evidence.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 flat item contexts 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _flat_item_contexts(value: object, contract: dict[str, object]) -> list[dict[str, object]]:
    items_path = str(contract.get("items_path") or "rows")
    items = _lookup_path(value, items_path)
    item_list = list(items) if isinstance(items, list) else list(value) if isinstance(value, list) else []
    return [
        {"group_index": -1, "group_name": "", "item": item, "item_index": index, "item_path": f"{items_path}[{index}]"}
        for index, item in enumerate(item_list)
    ]


# LLM: _groups 是 agent_py_agent/agent/contracts/artifact_collection_evidence.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 groups 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _groups(value: object, contract: dict[str, object]) -> list[object]:
    groups_path = str(contract.get("groups_path") or "").strip()
    if not groups_path:
        return []
    groups = _lookup_path(value, groups_path)
    return list(groups) if isinstance(groups, list) else []


# LLM: _items_from_group 是 agent_py_agent/agent/contracts/artifact_collection_evidence.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 items from group 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _items_from_group(group: object, contract: dict[str, object]) -> list[object]:
    items_path = str(contract.get("items_path") or "rows")
    items = _lookup_path(group, items_path)
    if isinstance(items, list):
        return list(items)
    return list(group) if isinstance(group, list) else []


# LLM: _source_refs_by_id 是 agent_py_agent/agent/contracts/artifact_collection_evidence.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 source refs by id 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _source_refs_by_id(value: object) -> dict[str, dict[str, object]]:
    refs = _lookup_path(value, "source_refs")
    if not isinstance(refs, list):
        return {}
    result: dict[str, dict[str, object]] = {}
    for item in refs:
        if isinstance(item, dict):
            _add_source_ref(result, item)
    return result


# LLM: _add_source_ref 是 agent_py_agent/agent/contracts/artifact_collection_evidence.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 add source ref 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _add_source_ref(result: dict[str, dict[str, object]], item: dict[str, object]) -> None:
    source_id = str(item.get("source_id") or "").strip()
    if source_id and (str(item.get("uri") or "").strip() or str(item.get("artifact_ref") or "").strip()):
        result[source_id] = item


# LLM: _claim_records 是 agent_py_agent/agent/contracts/artifact_collection_evidence.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 claim records 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _claim_records(value: object) -> list[dict[str, object]]:
    claims = _lookup_path(value, "claims")
    return [item for item in claims if isinstance(item, dict)] if isinstance(claims, list) else []


# LLM: _item_field_source_ids 是 agent_py_agent/agent/contracts/artifact_collection_evidence.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 item field source ids 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _item_field_source_ids(item: dict[str, object], field: str) -> list[str]:
    for holder in _source_mapping_holders(item):
        if values := _holder_field_source_ids(holder, field):
            return values
    return _string_refs(item.get("source_ids"))


# LLM: _holder_field_source_ids 是 agent_py_agent/agent/contracts/artifact_collection_evidence.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 holder field source ids 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _holder_field_source_ids(holder: dict[str, object], field: str) -> list[str]:
    for mapping_key in ("field_source_ids", "source_ids_by_field", "evidence_source_ids", "evidence_refs_by_field"):
        if values := _source_ids_from_mapping(holder.get(mapping_key), field):
            return values
    return []


# LLM: _source_mapping_holders 是 agent_py_agent/agent/contracts/artifact_collection_evidence.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 source mapping holders 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _source_mapping_holders(item: dict[str, object]) -> list[dict[str, object]]:
    evidence = item.get("evidence")
    return [item, evidence] if isinstance(evidence, dict) else [item]


# LLM: _source_ids_from_mapping 是 agent_py_agent/agent/contracts/artifact_collection_evidence.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 source ids from mapping 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _source_ids_from_mapping(value: object, field: str) -> list[str]:
    if not isinstance(value, dict):
        return []
    return _string_refs(value.get(field))


# LLM: _has_scoped_claim 是 agent_py_agent/agent/contracts/artifact_collection_evidence.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 has scoped claim 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _has_scoped_claim(
    field: str,
    value: object,
    context: dict[str, object],
    scope: _EvidenceScope,
) -> bool:
    probe = (field, value, context)
    return any(_claim_covers_item_field(probe, claim, scope) for claim in scope.claims)


# LLM: _claim_covers_item_field 是 agent_py_agent/agent/contracts/artifact_collection_evidence.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 claim covers item field 相关的结构化数据、路径或 finding，供当前合同链路调用。
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
    if _requires_verified(scope.validation_contract) and str(claim.get("verification_status") or "VERIFIED") != "VERIFIED":
        return False
    return _claim_matches_item_scope(claim, context)


# LLM: _claim_matches_item_scope 是 agent_py_agent/agent/contracts/artifact_collection_evidence.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 claim matches item scope 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _claim_matches_item_scope(claim: dict[str, object], context: dict[str, object]) -> bool:
    reserved = claim.get("reserved")
    if not isinstance(reserved, dict):
        return False
    if str(reserved.get("item_path") or "") == str(context.get("item_path") or ""):
        return True
    if _item_key_matches(reserved.get("item_key"), context.get("item")):
        return True
    return _index_scope_matches(reserved, context)


# LLM: _item_key_matches 是 agent_py_agent/agent/contracts/artifact_collection_evidence.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 item key matches 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _item_key_matches(item_key: object, item: object) -> bool:
    if not isinstance(item_key, dict) or not isinstance(item, dict) or not item_key:
        return False
    return all(_same_value(item.get(str(key)), val) for key, val in item_key.items())


# LLM: _index_scope_matches 是 agent_py_agent/agent/contracts/artifact_collection_evidence.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 index scope matches 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _index_scope_matches(reserved: dict[str, object], context: dict[str, object]) -> bool:
    if "item_index" not in reserved:
        return False
    try:
        if int(str(reserved.get("item_index"))) != int(str(context.get("item_index"))):
            return False
        group_index = int(str(context.get("group_index") or -1))
        return group_index < 0 or _group_scope_matches(reserved, context, group_index)
    except ValueError:
        return False


# LLM: _group_scope_matches 是 agent_py_agent/agent/contracts/artifact_collection_evidence.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 group scope matches 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _group_scope_matches(reserved: dict[str, object], context: dict[str, object], group_index: int) -> bool:
    if reserved.get("group_index") is not None:
        try:
            return int(str(reserved.get("group_index"))) == group_index
        except ValueError:
            return False
    group_name = str(reserved.get("group_name") or "")
    return bool(group_name and group_name == str(context.get("group_name") or ""))


# LLM: _lookup_path 是 agent_py_agent/agent/contracts/artifact_collection_evidence.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 lookup path 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _lookup_path(value: object, path: str) -> object:
    current = value
    for part in [item for item in path.split(".") if item]:
        if isinstance(current, dict):
            current = current.get(part)
            continue
        return None
    return current


# LLM: _has_value 是 agent_py_agent/agent/contracts/artifact_collection_evidence.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 has value 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _has_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


# LLM: _requires_verified 是 agent_py_agent/agent/contracts/artifact_collection_evidence.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 requires verified 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _requires_verified(validation_contract: dict[str, object]) -> bool:
    evidence = validation_contract.get("evidence_contract")
    return not isinstance(evidence, dict) or bool(evidence.get("require_verified", True))


# LLM: _string_refs 是 agent_py_agent/agent/contracts/artifact_collection_evidence.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 string refs 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _string_refs(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := str(item).strip())]


# LLM: _same_value 是 agent_py_agent/agent/contracts/artifact_collection_evidence.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 same value 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _same_value(left: object, right: object) -> bool:
    return left == right or str(left) == str(right)


# LLM: _group_name 是 agent_py_agent/agent/contracts/artifact_collection_evidence.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 group name 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _group_name(group: object) -> str:
    return str(group.get("name") or "") if isinstance(group, dict) else ""


# LLM: _item_location 是 agent_py_agent/agent/contracts/artifact_collection_evidence.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 item location 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _item_location(source_ref: str, context: dict[str, object], field: str) -> str:
    return f"{source_ref}:{context.get('item_path')}:{field}"


# LLM: _finding 是 agent_py_agent/agent/contracts/artifact_collection_evidence.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 finding 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _finding(code: str, message: str, location: str = "", value: str = "") -> ArtifactFinding:
    return ArtifactFinding(code=code, severity="hard", message=message, location=location, value=value)


# LLM: _compact_json 是 agent_py_agent/agent/contracts/artifact_collection_evidence.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 compact json 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

__all__ = ["item_evidence_findings"]
