from __future__ import annotations

"""Provider protocol adapters for canonical ToolCall values.

Native and text parsing are mutually exclusive for the whole run.  Native
assistant prose is never promoted into an executable call.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from ..tooling.models import ToolRuntimeSnapshot
from ..tooling.runtime_contracts import (
    ToolCall,
    ToolChoice,
    ToolProtocolSnapshot,
)

_TEXT_OPEN = "[TOOL_CALL]"
_TEXT_CLOSE = "[/TOOL_CALL]"
_NATIVE_PSEUDO_TOOL_MARKERS = (
    "[tool_call]",
    "[/tool_call]",
    "<tool_call",
    "</tool_call",
    "<tool_use",
    "</tool_use",
    "<function_call",
    "</function_call",
)


@dataclass(frozen=True)
class ToolProtocolViolation:
    code: str
    detail: str
    source_protocol: str
    evidence_preview: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "detail": self.detail,
            "source_protocol": self.source_protocol,
            "evidence_preview": self.evidence_preview,
        }


@dataclass(frozen=True)
class ProviderToolCallRequest:
    response: object
    protocol: ToolProtocolSnapshot
    runtime_snapshot: ToolRuntimeSnapshot
    turn_id: str
    attempt_id: str
    required_actions: tuple[object, ...] = ()
    tool_choice: ToolChoice | None = None


@dataclass(frozen=True)
class ProviderToolCallResult:
    calls: tuple[ToolCall, ...] = ()
    violations: tuple[ToolProtocolViolation, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.violations


def canonical_tool_calls_from_response(
    request: ProviderToolCallRequest,
) -> ProviderToolCallResult:
    """Normalize one response using only the protocol fixed at run start."""

    if request.protocol.run_id != request.runtime_snapshot.run_id:
        return ProviderToolCallResult(
            violations=(
                ToolProtocolViolation(
                    "TOOL_PROTOCOL_RUN_MISMATCH",
                    "protocol and runtime snapshots belong to different runs",
                    request.protocol.source_protocol,
                ),
            )
        )
    boundary_violations = _boundary_violations(request.response, request.protocol.source_protocol)
    if boundary_violations:
        adapted = ProviderToolCallResult(violations=boundary_violations)
    elif request.protocol.source_protocol == "native":
        adapted = _native_tool_calls(request)
    else:
        adapted = TextToolProtocolAdapter().tool_calls(request)
    return _enforce_tool_choice(request, adapted)


def _boundary_violations(
    response: object,
    source_protocol: str,
) -> tuple[ToolProtocolViolation, ...]:
    raw = getattr(response, "tool_protocol_violations", None) or []
    if not isinstance(raw, list):
        return (
            ToolProtocolViolation(
                "PROTOCOL_VIOLATION",
                "provider boundary violations must be a list",
                source_protocol,
            ),
        )
    violations: list[ToolProtocolViolation] = []
    for item in raw:
        if not isinstance(item, dict):
            violations.append(
                ToolProtocolViolation(
                    "PROTOCOL_VIOLATION",
                    "provider boundary violation entry is not an object",
                    source_protocol,
                )
            )
            continue
        violations.append(
            ToolProtocolViolation(
                str(item.get("code") or "PROTOCOL_VIOLATION"),
                str(item.get("detail") or "provider boundary rejected an incomplete tool response"),
                source_protocol,
                str(item.get("evidence_preview") or ""),
            )
        )
    return tuple(violations)


def _enforce_tool_choice(
    request: ProviderToolCallRequest,
    adapted: ProviderToolCallResult,
) -> ProviderToolCallResult:
    """Enforce the exact host choice after provider decoding and before execution."""

    choice = request.tool_choice
    if choice is None or not adapted.calls:
        return adapted
    if choice.mode == "none":
        violation = ToolProtocolViolation(
            "TOOL_CHOICE_VIOLATION",
            "the host disabled tools for this informational model turn",
            request.protocol.source_protocol,
        )
        return ProviderToolCallResult((), (*adapted.violations, violation))
    if choice.mode == "specific" and any(
        call.tool_name != choice.tool_name for call in adapted.calls
    ):
        violation = ToolProtocolViolation(
            "TOOL_CHOICE_VIOLATION",
            f"the host required tool {choice.tool_name!r} for this model turn",
            request.protocol.source_protocol,
        )
        return ProviderToolCallResult((), (*adapted.violations, violation))
    return adapted


def _native_tool_calls(request: ProviderToolCallRequest) -> ProviderToolCallResult:
    text = str(getattr(request.response, "text", "") or "")
    # 真机容错(2026-08-06):MiniMax native 偶发在正文【提及】工具标记(如"我来帮你查一下")。
    # _native_prose_violation 只对【完整工具调用格式对】判(如 [TOOL_CALL]...[/TOOL_CALL] 成对),
    # 纯正文提及不判——避免整轮误杀。一旦判出完整格式伪调用,立即返回 violation 不执行任何
    # blocks(治撒谎红线:正文伪调用夹带第二执行解释,即使同响应有合法 blocks 也不执行)。
    prose_violation = _native_prose_violation(text)
    if prose_violation is not None:
        return ProviderToolCallResult(violations=(prose_violation,))
    blocks = getattr(request.response, "tool_use_blocks", None) or []


    if not isinstance(blocks, list):
        return ProviderToolCallResult(
            violations=(
                ToolProtocolViolation(
                    "PROTOCOL_VIOLATION",
                    "native tool_use_blocks must be a list",
                    "native",
                ),
            )
        )
    calls: list[ToolCall] = []
    violations: list[ToolProtocolViolation] = []
    seen_ids: set[str] = set()
    for index, block in enumerate(blocks, start=1):
        if not isinstance(block, dict):
            violations.append(
                ToolProtocolViolation(
                    "PROTOCOL_VIOLATION",
                    f"native block {index} is not an object",
                    "native",
                )
            )
            continue
        call_id = str(block.get("id") or "").strip()
        tool_name = str(block.get("name") or "").strip()
        arguments = block.get("input")
        runtime = request.runtime_snapshot.runtime(tool_name)
        if not call_id or call_id in seen_ids:
            violations.append(
                ToolProtocolViolation(
                    "PROTOCOL_VIOLATION",
                    f"native block {index} has a missing or duplicate call id",
                    "native",
                )
            )
            continue
        if runtime is None:
            violations.append(
                ToolProtocolViolation(
                    "TOOL_UNAVAILABLE",
                    f"native block {index} names a tool outside the run snapshot",
                    "native",
                    tool_name[:160],
                )
            )
            continue
        if not isinstance(arguments, dict):
            violations.append(
                ToolProtocolViolation(
                    "TOOL_INVALID_ARGUMENTS",
                    f"native block {index} input is not an object",
                    "native",
                    tool_name[:160],
                )
            )
            continue
        seen_ids.add(call_id)
        calls.append(
            ToolCall(
                call_id=call_id,
                tool_name=tool_name,
                arguments=arguments,
                source_protocol="native",
                schema_hash=runtime.model_spec.schema_hash,
                run_id=request.protocol.run_id,
                turn_id=request.turn_id,
                attempt_id=request.attempt_id,
                required_action_id=_required_action_id(
                    tool_name,
                    request.required_actions,
                ),
            )
        )
    return ProviderToolCallResult(tuple(calls), tuple(violations))


class TextToolProtocolAdapter:
    """Isolated adapter for explicitly selected non-native runs.

    It accepts only one or more complete, top-level TOOL_CALL blocks and
    rejects prose, Markdown fences, code snippets and partial markers.
    """

    def tool_calls(self, request: ProviderToolCallRequest) -> ProviderToolCallResult:
        text = str(getattr(request.response, "text", "") or "")
        payloads, error = _parse_standalone_text_blocks(text)
        if error:
            return ProviderToolCallResult(
                violations=(
                    ToolProtocolViolation(
                        "PROTOCOL_VIOLATION",
                        error,
                        "text",
                        _protocol_evidence(text),
                    ),
                )
            )
        calls: list[ToolCall] = []
        for index, payload in enumerate(payloads, start=1):
            tool_name = str(payload.pop("tool", "") or "").strip()
            runtime = request.runtime_snapshot.runtime(tool_name)
            if runtime is None:
                return ProviderToolCallResult(
                    violations=(
                        ToolProtocolViolation(
                            "TOOL_UNAVAILABLE",
                            f"text block {index} names a tool outside the run snapshot",
                            "text",
                            tool_name[:160],
                        ),
                    )
                )
            call_id = _text_call_id(request.turn_id, index, tool_name, payload)
            calls.append(
                ToolCall(
                    call_id=call_id,
                    tool_name=tool_name,
                    arguments=payload,
                    source_protocol="text",
                    schema_hash=runtime.model_spec.schema_hash,
                    run_id=request.protocol.run_id,
                    turn_id=request.turn_id,
                    attempt_id=request.attempt_id,
                    required_action_id=_required_action_id(
                        tool_name,
                        request.required_actions,
                    ),
                )
            )
        return ProviderToolCallResult(tuple(calls))


def anthropic_tool_choice(choice: ToolChoice) -> dict[str, str]:
    if choice.mode == "auto":
        return {"type": "auto"}
    if choice.mode == "required":
        return {"type": "any"}
    if choice.mode == "specific":
        return {"type": "tool", "name": choice.tool_name}
    return {"type": "none"}


def openai_tool_choice(choice: ToolChoice) -> str | dict[str, object]:
    if choice.mode in {"auto", "required", "none"}:
        return choice.mode
    return {
        "type": "function",
        "function": {"name": choice.tool_name},
    }


def _native_prose_violation(text: str) -> ToolProtocolViolation | None:
    # 真机容错(2026-08-06):MiniMax native 偶发在正文【提及】工具标记(如"我来帮你查一下"),
    # 不是完整工具调用块。只对【完整工具调用格式】判 violation(如 [TOOL_CALL]...[/TOOL_CALL] 或
    # <tool_call>...</tool_call> 成对出现)——纯正文提及不判,避免整轮误杀;完整格式块 = 治撒谎红线,
    # 仍拦截(即使同响应有合法 blocks 也不执行,防正文夹带第二执行解释)。
    lowered = text.lower()
    has_pair = (
        ("[tool_call]" in lowered and "[/tool_call]" in lowered)
        or ("<tool_call" in lowered and "</tool_call>" in lowered)
        or ("<tool_use" in lowered and "</tool_use>" in lowered)
        or ("<function_call" in lowered and "</function_call>" in lowered)
    )
    if not has_pair:
        return None
    return ToolProtocolViolation(
        "PROTOCOL_VIOLATION",
        "native response contained a textual TOOL_CALL block; prose cannot become executable",
        "native",
        _protocol_evidence(text),
    )


def _parse_standalone_text_blocks(text: str) -> tuple[list[dict[str, Any]], str]:
    # 长期助手 式宽容解析(真机 2026-08-08 scrapy 复刻):弱模型(falsh 类)在工具轮
    # 几乎必然先输出 prose("我需要先看一下…")再写 [TOOL_CALL] 块,严格"纯块"
    # 会把已成功执行的工具轮整轮判 violation 杀死。语义:有合法块就提取执行,
    # 块与块之间的 prose 忽略;完全没有块标记=普通文本回复(非违规);有标记但
    # 没有形成合法块(未闭合/JSON 损坏/围栏包裹)才是协议违规。
    if _TEXT_OPEN not in text and _TEXT_CLOSE not in text:
        return [], ""
    payloads: list[dict[str, Any]] = []
    cursor = 0
    length = len(text)
    while True:
        open_at = text.find(_TEXT_OPEN, cursor)
        if open_at < 0:
            break
        body_start = open_at + len(_TEXT_OPEN)
        close = text.find(_TEXT_CLOSE, body_start)
        if close < 0:
            # 末尾未闭合宽容(真机 2026-08-09 深测):deepseek-v4-flash text 协议
            # 高频形态 = [TOOL_CALL] + 完整 JSON 后不带 [/TOOL_CALL] 就结束响应
            # (块尾即响应尾,82~900 字符的 wait/inspect 调用坐实,8 次 violation
            # 中至少 4 次属此形态)。块的合法性由「JSON 完整可解析」决定而非闭合
            # 标记——JSON 不完整(参数写到一半的真截断:apply_patch 的 patch 断在
            # 中途、raise_event 的字段断在字符串中)照样 json.loads 失败,维持
            # violation,半参数绝不执行。块后仍有 prose 时 loads 同样失败。
            tail = text[body_start:].strip()
            if not tail:
                return [], "text tool block is not closed"
            try:
                payload = json.loads(tail)
            except json.JSONDecodeError:
                return [], "text tool block is not closed"
            if not isinstance(payload, dict):
                return [], "text tool block payload must be an object"
            tool_name = str(payload.get("tool") or "").strip()
            if not tool_name:
                return [], "text tool block is missing tool name"
            payloads.append(dict(payload))
            return payloads, ""
        # 块被 markdown 围栏包裹(仅空白相隔)=模型在展示示例而非发起调用,仍判
        # 违规;普通 prose 前缀(「我先看一下…」)宽容提取,这是弱模型真实输出形态。
        before = open_at
        while before > 0 and text[before - 1].isspace():
            before -= 1
        after = close + len(_TEXT_CLOSE)
        while after < length and text[after].isspace():
            after += 1
        if text[max(0, before - 3) : before] == "```" or text[after : after + 3] == "```":
            return [], "text tool block must not be wrapped in Markdown fences"
        raw = text[body_start:close].strip()
        if not raw:
            return [], "text tool block must contain raw JSON, not Markdown"
        # 不再做字符串级反引号检查:JSON 字符串值里反引号合法(Go raw string/
        # 正则/模板高频,如 write_file 写 Go 代码),字符串级误杀会砍掉合法调用
        # (真机铁证 2026-08-08 celery 复刻:补 broker.go 被"raw JSON, not
        # Markdown"连拦 3 轮 break)。真正的围栏包裹已由上方 before/after 检查
        # 捕获,块内 ```json 围栏由 json.loads 失败兜底。
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return [], "text tool block does not contain one valid JSON object"
        if not isinstance(payload, dict):
            return [], "text tool block payload must be an object"
        tool_name = str(payload.get("tool") or "").strip()
        if not tool_name:
            return [], "text tool block is missing tool name"
        payloads.append(dict(payload))
        cursor = close + len(_TEXT_CLOSE)
    if not payloads:
        return [], "text tool response has tool markers but no valid standalone block"
    return payloads, ""


def _required_action_id(tool_name: str, actions: tuple[object, ...]) -> str:
    for action in actions:
        if str(getattr(action, "status", "") or "").strip().lower() != "open":
            continue
        allowed = tuple(getattr(action, "allowed_tools", ()) or ())
        if not allowed or tool_name in allowed:
            return str(getattr(action, "action_id", "") or "").strip()
    return ""


def _text_call_id(
    turn_id: str,
    index: int,
    tool_name: str,
    arguments: dict[str, Any],
) -> str:
    payload = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(
        f"{turn_id}:{index}:{tool_name}:{payload}".encode()
    ).hexdigest()[:24]
    return f"text_call_{digest}"


def _bounded_preview(text: str, limit: int = 320) -> str:
    value = str(text or "").strip()
    return value if len(value) <= limit else value[:limit] + "..."


def _protocol_evidence(text: str) -> str:
    """Describe hostile protocol-shaped prose without replaying it into the model."""

    value = str(text or "")
    lowered = value.lower()
    markers = sorted(
        marker
        for marker in {_TEXT_OPEN.lower(), _TEXT_CLOSE.lower(), *_NATIVE_PSEUDO_TOOL_MARKERS}
        if marker in lowered
    )
    return json.dumps(
        {
            "chars": len(value),
            "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
            "protocol_markers": markers,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = [
    "ProviderToolCallRequest",
    "ProviderToolCallResult",
    "TextToolProtocolAdapter",
    "ToolProtocolViolation",
    "anthropic_tool_choice",
    "canonical_tool_calls_from_response",
    "openai_tool_choice",
]
