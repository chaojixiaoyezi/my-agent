from __future__ import annotations

"""LLM contract: parse and normalize model-produced subagent JSON blocks.

Human version:
模型输出不可信，可能缺结束标记、JSON 损坏、外面包了 Markdown fence。
这里集中做解析和规范化，让业务流程只处理干净的结构化对象。
"""

import json

from .models import SubAgentParsedOutput
from .reports import ParentPlannerParsedOutput


def parse_subagent_runner_output(text: str) -> SubAgentParsedOutput:
    """解析 runner 模型回复中的结构化结果块。"""

    marker_start = "[SUBAGENT_RESULT]"
    marker_end = "[/SUBAGENT_RESULT]"
    candidates = _extract_subagent_result_blocks(text, marker_start, marker_end)
    if not candidates:
        if marker_start in text:
            return SubAgentParsedOutput(
                found=True,
                ok=False,
                parse_error="缺少 [/SUBAGENT_RESULT] 结束标记。",
            )
        return SubAgentParsedOutput(found=False, ok=False)

    parse_errors = []
    for raw in reversed(candidates):
        payload, error = _parse_runner_json_payload(raw)
        if error:
            parse_errors.append(error)
            continue
        return _parsed_output_from_payload(payload)

    if parse_errors:
        return SubAgentParsedOutput(
            found=True,
            ok=False,
            parse_error=parse_errors[0],
        )
    return SubAgentParsedOutput(found=True, ok=False, parse_error="未找到可解析的结构化结果。")


def parse_parent_planner_output(text: str) -> ParentPlannerParsedOutput:
    """解析父代理 planner 模型回复中的结构化结果块。"""

    marker_start = "[PARENT_PLANNER_RESULT]"
    marker_end = "[/PARENT_PLANNER_RESULT]"
    candidates = _extract_subagent_result_blocks(text, marker_start, marker_end)
    if not candidates:
        if marker_start in text:
            return ParentPlannerParsedOutput(
                found=True,
                ok=False,
                parse_error="缺少 [/PARENT_PLANNER_RESULT] 结束标记。",
            )
        return ParentPlannerParsedOutput(found=False, ok=False)

    parse_errors = []
    for raw in reversed(candidates):
        payload, error = _parse_runner_json_payload(raw)
        if error:
            parse_errors.append(error)
            continue
        return _parsed_parent_planner_from_payload(payload)

    if parse_errors:
        return ParentPlannerParsedOutput(
            found=True,
            ok=False,
            parse_error=parse_errors[0],
        )
    return ParentPlannerParsedOutput(found=True, ok=False, parse_error="未找到可解析的结构化结果。")


def _extract_subagent_result_blocks(text: str, marker_start: str, marker_end: str) -> list[str]:
    """提取所有成对的 runner 结果块，允许正文里先提到协议标记。"""

    blocks = []
    offset = 0
    while True:
        start = text.find(marker_start, offset)
        if start == -1:
            break
        end = text.find(marker_end, start + len(marker_start))
        if end == -1:
            break
        blocks.append(text[start + len(marker_start) : end].strip())
        offset = end + len(marker_end)
    return blocks


def _parsed_parent_planner_from_payload(payload: dict[str, object]) -> ParentPlannerParsedOutput:
    """把已解析 JSON payload 转成父代理 planner 结果。"""

    decision = str(payload.get("decision", "") or "").strip().upper()
    if not decision:
        decision = "DISPATCH" if bool(payload.get("should_dispatch", True)) else "HEARTBEAT_OK"
    suggested_max_runners = _int_value(payload.get("suggested_max_runners", 0))
    return ParentPlannerParsedOutput(
        found=True,
        ok=True,
        decision=decision,
        summary=str(payload.get("summary", "") or ""),
        should_dispatch=bool(payload.get("should_dispatch", decision != "HEARTBEAT_OK")),
        runner_instruction=str(payload.get("runner_instruction", "") or ""),
        suggested_max_runners=max(0, suggested_max_runners),
        actions=_dict_list(payload.get("actions", [])),
        blockers=_string_list(payload.get("blockers", [])),
        risks=_string_list(payload.get("risks", [])),
        notes=_string_list(payload.get("notes", [])),
        raw_json=payload,
    )


def _parsed_output_from_payload(payload: dict[str, object]) -> SubAgentParsedOutput:
    """把已解析 JSON payload 转成标准结果对象。"""

    return SubAgentParsedOutput(
        found=True,
        ok=True,
        status=str(payload.get("status", "") or ""),
        summary=str(payload.get("summary", "") or ""),
        blocked_reason=str(payload.get("blocked_reason", "") or ""),
        failure_type=str(payload.get("failure_type", "") or ""),
        used_skills=_string_list(payload.get("used_skills", [])),
        used_tools=_string_list(payload.get("used_tools", [])),
        evidence=_dict_list(payload.get("evidence", [])),
        # LLM: parse traceable claim packets separately from legacy evidence notes.
        evidence_packets=_dict_list(payload.get("evidence_packets", [])),
        findings=_dict_list(payload.get("findings", [])),
        capability_requests=_dict_list(payload.get("capability_requests", [])),
        artifacts=_dict_list(payload.get("artifacts", [])),
        tests=_dict_list(payload.get("tests", [])),
        patches=_dict_list(payload.get("patches", [])),
        lessons=_string_list(payload.get("lessons", [])),
        next_actions=_string_list(payload.get("next_actions", [])),
        raw_json=payload,
    )


def _strip_json_fence(raw: str) -> str:
    """去掉可选的 Markdown JSON fence。"""

    stripped = raw.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _parse_runner_json_payload(raw: str) -> tuple[dict[str, object], str]:
    """从 runner 结果块里解析 JSON object。"""

    stripped = _strip_json_fence(raw)
    candidates = [stripped]
    embedded = _extract_first_json_object_text(stripped)
    if embedded and embedded not in candidates:
        candidates.append(embedded)

    errors: list[str] = []
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            errors.append(f"JSON 解析失败: {exc}")
            continue
        if not isinstance(payload, dict):
            errors.append("结构化结果必须是 JSON object。")
            continue
        return {str(key): value for key, value in payload.items()}, ""
    return {}, errors[0] if errors else "未找到可解析的 JSON object。"


def _extract_first_json_object_text(text: str) -> str:
    """容忍模型在结果块里给 JSON 加了 `json` 或说明前缀。"""

    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            payload, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return text[index : index + end]
    return ""


def _dict_list(value: object) -> list[dict[str, object]]:
    """把任意值规范成 dict list。"""

    if not isinstance(value, list):
        return []
    result: list[dict[str, object]] = []
    for item in value:
        if isinstance(item, dict):
            result.append({str(key): val for key, val in item.items()})
    return result


def _string_list(value: object) -> list[str]:
    """把任意值规范成字符串列表。"""

    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _int_value(value: object) -> int:
    """把任意值尽量转成整数，失败时返回 0。"""

    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(float(value.strip()))
        except ValueError:
            return 0
    return 0


def _string_dict(value: object) -> dict[str, str]:
    """把任意值规范成字符串字典。"""

    if not isinstance(value, dict):
        return {}
    return {str(key): str(val) for key, val in value.items()}


def _split_allowed_items(items: list[str], allowed: set[str]) -> tuple[list[str], list[str]]:
    """拆分授权项和未授权项。"""

    accepted: list[str] = []
    ignored: list[str] = []
    for item in items:
        if item in allowed:
            accepted.append(item)
        else:
            ignored.append(item)
    return accepted, ignored


def _normalize_runner_items(items: list[dict[str, object]]) -> list[dict[str, object]]:
    """把 runner item 转成稳定可 JSON 化的浅层对象。"""

    normalized: list[dict[str, object]] = []
    for item in items:
        normalized.append(_normalize_single_runner_item(item))
    return normalized


def _normalize_single_runner_item(item: dict[str, object]) -> dict[str, object]:
    """Normalize one runner item dict to JSON-serializable form."""
    payload: dict[str, object] = {}
    for key, value in item.items():
        payload[str(key)] = _normalize_runner_value(value)
    return payload


def _normalize_runner_value(value: object) -> object:
    """Normalize a single runner field value to JSON-serializable form."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [str(entry) for entry in value]
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    return str(value)
