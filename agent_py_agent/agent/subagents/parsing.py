# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""LLM contract: parse and normalize model-produced subagent JSON blocks.

Human version:
模型输出不可信，可能缺结束标记、JSON 损坏、外面包了 Markdown fence。
这里集中做解析和规范化，让业务流程只处理干净的结构化对象。
"""

import json
from typing import TYPE_CHECKING

# LLM: coverage_records_from_payload keeps fallback/takeover coverage structured instead of prose-based.
from .coverage_records import coverage_records_from_payload
from .models import SubAgentParsedOutput
from .parsing_artifacts import artifact_items_from_payload
from .parsing_capability_requests import capability_requests_from_payload
from .parsing_evidence_refs import evidence_packets_with_top_level_refs
from .parsing_partial import extract_partial_subagent_result_text
from .parsing_values import (
    _dict_list as _dict_list,
)
from .parsing_values import (
    _int_value as _int_value,
)
from .parsing_values import (
    _normalize_runner_items as _normalize_runner_items,
)
from .parsing_values import (
    _split_allowed_items as _split_allowed_items,
)
from .parsing_values import (
    _string_dict as _string_dict,
)
from .parsing_values import (
    _string_list as _string_list,
)
from .reports import ParentPlannerParsedOutput

if TYPE_CHECKING:
    from ..action_protocol import SubagentResultEnvelope
    from .parsing_envelope import SubagentResultEnvelopeParseRequest


# LLM: parse_subagent_runner_output 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 解析并归一化子代理执行器output的输入形态，让下游只处理稳定结构；关键副作用: 会影响任务状态、执行器结果、验收和报告展示，需保持重试、超时和状态迁移语义。
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


# LLM: parse_subagent_result_envelope keeps the old import path while delegating typed conversion.
# 函数用途: 兼容旧导入位置；真正的 envelope 转换在 parsing_envelope.py，避免 parser 主文件继续变大。
def parse_subagent_result_envelope(
    request: SubagentResultEnvelopeParseRequest,
) -> SubagentResultEnvelope | None:
    """兼容旧模块入口，调用 typed result envelope bridge。"""

    from .parsing_envelope import parse_subagent_result_envelope as _parse

    return _parse(request)


# LLM: parse_parent_planner_output 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 解析并归一化父级规划器output的输入形态，让下游只处理稳定结构；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
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
        fallback = _parent_planner_from_alias_result(text)
        if fallback.found:
            return fallback
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


# LLM: _parent_planner_from_alias_result repairs a narrow marker mix-up without accepting arbitrary runner results.
# 函数用途: 当控制面回复误用 SUBAGENT_RESULT 包裹父级 planner JSON 时，仅在字段形状匹配时兜底解析。
def _parent_planner_from_alias_result(text: str) -> ParentPlannerParsedOutput:
    candidates = _extract_subagent_result_blocks(text, "[SUBAGENT_RESULT]", "[/SUBAGENT_RESULT]")
    for raw in reversed(candidates):
        payload, error = _parse_runner_json_payload(raw)
        if error or not _looks_like_parent_planner_payload(payload):
            continue
        return _parsed_parent_planner_from_payload(payload)
    return ParentPlannerParsedOutput(found=False, ok=False)


# LLM: _looks_like_parent_planner_payload keeps alias recovery schema-bound.
# 函数用途: 判断 JSON 是否真像父级 planner 决策，避免把普通 runner 结果错当调度命令。
def _looks_like_parent_planner_payload(payload: dict[str, object]) -> bool:
    if not isinstance(payload, dict):
        return False
    has_parent_fields = {"decision", "should_dispatch", "actions"} & set(payload)
    if len(has_parent_fields) < 2:
        return False
    decision = str(payload.get("decision", "") or "").strip().upper()
    if decision and decision not in {"DISPATCH", "HEARTBEAT_OK", "BLOCKED", "TAKEOVER"}:
        return False
    return "status" not in payload and "used_tools" not in payload


# LLM: _extract_subagent_result_blocks 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理extract子代理结果blocks相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
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


# LLM: _blocks_with_open_result_candidate keeps block scanning shallow for code-size guards.
# 函数用途: 把缺尾标记恢复候选追加到已有结果块列表；没有可靠候选时保持原列表不变。
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


# LLM: _extract_open_result_block_candidate salvages complete JSON when the model dropped only the closing marker.
# 函数用途: 在结果块缺少结束标记时，仅当标记后面直接是完整 JSON/fence JSON 才提取候选，避免把说明文字误当执行结果。
def _extract_open_result_block_candidate(section: str, *, allow_partial: bool = False) -> str:
    """从未闭合的结果块里提取完整 JSON 候选。"""

    stripped = section.strip()
    if not stripped:
        return ""
    direct = _extract_direct_json_object_text(stripped)
    if direct or not allow_partial:
        return direct
    return extract_partial_subagent_result_text(stripped)


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


# LLM: _extract_direct_json_object_text keeps open-marker recovery from swallowing nested objects.
# 函数用途: 只接受结果块开头的完整 JSON object，避免把坏外层 JSON 里的内层小对象误当成最终结果。
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


# LLM: _parsed_parent_planner_from_payload 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 解析并归一化来自parsed父级规划器载荷的输入形态，让下游只处理稳定结构；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
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


# LLM: _parsed_output_from_payload 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 解析并归一化来自parsedoutput载荷的输入形态，让下游只处理稳定结构；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
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
        used_skills=_string_list(payload.get("used_skills", [])),
        used_tools=_string_list(payload.get("used_tools", [])),
        evidence=_dict_list(payload.get("evidence", [])),
        # LLM: 可追溯声明包与旧证据备注分开解析，避免语义互相污染。
        evidence_packets=evidence_packets,
        findings=_dict_list(payload.get("findings", [])),
        coverage_records=coverage_records_from_payload(payload),
        capability_requests=capability_requests_from_payload(payload),
        artifacts=artifact_items_from_payload(payload),
        tests=_dict_list(payload.get("tests", [])),
        patches=_dict_list(payload.get("patches", [])),
        lessons=_string_list(payload.get("lessons", [])),
        next_actions=_string_list(payload.get("next_actions", [])),
        raw_json=payload,
    )


# LLM: _strip_json_fence 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理stripJSONfence相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
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


# LLM: _parse_runner_json_payload 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 解析并归一化执行器JSON载荷的输入形态，让下游只处理稳定结构；关键副作用: 会影响任务状态、执行器结果、验收和报告展示，需保持重试、超时和状态迁移语义。
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


# LLM: _extract_first_json_object_text 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理extractfirstJSONobject文本相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
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
