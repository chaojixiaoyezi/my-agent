# LLM: 本地新提交统一认证HTTP，先同ID显示登记，原owner由C准备；已有附着job只观察不重投。
# LLM: 本模块是 Gateway TUI 的轻量执行边界；导入时不得加载本地 SimpleAgent、模型后端、工具注册表或 Conversation runtime。
# 模块用途: 提交并轮询 Gateway 聊天请求，区分请求交付成功和模型回复完整；与本地 TUI 共用长度限制提示。

from __future__ import annotations

import time
from functools import partial
from typing import TYPE_CHECKING, Any

from .tui_runtime import TuiTurnSummary

if TYPE_CHECKING:
    from .gateway_client import GatewayChatObserver


# LLM: 已有gateway_request_id只接原已确认输入的同ID观察；本次HTTP未确认在prepare抛错，不进入无限续等，也不重投。
# 未提交请求仍检查服务；观察超时不是任务终态，页面存活时保留同一请求与流游标，不重提业务。
# 函数用途: 持续等原请求的真实终态；长时间无活动只提示，退出页面及时停止观察，不取消服务端任务。
def _worker_gateway_path(
    ctx: Any, *, observer: GatewayChatObserver | None = None,
) -> tuple[str, bool, TuiTurnSummary]:
    from ...agent.gateway_parts.permission_bridge import write_gateway_permission_decision
    from .gateway_client import (
        GatewayChunkPollRequest,
        check_gateway_alive,
        gateway_request_activity_paths,
        network_gateway_observer,
        poll_gateway_chunks,
    )

    network = network_gateway_observer(ctx.cfg.agent)
    if network is None and not str(getattr(ctx.job, "gateway_request_id", "") or "").strip() and not check_gateway_alive(ctx.cfg.paths):
        raise RuntimeError("gateway 已停止。请先执行 my-agent gateway start")
    request_id, chunk_path, terminal_path = _submit_gateway_job(ctx)
    _publish_gateway_request_id(ctx, request_id)
    timeout = _gateway_timeout(ctx.cfg)
    ctx.turn_adapter.configure_gateway_permission_sink(
        partial(write_gateway_permission_decision, chunk_path) if network is None else
        partial(network.permission_sink, conversation_id=ctx.cfg.current_session_id)
    )
    try:
        # 宿主观察器仍受原审批 sink 与退出清理包围，不重投或取消服务端请求。
        observe = observer if observer is not None else network if network is not None else poll_gateway_chunks
        response = observe(
            GatewayChunkPollRequest(
                chunk_path,
                terminal_path,
                time.time() + max(0.0, timeout),
                ctx.turn_adapter,
                [0],
                [0],
                activity_paths=() if network is not None else gateway_request_activity_paths(
                    ctx.cfg.paths,
                    request_id,
                    chunk_path,
                ),
                inactivity_timeout_seconds=max(0.0, timeout),
                on_event=ctx.turn_adapter.on_gateway_event,
                on_wait_timeout=partial(
                    ctx.cfg.tui_runtime.set_notice,
                    "暂未收到最终结果，继续等待原请求；无需重新发送。",
                    duration_seconds=3.0,
                    notice_kind="gateway_wait_timeout",
                ),
                is_wait_cancelled=ctx.cfg.stop_event.is_set,
            )
        )
    finally:
        ctx.turn_adapter.clear_gateway_permission_sink()
        ctx.cfg.tui_runtime.clear_notice(expected_kind="gateway_wait_timeout")
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


# LLM: POST前只登记显示去重ID；只在原同ID排队回执确认后保存job绑定，未确认抛错交原finally清理本地等待。
# 函数用途: 提交含附件 refs 的 TUI 请求，保持真实工作目录，并防止自己的正文经同会话观察流再显示一次。
# LLM: 新提交透传原输入和既有client_message_id/request_id，同canonical ID在HTTP effect前登记；不自填宿主owner或未发布expected_turn_id。
def _submit_new_gateway_job(cfg: Any, job: Any, turn_inject: list[str]):
    from .gateway_client import ChatRequestContent, submit_chat_request

    request_id, chunk_path, _response_path = submit_chat_request(
        cfg.paths,
        content=ChatRequestContent(
            prompt=job.user,
            inject=turn_inject,
            prompt_files=job.prompt_files,
            input_media=job.input_media,
            save=job.save,
            show_prompt=job.show_prompt,
            resume_context=job.resume_context,
            chat_session_id=cfg.current_session_id,
            system_task=job.system_task,
            original_prompt=getattr(job, "original_prompt", None),
            client_message_id=str(getattr(job, "client_message_id", "") or getattr(job, "request_id", "")),
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


# LLM: stop/control只携带同canonical公式的请求ID作目标提示；执行权限仍由原宿主门核验，已知ID不证明已经受理。
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


# LLM: Gateway错误保留安全正文且技术错误另列；prompt沿精确adapter围栏，silent stop不落正文。
# 函数用途: 把 Gateway response 转为统一终态；不把坏工具参数前的半句话当正常完成，也不丢掉该段正文。
def _gateway_outcome(ctx: Any, response: dict[str, Any]) -> tuple[str, bool, TuiTurnSummary]:
    from ...agent.gateway_parts.response_renderer import (
        current_context_token_estimate,
        is_silent_user_stop,
    )
    from ...agent.memory_archive.tokens import estimate_tokens

    # 接回的排队请求没有本地接受时钟，完成计时须保留 Gateway 的结构化时间。
    gateway_timing = {
        key: response[key]
        for key in ("duration_seconds", "started_at", "ended_at")
        if key in response and response[key] is not None
    } or None
    if is_silent_user_stop(response):
        return "", False, TuiTurnSummary(ok=False, interrupted=True, gateway_timing=gateway_timing)
    if ctx.job.show_prompt and response.get("prompt"):
        with ctx.cfg.tui_runtime.turn_console_output(getattr(ctx, "turn_adapter", None)):
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
        if not response.get("user_error") and error == gateway_client_error_message(None):
            error += _public_failure_diagnostics(response)
        text = str(response.get("response") or "") if gateway_model_response_error_projection(response) else ""
        return text, False, TuiTurnSummary(response_text=text, ok=False, error=error, gateway_timing=gateway_timing)
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
        gateway_timing=gateway_timing,
    )
    return text, False, summary


# LLM: 只消费公开标量；字段独立校验，不泄露raw error、不让控制字符或巨量数字撑坏终端。
# 函数用途: 为通用失败提示生成有界错误码、请求编号、耗时和尝试次数后缀。
def _public_failure_diagnostics(response: dict[str, Any]) -> str:
    import math

    parts = []
    for field, label in (("error_code", "错误码"), ("request_id", "请求")):
        value = response.get(field)
        if isinstance(value, str) and value.isprintable() and value.strip():
            parts.append(f"{label} {value.strip()[:120]}")
    duration = response.get("duration_seconds")
    if type(duration) in (int, float):
        try:
            seconds = float(duration)
        except (ValueError, OverflowError):
            seconds = -1.0
        if math.isfinite(seconds) and seconds >= 0:
            formatted = f"{seconds:.1f}"
            if len(formatted) <= 32:
                parts.append(f"用时 {formatted}s")
    attempts = response.get("attempts")
    if type(attempts) is int and attempts >= 0 and attempts.bit_length() <= 64:
        parts.append(f"尝试 {attempts} 次")
    return "（" + " · ".join(parts) + "）" if parts else ""


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
