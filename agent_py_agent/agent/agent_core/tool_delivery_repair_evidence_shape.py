# LLM: evidence-shape repair checks keep delivery repair grounded in machine source refs.
# 模块用途: 判断 repair_evidence_refs 写入是否包含 source_refs/claims，而不是任意表格占位数据。

from __future__ import annotations

import json


# LLM: violates_evidence_repair_shape rejects writer calls that cannot satisfy structured evidence repair.
# 函数用途: 对 repair_evidence_refs 的 write_structured_json 调用执行轻量机器形状检查。
def violates_evidence_repair_shape(
    call: dict[str, object],
    required_actions: list[dict[str, object]],
    *,
    path_matches: object,
    call_path: object,
) -> bool:
    tool = str(call.get("tool") or "").strip()
    path = str(call_path(call) or "").strip() if callable(call_path) else ""
    if tool != "write_structured_json" or not path:
        return False
    action = _matching_evidence_repair_action(required_actions, path, tool, path_matches=path_matches)
    if action is None:
        return False
    if not _merge_existing_enabled(call):
        return True
    return not _evidence_repair_payload_is_complete(_structured_json_data(call), action)


# LLM: _matching_evidence_repair_action finds the recovery action targeted by a writer call.
# 函数用途: 用 writer_tool/checkpoint_ref/recommended_action 三个机器字段匹配，不读普通文本。
def _matching_evidence_repair_action(
    required_actions: list[dict[str, object]],
    path: str,
    tool: str,
    *,
    path_matches: object,
) -> dict[str, object] | None:
    for action in required_actions:
        if _is_matching_evidence_repair(action, path, tool, path_matches=path_matches):
            return action
    return None


# LLM: _is_matching_writer_action keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _is_matching_writer_action(
    action: dict[str, object],
    path: str,
    tool: str,
    *,
    path_matches: object,
) -> bool:
    writer_tool = str(action.get("writer_tool") or "").strip()
    checkpoint_ref = str(action.get("checkpoint_ref") or "").strip()
    return bool(writer_tool == tool and checkpoint_ref and callable(path_matches) and path_matches(path, checkpoint_ref))


# LLM: _is_matching_evidence_repair keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _is_matching_evidence_repair(
    action: dict[str, object],
    path: str,
    tool: str,
    *,
    path_matches: object,
) -> bool:
    if str(action.get("recommended_action") or "") != "repair_evidence_refs":
        return False
    return _is_matching_writer_action(action, path, tool, path_matches=path_matches)


# LLM: _structured_json_data keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _structured_json_data(call: dict[str, object]) -> object:
    data = call.get("data")
    if isinstance(data, dict):
        return data
    if isinstance(data, str):
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


# LLM: evidence repair must be a metadata merge so existing staged rows/sheets are not overwritten.
# 函数用途: repair_evidence_refs 只能用 merge_existing 写入，避免补证据时丢掉已有 checkpoint 数据。
def _merge_existing_enabled(call: dict[str, object]) -> bool:
    value = call.get("merge_existing")
    return value is True or str(value).strip().lower() in {"1", "true", "yes"}


# LLM: _evidence_repair_payload_is_complete keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _evidence_repair_payload_is_complete(payload: object, action: dict[str, object]) -> bool:
    if not isinstance(payload, dict):
        return False
    sources = payload.get("source_refs")
    claims = payload.get("claims")
    if not isinstance(sources, list) or not sources or not isinstance(claims, list) or not claims:
        return False
    source_ids = {str(item.get("source_id") or "") for item in sources if isinstance(item, dict)}
    required_fields = [str(item) for item in action.get("required_fields", []) if str(item)]
    claimed_fields = {
        str(item.get("field") or "")
        for item in claims
        if _claim_has_valid_source(item, source_ids)
    }
    return all(field in claimed_fields for field in required_fields) if required_fields else bool(claimed_fields)


# LLM: _claim_has_valid_source keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _claim_has_valid_source(item: object, source_ids: set[str]) -> bool:
    if not isinstance(item, dict):
        return False
    claim_source_ids = item.get("source_ids")
    if not isinstance(claim_source_ids, list):
        return False
    return any(str(source_id) in source_ids for source_id in claim_source_ids)


__all__ = ["violates_evidence_repair_shape"]
