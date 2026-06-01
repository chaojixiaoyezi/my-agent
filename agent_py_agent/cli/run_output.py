# LLM: Run CLI output helpers keep cmd_run thin and status mapping structured.
# 模块用途: 处理 run 命令的流式输出、最终摘要和机器状态退出码。

from __future__ import annotations

import sys

from ..agent.backends import (
    ProviderRecoverableError,
    provider_recoverable_report,
)
from .thinking_spinner import ThinkingSpinner


# LLM: make_run_chunk_writer keeps streaming stdout state out of cmd_run.
# 函数用途: 生成 run 的流式输出回调，并记录是否已经向终端写过 response 正文。
def make_run_chunk_writer(spinner: ThinkingSpinner, stream_state: dict[str, object]):
    # LLM: _on_run_chunk is the tiny stdout sink used by streaming CLI runs.
    # 函数用途: 收到模型流式片段时停止 spinner、写入终端，并记录正文已流式输出。
    def _on_run_chunk(chunk: str) -> None:
        stream_state["seen"] = True
        stream_state["text"] = str(stream_state.get("text", "")) + chunk
        spinner.stop()
        sys.stdout.write(chunk)
        sys.stdout.flush()

    return _on_run_chunk


# LLM: print_run_result prints final CLI metadata without hiding post-tool final answers.
# 函数用途: 输出 run 的最终文本、调试 prompt、统计信息和 compact 建议；已完整流式打印的正文不重复打印。
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
        f"prompt_tokens≈{result.prompt_token_estimate}; inject_tokens≈{result.runtime_injection_token_estimate}; "
        f"archive_events={result.archive_events}; "
        f"resume_context={1 if result.memory_resume_context_injected else 0}; "
        f"resume_tokens≈{result.memory_resume_context_token_estimate}]"
    )
    _print_compact_suggestion(result)


# LLM: run_exit_code maps structured runtime status to process status without reading prose.
# 函数用途: 让系统级阻断、失败或超时用非零退出码暴露给调度器和真实 E2E。
def run_exit_code(result) -> int:
    raw_status = getattr(result, "runtime_status", "ok")
    status = raw_status.strip().lower() if isinstance(raw_status, str) else "ok"
    return 0 if status in {"", "ok", "succeeded"} else 2


# LLM: provider_recoverable_cli_report converts typed provider failures into one readable CLI result.
# 函数用途: 顶层 run 遇到 timeout/429/5xx/断线时，用统一恢复提示退出，避免各入口重复分类。
def provider_recoverable_cli_report(agent, exc: ProviderRecoverableError) -> str:
    return provider_recoverable_report(
        exc,
        timeout_seconds=getattr(getattr(agent, "config", None), "request_timeout", ""),
    )


# LLM: _should_print_final_response separates streamed-visible text from hidden post-tool final responses.
# 函数用途: 判断最终 response 是否已经完整出现在流式输出中，避免重复打印或吞掉工具后的最终回答。
def _should_print_final_response(response: str, streamed_text: str) -> bool:
    if not response:
        return False
    return response not in streamed_text


# LLM: _print_compact_suggestion keeps compact output out of cmd_run size-sensitive orchestration.
# 函数用途: 打印 compact 建议、auto cycle 停车状态和推荐命令；只读 result，不触发 apply 或 resume。
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
