# LLM: delivery repair payload extraction keeps closeout parsing outside the guard decision loop.
# 模块用途: 从 closeout.json 的结构化字段生成 staged-delivery 修复 payload。

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .tool_delivery_repair_attempt import strict_write_required
from .tool_delivery_repair_paths import repair_target_snapshots
from .tool_delivery_repair_required_calls import required_tool_calls
from .tool_delivery_repair_scope import report_matches_current_contract

_WRITE_FIRST_ACTIONS = {
    "invoke_builder_tool",
    "materialize_checkpoint",
    "repair_artifact_against_findings",
    "repair_collection_item_values",
    "repair_evidence_refs",
    "repair_structured_checkpoint_json",
    "write_non_empty_structured_rows",
}
_LIST_FIELDS = {
    "finding_codes",
    "finding_values",
    "artifact_refs",
    "collection_item_updates",
    "input_artifacts",
    "repair_targets",
    "required_columns",
    "required_fields",
    "source_artifact_refs",
    "source_artifacts",
    "source_refs",
    "write_tools",
}
_DICT_FIELDS = {
    "collection_contract",
}
_TEXT_FIELDS = (
    "artifact_id",
    "artifact_path",
    "builder_tool",
    "category",
    "checkpoint_materialization_mode",
    "checkpoint_ref",
    "checkpoint_shape_hint",
    "code",
    "evidence_shape_hint",
    "missing_columns",
    "groups_path",
    "items_path",
    "output_ref",
    "recommended_action",
    "source_ref",
    "writer_tool",
)


# LLM: delivery_repair_payload extracts only active write-first recovery actions from closeout.json.
# 函数用途: 读取 closeout 报告并筛选写入/构建优先级恢复动作；不读取自然语言日志。
def delivery_repair_payload(
    agent: object,
    current_contract: object | None = None,
    *,
    enforce_contract_scope: bool = False,
) -> dict[str, object]:
    report = _closeout_report(agent)
    if not report or report.get("ok") is True:
        return {}
    if enforce_contract_scope and not report_matches_current_contract(report, current_contract):
        return {}
    progress = report.get("delivery_progress")
    agent_root = _agent_workspace_root(agent)
    actions = _required_actions(progress)
    actions = _merge_required_actions(actions, _refreshed_required_actions(report, current_contract, agent_root))
    actions = _prioritize_required_actions(actions)
    if not isinstance(progress, dict) or not actions:
        return {}
    pending_targets = progress.get("pending_materialization_targets")
    calls = _enrich_required_tool_calls(
        required_tool_calls(actions),
        actions,
        agent_root,
    )
    return {
        "pending_materialization_targets": pending_targets if isinstance(pending_targets, list) else [],
        "report_ref": str(report.get("report_ref") or ""),
        "required_actions": actions,
        "repair_target_snapshots": repair_target_snapshots(actions, agent_root),
        "required_tool_calls": calls,
        "strict_write_required": strict_write_required(progress, agent_root=agent_root),
    }


# LLM: _refreshed_required_actions derives current executable repairs from the active delivery contract.
# 函数用途: 旧 closeout 的 recovery_actions 可能缺少新门补出的 builder/action；用当前合同重新推导一次并合并。
def _refreshed_required_actions(
    report: dict[str, object],
    current_contract: object | None,
    agent_root: Path,
) -> list[dict[str, object]]:
    if not isinstance(current_contract, dict) or not current_contract:
        return []
    try:
        from .main_agent_delivery_closeout_recovery import _recovery_actions

        refreshed = _recovery_actions(report, contract=current_contract, workspace_root=agent_root)
    except Exception:
        return []
    return _required_actions({"recovery_actions": refreshed})


# LLM: _merge_required_actions preserves persisted repair actions while adding fresher contract-derived actions.
# 函数用途: 按结构化 action identity 去重合并，不用自然语言提示判断哪个动作重要。
def _merge_required_actions(
    existing: list[dict[str, object]],
    refreshed: list[dict[str, object]],
) -> list[dict[str, object]]:
    merged: list[dict[str, object]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for action in [*existing, *refreshed]:
        identity = _action_identity(action)
        if identity in seen:
            continue
        seen.add(identity)
        merged.append(action)
    return merged


def _prioritize_required_actions(actions: list[dict[str, object]]) -> list[dict[str, object]]:
    has_source_collection_repair = any(_is_source_collection_repair(action) for action in actions)
    return sorted(
        actions,
        key=lambda action: _action_priority(action, has_source_collection_repair=has_source_collection_repair),
    )


def _action_priority(action: dict[str, object], *, has_source_collection_repair: bool) -> int:
    if _is_source_collection_repair(action):
        return 0
    if has_source_collection_repair and str(action.get("recommended_action") or "") == "invoke_builder_tool":
        return 40
    if str(action.get("writer_tool") or ""):
        return 10
    if str(action.get("recommended_action") or "") == "repair_artifact_against_findings":
        return 20
    if str(action.get("recommended_action") or "") == "invoke_builder_tool":
        return 30
    return 50


def _is_source_collection_repair(action: dict[str, object]) -> bool:
    return (
        str(action.get("writer_tool") or "") == "api_json_collection"
        and str(action.get("checkpoint_ref") or "")
        and isinstance(action.get("collection_contract"), dict)
    )


# LLM: _action_identity uses stable machine fields to dedupe delivery repairs.
# 函数用途: 把同类 checkpoint/output/artifact 恢复动作折叠成一个，避免重复 required_tool_calls。
def _action_identity(action: dict[str, object]) -> tuple[str, str, str, str]:
    return (
        str(action.get("recommended_action") or ""),
        str(action.get("checkpoint_ref") or ""),
        str(action.get("output_ref") or ""),
        str(action.get("artifact_path") or ""),
    )


# LLM: _required_actions 是 agent_py_agent/agent/agent_core/tool_delivery_repair_payload.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 required actions 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _required_actions(progress: object) -> list[dict[str, object]]:
    if not isinstance(progress, dict):
        return []
    actions = progress.get("recovery_actions")
    if not isinstance(actions, list):
        return []
    return [
        normalized
        for item in actions
        for normalized in [_required_action(item)]
        if normalized
    ]


# LLM: _required_action 是 agent_py_agent/agent/agent_core/tool_delivery_repair_payload.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 required action 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _required_action(item: object) -> dict[str, object]:
    if not isinstance(item, dict):
        return {}
    if str(item.get("recommended_action") or "").strip() not in _WRITE_FIRST_ACTIONS:
        return {}
    payload = {field: str(item.get(field) or "") for field in _TEXT_FIELDS}
    payload.update({field: item.get(field) if isinstance(item.get(field), list) else [] for field in _LIST_FIELDS})
    payload.update({field: item.get(field) if isinstance(item.get(field), dict) else {} for field in _DICT_FIELDS})
    payload["required_sheets_min"] = item.get("required_sheets_min") or 0
    payload["retryable"] = bool(item.get("retryable", True))
    return payload


# LLM: _closeout_report keeps the repair guard grounded in the same machine report that closeout writes.
# 函数用途: 读取当前工作区 .agent_delivery/closeout.json；不存在或损坏时返回空对象。
def _closeout_report(agent: object) -> dict[str, object]:
    path = _agent_workspace_root(agent) / ".agent_delivery" / "closeout.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _enrich_required_tool_calls(
    calls: list[dict[str, object]],
    actions: list[dict[str, object]],
    agent_root: Path,
) -> list[dict[str, object]]:
    source_artifacts = _latest_source_artifacts(agent_root)
    enriched: list[dict[str, object]] = []
    for call in calls:
        next_call = (
            _enrich_api_collection_call(call, actions, source_artifacts, agent_root)
            if source_artifacts
            else call
        )
        enriched.append(_enrich_collection_markdown_write_call(next_call, agent_root))
    return enriched


def _enrich_api_collection_call(
    call: dict[str, object],
    actions: list[dict[str, object]],
    source_artifacts: list[dict[str, object]],
    agent_root: Path,
) -> dict[str, object]:
    if str(call.get("tool") or "").strip() != "api_json_collection":
        return call
    if any(call.get(key) for key in ("requests", "request_ranges", "source_artifacts")):
        return call
    action = _matching_action_for_call(call, actions)
    if not _source_evidence_action(action):
        return call
    enriched = dict(call)
    enriched["source_artifacts"] = source_artifacts
    enriched.setdefault("item_path", "results")
    enriched.setdefault("drop_incomplete_items", True)
    enriched["fields"] = _source_artifact_fields(enriched, action, agent_root)
    enriched["evidence_fields"] = _source_artifact_evidence_fields(enriched, action)
    return enriched


def _matching_action_for_call(call: dict[str, object], actions: list[dict[str, object]]) -> dict[str, object]:
    path = str(call.get("path") or "").strip()
    for action in actions:
        if path and path == str(action.get("checkpoint_ref") or "").strip():
            return action
    return {}


def _source_evidence_action(action: dict[str, object]) -> bool:
    return (
        str(action.get("checkpoint_materialization_mode") or "").strip() == "source_evidence_first"
        or str(action.get("writer_tool") or "").strip() == "api_json_collection"
        or bool(action.get("requires_auditable_source_evidence"))
    )


def _latest_source_artifacts(agent_root: Path, *, max_items: int = 4) -> list[dict[str, object]]:
    index = agent_root / "memory_archive" / "artifacts" / "tool_outputs" / "index.jsonl"
    try:
        lines = index.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records = [_artifact_record(line) for line in reversed(lines)]
    return [record for record in records if record][:max_items]


def _artifact_record(line: str) -> dict[str, object]:
    try:
        item = json.loads(line)
    except json.JSONDecodeError:
        return {}
    if not isinstance(item, dict) or str(item.get("tool") or "") not in {"fetch_url", "http_request", "web_search"}:
        return {}
    path = Path(str(item.get("path") or "")).expanduser()
    if not path.exists():
        return {}
    call_id = str(item.get("scoped_call_id") or item.get("call_id") or item.get("sha256") or path.name)
    return {
        "artifact_ref": str(path),
        "item_path": _artifact_item_path(str(item.get("tool") or "")),
        "limit": 20,
        "name": str(item.get("tool") or "source"),
        "reserved": {"tool_call_id": str(item.get("call_id") or ""), "scoped_call_id": call_id},
        "source_id": call_id,
    }


def _artifact_item_path(tool: str) -> str:
    return "results" if tool == "web_search" else "items"


def _source_artifact_fields(call: dict[str, object], action: dict[str, object], agent_root: Path) -> dict[str, object]:
    fields = call.get("fields")
    columns = _string_list(call.get("columns"))
    values = _required_item_values(action)
    existing_values = _existing_checkpoint_constant_values(action, agent_root)
    field_values = fields if isinstance(fields, dict) else {}
    return {
        column: value
        for column in columns
        for value in [_source_artifact_field_value(column, values, existing_values, field_values)]
        if value is not None
    }


def _source_artifact_field_value(
    column: str,
    values: dict[str, object],
    existing_values: dict[str, object],
    fields: dict[str, object],
) -> object | None:
    if column in values:
        return {"value": values[column]}
    inferred = _common_source_field(column)
    if inferred:
        return inferred
    if column in existing_values:
        return {"value": existing_values[column]}
    return fields.get(column)


def _source_artifact_evidence_fields(call: dict[str, object], action: dict[str, object]) -> list[str]:
    collection = action.get("collection_contract")
    if isinstance(collection, dict):
        fields = _string_list(collection.get("required_item_evidence_fields"))
        if fields:
            return fields
    return _string_list(call.get("evidence_fields"))


def _enrich_collection_markdown_write_call(call: dict[str, object], agent_root: Path) -> dict[str, object]:
    if str(call.get("tool") or "").strip() != "write_file":
        return call
    path = str(call.get("path") or "").strip()
    source_ref = str(call.get("source_ref") or "").strip()
    required_keys = call.get("mapping_required_keys")
    if not path.lower().endswith((".md", ".markdown")) or not source_ref or not isinstance(required_keys, list):
        return call
    rows = _collection_rows_from_ref(agent_root, source_ref)
    selected = _rows_matching_mapping_keys(
        rows,
        required_keys,
        _positive_int(call.get("mapping_required_count")) or len(required_keys),
    )
    if not selected:
        return call
    enriched = dict(call)
    enriched["content"] = _markdown_from_collection_rows(selected, source_ref=source_ref)
    enriched["mutation_intent"] = "create_or_replace"
    return enriched


def _collection_rows_from_ref(agent_root: Path, source_ref: str) -> list[dict[str, object]]:
    try:
        value = json.loads((agent_root / source_ref).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return _checkpoint_rows(value)


def _rows_matching_mapping_keys(
    rows: list[dict[str, object]],
    required_keys: list[object],
    required_count: int,
) -> list[dict[str, object]]:
    selected: list[dict[str, object]] = []
    for key in required_keys:
        if not isinstance(key, dict):
            continue
        match = _row_matching_key(rows, key)
        if match and not any(_same_row(match, existing) for existing in selected):
            selected.append(match)
        if len(selected) >= required_count:
            return selected
    for row in rows:
        if not any(_same_row(row, existing) for existing in selected):
            selected.append(row)
        if len(selected) >= required_count:
            break
    return selected


def _row_matching_key(rows: list[dict[str, object]], key: dict[object, object]) -> dict[str, object]:
    for row in rows:
        if all(str(row.get(str(field)) or "").strip() == str(value).strip() for field, value in key.items()):
            return row
    return {}


def _same_row(left: dict[str, object], right: dict[str, object]) -> bool:
    left_marker = json.dumps(left, ensure_ascii=False, sort_keys=True, default=str)
    right_marker = json.dumps(right, ensure_ascii=False, sort_keys=True, default=str)
    return left_marker == right_marker


def _markdown_from_collection_rows(rows: list[dict[str, object]], *, source_ref: str) -> str:
    lines = ["# 交付文档", "", f"> source_ref: `{source_ref}`", ""]
    for index, row in enumerate(rows, start=1):
        title = str(row.get("title") or row.get("name") or row.get("项目名") or f"Item {index}").strip()
        lines.extend([f"## {index}. {title}", ""])
        for key, value in row.items():
            if key == "field_source_ids" or isinstance(value, (dict, list)):
                continue
            lines.append(f"- {key}: {value}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _required_item_values(action: dict[str, object]) -> dict[str, object]:
    collection = action.get("collection_contract")
    values = collection.get("required_item_values") if isinstance(collection, dict) else None
    return {str(key): value for key, value in values.items() if str(key)} if isinstance(values, dict) else {}


def _common_source_field(column: str) -> object:
    normalized = column.strip().lower()
    mapping: dict[str, Any] = {
        "abstract": {"path": "snippet", "default_template": "{snippet}"},
        "date": {"paths": ["date", "published", "published_at", "updated"], "date_from_url": True},
        "published": {"paths": ["published", "published_at", "date", "updated"], "date_from_url": True},
        "published_at": {"paths": ["published_at", "published", "date", "updated"], "date_from_url": True},
        "snippet": "snippet",
        "title": "title",
        "url": "url",
        "uri": "url",
    }
    if normalized in {"authors", "author"}:
        return {"path": "authors", "default": []}
    return mapping.get(normalized)


def _existing_checkpoint_constant_values(action: dict[str, object], agent_root: Path) -> dict[str, object]:
    checkpoint_ref = str(action.get("checkpoint_ref") or "").strip()
    if not checkpoint_ref:
        return {}
    try:
        value = json.loads((agent_root / checkpoint_ref).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    rows = _checkpoint_rows(value)
    if not rows:
        return {}
    candidates: dict[str, object] = {}
    first = rows[0]
    for key, value in first.items():
        if key == "field_source_ids" or not _is_scalar_value(value) or not _has_value(value):
            continue
        if all(isinstance(row, dict) and row.get(key) == value for row in rows[1:]):
            candidates[str(key)] = value
    return candidates


def _checkpoint_rows(value: object) -> list[dict[str, object]]:
    if isinstance(value, dict) and isinstance(value.get("rows"), list):
        return [row for row in value["rows"] if isinstance(row, dict)]
    rows: list[dict[str, object]] = []
    sheets = value.get("sheets") if isinstance(value, dict) else None
    for sheet in sheets if isinstance(sheets, list) else []:
        sheet_rows = sheet.get("rows") if isinstance(sheet, dict) else None
        if isinstance(sheet_rows, list):
            rows.extend(row for row in sheet_rows if isinstance(row, dict))
    return rows


def _is_scalar_value(value: object) -> bool:
    return isinstance(value, (str, int, float, bool)) or value is None


def _has_value(value: object) -> bool:
    return bool(value.strip()) and not _is_placeholder_value(value) if isinstance(value, str) else value is not None


def _is_placeholder_value(value: str) -> bool:
    text = value.strip()
    upper = text.upper()
    return (
        upper.startswith("__FILL")
        or upper in {"TODO", "TBD", "N/A", "NA", "UNKNOWN", "NONE", "NULL"}
        or text in {"...", "待补充", "待定", "未知", "暂无", "无"}
    )


def _positive_int(value: object) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _agent_workspace_root(agent: object) -> Path:
    tools = getattr(agent, "tools", None)
    workspace = getattr(tools, "workspace_root", None)
    return Path(workspace or getattr(agent, "root", ".")).resolve()


def _string_list(value: object) -> list[str]:
    return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []


__all__ = ["delivery_repair_payload"]
