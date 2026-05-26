# LLM: Partial runner-result recovery is isolated so the main parser stays small and auditable.
# 模块用途: 从尾部截断的 SUBAGENT_RESULT JSON 中恢复最小可收口结果；只接受带可追溯 refs 的证据包。

from __future__ import annotations

import json


# LLM: extract_partial_subagent_result_text salvages real runner success when only a long tail was cut.
# 函数用途: 在结果块 JSON 尾部截断时，仅凭已完整的 status/summary/evidence_packets 构造最小结果。
def extract_partial_subagent_result_text(text: str) -> str:
    """Build a minimal safe result from complete leading fields in an incomplete result block."""

    leading = _json_candidate_text(text)
    if not leading.startswith("{"):
        return ""
    status = _json_field_value(leading, "status")
    summary = _json_field_value(leading, "summary")
    evidence_packets = _dict_list(_json_field_value(leading, "evidence_packets"))
    if not evidence_packets:
        evidence_packets = _json_field_dict_items(leading, "evidence_packets")
    blocked_reason = _json_field_value(leading, "blocked_reason")
    failure_type = _json_field_value(leading, "failure_type")
    if not _partial_result_is_safe(status, evidence_packets, blocked_reason, failure_type):
        return ""
    payload = {
        "status": str(status or "DONE"),
        "summary": str(summary or "结构化结果尾部被截断，已从完整证据包恢复最小结果。"),
        "used_tools": _string_list(_json_field_value(leading, "used_tools")),
        "used_skills": _string_list(_json_field_value(leading, "used_skills")),
        "evidence": _dict_list(_json_field_value(leading, "evidence")),
        "evidence_packets": evidence_packets,
        "capability_requests": _dict_list(_json_field_value(leading, "capability_requests")),
        "artifacts": _dict_list(_json_field_value(leading, "artifacts")),
        "tests": _dict_list(_json_field_value(leading, "tests")),
        "patches": _dict_list(_json_field_value(leading, "patches")),
        "lessons": _string_list(_json_field_value(leading, "lessons")),
        "next_actions": _string_list(_json_field_value(leading, "next_actions")),
        "blocked_reason": str(blocked_reason or ""),
        "failure_type": str(failure_type or ""),
    }
    return json.dumps(payload, ensure_ascii=False)


# LLM: _json_candidate_text normalizes optional json/fence prefixes before field-level recovery.
# 函数用途: 将可能带 json 或 ```json 前缀的片段转为 JSON 起始文本，不做宽松全文搜索。
def _json_candidate_text(text: str) -> str:
    leading = text.lstrip()
    lowered = leading.lower()
    if lowered.startswith("json"):
        return leading[4:].lstrip()
    if leading.startswith("```"):
        return _strip_json_fence(leading)
    return leading


# LLM: _json_field_value reads one complete JSON field value without requiring the whole object to close.
# 函数用途: 从截断 JSON 的前半段读取已完整的字段；字段不完整或不存在时返回 None。
def _json_field_value(text: str, key: str) -> object:
    marker = f'"{key}"'
    start = text.find(marker)
    if start < 0:
        return None
    colon = text.find(":", start + len(marker))
    if colon < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(text[colon + 1:].lstrip())
    except json.JSONDecodeError:
        return None
    return value


# LLM: _json_field_dict_items recovers complete leading objects from an array cut off mid-stream.
# 函数用途: evidence_packets 数组尾部被截断时，保留前面已完整闭合的对象，供最小结果恢复使用。
def _json_field_dict_items(text: str, key: str) -> list[dict[str, object]]:
    marker = f'"{key}"'
    start = text.find(marker)
    if start < 0:
        return []
    colon = text.find(":", start + len(marker))
    bracket = text.find("[", colon + 1)
    if colon < 0 or bracket < 0:
        return []
    return _decode_complete_dict_items(text, bracket + 1)


# LLM: _decode_complete_dict_items scans only complete JSON objects and stops at the first cut tail.
# 函数用途: 从数组片段中提取已闭合对象；遇到截断对象立即停止，不尝试猜测补全。
def _decode_complete_dict_items(text: str, index: int) -> list[dict[str, object]]:
    decoder = json.JSONDecoder()
    items: list[dict[str, object]] = []
    while index < len(text):
        index = _skip_array_separators(text, index)
        if index >= len(text) or text[index] != "{":
            break
        try:
            value, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            break
        if isinstance(value, dict):
            items.append(value)
        index += end
    return items


# LLM: _skip_array_separators keeps partial array scanning small and deterministic.
# 函数用途: 跳过 JSON 数组中对象之间的空白和逗号，不尝试宽松修正文法。
def _skip_array_separators(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t\r\n,":
        index += 1
    return index


# LLM: _partial_result_is_safe prevents truncated prose from being accepted without traceable refs.
# 函数用途: 成功态必须有带 evidence_refs/artifact_refs 的证据包；阻断态必须有阻断原因或失败类型。
def _partial_result_is_safe(
    status: object,
    evidence_packets: list[dict[str, object]],
    blocked_reason: object,
    failure_type: object,
) -> bool:
    status_text = str(status or "").strip().upper()
    if status_text in {"BLOCKED", "FAILED", "TIMEOUT", "CHANNEL_ERROR"}:
        return bool(str(blocked_reason or failure_type or "").strip())
    if status_text not in {"COMPLETED", "DONE", "SUCCESS", "OK"}:
        return False
    return any(_packet_has_traceable_ref(item) for item in evidence_packets)


# LLM: _packet_has_traceable_ref keeps partial success recovery tied to concrete files or evidence refs.
# 函数用途: 判断一个 evidence_packet 是否包含父级可继续读取的 artifact_refs/evidence_refs。
def _packet_has_traceable_ref(packet: dict[str, object]) -> bool:
    return bool(_string_list(packet.get("artifact_refs")) or _string_list(packet.get("evidence_refs")))


# LLM: _string_list keeps partial recovery independent from the main parser module.
# 函数用途: 轻量规范化截断恢复里的字符串列表字段，避免和主 parser 形成循环导入。
def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item or "").strip()]


# LLM: _dict_list accepts only closed dict objects from partial JSON arrays.
# 函数用途: 轻量规范化截断恢复里的对象列表字段，丢弃未闭合或非对象条目。
def _dict_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


# LLM: _strip_json_fence removes optional Markdown fences before partial field scanning.
# 函数用途: 去掉模型可能包上的 ```json fence，让后续字段扫描从 JSON 文本开始。
def _strip_json_fence(raw: str) -> str:
    stripped = raw.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()
