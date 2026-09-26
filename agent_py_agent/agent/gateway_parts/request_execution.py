# LLM: 这里只编排单个已领取请求：身份先绑定再领取车道，随后准备上下文、执行、提交历史与响应。
# 上下文、绑定、历史、输入渲染分别归 request_context/request_binding/request_history/request_prompt；
# 修改须联测停止、Compact、repair 与重启恢复，保持原锁、提交顺序和 typed 错误，不另建兼容执行链。
# 同turn展示及已评估事实仅由本请求局部回调持有，失效清除后本turn不再推荐，不进恢复账或结果。
# 延迟transcript Compact沿同次恢复准备提交；原CAS后的上下文必须回传至下一溢出或最终持久化。
# typed overflow携带原生IR与精确插话ID，下一轮重建权限/前缀，不能用归档preview替代原完整工具回执。
# 决策实验对照记录由能力观察出口拆出写入请求记录；回合正常收尾后才补写实际工具用量并在 apply 授权内检查晋升。
# 模块用途: 协调 Gateway 一轮请求的租约、模型执行、超窗恢复和收尾，复用各组件的唯一事实源。
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from ..agent_core.runtime_mixin import RunParams
from ..command_catalog import system_slash_command_name
from ..concurrency.interrupt import is_interrupted
from ..conversation.audit_lifecycle import (
    AuditLifecycleError,
    audit_scope_payload,
    prepare_named_audit,
    project_audit_runtime_attributes,
)
from ..conversation.authority import (
    CONVERSATION_AUDIT_PREPARE_ATTR,
    CONVERSATION_EXECUTION_CWD_ATTR,
    CONVERSATION_RUNTIME_WORKSPACE_ROOTS_ATTR,
    CONVERSATION_TASK_TURN_ACTIVE_ATTR,
    CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR,
    CONVERSATION_WORKSPACE_EXECUTION_RUNNING_ATTR,
    CONVERSATION_WORKSPACE_EXECUTION_STATE_AVAILABLE_ATTR,
    CONVERSATION_WORKSPACE_TASK_ID_ATTR,
    CONVERSATION_WORKSPACE_TASK_STATUS_ATTR,
)
from ..conversation.compact_carry import compact_overflow_carry
from ..conversation.control_commands import conversation_task_attributes
from ..gateway_compact_context import build_gateway_compact_load_request
from ..tooling.operation_verification import public_operation_verification
from . import (
    request_binding,
    request_context,
    request_experiment_records,
    request_history,
    request_prompt,
)
from .approval_session import (
    agent_tool_approval_session_cache,
    tool_approval_session_scope,
)
from .audit_service import (
    AuditRequestCompletedParams,
    audit_request_completed,
    audit_request_processing,
)
from .io import (
    gateway_response_path,
    read_json_file_report,
)
from .lease_service import refresh_processing_lease, start_lease_heartbeat
from .paths import (
    claimed_request_chunk_path,
    gateway_paths,
)
from .recovery import _gateway_request_attempts
from .request_errors import (
    ActiveTurnOutcomeUncertainError,
    ConversationPersistenceError,
    SystemCommandRoutingError,
    gateway_model_response_error_projection,
    gateway_request_load_error_response,
)
from .stream_writer import BufferedChunkStreamWriter, open_chunk_stream

if TYPE_CHECKING:
    from ...core import SimpleAgent

_EMPTY_PROMPT_MESSAGE = "gateway ask prompt/goal cannot be empty"
logger = logging.getLogger(__name__)


# LLM: 初始响应只携带队列事实；成功、结束时间和模型用量必须由后续实际结果填入。
# 类用途: 固定响应创建所需的请求身份和起始时间，不授予执行权。
@dataclass(frozen=True)
class _GatewayResponseBaseContext:
    request: dict
    request_path: Path
    request_id: str
    kind: str
    started_at: float


# LLM: 阶段计时只记请求准备/执行的关键间隔（毫秒），用于定位每轮固定开销；它不参与任何
# 业务判定，失败时静默跳过，避免测量代码反噬执行路径。
# 函数用途: 把「自某起点以来的耗时」写入可选的请求阶段字典。
def _record_gateway_stage(
    stages: dict[str, float] | None,
    key: str,
    started_mono: float,
) -> None:
    if stages is None:
        return
    stages[key] = round((time.monotonic() - started_mono) * 1000, 1)


# LLM: 同一请求跨 Compact 重试继续使用当前上下文与完整归档；本包不拥有独立状态。
# 类用途: 将本轮已准备的历史和溢出携带数据交给运行参数构造器。
@dataclass(frozen=True)
class _GatewayRunParamsRequest:
    request: dict
    context: request_context.GatewayAskRunContext
    conversation: request_context.GatewayConversationContext
    prompt: str
    carried_archive_tool_calls: tuple[dict[str, object], ...] = ()
    carried_active_turn_user_inputs: tuple[dict[str, object], ...] = ()


# LLM: 请求租约与会话执行车道是不同边界；只能按本包的精确 request/worker/attempt 刷新队列租约。
# 类用途: 汇集启动请求心跳所需的宿主与队列记录。
@dataclass(frozen=True)
class _GatewayLeaseStartContext:
    agent: SimpleAgent
    request: dict
    request_path: Path
    request_id: str
    refresh_lease: bool
    worker_id: str


# LLM: 响应默认失败，后续执行结果才能改终态；字段协议需与客户端和恢复读取联测。
# 函数用途: 创建尚未执行的响应容器，保留原请求和租约事实。
def _build_gateway_response_base(context: _GatewayResponseBaseContext) -> dict:
    request = context.request
    return {
        "id": context.request_id,
        "kind": context.kind or "unknown",
        "ok": False,
        "status": "failed",
        "created_at": request.get("created_at", 0),
        "started_at": context.started_at,
        "ended_at": 0,
        "duration_seconds": 0,
        "response": "",
        "error_code": "",
        "error": "",
        "backend": "",
        "used_memories": 0,
        "tool_rounds": 0,
        "prompt": "",
        "request_file": str(context.request_path),
        "attempts": _gateway_request_attempts(request),
        "lease_owner": request.get("lease_owner", ""),
        "lease_started_at": request.get("lease_started_at", 0),
        "lease_heartbeat_at": request.get("lease_heartbeat_at", 0),
    }


# LLM: Gateway 对外 response 只放 user-facing projection；模型 token 只转发结构化账本，供应商错误和空正文截断保持 typed 终态。
# 函数用途: 整理最终响应；模型未完整返回时结束本轮等待并给出准确原因，不改变已有工作或自动重跑。
def _update_response_from_result(response: dict, result, request: dict) -> None:
    channel_delivery = dict(getattr(result, "channel_delivery", {}) or {})
    public_delivery = _public_channel_delivery(channel_delivery)
    # An empty projected body is authoritative: it means the channel boundary
    # intentionally suppressed an internal/runtime payload.  Falling back to
    # the raw model response here would undo that safety decision.
    public_response = (
        str(channel_delivery.get("content") or "")
        if channel_delivery
        else str(result.response or "")
    )
    response.update(
        {
            "ok": True,
            "status": "done",
            "response": public_response,
            "backend": result.backend,
            "used_memories": result.used_memories,
            "tool_rounds": result.tool_rounds,
            "prompt": result.prompt if request.get("include_prompt") else "",
            "current_context_token_estimate": result.prompt_token_estimate,
            "prompt_token_estimate": result.prompt_token_estimate,
            "runtime_injection_token_estimate": result.runtime_injection_token_estimate,
            "turn_token_estimate": result.turn_token_estimate,
            "cumulative_token_estimate": result.cumulative_token_estimate,
            "logical_model_turn_count": int(getattr(result, "logical_model_turn_count", 0) or 0),
            "physical_model_attempt_count": int(
                getattr(result, "physical_model_attempt_count", 0) or 0
            ),
            "model_retry_count": int(getattr(result, "model_retry_count", 0) or 0),
            "provider_http_attempt_count": int(
                getattr(result, "provider_http_attempt_count", 0) or 0
            ),
            "provider_http_retry_count": int(getattr(result, "provider_http_retry_count", 0) or 0),
            "model_call_status_counts": dict(
                getattr(result, "model_call_status_counts", None) or {}
            ),
            "model_accounted_input_tokens": int(
                getattr(result, "model_accounted_input_tokens", 0) or 0
            ),
            "model_output_tokens": int(getattr(result, "model_output_tokens", 0) or 0),
            "model_total_tokens": int(getattr(result, "model_total_tokens", 0) or 0),
            "model_cached_input_tokens": int(getattr(result, "model_cached_input_tokens", 0) or 0),
            "model_cache_creation_input_tokens": int(
                getattr(result, "model_cache_creation_input_tokens", 0) or 0
            ),
            "model_provider_usage_call_count": int(
                getattr(result, "model_provider_usage_call_count", 0) or 0
            ),
            "model_estimated_usage_call_count": int(
                getattr(result, "model_estimated_usage_call_count", 0) or 0
            ),
            # LLM: 这是累计模型账本的冻结分栏投影；不从 Context 行或响应正文重新估算。
            "model_usage_breakdown": dict(getattr(result, "model_usage_breakdown", None) or {}),
            "memory_resume_context_injected": result.memory_resume_context_injected,
            "memory_resume_context_query": result.memory_resume_context_query,
            "memory_resume_context_matches": result.memory_resume_context_matches,
            "memory_resume_context_token_estimate": result.memory_resume_context_token_estimate,
            "memory_resume_context_error": result.memory_resume_context_error,
            "runtime_status": str(getattr(result, "runtime_status", "ok") or "ok"),
            "runtime_reason": str(getattr(result, "runtime_reason", "") or ""),
            "runtime_source": str(getattr(result, "runtime_source", "") or ""),
            "turn_end_reason": str(getattr(result, "turn_end_reason", "") or ""),
            "conversation_persist_degraded": bool(
                getattr(result, "conversation_persist_degraded", False)
            ),
            "conversation_persist_error": str(
                getattr(result, "conversation_persist_error", "") or ""
            ),
            "channel_delivery": public_delivery,
        }
    )
    response.update(gateway_model_response_error_projection(result))


# LLM: Gateway 客户端不需要服务器 path；跨轮复用引用只进 owner transcript metadata，不进公开响应。
# 函数用途: 生成不含绝对路径和验收细节的通道交付响应字段。
def _public_channel_delivery(value: dict[str, object]) -> dict[str, object]:
    artifacts = value.get("artifacts")
    names: list[str] = []
    if isinstance(artifacts, list):
        names = [
            str(item.get("name") or "")
            for item in artifacts
            if isinstance(item, dict) and str(item.get("name") or "")
        ]
    public = {
        "content": str(value.get("content") or ""),
        "artifact_names": names,
        "internal_signal": value.get("internal_signal") is True,
        "projection_status": str(value.get("projection_status") or "plain_text"),
    }
    verification = value.get("operation_verification")
    if isinstance(verification, dict):
        public["operation_verification"] = public_operation_verification(verification)
    return public


# LLM: 心跳只能刷新当前 transport attempt 与 lease epoch；不能覆盖新执行者，结束必须配对停止线程。
# 函数用途: 按请求状态刷新持久租约并启动心跳，未领取的普通调用不新建心跳。
def _start_gateway_request_lease(
    context: _GatewayLeaseStartContext,
) -> tuple[threading.Event | None, threading.Thread | None]:
    should_refresh = (
        context.refresh_lease or str(context.request.get("status") or "") == "processing"
    )
    if not should_refresh:
        return None, None
    lease_worker = context.worker_id or str(context.request.get("lease_owner") or "")
    execution_attempt_id = str(context.request.get("execution_attempt_id") or "").strip()
    try:
        lease_epoch = max(0, int(context.request.get("lease_epoch") or 0))
    except (TypeError, ValueError):
        lease_epoch = 0
    refresh_processing_lease(
        context.request_path,
        request_id=context.request_id,
        worker_id=lease_worker,
        execution_attempt_id=execution_attempt_id,
        lease_epoch=lease_epoch,
    )
    return start_lease_heartbeat(
        context.agent,
        context.request_path,
        request_id=context.request_id,
        worker_id=lease_worker,
        execution_attempt_id=execution_attempt_id,
        lease_epoch=lease_epoch,
    )


# LLM: 原模型仍在排队前冻结；只捕获此次已有 owner 设置读取，退出清理。观察不能移动冻结点或改有效模型。
# 函数用途: 执行 Gateway 对话并固定本会话模型，关闭观察不增加配置读盘或改变出站输入。
def _run_gateway_ask(context: request_context.GatewayAskRunContext):
    from ..settings.model_profiles import capture_selected_model_read
    from ..settings.model_scope import selected_model_scope

    prompt = str(context.request.get("prompt") or context.request.get("goal") or "").strip()
    preflight = request_context.preflight_gateway_conversation(request_context.GatewayConversationLoadRequest(
        context.agent, context.request, context.request_id, prompt, context.on_chunk,
    ))
    _require_gateway_conversation_ready(context.request, preflight)
    with capture_selected_model_read() as captured, selected_model_scope(context.agent, thread_id=preflight.thread_id):
        return _run_gateway_ask_with_model(context, captured=captured[0] if captured else None)


# LLM: 原模型快照覆盖排队；首次只加载Compact来源，完整准备后先压缩再选模，发送拒绝沿新上下文回退；重复执行不重新决策。
# 没有会话来源（未绑定 thread 的请求）时不安装恢复宿主，与 overflow 入口的同一判定保持一致。
# 函数用途: 取得准确执行权后冻结来源与可选建议，在原生成安全点完成自动压缩和选模。
def _run_gateway_ask_with_model(context: request_context.GatewayAskRunContext, *, captured=None):
    from ..gateway_model_observation import GatewayModelObservation

    request = context.request
    prompt = str(request.get("prompt") or request.get("goal") or "").strip()
    if not prompt:
        raise ValueError(_EMPTY_PROMPT_MESSAGE)
    load_request = request_context.GatewayConversationLoadRequest(
        context.agent,
        request,
        context.request_id,
        prompt,
        context.on_chunk,
    )
    preflight = request_context.preflight_gateway_conversation(load_request)
    _require_gateway_conversation_ready(request, preflight)
    with request_binding.gateway_conversation_execution_lane(
        context,
        preflight.thread_id,
    ) as claim:
        # Reserve the thread before reading compact/history/task state.  A turn
        # queued behind another turn must see that prior turn's final transcript,
        # not the stale snapshot from the time it entered the Gateway.
        _conversation_prep_started = time.monotonic()
        observer = GatewayModelObservation(context, captured, claim)
        with observer.scope():
            try:
                conversation = request_context.gateway_conversation_context(replace(load_request, on_thread_loaded=observer, defer_compact=True))
                _record_gateway_stage(context.stages, "conversation_prep_ms", _conversation_prep_started)
                _require_gateway_conversation_ready(request, conversation)
                from ..gateway_compact_recovery import prepare_gateway_compact_recovery

                observer.compact_recovery = (
                    prepare_gateway_compact_recovery(context, conversation, force=False)
                    if conversation.compact_source is not None else None
                )
                _configure_gateway_main_activity(context, conversation)
                if conversation.compact_generation > preflight.compact_generation:
                    _publish_gateway_compact_boundary(context.on_chunk, conversation.compact_generation)
                return _execute_gateway_conversation_turn(context, prompt, conversation, observer=observer)
            except BaseException:
                observer.settle_preparation_failure()
                raise


# LLM: 只在取得会话执行车道后调用，绑定宿主解析的 owner/thread/task，普通客户端不扩展公开范围。
# 函数用途: 将富 TUI 前台接到同会话标量与公开过程流，后续任务晋升仍读原请求的结构化绑定。
def _configure_gateway_main_activity(context: request_context.GatewayAskRunContext, conversation: request_context.GatewayConversationContext) -> None:
    writer = context.on_chunk
    if not isinstance(writer, BufferedChunkStreamWriter) or not writer.rich_transcript or not conversation.thread_id:
        return
    from .foreground_transcript import GatewayForegroundTranscriptSink
    from .main_activity import GatewayMainActivitySink

    writer.main_activity_sink = GatewayMainActivitySink(
        context.agent, thread_id=conversation.thread_id, request_id=context.request_id, request=context.request,
        task_id=str(getattr(conversation.workspace_task, "task_id", "") or ""),
    )
    writer.transcript_sink = GatewayForegroundTranscriptSink(
        context.agent, thread_id=conversation.thread_id, request_id=context.request_id, request=context.request,
        task_id=str(getattr(conversation.workspace_task, "task_id", "") or ""),
    )


# LLM: 仅在车道内运行；observer只沿内部调用传入，恢复上下文须回到最终持久化；公共命令判据先于用户历史和模型。
#   模型回合正常返回后先做决策实验收尾（补写实际工具用量、apply 授权内晋升检查），再持久化答复；收尾不改变结果。
# 函数用途: 配置流和审批并执行会话；明确系统命令及历史写入失败均阻止模型副作用。
def _execute_gateway_conversation_turn(
    context: request_context.GatewayAskRunContext,
    prompt: str,
    conversation: request_context.GatewayConversationContext,
    *, observer=None,
):
    request = context.request
    _set_gateway_identifier_redactions(
        context.on_chunk,
        request_history.gateway_identifier_redactions(context, conversation),
    )
    _set_gateway_verbose_level(context.on_chunk, conversation.verbose_level)
    _configure_gateway_approval_session(context, conversation)
    if system_slash_command_name(prompt):
        raise SystemCommandRoutingError("系统命令必须在控制入口处理，不能进入模型执行队列")
    _register_named_system_task(context, conversation, prompt)
    if not request_history.append_gateway_conversation_message(
        context.agent,
        request,
        conversation,
        request_id=context.request_id,
        role="user",
        content=prompt,
    ):
        raise ConversationPersistenceError("当前消息无法可靠写入会话记录，请稍后重试")
    result, conversation = _run_gateway_turn_with_conversation_compact(
        context,
        prompt,
        conversation,
        observer=observer,
    )
    # 实验收尾只在回合正常返回后执行；普通请求零 I/O，停止/失败不补写，任何异常都不改变本轮结果。
    request_experiment_records.finish_decision_experiment_turn(context, result)
    return request_history.persist_gateway_assistant_result(context, conversation, result)


# LLM: Approval scope must match the same canonical execution cwd passed to model/tool execution.
# Missing owner, thread, or cwd disables reuse; the mode provider is bound to the same authenticated owner, never model arguments.
# 函数用途: 在模型开始前绑定“本会话允许”的生命周期和本用户自主模式，支持当前精确审批原地续跑。
def _configure_gateway_approval_session(
    context: request_context.GatewayAskRunContext,
    conversation: request_context.GatewayConversationContext,
) -> None:
    from functools import partial

    from ..user_space.approval_mode import autonomous_tool_decision

    if isinstance(context.on_chunk, BufferedChunkStreamWriter):
        context.on_chunk.approval_mode_decision_provider = partial(autonomous_tool_decision, context.agent)
    configure = getattr(context.on_chunk, "configure_approval_session", None)
    if not callable(configure):
        return
    scope = conversation.scope
    owner_id = str(getattr(scope, "owner_id", "") or "").strip()
    conversation_id = str(getattr(scope, "channel_conversation_id", "") or "").strip()
    execution_cwd = (
        str(conversation.workspace_task.task_path or "").strip()
        if conversation.workspace_task is not None
        else str(conversation.cwd or "").strip()
    )
    config = getattr(context.agent, "config", None)
    if not owner_id or not conversation.thread_id or not execution_cwd:
        return

    # LLM: Thread-local run workspace changes only through canonical task promotion; resolving
    # here avoids freezing the pre-promotion owner home into the whole session approval key.
    # 函数用途: 每次审批时用当前工具实际工作的任务目录生成精确作用域。
    def current_scope() -> str:
        return tool_approval_session_scope(
            owner_id=owner_id,
            thread_id=conversation.thread_id,
            conversation_id=conversation_id,
            cwd=_gateway_approval_runtime_cwd(context.agent, execution_cwd),
            access_mode=getattr(config, "access_mode", ""),
            path_access_mode=getattr(config, "path_access_mode", ""),
        )

    from ..user_space.operation_grants import record_owner_operation_grant

    # 函数用途: 用户在面板选"长期允许"后，把这类操作记进本用户策略文件（唯一权威），下次同类调用不再询问。
    def remember_grant(grant_key: str) -> None:
        record_owner_operation_grant(context.agent.home_paths, grant_key, source="gateway_stream_approval")

    configure(
        agent_tool_approval_session_cache(context.agent),
        current_scope,
        remember_grant,
    )


# LLM: Tool execution and approval must observe the same thread-local promoted workspace. The
# immutable run attributes are a secondary source; the resolved conversation cwd is fallback only.
# 函数用途: 取得当前工具调用真正使用的任务目录，解决首轮建任务后审批作用域前后不一致。
def _gateway_approval_runtime_cwd(agent: object, fallback: str) -> str:
    current_workspace = str(getattr(agent, "_current_run_task_workspace", "") or "").strip()
    if current_workspace:
        return current_workspace
    params = getattr(agent, "_current_run_params", None)
    attributes = getattr(params, "task_attributes", None)
    if isinstance(attributes, dict):
        runtime_cwd = str(attributes.get(CONVERSATION_EXECUTION_CWD_ATTR) or "").strip()
        if runtime_cwd:
            return runtime_cwd
    return str(fallback or "").strip()


# LLM: 只消费 ingress 已校验的 system_task，模型副作用前持久绑定精确准备任务；聊天文本不能启动审计。
# 函数用途: 为命名工作的准备轮登记 canonical task 并同步当前队列记录。
def _register_named_system_task(
    context: request_context.GatewayAskRunContext,
    conversation: request_context.GatewayConversationContext,
    prompt: str,
) -> None:
    """Reserve one named Audit prepare turn before model side effects."""
    context.request.pop("conversation_audit_scope", None)
    attributes = conversation_task_attributes(context.request.get("system_task"))
    work_kind = str(attributes.get("conversation_work_kind") or "").strip()
    work_name = str(attributes.get("conversation_work_name") or "").strip()
    if work_kind not in {"audit"} or not work_name:
        return
    if attributes.get(CONVERSATION_AUDIT_PREPARE_ATTR) is not True:
        raise SystemCommandRoutingError("Audit 启动必须在控制入口处理，不能进入模型执行队列")
    store = getattr(context.agent, "conversation_store", None)
    if store is None or not conversation.thread_id:
        raise ConversationPersistenceError("命名任务当前无法登记，请稍后重试")
    try:
        link = prepare_named_audit(
            context.agent,
            store,
            thread_id=conversation.thread_id,
            work_name=work_name,
            prompt=prompt,
            prepare_request_id=context.request_id,
        )
    except AuditLifecycleError as exc:
        raise ConversationPersistenceError(str(exc)) from exc
    context.request["conversation_audit_scope"] = audit_scope_payload(link)
    request_binding.persist_gateway_request_task_binding(
        context.request_path,
        context.request_id,
        thread_id=conversation.thread_id,
        task_id=link.task_id,
        task_path=str(getattr(link, "task_path", "") or ""),
    )


# LLM: One Gateway request may cross several provider slices, but every overflow must advance the
# same canonical Compact generation before retry. Full carried archives remain effect authority;
# the same active request retains exact host rejection memory without inheriting approval grants.
# 与后台共用携带 reducer；真实工具循环已按原身份释放 mailbox，这里只按释放ID排除插话并携带原IR。
# 展示callback同步更新本turn局部值及当前宿主参数；新请求重置，失效None与已评估事实阻止额外决策。
# 能力推荐的结构化观测另走 observer，只追加到本请求记录，不改展示回调的语义；实验对照记录由同一出口拆出另写。
# 有宿主时transcript延迟到完整恢复输入就绪后提交；成功材料从同次run回传，后续overflow/最终持久化不复活旧上下文。
# 函数用途: 在同一用户回合内处理上下文超限，正式压缩旧会话或本轮工具历史后继续执行。
def _run_gateway_turn_with_conversation_compact(
    context: request_context.GatewayAskRunContext,
    prompt: str,
    conversation: request_context.GatewayConversationContext,
    *, observer=None,
) -> tuple[object, request_context.GatewayConversationContext]:
    """Compact the authoritative thread inline and retry the same user turn."""
    request = context.request
    current = conversation
    carried_archive_tool_calls = _gateway_recovered_active_turn_tool_calls(context)
    _recover_gateway_active_turn_authority(context, carried_archive_tool_calls)
    carried_active_turn_user_inputs: list[dict[str, object]] = []
    native_compact_carry = None
    runtime_rejected_actions: list[dict[str, str]] = []
    capability_presentation = None
    presentation_evaluated = False

    # LLM: 回调只由原运行/Compact同步调用；不持久化或改活快照，清旧值后仍保留本turn已评估事实。
    # 函数用途: 在当前请求内保存真实采用结果，让压缩和后续重试使用同一展示或基础面。
    def retain_presentation(value) -> None:
        nonlocal capability_presentation, presentation_evaluated
        capability_presentation, presentation_evaluated = value, True
        run_params.capability_presentation = value
        run_params.capability_presentation_evaluated = True

    for _attempt in range(8):
        run_params = _gateway_run_params(
            _GatewayRunParamsRequest(
                request,
                context,
                current,
                prompt,
                tuple(carried_archive_tool_calls),
                tuple(carried_active_turn_user_inputs),
            )
        )
        run_params.runtime_rejected_actions = runtime_rejected_actions
        run_params.native_compact_carry = native_compact_carry
        run_params.conversation_turn_id = context.request_id
        run_params.capability_presentation = capability_presentation
        run_params.capability_presentation_evaluated = presentation_evaluated
        run_params.capability_presentation_turn_id = str(request.get("execution_attempt_id") or context.request_id)
        run_params.capability_presentation_callback = retain_presentation
        run_params.capability_presentation_observer = partial(
            request_experiment_records.observe_capability_presentation, context)
        _run_started = time.monotonic()
        result = context.agent.run(
            prompt,
            params=run_params,
        )
        if observer is not None and observer.compact_recovery is not None and observer.compact_recovery.committed:
            current = observer.compact_recovery.host_state
        _record_gateway_stage(context.stages, "run_ms", _run_started)
        if str(getattr(result, "runtime_status", "") or "").strip().lower() != "context_overflow":
            return result, current
        from ..conversation.compact_carry import native_compact_carry_from_result

        native_compact_carry = native_compact_carry_from_result(result)
        released_input_ids = native_compact_carry.released_input_ids if native_compact_carry is not None else ()
        run_params.native_compact_carry = native_compact_carry
        carried_archive_tool_calls, carried_active_turn_user_inputs = compact_overflow_carry(
            carried_archive_tool_calls=carried_archive_tool_calls,
            carried_active_turn_user_inputs=carried_active_turn_user_inputs,
            result_archive_tool_calls=getattr(result, "archive_tool_calls", None),
            result_active_turn_user_inputs=getattr(result, "active_turn_user_inputs", None),
            released_input_ids=released_input_ids,
        )
        current = _gateway_compact_overflowing_turn(
            context,
            prompt,
            current,
            run_params,
            carried_archive_tool_calls,
            observer=observer,
        )
    raise ConversationPersistenceError("当前会话压缩后仍超过模型上下文上限")


# LLM: 有宿主时transcript或活动工具都只刷新来源，下一真实render/select才计量和CAS；无宿主不能粗估压活动归档。
# 每次overflow清前一恢复载体，不借持久结果重建展示选择；唯一产品调用方始终提供observer。
# 函数用途: 给同轮下一模型请求安装完整恢复，缺少可压来源时明确停止而不账外重试。
def _gateway_compact_overflowing_turn(
    context: request_context.GatewayAskRunContext,
    prompt: str,
    current: request_context.GatewayConversationContext,
    run_params: RunParams,
    carried_archive_tool_calls: list[dict[str, object]],
    *, observer=None,
) -> request_context.GatewayConversationContext:
    request = context.request
    if observer is not None:
        observer.compact_recovery = None
    load = build_gateway_compact_load_request(context, prompt, run_params, carried_archive_tool_calls)
    refreshed = request_context.gateway_conversation_context(
        replace(load, defer_compact=observer is not None), force_compact=observer is None,
    )
    _require_gateway_conversation_ready(request, refreshed)
    if (observer is not None and refreshed.compact_source is not None
            and (refreshed.compact_source.messages or carried_archive_tool_calls or run_params.native_compact_carry is not None)):
        from ..gateway_compact_recovery import prepare_gateway_compact_recovery

        observer.compact_recovery = prepare_gateway_compact_recovery(context, refreshed)
        return refreshed
    if refreshed.compact_generation > current.compact_generation:
        _publish_gateway_compact_boundary(context.on_chunk, refreshed.compact_generation)
        return refreshed
    raise ConversationPersistenceError("当前会话无法继续压缩，请稍后重试")


# LLM: A reclaimed Gateway request is the same active turn, not a new turn. Restore only exact
# owner-local rows carrying its structured conversation_request_id, and resolve that owner root
# through the shared low-layer user_space helper rather than importing agent-core. Never rebuild
# progress from assistant prose, scan child workspaces, or silently replay a corrupt index.
# 函数用途: Gateway 崩溃重排后恢复本轮已执行工具，避免模型重建 Todo、重复派工或重做写操作。
def _gateway_recovered_active_turn_tool_calls(
    context: request_context.GatewayAskRunContext,
) -> list[dict[str, object]]:
    if not request_binding.gateway_request_is_active_turn_recovery(context.request, context.request_id):
        return []
    from ..memory_archive.compact_tool_output_refs import carried_tool_call_records
    from ..user_space.runtime_paths import runtime_owner_root

    try:
        return carried_tool_call_records(
            runtime_owner_root(context.agent),
            {"conversation_request_id": context.request_id},
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise ConversationPersistenceError(
            "当前回合的工具历史无法可靠恢复，已停止自动重放，请稍后重试"
        ) from exc


# LLM: The transport marker only identifies the reclaimed request. RuntimeDB independently proves
# exact task+run ownership and complete terminal operation records before releasing UNKNOWN. Keep
# this bridge before agent.run so generic authority binding remains fail-closed for every other case.
#   New requests carry actual DB identity, distinct from the conversation display binding;
# RuntimeDB also reconciles a provably dead current runner for this exact owner/run only.
# 函数用途: 按真实执行绑定核对原回合工具与进程死亡；结果未确认时返回专用错误，不猜身份或放宽 UNKNOWN。
def _recover_gateway_active_turn_authority(
    context: request_context.GatewayAskRunContext,
    carried_archive_tool_calls: list[dict[str, object]],
) -> None:
    if not request_binding.gateway_request_is_active_turn_recovery(context.request, context.request_id):
        return
    repo = getattr(getattr(context.agent, "subagents", None), "runtime_db", None)
    recover = getattr(repo, "recover_recorded_active_turn_attempt", None)
    if not callable(recover):
        return
    runtime = request_binding.gateway_runtime_authority(context.request, context.request_id)
    if not runtime:
        # 旧请求只沿用其原有精确 task+request 证明，不能按最新 run 或展示路径猜另一个执行。
        runtime = context.request.get("conversation_runtime")
    runtime = runtime if isinstance(runtime, dict) else {}
    task_id = str(runtime.get("task_id") or "").strip()
    operation_facts: dict[str, dict[str, str]] = {}
    for record in carried_archive_tool_calls:
        operation_id = str(record.get("operation_id") or "").strip()
        if not operation_id:
            continue
        facts = {
            "status": str(record.get("tool_operation_status") or "").strip(),
            "operation_type": str(record.get("tool") or "").strip(),
        }
        previous = operation_facts.get(operation_id)
        if previous is not None and previous != facts:
            raise ConversationPersistenceError("当前回合工具记录互相冲突，已停止自动续跑")
        operation_facts[operation_id] = facts
    result = recover(
        task_id=task_id,
        run_id=str(runtime.get("run_id") or context.request_id),
        recorded_operation_facts=operation_facts,
        operator="gateway-active-turn-recovery",
        expected_attempt_id=str(runtime.get("attempt_id") or ""),
        expected_agent_run_id=str(runtime.get("agent_run_id") or ""),
    )
    recovery_status = str(result.get("status") or "")
    if recovery_status in {"recovered", "not_required"}:
        return
    if recovery_status == "absent" and not carried_archive_tool_calls:
        return
    if result.get("reason") == "operation_outcome_uncertain":
        raise ActiveTurnOutcomeUncertainError(
            "上一轮操作结果未确认：" + json.dumps(result, ensure_ascii=False)
        )
    raise ConversationPersistenceError(
        "当前回合执行恢复未通过核对：" + json.dumps(result, ensure_ascii=False)
    )


# LLM: 标识替换仅作用于公开流；原生模型历史和运行权限仍读结构化身份，普通回调无需该能力。
# 函数用途: 把本轮内部编号的公开标签交给支持脱敏的输出端。
def _set_gateway_identifier_redactions(
    on_chunk: object,
    identifiers: tuple[tuple[object, str], ...],
) -> None:
    setter = getattr(on_chunk, "set_identifier_redactions", None)
    if callable(setter):
        setter(identifiers)


# LLM: verbose 只改变可见过程，不改变模型请求和事实落账；仅调用输出端显式接口。
# 函数用途: 将当前会话的展示详细度应用到本轮流输出。
def _set_gateway_verbose_level(on_chunk: object, level: str) -> None:
    setter = getattr(on_chunk, "set_verbose_level", None)
    if callable(setter):
        setter(level)


# LLM: Gateway execution 只调用 writer 的显式 typed 接口；普通 callable/IM sink 没有该能力时保持静默而不写自然语言 fallback。
# 函数用途: 将 canonical compact generation 前进投影到支持该事件的客户端流。
def _publish_gateway_compact_boundary(on_chunk: object, generation: int) -> None:
    writer = getattr(on_chunk, "write_compact_boundary", None)
    if callable(writer):
        writer(generation)


# LLM: 声明 conversation 的请求必须拿到可读 canonical thread；加载失败不能降级成无历史的新对话。
# 函数用途: 在选模型和执行前检查会话上下文是否可靠可用。
def _require_gateway_conversation_ready(
    request: dict,
    conversation: request_context.GatewayConversationContext,
) -> None:
    if not isinstance(request.get("conversation"), dict):
        return
    if not conversation.thread_id or conversation.load_errors:
        raise ConversationPersistenceError("会话记录当前不可用，请稍后重试")


# LLM: 模型前统一 canonical task、RuntimeDB 身份与已冻结Compact view；active Goal跨前后台保持同一任务。
# 函数用途: 构造精确运行参数，已发布的恢复身份优先，未发布时沿已经校验的活动任务或 Goal 绑定。
# LLM: 附件先按已解析 owner 校验，仅结构化 refs 进入 run；跨 owner 或损坏附件不可退化成纯文本请求。
# 函数用途: 构造当前请求执行参数，并验证显式输入附件的归属与资源上限。
def _gateway_run_params(inputs: _GatewayRunParamsRequest) -> RunParams:
    request = inputs.request
    context = inputs.context
    conversation = inputs.conversation
    attrs = _gateway_run_task_attributes(conversation, request, context.request_id)
    if request.get("input_media"):
        from ..conversation.input_media import input_media_root, validate_input_media

        refs = validate_input_media(request["input_media"], root=input_media_root(context.agent),
                                    max_bytes=context.agent.config.input_media_max_bytes,
                                    max_files=context.agent.config.input_media_max_files)
        attrs = {**(attrs or {}), "input_media": list(refs)}
    task_id = str(request_binding.gateway_runtime_authority(request, context.request_id).get("task_id") or "")
    selected_task_id = str((attrs or {}).get("conversation_task_id") or "")
    goal = conversation.thread_goal or {}
    if not selected_task_id and goal.get("status") == "active":
        selected_task_id = str(goal.get("task_id") or "")
    if task_id and selected_task_id and task_id != selected_task_id:
        from .request_errors import ConversationTaskBindingError

        raise ConversationTaskBindingError("当前请求的执行身份与会话任务绑定不一致")
    task_id = task_id or selected_task_id
    return RunParams(
        inject=request_prompt.gateway_injections(request, conversation),
        prompt_files=[str(item) for item in request.get("prompt_files", [])],
        save=bool(request.get("save", True)),
        request_id=context.request_id,
        task_id=task_id,
        attempt_id=str(request.get("execution_attempt_id") or "").strip() or context.request_id,
        source="gateway",
        resume_context=_gateway_resume_context(request, conversation),
        recovery_next_actions=[
            "If this gateway request must be recovered, inspect the gateway response and LocalStore gateway_request records first."
        ],
        recovery_content_paths=[str(context.request_path), str(context.response_path)],
        on_chunk=context.on_chunk,
        root_user_prompt=inputs.prompt,
        task_attributes=attrs,
        context_scope="conversation" if conversation.thread_id else "default",
        carried_archive_tool_calls=[dict(item) for item in inputs.carried_archive_tool_calls],
        carried_active_turn_user_inputs=[
            dict(item) for item in inputs.carried_active_turn_user_inputs
        ],
        active_turn_transition_callback=request_binding.GatewayActiveTurnTransition(
            context.request_path,
            context.request_id,
            str(request.get("execution_attempt_id") or "").strip() or context.request_id,
        ),
        conversation_history_seed=request_prompt.gateway_conversation_history_seed(
            conversation,
            work_scope=request_binding.gateway_message_work_scope(request),
        ),
        compact_context=conversation.compact_context,
        conversation_task_binding_callback=request_binding.GatewayTaskBindingWriter(
            context.request_path,
            context.request_id,
            context.request,
            str(request.get("execution_attempt_id") or "").strip() or context.request_id,
        ),
        partial_turn_callback=partial(request_history.persist_gateway_partial_result, context, conversation),
    )


# LLM: Stamp only an exact request/Goal/active workspace before the first model sample. A terminal
# link may appear here only through exact authority; a historical thread projection alone cannot.
# 函数用途: 生成本轮结构化会话参数；有精确执行权时先进入原目录，否则由首个工作工具懒建新目录。
def _gateway_task_attributes(conversation: request_context.GatewayConversationContext) -> dict | None:
    attrs: dict[str, object] = {}
    if conversation.thread_id:
        attrs["conversation_thread_id"] = conversation.thread_id
        # 结构化 thread 是当前长期 IM/TUI 对话的耐久身份。Memory recall 自行 canonicalize
        # 为 session:<thread_id>；模型和客户端都不需要也不能猜这个内部 scope key。
        attrs["session_id"] = conversation.thread_id
        attrs[CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR] = True
    if conversation.cwd:
        attrs[CONVERSATION_EXECUTION_CWD_ATTR] = conversation.cwd
        attrs[CONVERSATION_RUNTIME_WORKSPACE_ROOTS_ATTR] = list(
            conversation.runtime_workspace_roots or (conversation.cwd,)
        )
    if conversation.workspace_task is not None:
        task = conversation.workspace_task
        # exact task 选择用于运行恢复；用户 cwd 和家目录权限不随内部记录路径改变。
        attrs[CONVERSATION_WORKSPACE_TASK_ID_ATTR] = task.task_id
        attrs[CONVERSATION_WORKSPACE_TASK_STATUS_ATTR] = task.status
        attrs[CONVERSATION_WORKSPACE_EXECUTION_RUNNING_ATTR] = task.execution_running
        attrs[CONVERSATION_WORKSPACE_EXECUTION_STATE_AVAILABLE_ATTR] = (
            task.execution_state_available
        )
        # 终态任务只可能来自 exact request/Goal 选择；它不预填旧 live identity。
        # 首个 promotes_task 工具再按结构化选择决定恢复或建立 successor。
        if str(task.status or "").strip().lower() in {"active", "interrupted"}:
            attrs["conversation_task_id"] = task.task_id
            attrs["run_workspace"] = {
                "task_root": task.task_path,
                "output_dir": str(Path(task.task_path) / "output"),
                "work_dir": str(Path(task.task_path) / "work"),
            }
    return attrs or None


# LLM: 已绑定会话时，默认只使用该会话历史；归档续接必须保留调用方显式配置，不混入其他 owner 任务。
# 函数用途: 决定本轮是否额外加载历史归档，避免两套上下文重复注入。
def _gateway_resume_context(
    request: dict,
    conversation: request_context.GatewayConversationContext,
) -> bool | None:
    """会话历史已是权威上下文时，默认不再自动续接 owner 旧归档。"""
    if "resume_context" in request:
        return bool(request.get("resume_context"))
    return False if conversation.thread_id else None


# LLM: 只合并 ingress 已校验的 typed 工作属性；不解析 slash 正文或修改输入字典。
# 函数用途: 将控制入口保留的任务模式加入本轮运行参数。
def _apply_system_task_attributes(
    attrs: dict | None,
    system_task: object,
) -> dict | None:
    """Merge one ingress-validated task mode without re-reading slash text."""
    task_attrs = conversation_task_attributes(system_task)
    if not task_attrs:
        return attrs
    return {**dict(attrs or {}), **task_attrs}


# LLM: 活动回合只由 request_binding 校验；Audit 范围沿准备轮原绑定投影，不从线程旧指针推断执行权。
# 函数用途: 汇总当前会话、工作模式和精确回合归属，交给运行器执行。
def _gateway_run_task_attributes(
    conversation: request_context.GatewayConversationContext,
    request: dict,
    request_id: str,
) -> dict | None:
    """Project the exact ingress-reserved named task into the current model turn."""
    attrs = _apply_system_task_attributes(
        _gateway_task_attributes(conversation),
        request.get("system_task"),
    )
    if request_binding.gateway_request_owns_active_task_turn(attrs, request, request_id):
        attrs = dict(attrs or {})
        attrs[CONVERSATION_TASK_TURN_ACTIVE_ATTR] = True
    task_attrs = conversation_task_attributes(request.get("system_task"))
    if str(task_attrs.get("conversation_work_kind") or "").strip() != "audit":
        return attrs

    scope = request.get("conversation_audit_scope")
    scope = scope if isinstance(scope, dict) else {}
    return project_audit_runtime_attributes(
        attrs,
        scope,
        thread_id=conversation.thread_id,
        turn_request_id=request_id,
    )


# LLM: 所有退出路径必须停止本请求心跳；有界 join 不能替代 durable 终态提交或取消其他请求。
# 函数用途: 通知租约线程停止并等候退出，避免结束后继续续租。
def _stop_gateway_request_lease(
    lease_stop: threading.Event | None,
    lease_thread: threading.Thread | None,
) -> None:
    if lease_stop is not None:
        lease_stop.set()
    if lease_thread is not None:
        lease_thread.join(timeout=2)


# LLM: 收尾重新读取原队列记录；读错保留 typed 诊断，不能用初始副本伪造最新心跳。
# 函数用途: 将请求最终租约字段补到响应，供恢复与诊断对账。
def _copy_final_lease_fields(response: dict, request_path: Path) -> None:
    report = read_json_file_report(
        request_path, context="gateway.request_execution.final_request.read"
    )
    if report.load_error is not None:
        response["final_request_load_error"] = report.load_error
        return
    final_request = report.payload
    if not final_request:
        return
    response["lease_owner"] = final_request.get("lease_owner", response.get("lease_owner", ""))
    response["lease_started_at"] = final_request.get(
        "lease_started_at", response.get("lease_started_at", 0)
    )
    response["lease_heartbeat_at"] = final_request.get(
        "lease_heartbeat_at",
        response.get("lease_heartbeat_at", 0),
    )


# LLM: 当前入口只执行 ask；空输入及不支持的 kind 保留原错误协议，模型结果只通过公开投影写响应。
# 函数用途: 将队列上下文接入会话主链，并用实际运行结果更新本轮响应。
def _execute_gateway_request_body(context: dict, on_chunk) -> None:
    response = context["response"]
    kind = str(response.get("kind") or "").strip()
    if kind != "ask":
        response["error_code"] = "UNSUPPORTED_KIND"
        raise ValueError(f"unsupported gateway request kind: {kind or 'empty'}")
    try:
        result = _run_gateway_ask(
            request_context.GatewayAskRunContext(
                context["agent"],
                context["request"],
                context["request_path"],
                context["response_path"],
                context["request_id"],
                on_chunk,
                context.get("stage_timings"),
            )
        )
    except ValueError as exc:
        if str(exc) == _EMPTY_PROMPT_MESSAGE:
            response["error_code"] = "EMPTY_PROMPT"
        raise
    _update_response_from_result(response, result, context["request"])


# LLM: 队列读取失败必须产生可审计响应并跳过执行；身份和响应路径只按原文件及结构化字段解析。
# 函数用途: 读取本轮持久请求、创建响应并记录 processing 审计。
def _prepare_gateway_request_context(agent: SimpleAgent, request_path: Path) -> dict:
    request_report = read_json_file_report(
        request_path, context="gateway.request_execution.request.read"
    )
    if request_report.load_error is not None:
        started_at = time.time()
        response = gateway_request_load_error_response(
            request_path,
            request_report.load_error,
            started_at=started_at,
        )
        context = {
            "request": {},
            "request_id": str(request_path.stem),
            "kind": "unknown",
            "started_at": started_at,
            "request_path": request_path,
            "response_path": gateway_response_path(gateway_paths(agent), request_path.stem),
            "response": response,
            "skip_execution": True,
        }
        audit_request_processing(agent, context)
        return context
    request = request_report.payload
    request_id = str(request.get("id") or request_path.stem)
    kind = str(request.get("kind") or "").strip() or ("ask" if request_id else "")
    response_path = gateway_response_path(gateway_paths(agent), request_id)
    started_at = time.time()
    response = _build_gateway_response_base(
        _GatewayResponseBaseContext(request, request_path, request_id, kind, started_at)
    )
    context = {
        "request": request,
        "request_id": request_id,
        "kind": kind,
        "started_at": started_at,
        "request_path": request_path,
        "response_path": response_path,
        "response": response,
    }
    audit_request_processing(agent, context)
    return context

# LLM: 插话持久操作经 guidance 领域组件； 收尾窗口里到达的插话可能还没被任何模型安全点认领；用户消息不能因为"那一轮刚好结束"而
#   永久失联。这里只释放 pending 回执（已进提示的 reserved/submitted 不动），交给下一轮认领。
#   fail-silent：恢复动作绝不反噬回合收口，也不改交付状态。
# 函数用途: 在 Gateway 回合终态释放未被认领的补充消息。
def _release_unclaimed_turn_guidance(context: dict) -> None:
    agent = context.get("agent")
    store = getattr(agent, "conversation_store", None)
    release = getattr(getattr(getattr(store, 'guidance', None), 'recovery', None), 'release_unclaimed', None)
    if not callable(release):
        return
    try:
        release(str(context.get("request_id") or ""))
    except Exception:
        return


# LLM: 收尾先释放尚未认领的引导，再读取最终租约；阶段耗时只作诊断，不参与成功判定。
# 函数用途: 释放待续接输入并补齐响应时间和阶段日志，实际投递仍由原出口完成。
def _finalize_gateway_response(context: dict, response: dict) -> None:
    _release_unclaimed_turn_guidance(context)
    ended_at = time.time()
    _copy_final_lease_fields(response, context["request_path"])
    response["ended_at"] = ended_at
    response["duration_seconds"] = round(ended_at - context["started_at"], 3)
    stages = context.get("stage_timings")
    if stages:
        response["stages_ms"] = dict(stages)
        logger.info(
            "gateway request stages request_id=%s stages=%s",
            context.get("request_id") or "",
            json.dumps(stages, ensure_ascii=False),
        )


# LLM: 异常退出也保留已经观测到的工具轮数；只能合并 typed 流统计，不能据此声称工具成功。
# 函数用途: 将本轮实际工具活动补入最终响应，避免错误路径丢失执行统计。
def _project_observed_gateway_run_facts(
    response: dict,
    chunk_writer: BufferedChunkStreamWriter,
) -> None:
    """Merge structured live events into the terminal response after any exit."""
    response["tool_rounds"] = max(
        int(response.get("tool_rounds") or 0),
        chunk_writer.observed_tool_rounds,
    )


# LLM: 审计必须使用与最终响应同一份 request/path/result；保留队列出口的终态提交顺序。
# 函数用途: 将本轮已整理的响应和请求交给审计服务落账。
def _complete_gateway_request_audit(
    agent: SimpleAgent, context: dict, request_path: Path, response: dict
) -> None:
    audit_request_completed(
        agent,
        params=AuditRequestCompletedParams(
            response=response,
            request=context["request"],
            request_path=request_path,
            response_path=context["response_path"],
        ),
    )


# LLM: 执行 owner 只取宿主为本请求解析出的 agent.home_paths.owner_id（规范编号，与 audit_records/admin_controls 同源），
#   写进终态响应供审计按请求归属；不从 user_id、渠道或正文推断。
# 函数用途: 返回执行本请求的用户（owner）规范编号。
def _executing_owner_id(agent: object) -> str:
    return str(getattr(getattr(agent, "home_paths", None), "owner_id", "") or "local/main")


# LLM: 每个 claimed request 只创建一个 chunk writer；审批只读 client_capabilities 或服务端核实的管理员 IM 私聊，失败只投影 typed HTTP 事实，不把异常正文公开或用作重试依据。
# 函数用途: 执行一条 Gateway 请求、维护 lease/chunk，并保存已有执行与本次失败的真实响应。
def _handle_gateway_request(
    agent: SimpleAgent,
    request_path: Path,
    *,
    refresh_lease: bool = False,
    worker_id: str = "",
) -> dict:
    started_mono = time.monotonic()
    context = _prepare_gateway_request_context(agent, request_path)
    stage_timings: dict[str, float] = {
        "request_read_ms": round((time.monotonic() - started_mono) * 1000, 1)
    }
    context["stage_timings"] = stage_timings
    response = context["response"]
    response["owner_id"] = _executing_owner_id(agent)
    if context.get("skip_execution"):
        _finalize_gateway_response(context, response)
        _complete_gateway_request_audit(agent, context, request_path, response)
        return response
    if request_binding.gateway_cancel_requested(request_path, context["request_id"]):
        _apply_cancelled_gateway_response(response)
        _finalize_gateway_response(context, response)
        _complete_gateway_request_audit(agent, context, request_path, response)
        return response
    lease_stop, lease_thread = _start_gateway_request_lease(
        _GatewayLeaseStartContext(
            agent,
            context["request"],
            request_path,
            context["request_id"],
            refresh_lease,
            worker_id,
        )
    )
    chunk_path = claimed_request_chunk_path(request_path, context["request_id"])
    chunk_path_abs, _ = open_chunk_stream(chunk_path)
    chunk_writer = BufferedChunkStreamWriter(
        chunk_path_abs,
        interactive_approvals=_gateway_request_interactive_approvals(agent, context["request"]),
        rich_transcript=_gateway_client_supports_rich_transcript(context["request"]),
        delivery_channel=request_history.gateway_request_channel(context["request"]),
    )

    execution_started_mono = time.monotonic()
    try:
        _execute_gateway_request_body({**context, "agent": agent}, chunk_writer)
    except Exception as exc:
        from .request_errors import gateway_provider_error_projection

        error_code = response.get("error_code") or str(
            getattr(exc, "error_code", "") or type(exc).__name__.upper()
        )
        response.update(
            {
                "ok": False,
                "status": "failed",
                "error_code": error_code,
                "error": f"{type(exc).__name__}: {exc}",
                "user_error": _gateway_user_error(agent, context["request"], error_code),
                **gateway_provider_error_projection(exc),
            }
        )
    finally:
        _stop_gateway_request_lease(lease_stop, lease_thread)
        chunk_writer.close()
        # 回合结束必收口未消费的补充消息（会话运行时 语义：pending input 不得挂在已结束
        # turn 上占 conversation lane；否则同 thread 后续请求全部排队挂起——#7 实证）。
        _settle_pending_gateway_guidance(agent, context["request_id"])
        _record_gateway_stage(stage_timings, "execution_ms", execution_started_mono)
        stage_timings["total_ms"] = round((time.monotonic() - started_mono) * 1000, 1)
    _project_observed_gateway_run_facts(response, chunk_writer)
    if request_binding.gateway_cancel_requested(request_path, context["request_id"]):
        _apply_cancelled_gateway_response(response)
    _finalize_gateway_response(context, response)
    _complete_gateway_request_audit(agent, context, request_path, response)
    return response


# LLM: 插话持久操作经 guidance 领域组件； 回合结束时未消费的 steer/guidance 必须收口（reject），否则残留消息占住
# conversation lane，同一 thread 的后续请求永久排队（#7 真机实证：进行中提交的消息
# 在回合结束后挂 processing）。收口失败留给 recovery 兜底，静默不反噬执行路径。
# 函数用途: 在回合终态落账前拒绝该回合仍挂起的补充消息。
def _settle_pending_gateway_guidance(agent: SimpleAgent, request_id: str) -> None:
    store = getattr(agent, "conversation_store", None)
    if store is None or not str(request_id or "").strip():
        return
    try:
        store.guidance.recovery.reject_pending(request_id, reject_reserved=True)
    except Exception:  # noqa: BLE001 收口失败不阻断回合收尾，recovery 会再次处理
        pass


# LLM: capability 缺失或非布尔真值一律视为不支持，避免普通 Gateway/IM 请求在无人确认时永久挂起。
# 函数用途: 判断请求客户端是否显式支持交互工具审批。
def _gateway_client_supports_tool_approval(request: object) -> bool:
    if not isinstance(request, dict):
        return False
    capabilities = request.get("client_capabilities")
    return bool(isinstance(capabilities, dict) and capabilities.get("tool_approval") is True)


# LLM: 文案仍只按结构化 error_code 映射；尚未配置模型时，未绑定管理员的 IM 私聊追加 /admin 指引（判据见
#   request_worker.admin_binding_hint_for_request），其余错误码不变。
# 函数用途: 生成返回给客户端的失败说明，让飞书里的管理员直接知道先绑定身份。
def _gateway_user_error(agent: object, request: object, error_code: object) -> str:
    from .request_errors import gateway_client_error_message

    message = gateway_client_error_message(error_code)
    if str(error_code or "").strip().upper() != "MODEL_NOT_CONFIGURED" or not isinstance(request, dict):
        return message
    from .request_worker import admin_binding_hint_for_request

    return message + admin_binding_hint_for_request(agent, request)


# LLM: 交互审批只来自两种结构化事实：客户端显式声明 tool_approval（TUI），或服务端核实本请求来自已绑定管理员的 IM 私聊
#   且执行 owner 正是本机管理员（local/main）。IM 客户端不能自己声明这项能力；绑定和 owner 都由 Gateway 读自己的配置与文件。
# 函数用途: 决定本请求遇到需要确认的工具时是等用户决定（TUI 面板或 IM 的 /approve、/deny），还是立即按无法确认拒绝。
def _gateway_request_interactive_approvals(agent: object, request: object) -> bool:
    if _gateway_client_supports_tool_approval(request):
        return True
    if not isinstance(request, dict):
        return False
    from ..user_space.approval_mode import is_permission_admin
    from .request_worker import admin_channel_identity_for_request

    if not is_permission_admin(getattr(agent, "home_paths", None)):
        return False
    return admin_channel_identity_for_request(agent, request) is not None


# LLM: 富 transcript 只接受 client_capabilities.rich_transcript 的精确布尔真值，不从 source/TTY 猜测。
# 函数用途: 判断请求客户端是否显式支持思考、逐轮说明和结构化工具结果。
def _gateway_client_supports_rich_transcript(request: object) -> bool:
    if not isinstance(request, dict):
        return False
    capabilities = request.get("client_capabilities")
    return bool(isinstance(capabilities, dict) and capabilities.get("rich_transcript") is True)


# LLM: 停止只中断当前请求，持久任务仍可恢复；保留已提交工具历史，并沿原交付协议抑制正文。
# 函数用途: 将当前响应设为已中断，向客户端报告用户停止的实际状态。
def _apply_cancelled_gateway_response(response: dict) -> None:
    response.update(
        {
            "ok": True,
            "status": "interrupted",
            "response": "",
            "error_code": "INTERRUPTED",
            "error": "",
            "channel_delivery": request_history.silent_user_stop_delivery(),
        }
    )
