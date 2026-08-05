
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .models import ToolExecutionResult, ToolSpec

_MAX_RETRY_ATTEMPTS = 1
_MAX_PROMPT_OUTPUT_CHARS = 12000
_OUTPUT_HEAD_CHARS = 8000
_OUTPUT_TAIL_CHARS = 2000
_PRESERVE_PROMPT_OUTPUT_TOOLS = frozenset({"read_file"})


@dataclass(frozen=True)
class ResilientToolInvokeRequest:
    invoke: Callable[[], ToolExecutionResult]
    spec: ToolSpec


def resilient_tool_invoke(request: ResilientToolInvokeRequest) -> ToolExecutionResult:
    result = request.invoke()
    retry_attempts = 0
    if _should_retry_result(result, request.spec):
        retry_attempts = _MAX_RETRY_ATTEMPTS
        result = request.invoke()
    _attach_resilience_facts(result, retry_attempts)
    _apply_large_output_policy(result)
    return result


def attach_tool_output_projection(
    result: ToolExecutionResult,
    spec: ToolSpec,
) -> None:
    """Attach the effective model projection policy to an executed result.

    ToolSpec owns the baseline. A handler may only tighten that baseline for a
    particular result (for example, read_file opening an external tool-output
    artifact); it must never downgrade a spec-declared external/default policy.
    """

    policy = result.result_envelope.get("tool_output_policy")
    result_trust = _declared_policy_value(policy, "trust")
    result_redaction = _declared_policy_value(policy, "redaction")
    _merge_tool_output_policy(
        result,
        {
            "trust": (
                "external_data"
                if "external_data" in {spec.output_trust, result_trust}
                else "runtime"
            ),
            "redaction": (
                "default"
                if "default" in {spec.output_redaction, result_redaction}
                else "source_code"
            ),
        },
    )


def _should_retry_result(result: ToolExecutionResult, spec: ToolSpec) -> bool:
    if result.ok or not result.retryable:
        return False
    return spec.effect == "read_only"


def _attach_resilience_facts(result: ToolExecutionResult, retry_attempts: int) -> None:
    result.result_envelope.setdefault("tool_resilience", {})["retry_attempts"] = retry_attempts


def _apply_large_output_policy(
    result: ToolExecutionResult,
) -> None:
    if not result.ok or len(result.output) <= _MAX_PROMPT_OUTPUT_CHARS:
        result.result_envelope.setdefault("tool_output_policy", {"truncated": False})
        return
    if _preserve_prompt_output(result):
        _merge_tool_output_policy(
            result,
            {
                "truncated": False,
                "preserved": True,
                "original_chars": len(result.output),
            },
        )
        return
    original_chars = len(result.output)
    live_prompt_output = _truncated_output(
        result.output,
        original_chars=original_chars,
    )
    _merge_tool_output_policy(
        result,
        {
            "truncated": True,
            "original_chars": original_chars,
            "prompt_chars": len(live_prompt_output),
            "live_prompt_output": live_prompt_output,
        },
    )


def _merge_tool_output_policy(result: ToolExecutionResult, facts: dict[str, object]) -> None:
    policy = result.result_envelope.setdefault("tool_output_policy", {})
    if not isinstance(policy, dict):
        policy = {}
        result.result_envelope["tool_output_policy"] = policy
    policy.update(facts)


def _declared_policy_value(policy: object, key: str) -> str:
    if not isinstance(policy, dict) or key not in policy:
        return ""
    return str(policy.get(key) or "").strip().lower()


def _preserve_prompt_output(result: ToolExecutionResult) -> bool:
    if str(result.tool or "") in _PRESERVE_PROMPT_OUTPUT_TOOLS:
        return True
    policy = result.result_envelope.get("tool_output_policy")
    return isinstance(policy, dict) and bool(policy.get("preserve_prompt_output"))


def _truncated_output(text: str, *, original_chars: int) -> str:
    return (
        text[:_OUTPUT_HEAD_CHARS].rstrip()
        + "\n...[tool output truncated]...\n"
        + text[-_OUTPUT_TAIL_CHARS:].lstrip()
        + (
            "\n\n完整工具输出由统一归档层保存；"
            f"使用本次 output_scoped_call_id 分段读取 (original_chars={original_chars})"
        )
    )


__all__ = [
    "ResilientToolInvokeRequest",
    "attach_tool_output_projection",
    "resilient_tool_invoke",
]
