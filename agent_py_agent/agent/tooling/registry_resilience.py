
from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .models import ToolExecutionResult, ToolSpec

_MAX_RETRY_ATTEMPTS = 1
_MAX_PROMPT_OUTPUT_CHARS = 12000
_OUTPUT_HEAD_CHARS = 8000
_OUTPUT_TAIL_CHARS = 2000
_ARTIFACT_SUBDIR = Path("blobs") / "tool_outputs"
_PRESERVE_PROMPT_OUTPUT_TOOLS = frozenset({"read_file"})


@dataclass(frozen=True)
class ResilientToolInvokeRequest:
    invoke: Callable[[], ToolExecutionResult]
    spec: ToolSpec
    workspace_root: Path
    write_boundary: dict[str, object] | None = None


def resilient_tool_invoke(request: ResilientToolInvokeRequest) -> ToolExecutionResult:
    result = request.invoke()
    retry_attempts = 0
    if _should_retry_result(result, request.spec):
        retry_attempts = _MAX_RETRY_ATTEMPTS
        result = request.invoke()
    _attach_resilience_facts(result, retry_attempts)
    _apply_large_output_policy(result, request.workspace_root, request.write_boundary)
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
    workspace_root: Path,
    write_boundary: dict[str, object] | None,
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
    artifact_ref = _write_output_artifact(result, workspace_root, write_boundary)
    original_chars = len(result.output)
    result.output = _truncated_output(result.output, artifact_ref=artifact_ref, original_chars=original_chars)
    _merge_tool_output_policy(
        result,
        {
            "truncated": True,
            "artifact_ref": artifact_ref,
            "original_chars": original_chars,
            "prompt_chars": len(result.output),
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


def _write_output_artifact(
    result: ToolExecutionResult,
    workspace_root: Path,
    write_boundary: dict[str, object] | None,
) -> str:
    digest = hashlib.sha256(result.output.encode("utf-8", "replace")).hexdigest()[:16]
    safe_tool = "".join(char if char.isalnum() or char in {"_", "-"} else "_" for char in result.tool) or "tool"
    root, ref_root = _tool_output_artifact_roots(workspace_root, write_boundary)
    target = root / f"{safe_tool}-{digest}.txt"
    _write_text_atomic(target, result.output)
    return _display_ref(target, ref_root)


def _tool_output_artifact_roots(
    workspace_root: Path,
    write_boundary: dict[str, object] | None,
) -> tuple[Path, Path]:
    task_work = _boundary_path(write_boundary, "task_work_dir")
    if task_work is not None:
        return task_work / _ARTIFACT_SUBDIR, _boundary_path(write_boundary, "task_root") or task_work.parent
    workspace = Path(workspace_root).expanduser().resolve(strict=False)
    return workspace / "work" / _ARTIFACT_SUBDIR, workspace


def _boundary_path(write_boundary: dict[str, object] | None, key: str) -> Path | None:
    if not isinstance(write_boundary, dict):
        return None
    text = str(write_boundary.get(key) or "").strip()
    if not text:
        return None
    try:
        return Path(text).expanduser().resolve(strict=False)
    except OSError:
        return None


def _display_ref(target: Path, ref_root: Path) -> str:
    try:
        return target.resolve(strict=False).relative_to(ref_root.resolve(strict=False)).as_posix()
    except ValueError:
        return str(target.resolve(strict=False))


def _write_text_atomic(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _truncated_output(text: str, *, artifact_ref: str, original_chars: int) -> str:
    return (
        text[:_OUTPUT_HEAD_CHARS].rstrip()
        + "\n...[tool output truncated]...\n"
        + text[-_OUTPUT_TAIL_CHARS:].lstrip()
        + f"\n\n完整工具输出已归档: {artifact_ref} (original_chars={original_chars})"
    )


__all__ = [
    "ResilientToolInvokeRequest",
    "attach_tool_output_projection",
    "resilient_tool_invoke",
]
