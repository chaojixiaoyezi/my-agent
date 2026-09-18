# LLM: 本模块是 Gateway TUI 的轻量执行边界；导入时不得加载本地 SimpleAgent、模型后端、工具注册表或 Conversation runtime。
# 模块用途: 提交并轮询 Gateway 聊天请求，区分请求交付成功和模型回复完整；与本地 TUI 共用长度限制提示。

from __future__ import annotations

import time
from functools import partial
from typing import Any

from .tui_runtime import TuiTurnSummary


# LLM: 已有 gateway_request_id 表示持久提交成功，必须沿原终态信封等待，不能因重启瞬间 PID 不可见丢弃回执或重新提交。
# 未提交请求仍检查服务；超时/取消沿现有协议，不由进程活性猜模型任务的成败。
# 函数用途: 等待原 Gateway 请求的回复，让重启间隙已入队的消息继续接收结果，不误报停止后诱导用户重发。
def _worker_gateway_path(ctx: Any) -> tuple[str, bool, TuiTurnSummary]:
    from ...agent.gateway_parts.permission_bridge import write_gateway_permission_decision
    from .gateway_client import (
        GatewayChunkPollRequest,
        check_gateway_alive,
        gateway_request_activity_paths,
        poll_gateway_chunks,
    )

    if not str(getattr(ctx.job, "gateway_request_id", "") or "").strip() and not check_gateway_alive(ctx.cfg.paths):
        raise RuntimeError("gateway 已停止。请先执行 my-agent gateway start")
    request_id, chunk_path, terminal_path = _submit_gateway_job(ctx)
    _publish_gateway_request_id(ctx, request_id)
    timeout = _gateway_timeout(ctx.cfg)
    ctx.turn_adapter.configure_gateway_permission_sink(
        partial(write_gateway_permission_decision, chunk_path)
    )
    try:
        response = poll_gateway_chunks(
            GatewayChunkPollRequest(
                chunk_path,
                terminal_path,
                time.time() + max(0.0, timeout),
                ctx.turn_adapter,
                [0],
                [0],
                activity_paths=gateway_request_activity_paths(
                    ctx.cfg.paths,
                    request_id,
                    chunk_path,
                ),
                inactivity_timeout_seconds=max(0.0, timeout),
                on_event=ctx.turn_adapter.on_gateway_event,
            )
        )
    finally:
        ctx.turn_adapter.clear_gateway_permission_sink()
    if not response:
        raise TimeoutError(
            f"gateway 请求等待超时: request_id={request_id} terminal={terminal_path}"
        )
    return _gateway_outcome(ctx, response)


# LLM: submit uses the canonical Gateway writer and returns the exact terminal authority path;
# neither a local TUI job id nor the response projection may stand in for that identity.
# 函数用途: 把当前聊天任务提交到 Gateway，并返回真实请求、流和终态路径。
def _submit_gateway_job(ctx: Any):
    existing_request_id = str(getattr(ctx.job, "gateway_request_id", "") or "").strip()
    if existing_request_id:
        from ...agent.gateway_parts.paths import gateway_chunk_path

        return (
            existing_request_id,
            gateway_chunk_path(ctx.cfg.paths, existing_request_id),
            ctx.cfg.paths.terminal / f"{existing_request_id}.json",
        )
    return _submit_new_gateway_job(ctx.cfg, ctx.job, ctx.turn_inject)


# LLM: 入队前只登记显示去重 ID；运行/停止快照仍在 durable submit 成功后发布，不让未提交编号获得控制权。
# 函数用途: 提交新 TUI 请求，保持真实工作目录，并防止自己的正文经同会话观察流再显示一次。
def _submit_new_gateway_job(cfg: Any, job: Any, turn_inject: list[str]):
    from .gateway_client import ChatRequestContent, submit_chat_request

    request_id, chunk_path, _response_path = submit_chat_request(
        cfg.paths,
        content=ChatRequestContent(
            prompt=job.user,
            inject=turn_inject,
            prompt_files=job.prompt_files,
            save=job.save,
            show_prompt=job.show_prompt,
            resume_context=job.resume_context,
            chat_session_id=cfg.current_session_id,
            system_task=job.system_task,
            interactive_approvals=job.tool_approval,
            rich_transcript=job.rich_transcript,
        ),
        agent=cfg.agent,
        workspace_root=getattr(cfg.agent, "root", ""),
        workspace_roots=getattr(cfg.agent, "workspace_roots", None),
        on_request_allocated=getattr(getattr(cfg, "tui_runtime", None), "register_gateway_request", None),
    )
    job.gateway_request_id = request_id
    return request_id, chunk_path, cfg.paths.terminal / f"{request_id}.json"


# LLM: stop/control 必须指向 Gateway 返回的真实 request id；更新只发生在提交成功之后。
# 函数用途: 将运行快照中的请求 id 切换为 Gateway canonical id。
def _publish_gateway_request_id(ctx: Any, request_id: str) -> None:
    with ctx.cfg.state_lock:
        ctx.cfg.running_request_id_ref[0] = request_id


# LLM: timeout 只从显式 CLI override 或 agent config 读取，不由响应文本/活动文案调整。
# 函数用途: 返回本次 Gateway 不活跃等待上限。
def _gateway_timeout(cfg: Any) -> float:
    if cfg.args.gateway_timeout is not None:
        return float(cfg.args.gateway_timeout)
    return float(cfg.agent.config.gateway_request_timeout)


# LLM: Gateway typed provider error 保留已安全投影正文，技术错误另列；max-tokens 仍非完整回复，silent stop 不落正文。
# 函数用途: 把 Gateway response 转为统一终态；不把坏工具参数前的半句话当正常完成，也不丢掉该段正文。
def _gateway_outcome(ctx: Any, response: dict[str, Any]) -> tuple[str, bool, TuiTurnSummary]:
    from ...agent.gateway_parts.response_renderer import (
        current_context_token_estimate,
        is_silent_user_stop,
    )
    from ...agent.memory_archive.tokens import estimate_tokens

    if is_silent_user_stop(response):
        return "", False, TuiTurnSummary(ok=False, interrupted=True)
    if ctx.job.show_prompt and response.get("prompt"):
        ctx.cfg.tui_runtime.write_console(
            "===== FINAL PROMPT =====\n"
            + str(response.get("prompt") or "")
            + "\n===== RESPONSE ====="
        )
    if not response.get("ok"):
        from ...agent.gateway_parts.request_errors import (
            gateway_client_error_message,
            gateway_model_response_error_projection,
        )

        error = str(
            response.get("user_error")
            or gateway_client_error_message(response.get("error_code"))
        )
        text = str(response.get("response") or "") if gateway_model_response_error_projection(response) else ""
        return text, False, TuiTurnSummary(response_text=text, ok=False, error=error)
    text = str(response.get("response") or "")
    context_tokens = current_context_token_estimate(response)
    with ctx.cfg.state_lock:
        ctx.cfg.last_token_estimate_ref[0] = context_tokens
    error = _model_length_error(response)
    summary = TuiTurnSummary(
        response_text=text,
        ok=not error,
        error=error,
        context_tokens=context_tokens,
        output_tokens=estimate_tokens(text) if text else 0,
        tool_rounds=_nonnegative_int(response.get("tool_rounds")),
    )
    return text, False, summary


# LLM: Only host-owned turn_end/runtime fields select this display notice. No prose parsing,
# model calls, state writes, or automatic continuation; both TUI execution paths use this helper.
# 函数用途: 明确告诉用户模型这段回复受到长度限制，而不是无提示地停在半句话；不判断任务质量。
def _model_length_error(value: object) -> str:
    from ...agent.turn_end import result_turn_end_reason, turn_end_notice

    return turn_end_notice(result_turn_end_reason(value))


# LLM: 非负转换只用于显示统计，不能改变执行、重试或工具完成状态。
# 函数用途: 安全读取 token/tool round 数字字段。
def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


__all__ = ["_submit_new_gateway_job", "_worker_gateway_path"]
