"""LLM contract: parse and normalize model-produced subagent JSON blocks.

Human version:
模型输出不可信，可能缺结束标记、JSON 损坏、外面包了 Markdown fence。
这里集中做解析和规范化，让业务流程只处理干净的结构化对象。
"""

from __future__ import annotations

import json

from ...common.value_parsing import text_or_sequence_strings
from ..coverage_records import coverage_records_from_payload
from ..models import SubAgentParsedOutput
from ..reports import ParentPlannerParsedOutput
from .artifacts import artifact_items_from_payload
from .evidence_refs import evidence_packets_with_top_level_refs
from .partial import extract_partial_subagent_result_text
from .values import (
    _dict_list as _dict_list,
)
from .values import (
    _int_value as _int_value,
)
from .values import (
    _normalize_runner_items as _normalize_runner_items,
)
from .values import (
    _split_allowed_items as _split_allowed_items,
)
from .values import (
    _string_dict as _string_dict,
)


def parse_subagent_runner_output(text: str) -> SubAgentParsedOutput:
    """解析 runner 模型回复中的结构化结果块。"""

    marker_start = "[SUBAGENT_RESULT]"
    marker_end = "[/SUBAGENT_RESULT]"
    candidates = _extract_subagent_result_blocks(text, marker_start, marker_end, allow_partial=True)
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


def _extract_subagent_result_blocks(
    text: str,
    marker_start: str,
    marker_end: str,
    *,
    allow_partial: bool = False,
) -> list[str]:
    """提取所有成对的 runner 结果块，允许正文里先提到协议标记。"""

    blocks = []
    offset = 0
    while True:
        start = text.find(marker_start, offset)
        if start == -1:
            return blocks
        body_start = start + len(marker_start)
        end = text.find(marker_end, body_start)
        if end == -1:
            return _blocks_with_open_result_candidate(blocks, text[body_start:], allow_partial=allow_partial)
        blocks.append(text[body_start:end].strip())
        offset = end + len(marker_end)


def _blocks_with_open_result_candidate(
    blocks: list[str],
    section: str,
    *,
    allow_partial: bool = False,
) -> list[str]:
    """Append an unclosed result candidate when recovery is safe."""

    open_candidate = _extract_open_result_block_candidate(section, allow_partial=allow_partial)
    if not open_candidate:
        return blocks
    return [*blocks, open_candidate]


def _extract_open_result_block_candidate(section: str, *, allow_partial: bool = False) -> str:
    """从未闭合的结果块里提取完整 JSON 候选。"""

    stripped = section.strip()
    if not stripped:
        return ""
    direct = _extract_direct_json_object_text(stripped)
    if direct or not allow_partial:
        return direct
    return extract_partial_subagent_result_text(stripped)


def _json_candidate_text(text: str) -> str:
    leading = text.lstrip()
    lowered = leading.lower()
    if lowered.startswith("json"):
        return leading[4:].lstrip()
    if leading.startswith("```"):
        return _strip_json_fence(leading)
    return leading


def _extract_direct_json_object_text(text: str) -> str:
    """Return a complete JSON object only when it starts the recovered block."""

    leading = _json_candidate_text(text)
    if not leading.startswith("{"):
        return ""
    try:
        payload, end = json.JSONDecoder().raw_decode(leading)
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    return leading[:end].strip()


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
        blockers=text_or_sequence_strings(payload.get("blockers", [])),
        risks=text_or_sequence_strings(payload.get("risks", [])),
        notes=text_or_sequence_strings(payload.get("notes", [])),
        raw_json=payload,
    )


def _parsed_output_from_payload(payload: dict[str, object]) -> SubAgentParsedOutput:
    """把已解析 JSON payload 转成标准结果对象。"""

    evidence_packets = evidence_packets_with_top_level_refs(payload)
    return SubAgentParsedOutput(
        found=True,
        ok=True,
        status=str(payload.get("status", "") or ""),
        summary=str(payload.get("summary", "") or ""),
        blocked_reason=str(payload.get("blocked_reason", "") or ""),
        failure_type=str(payload.get("failure_type", "") or ""),
        used_skills=text_or_sequence_strings(payload.get("used_skills", [])),
        used_tools=text_or_sequence_strings(payload.get("used_tools", [])),
        evidence=_dict_list(payload.get("evidence", [])),
        evidence_packets=evidence_packets,
        findings=_dict_list(payload.get("findings", [])),
        coverage_records=coverage_records_from_payload(payload),
        capability_requests=capability_requests_from_payload(payload),
        artifacts=artifact_items_from_payload(payload),
        tests=_dict_list(payload.get("tests", [])),
        patches=_dict_list(payload.get("patches", [])),
        lessons=text_or_sequence_strings(payload.get("lessons", [])),
        next_actions=text_or_sequence_strings(payload.get("next_actions", [])),
        raw_json=payload,
    )


def capability_requests_from_payload(payload: dict[str, object]) -> list[dict[str, object]]:
    return _dict_list(payload.get("capability_requests", []))


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
