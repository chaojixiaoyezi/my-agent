# LLM: plain与TUI共用认证HTTP提交原输入/消息ID；原同ID终态观察和模型公开净化仍唯一。

# LLM: 本模块执行 plain chat 的 Gateway/direct 路径；Gateway 历史只能由服务端 ConversationStore 注入，避免上下文重复。
# 模块用途: 处理普通终端单轮聊天与历史；local正文等同call确认，Gateway沿既有公开流，原执行/控制身份不变。

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from ...agent.concurrency.interrupt import register_interruptible
from ...agent.conversation.control_commands import (
    conversation_request_interrupt_name,
    conversation_task_attributes,
)
from ...agent.conversation.learning_source import LEARNING_SOURCE_KEY, user_dialogue_projection
from ...agent.gateway_parts.response_renderer import (
    current_context_token_estimate,
    is_silent_user_stop,
)
from .gateway_client import (
    ChatRequestContent,
    GatewayChunkPollRequest,
    GatewayTimingContext,
    check_gateway_alive,
    format_gateway_timing,
    gateway_request_activity_paths,
    poll_gateway_chunks,
    submit_chat_request,
)
from .plain_state import _CHAT_RESPONSE_STYLE_INJECT, resume_context_override
from .plain_ui import _make_chunk_handler, _render_assistant_response
from .rendering import GRAY, RESET, _cprint

# 参数减量第 3 批：长输出折叠时保留的预览行数/字数不再是配置项。
_CHAT_COLLAPSE_PREVIEW_LINE_COUNT = 12
# 聊天长输出折叠保留预览 900 字符
_CHAT_COLLAPSE_PREVIEW_CHARS = 900


# LLM: direct local_run 必须是 worker 事先发布的原句柄；Gateway 不携带本地运行权威。
# 类用途: 汇总单条普通终端消息需要的依赖和运行绑定回调。
@dataclass
class PlainJobContext:
    job: object
    agent: object
    args: object
    paths: object
    assistant_outputs: list[str]
    build_history_context: Callable[[], str]
    current_session_id: str = ""
    local_run: object | None = None


# LLM: plain与TUI共用认证HTTP，原输入和消息ID只作运输事实；未知显式收口不重投，确认后原canonical终态负责正文。
# 函数用途: 提交一轮普通终端输入并显示原结果，未确认时保ID说明而不无限等待或宣称业务失败。
def _plain_gateway_handle(ctx: PlainJobContext) -> tuple[str, bool]:
    if not check_gateway_alive(ctx.paths):
        raise RuntimeError("gateway 已停止。请先执行 my-agent gateway start")
    started_at = time.perf_counter()
    on_chunk, stream_started_ref = _make_chunk_handler(
        ctx.agent.config.agent_name,
        _next_message_id(ctx),
        preview_chars=_CHAT_COLLAPSE_PREVIEW_CHARS,
    )
    request_id, chunk_path, _response_path = submit_chat_request(
        ctx.paths,
        content=ChatRequestContent(
            prompt=ctx.job.user,
            inject=_turn_inject(ctx, include_history=False),
            prompt_files=ctx.job.prompt_files,
            save=not ctx.args.no_save,
            show_prompt=ctx.job.show_prompt,
            resume_context=resume_context_override(ctx.args),
            chat_session_id=ctx.current_session_id,
            system_task=ctx.job.system_task,
            original_prompt=getattr(ctx.job, "original_prompt", None),
            client_message_id=str(getattr(ctx.job, "client_message_id", "") or getattr(ctx.job, "request_id", "")),
        ),
        agent=ctx.agent,
    )
    terminal_response_streamed_ref = [0]
    inactivity_timeout = _gateway_timeout_seconds(ctx)
    response = poll_gateway_chunks(
        GatewayChunkPollRequest(
            chunk_path,
            ctx.paths.terminal / f"{request_id}.json",
            time.time() + inactivity_timeout,
            on_chunk,
            [0],
            terminal_response_streamed_ref,
            activity_paths=gateway_request_activity_paths(
                ctx.paths,
                request_id,
                chunk_path,
            ),
            inactivity_timeout_seconds=inactivity_timeout,
        )
    )
    response_text = response.get("response", "")
    if not response and request_id:
        raise TimeoutError(f"gateway 请求等待超时: request_id={request_id}")
    if is_silent_user_stop(response):
        return "", False
    terminal_response_streamed = terminal_response_streamed_ref[0] > 0
    _render_if_needed(ctx, response_text, terminal_response_streamed)
    _print_gateway_timing(request_id, started_at, response, ctx)
    return response_text, terminal_response_streamed


# LLM: direct保留原句柄/绑定和单次历史注入；显示callback明确local候选未公开，真实response提交后才显示过程。
# 函数用途: 沿原agent调用模型及保存会话，完整终答走plain_ui公开门，候选不算已显示。
def _plain_local_handle(ctx: PlainJobContext) -> tuple[str, bool]:
    from ...agent.agent_core.runtime.loop_models import RunParams

    if ctx.local_run is None or ctx.local_run.request_id != ctx.job.request_id:
        raise ValueError("本地聊天缺少本轮控制句柄")
    started_at = time.perf_counter()
    on_chunk, stream_started_ref = _make_chunk_handler(
        ctx.agent.config.agent_name,
        _next_message_id(ctx),
        preview_chars=_CHAT_COLLAPSE_PREVIEW_CHARS,
        committed_model_only=True,
    )
    attrs = conversation_task_attributes(ctx.job.system_task)
    if not ctx.job.system_task:
        # plain direct 是用户对话入口：当前回合来源固定为用户对话，只标本轮不追认历史；系统任务回合不标。
        attrs = {**attrs, LEARNING_SOURCE_KEY: user_dialogue_projection()}
    with register_interruptible(conversation_request_interrupt_name(ctx.job.request_id)):
        ctx.local_run.check_admission()
        result = ctx.agent.run(
            ctx.job.user,
            params=RunParams(conversation_task_binding_callback=ctx.local_run),
            inject=_turn_inject(ctx, include_history=True),
            prompt_files=ctx.job.prompt_files,
            save=not ctx.args.no_save,
            request_id=ctx.job.request_id,
            source="chat",
            resume_context=resume_context_override(ctx.args),
            recovery_next_actions=["如需恢复本轮 chat，先用 memory-resume 搜索用户消息或时间范围。"],
            on_chunk=on_chunk,
            task_attributes=attrs,
        )
    agent_response_text = result.response
    if is_silent_user_stop(result):
        return "", False
    _render_if_needed(ctx, agent_response_text, stream_started_ref[0])
    _print_local_timing(result, started_at)
    return agent_response_text, stream_started_ref[0]


def _next_message_id(ctx: PlainJobContext) -> int:
    return len(ctx.assistant_outputs) + 1


# LLM: Gateway 路径的历史由 ConversationStore 唯一注入，只有 direct 路径可以附加本进程内存历史；style inject 两者共用。
# 函数用途: 为 plain chat 构造不重复会话历史的当前轮附加上下文。
def _turn_inject(
    ctx: PlainJobContext,
    *,
    include_history: bool,
) -> list[str]:
    history_ctx = ctx.build_history_context() if include_history else ""
    turn_inject = list(ctx.job.inject) + [_CHAT_RESPONSE_STYLE_INJECT]
    if history_ctx:
        turn_inject.append(history_ctx)
    return turn_inject


def _gateway_timeout_seconds(ctx: PlainJobContext) -> float:
    timeout = getattr(ctx.args, "gateway_timeout", None)
    if timeout is None:
        timeout = ctx.agent.config.gateway_request_timeout
    return max(0.0, float(timeout))


def _render_if_needed(
    ctx: PlainJobContext, response_text: str, stream_started: bool
) -> None:
    if not stream_started:
        from .plain_ui import AssistantResponseRenderRequest

        _render_assistant_response(
            AssistantResponseRenderRequest(
                text=response_text,
                assistant_outputs=ctx.assistant_outputs,
                agent_name=ctx.agent.config.agent_name,
                preview_lines=_CHAT_COLLAPSE_PREVIEW_LINE_COUNT,
                preview_chars=_CHAT_COLLAPSE_PREVIEW_CHARS,
            )
        )


def _print_gateway_timing(
    request_id: str,
    started_at: float,
    response: dict,
    ctx: PlainJobContext,
) -> None:
    elapsed = time.perf_counter() - started_at
    _cprint(
        f"{GRAY}{format_gateway_timing(GatewayTimingContext(request_id, elapsed, response, True))}{RESET}"
    )


def _print_local_timing(result, started_at: float) -> None:
    elapsed = time.perf_counter() - started_at
    _cprint(
        f"{GRAY}[耗时 {elapsed:.2f}s; 工具轮数 {result.tool_rounds}; "
        f"ctx_tokens~{current_context_token_estimate(result)}; "
        f"prompt_tokens~{result.prompt_token_estimate}; "
        f"resume_context={1 if result.memory_resume_context_injected else 0}]{RESET}"
    )


__all__ = [
    "PlainJobContext",
    "_plain_gateway_handle",
    "_plain_local_handle",
]
