"""LLM: TUI worker body — runs in a daemon thread consuming the job queue.

给人看的解释：
从 tui.py 拆出来避免文件超过 600 行。
处理每一轮对话：本地模型调用、gateway 代理、流式输出、spinner、结果记录。
"""

from __future__ import annotations

import queue
import time
from dataclasses import dataclass
from typing import Any

from .gateway_client import (
    ChatRequestContent,
    check_gateway_alive,
    poll_gateway_chunks,
    submit_chat_request,
)
from .renderer import GRAY, GREEN, RESET


@dataclass
class TuiWorkerConfig:
    """Bundle of all _tui_worker_body parameters."""
    jobs: Any  # queue.Queue
    state_lock: Any
    is_running_ref: list
    pending_jobs_ref: list
    running_prompt_ref: list
    running_started_at_ref: list
    agent: Any
    args: Any
    paths: Any
    use_gateway: bool
    conversation_history: list[tuple[str, str]]
    history_lock: Any
    build_history_context: Any
    assistant_outputs: list[str]
    thinking_line_ref: list
    stream_buf_ref: list
    app_ref: list
    last_token_estimate_ref: list
    stop_event: Any


_CHAT_RESPONSE_STYLE_INJECT = (
    "这是 CLI 聊天界面。回答风格要求："
    "1. 不要用模板化欢迎词；"
    "2. 不要在结尾主动列出'你可以问我这三个问题'这类建议问题；"
    "3. 直接围绕用户当前输入回答，除非用户要求，否则不要做教学式铺垫；"
    "4. 除非用户明确要求，不要先介绍你会什么、不要先列能力清单；"
    "5. 默认优先用简短自然语言回答，不要动不动列 1、2、3。"
)


def _tui_update_running_state(cfg, job):
    """Update running state when starting to process a job."""
    with cfg.state_lock:
        cfg.pending_jobs_ref[0] -= 1
        cfg.is_running_ref[0] = True
        cfg.running_prompt_ref[0] = job.user
        cfg.running_started_at_ref[0] = time.perf_counter()


def _tui_cleanup_after_job(cfg, job, agent_response_text, response_recorded):
    """Cleanup after job completes: record response, update history, reset state."""
    if agent_response_text:
        if not response_recorded:
            cfg.assistant_outputs.append(agent_response_text)
        _append_conversation_turn(
            cfg.conversation_history,
            cfg.history_lock,
            job.user,
            agent_response_text,
        )
    with cfg.state_lock:
        cfg.is_running_ref[0] = False
        cfg.running_prompt_ref[0] = ""
        cfg.running_started_at_ref[0] = 0.0
    cfg.thinking_line_ref[0] = ""
    cfg.stream_buf_ref[0] = ""
    cfg.jobs.task_done()


def _tui_process_job(cfg, job):
    """Process a single job. Returns (agent_response_text, response_recorded)."""
    started_at = cfg.running_started_at_ref[0]
    history_ctx = cfg.build_history_context()
    turn_inject = list(job.inject) + [_CHAT_RESPONSE_STYLE_INJECT] + ([history_ctx] if history_ctx else [])
    next_message_id = len(cfg.assistant_outputs) + 1

    spinner, on_spinner_update = _make_spinner(cfg, next_message_id)
    spinner.start()

    begin_stream, on_stream_chunk = _make_stream_callbacks(cfg, next_message_id, spinner)

    if cfg.use_gateway:
        return _worker_gateway_path(cfg, job, turn_inject, started_at, on_stream_chunk)
    else:
        return _worker_local_path(cfg, job, turn_inject, started_at, on_stream_chunk)


def _tui_worker_body(cfg: TuiWorkerConfig) -> None:
    """Worker loop for TUI mode. Runs in a separate daemon thread."""
    while not cfg.stop_event.is_set():
        try:
            job = cfg.jobs.get(timeout=0.5)
        except queue.Empty:
            continue
        _tui_update_running_state(cfg, job)
        agent_response_text = ""
        response_recorded = False
        try:
            agent_response_text, response_recorded = _tui_process_job(cfg, job)
        except Exception as exc:
            _set_thinking_line("", cfg.thinking_line_ref)
            from .rendering import _cprint
            _cprint(f"错误: {exc}")
            agent_response_text = ""
        finally:
            _tui_cleanup_after_job(cfg, job, agent_response_text, response_recorded)


def _make_spinner(cfg: TuiWorkerConfig, next_message_id: int):
    """Create spinner and its update callback."""
    def on_spinner_update(text: str) -> None:
        _set_thinking_line(text, cfg.thinking_line_ref)
        if cfg.app_ref[0] is not None:
            cfg.app_ref[0].invalidate()

    from ..thinking_spinner import ThinkingSpinner
    spinner = ThinkingSpinner(
        on_update=on_spinner_update,
        on_stop=lambda: (
            _set_thinking_line("", cfg.thinking_line_ref),
            cfg.app_ref[0].invalidate() if cfg.app_ref[0] else None,
        ),
    )
    return spinner, on_spinner_update


def _make_stream_callbacks(cfg: TuiWorkerConfig, next_message_id: int, spinner):
    """Create begin_stream and on_stream_chunk closures."""
    stream_started_ref = [False]
    stream_has_visible_ref = [False]

    def begin_stream() -> None:
        if stream_started_ref[0]:
            return
        spinner.stop()
        from .rendering import _cprint
        _cprint(f"\n{GREEN}{cfg.agent.config.agent_name}#{next_message_id}>{RESET}")
        stream_started_ref[0] = True

    def on_stream_chunk(chunk: str) -> None:
        if not chunk or not chunk.strip():
            return
        begin_stream()
        stream_has_visible_ref[0] = True
        _append_stream_text(chunk, cfg.stream_buf_ref)

    return begin_stream, on_stream_chunk


def _worker_gateway_path(
    cfg: TuiWorkerConfig,
    job,
    turn_inject: list[str],
    started_at: float,
    on_stream_chunk,
) -> tuple[str, bool]:
    """Handle gateway mode job processing. Returns (agent_response_text, response_recorded)."""
    from .rendering import _cprint

    if not check_gateway_alive(cfg.paths):
        raise RuntimeError("gateway 已停止。请先执行: my-agent gateway start")
    request_id, chunk_path, response_path = submit_chat_request(
        cfg.paths,
        content=ChatRequestContent(
            prompt=job.user,
            inject=turn_inject,
            prompt_files=job.prompt_files,
            save=not cfg.args.no_save,
            show_prompt=job.show_prompt,
            resume_context=resume_context_override(cfg.args),
        ),
        agent=cfg.agent,
    )
    timeout = cfg.args.gateway_timeout if cfg.args.gateway_timeout is not None else cfg.agent.config.gateway_request_timeout
    chunks_printed_ref = [0]
    deadline = time.time() + max(0.0, timeout)
    response = poll_gateway_chunks(
        chunk_path, response_path, deadline, on_stream_chunk, chunks_printed_ref=chunks_printed_ref
    )
    elapsed = time.perf_counter() - started_at
    if response:
        _flush_stream_buf(cfg.stream_buf_ref)
    if not response:
        raise TimeoutError(f"gateway 请求等待超时: request_id={request_id} response={response_path}")
    if job.show_prompt and response.get("prompt"):
        _cprint("===== FINAL PROMPT =====")
        _cprint(response.get("prompt", ""))
        _cprint("===== RESPONSE =====")
    _cprint(
        f"{GRAY}[耗时 {elapsed:.2f}s; gateway_request={request_id}; "
        f"工具轮数 {response.get('tool_rounds', 0)}; "
        f"prompt_tokens~{response.get('prompt_token_estimate', 0)}; "
        f"resume_context={1 if response.get('memory_resume_context_injected') else 0}]{RESET}"
    )
    if response.get("ok"):
        agent_response_text = _update_response_state(
            response, cfg.state_lock, cfg.last_token_estimate_ref,
        )
        response_recorded = _maybe_record_response(
            agent_response_text, False, cfg.assistant_outputs, cfg.agent,
        )
        return agent_response_text, response_recorded
    else:
        _cprint(f"错误: {response.get('error', 'gateway 请求失败')}")
        return "", False


def _worker_local_path(
    cfg: TuiWorkerConfig,
    job,
    turn_inject: list[str],
    started_at: float,
    on_stream_chunk,
) -> tuple[str, bool]:
    """Handle local mode job processing. Returns (agent_response_text, response_recorded)."""
    from .rendering import _cprint, _render_assistant_response

    result = cfg.agent.run(
        job.user,
        inject=turn_inject,
        prompt_files=job.prompt_files,
        save=not cfg.args.no_save,
        source="chat",
        resume_context=resume_context_override(cfg.args),
        recovery_next_actions=["如需恢复本轮 chat，先用 memory-resume 搜索用户消息或时间范围。"],
        on_chunk=on_stream_chunk,
    )
    elapsed = time.perf_counter() - started_at
    _flush_stream_buf(cfg.stream_buf_ref)
    if job.show_prompt:
        _cprint("===== FINAL PROMPT =====")
        _cprint(result.prompt)
        _cprint("===== RESPONSE =====")
    _cprint(
        f"{GRAY}[耗时 {elapsed:.2f}s; 工具轮数 {result.tool_rounds}; "
        f"prompt_tokens~{result.prompt_token_estimate}; "
        f"resume_context={1 if result.memory_resume_context_injected else 0}]{RESET}"
    )
    agent_response_text = result.response
    with cfg.state_lock:
        cfg.last_token_estimate_ref[0] = result.prompt_token_estimate
    if agent_response_text.strip():
        _render_assistant_response(agent_response_text, cfg.assistant_outputs, cfg.agent.config.agent_name)
        return agent_response_text, True
    return agent_response_text, False


# ---------------------------------------------------------------------------
# Stream helpers
# ---------------------------------------------------------------------------

def _append_stream_text(chunk: str, stream_buf_ref: list) -> None:
    """Append text to stream buffer, emitting complete lines."""
    if not chunk:
        return
    stream_buf_ref[0] += chunk
    while "\n" in stream_buf_ref[0]:
        line, stream_buf_ref[0] = stream_buf_ref[0].split("\n", 1)
        _emit_stream_line(line)


def _emit_stream_line(text: str) -> None:
    """Emit a single stream line in green."""
    from .rendering import _cprint
    _cprint(f"{GREEN}{text}{RESET}")


def _flush_stream_buf(stream_buf_ref: list) -> None:
    """Flush pending stream buffer lines."""
    buf = stream_buf_ref[0]
    if buf:
        _emit_stream_line(buf)
        stream_buf_ref[0] = ""


def _set_thinking_line(text: str, thinking_line_ref: list) -> None:
    """Set thinking line, stripping formatting artifacts."""
    import re
    if not text:
        thinking_line_ref[0] = ""
        return
    cleaned = text.replace("╭ 蛐蛐人：", "").strip()
    cleaned = re.sub(r"\s+\d+\.\d+s$", "", cleaned)
    thinking_line_ref[0] = cleaned


def _update_response_state(
    response: dict,
    state_lock,
    last_token_estimate_ref: list,
) -> str:
    """Update shared state after gateway response. Returns response text."""
    agent_response_text = response.get("response", "")
    with state_lock:
        last_token_estimate_ref[0] = response.get("prompt_token_estimate", 0)
    return agent_response_text


def _maybe_record_response(
    text: str,
    stream_has_visible_text: bool,
    assistant_outputs: list[str],
    agent,
) -> bool:
    """Conditionally render and record response. Returns True if recorded."""
    if text and (not stream_has_visible_text) and text.strip():
        from .rendering import _render_assistant_response
        _render_assistant_response(text, assistant_outputs, agent.config.agent_name)
        return True
    return False


# ---------------------------------------------------------------------------
# Conversation history helper
# ---------------------------------------------------------------------------

MAX_HISTORY_TURNS = 8


def _append_conversation_turn(
    conversation_history: list[tuple[str, str]],
    history_lock,
    user_message: str,
    assistant_message: str,
) -> None:
    """Append one turn and keep the in-memory buffer bounded."""
    with history_lock:
        conversation_history.append((user_message, assistant_message))
        if len(conversation_history) > MAX_HISTORY_TURNS * 2:
            conversation_history[:] = conversation_history[-MAX_HISTORY_TURNS:]


def resume_context_override(args) -> str | None:
    """Get resume_context override from args."""
    if hasattr(args, "resume_context"):
        return args.resume_context
    return None