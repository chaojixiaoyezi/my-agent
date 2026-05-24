# LLM: delivery repair required calls turn machine recovery actions into executable tool skeletons.
# 模块用途: 从 recovery_actions 生成 required_tool_calls，让模型看到下一步应调用的结构化工具参数。

from __future__ import annotations

import json

_CREATE_OR_REPLACE_FINDING_CODES = frozenset(
    {
        "ARTIFACT_MISSING",
        "STATIC_SITE_MISSING_REQUIRED_FILES",
    }
)
_FULL_REWRITE_FINDING_CODES = frozenset(
    {
        "ARTIFACT_MAPPING_MISSING",
        "STATIC_SITE_FORM_BINDING_HITS",
        "STATIC_SITE_HTML_STRUCTURE_HITS",
        "STATIC_SITE_INERT_CONTROL_HITS",
        "STATIC_SITE_MISSING_DOM_ID_HITS",
        "STATIC_SITE_MISSING_JS_API_HITS",
    }
)
_MAX_REPAIR_FINDING_VALUES = 64


# LLM: required_tool_calls returns compact tool skeletons derived from recovery action fields only.
# 函数用途: 将 writer_tool/builder_tool、checkpoint/source/output refs 转成可执行工具调用模板。
def required_tool_calls(required_actions: list[dict[str, object]]) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    for action in required_actions:
        if writer := _writer_call(action):
            calls.append(writer)
        if builder := _builder_call(action):
            calls.append(builder)
        calls.extend(_artifact_repair_calls(action))
    return calls


# LLM: _writer_call keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _writer_call(action: dict[str, object]) -> dict[str, object]:
    tool = str(action.get("writer_tool") or "").strip()
    path = str(action.get("checkpoint_ref") or "").strip()
    if not tool or not path:
        return {}
    if tool == "api_json_collection":
        return _api_json_collection_call(action, path)
    call: dict[str, object] = {"tool": tool, "path": path}
    if updates := _collection_item_updates(action):
        call["collection_item_updates"] = updates
        if items_path := str(action.get("items_path") or "").strip():
            call["items_path"] = items_path
        if groups_path := str(action.get("groups_path") or "").strip():
            call["groups_path"] = groups_path
    if hint := _json_hint(action):
        _apply_json_hint(call, hint)
    if tool == "write_structured_json" or str(action.get("recommended_action") or "") == "repair_evidence_refs":
        if not _is_full_checkpoint_hint(hint):
            call["merge_existing"] = True
    return call


# LLM: api_json_collection required calls are source-collection skeletons, not checkpoint JSON payloads.
# 函数用途: 从 collection_contract 和 required_columns 生成可填写的 API 采集工具骨架，避免把 shape hint 塞进无效 data 字段。
def _api_json_collection_call(action: dict[str, object], path: str) -> dict[str, object]:
    columns = _api_collection_columns(action)
    llm_generated_fields = _api_collection_llm_generated_fields(action)
    call: dict[str, object] = {
        "tool": "api_json_collection",
        "path": path,
        "columns": columns,
        "fields": _api_collection_fields(action, columns),
        "evidence_fields": _api_collection_evidence_fields(action, columns),
        "completion_evidence": {
            "method": "api_json_collection",
            "scope": _completion_scope(action),
        },
    }
    if llm_generated_fields:
        call["llm_generated_fields"] = llm_generated_fields
    call.update(_api_collection_request_fields(action))
    collection = action.get("collection_contract")
    if isinstance(collection, dict):
        call["collection_contract"] = collection
    return call


def _api_collection_fields(action: dict[str, object], columns: list[str]) -> dict[str, object]:
    request = _api_collection_request(action)
    fields = request.get("fields") if isinstance(request, dict) else None
    if isinstance(fields, dict) and fields:
        return {str(key): value for key, value in fields.items() if str(key).strip()}
    return _api_collection_field_skeleton(columns)


def _api_collection_evidence_fields(action: dict[str, object], columns: list[str]) -> list[str]:
    request = _api_collection_request(action)
    if isinstance(request, dict):
        values = _string_list(request.get("evidence_fields"))
        if values:
            return values
    collection = action.get("collection_contract")
    if isinstance(collection, dict):
        values = _string_list(collection.get("required_item_evidence_fields"))
        if values:
            return values
    return columns


def _api_collection_llm_generated_fields(action: dict[str, object]) -> list[str]:
    fields: list[str] = []
    collection = action.get("collection_contract")
    if isinstance(collection, dict):
        fields.extend(_string_list(collection.get("llm_generated_fields")))
        request = collection.get("api_request")
        if isinstance(request, dict):
            fields.extend(_string_list(request.get("llm_generated_fields")))
    return list(dict.fromkeys(fields))


def _api_collection_request_fields(action: dict[str, object]) -> dict[str, object]:
    request = _api_collection_request(action)
    if not isinstance(request, dict):
        request = {}
    keys = (
        "request_ranges",
        "requests",
        "source_artifacts",
        "url_template",
        "item_path",
        "limit_per_request",
        "request_delay_seconds",
    )
    fields = {key: request[key] for key in keys if key in request}
    collection = action.get("collection_contract")
    if isinstance(collection, dict) and isinstance(collection.get("item_date_bounds"), dict):
        fields["item_date_bounds"] = collection["item_date_bounds"]
    return fields


def _api_collection_request(action: dict[str, object]) -> dict[str, object]:
    collection = action.get("collection_contract")
    if not isinstance(collection, dict):
        return {}
    request = collection.get("api_request")
    return dict(request) if isinstance(request, dict) else {}


def _api_collection_columns(action: dict[str, object]) -> list[str]:
    columns = _string_list(action.get("required_columns"))
    collection = action.get("collection_contract")
    if isinstance(collection, dict):
        columns.extend(_string_list(collection.get("required_item_fields")))
        request = collection.get("api_request")
        if isinstance(request, dict) and isinstance(request.get("fields"), dict):
            columns.extend(str(key) for key in request["fields"] if str(key).strip())
    if not columns and (hint := _json_hint(action)):
        columns.extend(_columns_from_shape_hint(hint))
    return list(dict.fromkeys(columns))


def _api_collection_field_skeleton(columns: list[str]) -> dict[str, object]:
    return {
        column: {"path": "__FILL_JSON_PATH__"}
        for column in columns
    }


def _completion_scope(action: dict[str, object]) -> str:
    hint = _json_hint(action)
    evidence = hint.get("completion_evidence") if isinstance(hint, dict) else {}
    return _scope_from_evidence(evidence) or "declared_collection_contract"


def _scope_from_evidence(evidence: object) -> str:
    return str(evidence.get("scope") or "").strip() if isinstance(evidence, dict) else ""


def _columns_from_shape_hint(value: object) -> list[str]:
    if not isinstance(value, dict):
        return []
    return _columns_from_sheets(value.get("sheets")) or _columns_from_rows(value.get("rows"))


def _columns_from_sheets(sheets: object) -> list[str]:
    if not isinstance(sheets, list):
        return []
    for sheet in sheets:
        columns = _string_list(sheet.get("columns")) if isinstance(sheet, dict) else []
        if columns:
            return columns
    return []


def _columns_from_rows(rows: object) -> list[str]:
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return []
    return [str(key) for key in rows[0] if str(key) != "field_source_ids"]


# LLM: _builder_call keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _builder_call(action: dict[str, object]) -> dict[str, object]:
    tool = str(action.get("builder_tool") or "").strip()
    output_ref = str(action.get("output_ref") or "").strip()
    source_ref = str(action.get("source_ref") or "").strip()
    if not tool or not output_ref:
        return {}
    call: dict[str, object] = {"tool": tool, "path": output_ref}
    if source_ref:
        call[_source_param_name(action, tool)] = source_ref
    return call


# LLM: _artifact_repair_calls exposes executable targets for failed artifact findings.
# 函数用途: 将 repair_targets/write_tools 变成模型可直接调用的补丁工具骨架，避免 required_tool_calls 为空。
def _artifact_repair_calls(action: dict[str, object]) -> list[dict[str, object]]:
    if str(action.get("recommended_action") or "") != "repair_artifact_against_findings":
        return []
    intent = _artifact_repair_intent(action)
    tool = _artifact_repair_tool(action, intent=intent)
    if not tool:
        return []
    targets = action.get("repair_targets")
    target_values = [str(item) for item in targets if str(item)] if isinstance(targets, list) else []
    if not target_values:
        artifact_path = str(action.get("artifact_path") or "").strip()
        target_values = [artifact_path] if artifact_path else []
    findings = action.get("finding_values")
    finding_values = [str(item) for item in findings if str(item)] if isinstance(findings, list) else []
    mapping_requirement = _mapping_requirement(action, finding_values)
    return [
        _with_mapping_requirement(
            {
                "tool": tool,
                "path": target,
                "finding_values": finding_values[:_MAX_REPAIR_FINDING_VALUES],
                "mutation_intent": intent,
                **_source_ref_payload(action),
            },
            mapping_requirement,
        )
        for target in target_values[:4]
    ]


def _source_ref_payload(action: dict[str, object]) -> dict[str, object]:
    source_ref = str(action.get("source_ref") or "").strip()
    return {"source_ref": source_ref} if source_ref else {}


# LLM: _artifact_repair_tool picks a deterministic patch-capable tool from the action manifest.
# 函数用途: 根据结构化 finding code 选择局部替换、创建或整文件重写工具，不读验收文案。
def _artifact_repair_tool(action: dict[str, object], *, intent: str) -> str:
    values = _write_tool_values(action)
    candidates = ("replace_in_file", "write_file", "file_write_session")
    if intent in {"create_or_replace", "rewrite"}:
        candidates = ("write_file", "file_write_session", *candidates)
    return _first_write_tool(values, candidates) or (values[0] if values else "")


# LLM: _write_tool_values 是 agent_py_agent/agent/agent_core/tool_delivery_repair_required_calls.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 write tool values 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _write_tool_values(action: dict[str, object]) -> list[str]:
    tools = action.get("write_tools")
    return [str(item) for item in tools if str(item)] if isinstance(tools, list) else []


def _collection_item_updates(action: dict[str, object]) -> list[object]:
    updates = action.get("collection_item_updates")
    return list(updates) if isinstance(updates, list) else []


def _string_list(value: object) -> list[str]:
    return [text for item in value if (text := str(item).strip())] if isinstance(value, list) else []


# LLM: _first_write_tool 是 agent_py_agent/agent/agent_core/tool_delivery_repair_required_calls.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 first write tool 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _first_write_tool(values: list[str], candidates: tuple[str, ...]) -> str:
    for candidate in candidates:
        if candidate in values:
            return candidate
    return ""


# LLM: _artifact_repair_intent classifies the mutation shape from stable finding codes.
# 函数用途: 缺文件要创建，HTML 结构损坏要整文件重写，其他问题默认局部修补。
def _artifact_repair_intent(action: dict[str, object]) -> str:
    raw_codes = action.get("finding_codes")
    codes = {str(code) for code in raw_codes if str(code)} if isinstance(raw_codes, list) else set()
    if codes & _CREATE_OR_REPLACE_FINDING_CODES:
        return "create_or_replace"
    if codes & _FULL_REWRITE_FINDING_CODES:
        return "rewrite"
    return "patch"


def _with_mapping_requirement(call: dict[str, object], requirement: dict[str, object]) -> dict[str, object]:
    if requirement:
        call.update(requirement)
    return call


# LLM: mapping repair requirements come from structured finding JSON emitted by artifact_collection_mapping.
# 函数用途: 从 ARTIFACT_MAPPING_MISSING 的 value JSON 提取缺失 key 和最低映射数量，供 required calls/guard 使用。
def _mapping_requirement(action: dict[str, object], finding_values: list[str]) -> dict[str, object]:
    if not _has_finding_code(action, "ARTIFACT_MAPPING_MISSING"):
        return {}
    keys: list[dict[str, str]] = []
    required = 0
    for raw in finding_values:
        value = _parse_json_text(raw)
        if not isinstance(value, dict):
            continue
        required = max(required, _positive_int(value.get("required")))
        missing = value.get("missing_keys")
        if isinstance(missing, list):
            for item in missing:
                key_values = _mapping_key_values(item)
                if key_values:
                    keys.append(key_values)
    unique_keys = _unique_mapping_keys(keys)
    if not unique_keys:
        return {}
    count = required or len(unique_keys)
    return {
        "mapping_required_count": max(1, min(count, len(unique_keys))),
        "mapping_required_keys": unique_keys[:10],
    }


def _has_finding_code(action: dict[str, object], code: str) -> bool:
    raw_codes = action.get("finding_codes")
    return isinstance(raw_codes, list) and code in {str(item) for item in raw_codes}


def _mapping_key_values(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item).strip() for key, item in value.items() if str(key).strip() and str(item).strip()}


def _unique_mapping_keys(values: list[dict[str, str]]) -> list[dict[str, str]]:
    unique: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in values:
        marker = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(value)
    return unique


def _positive_int(value: object) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


# LLM: _source_param_name prefers explicit builder schema fields and only uses known-tool defaults as a fast path.
# 函数用途: 优先读取结构化 source_param/source_param_name/input_param；未知 builder 走 source_path 兜底，不因映射缺失拒绝。
def _source_param_name(action: dict[str, object], tool: str) -> str:
    for key in ("source_param", "source_param_name", "input_param"):
        value = str(action.get(key) or "").strip()
        if value:
            return value
    return {
        "data_to_workbook": "source_json_path",
        "markdown_to_pdf": "source_markdown_path",
    }.get(tool, "source_path")


# LLM: _json_hint keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _json_hint(action: dict[str, object]) -> object:
    for key in ("evidence_shape_hint", "checkpoint_shape_hint"):
        if value := _parse_json_text(str(action.get(key) or "").strip()):
            return value
    return {}


# LLM: _apply_json_hint maps checkpoint shape hints to real tool parameters.
# 函数用途: 把 generated_rows、rows、sheets、completion_evidence 等机器字段拆到工具顶层，不塞成自然语言或嵌套 data。
def _apply_json_hint(call: dict[str, object], hint: object) -> None:
    if not isinstance(hint, dict):
        call["data"] = hint
        return
    for key in (
        "data",
        "rows",
        "sheets",
        "generated_rows",
        "completion_evidence",
        "source_refs",
        "claims",
        "columns",
        "name",
    ):
        if key in hint:
            call[key] = hint[key]


# LLM: full checkpoint hints should replace bad staged JSON rather than merge with stale rows.
# 函数用途: generated_rows/rows/sheets 代表整份 checkpoint 形状，返工时不自动 merge_existing。
def _is_full_checkpoint_hint(value: object) -> bool:
    return isinstance(value, dict) and any(key in value for key in ("rows", "sheets", "generated_rows"))


# LLM: _parse_json_text keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _parse_json_text(text: str) -> object:
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


__all__ = ["required_tool_calls"]
