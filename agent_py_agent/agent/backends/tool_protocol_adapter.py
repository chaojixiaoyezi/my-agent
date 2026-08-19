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
from .text_protocol_parser import (
    _TEXT_CLOSE,
    _TEXT_OPEN,
    MAX_BLOCK_CHARS,
    MAX_RESPONSE_CHARS,
    MAX_TEXT_CALLS,
    scan_text_blocks,
)

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
        # text 协议已删除(EXEC-31b): 非 native 快照只能来自旧数据/配置错误, 不执行。
        adapted = ProviderToolCallResult(
            violations=(
                ToolProtocolViolation(
                    "PROTOCOL_VIOLATION",
                    "text tool protocol was removed; only native is supported",
                    request.protocol.source_protocol,
                ),
            )
        )
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


def _required_action_id(tool_name: str, actions: tuple[object, ...]) -> str:
    for action in actions:
        if str(getattr(action, "status", "") or "").strip().lower() != "open":
            continue
        allowed = tuple(getattr(action, "allowed_tools", ()) or ())
        if not allowed or tool_name in allowed:
            return str(getattr(action, "action_id", "") or "").strip()
    return ""


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
    "ToolProtocolViolation",
    "anthropic_tool_choice",
    "canonical_tool_calls_from_response",
    "openai_tool_choice",
]
