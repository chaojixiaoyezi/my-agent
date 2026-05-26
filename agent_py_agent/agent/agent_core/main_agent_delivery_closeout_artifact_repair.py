# LLM: Artifact finding repair helpers turn validator facts into write-first recovery actions.
# 模块用途: 根据任意产物验收 findings 生成通用 repair action 和 bounded repair_targets，不从自然语言报告猜路径。

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .main_agent_delivery_closeout_recovery_models import RecoveryActionLedger

_MAX_FINDING_VALUES_PER_ACTION = 64
_SAFE_REPAIR_SUFFIX_RE = re.compile(r"^\.[a-z0-9][a-z0-9._+-]{0,63}$")


# LLM: append_artifact_finding_repair_actions 是 agent_py_agent/agent/agent_core/main_agent_delivery_closeout_artifact_repair.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 append artifact finding repair actions 相关的结构化数据、路径或 finding，供当前合同链路调用。
def append_artifact_finding_repair_actions(report: dict[str, Any], ledger: RecoveryActionLedger) -> None:
    for item in report.get("artifacts", []):
        if not isinstance(item, dict) or item.get("ok"):
            continue
        findings = list(artifact_findings(item))
        if not findings:
            continue
        action_key = f"ACCEPTANCE_ARTIFACT_REPAIR_REQUIRED:{item.get('artifact_id') or item.get('path')}"
        if action_key in ledger.seen:
            continue
        ledger.seen.add(action_key)
        ledger.actions.append(_artifact_finding_repair_action(item, findings))


# LLM: append_collection_value_repair_actions converts row-value findings into checkpoint updates.
# 函数用途: 将 COLLECTION_ITEM_VALUE_MISMATCH 这类机器 finding 转成 write_file 可执行更新，不让模型猜 JSON 文本。
def append_collection_value_repair_actions(
    report: dict[str, Any],
    contract: dict[str, Any],
    ledger: RecoveryActionLedger,
) -> None:
    for action in _collection_value_repair_actions(report, contract):
        action_key = f"{action['code']}:{action['checkpoint_ref']}"
        if action_key in ledger.seen:
            continue
        ledger.seen.add(action_key)
        ledger.actions.append(action)


def _collection_value_repair_actions(
    report: dict[str, Any],
    contract: dict[str, Any],
):
    for item in report.get("artifacts", []):
        yield from _collection_value_repair_actions_for_item(item, contract)


def _collection_value_repair_actions_for_item(item: object, contract: dict[str, Any]):
    if not isinstance(item, dict) or item.get("ok"):
        return
    collection_contract = _collection_contract_for_item(item, contract)
    if not collection_contract:
        return
    for checkpoint_ref, updates in _collection_updates_by_ref(artifact_findings(item), collection_contract).items():
        yield _collection_value_repair_action(checkpoint_ref, updates, collection_contract)
    for checkpoint_ref in _collection_placeholder_refs(artifact_findings(item), collection_contract):
        yield _collection_placeholder_repair_action(checkpoint_ref, collection_contract)
    for code, checkpoint_ref in _collection_count_refs(artifact_findings(item), collection_contract):
        yield _collection_count_repair_action(code, checkpoint_ref, collection_contract)
    for code, checkpoint_ref in _collection_date_refs(artifact_findings(item), collection_contract):
        yield _collection_date_repair_action(code, checkpoint_ref, collection_contract)
    if action := _collection_mapping_repair_action(artifact_findings(item), collection_contract):
        yield action


def _collection_contract_for_item(item: dict[str, Any], contract: dict[str, Any]) -> dict[str, object]:
    validation_contract = _artifact_validation_contract(item, contract)
    collection_contract = validation_contract.get("collection_contract")
    return dict(collection_contract) if isinstance(collection_contract, dict) else {}


# LLM: failed_findings 是 agent_py_agent/agent/agent_core/main_agent_delivery_closeout_artifact_repair.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 failed findings 相关的结构化数据、路径或 finding，供当前合同链路调用。
def failed_findings(report: dict[str, Any]):
    for item in report.get("artifacts", []):
        if item.get("ok"):
            continue
        yield from artifact_findings(item)


# LLM: artifact_findings 是 agent_py_agent/agent/agent_core/main_agent_delivery_closeout_artifact_repair.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 artifact findings 相关的结构化数据、路径或 finding，供当前合同链路调用。
def artifact_findings(item: dict[str, Any]):
    findings = item.get("acceptance_report", {}).get("findings", [])
    yield from (finding for finding in findings if isinstance(finding, dict))


# LLM: _artifact_validation_contract matches a closeout artifact to its original machine contract.
# 函数用途: 通过 artifact_id 回到 delivery_contract.artifacts，不从自然语言报告推断合同。
def _artifact_validation_contract(item: dict[str, Any], contract: dict[str, Any]) -> dict[str, object]:
    artifact_id = str(item.get("artifact_id") or "").strip()
    for artifact in contract.get("artifacts", []):
        if not isinstance(artifact, dict):
            continue
        if artifact_id and artifact_id == str(artifact.get("artifact_id") or "").strip():
            return _validation_contract(artifact)
    return {}


def _validation_contract(artifact: dict[str, object]) -> dict[str, object]:
    value = artifact.get("validation_contract")
    return dict(value) if isinstance(value, dict) else {}


# LLM: _collection_updates_by_ref groups row fixes by source checkpoint.
# 函数用途: 解析 finding.location 和 finding.value 的机器字段，形成去重后的集合更新列表。
def _collection_updates_by_ref(
    findings: Any,
    collection_contract: dict[str, object],
) -> dict[str, list[dict[str, object]]]:
    updates_by_ref: dict[str, list[dict[str, object]]] = {}
    seen: set[tuple[str, int, str]] = set()
    for finding in findings:
        update = _collection_update_from_finding(finding, collection_contract)
        if not update:
            continue
        checkpoint_ref = str(update.pop("checkpoint_ref"))
        key = (checkpoint_ref, int(update["item_index"]), str(update["field_path"]))
        if key in seen:
            continue
        seen.add(key)
        updates_by_ref.setdefault(checkpoint_ref, []).append(update)
    return updates_by_ref


def _collection_placeholder_refs(
    findings: Any,
    collection_contract: dict[str, object],
) -> list[str]:
    refs: list[str] = []
    declared_ref = str(collection_contract.get("source_json_ref") or "").strip()
    for finding in findings:
        if str(finding.get("code") or "") != "COLLECTION_ITEM_PLACEHOLDER_VALUE":
            continue
        location = _parse_collection_location(str(finding.get("location") or ""))
        if not location:
            continue
        checkpoint_ref = str(location["checkpoint_ref"])
        if declared_ref and _normalized_ref(checkpoint_ref) != _normalized_ref(declared_ref):
            continue
        if checkpoint_ref not in refs:
            refs.append(checkpoint_ref)
    return refs


def _collection_count_refs(
    findings: Any,
    collection_contract: dict[str, object],
) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    declared_ref = str(collection_contract.get("source_json_ref") or "").strip()
    for finding in findings:
        code = str(finding.get("code") or "")
        if code not in {"COLLECTION_TOO_FEW_ITEMS", "COLLECTION_TOO_FEW_GROUPS", "COLLECTION_GROUP_TOO_FEW_ITEMS"}:
            continue
        checkpoint_ref = _collection_finding_checkpoint_ref(finding, declared_ref)
        if not checkpoint_ref:
            continue
        key = (code, checkpoint_ref)
        if key in seen:
            continue
        seen.add(key)
        refs.append(key)
    return refs


def _collection_date_refs(
    findings: Any,
    collection_contract: dict[str, object],
) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    declared_ref = str(collection_contract.get("source_json_ref") or "").strip()
    for finding in findings:
        code = str(finding.get("code") or "")
        if code not in {"COLLECTION_ITEM_DATE_INVALID", "COLLECTION_ITEM_DATE_BEFORE_MIN", "COLLECTION_ITEM_DATE_AFTER_MAX"}:
            continue
        checkpoint_ref = _collection_finding_checkpoint_ref(finding, declared_ref)
        if not checkpoint_ref:
            continue
        key = (code, checkpoint_ref)
        if key in seen:
            continue
        seen.add(key)
        refs.append(key)
    return refs


def _collection_finding_checkpoint_ref(finding: dict[str, Any], declared_ref: str) -> str:
    location = _file_ref_head(str(finding.get("location") or ""))
    if declared_ref and location and _normalized_ref(location) != _normalized_ref(declared_ref):
        return ""
    return location or declared_ref


# LLM: _collection_update_from_finding reads only structured code/location/value fields.
# 函数用途: 从 source.json#row:field 和 {"expected":...} 中提取集合字段修复，不读取 message 文案。
def _collection_update_from_finding(
    finding: dict[str, Any],
    collection_contract: dict[str, object],
) -> dict[str, object]:
    if str(finding.get("code") or "") != "COLLECTION_ITEM_VALUE_MISMATCH":
        return {}
    location = _parse_collection_location(str(finding.get("location") or ""))
    expected = _expected_value(finding.get("value"))
    if not location or expected is _NO_EXPECTED_VALUE:
        return {}
    checkpoint_ref = str(location["checkpoint_ref"])
    declared_ref = str(collection_contract.get("source_json_ref") or "").strip()
    if declared_ref and _normalized_ref(checkpoint_ref) != _normalized_ref(declared_ref):
        return {}
    return {
        "checkpoint_ref": checkpoint_ref,
        "item_index": location["item_index"],
        "field_path": location["field_path"],
        "value": expected,
    }


def _parse_collection_location(location: str) -> dict[str, object]:
    checkpoint_ref, marker, tail = location.partition("#")
    if not marker:
        return {}
    index_text, sep, field_path = tail.partition(":")
    if not sep:
        return {}
    try:
        item_index = int(index_text)
    except ValueError:
        return {}
    if item_index < 0 or not field_path.strip():
        return {}
    return {
        "checkpoint_ref": checkpoint_ref.strip().replace("\\", "/"),
        "item_index": item_index,
        "field_path": field_path.strip(),
    }


_NO_EXPECTED_VALUE = object()


def _expected_value(value: object) -> object:
    if not isinstance(value, str):
        return _NO_EXPECTED_VALUE
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return _NO_EXPECTED_VALUE
    if not isinstance(parsed, dict) or "expected" not in parsed:
        return _NO_EXPECTED_VALUE
    return parsed["expected"]


def _normalized_ref(value: str) -> str:
    return value.strip().replace("\\", "/")


def _collection_value_repair_action(
    checkpoint_ref: str,
    updates: list[dict[str, object]],
    collection_contract: dict[str, object],
) -> dict[str, object]:
    action: dict[str, object] = {
        "code": "COLLECTION_ITEM_VALUE_MISMATCH",
        "category": "artifact",
        "retryable": True,
        "recommended_action": "repair_collection_item_values",
        "checkpoint_ref": checkpoint_ref,
        "writer_tool": "write_file",
        "write_tools": ["write_file"],
        "items_path": str(collection_contract.get("items_path") or "rows"),
        "collection_item_updates": updates,
        "recovery_hint": "集合 JSON 的行级机器字段不符合合同；按 collection_item_updates 更新 checkpoint 后重新验收。",
    }
    groups_path = str(collection_contract.get("groups_path") or "").strip()
    if groups_path:
        action["groups_path"] = groups_path
    return action


def _collection_placeholder_repair_action(
    checkpoint_ref: str,
    collection_contract: dict[str, object],
) -> dict[str, object]:
    action: dict[str, object] = {
        "code": "COLLECTION_ITEM_PLACEHOLDER_VALUE",
        "category": "artifact",
        "retryable": True,
        "recommended_action": "repair_structured_checkpoint_json",
        "checkpoint_ref": checkpoint_ref,
        "writer_tool": "write_file",
        "write_tools": ["write_file"],
        "items_path": str(collection_contract.get("items_path") or "rows"),
        "collection_contract": dict(collection_contract),
        "required_columns": _collection_required_columns(collection_contract),
        "recovery_hint": "集合 JSON 的必填字段仍含模板占位值；重新采集或重写来源 checkpoint，不能保留 __FILL__/TODO 这类占位符。",
    }
    groups_path = str(collection_contract.get("groups_path") or "").strip()
    if groups_path:
        action["groups_path"] = groups_path
    return action


def _collection_count_repair_action(
    code: str,
    checkpoint_ref: str,
    collection_contract: dict[str, object],
) -> dict[str, object]:
    action: dict[str, object] = {
        "code": code,
        "category": "artifact",
        "retryable": True,
        "recommended_action": "repair_structured_checkpoint_json",
        "checkpoint_ref": checkpoint_ref,
        "writer_tool": "write_file",
        "write_tools": ["write_file"],
        "items_path": str(collection_contract.get("items_path") or "rows"),
        "collection_contract": dict(collection_contract),
        "required_columns": _collection_required_columns(collection_contract),
        "recovery_hint": "集合 JSON 的数量没有达到合同要求；继续采集或补齐来源 checkpoint，保持 source_refs/claims/completion_evidence 可审计。",
    }
    groups_path = str(collection_contract.get("groups_path") or "").strip()
    if groups_path:
        action["groups_path"] = groups_path
    return action


def _collection_date_repair_action(
    code: str,
    checkpoint_ref: str,
    collection_contract: dict[str, object],
) -> dict[str, object]:
    action = _collection_count_repair_action(code, checkpoint_ref, collection_contract)
    action["recovery_hint"] = "集合 JSON 的日期字段不满足时间窗口合同；重新采集或过滤来源 checkpoint，保留满足 item_date_bounds 的可审计条目。"
    return action


def _collection_required_columns(collection_contract: dict[str, object]) -> list[str]:
    fields = collection_contract.get("required_item_fields")
    return [str(item).strip() for item in fields if str(item).strip()] if isinstance(fields, list) else []


def _collection_mapping_repair_action(
    findings: Any,
    collection_contract: dict[str, object],
) -> dict[str, object]:
    finding_values = [
        str(finding.get("value") or "").strip()
        for finding in findings
        if isinstance(finding, dict) and str(finding.get("code") or "") == "ARTIFACT_MAPPING_MISSING"
    ]
    finding_values = [value for value in finding_values if value]
    mapping = collection_contract.get("mapping")
    artifact_ref = str(mapping.get("artifact_ref") or "").strip() if isinstance(mapping, dict) else ""
    source_ref = str(collection_contract.get("source_json_ref") or "").strip()
    if not finding_values or not artifact_ref or not source_ref:
        return {}
    return {
        "category": "artifact",
        "code": "ARTIFACT_MAPPING_MISSING",
        "recommended_action": "repair_artifact_against_findings",
        "artifact_path": artifact_ref,
        "checkpoint_ref": source_ref,
        "finding_codes": ["ARTIFACT_MAPPING_MISSING"],
        "finding_values": finding_values[:_MAX_FINDING_VALUES_PER_ACTION],
        "repair_targets": [artifact_ref],
        "retryable": True,
        "source_ref": source_ref,
        "write_tools": ["write_file", "apply_patch", "write_file"],
        "recovery_hint": "集合映射产物缺少 source_json_ref 中的条目；按结构化来源生成或修复目标文档后重新验收。",
    }


# LLM: _artifact_finding_repair_action 是 agent_py_agent/agent/agent_core/main_agent_delivery_closeout_artifact_repair.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 artifact finding repair action 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _artifact_finding_repair_action(
    item: dict[str, Any],
    findings: list[dict[str, Any]],
) -> dict[str, object]:
    return {
        "code": "ACCEPTANCE_ARTIFACT_REPAIR_REQUIRED",
        "category": "artifact",
        "retryable": True,
        "recommended_action": "repair_artifact_against_findings",
        "artifact_id": str(item.get("artifact_id") or ""),
        "artifact_kind": str(item.get("kind") or ""),
        "artifact_path": str(item.get("path") or ""),
        "finding_codes": _finding_values(findings, "code"),
        "finding_values": _finding_values(findings, "value"),
        "repair_targets": _repair_targets(item, findings),
        "write_tools": ["write_file", "apply_patch", "write_file"],
        "recovery_hint": "产物验收已给出结构化 findings；优先修改对应产物文件，然后重新验收。",
    }


# LLM: _finding_values 是 agent_py_agent/agent/agent_core/main_agent_delivery_closeout_artifact_repair.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 finding values 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _finding_values(findings: list[dict[str, Any]], key: str) -> list[str]:
    values: list[str] = []
    for finding in findings:
        value = str(finding.get(key) or "").strip()
        if value and value not in values:
            values.append(value)
    return values[:_MAX_FINDING_VALUES_PER_ACTION]


# LLM: _repair_targets 是 agent_py_agent/agent/agent_core/main_agent_delivery_closeout_artifact_repair.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 repair targets 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _repair_targets(item: dict[str, Any], findings: list[dict[str, Any]]) -> list[str]:
    artifact_path = Path(str(item.get("path") or "")).expanduser()
    candidates = _finding_file_targets(artifact_path, findings)
    if artifact_path.is_file():
        candidates.append(artifact_path)
    elif artifact_path.is_dir():
        candidates.extend(_existing_text_targets(artifact_path))
    return _unique_paths(candidates)[:8]


# LLM: _finding_file_targets 是 agent_py_agent/agent/agent_core/main_agent_delivery_closeout_artifact_repair.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 finding file targets 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _finding_file_targets(artifact_path: Path, findings: list[dict[str, Any]]) -> list[Path]:
    targets: list[Path] = []
    for finding in findings:
        targets.extend(_finding_targets_for_one_finding(artifact_path, finding))
    return targets


# LLM: _finding_targets_for_one_finding isolates per-finding file-ref repair extraction.
# 函数用途: 从一个结构化 finding 的 location/value 里提取可修复文件目标，控制主循环复杂度。
def _finding_targets_for_one_finding(artifact_path: Path, finding: dict[str, Any]) -> list[Path]:
    targets: list[Path] = []
    for value in (str(finding.get("location") or ""), str(finding.get("value") or "")):
        target = _repair_target_for_ref(artifact_path, _file_ref_head(value), finding)
        if target is not None:
            targets.append(target)
    return targets


# LLM: _repair_target_for_ref validates one structured file reference before path resolution.
# 函数用途: 把 finding 中的单个文件引用转成安全路径；非文件引用返回 None。
def _repair_target_for_ref(artifact_path: Path, head: str, finding: dict[str, Any]) -> Path | None:
    if not _is_repair_file_ref(artifact_path, head, finding):
        return None
    return _safe_artifact_related_path(artifact_path, head)


# LLM: _file_ref_head 是 agent_py_agent/agent/agent_core/main_agent_delivery_closeout_artifact_repair.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 file ref head 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _file_ref_head(value: str) -> str:
    head = value.split(":", 1)[0].split("#", 1)[0].strip()
    return head.replace("\\", "/")


# LLM: _existing_text_targets 是 agent_py_agent/agent/agent_core/main_agent_delivery_closeout_artifact_repair.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 existing text targets 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _existing_text_targets(artifact_path: Path) -> list[Path]:
    try:
        files = [
            path
            for path in artifact_path.rglob("*")
            if path.is_file() and _has_safe_repair_suffix(path)
        ]
    except OSError:
        return []
    return sorted(files, key=lambda path: (len(path.parts), str(path)))[:8]


# LLM: _is_repair_file_ref keeps repair target discovery open-world while avoiding dotted API names.
# 函数用途: 只接受安全文件引用；新格式靠真实文件或带路径 ref 进入，不靠固定后缀表。
def _is_repair_file_ref(artifact_path: Path, value: str, finding: dict[str, Any]) -> bool:
    if not value or not _has_safe_repair_suffix(Path(value)):
        return False
    if "/" in value or "\\" in value:
        return True
    target = _safe_artifact_related_path(artifact_path, value)
    if target and target.exists() and target.is_file():
        return True
    return _finding_declares_file_ref(finding)


# LLM: _finding_declares_file_ref trusts structured finding codes, not file-type suffix enums.
# 函数用途: 缺失文件尚不存在时，只有文件类机器 finding 才能把短文件名加入 repair_targets。
def _finding_declares_file_ref(finding: dict[str, Any]) -> bool:
    code = str(finding.get("code") or "").upper()
    return "FILE" in code


# LLM: _has_safe_repair_suffix validates suffix shape instead of closed file-type enums.
# 函数用途: 判断路径是否像安全文件名，避免开放格式因未登记在表里被丢弃。
def _has_safe_repair_suffix(path: Path) -> bool:
    return bool(path.name and _SAFE_REPAIR_SUFFIX_RE.fullmatch(path.suffix.lower()))


# LLM: _safe_artifact_child 是 agent_py_agent/agent/agent_core/main_agent_delivery_closeout_artifact_repair.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 safe artifact child 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _safe_artifact_child(artifact_path: Path, rel: str) -> Path | None:
    if Path(rel).is_absolute():
        return None
    root = artifact_path if artifact_path.is_dir() else artifact_path.parent
    try:
        candidate = (root / rel).resolve()
        candidate.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return candidate


# LLM: _safe_artifact_related_path 是 agent_py_agent/agent/agent_core/main_agent_delivery_closeout_artifact_repair.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 safe artifact related path 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _safe_artifact_related_path(artifact_path: Path, rel: str) -> Path | None:
    if Path(rel).is_absolute() or _has_parent_ref(rel):
        return None
    workspace_ref = _workspace_relative_candidate(artifact_path, rel)
    if workspace_ref is not None:
        return workspace_ref
    for root in _candidate_repair_roots(artifact_path):
        try:
            candidate = (root / rel).resolve()
            candidate.relative_to(root.resolve())
        except (OSError, ValueError):
            continue
        if candidate.exists():
            return candidate
    return _safe_artifact_child(artifact_path, rel)


# LLM: _workspace_relative_candidate resolves outputs/... style refs against ancestor roots.
# 函数用途: 当 finding 给出工作区相对路径时，先按已有顶层目录定位，避免重复拼接 artifact 子目录。
def _workspace_relative_candidate(artifact_path: Path, rel: str) -> Path | None:
    first_part = Path(rel).parts[0] if Path(rel).parts else ""
    if not first_part:
        return None
    for root in _candidate_repair_roots(artifact_path):
        try:
            existing_anchor = (root / first_part).resolve(strict=False)
            if not existing_anchor.exists() or not existing_anchor.is_dir():
                continue
            candidate = (root / rel).resolve(strict=False)
            candidate.relative_to(root.resolve(strict=False))
        except (OSError, ValueError):
            continue
        return candidate
    return None


# LLM: _candidate_repair_roots 是 agent_py_agent/agent/agent_core/main_agent_delivery_closeout_artifact_repair.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 candidate repair roots 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _candidate_repair_roots(artifact_path: Path) -> list[Path]:
    start = artifact_path if artifact_path.is_dir() else artifact_path.parent
    roots = [start, *list(start.parents)]
    return roots[:8]


# LLM: _has_parent_ref 是 agent_py_agent/agent/agent_core/main_agent_delivery_closeout_artifact_repair.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 has parent ref 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _has_parent_ref(rel: str) -> bool:
    return any(part == ".." for part in Path(rel).parts)


# LLM: _unique_paths 是 agent_py_agent/agent/agent_core/main_agent_delivery_closeout_artifact_repair.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 unique paths 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _unique_paths(paths: list[Path]) -> list[str]:
    seen: set[str] = set()
    values: list[str] = []
    for path in paths:
        value = str(path)
        if value and value not in seen:
            seen.add(value)
            values.append(value)
    return values


__all__ = [
    "append_artifact_finding_repair_actions",
    "append_collection_value_repair_actions",
    "artifact_findings",
    "failed_findings",
]
