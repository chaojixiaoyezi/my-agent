
from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .models import ToolExecutionResult, ToolSpec

_MAX_RETRY_ATTEMPTS = 1
_MAX_PROMPT_OUTPUT_CHARS = 12000
_OUTPUT_HEAD_CHARS = 8000
_OUTPUT_TAIL_CHARS = 2000
_ARTIFACT_DIR = ".agent_tool_outputs"


def resilient_tool_invoke(
    *,
    invoke: Callable[[], ToolExecutionResult],
    spec: ToolSpec,
    payload: dict[str, Any],
    workspace_root: Path,
) -> ToolExecutionResult:
    result = invoke()
    retry_attempts = 0
    if _should_retry_result(result, spec, payload):
        retry_attempts = _MAX_RETRY_ATTEMPTS
        result = invoke()
    _attach_resilience_facts(result, retry_attempts)
    _apply_large_output_policy(result, workspace_root)
    return result


def _should_retry_result(result: ToolExecutionResult, spec: ToolSpec, payload: dict[str, Any]) -> bool:
    if result.ok or not result.retryable:
        return False
    if spec.effect == "read_only":
        return True
    return bool(spec.requires_idempotency and payload.get("idempotency_key"))


def _attach_resilience_facts(result: ToolExecutionResult, retry_attempts: int) -> None:
    result.result_envelope.setdefault("tool_resilience", {})["retry_attempts"] = retry_attempts


def _apply_large_output_policy(result: ToolExecutionResult, workspace_root: Path) -> None:
    if not result.ok or len(result.output) <= _MAX_PROMPT_OUTPUT_CHARS:
        result.result_envelope.setdefault("tool_output_policy", {"truncated": False})
        return
    if _preserve_prompt_output(result):
        result.result_envelope["tool_output_policy"] = {
            "truncated": False,
            "preserved": True,
            "original_chars": len(result.output),
        }
        return
    artifact_ref = _write_output_artifact(result, workspace_root)
    original_chars = len(result.output)
    result.output = _truncated_output(result.output, artifact_ref=artifact_ref, original_chars=original_chars)
    result.result_envelope["tool_output_policy"] = {
        "truncated": True,
        "artifact_ref": artifact_ref,
        "original_chars": original_chars,
        "prompt_chars": len(result.output),
    }


def _preserve_prompt_output(result: ToolExecutionResult) -> bool:
    policy = result.result_envelope.get("tool_output_policy")
    return isinstance(policy, dict) and bool(policy.get("preserve_prompt_output"))


def _write_output_artifact(result: ToolExecutionResult, workspace_root: Path) -> str:
    digest = hashlib.sha256(result.output.encode("utf-8", "replace")).hexdigest()[:16]
    safe_tool = "".join(char if char.isalnum() or char in {"_", "-"} else "_" for char in result.tool) or "tool"
    artifact = Path(_ARTIFACT_DIR) / f"{safe_tool}-{digest}.txt"
    target = Path(workspace_root).resolve(strict=False) / artifact
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(result.output, encoding="utf-8")
    return artifact.as_posix()


def _truncated_output(text: str, *, artifact_ref: str, original_chars: int) -> str:
    return (
        text[:_OUTPUT_HEAD_CHARS].rstrip()
        + "\n...[tool output truncated]...\n"
        + text[-_OUTPUT_TAIL_CHARS:].lstrip()
        + f"\n\n完整工具输出已归档: {artifact_ref} (original_chars={original_chars})"
    )


__all__ = ["resilient_tool_invoke"]
