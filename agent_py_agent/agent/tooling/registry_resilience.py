# LLM: Registry resilience adds safe retries and refs-only large-output handling at the tool gateway.
# 模块用途: 在工具执行网关统一处理可重试错误和大输出归档，避免模型循环重试或把大文本塞回上下文。

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


# LLM: resilient_tool_invoke wraps one authorized tool call with bounded gateway resilience.
# 函数用途: 对安全可重试工具做一次有限重试，并把超大输出转成 artifact ref。
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


# LLM: _should_retry_result restricts retry to retryable errors and safe side-effect classes.
# 函数用途: 只读工具可自动重试；可变更工具必须显式具备幂等条件才允许网关重放。
def _should_retry_result(result: ToolExecutionResult, spec: ToolSpec, payload: dict[str, Any]) -> bool:
    if result.ok or not result.retryable:
        return False
    if spec.effect == "read_only":
        return True
    return bool(spec.requires_idempotency and payload.get("idempotency_key"))


# LLM: _attach_resilience_facts records retry behavior even when no retry happened.
# 函数用途: 给工具结果留下机器可读 retry_attempts，便于 replay/审计区分模型重试和网关重试。
def _attach_resilience_facts(result: ToolExecutionResult, retry_attempts: int) -> None:
    result.result_envelope.setdefault("tool_resilience", {})["retry_attempts"] = retry_attempts


# LLM: _apply_large_output_policy archives oversized outputs and keeps prompt payload bounded.
# 函数用途: 完整工具输出落盘，返回给模型的 output 只保留头尾和 artifact_ref。
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


# LLM: _preserve_prompt_output respects tool-declared machine outputs that must remain parseable.
# 函数用途: 允许工具通过结构化 envelope 声明完整输出必须保留，不把 JSON/schema 清单截成非法文本。
def _preserve_prompt_output(result: ToolExecutionResult) -> bool:
    policy = result.result_envelope.get("tool_output_policy")
    return isinstance(policy, dict) and bool(policy.get("preserve_prompt_output"))


# LLM: _write_output_artifact stores full tool output content under the workspace.
# 函数用途: 用 hash 稳定生成工具输出文件，重复大输出不会制造随机路径。
def _write_output_artifact(result: ToolExecutionResult, workspace_root: Path) -> str:
    digest = hashlib.sha256(result.output.encode("utf-8", "replace")).hexdigest()[:16]
    safe_tool = "".join(char if char.isalnum() or char in {"_", "-"} else "_" for char in result.tool) or "tool"
    artifact = Path(_ARTIFACT_DIR) / f"{safe_tool}-{digest}.txt"
    target = Path(workspace_root).resolve(strict=False) / artifact
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(result.output, encoding="utf-8")
    return artifact.as_posix()


# LLM: _truncated_output gives the model enough context plus a durable ref to full content.
# 函数用途: 构造头尾截断文本，避免大工具输出直接撑爆上下文。
def _truncated_output(text: str, *, artifact_ref: str, original_chars: int) -> str:
    return (
        text[:_OUTPUT_HEAD_CHARS].rstrip()
        + "\n...[tool output truncated]...\n"
        + text[-_OUTPUT_TAIL_CHARS:].lstrip()
        + f"\n\n完整工具输出已归档: {artifact_ref} (original_chars={original_chars})"
    )


__all__ = ["resilient_tool_invoke"]
