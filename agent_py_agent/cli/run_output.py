
from __future__ import annotations

import sys

from ..agent.backends import (
    ProviderRecoverableError,
    provider_recoverable_report,
)
from ..agent.gateway_parts.response_renderer import current_context_token_estimate
from .thinking_spinner import ThinkingSpinner


def make_run_chunk_writer(spinner: ThinkingSpinner, stream_state: dict[str, object]):
    def _on_run_chunk(chunk: str) -> None:
        stream_state["seen"] = True
        stream_state["text"] = str(stream_state.get("text", "")) + chunk
        spinner.stop()
        sys.stdout.write(chunk)
        sys.stdout.flush()

    return _on_run_chunk


def print_run_result(result, *, show_prompt: bool, streamed_text: str = "") -> None:
    if show_prompt:
        print("===== FINAL PROMPT =====")
        print(result.prompt)
        print("===== RESPONSE =====")
    if _should_print_final_response(str(result.response), streamed_text):
        if streamed_text and not streamed_text.endswith("\n"):
            print()
        print(result.response)
    print(
        f"\n[backend={result.backend}; used_memories={result.used_memories}; "
        f"tool_rounds={result.tool_rounds}; routed_rules={result.memory_route_matches}; "
        f"ctx_tokens≈{current_context_token_estimate(result)}; "
        f"prompt_tokens≈{result.prompt_token_estimate}; inject_tokens≈{result.runtime_injection_token_estimate}; "
        f"archive_events={result.archive_events}; "
        f"resume_context={1 if result.memory_resume_context_injected else 0}; "
        f"resume_tokens≈{result.memory_resume_context_token_estimate}]"
    )
    _print_compact_suggestion(result)


def run_exit_code(result) -> int:
    raw_status = getattr(result, "runtime_status", "ok")
    status = raw_status.strip().lower() if isinstance(raw_status, str) else "ok"
    return 0 if status in {"", "ok"} else 2


def provider_recoverable_cli_report(agent, exc: ProviderRecoverableError) -> str:
    return provider_recoverable_report(
        exc,
        timeout_seconds=getattr(getattr(agent, "config", None), "request_timeout", ""),
    )


def _should_print_final_response(response: str, streamed_text: str) -> bool:
    if not response:
        return False
    return response not in streamed_text


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
