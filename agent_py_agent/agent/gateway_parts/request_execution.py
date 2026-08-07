from __future__ import annotations

"""execution helpers keep one claimed gateway request inside focused contexts.

Gateway responses expose both per-turn and cumulative token estimates. CLI/TUI
status should use the cumulative field when showing current context pressure.
"""

import json
import logging
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..agent_core.runtime_mixin import RunParams
from ..common.audit_activation import (
    AUDIT_ATTR,
)
from ..concurrency.interrupt import is_interrupted
from ..conversation.active_turn_input import merge_active_turn_user_inputs
from ..conversation.audit_lifecycle import (
    AuditLifecycleError,
    audit_scope_payload,
    prepare_named_audit,
    project_audit_runtime_attributes,
)
from ..conversation.authority import (
    CONVERSATION_AUDIT_PREPARE_ATTR,
    CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR,
    CONVERSATION_WORK_KIND_ATTR,
    CONVERSATION_WORK_NAME_ATTR,
    CONVERSATION_WORKSPACE_EXECUTION_RUNNING_ATTR,
    CONVERSATION_WORKSPACE_EXECUTION_STATE_AVAILABLE_ATTR,
    CONVERSATION_WORKSPACE_TASK_ID_ATTR,
    CONVERSATION_WORKSPACE_TASK_STATUS_ATTR,
)
from ..conversation.channels import (
    project_user_reply,
    redact_host_absolute_paths,
    redact_structured_identifiers,
)
from ..conversation.compact import (
    ConversationScope,
    conversation_scope,
    prepare_conversation_context,
)
from ..conversation.control_commands import (
    conversation_task_attributes,
    system_slash_command_name,
)
from ..conversation.history_index import (
    ensure_thread_history_indexed,
    index_conversation_message,
)
from ..conversation.models import is_audit_background_transcript_entry
from ..conversation.run_claim import (
    ConversationRunLaneRequest,
    claim_heartbeat_interval_seconds,
    conversation_run_lane,
)
from ..ingestion.source_binding import public_audit_source_bindings
from ..tooling.operation_verification import public_operation_verification
from .audit_service import (
    AuditRequestCompletedParams,
    audit_request_completed,
    audit_request_processing,
)
from .io import (
    gateway_response_path,
    read_json_file,
    read_json_file_report,
    update_json_file_atomic,
)
from .lease_service import refresh_processing_lease, start_lease_heartbeat
from .paths import gateway_chunk_path, gateway_paths
from .recovery import _gateway_request_attempts
from .request_errors import (
    ConversationPersistenceError,
    SystemCommandRoutingError,
    UserReplyUnavailableError,
    gateway_request_load_error_response,
)
from .response_renderer import is_silent_user_stop, read_gateway_response_file

if TYPE_CHECKING:
    from ...core import SimpleAgent

_EMPTY_PROMPT_MESSAGE = "gateway ask prompt/goal cannot be empty"
_CHUNK_STREAM_FLUSH_INTERVAL_SECONDS = 0.08
_CHUNK_STREAM_FLUSH_CHARS = 128
_GATEWAY_FOREGROUND_CLAIM_REASON = "gateway_foreground_turn"
_MAX_PROMPT_OPERATION_EVIDENCE_EVENTS = 4
logger = logging.getLogger(__name__)


def open_chunk_stream(chunk_path: Path) -> tuple[Path, float]:
    chunk_path.parent.mkdir(parents=True, exist_ok=True)
    return chunk_path, time.time()


def claimed_request_chunk_path(request_path: Path, request_id: str) -> Path:
    """Keep live chunks beside the authoritative claimed queue record.

    The request worker claims records in the base Gateway queue even when the
    actual run uses an owner-scoped agent.  HTTP progress polling and final
    archival both follow that claimed record, so deriving this path from the
    owner agent root would make per-user progress invisible to the adapter.
    """
    return request_path.with_name(f"{request_id}.chunks.jsonl")


# LLM: typed progress 与模型 delta 共用归档文件但保留 kind，客户端不得再解析“[工具]”正文猜状态。
# 函数用途: 原子追加一个带类型的 Gateway 流事件。
def write_chunk_event(chunk_path: Path, payload: dict[str, object]) -> None:
    try:
        line = json.dumps({"t": time.time(), **payload}, ensure_ascii=False)
        with open(chunk_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def close_chunk_stream(chunk_path: Path) -> None:
    # Keep the chunk file after completion so clients that observe the final
    # response first can still drain the last streamed tokens.
    return


@dataclass
class BufferedChunkStreamWriter:
    """Buffer model deltas and persist typed user-visible stream events."""

    chunk_path: Path
    flush_interval_seconds: float = _CHUNK_STREAM_FLUSH_INTERVAL_SECONDS
    flush_chars: int = _CHUNK_STREAM_FLUSH_CHARS
    _buffer: list[str] = field(default_factory=list)
    _buffer_chars: int = 0
    _last_flush_at: float = field(default_factory=time.monotonic)
    _verbose_level: str = "off"
    _model_segment: list[str] = field(default_factory=list)
    _commentary_emitted: bool = False
    _identifier_redactions: tuple[tuple[object, str], ...] = ()
    _observed_tool_rounds: int = 0

    def __call__(self, text: str) -> None:
        self.write(text)

    def write_model(self, text: str) -> None:
        """Keep one tentative model segment until its lifecycle is known.

        A provider delta is not yet an accepted user reply: the same generation
        may go on to call a tool, be invalidated by steering, or be replaced by
        the final structured response.  Only the sanitized commentary emitted
        at a real tool boundary and the terminal response file are public.
        """
        if text and not self._commentary_emitted:
            self._model_segment.append(text)

    def set_verbose_level(self, level: str) -> None:
        normalized = str(level or "off").strip().lower()
        self._verbose_level = normalized if normalized in {"off", "on", "full"} else "off"

    def begin_active_turn_input(self) -> None:
        """Allow one new model-authored commentary after live user steering.

        Ordinary tool rounds remain suppressed after the first commentary.  A
        real user message arriving during the active turn opens exactly one new
        segment so the same run can answer without waiting for final closeout.
        """
        self._model_segment.clear()
        self._commentary_emitted = False

    def set_identifier_redactions(
        self,
        identifiers: tuple[tuple[object, str], ...],
    ) -> None:
        """Install exact trusted identifiers before any model commentary is published."""
        self._identifier_redactions = tuple(identifiers)

    def write(self, text: str) -> None:
        if not text:
            return
        self._buffer.append(text)
        self._buffer_chars += len(text)
        if self._should_flush(text):
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        text = "".join(self._buffer)
        self._buffer.clear()
        self._buffer_chars = 0
        self._last_flush_at = time.monotonic()
        write_chunk_event(
            self.chunk_path,
            {
                "kind": "runtime_progress",
                "text": text,
                "verbose_level": self._verbose_level,
            },
        )

    def write_progress(self, event: dict[str, object], legacy_text: str) -> None:
        try:
            observed_round = int(event.get("round") or 0)
        except (TypeError, ValueError):
            observed_round = 0
        self._observed_tool_rounds = max(self._observed_tool_rounds, observed_round)
        self.flush()
        if event.get("phase") == "started":
            self._write_first_model_commentary()
        progress = dict(event)
        if self._verbose_level != "full":
            progress.pop("output", None)
        write_chunk_event(
            self.chunk_path,
            {
                "kind": "tool_progress",
                "text": legacy_text,
                "verbose_level": self._verbose_level,
                "progress": progress,
            },
        )

    @property
    def observed_tool_rounds(self) -> int:
        """Return exact structured progress observed during this request."""
        return self._observed_tool_rounds

    def close(self) -> None:
        self.flush()
        close_chunk_stream(self.chunk_path)

    def _write_first_model_commentary(self) -> None:
        if self._commentary_emitted or not self._model_segment:
            return
        raw = "".join(self._model_segment)
        self._model_segment.clear()
        projection = project_user_reply(raw)
        content = redact_structured_identifiers(
            redact_host_absolute_paths(projection.content),
            self._identifier_redactions,
        ).strip()
        if not content:
            return
        self._commentary_emitted = True
        write_chunk_event(
            self.chunk_path,
            {
                "kind": "assistant_commentary",
                "text": content,
            },
        )

    def _should_flush(self, latest_text: str) -> bool:
        if self._buffer_chars >= max(1, int(self.flush_chars)):
            return True
        if latest_text.endswith("\n"):
            return True
        elapsed = time.monotonic() - self._last_flush_at
        return elapsed >= max(0.0, float(self.flush_interval_seconds))


@dataclass(frozen=True)
class _GatewayResponseBaseContext:
    request: dict
    request_path: Path
    request_id: str
    kind: str
    started_at: float


@dataclass(frozen=True)
class _GatewayAskRunContext:
    agent: SimpleAgent
    request: dict
    request_path: Path
    response_path: Path
    request_id: str
    on_chunk: object


# LLM: This value is the resolved 会话运行时 thread workspace, not proof that the current turn
# has started task execution; lifecycle activation still happens at the first promoting tool.
# 类用途: 保存本轮从会话状态继承的确切任务目录，普通聊天只进入目录而不会重开任务。
@dataclass(frozen=True)
class _GatewayWorkspaceSelection:
    task_id: str
    status: str
    goal: str
    task_path: str
    execution_running: bool = False
    execution_state_available: bool = True
    execution_sources: tuple[str, ...] = ()


# LLM: Gateway projects one owner/thread history plus one sticky workspace; internal run records
# never become model-visible task choices.
# 类用途: 保存一个持续 thread 的历史和跨轮继承工作目录。
@dataclass(frozen=True)
class _GatewayConversationContext:
    thread_id: str = ""
    compact_summary: str = ""
    compact_operation_evidence: dict[str, object] = field(default_factory=dict)
    compact_operation_evidence_ref: str = ""
    # LLM: Keep retained-tail operation facts outside prose so text-only history projection
    # cannot discard a recent verified side effect.
    # 字段用途: 保存近期未压缩 assistant metadata 中的程序核验事实，与摘要证据分栏注入下一轮。
    recent_operation_evidence: dict[str, object] = field(default_factory=dict)
    compact_generation: int = 0
    verbose_level: str = "off"
    scope: ConversationScope | None = None
    history: tuple[tuple[str, str], ...] = ()
    recent_artifacts: tuple[dict[str, object], ...] = ()
    workspace_task: _GatewayWorkspaceSelection | None = None
    thread_goal: dict[str, object] | None = None
    named_work: tuple[dict[str, str], ...] = ()
    load_errors: tuple[dict, ...] = ()


@dataclass(frozen=True)
class _GatewayConversationLoadRequest:
    agent: SimpleAgent
    request: dict
    request_id: str
    prompt: str


@dataclass(frozen=True)
class _GatewayRunParamsRequest:
    request: dict
    context: _GatewayAskRunContext
    conversation: _GatewayConversationContext
    prompt: str
    carried_archive_tool_calls: tuple[dict[str, object], ...] = ()
    carried_active_turn_user_inputs: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True)
class _GatewayTaskBindingWriter:
    """Publish live request -> durable task lineage for /status, /btw and /stop."""

    request_path: Path
    request_id: str

    def __call__(self, link: object) -> bool:
        return _persist_gateway_request_task_binding(
            self.request_path,
            self.request_id,
            thread_id=str(getattr(link, "thread_id", "") or ""),
            task_id=str(getattr(link, "task_id", "") or ""),
            task_path=str(getattr(link, "task_path", "") or ""),
        )


@dataclass(frozen=True)
class _GatewayLeaseStartContext:
    agent: SimpleAgent
    request: dict
    request_path: Path
    request_id: str
    refresh_lease: bool
    worker_id: str


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


# LLM: Gateway 对外 response 只放 user-facing projection；内部运行协议不进入用户正文。
# 函数用途: 将一次模型运行结果整理成可供客户端读取的最终响应。
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
            "memory_resume_context_injected": result.memory_resume_context_injected,
            "memory_resume_context_query": result.memory_resume_context_query,
            "memory_resume_context_matches": result.memory_resume_context_matches,
            "memory_resume_context_token_estimate": result.memory_resume_context_token_estimate,
            "memory_resume_context_error": result.memory_resume_context_error,
            "runtime_status": str(getattr(result, "runtime_status", "ok") or "ok"),
            "runtime_reason": str(getattr(result, "runtime_reason", "") or ""),
            "runtime_source": str(getattr(result, "runtime_source", "") or ""),
            "conversation_persist_degraded": bool(
                getattr(result, "conversation_persist_degraded", False)
            ),
            "conversation_persist_error": str(
                getattr(result, "conversation_persist_error", "") or ""
            ),
            "channel_delivery": public_delivery,
        }
    )


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


def _start_gateway_request_lease(
    context: _GatewayLeaseStartContext,
) -> tuple[threading.Event | None, threading.Thread | None]:
    should_refresh = (
        context.refresh_lease or str(context.request.get("status") or "") == "processing"
    )
    if not should_refresh:
        return None, None
    lease_worker = context.worker_id or str(context.request.get("lease_owner") or "")
    refresh_processing_lease(
        context.request_path, request_id=context.request_id, worker_id=lease_worker
    )
    return start_lease_heartbeat(
        context.agent, context.request_path, request_id=context.request_id, worker_id=lease_worker
    )


# LLM: assistant 落账前先拆出用户正文与产物 metadata，内部协议不得进入权威 transcript。
# 函数用途: 执行一轮 Gateway 对话，并可靠保存用户消息、回复投影和近期产物引用。
def _run_gateway_ask(context: _GatewayAskRunContext):
    request = context.request
    prompt = str(request.get("prompt") or request.get("goal") or "").strip()
    if not prompt:
        raise ValueError(_EMPTY_PROMPT_MESSAGE)
    load_request = _GatewayConversationLoadRequest(
        context.agent,
        request,
        context.request_id,
        prompt,
    )
    preflight = _preflight_gateway_conversation(load_request)
    _require_gateway_conversation_ready(request, preflight)
    with _gateway_conversation_execution_lane(
        context.agent,
        preflight.thread_id,
        request_id=context.request_id,
    ):
        # Reserve the thread before reading compact/history/task state.  A turn
        # queued behind another turn must see that prior turn's final transcript,
        # not the stale snapshot from the time it entered the Gateway.
        conversation = _gateway_conversation_context(load_request)
        _require_gateway_conversation_ready(request, conversation)
        return _execute_gateway_conversation_turn(context, prompt, conversation)


def _execute_gateway_conversation_turn(
    context: _GatewayAskRunContext,
    prompt: str,
    conversation: _GatewayConversationContext,
):
    request = context.request
    _set_gateway_identifier_redactions(
        context.on_chunk,
        _gateway_identifier_redactions(context, conversation),
    )
    _set_gateway_verbose_level(context.on_chunk, conversation.verbose_level)
    if system_slash_command_name(prompt):
        raise SystemCommandRoutingError("系统命令必须在控制入口处理，不能进入模型执行队列")
    _register_named_system_task(context, conversation, prompt)
    if not _append_gateway_conversation_message(
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
    )
    return _persist_gateway_assistant_result(context, conversation, result)


def _register_named_system_task(
    context: _GatewayAskRunContext,
    conversation: _GatewayConversationContext,
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
    _persist_gateway_request_task_binding(
        context.request_path,
        context.request_id,
        thread_id=conversation.thread_id,
        task_id=link.task_id,
        task_path=str(getattr(link, "task_path", "") or ""),
    )


def _run_gateway_turn_with_conversation_compact(
    context: _GatewayAskRunContext,
    prompt: str,
    conversation: _GatewayConversationContext,
) -> tuple[object, _GatewayConversationContext]:
    """Compact the authoritative thread inline and retry the same user turn."""
    request = context.request
    current = conversation
    carried_archive_tool_calls: list[dict[str, object]] = []
    carried_active_turn_user_inputs: list[dict[str, object]] = []
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
        result = context.agent.run(
            prompt,
            params=run_params,
        )
        if str(getattr(result, "runtime_status", "") or "").strip().lower() != "context_overflow":
            return result, current
        # The same durable turn continues after transcript compaction.  Carry
        # its typed tool archive and injected user steering into the fresh
        # provider request so completed reads/writes, one-shot orchestration,
        # tool-round budgets and /btw inputs are not reset or replayed.
        result_archive = [
            dict(item)
            for item in list(getattr(result, "archive_tool_calls", None) or [])
            if isinstance(item, dict)
        ]
        from ..agent_core.runtime_mixin import _result_added_tool_progress

        made_tool_progress = _result_added_tool_progress(
            run_params,
            result,
            result_archive,
        )
        if result_archive:
            carried_archive_tool_calls = result_archive
        prior_active_turn_user_inputs = list(carried_active_turn_user_inputs)
        carried_active_turn_user_inputs = merge_active_turn_user_inputs(
            carried_active_turn_user_inputs,
            getattr(result, "active_turn_user_inputs", None),
        )
        made_guidance_progress = carried_active_turn_user_inputs != prior_active_turn_user_inputs
        refreshed = _gateway_conversation_context(
            _GatewayConversationLoadRequest(
                context.agent,
                request,
                context.request_id,
                prompt,
            ),
            force_compact=True,
        )
        _require_gateway_conversation_ready(request, refreshed)
        if refreshed.compact_generation <= current.compact_generation:
            # A single user turn can cross the pressure boundary repeatedly while
            # its completed transcript prefix stays unchanged.  Continue only
            # when typed tool/guidance state advanced; otherwise the bounded loop
            # would merely replay the same overflowing request.
            if not (made_tool_progress or made_guidance_progress):
                raise ConversationPersistenceError("当前会话无法继续压缩，请稍后重试")
        current = refreshed
    raise ConversationPersistenceError("当前会话压缩后仍超过模型上下文上限")


def _persist_gateway_assistant_result(
    context: _GatewayAskRunContext,
    conversation: _GatewayConversationContext,
    result: object,
):
    # `/stop` is a control-plane interruption, not an assistant utterance.  The
    # typed runtime result still closes the request and lets pollers retire
    # their pending reply, but it must never become transcript history,
    # searchable memory, a compact input, or a channel message.
    if is_silent_user_stop(result):
        result.channel_delivery = _silent_user_stop_delivery()
        return result
    delivery_projection = project_user_reply(str(getattr(result, "response", "") or ""))
    if delivery_projection.internal_signal:
        raise UserReplyUnavailableError("任务只返回了运行时内部信号，没有生成可交付给用户的回复")
    if not delivery_projection.content:
        raise UserReplyUnavailableError("模型没有生成可安全交付的自然回复")
    channel_delivery = delivery_projection.to_dict()
    public_content = redact_structured_identifiers(
        redact_host_absolute_paths(delivery_projection.content),
        _gateway_identifier_redactions(context, conversation, result=result),
    )
    channel_delivery["content"] = public_content
    channel_delivery["artifacts"] = _metadata_artifact_refs(
        getattr(result, "delivery_artifacts", None)
    )
    operation_verification = public_operation_verification(
        getattr(result, "operation_verification", None)
    )
    channel_delivery["operation_verification"] = operation_verification
    result.channel_delivery = channel_delivery
    if not _append_gateway_conversation_message(
        context.agent,
        context.request,
        conversation,
        request_id=context.request_id,
        role="assistant",
        content=public_content,
        delivery_artifacts=channel_delivery["artifacts"],
        operation_verification=channel_delivery.get("operation_verification"),
    ):
        _queue_gateway_conversation_repair(
            context.agent,
            context.request,
            conversation,
            request_id=context.request_id,
            role="assistant",
            content=public_content,
            delivery_artifacts=channel_delivery["artifacts"],
            operation_verification=channel_delivery.get("operation_verification"),
        )
        result.conversation_persist_degraded = True
        result.conversation_persist_error = "assistant transcript append deferred for repair"
    return result


_IDENTIFIER_PUBLIC_LABELS = {
    "user_id": "当前用户",
    "canonical_user_id": "当前用户",
    "channel_user_id": "当前用户",
    "conversation_id": "当前会话",
    "channel_conversation_id": "当前会话",
    "channel_chat_id": "当前会话",
    "message_id": "当前消息",
    "request_id": "当前请求",
    "thread_id": "当前会话",
    "task_id": "当前任务",
    "goal_id": "当前目标",
    "audit_id": "当前监控",
    "owner_id": "当前空间",
    "reply_to": "当前消息",
    "progress_handle": "当前进度",
}


# LLM: 用户出口遮蔽只枚举可信 request/conversation/task 结构中的稳定字段；不得解析模型正文猜 ID。
# 函数用途: 为最终回复和流式 commentary 生成同一份精确标识替换表，内部 transcript 保留原始正文。
def _gateway_identifier_redactions(
    context: _GatewayAskRunContext,
    conversation: _GatewayConversationContext,
    *,
    result: object | None = None,
) -> tuple[tuple[object, str], ...]:
    pairs: list[tuple[object, str]] = [(context.request_id, "当前请求")]
    for source in (
        context.request,
        context.request.get("metadata"),
        context.request.get("conversation"),
        context.request.get("conversation_runtime"),
        context.request.get("conversation_audit_scope"),
    ):
        if not isinstance(source, dict):
            continue
        pairs.extend(
            (source.get(key), label)
            for key, label in _IDENTIFIER_PUBLIC_LABELS.items()
            if source.get(key)
        )
    pairs.append((conversation.thread_id, "当前会话"))
    if conversation.scope is not None:
        pairs.extend(
            (
                (conversation.scope.owner_id, "当前空间"),
                (conversation.scope.canonical_user_id, "当前用户"),
                (conversation.scope.channel_conversation_id, "当前会话"),
                (conversation.scope.channel_user_id, "当前用户"),
            )
        )
    if isinstance(conversation.thread_goal, dict):
        pairs.extend(
            (
                (conversation.thread_goal.get("goal_id"), "当前目标"),
                (conversation.thread_goal.get("task_id"), "当前任务"),
            )
        )
    if conversation.workspace_task is not None:
        pairs.append((conversation.workspace_task.task_id, "当前任务"))
    store = getattr(context.agent, "conversation_store", None)
    if store is not None and conversation.thread_id:
        links, _load_errors = store.task_links_report(conversation.thread_id)
        for link in links:
            task_id = str(getattr(link, "task_id", "") or "").strip()
            if not task_id:
                continue
            work_kind = str(getattr(link, "work_kind", "") or "").strip().lower()
            label = {
                "audit": "当前监控",
                "goal": "当前目标",
            }.get(work_kind, "当前任务")
            pairs.append((task_id, label))
    pairs.extend(_result_identifier_redactions(result))
    return tuple(pairs)


# LLM: 本轮工具新生成的运行标识只能从可信工具结果外壳读取；不得扫描模型正文猜测 ID。
# 函数用途: 让新建 watch 等本轮才出现的内部标识在最终正文与会话落账前统一被遮蔽。
def _result_identifier_redactions(result: object | None) -> tuple[tuple[object, str], ...]:
    pairs: list[tuple[object, str]] = []
    records = getattr(result, "archive_tool_calls", None) if result is not None else None
    for record in records or ():
        if not isinstance(record, dict):
            continue
        tool = str(record.get("tool") or "").strip()
        parameters = record.get("parameters")
        if tool == "watch_stream" and isinstance(parameters, dict):
            pairs.append((parameters.get("watch_id"), "当前来源读取"))
        payload = _complete_runtime_tool_output(record)
        if tool == "watch_stream":
            pairs.append((payload.get("watch_id"), "当前来源读取"))
            source_binding = payload.get("source_binding")
            if isinstance(source_binding, dict):
                pairs.append((source_binding.get("watch_id"), "当前来源读取"))
        elif tool == "publish_audit_update":
            pairs.append((payload.get("audit_id"), "当前监控"))
    return tuple(pairs)


def _complete_runtime_tool_output(record: dict[str, object]) -> dict[str, object]:
    """Parse only a complete, in-memory runtime tool envelope preview."""
    if record.get("output_externalized") is True:
        return {}
    raw = record.get("output_preview")
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _set_gateway_identifier_redactions(
    on_chunk: object,
    identifiers: tuple[tuple[object, str], ...],
) -> None:
    setter = getattr(on_chunk, "set_identifier_redactions", None)
    if callable(setter):
        setter(identifiers)


def _gateway_conversation_execution_lane(
    agent: SimpleAgent,
    thread_id: str,
    *,
    request_id: str,
):
    """Serialize one thread's foreground and background model turns.

    会话运行时 reserves one ``active_turn`` before automatic idle work.  The
    conversation claim file adapts that invariant to my-agent's durable runtime.
    Foreground Gateway turns must own the same lane; otherwise a scheduled
    background wake can run the task concurrently and deliver a false completion
    while the foreground request is still changing files.
    """
    thread_id = str(thread_id or "").strip()
    if not thread_id:
        return nullcontext()
    store = agent.conversation_store
    config = getattr(agent, "config", None)
    ttl_seconds = max(1, int(getattr(config, "background_claim_ttl_seconds", 90) or 90))
    interval_seconds = claim_heartbeat_interval_seconds(
        ttl_seconds=ttl_seconds,
        configured_interval_seconds=getattr(
            config,
            "background_claim_heartbeat_interval_seconds",
            0,
        ),
    )
    return conversation_run_lane(
        ConversationRunLaneRequest(
            store=store,
            thread_id=thread_id,
            # This is an execution owner, not durable workspace identity.  Keeping
            # the ids distinct lets admission reject only a true second executor
            # while this foreground turn owns the shared lane.
            claim_task_id=f"gateway:{request_id}",
            reason=_GATEWAY_FOREGROUND_CLAIM_REASON,
            lease_seconds=ttl_seconds,
            heartbeat_interval_seconds=interval_seconds,
            interrupt_check=is_interrupted,
            runtime_facts={"execution_source": "gateway", "request_id": request_id},
        )
    )


def _set_gateway_verbose_level(on_chunk: object, level: str) -> None:
    setter = getattr(on_chunk, "set_verbose_level", None)
    if callable(setter):
        setter(level)


def _require_gateway_conversation_ready(
    request: dict,
    conversation: _GatewayConversationContext,
) -> None:
    if not isinstance(request.get("conversation"), dict):
        return
    if not conversation.thread_id or conversation.load_errors:
        raise ConversationPersistenceError("会话记录当前不可用，请稍后重试")


def _gateway_run_params(inputs: _GatewayRunParamsRequest) -> RunParams:
    request = inputs.request
    context = inputs.context
    conversation = inputs.conversation
    return RunParams(
        inject=_gateway_injections(request, conversation),
        prompt_files=[str(item) for item in request.get("prompt_files", [])],
        save=bool(request.get("save", True)),
        request_id=context.request_id,
        source="gateway",
        resume_context=_gateway_resume_context(request, conversation),
        recovery_next_actions=[
            "If this gateway request must be recovered, inspect the gateway response and LocalStore gateway_request records first."
        ],
        recovery_content_paths=[str(context.request_path), str(context.response_path)],
        on_chunk=context.on_chunk,
        root_user_prompt=inputs.prompt,
        task_attributes=_gateway_run_task_attributes(
            conversation,
            request,
            context.request_id,
        ),
        context_scope="conversation" if conversation.thread_id else "default",
        carried_archive_tool_calls=[dict(item) for item in inputs.carried_archive_tool_calls],
        carried_active_turn_user_inputs=[
            dict(item) for item in inputs.carried_active_turn_user_inputs
        ],
        conversation_task_binding_callback=_GatewayTaskBindingWriter(
            context.request_path,
            context.request_id,
        ),
    )


def _persist_gateway_request_task_binding(
    request_path: Path,
    request_id: str,
    *,
    thread_id: str,
    task_id: str,
    task_path: str,
) -> bool:
    """Atomically bind one claimed request to the durable task it is executing."""
    expected_id = str(request_id or "").strip()
    selected_task_id = str(task_id or "").strip()
    selected_thread_id = str(thread_id or "").strip()
    if not expected_id or not selected_task_id or not selected_thread_id:
        return False
    updated = False

    def updater(current: dict) -> dict:
        nonlocal updated
        current_id = str(current.get("id") or current.get("request_id") or request_path.stem)
        if current_id != expected_id:
            return current
        current["conversation_runtime"] = {
            "thread_id": selected_thread_id,
            "task_id": selected_task_id,
            "task_path": str(task_path or ""),
        }
        updated = True
        return current

    try:
        update_json_file_atomic(request_path, updater, require_existing=True)
    except (OSError, FileNotFoundError, TypeError):
        return False
    return updated


def _gateway_injections(request: dict, conversation: _GatewayConversationContext) -> list[str]:
    items = [str(item) for item in request.get("inject", [])]
    section = _conversation_prompt_section(
        conversation,
        work_scope=_gateway_message_work_scope(request),
    )
    audit_prepare_section = _audit_prepare_prompt_section(request)
    audit_runtime_section = _audit_runtime_prompt_section(request)
    return [
        *items,
        *([section] if section else []),
        *([audit_prepare_section] if audit_prepare_section else []),
        *([audit_runtime_section] if audit_runtime_section else []),
    ]


def _gateway_message_work_scope(request: object) -> dict[str, object]:
    """Return the exact typed named-work attribution for one Gateway turn.

    The slash parser and durable Audit reservation own these facts.  Message
    history never derives them from prose, so interleaved named Audits can
    share one transcript without treating sibling requirements as authority.
    """

    row = request if isinstance(request, dict) else {}
    attrs = conversation_task_attributes(row.get("system_task"))
    work_kind = str(attrs.get(CONVERSATION_WORK_KIND_ATTR) or "").strip().lower()
    work_name = str(attrs.get(CONVERSATION_WORK_NAME_ATTR) or "").strip()
    if work_kind != "audit" or not work_name:
        return {}
    scope = row.get("conversation_audit_scope")
    scope = scope if isinstance(scope, dict) else {}
    runtime = row.get("conversation_runtime")
    runtime = runtime if isinstance(runtime, dict) else {}
    task_id = str(scope.get("audit_id") or runtime.get("task_id") or "").strip()
    selected: dict[str, object] = {
        CONVERSATION_WORK_KIND_ATTR: work_kind,
        CONVERSATION_WORK_NAME_ATTR: work_name,
    }
    if task_id:
        selected["conversation_task_id"] = task_id
    if attrs.get(CONVERSATION_AUDIT_PREPARE_ATTR) is True:
        selected[CONVERSATION_AUDIT_PREPARE_ATTR] = True
    return selected


def _is_scoped_audit_prepare(work_scope: object) -> bool:
    scope = work_scope if isinstance(work_scope, dict) else {}
    return (
        scope.get(CONVERSATION_AUDIT_PREPARE_ATTR) is True
        and str(scope.get(CONVERSATION_WORK_KIND_ATTR) or "").strip().lower() == "audit"
        and bool(str(scope.get(CONVERSATION_WORK_NAME_ATTR) or "").strip())
    )


def _history_row_visible_in_work_scope(
    metadata: object,
    work_scope: object,
) -> bool:
    """Hide only typed sibling Audit turns from an exact prepare projection."""

    if not _is_scoped_audit_prepare(work_scope):
        return True
    row = metadata if isinstance(metadata, dict) else {}
    row_kind = str(row.get(CONVERSATION_WORK_KIND_ATTR) or "").strip().lower()
    if row_kind != "audit":
        return True
    current = work_scope if isinstance(work_scope, dict) else {}
    current_task_id = str(current.get("conversation_task_id") or "").strip()
    row_task_id = str(row.get("conversation_task_id") or "").strip()
    if current_task_id and row_task_id:
        return row_task_id == current_task_id
    current_name = str(current.get(CONVERSATION_WORK_NAME_ATTR) or "").strip()
    row_name = str(row.get(CONVERSATION_WORK_NAME_ATTR) or "").strip()
    return bool(current_name and row_name and row_name == current_name)


def _audit_prepare_prompt_section(request: dict) -> str:
    task_attrs = conversation_task_attributes(request.get("system_task"))
    if task_attrs.get(CONVERSATION_AUDIT_PREPARE_ATTR) is not True:
        return ""
    scope = request.get("conversation_audit_scope")
    if not isinstance(scope, dict):
        return ""
    public = {
        "name": str(scope.get("name") or ""),
        "status": str(scope.get("status") or ""),
        "effective_prompt": str(scope.get("effective_prompt") or ""),
        "pending_prompt": str(scope.get("pending_prompt") or ""),
        "effective_revision": int(scope.get("effective_revision") or 0),
        "run_epoch": max(0, int(scope.get("run_epoch") or 0)),
        "source_bindings": public_audit_source_bindings(scope.get("source_bindings")),
    }
    return "\n".join(
        [
            "# Current Audit Preparation Scope",
            "- This turn belongs only to the exact Audit below and still uses the normal conversation and agent loop.",
            "- Every requirement in this slash-command turn is scoped only to this named Audit. It is not an owner persona or long-term user-memory update; do not emit or apply persona/memory mutations from this turn.",
            "- Answer questions, inspect documents, write probes, or run tests as the user asks.",
            "- Do not claim that discussion or a successful test changed the running Audit.",
            "- Only publish_audit_update can replace the effective Audit prompt; use it only when the user explicitly asks to apply the prepared change.",
            "- Publication preserves this turn's pending_prompt verbatim as the business authority; effective_prompt is derived validation context and cannot override that user text.",
            "- When verified source transports change, pass only the exact successful watch_id values returned by watch_stream(open) as source_probe_refs; the host copies the persisted transport facts. Omit source_probe_refs when only the judgment instructions change.",
            "- Notes, scripts, tests, skills and documents may be created in this Audit workspace as ordinary Agent work. The host does not require a business-document template; source_profile_ref is optional.",
            "- effective_prompt is durable operating context. Probe observations stay validation evidence unless the Agent deliberately turns a verified stable fact into an ordinary workspace note or instruction.",
            json.dumps(public, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        ]
    )


# LLM: Active Audit source ids come from the ingress-reserved durable link, not
# from user prose or a model-rewritten URL.  This block exposes only the safe
# dispatch handles needed by the ordinary root Agent; transport secrets and the
# actual open parameters stay inside trusted tool completion.
# 函数用途: 在 Audit 启动轮次告诉主代理有哪些已发布来源可派工，不让它重抄请求地址和游标。
def _audit_runtime_prompt_section(request: dict) -> str:
    task_attrs = conversation_task_attributes(request.get("system_task"))
    if task_attrs.get(AUDIT_ATTR) is not True:
        return ""
    scope = request.get("conversation_audit_scope")
    if not isinstance(scope, dict):
        return ""
    bindings = public_audit_source_bindings(scope.get("source_bindings"))
    sources = [
        {
            "source_id": str(item.get("source_id") or ""),
            "source_profile_ref": str(item.get("source_profile_ref") or ""),
            "document_refs": [
                str(ref) for ref in item.get("document_refs", []) or [] if str(ref or "").strip()
            ],
        }
        for item in bindings
        if str(item.get("source_id") or "").strip()
    ]
    if not sources:
        return ""
    public = {
        "name": str(scope.get("name") or ""),
        "effective_revision": int(scope.get("effective_revision") or 0),
        "sources": sources,
    }
    return "\n".join(
        [
            "# Current Audit Runtime Scope",
            "- The named Audit already has exact host-verified source transports.",
            "- The host reconciles one direct leaf worker for each source_id below through the canonical create_subagents lifecycle before the root model turn.",
            "- Coordinate or inspect those workers. Do not open sources from the root turn and do not create duplicate source leaves; additional investigation or summary workers remain your decision.",
            "- Do not restate or guess URLs, request bodies, cursor fields, watch ids, or source transport parameters; the Tool Gateway supplies them after the source_id is selected.",
            "- If the effective context contains derived notes and a verbatim user prepare section, the verbatim user section wins on business meaning; lifecycle state still comes from the host command state.",
            json.dumps(public, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        ]
    )


# LLM: Stamp the exact sticky workspace lineage on every turn.  Promotion creates a fresh ordinary
# execution over terminal history, while an exact paused /goal may resume in place.
# 函数用途: 生成本轮结构化会话参数；同一会话直接续用目录，无需让模型选择历史任务。
def _gateway_task_attributes(conversation: _GatewayConversationContext) -> dict | None:
    attrs: dict[str, object] = {}
    if conversation.thread_id:
        attrs["conversation_thread_id"] = conversation.thread_id
        attrs[CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR] = True
    if conversation.workspace_task is not None:
        task = conversation.workspace_task
        attrs[CONVERSATION_WORKSPACE_TASK_ID_ATTR] = task.task_id
        attrs[CONVERSATION_WORKSPACE_TASK_STATUS_ATTR] = task.status
        attrs[CONVERSATION_WORKSPACE_EXECUTION_RUNNING_ATTR] = task.execution_running
        attrs[CONVERSATION_WORKSPACE_EXECUTION_STATE_AVAILABLE_ATTR] = (
            task.execution_state_available
        )
        attrs["conversation_task_id"] = task.task_id
        attrs["run_workspace"] = {
            "task_root": task.task_path,
            "output_dir": str(Path(task.task_path) / "output"),
            "work_dir": str(Path(task.task_path) / "work"),
        }
    return attrs or None


def _gateway_resume_context(
    request: dict,
    conversation: _GatewayConversationContext,
) -> bool | None:
    """会话历史已是权威上下文时，默认不再自动续接 owner 旧归档。"""
    if "resume_context" in request:
        return bool(request.get("resume_context"))
    return False if conversation.thread_id else None


def _apply_system_task_attributes(
    attrs: dict | None,
    system_task: object,
) -> dict | None:
    """Merge one ingress-validated task mode without re-reading slash text."""
    task_attrs = conversation_task_attributes(system_task)
    if not task_attrs:
        return attrs
    return {**dict(attrs or {}), **task_attrs}


def _gateway_run_task_attributes(
    conversation: _GatewayConversationContext,
    request: dict,
    request_id: str,
) -> dict | None:
    """Project the exact ingress-reserved named task into the current model turn."""
    attrs = _apply_system_task_attributes(
        _gateway_task_attributes(conversation),
        request.get("system_task"),
    )
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


def _preflight_gateway_conversation(
    inputs: _GatewayConversationLoadRequest,
) -> _GatewayConversationContext:
    """Resolve only the durable thread identity before reserving its run lane."""
    spec = inputs.request.get("conversation")
    if not isinstance(spec, dict):
        return _GatewayConversationContext()
    store = getattr(inputs.agent, "conversation_store", None)
    if store is None:
        return _GatewayConversationContext(
            load_errors=({"error_code": "conversation_store_unavailable"},)
        )
    thread, error = _load_gateway_thread(inputs, store, spec)
    if error is not None or thread is None:
        return _GatewayConversationContext(
            load_errors=(error or {"error_code": "thread_unavailable"},)
        )
    return _GatewayConversationContext(thread_id=str(thread.thread_id or ""))


# LLM: 同一 thread 的历史与近期产物分别加载；内部 run 索引不进入模型上下文。
# 函数用途: 组装本轮 Gateway 对话所需的权威历史、工作目录和产物上下文。
def _gateway_conversation_context(
    inputs: _GatewayConversationLoadRequest,
    *,
    force_compact: bool = False,
) -> _GatewayConversationContext:
    agent = inputs.agent
    spec = inputs.request.get("conversation")
    if not isinstance(spec, dict):
        return _GatewayConversationContext()
    store = getattr(agent, "conversation_store", None)
    if store is None:
        return _GatewayConversationContext(
            load_errors=({"error_code": "conversation_store_unavailable"},)
        )
    load_errors: list[dict] = []
    thread, thread_error = _load_gateway_thread(inputs, store, spec)
    if thread_error is not None or thread is None:
        return _GatewayConversationContext(
            load_errors=(thread_error or {"error_code": "thread_unavailable"},)
        )
    _repair_gateway_conversation_messages(store, thread.thread_id, load_errors)
    scope = conversation_scope(agent, thread, spec)
    _ensure_gateway_conversation_index(agent, store, thread.thread_id)
    thread, history_rows, history_token_budget, recent_operation_evidence = (
        _load_gateway_compact_context(
            inputs,
            store,
            thread,
            load_errors,
            force=force_compact,
        )
    )
    history, recent_artifacts = _gateway_conversation_refs(
        agent,
        thread.thread_id,
        inputs.request_id,
        load_errors,
        history_rows=history_rows,
        history_token_budget=history_token_budget,
        work_scope=_gateway_message_work_scope(inputs.request),
    )
    thread_goal = _gateway_thread_goal(store, thread.thread_id, load_errors)
    named_work = _gateway_named_work(store, thread.thread_id, load_errors)
    workspace_task = _gateway_workspace_task(
        store,
        thread,
        thread_goal,
        load_errors,
    )
    return _GatewayConversationContext(
        thread_id=thread.thread_id,
        compact_summary=thread.summary,
        compact_operation_evidence=dict(getattr(thread, "compact_operation_evidence", {}) or {}),
        compact_operation_evidence_ref=_compact_operation_evidence_ref(
            agent,
            thread.thread_id,
        ),
        recent_operation_evidence=recent_operation_evidence,
        compact_generation=thread.compact_generation,
        verbose_level=thread.verbose_level,
        scope=scope,
        history=history,
        recent_artifacts=recent_artifacts,
        workspace_task=workspace_task,
        thread_goal=thread_goal,
        named_work=named_work,
        load_errors=tuple(load_errors),
    )


def _load_gateway_thread(
    inputs: _GatewayConversationLoadRequest,
    store: object,
    spec: dict,
) -> tuple[object | None, dict | None]:
    """Load the scoped thread without mixing persistence errors into prompt assembly."""
    try:
        thread = store.get_or_create_thread(
            {
                "canonical_user_id": str(spec.get("canonical_user_id") or "local-agent"),
                "owner_id": str(
                    getattr(getattr(inputs.agent, "home_paths", None), "owner_id", "") or ""
                ),
                "owner_home": str(
                    getattr(getattr(inputs.agent, "home_paths", None), "owner_home_dir", "") or ""
                ),
                "channel": str(spec.get("channel") or "chat"),
                "channel_conversation_id": str(spec.get("channel_conversation_id") or ""),
                "channel_user_id": str(spec.get("channel_user_id") or "local-cli"),
                "title": inputs.prompt[:80] or inputs.request_id,
            }
        )
    except Exception as exc:
        return None, _conversation_error(exc, "gateway.conversation.thread")
    return thread, None


def _load_gateway_compact_context(
    inputs: _GatewayConversationLoadRequest,
    store: object,
    thread: object,
    load_errors: list[dict],
    *,
    force: bool = False,
) -> tuple[object, object, int, dict[str, object]]:
    """Prepare compact state and preserve the original thread on a reported failure."""
    try:
        compact = prepare_conversation_context(
            inputs.agent,
            store,
            thread,
            current_prompt=inputs.prompt,
            exclude_request_id=inputs.request_id,
            force=force,
        )
    except Exception as exc:
        load_errors.append(_conversation_error(exc, "gateway.conversation.compact"))
        return thread, (), 0, {}
    return (
        compact.thread,
        compact.messages,
        compact.trigger_tokens,
        dict(compact.recent_operation_evidence or {}),
    )


# LLM: The full structured ledger stays owner-local and append-only; the prompt receives only a
# bounded projection plus this exact ref. This follows the same refs-first rule as large tool
# output and avoids turning historical verification into an ever-growing fixed prompt prefix.
# 函数用途: 返回当前会话完整 Compact 操作证据的 owner 内路径，供模型按需读取而非常驻展开。
def _compact_operation_evidence_ref(agent: object, thread_id: str) -> str:
    home = getattr(agent, "home_paths", None)
    root = str(getattr(home, "owner_compact_dir", "") or "").strip()
    if not root or not str(thread_id or "").strip():
        return ""
    path = Path(root) / "conversations" / f"{thread_id}.jsonl"
    return str(path) if path.is_file() else ""


# LLM: An ordinary foreground turn may project one unambiguous goal as background
# context. Multiple named goals stay out of the ordinary prompt; their exact
# continuation turns carry ``thread_goal_id`` and `/status` lists them all.
# 函数用途: 普通聊天只在目标唯一时注入摘要；多个命名目标不猜“当前目标”，也不误报会话损坏。
def _gateway_thread_goal(
    store: object, thread_id: str, load_errors: list[dict]
) -> dict[str, object] | None:
    try:
        goals, error = store.load_goals_report(thread_id)
    except Exception as exc:
        load_errors.append(_conversation_error(exc, "gateway.conversation.goal"))
        return None
    if error is not None:
        load_errors.append(error)
        return None
    unfinished = [goal for goal in goals if str(getattr(goal, "status", "") or "") != "complete"]
    if len(unfinished) != 1:
        return None
    goal = unfinished[0]
    return {
        "goal_id": str(getattr(goal, "goal_id", "") or ""),
        "task_id": str(getattr(goal, "task_id", "") or ""),
        "name": str(getattr(goal, "name", "") or ""),
        "objective": str(getattr(goal, "objective", "") or ""),
        "status": str(getattr(goal, "status", "") or ""),
        "token_budget": getattr(goal, "token_budget", None),
        "tokens_used": int(getattr(goal, "tokens_used", 0) or 0),
        "time_used_seconds": int(getattr(goal, "time_used_seconds", 0) or 0),
    }


def _gateway_named_work(
    store: object,
    thread_id: str,
    load_errors: list[dict],
) -> tuple[dict[str, str], ...]:
    """Project only active user-visible names, never an implicit current objective."""
    try:
        links, link_errors = store.task_links_report(thread_id)
        goals, goal_error = store.load_goals_report(thread_id)
    except Exception as exc:
        load_errors.append(_conversation_error(exc, "gateway.conversation.named_work"))
        return ()
    if link_errors:
        load_errors.extend(error for error in link_errors if isinstance(error, dict))
        return ()
    if goal_error is not None:
        load_errors.append(goal_error)
        return ()
    terminal_links = {
        "abandoned",
        "cancelled",
        "channel_error",
        "completed",
        "done",
        "failed",
        "superseded",
        "taken_over",
        "timeout",
    }
    items = [
        {
            "kind": "audit",
            "name": str(getattr(link, "work_name", "") or "").strip(),
            "status": str(getattr(link, "status", "") or "").strip().lower(),
        }
        for link in links
        if str(getattr(link, "work_kind", "") or "").strip().lower() == "audit"
        and str(getattr(link, "work_name", "") or "").strip()
        and str(getattr(link, "status", "") or "").strip().lower() not in terminal_links
    ]
    items.extend(
        {
            "kind": "goal",
            "name": str(getattr(goal, "name", "") or "").strip(),
            "status": str(getattr(goal, "status", "") or "").strip().lower(),
        }
        for goal in goals
        if str(getattr(goal, "name", "") or "").strip()
        and str(getattr(goal, "status", "") or "").strip().lower() != "complete"
    )
    return tuple(
        sorted(
            items,
            key=lambda item: (item["kind"], item["name"].casefold()),
        )
    )


# LLM: Resolve the sticky workspace from ConversationThread.workspace_task_id. For pre-v3 records,
# migrate only an exact goal task or one unambiguous root task; never inspect the user prompt.
# 函数用途: 找出该会话下一轮默认进入的原任务目录，并对旧数据做无歧义兼容。
def _gateway_workspace_task(
    store: object,
    thread: object,
    thread_goal: dict[str, object] | None,
    load_errors: list[dict],
) -> _GatewayWorkspaceSelection | None:
    thread_id = str(getattr(thread, "thread_id", "") or "").strip()
    try:
        links, errors = store.task_links_report(thread_id)
    except Exception as exc:
        load_errors.append(_conversation_error(exc, "gateway.conversation.workspace_task"))
        return None
    if errors:
        load_errors.extend(error for error in errors if isinstance(error, dict))
        return None
    from ..conversation.task_promotion import is_reusable_conversation_workspace

    # A detached named task owns its own execution lane and workspace.  It may
    # remain active while the parent conversation starts unrelated work, so it
    # must never become the parent turn's implicit cwd merely because it was the
    # last task to materialize a directory.
    selectable = [
        link
        for link in links
        if is_reusable_conversation_workspace(link)
        and str(getattr(link, "cancellation_scope", "") or "").strip().lower() != "detached"
    ]
    selected_id = str(getattr(thread, "workspace_task_id", "") or "").strip()
    selected = None
    strict_selection = bool(selected_id)
    if selected_id:
        selected = next(
            (link for link in links if str(getattr(link, "task_id", "") or "") == selected_id),
            None,
        )
        if selected is None:
            load_errors.append(
                _conversation_error(
                    ValueError(f"sticky conversation workspace task is missing: {selected_id}"),
                    "gateway.conversation.workspace_task",
                )
            )
        elif selected not in selectable:
            # sticky 指向的任务已不可复用(如 superseded/取消)时回退常规选择,
            # 不能因此让本轮开新任务目录——真机(2026-08-08):用户连续消息被
            # 截断成新任务名,模型在新目录找不到旧产物,复刻任务停摆。
            selected = None
            strict_selection = False
    goal_task_id = str((thread_goal or {}).get("task_id") or "").strip()
    if goal_task_id:
        selected = next(
            (
                link
                for link in selectable
                if str(getattr(link, "task_id", "") or "") == goal_task_id
            ),
            None,
        )
        strict_selection = strict_selection or selected is not None
    if selected is None:
        # 同路径的多条历史链接算一个工作区;去重后唯一才可隐式继承,
        # 否则同一目录反复续跑会被 len>1 误判为"多个候选"而开新任务。
        unique_paths = {
            path: link
            for link in selectable
            if (path := _existing_gateway_workspace_path(getattr(link, "task_path", "")))
        }
        if len(unique_paths) == 1:
            selected = next(iter(unique_paths.values()))
        elif len(unique_paths) > 1:
            # 多个候选工作区时同会话普通消息默认延续最近创建的可复用任务,
            # 不因多候选而开新任务目录(开新任务会让用户消息截断成目录名,
            # 模型在新目录找不到旧产物,真机 2026-08-08 scrapy 复刻停摆三连)。
            selected = next(reversed(list(unique_paths.values())))
    if selected is None:
        return None
    task_path = _existing_gateway_workspace_path(getattr(selected, "task_path", ""))
    if not task_path:
        if strict_selection:
            load_errors.append(
                _conversation_error(
                    ValueError(
                        "sticky conversation workspace path is missing or not a directory: "
                        f"{getattr(selected, 'task_id', '')}"
                    ),
                    "gateway.conversation.workspace_task",
                )
            )
        return None
    from ..conversation.task_promotion import conversation_task_execution_state

    execution = conversation_task_execution_state(
        store,
        thread_id,
        str(getattr(selected, "task_id", "") or ""),
    )
    return _GatewayWorkspaceSelection(
        task_id=str(getattr(selected, "task_id", "") or ""),
        status=str(getattr(selected, "status", "") or ""),
        goal=str(getattr(selected, "goal", "") or ""),
        task_path=task_path,
        execution_running=execution.get("running") is True,
        execution_state_available=execution.get("state_available") is True,
        execution_sources=tuple(str(item) for item in execution.get("sources", []) if str(item)),
    )


# LLM: Workspace inheritance accepts only an existing directory from the exact owner-local task
# link. It resolves symlinks once so all downstream boundaries receive one canonical path.
# 函数用途: 校验并规范化会话任务目录；路径不存在或不是目录时返回空。
def _existing_gateway_workspace_path(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        path = Path(text).expanduser().resolve(strict=True)
    except OSError:
        return ""
    return str(path) if path.is_dir() else ""


# LLM: 对话正文和产物引用都来自同一 thread，但保持两种 typed 结果，禁止把 path 混进历史正文。
# 函数用途: 读取本轮所需的有界历史与近期产物引用。
def _gateway_conversation_refs(
    agent: SimpleAgent,
    thread_id: str,
    request_id: str,
    load_errors: list[dict],
    *,
    history_rows: object = None,
    history_token_budget: int = 0,
    work_scope: dict[str, object] | None = None,
) -> tuple[tuple[tuple[str, str], ...], tuple[dict[str, object], ...]]:
    history = _gateway_conversation_history(
        agent,
        thread_id,
        request_id,
        load_errors,
        rows=history_rows,
        token_budget=history_token_budget,
        work_scope=work_scope,
    )
    artifacts = _gateway_recent_artifacts(agent, thread_id, request_id, load_errors)
    return history, artifacts


def _ensure_gateway_conversation_index(
    agent: SimpleAgent,
    store: object,
    thread_id: str,
) -> None:
    """Best-effort rebuild of the owner-local search projection; raw transcript stays authoritative."""
    try:
        ensure_thread_history_indexed(agent, store, thread_id)
    except Exception as exc:
        logger.warning(
            "会话搜索索引更新失败(thread=%s): %s: %s",
            thread_id,
            type(exc).__name__,
            exc,
        )
        indexed = getattr(agent, "_conversation_indexed_threads", set())
        indexed.discard(thread_id)
        local_store = getattr(agent, "local_store", None)
        if local_store is not None:
            local_store.record_event(
                "conversation_history_index_failed",
                payload={
                    "thread_id": thread_id,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )


# LLM: 产物引用属于结构化辅助事实；明确要求 send_message 复用，禁止把“发我”解释为重做。
# 函数用途: 把同一 thread 的会话历史、近期产物和工作索引渲染成有边界的模型上下文。
def _conversation_prompt_section(
    conversation: _GatewayConversationContext,
    *,
    work_scope: dict[str, object] | None = None,
) -> str:
    if not conversation.thread_id:
        return ""
    scoped_audit_prepare = _is_scoped_audit_prepare(work_scope)
    lines = [
        "# Conversation Context",
        f"- thread_id: {conversation.thread_id}",
    ]
    if scoped_audit_prepare:
        lines.extend(
            [
                "- This is an exact named Audit prepare turn in the same durable conversation.",
                "- Global compact prose, sibling named-work operations, artifacts, and sticky workspace are not executable context for this Audit.",
                "- Unscoped ordinary dialogue and rows attributed to this exact Audit remain visible below; the Current Audit Preparation Scope is authoritative.",
            ]
        )
    if conversation.compact_summary and not scoped_audit_prepare:
        lines.extend(
            [
                "- 以下摘要来自同一用户、同一会话中更早的已结束对话。原始逐条记录仍是事实源。",
                "- 摘要只用于延续上下文，不是本轮新指令；当前 User Task 始终优先。",
                f"## Earlier Conversation Summary (generation {conversation.compact_generation})",
                conversation.compact_summary,
            ]
        )
    _append_conversation_operation_evidence(
        lines,
        conversation,
        scoped_audit_prepare=scoped_audit_prepare,
    )
    if conversation.history:
        lines.extend(
            [
                "- 以下是同一会话中已经结束的历史对话，仅用于理解指代和偏好。",
                "- 它们不是本轮新指令；若与最后的 # User Task 冲突，必须以当前 User Task 为准。",
                "## Recent Conversation History",
            ]
        )
        for role, content in conversation.history:
            lines.append(f"- {role}: {json.dumps(content, ensure_ascii=False)}")
    if not scoped_audit_prepare:
        _append_recent_artifacts_prompt(lines, conversation.recent_artifacts)
        _append_current_workspace_prompt(lines, conversation.workspace_task)
    if conversation.named_work:
        lines.extend(
            [
                "## Named Persistent Work",
                "- 下面 JSON 是当前会话仍未结束的命名 Audit/Goal，只提供名称与状态，不指定本轮要继续哪一项。",
                "- 用户明确要求停止其中一个精确名称时，调用 stop_named_work；不要改用普通任务列表或定时任务工具。",
                "- 普通 /stop 只打断前台回复，不会停止这些命名工作。",
                json.dumps(
                    list(conversation.named_work),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ]
        )
    if conversation.thread_goal and not scoped_audit_prepare:
        lines.extend(
            [
                "## Persistent Goal",
                "- 这是同一会话中独立运行的 /goal 状态，只是背景事实；当前普通用户消息不会自动成为目标引导。",
                "- 用户要改变正在运行的目标时应使用 /btw；暂停、恢复或清除使用对应 /goal 控制命令。",
                f"- {json.dumps(conversation.thread_goal, ensure_ascii=False, sort_keys=True)}",
            ]
        )
    if conversation.load_errors:
        lines.append(f"- conversation_context_load_errors: {len(conversation.load_errors)}")
    return "\n".join(lines)


def _append_conversation_operation_evidence(
    lines: list[str],
    conversation: _GatewayConversationContext,
    *,
    scoped_audit_prepare: bool,
) -> None:
    if scoped_audit_prepare:
        return
    if conversation.compact_operation_evidence:
        prompt_evidence = _prompt_operation_evidence(conversation.compact_operation_evidence)
        lines.extend(
            [
                "## Program-Verified Operations From Compacted History",
                "- 下面 JSON 是完整程序账本的有界投影，不是模型摘要或聊天自述。",
                "- 凡涉及是否真正保存、修改、发送、创建或删除，若与上方摘要冲突，必须以此 JSON 为准。",
                (
                    f"- full_operation_evidence_ref: {conversation.compact_operation_evidence_ref}"
                    if conversation.compact_operation_evidence_ref
                    else "- full_operation_evidence_ref: unavailable"
                ),
                json.dumps(
                    prompt_evidence,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ]
        )
    if conversation.recent_operation_evidence:
        lines.extend(
            [
                "## Program-Verified Operations From Recent Raw History",
                "- 下面 JSON 是尚未压缩的近期 assistant metadata 的有界投影，与上面的 compact 证据同为程序事实。",
                json.dumps(
                    _prompt_operation_evidence(conversation.recent_operation_evidence),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ]
        )


# LLM: Aggregate counts remain complete while only the newest detailed events stay hot. Full
# events are never deleted: the compact checkpoint ref above remains the authoritative source.
# 函数用途: 将操作证据压成固定上限的模型视图，保留完整总数并标明省略的详细事件数。
def _prompt_operation_evidence(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    events = [item for item in value.get("events", []) if isinstance(item, dict)]
    kept = events[-_MAX_PROMPT_OPERATION_EVIDENCE_EVENTS:]
    projection = {
        key: value.get(key)
        for key in (
            "schema",
            "coverage",
            "assistant_message_count",
            "verified_assistant_message_count",
            "unverified_assistant_message_count",
            "operation_event_count",
            "operation_count",
            "counts",
        )
        if key in value
    }
    already_omitted = max(0, _safe_nonnegative_int(value.get("omitted_event_count")))
    projection.update(
        {
            "events": kept,
            "omitted_event_count": already_omitted + max(0, len(events) - len(kept)),
            "prompt_event_limit": _MAX_PROMPT_OPERATION_EVIDENCE_EVENTS,
        }
    )
    return projection


def _safe_nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


# LLM: This prompt exposes one sticky cwd, not a menu of historical task records.  The newest user
# message owns the next turn exactly as it does in 会话运行时.
# 函数用途: 告诉模型会话和目录连续，当前用户消息直接决定本轮，不需要开关或选择旧任务。
def _append_current_workspace_prompt(
    lines: list[str],
    workspace: _GatewayWorkspaceSelection | None,
) -> None:
    if workspace is None:
        return
    status = str(workspace.status or "").strip().lower()
    if status == "active" and workspace.execution_running:
        lifecycle_guidance = (
            "- 当前目录已有结构化执行者；可以正常聊天或给当前运行补充引导，"
            "但不得在同一目录另起一个并发写入者。"
        )
    else:
        lifecycle_guidance = (
            "- 当前没有占用该目录的执行者；若本轮需要工作，直接按当前 User Task 调用工具。"
        )
    lines.extend(
        [
            "## Current Workspace",
            "- 这是该 thread 跨轮继承的工作目录，语义与 Codex thread 的持续 cwd 一致。",
            "- 普通聊天、代码修改和其他工作都在同一个会话历史里；当前 User Task 直接决定本轮做什么。",
            "- 不要要求用户选择、开始、完成或关闭历史任务。task_progress 只是可选进度笔记，不控制后续轮次。",
            lifecycle_guidance,
            (
                f"- task_path={json.dumps(workspace.task_path, ensure_ascii=False)} "
                f"execution_running={json.dumps(workspace.execution_running)} "
                f"execution_state_available={json.dumps(workspace.execution_state_available)}"
            ),
        ]
    )


# LLM: 最近产物区明确指示复用 send_message；它是结构化事实，不改变当前用户指令。
# 函数用途: 把近期产物引用追加到模型会话段落。
def _append_recent_artifacts_prompt(
    lines: list[str],
    artifacts: tuple[dict[str, object], ...],
) -> None:
    if not artifacts:
        return
    lines.extend(
        [
            "## Recent Artifact Refs",
            "- 这些是同一会话中上一轮已经生成并登记的可信产物，不是要求你重新生成的任务。",
            "- 用户说‘发我/把上一个文件给我’时，直接调用 send_message，并把对应 path 放进 "
            "attachments；不要重新搜索、复制或制作一遍。",
        ]
    )
    for artifact in artifacts:
        lines.append(f"- {json.dumps(artifact, ensure_ascii=False, sort_keys=True)}")


# LLM: 读取旧 assistant 时再次应用 user projection，兼容升级前已落盘的内部完成协议。
# 函数用途: 返回同一 thread 的有界用户可见历史，不把机器协议重新注入模型。
def _gateway_conversation_history(
    agent: SimpleAgent,
    thread_id: str,
    current_request_id: str,
    load_errors: list[dict],
    *,
    rows: object = None,
    token_budget: int = 0,
    work_scope: dict[str, object] | None = None,
) -> tuple[tuple[str, str], ...]:
    store = getattr(agent, "conversation_store", None)
    config = getattr(agent, "config", None)
    max_turns = max(
        1,
        int(getattr(config, "conversation_history_max_turns", 20) or 20),
    )
    total_chars = max(
        1000,
        int(getattr(config, "conversation_history_max_chars", 48_000) or 48_000),
    )
    message_chars = max(
        1000,
        int(getattr(config, "conversation_history_message_max_chars", 12_000) or 12_000),
    )
    supplied_rows = isinstance(rows, (list, tuple))
    if supplied_rows:
        message_rows = list(rows)
    else:
        try:
            message_rows, errors = store.recent_messages_report(
                thread_id,
                limit=max_turns * 2 + 8,
            )
        except Exception as exc:
            load_errors.append(_conversation_error(exc, "gateway.conversation.messages"))
            return ()
        load_errors.extend(error for error in errors if isinstance(error, dict))
    candidates: list[tuple[str, str]] = []
    for row in message_rows:
        role = str(getattr(row, "role", "") or "").strip().lower()
        metadata = getattr(row, "metadata", None)
        metadata = metadata if isinstance(metadata, dict) else {}
        if (
            role not in {"user", "assistant"}
            or metadata.get("gateway_request_id") == current_request_id
            or is_audit_background_transcript_entry(row)
            or not _history_row_visible_in_work_scope(metadata, work_scope)
        ):
            continue
        content = str(getattr(row, "content", "") or "")
        if role == "assistant":
            content = project_user_reply(content).content
        candidates.append((role, _clip_conversation_message(content, message_chars)))
    history_chars = total_chars
    history_messages = max_turns * 2
    if supplied_rows:
        history_chars = max(history_chars, max(0, int(token_budget)) * 3)
        history_messages = max(history_messages, len(candidates))
    return _latest_conversation_messages(
        candidates,
        max_messages=history_messages,
        max_chars=history_chars,
    )


# LLM: 近期产物只从当前 thread 的 assistant metadata 或旧版完整完成协议恢复；普通对话文字不获此权威。
# 函数用途: 为“把上一个文件发我”提供已登记产物引用，避免模型重新搜索、复制或生成。
def _gateway_recent_artifacts(
    agent: SimpleAgent,
    thread_id: str,
    current_request_id: str,
    load_errors: list[dict],
) -> tuple[dict[str, object], ...]:
    store = getattr(agent, "conversation_store", None)
    try:
        rows, errors = store.recent_messages_report(thread_id, limit=80)
    except Exception as exc:
        load_errors.append(_conversation_error(exc, "gateway.conversation.recent_artifacts"))
        return ()
    load_errors.extend(error for error in errors if isinstance(error, dict))
    selected: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    candidates = [
        ref
        for row in reversed(rows)
        for ref in reversed(_conversation_row_artifacts(row, current_request_id))
    ]
    for ref in candidates:
        key = (str(ref.get("artifact_id") or ""), str(ref.get("path") or ""))
        if key in seen:
            continue
        seen.add(key)
        selected.append(ref)
        if len(selected) >= 20:
            return tuple(reversed(selected))
    return tuple(reversed(selected))


# LLM: 只有非当前请求的 assistant 行能贡献产物引用；权威来源只有结构化 metadata。
# 函数用途: 从一条会话消息提取可信的最小产物引用。
def _conversation_row_artifacts(row: object, current_request_id: str) -> list[dict[str, object]]:
    if str(getattr(row, "role", "") or "").strip().lower() != "assistant":
        return []
    metadata = getattr(row, "metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    if metadata.get("gateway_request_id") == current_request_id:
        return []
    return _metadata_artifact_refs(metadata.get("delivery_artifacts"))


# LLM: delivery_artifacts 写入会话 metadata 前只保留最小稳定字段；不得复制验收协议或任意嵌套对象。
# 函数用途: 清洗能跨轮复用的产物引用。
def _metadata_artifact_refs(value: object) -> list[dict[str, object]]:
    if not isinstance(value, (list, tuple)):
        return []
    refs: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        artifact_id = str(item.get("artifact_id") or "").strip()
        path = str(item.get("path") or "").strip()
        if not artifact_id and not path:
            continue
        key = (artifact_id, path)
        if key in seen:
            continue
        seen.add(key)
        refs.append(
            {
                "artifact_id": artifact_id,
                "path": path,
                "name": str(item.get("name") or Path(path).name or artifact_id),
                "kind": str(item.get("kind") or "file"),
                "ok": item.get("ok") is True,
            }
        )
    return refs


# LLM: 会话 metadata 只能保存 canonical public operation projection；不得复制内部 call/operation ID。
# 函数用途: 对 Gateway 正常落账和延迟补账使用同一份有界操作核验摘要。
def _metadata_operation_verification(value: object) -> dict[str, object]:
    return public_operation_verification(value)


def _clip_conversation_message(content: str, limit: int) -> str:
    if len(content) <= limit:
        return content
    marker = "\n…[中间内容已折叠]…\n"
    if limit <= len(marker) + 2:
        return content[:limit]
    head = (limit - len(marker)) // 2
    tail = limit - len(marker) - head
    return f"{content[:head]}{marker}{content[-tail:]}"


def _latest_conversation_messages(
    candidates: list[tuple[str, str]],
    *,
    max_messages: int,
    max_chars: int,
) -> tuple[tuple[str, str], ...]:
    selected: list[tuple[str, str]] = []
    used = 0
    for role, content in reversed(candidates[-max_messages:]):
        remaining = max_chars - used
        if remaining <= 0:
            break
        bounded = _clip_conversation_message(content, remaining)
        selected.append((role, bounded))
        used += len(bounded)
    selected.reverse()
    return tuple(selected)


# LLM: assistant 正文、delivery_artifacts 与 operation verification 分栏落账；metadata 只接受公开投影。
# 函数用途: 幂等追加一条 Gateway 会话消息，并保存可跨轮复用的产物和操作核验 metadata。
def _append_gateway_conversation_message(
    agent: SimpleAgent,
    request: dict,
    conversation: _GatewayConversationContext,
    *,
    request_id: str,
    role: str,
    content: str,
    delivery_artifacts: object = (),
    operation_verification: object = None,
) -> bool:
    if not conversation.thread_id or not content:
        return not conversation.thread_id
    store = getattr(agent, "conversation_store", None)
    metadata = request.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    channel_message_id = str(metadata.get("message_id") or "") if role == "user" else ""
    try:
        rows, errors = store.recent_messages_report(conversation.thread_id, limit=100)
        if errors:
            raise OSError("conversation message ledger could not be read reliably")
        if any(
            str(getattr(row, "role", "") or "") == role
            and (
                str((getattr(row, "metadata", {}) or {}).get("gateway_request_id") or "")
                == request_id
                or (
                    role == "user"
                    and channel_message_id
                    and str(getattr(row, "channel_message_id", "") or "") == channel_message_id
                )
            )
            for row in rows
        ):
            return True
        entry_metadata: dict[str, object] = {
            "gateway_request_id": request_id,
            "delivery_artifacts": _metadata_artifact_refs(delivery_artifacts),
        }
        entry_metadata.update(_gateway_message_work_scope(request))
        if role == "assistant" and operation_verification is not None:
            entry_metadata["operation_verification"] = _metadata_operation_verification(
                operation_verification
            )
        entry = store.append_message(
            {
                "thread_id": conversation.thread_id,
                "role": role,
                "content": content,
                "channel": str(metadata.get("channel") or request.get("source") or "gateway"),
                "channel_message_id": channel_message_id,
                "metadata": entry_metadata,
            }
        )
        try:
            index_conversation_message(agent, store, entry)
        except Exception as exc:
            logger.warning(
                "会话消息索引失败(thread=%s, message=%s): %s: %s",
                conversation.thread_id,
                entry.message_id,
                type(exc).__name__,
                exc,
            )
            indexed = getattr(agent, "_conversation_indexed_threads", set())
            indexed.discard(conversation.thread_id)
        return True
    except Exception as exc:
        logger.error(
            "会话消息持久化失败(thread=%s, role=%s): %s: %s",
            conversation.thread_id,
            role,
            type(exc).__name__,
            exc,
        )
        return False


# LLM: 延迟补账必须携带同一份净化正文、产物和操作核验 metadata，不能回退保存内部结果。
# 函数用途: assistant transcript 暂时写失败时保存可幂等修复的记录。
def _queue_gateway_conversation_repair(
    agent: SimpleAgent,
    request: dict,
    conversation: _GatewayConversationContext,
    *,
    request_id: str,
    role: str,
    content: str,
    delivery_artifacts: object = (),
    operation_verification: object = None,
) -> None:
    store = getattr(agent, "conversation_store", None)
    root = getattr(store, "root", None)
    if not root or not conversation.thread_id or not content:
        return
    metadata = request.get("metadata") if isinstance(request.get("metadata"), dict) else {}
    repair_metadata: dict[str, object] = {
        "gateway_request_id": request_id,
        "repair": True,
        "delivery_artifacts": _metadata_artifact_refs(delivery_artifacts),
    }
    repair_metadata.update(_gateway_message_work_scope(request))
    if role == "assistant" and operation_verification is not None:
        repair_metadata["operation_verification"] = _metadata_operation_verification(
            operation_verification
        )
    payload = {
        "thread_id": conversation.thread_id,
        "role": role,
        "content": content,
        "channel": str(metadata.get("channel") or request.get("source") or "gateway"),
        "channel_message_id": "",
        "metadata": repair_metadata,
    }
    path = Path(root) / "message_repairs" / f"{request_id}-{role}.json"
    try:
        from .io import write_json_file

        write_json_file(path, payload)
        indexed = getattr(agent, "_conversation_indexed_threads", set())
        indexed.discard(conversation.thread_id)
    except Exception as exc:
        logger.error("会话修复记录写入失败(%s): %s: %s", path, type(exc).__name__, exc)


def _repair_gateway_conversation_messages(
    store: object,
    thread_id: str,
    load_errors: list[dict],
) -> None:
    root = getattr(store, "root", None)
    repair_dir = Path(root) / "message_repairs" if root else None
    if repair_dir is None or not repair_dir.exists():
        return
    for path in sorted(repair_dir.glob("*.json"))[:100]:
        report = read_json_file_report(path, context="gateway.conversation.repair.read")
        if report.load_error is not None:
            load_errors.append(report.load_error)
            continue
        payload = report.payload
        if str(payload.get("thread_id") or "") != thread_id:
            continue
        try:
            rows, errors = store.recent_messages_report(thread_id, limit=200)
            if errors:
                load_errors.extend(errors)
                continue
            request_id = str((payload.get("metadata") or {}).get("gateway_request_id") or "")
            role = str(payload.get("role") or "")
            already_written = any(
                str(getattr(row, "role", "") or "") == role
                and str((getattr(row, "metadata", {}) or {}).get("gateway_request_id") or "")
                == request_id
                for row in rows
            )
            if not already_written:
                store.append_message(payload)
            path.unlink()
        except Exception as exc:
            load_errors.append(_conversation_error(exc, "gateway.conversation.repair.append"))


def _conversation_error(exc: BaseException, context: str) -> dict:
    from ..runtime_errors import runtime_error_report

    return runtime_error_report(exc, context=context)


def _stop_gateway_request_lease(
    lease_stop: threading.Event | None,
    lease_thread: threading.Thread | None,
) -> None:
    if lease_stop is not None:
        lease_stop.set()
    if lease_thread is not None:
        lease_thread.join(timeout=2)


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


def _execute_gateway_request_body(context: dict, on_chunk) -> None:
    response = context["response"]
    kind = str(response.get("kind") or "").strip()
    if kind != "ask":
        response["error_code"] = "UNSUPPORTED_KIND"
        raise ValueError(f"unsupported gateway request kind: {kind or 'empty'}")
    try:
        result = _run_gateway_ask(
            _GatewayAskRunContext(
                context["agent"],
                context["request"],
                context["request_path"],
                context["response_path"],
                context["request_id"],
                on_chunk,
            )
        )
    except ValueError as exc:
        if str(exc) == _EMPTY_PROMPT_MESSAGE:
            response["error_code"] = "EMPTY_PROMPT"
        raise
    _update_response_from_result(response, result, context["request"])


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
    existing_response = read_gateway_response_file(
        response_path,
        request_id=request_id,
        context="gateway.request_execution.response.read",
    )
    if existing_response:
        return {"existing_response": existing_response}
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


def _finalize_gateway_response(context: dict, response: dict) -> None:
    ended_at = time.time()
    _copy_final_lease_fields(response, context["request_path"])
    response["ended_at"] = ended_at
    response["duration_seconds"] = round(ended_at - context["started_at"], 3)


def _project_observed_gateway_run_facts(
    response: dict,
    chunk_writer: BufferedChunkStreamWriter,
) -> None:
    """Merge structured live events into the terminal response after any exit."""
    response["tool_rounds"] = max(
        int(response.get("tool_rounds") or 0),
        chunk_writer.observed_tool_rounds,
    )


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


def _handle_gateway_request(
    agent: SimpleAgent,
    request_path: Path,
    *,
    refresh_lease: bool = False,
    worker_id: str = "",
) -> dict:
    context = _prepare_gateway_request_context(agent, request_path)
    if context.get("existing_response"):
        return context["existing_response"]
    response = context["response"]
    if context.get("skip_execution"):
        _finalize_gateway_response(context, response)
        _complete_gateway_request_audit(agent, context, request_path, response)
        return response
    if _gateway_cancel_requested(request_path, context["request_id"]):
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
    chunk_writer = BufferedChunkStreamWriter(chunk_path_abs)

    try:
        _execute_gateway_request_body({**context, "agent": agent}, chunk_writer)
    except Exception as exc:
        response.update(
            {
                "ok": False,
                "status": "failed",
                "error_code": response.get("error_code")
                or str(getattr(exc, "error_code", "") or type(exc).__name__.upper()),
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
    finally:
        _stop_gateway_request_lease(lease_stop, lease_thread)
        chunk_writer.close()
    _project_observed_gateway_run_facts(response, chunk_writer)
    if _gateway_cancel_requested(request_path, context["request_id"]):
        _apply_cancelled_gateway_response(response)
    _finalize_gateway_response(context, response)
    _complete_gateway_request_audit(agent, context, request_path, response)
    return response


# LLM: A durable stop marker wins over model/tool completion, including restart recovery races.
# 函数用途：复读当前 processing 记录，判断这轮请求是否已被用户要求停止。
def _gateway_cancel_requested(request_path: Path, request_id: str) -> bool:
    report = read_json_file_report(request_path, context="gateway.control.cancel.read")
    if report.load_error is not None or not report.payload:
        return False
    current_id = str(report.payload.get("id") or request_path.stem)
    return current_id == request_id and bool(report.payload.get("cancel_requested"))


# LLM: A user stop interrupts only this run; the durable conversation task remains resumable.
# 函数用途：把当前请求归一成“已中断”运行态，不把整项任务永久取消。
def _apply_cancelled_gateway_response(response: dict) -> None:
    response.update(
        {
            "ok": True,
            "status": "interrupted",
            "response": "",
            "error_code": "INTERRUPTED",
            "error": "",
            "channel_delivery": _silent_user_stop_delivery(),
        }
    )


def _silent_user_stop_delivery() -> dict[str, object]:
    """Return the channel projection for a non-message user interruption."""
    return {
        "content": "",
        "artifact_names": [],
        "internal_signal": True,
        "projection_status": "suppressed_user_stop",
    }
