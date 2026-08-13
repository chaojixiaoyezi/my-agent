
from __future__ import annotations

import sys
from dataclasses import dataclass

from ..agent.backends import (
    ProviderRecoverableError,
    provider_recoverable_report,
)
from ..agent.gateway_parts.response_renderer import current_context_token_estimate
from .thinking_spinner import ThinkingSpinner


@dataclass
class _LocalRunChunkWriter:
    """Keep tentative model deltas private until the final result is committed."""

    spinner: ThinkingSpinner
    stream_state: dict[str, object]

    def __call__(self, chunk: str) -> None:
        self.write_model(chunk)

    def write_model(self, chunk: str) -> None:
        if not chunk:
            return
        self.stream_state["model_text"] = (
            str(self.stream_state.get("model_text", "")) + chunk
        )
        self.spinner.stop()

    def write_progress(self, _event: dict[str, object], legacy_text: str) -> None:
        if not legacy_text:
            return
        self.stream_state["seen"] = True
        self.stream_state["text"] = (
            str(self.stream_state.get("text", "")) + legacy_text
        )
        self.spinner.stop()
        sys.stdout.write(legacy_text)
        sys.stdout.flush()


def make_run_chunk_writer(spinner: ThinkingSpinner, stream_state: dict[str, object]):
    return _LocalRunChunkWriter(spinner, stream_state)


# LLM: Print model-authored final content separately from program-owned runtime diagnostics;
# typed transcript degradation is observable but must never rewrite or replace the model body.
# 函数用途: 输出 CLI 最终回复、运行摘要及结构化降级提示，避免把系统诊断混入模型正文。
def print_run_result(result, *, show_prompt: bool, streamed_text: str = "") -> None:
    if show_prompt:
        print("===== FINAL PROMPT =====")
        print(result.prompt)
        print("===== RESPONSE =====")
    pending_response = _unstreamed_final_response(result, streamed_text)
    if pending_response:
        if streamed_text and not streamed_text.endswith("\n"):
            print()
        print(pending_response)
    print(
        f"\n[backend={result.backend}; used_memories={result.used_memories}; "
        f"tool_rounds={result.tool_rounds}; routed_rules={result.memory_route_matches}; "
        f"ctx_tokens≈{current_context_token_estimate(result)}; "
        f"prompt_tokens≈{result.prompt_token_estimate}; inject_tokens≈{result.runtime_injection_token_estimate}; "
        f"archive_events={result.archive_events}; "
        f"resume_context={1 if result.memory_resume_context_injected else 0}; "
        f"resume_tokens≈{result.memory_resume_context_token_estimate}]"
    )
    if getattr(result, "conversation_persist_degraded", False) is True:
        detail = str(getattr(result, "conversation_persist_error", "") or "").strip()
        print(
            "[conversation_persist_degraded] 最终回复已返回，但 assistant 会话记录未可靠落账。"
            + (f" detail={detail}" if detail else "")
        )
    _print_compact_suggestion(result)


def run_exit_code(result) -> int:
    raw_status = getattr(result, "runtime_status", "ok")
    status = raw_status.strip() if isinstance(raw_status, str) else "ok"
    return 0 if status in {"", "ok"} else 2


def provider_recoverable_cli_report(agent, exc: ProviderRecoverableError) -> str:
    return provider_recoverable_report(
        exc,
        timeout_seconds=getattr(getattr(agent, "config", None), "request_timeout", ""),
    )


def _unstreamed_final_response(result: object, streamed_text: str) -> str:
    """Return only the committed final text not already delivered by streaming.

    The model-authored body and program-owned additions are separate structured
    fields on ``AgentRunResult``.  A streamed model body may be suppressed only
    in the safe direction: it is the exact tail of delivered model output and
    the exact prefix of the committed response.  This preserves the rendered
    final-item state and 长期助手' prefix-only stream matching without inspecting
    what the prose says.
    """
    response = str(getattr(result, "response", "") or "")
    if not response:
        return ""
    model_response = str(getattr(result, "model_response", "") or "").rstrip()
    delivered = str(streamed_text or "").rstrip()
    if (
        model_response
        and delivered.endswith(model_response)
        and response.startswith(model_response)
    ):
        return response[len(model_response) :].lstrip("\n")
    return "" if response in streamed_text else response


def _print_compact_suggestion(result) -> None:
    if not result.memory_compact_suggested:
        return
    print(f"[compact_suggestion={result.memory_compact_status}; {result.memory_compact_message}]")
    print(
        "[compact_auto="
        f"{result.memory_compact_auto_status}; next={result.memory_compact_auto_next_action}; "
        f"tools={result.memory_compact_auto_tool_execution}; "
        f"continue_ready={result.memory_compact_auto_continue_ready}; "
        f"apply_id={result.memory_compact_auto_apply_id or '-'}]"
    )
    for command in result.memory_compact_commands or []:
        print(f"- {command}")
