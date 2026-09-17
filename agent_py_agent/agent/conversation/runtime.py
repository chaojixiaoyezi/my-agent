# LLM: 后台稳定 task 身份与逐轮请求分离；唤醒共用 coordinator 软指导，不能生成永久禁写或额外授权。
# 模块用途: 处理后台事件、上下文和投递；子代理返回接回原用户目标和分工，截断回复保留真实原因。
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import field, replace
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ..agent_core.orchestration.coordinator_policy import coordinator_tool_boundary_text
from ..backends.errors import (
    is_provider_quota_exhausted_error,
    is_provider_transient_error,
    is_provider_usage_limit_error,
)
from ..concurrency.interrupt import is_interrupted, register_interruptible
from ..contracts.subagent_completion import (
    DEFAULT_VISIBLE_SUBAGENT_COMPLETIONS,
    subagent_completion_context_from_observations,
)
from ..runtime_errors import compact_error_message, runtime_error_report
from ..settings.config import DEFAULT_EXECUTION_PERSISTENCE
from ..settings.runtime_guard_config import runtime_guard_int
from ..subagents.role_templates import active_model_subagent_tools
from .agent_activity import (
    BackgroundMainActivitySink,
    task_progress_projection_for_task,
)
from .authority import (
    CONVERSATION_BACKGROUND_EVENT_REASON_ATTR,
    CONVERSATION_BACKGROUND_SUBAGENT_PHASE_ATTR,
    CONVERSATION_BACKGROUND_WAKE_SIGNAL_IDS_ATTR,
    CONVERSATION_BACKGROUND_WAKE_SNAPSHOT_IDS_ATTR,
    CONVERSATION_EXECUTION_CWD_ATTR,
    CONVERSATION_REQUEST_ID_ATTR,
    CONVERSATION_RUNTIME_WORKSPACE_ROOTS_ATTR,
    CONVERSATION_TASK_TURN_ACTIVE_ATTR,
    CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR,
)
from .control_commands import conversation_request_interrupt_name
from .models import (
    SUBAGENT_LIFECYCLE_WAKE_REASONS,
    THREAD_TASK_LINK_ACTIVE_STATUS,
    ConversationThread,
    MessageLogEntry,
    ObservationEvent,
    WakeSignal,
)
from .run_claim import ConversationRunClaimHeartbeat, claim_heartbeat_interval_seconds
from .store import ConversationStore
from .task_runtime_state import task_runtime_state


# Scheduled wakes resume the same Agent with typed state.  Runtime boundaries
# are authoritative, but the model chooses the work plan and tool sequence.
def _scheduled_continuation_prompt(reason: str) -> str:
    return (
        "This is a continuation turn for the same durable task, not a new user "
        "message. Use the persisted objective, Active Wake Signal, task state, "
        "available tools, and evidence references to decide the next useful action. "
        "When Subagent Completion Inputs are present, consume their bounded final "
        "messages or exact final_report_ref values instead of guessing managed paths. "
        "Cursors, queues, permissions, coverage, cancellation state, and source "
        "references are runtime boundaries; they do not prescribe analysis, "
        "delegation, review, or reporting. Do not repeat work already proved "
        "complete or ask for an answer when no user message is pending. Continue "
        "useful work when possible, yield when the task is genuinely waiting, and "
        "claim completion only when current evidence supports the objective."
        f"\nWake reason: {reason}"
    )


# Child lifecycle facts wake the same Agent; they do not select a workflow.
_SUBAGENT_INTEGRATION_WAKE_PROMPT = (
    "A subagent lifecycle event resumed this same task. Read the typed wake "
    "signal, objective, persisted artifacts, tool records, and evidence "
    "references. Each subagent-completion.v1 envelope carries that child's "
    "bounded completion_message plus exact final_report_ref, declared_output_refs, "
    "and artifact_refs. Consume the current metadata envelope and, when "
    "metadata.events exists, every completion envelope inside that batch; "
    "read final_report_ref before guessing or searching for an output directory. "
    + DEFAULT_EXECUTION_PERSISTENCE
    + " "
    "A host-owned child status is lifecycle authority, while completion prose is "
    "integration evidence and cannot declare the root objective complete. Decide "
    "the next action from current facts "
    "and available capabilities. The runtime does not require a particular "
    "child count, role, review path, integration order, or tool. Current Task "
    "Runtime State and current tool results are authoritative for task identity, "
    "source identity, counts, coverage, and terminal state; never substitute those "
    "facts from child prose, an earlier assistant message, or a derived artifact. "
    "For subagent_capability_request_open, read metadata.capability_request and "
    "metadata.parent_tool_authority as host facts. A capability grant/deny is the "
    "direct parent's scoped control decision, never an end-user TUI approval. If "
    "all_requested_tools_grantable is true, those exact tools are already inside "
    "the parent's authority ceiling. When that fact is true and no typed owner/workspace "
    "conflict exists, resolve it with grant directly; do not deny merely because the "
    "current model-facing catalog hides child-requested tools. If unavailable_tools is "
    "non-empty, grant will fail closed. "
    "后续工具调用仍由当前 owner 的工具权限策略裁决；能力授予不绕过审批，也不意味着已有授权的动作必须再次询问用户。"
    + coordinator_tool_boundary_text()
    + " "
    "If the structured projection says rows were omitted, use the supplied refs "
    "or wait for another lifecycle event before reporting them. Do not ask the user "
    "how to find an already delivered child result when its completion message or "
    "readable report ref is present. Avoid duplicate work and polling. "
    "If known objective gaps remain and the available tools or child capacity can still address "
    "them, continue authorized work instead of returning a partial final report. Report completion "
    "only when the current objective and runtime facts support it. Reconcile the canonical "
    "task_progress plan on every child completion: explicit covers are already credited by the "
    "host; for an unbound child, update only the exact existing item ids that your own delegation "
    "and current evidence prove complete, and do not rely on title similarity alone. Do not create a "
    "duplicate plan or leave proved-complete original items stale before final. Describe unresolved limitations "
    "only when they are genuine current blockers or the user has paused or redirected the task."
)


_GOAL_SUBAGENTS_ACTIVE_PROMPT = (
    "\n\nStructured runtime fact: one or more subagents related to this exact goal task are still "
    "nonterminal. Their lifecycle events will wake this same goal again. Treat this as current "
    "state, not as a required workflow: decide whether to continue parent work, guide, "
    "delegate, wait, or yield from the objective and available evidence. Nonterminal child state "
    "alone cannot prove the goal complete."
)

_AUDIT_FINDING_REPORT_PROMPT = (
    "One or more typed Audit findings requested an owner-facing report in this same "
    "conversation. Use every finding row in the Active Wake Signal, including each "
    "summary, identity, score, verdict, evidence_records, and evidence reference, as the facts for this "
    "report. Write one natural, concise message that covers every included finding "
    "and keeps their source identities distinct; do "
    "not replace it with child counts, queue status, tool narration, or a generic "
    "progress update. finding_id is an internal delivery and deduplication identity, "
    "not a source-domain record identifier. Use identifiers from an inline complete "
    "raw_event when present. When evidence_records marks a record reference-only, "
    "inspect its exact source_ref before stating source-domain identifiers. The typed "
    "report_scope is incremental; do not describe these rows as a whole-Audit or "
    "whole-window total. You may inspect cited evidence or coordinate further work "
    "when useful. Do not claim an investigation has started unless a real "
    "investigation run exists. Other evidence_refs remain available as supporting "
    "context but are not delivery receipts. Follow the runtime-provided owner "
    "delivery contract exactly; do not substitute a different channel or output "
    "path."
)


def _audit_finding_report_prompt(
    *,
    proactive_delivery_available: bool | None,
    transcript_delivery_available: bool | None,
) -> str:
    """Render the one reporting contract that matches the resolved route."""

    if proactive_delivery_available is True:
        return (
            _AUDIT_FINDING_REPORT_PROMPT
            + " The resolved owner route supports proactive delivery. Call the exposed "
            "send_message tool exactly once with the report text and every exact "
            "metadata.delivery_evidence_refs from the Active Wake Signal. The tool "
            "receipt is the only owner-facing output; keep final assistant text internal."
        )
    if transcript_delivery_available is True or (
        transcript_delivery_available is None and proactive_delivery_available is False
    ):
        return (
            _AUDIT_FINDING_REPORT_PROMPT
            + " The resolved owner route is the conversation transcript and has no "
            "proactive provider. Return the complete owner-facing report as the final "
            "assistant text. No send_message tool is available in this turn."
        )
    if proactive_delivery_available is False and transcript_delivery_available is False:
        return (
            _AUDIT_FINDING_REPORT_PROMPT
            + " The resolved route has no declared owner-delivery capability. Do not "
            "claim that the report was delivered and do not substitute another channel. "
            "Return no owner-facing report; the durable wake must remain retryable until "
            "a declared route becomes available."
        )
    return (
        _AUDIT_FINDING_REPORT_PROMPT
        + " Use only the owner-delivery capability exposed in this turn: if "
        "send_message is available, its typed receipt is authoritative; otherwise "
        "return the complete report as final assistant text."
    )


_AUDIT_PROVIDER_QUOTA_PROMPT = (
    "A typed Audit source-worker event says the configured model provider has "
    "exhausted its usable account or plan quota. The source cursor, durable input, "
    "and completed verdicts remain authoritative and must not be replayed or "
    "discarded. Report this pause naturally to the user once, including that an "
    "operator must restore quota or switch to an available model before resuming. "
    "Do not claim automatic recovery, do not retry the same credential, and do not "
    "describe internal tool or child-agent chatter."
)


_AUDIT_CAPACITY_REPORT_PROMPT = (
    "A typed Audit capacity event resumed this same conversation. Use only the "
    "Active Wake Signal's source count, active-worker count, pending count, oldest "
    "pending age, ingest rate, processing rate, latency and alert/recovered state. "
    "Report the capacity "
    "condition naturally and concisely to the user. This is operational health, "
    "not an attack verdict; do not infer event meaning, invent remediation already "
    "performed, recommend duplicate workers when active workers are already shown, "
    "or expose internal worker/tool narration."
)


# LLM: 后台提示只投影本轮冻结的 Goal 与事件；同线程其他目标不成为当前回合的派工需求。
# 函数用途: 根据结构化唤醒生成说明，保留普通后台、目标续跑和子代理回报的既有分流。
def background_prompt(
    reason: str,
    *,
    goal: object | None = None,
    other_goals: tuple[object, ...] = (),
    goal_subagent_phase: str = "",
    wake_signal: dict[str, Any] | None = None,
    proactive_delivery_available: bool | None = None,
    transcript_delivery_available: bool | None = None,
) -> str:
    normalized_reason = str(reason or "").strip().lower()
    if normalized_reason == "subagent_runner_finished" and _wake_payload_is_audit_provider_quota(
        wake_signal
    ):
        return _AUDIT_PROVIDER_QUOTA_PROMPT
    if normalized_reason == "scheduled_job_due":
        wake = wake_signal if isinstance(wake_signal, dict) else {}
        metadata = wake.get("metadata") if isinstance(wake.get("metadata"), dict) else {}
        prompt = str(metadata.get("scheduler_prompt") or "").strip()
        return (
            "这是当前用户先前在同一会话中登记的持久计划，现在已经到点。"
            "把下面的内容当作该用户本轮的真实要求，结合这条会话已有上下文直接执行；"
            "普通回复仍由你根据真实执行结果自然撰写。\n\n" + prompt
        )
    if goal is not None and normalized_reason in {
        "thread_goal_continue",
        *SUBAGENT_LIFECYCLE_WAKE_REASONS,
    }:
        from .goal_prompting import continuation_prompt

        prompt = continuation_prompt(goal, other_goals=other_goals)
        if goal_subagent_phase == "subagents_active":
            return prompt + _GOAL_SUBAGENTS_ACTIVE_PROMPT
        if goal_subagent_phase == "subagents_terminal":
            return prompt + "\n\n" + _SUBAGENT_INTEGRATION_WAKE_PROMPT + f"\n唤醒原因:{reason}"
        return prompt
    if normalized_reason == "thread_goal_continue":
        return "Continue working toward the active thread goal. Call get_goal first."
    if normalized_reason == "managed_process_exited":
        return ("当前会话先前启动的受管后台命令已结束，退出状态和日志引用在结构化唤醒事实中。"
                "核对相关结果，向用户汇报；若原任务仍进行中则继续相关工作。"
                "命令退出不等于任务验证通过，不重跑原命令，不重新开启已经结束或暂停的目标。")
    if normalized_reason == "audit_finding":
        return _audit_finding_report_prompt(
            proactive_delivery_available=proactive_delivery_available,
            transcript_delivery_available=transcript_delivery_available,
        )
    if normalized_reason == "audit_capacity_alert":
        return _AUDIT_CAPACITY_REPORT_PROMPT
    if normalized_reason in SUBAGENT_LIFECYCLE_WAKE_REASONS:
        return _SUBAGENT_INTEGRATION_WAKE_PROMPT + f"\n唤醒原因:{reason}"
    if normalized_reason in _SCHEDULED_WAKE_REASONS:
        return _scheduled_continuation_prompt(reason)
    return (
        "The same Agent was resumed by a structured background event. Use the "
        "persisted user objective, task state, evidence, and currently available "
        "capabilities to decide the next action; the wake reason does not impose "
        f"a workflow.\nWake reason: {reason}"
    )


def default_route_target(thread: ConversationThread, route_channel: str) -> str:
    for binding in thread.channel_bindings:
        if binding.channel == route_channel:
            return binding.channel_conversation_id
    return (
        thread.channel_bindings[-1].channel_conversation_id
        if thread.channel_bindings
        else thread.thread_id
    )


def json_block(value: Any) -> str:
    return "```json\n" + json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n```"


def pending_wake_payload(
    store: ConversationStore, thread_id: str, *, limit: int
) -> list[dict[str, Any]]:
    return [
        item.to_dict()
        for item in store.pending_wake_signals(limit=limit)
        if item.thread_id == thread_id
    ]


def wake_signal_payload(signal: WakeSignal | dict[str, Any] | None) -> dict[str, Any] | None:
    if isinstance(signal, WakeSignal):
        return signal.to_dict()
    return dict(signal) if isinstance(signal, dict) else None


# LLM: 持久 wake 账本可以保留旧策略快照用于审计，但投给模型的副本必须删掉
# 已退役控制工具，避免模型从 Active/Pending Wake Signal 里重新学会轮询或手工派工。
# 函数用途: 生成不含退役子代理工具名的模型可见 wake signal 副本。
def _model_visible_wake_signal(signal: object) -> dict[str, Any] | None:
    if not isinstance(signal, dict):
        return None
    payload = dict(signal)
    snapshot = payload.get("policy_snapshot")
    if not isinstance(snapshot, dict):
        return payload
    visible_snapshot = dict(snapshot)
    for key in ("allowed_tools", "disabled_tools"):
        if key in visible_snapshot:
            visible_snapshot[key] = active_model_subagent_tools(
                tool_names(visible_snapshot.get(key))
            )
    payload["policy_snapshot"] = visible_snapshot
    return payload


# LLM: observation 批次身份必须同时包含 thread 与 root task；同线程旧任务的
# unknown 恢复门不能吞并或阻塞新任务事件。
# 函数用途: 按会话和根任务拆分待处理观察，保持原始到达顺序。
def observations_by_thread_and_task(
    observations: list[ObservationEvent],
) -> dict[tuple[str, str], list[ObservationEvent]]:
    grouped: dict[tuple[str, str], list[ObservationEvent]] = {}
    for observation in observations:
        key = (observation.thread_id, str(observation.root_task_id or ""))
        grouped.setdefault(key, []).append(observation)
    return grouped


def first_root_task_id(observations: list[ObservationEvent]) -> str:
    return next((item.root_task_id for item in observations if item.root_task_id), "")


def now(value: float | None = None) -> float:
    return float(time.time() if value is None else value)


# Conversation runtime channel snapshots


class ChannelMessageRuntime:
    def __init__(self, *, runtime: BackgroundMainAgentRuntime, store: ConversationStore):
        self.runtime = runtime
        self.store = store

    def receive(self, request: dict) -> ConversationThread:
        current = now(request.get("now"))
        thread = self.store.get_or_create_thread(
            {
                "canonical_user_id": request.get("canonical_user_id", ""),
                "channel": request.get("channel", ""),
                "channel_conversation_id": request.get("channel_conversation_id", ""),
                "channel_user_id": request.get("channel_user_id", ""),
                "reuse_latest_for_user": True,
                "owner_id": _agent_owner_id(self.runtime.agent),
                "owner_home": _agent_owner_home(self.runtime.agent),
                "now": current,
            }
        )
        self.store.append_message(
            {
                "thread_id": thread.thread_id,
                "role": "user",
                "content": request.get("content", ""),
                "channel": request.get("channel", ""),
                "now": current,
            }
        )
        if request.get("run_background", True):
            self.runtime.run_once(
                {
                    "thread_id": thread.thread_id,
                    "reason": "incoming_channel_message",
                    "route_channel": request.get("channel", ""),
                    "route_target": request.get("channel_conversation_id", ""),
                    "now": current,
                }
            )
        latest = self.store.load_thread(thread.thread_id)
        if latest is None:
            raise KeyError(f"unknown conversation thread: {thread.thread_id}")
        return latest


def _agent_owner_id(agent: object) -> str:
    home_paths = getattr(agent, "home_paths", None)
    return str(getattr(home_paths, "owner_id", "") or "")


def _agent_owner_home(agent: object) -> str:
    home_paths = getattr(agent, "home_paths", None)
    return str(getattr(home_paths, "owner_home_dir", "") or "")


# Conversation runtime tool policy
"""Background main-agent tool policy helpers."""


from dataclasses import dataclass

# Background wake turns are normal continuations of the same Agent.  This is
# the common capability surface before owner/task policy applies reductions.
_BACKGROUND_WORK_TOOLS = (
    "skill_search",
    "remember",
    "update_persona",
    "read_file",
    "list_files",
    "search_text",
    "write_file",
    "edit_file",
    "run_command",
    "process_session",
    "watch_stream",
    "task_progress",
    "resolve_capability_requests",
    "cancel_subagents",
)

DEFAULT_BACKGROUND_ALLOWED_TOOLS = (
    "send_guidance",
    "create_subagents",
    *_BACKGROUND_WORK_TOOLS,
)

# A typed finding is already durable before it wakes the owner Agent.  The
# reporting turn may inspect, coordinate, investigate and deliver it, but does
# not get a persistence tool: the producer owns persistence, the consumer owns
# acknowledgement and presentation.  Proactive delivery uses the common typed
# send_message receipt; final text is internal coordination output.
AUDIT_FINDING_ALLOWED_TOOLS = (
    *DEFAULT_BACKGROUND_ALLOWED_TOOLS,
    "send_message",
)

# Wake reason changes context, not capability. Owner/task policy still performs
# the authoritative capability reduction.
SCHEDULED_BACKGROUND_ALLOWED_TOOLS = DEFAULT_BACKGROUND_ALLOWED_TOOLS

GOAL_BACKGROUND_ALLOWED_TOOLS = (
    *DEFAULT_BACKGROUND_ALLOWED_TOOLS,
    "get_goal",
    "update_goal",
)

SUBAGENT_INTEGRATION_ALLOWED_TOOLS = DEFAULT_BACKGROUND_ALLOWED_TOOLS
GOAL_SUBAGENTS_ACTIVE_ALLOWED_TOOLS = GOAL_BACKGROUND_ALLOWED_TOOLS
GOAL_SUBAGENTS_TERMINAL_ALLOWED_TOOLS = GOAL_BACKGROUND_ALLOWED_TOOLS

CONTROL_ACTION_DESCRIPTIONS = {
    "skill_search": "检索或读取当前轮已经授权的 Skill 正文。",
    "remember": "把当前 owner 的长期事实写入其隔离记忆。",
    "update_persona": "维护当前 owner 的 USER/AGENTS 人格约定；SOUL 仍需用户确认。",
    "send_guidance": "给正在运行的代理追加软提示。",
    "create_subagents": "创建并启动新的下级代理。",
    "read_file": "读取子代理产出的文件/产物,用于整合与验收。",
    "list_files": "查看子代理在工作区写了哪些产物。",
    "search_text": "在子代理产物里检索内容。",
    "write_file": "写最终交付物,或把子代理产物整合成成品。",
    "edit_file": "修订/整合已有交付文件。",
    "run_command": "运行 import/测试做交付前自检。",
    "process_session": "查询、有界等待或停止当前用户会话启动的后台命令。",
    "watch_stream": "读取有持久游标和覆盖账的数据源；Audit 模式按完整记录和 ack/source_ref 对账。",
    "task_progress": "更新任务清单进展。",
    "resolve_capability_requests": "批准或拒绝子代理的能力申请,让它能继续干。",
    "cancel_subagents": "打断并结束一个不应继续运行的直属子代理；它不负责轮询、推动或验收。",
    "send_message": "向当前 owner 的已绑定通道发送一条模型撰写的消息；证据型后台事件必须原样携带其 evidence_refs。",
    "get_goal": "读取当前 /goal 持续目标及其权威状态。",
    "update_goal": "仅在持续目标真正完成或确实阻塞时写入 complete/blocked 终态。",
}


@dataclass(frozen=True)
class BackgroundToolPolicyRequest:
    """Facts used to choose the background main-agent control tool profile."""

    reason: str = ""
    wake_signal: dict[str, Any] | None = None
    config: object | None = None
    owner_policy: object | None = None
    policy_snapshot: dict[str, Any] | None = None
    # These fields come only from the persisted goal/task/subagent records. They
    # never depend on model prose or natural-language intent classification.
    active_goal: bool = False
    goal_subagent_phase: str = ""
    # Resolved before model execution. False means the current owner route has
    # no provider receipt path, so send_message must not appear in schemas,
    # tool search, or final execution. None preserves standalone policy probes.
    proactive_delivery_available: bool | None = None


@dataclass(frozen=True)
class BackgroundToolPolicyDecision:
    """Final background tool list plus where each restriction came from."""

    allowed_tools: tuple[str, ...]
    profile: str
    sources: tuple[str, ...]
    removed_tools: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "background-tool-policy.v1",
            "profile": self.profile,
            "allowed_tools": list(self.allowed_tools),
            "sources": list(self.sources),
            "removed_tools": list(self.removed_tools),
        }


def background_allowed_tools(
    config: object | None = None,
    request: BackgroundToolPolicyRequest | None = None,
) -> list[str]:
    return list(background_tool_policy_decision(config, request=request).allowed_tools)


# LLM: 后台轮工具表必须先淘汰旧子代理控制面，再套 owner/task/delivery 减法；
# 存量配置不能把已注销工具重新带回模型 prompt。
# 函数用途: 根据唤醒类型和结构化策略计算后台主代理本轮可见工具。
def background_tool_policy_decision(
    config: object | None = None,
    request: BackgroundToolPolicyRequest | None = None,
) -> BackgroundToolPolicyDecision:
    if request is None:
        request = BackgroundToolPolicyRequest(config=config)
    config = request.config if request.config is not None else config
    configured = tool_names(getattr(config, "background_main_agent_allowed_tools", None))
    if configured:
        tools = configured
        profile = "configured"
        sources = ["agent_config.background_main_agent_allowed_tools"]
    else:
        profile, default_tools = _default_profile_for_request(request)
        tools = list(default_tools)
        sources = [f"default_profile:{profile}"]
    tools = active_model_subagent_tools(tools)
    tools, removed = _apply_policy_limits(tools, request)
    if removed:
        sources.append("owner_or_task_policy")
    if request.proactive_delivery_available is False and "send_message" in tools:
        tools = [tool for tool in tools if tool != "send_message"]
        removed = [*removed, "send_message"]
        sources.append("delivery_route_capability")
    return BackgroundToolPolicyDecision(
        allowed_tools=tuple(tools),
        profile=profile,
        sources=tuple(sources),
        removed_tools=tuple(removed),
    )


def background_control_action_lines(
    config: object | None = None,
    request: BackgroundToolPolicyRequest | None = None,
) -> list[str]:
    lines: list[str] = []
    for name in background_allowed_tools(config, request=request):
        description = CONTROL_ACTION_DESCRIPTIONS.get(name)
        if description:
            lines.append(f"- {name}: {description}")
    return lines


def tool_names(value: object) -> list[str]:
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, (list, tuple)):
        raw_items = value
    else:
        return []
    return list(dict.fromkeys(str(item).strip() for item in raw_items if str(item).strip()))


def _default_profile_for_request(
    request: BackgroundToolPolicyRequest,
) -> tuple[str, tuple[str, ...]]:
    # Structured state selects context/profile labels, not a required workflow.
    reason = str(request.reason or "").strip().lower()
    if request.active_goal and (
        reason == "thread_goal_continue" or reason in SUBAGENT_LIFECYCLE_WAKE_REASONS
    ):
        if request.goal_subagent_phase == "subagents_active":
            return "thread_goal_subagents_active", GOAL_SUBAGENTS_ACTIVE_ALLOWED_TOOLS
        if request.goal_subagent_phase == "subagents_terminal":
            return "thread_goal_subagents_terminal", GOAL_SUBAGENTS_TERMINAL_ALLOWED_TOOLS
    if reason == "thread_goal_continue":
        return "thread_goal", GOAL_BACKGROUND_ALLOWED_TOOLS
    if reason == "audit_finding":
        return "audit_finding", AUDIT_FINDING_ALLOWED_TOOLS
    if _is_subagent_lifecycle_wake(request):
        return "subagent_integration", SUBAGENT_INTEGRATION_ALLOWED_TOOLS
    if _is_urgent_wake(request):
        return "urgent", DEFAULT_BACKGROUND_ALLOWED_TOOLS
    if _is_scheduled_progress(request):
        return "scheduled_progress", SCHEDULED_BACKGROUND_ALLOWED_TOOLS
    return "default", DEFAULT_BACKGROUND_ALLOWED_TOOLS


def _apply_policy_limits(
    tools: list[str],
    request: BackgroundToolPolicyRequest,
) -> tuple[list[str], list[str]]:
    allowed = list(dict.fromkeys(tools))
    task_policy = request.policy_snapshot if isinstance(request.policy_snapshot, dict) else {}
    task_allowed = tool_names(task_policy.get("allowed_tools"))
    if task_allowed:
        allowed = [tool for tool in allowed if tool in set(task_allowed)]
    disabled = set(_disabled_tools_from_owner(request.owner_policy))
    disabled.update(tool_names(task_policy.get("disabled_tools")))
    filtered = [tool for tool in allowed if tool not in disabled]
    removed = [tool for tool in allowed if tool not in filtered]
    return filtered, removed


def _disabled_tools_from_owner(owner_policy: object | None) -> list[str]:
    return tool_names(getattr(owner_policy, "disabled_tools", ()))


def _is_urgent_wake(request: BackgroundToolPolicyRequest) -> bool:
    wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    urgency = str(wake.get("urgency") or "").strip().lower()
    reason = str(request.reason or wake.get("reason") or "").strip().lower()
    # observation_requires_main_agent = 宿主写入的真实观察事件把主代理叫回。
    # 真机根 bug:它原来不在此集 → 落 default 分支拿到含 create_subagents 的工具集 → 主代理不上报、
    # 反去重派工(真事件永远不发用户)。它语义就是"紧急事件待上报",归入 urgent → 走上报提示词分支。
    return urgency == "urgent" or reason in {
        "urgent_wake_signal",
        "wake_signal",
        "observation_requires_main_agent",
    }


# 定时类唤醒 reason 的权威名单:工具策略(_is_scheduled_progress)与提示词分支
#   (background_prompt → _scheduled_continuation_prompt)共用,防两处漂移。
_SCHEDULED_WAKE_REASONS = frozenset(
    {"scheduled_progress_report", "progress_policy_due", "due_progress_policy", "scheduled_job_due"}
)


def _is_scheduled_progress(request: BackgroundToolPolicyRequest) -> bool:
    return str(request.reason or "").strip().lower() in _SCHEDULED_WAKE_REASONS


# 子代理→主代理的"生命周期"推送:完成/卡住/失败(subagent_runner_finished)、申请能力
# (subagent_capability_request_open)、能力获批可续跑(subagent_capability_granted)。
# 这些唤醒叫回主代理是为了真整合收口/批能力,所以要给整合工具集(见 SUBAGENT_INTEGRATION_ALLOWED_TOOLS)。
def _is_subagent_lifecycle_wake(request: BackgroundToolPolicyRequest) -> bool:
    wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    reason = str(request.reason or wake.get("reason") or "").strip().lower()
    return reason in SUBAGENT_LIFECYCLE_WAKE_REASONS


# Conversation runtime worker
from collections.abc import Callable
from dataclasses import dataclass

from ..agent_core.runtime.loop_models import RunParams
from ..runtime_errors import DataCorruptionError
from .channels import (
    PROACTIVE_PUSH_CHANNELS,
    ChannelAttachment,
    DeliveryContext,
    DeliveryServiceProtocol,
    FakeDeliveryService,
    ReplyEnvelope,
    project_user_reply,
    supports_transcript_delivery,
)
from .models import BackgroundDeliveryCommit, BackgroundMainAgentReport


@dataclass(frozen=True)
class BackgroundRunRequest:
    thread_id: str
    task_id: str = ""
    reason: str = "scheduled_progress_report"
    route_channel: str = "internal"
    route_target: str = ""
    now: float = 0.0
    wake_signal: dict[str, Any] | None = None


# LLM: This private control signal means a healthy background turn exhausted its bounded Compact
# slice after every attempt advanced canonical generation. It must be caught before generic runtime
# failures so the durable wake stays pending and the next scheduler slice resumes from checkpoints.
# 类用途: 表示一次超长后台任务已完成本片压缩配额，需要公平让出线程后继续；它不是任务失败。
class _BackgroundCompactSliceYield(RuntimeError):
    pass


# LLM: 一次采样冻结精确目标、兄弟目标状态与 child phase；兄弟目标不是本回合的执行范围。
# 类用途: 固定一次后台模型调用所见的目标、任务目录和子代理阶段，避免采样中途漂移。
@dataclass(frozen=True)
class GoalRuntimeContext:
    goal: object | None = None
    task_objective: str = ""
    task_path: str = ""
    subagent_phase: str = ""
    state_error: str = ""
    other_goals: tuple[object, ...] = ()


def _material_tool_success_count(
    agent: object,
    calls: list[dict[str, object]],
) -> int:
    """Count successful state-changing calls from the run-time policy, never prose."""
    from ..tooling.models import tool_effect_for_runtime_policy

    registry = getattr(agent, "tools", None)
    if registry is None or not hasattr(registry, "runtime_snapshot"):
        return 0
    snapshot = registry.runtime_snapshot()
    count = 0
    for record in calls:
        if record.get("ok") is not True:
            continue
        tool_name = str(record.get("tool") or "").strip()
        runtime = snapshot.runtime(tool_name)
        if runtime is None:
            continue
        raw = record.get("parameters")
        arguments = dict(raw) if isinstance(raw, dict) else {}
        arguments.pop("tool", None)
        if tool_effect_for_runtime_policy(runtime.runtime_policy, arguments) in {
            "mutating",
            "dangerous",
        }:
            count += 1
    return count


# LLM: 同一后台轮冻结 Goal/child phase；Goal 的持久任务编号不是普通用户请求编号，先按本线程事实解析归属。
# 原始用户历史不裁剪；拆开的命名目标不得把整条多目标消息重新作为各自的任务。只读，不改历史或任务账。
# 函数用途: 固定后台续接的正确用户要求与子树阶段，避免新任务完成后主代理回去回答旧任务。
def _goal_runtime_context(
    agent: object,
    store: ConversationStore,
    request: BackgroundRunRequest,
) -> GoalRuntimeContext:
    """Resolve one exact goal and freeze the child phase for this background turn."""
    task_id = str(request.task_id or "").strip()
    if not task_id:
        return GoalRuntimeContext()
    task_objective, task_path, link_error = _background_task_continuation_fields(
        store,
        request.thread_id,
        task_id,
    )
    phase = ""
    state_error = link_error
    if str(request.reason or "").strip().lower() in SUBAGENT_LIFECYCLE_WAKE_REASONS:
        phase, phase_error = _goal_subagent_phase(agent, task_id)
        state_error = phase_error or link_error
    try:
        wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
        metadata = wake.get("metadata") if isinstance(wake.get("metadata"), dict) else {}
        goal = store.load_goal(request.thread_id, goal_id=str(metadata.get("goal_id") or ""), task_id=task_id)
    except Exception:
        return GoalRuntimeContext(
            task_objective=task_objective,
            task_path=task_path,
            subagent_phase=phase,
            state_error=state_error or "goal_state_load_error",
        )
    if not link_error and _is_active_turn_lifecycle_continuation(request, task_objective):
        durable_goal_task_id = task_id if (
            goal is not None
            and str(getattr(goal, "thread_id", "") or "") == request.thread_id
            and str(getattr(goal, "task_id", "") or "") == task_id
        ) else ""
        task_objective = _background_request_objective(
            store, request, durable_goal_task_id=durable_goal_task_id,
        ) or task_objective
    if (
        goal is None
        or str(getattr(goal, "task_id", "") or "").strip() != task_id
        or str(getattr(goal, "status", "") or "").strip().lower() != "active"
    ):
        return GoalRuntimeContext(
            task_objective=task_objective,
            task_path=task_path,
            subagent_phase=phase,
            state_error=state_error,
        )
    if not phase and not state_error:
        phase, state_error = _goal_subagent_phase(agent, task_id)
    goals, goals_error = store.load_goals_report(request.thread_id)
    return GoalRuntimeContext(
        goal=goal,
        task_objective=str(goal.objective),
        task_path=task_path,
        subagent_phase=phase,
        state_error=state_error or ("goal_state_load_error" if goals_error else ""),
        other_goals=tuple(item for item in goals if item.goal_id != goal.goal_id),
    )


# LLM: Task link owns the durable workspace and the objective of wakes without a request id.
# Lifecycle requests resolve their own user input separately; never overwrite this historical link.
# 函数用途: 读取运行目录和原始登记目标；后续回合的正文须由精确请求另取，不能改写旧登记来凑新目标。
def _background_task_continuation_fields(
    store: ConversationStore,
    thread_id: str,
    task_id: str,
) -> tuple[str, str, str]:
    try:
        link, load_error = store.load_task_link_report(task_id)
    except Exception:
        return "", "", "task_link_load_error"
    if load_error is not None:
        return "", "", "task_link_load_error"
    if link is None:
        return "", "", ""
    if (
        str(getattr(link, "task_id", "") or "").strip() != str(task_id or "").strip()
        or str(getattr(link, "thread_id", "") or "").strip() != str(thread_id or "").strip()
    ):
        return "", "", "task_link_scope_mismatch"
    if str(getattr(link, "status", "") or "").strip().lower() != THREAD_TASK_LINK_ACTIVE_STATUS:
        return "", "", ""
    return (
        str(getattr(link, "goal", "") or "").strip(),
        str(getattr(link, "task_path", "") or "").strip(),
        "",
    )


# LLM: Resolve only host-owned wake request ids in this store/thread's canonical transcript.
# Page cursors are ephemeral reads, not model state. The exact Goal task already validated by
# the caller has no synthetic user message; all other explicit request ids still fail if missing.
# 函数用途: 按普通派工请求找回用户原话；已核实的 Goal 任务沿目标账读取，不伪造消息或吞掉真实历史损坏。
def _background_request_objective(
    store: ConversationStore,
    request: BackgroundRunRequest,
    *,
    durable_goal_task_id: str = "",
) -> str:
    pending = set(_background_active_turn_request_ids(request))
    if durable_goal_task_id and durable_goal_task_id == request.task_id:
        pending.discard(durable_goal_task_id)
    if not pending:
        return ""
    selected: list[str] = []
    before: int | None = None
    while pending:
        page = store.history_page_report(request.thread_id, before=before, limit=80)
        if page.errors:
            raise DataCorruptionError("background request transcript is unreadable")
        for row in reversed(page.rows):
            metadata = row.metadata if isinstance(row.metadata, dict) else {}
            request_id = str(metadata.get(CONVERSATION_REQUEST_ID_ATTR) or metadata.get("gateway_request_id") or "")
            if row.role != "user" or metadata.get("kind") == "active_turn_user_input" or request_id not in pending:
                continue
            if row.thread_id != request.thread_id or not row.content.strip():
                raise DataCorruptionError("background request input is invalid")
            selected.append(row.content)
            pending.remove(request_id)
        if not pending:
            return "\n\n".join(reversed(selected))
        if not page.before or (before is not None and page.before >= before):
            raise DataCorruptionError("background request input is missing from its conversation")
        before = page.before
    return ""


# LLM: 一片后台工作的交付输入必须一次性冻结：模型产出、投递判定、路线能力、工具计数都来自
# 同一片 run，收口阶段不得重新采样；字段用默认值只是为了少写样板，语义上全部必填。
# 类用途: 承载 run_once 收口阶段需要的全部结构化事实。
@dataclass(frozen=True)
class _BackgroundSlicePlan:
    request: BackgroundRunRequest
    delivery_context: DeliveryContext
    channel: str
    target: str
    response: str
    deliver: bool
    delivery_reason: str
    route_supports_proactive: bool = False
    route_supports_transcript: bool = False
    route_ownership: str = ""
    delivery_artifacts: tuple[dict[str, object], ...] = ()
    message_tool_deliveries: tuple[dict[str, object], ...] = ()
    operation_verification: dict[str, object] | None = None
    assistant_commentaries: tuple[str, ...] = ()
    display_snapshot: dict[str, object] | None = None
    counters: tuple[int, int, int] = (0, 0, 0)


# LLM: 交付收口只有一条路径：能自己外发的走 message-tool 直投镜像，其余走 _record_response 的
# canonical 落账 + 冻结重投；唤醒确认只读结构化提交事实。禁止在这里新增按渠道名的分支。
# 函数用途: 把一片后台模型产出交付给 owner，并返回结构化报告。
def _complete_background_slice(
    runtime: BackgroundMainAgentRuntime,
    plan: _BackgroundSlicePlan,
) -> BackgroundMainAgentReport:
    request = plan.request
    if _message_tool_delivery_satisfied(request, plan.message_tool_deliveries):
        # 通道运行时's cron runner treats committed message-tool delivery as the
        # source reply and skips its announce fallback. Mirror the payload into
        # the same transcript, but never call the channel a second time.
        reported_content = _mirror_message_tool_deliveries(
            runtime.store,
            request,
            plan.delivery_context,
            plan.message_tool_deliveries,
        )
        delivery_status = "sent"
        delivery_reason = _message_tool_delivery_reason(request)
        commit = BackgroundDeliveryCommit(
            content=reported_content,
            delivery_status=delivery_status,
            persisted=False,
            commit_kind="external_delivery",
            transcript_route=plan.route_supports_transcript,
        )
    else:
        commit = runtime._record_response(
            request,
            plan.delivery_context,
            plan.response,
            delivery_artifacts=plan.delivery_artifacts,
            operation_verification=plan.operation_verification,
            assistant_commentaries=plan.assistant_commentaries,
            display_snapshot=plan.display_snapshot,
            deliver=plan.deliver,
            delivery_reason=plan.delivery_reason,
            route_supports_transcript=plan.route_supports_transcript,
        )
        reported_content = commit.content
        delivery_status = commit.delivery_status
        delivery_reason = plan.delivery_reason
    # 唤醒只有在答复真的落到某个权威位置（外发成功或 canonical 记录）时才确认；
    # 有外发义务却未送达的路线留给冻结重投，绝不重跑业务。
    wake_handled = _background_owner_delivery_committed(
        request,
        target=plan.target,
        ownership=plan.route_ownership,
        canonical_record=bool(plan.route_supports_transcript or plan.route_ownership != _ROUTE_EXTERNAL),
        commit=commit,
    )
    tool_call_count, tool_success_count, material_progress_count = plan.counters
    return BackgroundMainAgentReport(
        thread_id=request.thread_id,
        task_id=request.task_id,
        reason=request.reason,
        response=reported_content,
        route_channel=plan.channel,
        route_target=plan.target,
        created_at=request.now,
        tool_call_count=tool_call_count,
        tool_success_count=tool_success_count,
        goal_continuation_allowed=(plan.display_snapshot or {}).get("goal_continuation_allowed") is True,
        material_progress_count=material_progress_count,
        delivery_status=delivery_status,
        delivery_reason=delivery_reason,
        wake_handled=wake_handled,
        task_status=_background_task_link_status(runtime.agent, request, store=runtime.store),
        commit_kind=commit.commit_kind,
        message_id=commit.message_id,
        route_ownership=plan.route_ownership,
    )


class BackgroundMainAgentRuntime:
    def __init__(
        self,
        *,
        agent: object,
        store: ConversationStore,
        channels: DeliveryServiceProtocol | None = None,
    ):
        self.agent = agent
        self.store = store
        # The runtime store is the one durable thread authority. Finalization,
        # goal tools, transcript writes, and scheduler reconciliation must not
        # silently use a second ConversationStore instance.
        if getattr(agent, "conversation_store", None) is not store:
            agent.conversation_store = store
        self.channels = channels or FakeDeliveryService()

    # LLM: 后台轮先冻结目标/子树阶段；完整展示快照随 canonical final 提交，不参与投递或生命周期裁决。
    # 函数用途: 执行一次结构化后台唤醒，并按会话渠道记录或发送模型回复。
    def run_once(self, params: dict) -> BackgroundMainAgentReport:
        request = _run_request(params)
        if callable(getattr(self.store, "load_thread_report", None)):
            thread, load_error = self.store.load_thread_report(request.thread_id)
            if load_error is not None:
                raise DataCorruptionError(str(load_error))
        else:
            thread = self.store.load_thread(request.thread_id)
        if thread is None:
            raise KeyError(f"unknown conversation thread: {request.thread_id}")
        channel, target = _resolve_delivery_route(
            thread,
            request,
            supports_proactive=getattr(self.channels, "supports_proactive", None),
        )
        route_supports_proactive = bool(target and self.channels.supports_proactive(channel))
        route_supports_transcript = _route_supports_transcript(self.channels, channel)
        # 归属只读声明级事实：外发能力此刻是否可用不改变这条路线欠不欠一次真实外送。
        route_ownership = _background_route_ownership(self.channels, channel)
        goal_context = _goal_runtime_context(self.agent, self.store, request)
        (
            response,
            tool_call_count,
            tool_success_count,
            material_progress_count,
            delivery_artifacts,
            message_tool_deliveries,
            operation_verification,
            assistant_commentaries,
            display_snapshot,
        ) = self._run_agent(
            thread,
            request,
            goal_context=goal_context,
            proactive_delivery_available=route_supports_proactive,
            transcript_delivery_available=route_supports_transcript,
        )
        delivery_context = DeliveryContext(
            channel=channel,
            target=target,
            mode="proactive",
            thread_id=request.thread_id,
            task_id=request.task_id,
            idempotency_key=_background_delivery_idempotency_key(request),
        )
        deliver, delivery_reason = _background_delivery_decision(
            self.agent,
            request,
            store=self.store,
            resolved_channel=channel,
            resolved_route_supports_proactive=route_supports_proactive,
            resolved_route_supports_transcript=route_supports_transcript,
        )
        return _complete_background_slice(
            self,
            _BackgroundSlicePlan(
                request=request,
                delivery_context=delivery_context,
                channel=channel,
                target=target,
                response=response,
                delivery_artifacts=delivery_artifacts,
                message_tool_deliveries=message_tool_deliveries,
                operation_verification=operation_verification,
                assistant_commentaries=assistant_commentaries,
                display_snapshot=display_snapshot,
                deliver=deliver,
                delivery_reason=delivery_reason,
                route_supports_proactive=route_supports_proactive,
                route_supports_transcript=route_supports_transcript,
                route_ownership=route_ownership,
                counters=(tool_call_count, tool_success_count, material_progress_count),
            ),
        )

    def redeliver_cached_wake(
        self,
        signal: WakeSignal,
        *,
        channel: str,
        target: str,
        now: float,
    ) -> BackgroundMainAgentReport | None:
        """Retry one frozen owner payload without spending another model turn."""

        prepared = _cached_owner_delivery(signal)
        if prepared is None:
            return None
        request = BackgroundRunRequest(
            thread_id=signal.thread_id,
            task_id=signal.root_task_id,
            reason=signal.reason,
            route_channel=channel,
            route_target=target,
            now=now,
            wake_signal=signal.to_dict(),
        )
        delivery_context = DeliveryContext(
            channel=channel,
            target=target,
            mode="proactive",
            thread_id=signal.thread_id,
            task_id=signal.root_task_id,
            idempotency_key=_background_delivery_idempotency_key(request),
        )
        route_supports_transcript = _route_supports_transcript(self.channels, channel)
        ownership = _background_route_ownership(self.channels, channel)
        frozen = _frozen_owner_delivery(prepared)
        commit = self._record_response(
            request,
            delivery_context,
            frozen.content,
            delivery_artifacts=frozen.delivery_artifacts,
            assistant_commentaries=frozen.assistant_commentaries,
            deliver=True,
            delivery_reason="cached_owner_delivery_retry",
            route_supports_transcript=route_supports_transcript,
            frozen_retry=frozen,
        )
        wake_handled = _background_owner_delivery_committed(
            request,
            target=target,
            ownership=ownership,
            canonical_record=bool(route_supports_transcript or ownership != _ROUTE_EXTERNAL),
            commit=commit,
        )
        return BackgroundMainAgentReport(
            thread_id=signal.thread_id,
            task_id=signal.root_task_id,
            reason=signal.reason,
            response=commit.content,
            route_channel=channel,
            route_target=target,
            created_at=now,
            delivery_status=commit.delivery_status,
            delivery_reason="cached_owner_delivery_retry",
            wake_handled=wake_handled,
            goal_continuation_allowed=frozen.message_metadata.get("goal_continuation_allowed") is True,
            task_status=_background_task_link_status(self.agent, request, store=self.store),
            commit_kind=commit.commit_kind,
            message_id=commit.message_id,
            route_ownership=ownership,
        )

    # LLM: GoalRuntimeContext 是唯一采样；返回的显示快照只供 final 持久化，不能注入模型或改变 child phase。
    # 函数用途: 用冻结的后台上下文调用主代理，并整理工具、产物与投递结果。
    def _run_agent(
        self,
        thread,
        request: BackgroundRunRequest,
        *,
        goal_context: GoalRuntimeContext | None = None,
        proactive_delivery_available: bool | None = None,
        transcript_delivery_available: bool | None = None,
    ) -> tuple[
        str,
        int,
        int,
        int,
        tuple[dict[str, object], ...],
        tuple[dict[str, object], ...],
        dict[str, object],
        tuple[str, ...],
        dict[str, object],
    ]:
        resolved_goal_context = goal_context or _goal_runtime_context(
            self.agent,
            self.store,
            request,
        )
        result, display_snapshot = _invoke_background_main_agent(
            self,
            thread,
            request,
            resolved_goal_context,
            (proactive_delivery_available, transcript_delivery_available),
        )
        calls = [
            item
            for item in (getattr(result, "archive_tool_calls", None) or [])
            if isinstance(item, dict)
        ]
        successes = sum(1 for item in calls if item.get("ok") is True)
        material_progress = _material_tool_success_count(self.agent, calls)
        artifacts = tuple(
            dict(item)
            for item in (getattr(result, "delivery_artifacts", None) or [])
            if isinstance(item, dict)
        )
        deliveries = tuple(
            dict(item)
            for item in (getattr(result, "message_tool_deliveries", None) or [])
            if isinstance(item, dict)
        )
        operation_verification = _public_result_operation_verification(result)
        assistant_commentaries = tuple(
            text
            for value in (getattr(result, "assistant_commentary_messages", None) or ())
            if (text := str(value or "").strip())
        )
        return (
            str(getattr(result, "response", "") or ""),
            len(calls),
            successes,
            material_progress,
            artifacts,
            deliveries,
            operation_verification,
            assistant_commentaries,
            display_snapshot,
        )

    # LLM: canonical 记录与外部投递是两条独立事实：正文归属看路线归属（声明级），外发成功与否只看回执。
    # 未送达但有外发义务的正文必须整封冻结在唤醒上重投，不能在无记录的情况下确认唤醒；
    # frozen_retry 非空表示这是重投：整封 envelope（正文/附件/过程/metadata）都来自冻结载荷。
    # 函数用途: 处理渠道投递，并把真实正文和本工作片显示交给唯一会话提交层。
    def _record_response(
        self,
        request: BackgroundRunRequest,
        delivery_context: DeliveryContext,
        internal_content: str,
        *,
        delivery_artifacts: tuple[dict[str, object], ...] = (),
        operation_verification: dict[str, object] | None = None,
        assistant_commentaries: tuple[str, ...] = (),
        display_snapshot: dict[str, object] | None = None,
        deliver: bool,
        delivery_reason: str,
        route_supports_proactive: bool | None = None,
        route_supports_transcript: bool | None = None,
        frozen_retry: _FrozenOwnerDelivery | None = None,
    ) -> BackgroundDeliveryCommit:
        # 内部协议仍交给真实 DeliveryService 做主动消息抑制，但普通 transcript/report
        # 只能保存用户投影，否则下一轮 compact 和 owner-local 搜索会被机器协议污染。
        projection = project_user_reply(internal_content)
        if _background_reply_suppressed(
            self,
            request,
            delivery_reason=delivery_reason,
            deliver=deliver,
            projection_content=projection.content,
            has_attachments=bool(_channel_attachments(delivery_artifacts)),
        ):
            return _uncommitted_delivery(projection.content if deliver else "", "suppressed")
        # ReplyEnvelope is a user-content envelope, not an internal protocol carrier.
        # Sending the already projected text also keeps the real DeliveryService from
        # having to distinguish a valid completion signal from other internal signals.
        attachments = _channel_attachments(delivery_artifacts)
        evidence_refs = (
            frozen_retry.evidence_refs
            if frozen_retry is not None and frozen_retry.evidence_refs
            else (
                _background_delivery_evidence_refs(request)
                if _audit_finding_report_event(request)
                else _background_evidence_refs(request)
            )
        )
        # 重投路径由 redeliver_cached_wake 把冻结载荷的 delivery_artifacts 当作 delivery_artifacts
        # 传进来，因此附件与过程回复必须继续从参数推导；重投只额外接管 metadata 与"是否已外发"。
        envelope = ReplyEnvelope(
            content=projection.content,
            attachments=attachments,
            evidence_refs=evidence_refs,
        )
        message_metadata = _background_owner_message_metadata(
            request,
            delivery_reason=delivery_reason,
            projection_status=projection.projection_status,
            delivery_artifacts=delivery_artifacts,
            evidence_refs=evidence_refs,
            operation_verification=operation_verification,
            frozen_retry=frozen_retry,
        )
        if display_snapshot is not None and frozen_retry is None:
            message_metadata["goal_continuation_allowed"] = display_snapshot.get("goal_continuation_allowed") is True
        wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
        wake_signal_id = str(wake.get("wake_signal_id") or "").strip()
        route = _background_delivery_route(
            self.channels,
            delivery_context,
            route_supports_transcript=route_supports_transcript,
        )
        receipt, delivery_status = _background_external_delivery(
            self,
            delivery_context,
            envelope,
            frozen_retry=frozen_retry,
        )
        # The delivery service owns final user-boundary redaction.  Persist the
        # exact content recorded by its receipt so external IM and local
        # transcript never diverge; simple test/legacy receipts without content
        # retain the already-sanitized projection as a compatibility fallback.
        committed_content = str(getattr(receipt, "content", projection.content) or "")
        commit = _commit_background_response(
            self,
            request,
            delivery_context,
            receipt=receipt,
            delivery_status=delivery_status,
            committed_content=committed_content,
            canonical_record=route.canonical_record,
            transcript_route=route.transcript_route,
            evidence_refs=evidence_refs,
            message_metadata=message_metadata,
            assistant_commentaries=assistant_commentaries,
            display_snapshot=display_snapshot,
            audit_refs_settled=bool(frozen_retry is not None and frozen_retry.external_sent),
        )
        frozen_now = _freeze_pending_owner_delivery(
            self.store,
            _OwnerDeliveryFreeze(
                request=request,
                wake_signal_id=wake_signal_id,
                content=projection.content,
                delivery_artifacts=delivery_artifacts,
                assistant_commentaries=assistant_commentaries,
                evidence_refs=evidence_refs,
                message_metadata=message_metadata,
                delivery_status=delivery_status,
                receipt_id=str(getattr(receipt, "receipt_id", "") or ""),
                target=delivery_context.target,
                ownership=route.ownership,
                canonical_record=route.canonical_record,
                persisted=commit.persisted,
            ),
        )
        return replace(commit, outbox_frozen=frozen_now) if frozen_now else commit


# LLM: 重投载荷必须是整封 owner envelope 的不可变快照：正文、附件（delivery_artifacts）、
# 过程回复、审计引用、canonical metadata、以及"外发是否已经成功"这一事实。
# 只存正文会让重投丢附件/丢过程/重复外发；只存状态会让重投无从重建消息。
# 类用途: 承载一条已冻结的 owner 交付载荷（v1/v2 载荷统一投影）。
@dataclass(frozen=True)
class _FrozenOwnerDelivery:
    content: str
    delivery_artifacts: tuple[dict[str, object], ...] = ()
    assistant_commentaries: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    message_metadata: dict[str, object] = field(default_factory=dict)
    external_sent: bool = False
    receipt_id: str = ""
    ownership: str = ""


# LLM: 冻结判定所需的全部事实；target 必须用已解析的投递目标，不能回读 request.route_target。
# 类用途: 承载“这条答复是否要整封冻结在唤醒上”的结构化输入。
@dataclass(frozen=True)
class _OwnerDeliveryFreeze:
    request: BackgroundRunRequest
    wake_signal_id: str
    content: str
    delivery_artifacts: tuple[dict[str, object], ...]
    assistant_commentaries: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    message_metadata: dict[str, object]
    delivery_status: str
    target: str
    ownership: str
    canonical_record: bool
    persisted: bool
    receipt_id: str = ""


# LLM: 冻结的触发条件只有两条，都是"答复还没落到任何权威位置"：
# ① 归属外部、有 target、但这次没送达（欠一次真实外发）；
# ② 这条路线本来要靠 canonical 承担交付，但 canonical 没落账（例如外发成功后本地写失败）。
# 附件与纯附件回复同样必须冻结——以前直接跳过附件，重投就只能重跑模型并丢掉附件。
# 撤销/静默（suppressed）与空载荷永不冻结。
# 函数用途: 把未完成的 owner 交付整封冻结到待处理唤醒上，返回是否已冻结。
def _freeze_pending_owner_delivery(
    store: ConversationStore,
    freeze: _OwnerDeliveryFreeze,
) -> bool:
    if not freeze.wake_signal_id:
        return False
    if not (str(freeze.content or "").strip() or freeze.delivery_artifacts):
        return False
    status = str(freeze.delivery_status or "").strip().lower()
    if status == "suppressed":
        return False
    obligation = _background_delivery_obligation(
        target=freeze.target,
        ownership=freeze.ownership,
    )
    external_pending = bool(obligation and status != "sent")
    canonical_pending = bool(freeze.canonical_record and not freeze.persisted)
    if not (
        external_pending
        or canonical_pending
        or _audit_capacity_report_event(freeze.request)
    ):
        return False
    # 冻结成功与否必须来自 store 的真实写入结果：唤醒不存在/已不是 pending 时
    # 载荷不会落盘，此时不能对外声称"已冻结、可重投"。
    cached = store.cache_pending_wake_delivery(
        freeze.wake_signal_id,
        {
            "schema_version": "wake-owner-delivery.v2",
            "reason": freeze.request.reason,
            "task_id": freeze.request.task_id,
            "content": freeze.content,
            "delivery_artifacts": [dict(item) for item in freeze.delivery_artifacts],
            "assistant_commentaries": list(freeze.assistant_commentaries),
            "evidence_refs": list(freeze.evidence_refs),
            "message_metadata": dict(freeze.message_metadata),
            "external_sent": bool(status == "sent"),
            "receipt_id": freeze.receipt_id,
            "ownership": freeze.ownership,
            "last_delivery_status": status,
            "created_at": time.time(),
        },
    )
    return cached is not None


# LLM: 冻结载荷的读取必须 fail-closed：schema/reason/task_id 任一不匹配都不重投，
# 载荷既可以是 v2（整封 envelope），也要能读旧 v1（只有正文）而不丢已冻结的答复。
# 函数用途: 把唤醒上的冻结载荷投影成 _FrozenOwnerDelivery。
def _frozen_owner_delivery(payload: dict[str, object]) -> _FrozenOwnerDelivery | None:
    artifacts = payload.get("delivery_artifacts")
    commentaries = payload.get("assistant_commentaries")
    refs = payload.get("evidence_refs")
    metadata = payload.get("message_metadata")
    return _FrozenOwnerDelivery(
        content=str(payload.get("content") or ""),
        delivery_artifacts=tuple(
            dict(item) for item in artifacts if isinstance(item, dict)
        )
        if isinstance(artifacts, list)
        else (),
        assistant_commentaries=tuple(
            str(item) for item in commentaries if str(item or "").strip()
        )
        if isinstance(commentaries, list)
        else (),
        evidence_refs=tuple(str(item) for item in refs if isinstance(item, str) and item.strip())
        if isinstance(refs, list)
        else (),
        message_metadata=dict(metadata) if isinstance(metadata, dict) else {},
        external_sent=payload.get("external_sent") is True,
        receipt_id=str(payload.get("receipt_id") or ""),
        ownership=str(payload.get("ownership") or ""),
    )


# LLM: canonical 行 metadata 只能在这里构造：重投时 frozen metadata 是权威，
# 只补齐本次重投的时间无关字段，避免重投把原始 task/refs/过程信息覆盖成重投时的值。
# 函数用途: 组装后台 owner 消息的 canonical metadata。
def _background_owner_message_metadata(
    request: BackgroundRunRequest,
    *,
    delivery_reason: str,
    projection_status: str,
    delivery_artifacts: tuple[dict[str, object], ...],
    evidence_refs: tuple[str, ...],
    operation_verification: dict[str, object] | None,
    frozen_retry: _FrozenOwnerDelivery | None,
) -> dict[str, object]:
    if frozen_retry is not None and frozen_retry.message_metadata:
        return {
            **frozen_retry.message_metadata,
            "background_delivery_retry_reason": delivery_reason,
        }
    metadata: dict[str, object] = {
        "reason": request.reason,
        "task_id": request.task_id,
        "delivery_artifacts": [dict(item) for item in delivery_artifacts],
        "projection_status": projection_status,
        "background_delivery_reason": delivery_reason,
        "evidence_refs": list(evidence_refs),
    }
    if operation_verification is not None:
        metadata["operation_verification"] = operation_verification
    return metadata


# LLM: 外部发送只有这一个出口：重投时若冻结载荷已证明外发成功，就绝不能再次外发
# （只补本地 canonical），否则会对真实用户重复发消息。
# 函数用途: 执行本次外部投递或复用冻结的外发事实，返回 (回执, 投递状态)。
def _background_external_delivery(
    runtime: BackgroundMainAgentRuntime,
    delivery_context: DeliveryContext,
    envelope: ReplyEnvelope,
    *,
    frozen_retry: _FrozenOwnerDelivery | None,
) -> tuple[object, str]:
    if frozen_retry is not None and frozen_retry.external_sent:
        return (
            SimpleNamespace(
                delivery_status="sent",
                channel=delivery_context.channel,
                target=delivery_context.target,
                content=envelope.content,
                evidence_refs=envelope.evidence_refs,
                receipt_id=frozen_retry.receipt_id,
                replayed=True,
            ),
            "sent",
        )
    receipt = runtime.channels.deliver(delivery_context, envelope)
    return receipt, str(getattr(receipt, "delivery_status", "") or "sent")


# LLM: 投递前的三道抑制判定必须同源：未授权投递、任务已终态、没有可交付正文。它们都表示
# “这片回复不面向 owner”，因此既不外发也不落 canonical；goal 终态交付是唯一例外。
# 函数用途: 判断这片后台回复是否在调用投递服务之前就应被抑制。
def _background_reply_suppressed(
    runtime: BackgroundMainAgentRuntime,
    request: BackgroundRunRequest,
    *,
    delivery_reason: str,
    deliver: bool,
    projection_content: str,
    has_attachments: bool,
) -> bool:
    if not deliver:
        return True
    terminal_status = _background_task_link_status(runtime.agent, request, store=runtime.store)
    goal_terminal_delivery = delivery_reason in {
        "thread_goal_blocked",
        "thread_goal_budget_limited",
        "thread_goal_usage_limited",
    }
    if (
        terminal_status in {"abandoned", "cancelled", "interrupted", "superseded"}
        and not goal_terminal_delivery
    ):
        return True
    return not str(projection_content or "").strip() and not has_attachments


# LLM: 后台按 canonical thread 的模型引用冻结一次配置，再交接 typed turn-end；不重读 owner 选择或改生命周期。
# 函数用途: 用本会话模型运行后台工作片，保留真实结束原因；用户其他窗口选模型不会改变这个任务。
def _invoke_background_main_agent(
    runtime: BackgroundMainAgentRuntime,
    thread: ConversationThread,
    request: BackgroundRunRequest,
    goal_context: GoalRuntimeContext,
    delivery_availability: tuple[bool | None, bool | None],
) -> tuple[object, dict[str, object]]:
    from ..settings.model_scope import selected_model_scope

    with selected_model_scope(runtime.agent, thread_id=thread.thread_id):
        return _invoke_background_main_agent_with_model(runtime, thread, request, goal_context, delivery_availability)


# LLM: 后台一整片（包括 Compact）复用作用域模型快照；不因此新建线程、工作片或控制记录。
# 函数用途: 用当前选定模型继续后台主代理，子代理仍从创建时的配置继承。
def _invoke_background_main_agent_with_model(
    runtime: BackgroundMainAgentRuntime,
    thread: ConversationThread,
    request: BackgroundRunRequest,
    goal_context: GoalRuntimeContext,
    delivery_availability: tuple[bool | None, bool | None],
) -> tuple[object, dict[str, object]]:
    proactive_delivery_available, transcript_delivery_available = delivery_availability
    wake_prompt = background_prompt(
        request.reason,
        goal=goal_context.goal,
        other_goals=goal_context.other_goals,
        goal_subagent_phase=goal_context.subagent_phase,
        wake_signal=request.wake_signal,
        proactive_delivery_available=proactive_delivery_available,
        transcript_delivery_available=transcript_delivery_available,
    )
    user_prompt, continuation_injection = _background_model_inputs(
        request,
        task_objective=goal_context.task_objective,
        wake_prompt=wake_prompt,
    )
    activity_sink = BackgroundMainActivitySink(
        runtime.agent,
        thread_id=thread.thread_id,
        task_id=request.task_id,
    )
    try:
        result = _run_background_main_turn_with_compact(
            runtime,
            thread,
            request,
            goal_context,
            user_prompt=user_prompt,
            continuation_injection=continuation_injection,
            proactive_delivery_available=proactive_delivery_available,
            activity_sink=activity_sink,
        )
    except _BackgroundCompactSliceYield:
        activity_sink.finish()
        raise
    except Exception:
        activity_sink.fail()
        raise
    activity_sink.finish()
    from ..turn_end import result_turn_end_reason

    snapshot = activity_sink.display_history_snapshot()
    snapshot["turn_end_reason"] = result_turn_end_reason(result)
    snapshot["goal_continuation_allowed"] = (
        snapshot["turn_end_reason"] == "completed" or should_continue_task(result)[0]
    )
    return result, snapshot


# LLM: Background lifecycle wakes continue the same authoritative active turn. Like foreground
# Gateway and child runners, context overflow must compact the durable transcript and retry while
# carrying typed tool and steering state; a new scheduler slice must not be the retry mechanism.
# 函数用途: 后台主代理在同一工作片压缩并续跑；每次模型执行同时换工具显示批次，避免同号覆盖旧工具。
def _run_background_main_turn_with_compact(
    runtime: BackgroundMainAgentRuntime,
    thread: ConversationThread,
    request: BackgroundRunRequest,
    goal_context: GoalRuntimeContext,
    *,
    user_prompt: str,
    continuation_injection: list[str],
    proactive_delivery_available: bool | None,
    activity_sink: BackgroundMainActivitySink,
) -> object:
    current = thread
    carried_archive_tool_calls: list[dict[str, object]] | None = None
    carried_active_turn_user_inputs: list[dict[str, object]] = []
    for _attempt in range(8):
        history_seed = _background_history_seed_or_raise(
            runtime.agent,
            runtime.store,
            current,
            request,
            proactive_delivery_available=proactive_delivery_available,
        )
        run_params = _run_params(
            current.thread_id,
            request,
            runtime.agent,
            goal_context=goal_context,
            proactive_delivery_available=proactive_delivery_available,
            thread=current,
            history_seed=history_seed,
        )
        if carried_archive_tool_calls is None:
            carried_archive_tool_calls = list(run_params.carried_archive_tool_calls or [])
        else:
            run_params.carried_archive_tool_calls = list(carried_archive_tool_calls)
        run_params.carried_active_turn_user_inputs = list(carried_active_turn_user_inputs)
        run_params.inject = [
            context_markdown(
                agent=runtime.agent,
                store=runtime.store,
                thread=current,
                request=request,
                proactive_delivery_available=proactive_delivery_available,
                # 已经带上 canonical 历史时不再重复注入最近消息副本：
                # 两份历史会重复计费、并且摘要副本没有工具细节。
                include_recent_messages=history_seed is None,
            ),
            *continuation_injection,
        ]
        run_params.on_chunk = activity_sink
        activity_sink.begin_model_attempt(_attempt + 1)
        result = runtime.agent.run(user_prompt, params=run_params)
        if str(getattr(result, "runtime_status", "") or "").strip().lower() != ("context_overflow"):
            return result
        (
            carried_archive_tool_calls,
            carried_active_turn_user_inputs,
        ) = _next_background_overflow_carry(
            runtime.agent,
            run_params,
            result,
            carried_archive_tool_calls,
            carried_active_turn_user_inputs,
        )
        refreshed = _compact_background_main_thread(
            runtime,
            current,
            current_prompt=user_prompt,
            activity_sink=activity_sink,
            run_params=run_params,
            carried_archive_tool_calls=carried_archive_tool_calls,
        )
        if refreshed.compact_generation <= current.compact_generation:
            from .active_turn_compact import (
                ActiveTurnArchiveCompactRequest,
                compact_carried_active_turn_archive,
            )

            active_turn_compact = compact_carried_active_turn_archive(
                runtime.agent,
                runtime.store,
                refreshed,
                carried_archive_tool_calls,
                ActiveTurnArchiveCompactRequest(
                    task_attributes=run_params.task_attributes,
                    request_id=str(run_params.request_id or request.task_id or ""),
                    attempt_id=str(run_params.attempt_id or run_params.request_id or ""),
                    task_prompt=user_prompt,
                    progress_callback=activity_sink.write_conversation_compact_progress,
                    interrupt_check=is_interrupted,
                ),
            )
            if not active_turn_compact.compacted:
                raise RuntimeError(
                    "background main thread cannot compact the overflowing active turn"
                )
            refreshed = active_turn_compact.thread
        current = refreshed
    raise _BackgroundCompactSliceYield(
        "background main compact slice advanced eight generations and will resume"
    )


# LLM: Compact retry carry contains only structured runtime records. It never reconstructs work
# from assistant prose, and released pre-provider steering ids return to their canonical mailbox.
# 函数用途: 合并后台超限轮已经完成的工具与插话，供压缩后的同一轮安全续做。
def _next_background_overflow_carry(
    agent: object,
    run_params: RunParams,
    result: object,
    carried_archive_tool_calls: list[dict[str, object]],
    carried_active_turn_user_inputs: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    from ..agent_core.runtime_mixin import release_active_turn_inputs_for_compact
    from .active_turn_input import (
        exclude_active_turn_user_input_ids,
        merge_active_turn_user_inputs,
    )

    released_input_ids = release_active_turn_inputs_for_compact(agent, run_params)
    result_archive = [
        dict(item)
        for item in list(getattr(result, "archive_tool_calls", None) or [])
        if isinstance(item, dict)
    ]
    next_archive = result_archive or carried_archive_tool_calls
    next_inputs = exclude_active_turn_user_input_ids(
        merge_active_turn_user_inputs(
            carried_active_turn_user_inputs,
            getattr(result, "active_turn_user_inputs", None),
        ),
        released_input_ids,
    )
    return next_archive, next_inputs


# LLM: The latest ConversationThread and canonical Compact CAS are the only retry authority. The
# tooling's pure archive reducer supplies pending one-call tools; interrupt crosses summary/commit, display does not.
# 函数用途: 以后台主代理已完成历史和当前工具归档可中断地压缩，并返回同一轮继续用的最新线程。
def _compact_background_main_thread(
    runtime: BackgroundMainAgentRuntime,
    current: ConversationThread,
    *,
    current_prompt: str,
    activity_sink: BackgroundMainActivitySink,
    run_params: RunParams,
    carried_archive_tool_calls: list[dict[str, object]],
) -> ConversationThread:
    from ..tooling.tool_search_state import pending_carried_loaded_tool_names
    from .compact import ConversationCompactOptions, prepare_conversation_context
    from .compact_provider_surface import ConversationCompactModelSurface

    latest, load_error = runtime.store.load_thread_report(current.thread_id)
    if load_error is not None or latest is None:
        raise RuntimeError("background main conversation thread could not be reloaded")
    compact = prepare_conversation_context(
        runtime.agent,
        runtime.store,
        latest,
        options=ConversationCompactOptions(
            current_prompt=str(current_prompt or ""),
            force=True,
            progress_callback=activity_sink.write_conversation_compact_progress,
            interrupt_check=is_interrupted,
            model_surface=ConversationCompactModelSurface(
                allowed_tools=(
                    tuple(run_params.allowed_tools)
                    if run_params.allowed_tools is not None
                    else None
                ),
                prompt_files=tuple(run_params.prompt_files or ()),
                system_prompt_override=run_params.system_prompt_override,
                context_scope=str(run_params.context_scope or "conversation"),
                loaded_tool_names=tuple(
                    sorted(
                        pending_carried_loaded_tool_names(carried_archive_tool_calls)
                    )
                ),
            ),
        ),
    )
    return compact.thread


# LLM: 会话运行时 keeps child completion inside the originating active turn. When an exact task
# objective exists, keep it in the User Task slot and move the synthetic wake instruction into
# runtime injection; other background event kinds retain their existing one-shot prompt.
# 函数用途: 选择后台模型真正看到的用户任务和追加唤醒说明，避免子代理回报后把原任务换掉。
def _background_model_inputs(
    request: BackgroundRunRequest,
    *,
    task_objective: str,
    wake_prompt: str,
) -> tuple[str, list[str]]:
    objective = str(task_objective or "").strip()
    if _is_active_turn_lifecycle_continuation(request, objective):
        return objective, ["[active-turn-continuation]\n" + str(wake_prompt or "").strip()]
    return str(wake_prompt or ""), []


# LLM: Detached Audit quota notices share a lifecycle reason for delivery but are not a slice
# of the root task's active turn. The typed wake metadata, never message prose, owns this split.
# 函数用途: 判断一条结构化唤醒是否应沿用原主任务的用户目标与工具历史。
def _is_active_turn_lifecycle_continuation(
    request: BackgroundRunRequest,
    task_objective: str,
) -> bool:
    return (
        bool(str(task_objective or "").strip())
        and str(request.reason or "").strip().lower() in SUBAGENT_LIFECYCLE_WAKE_REASONS
        and not _wake_payload_is_audit_provider_quota(request.wake_signal)
    )


# LLM: canonical final 保存正文、过程和 host-owned turn-end；commentary 不携带终态，缺失原因不从文字补造。
# 外发结果只作为 metadata 事实，不参与“要不要给 owner 留下这条回复”的判定。
# 函数用途: 幂等保存后台回复及长度限制原因，并返回结构化落账结果。
def _commit_background_response(
    runtime: BackgroundMainAgentRuntime,
    request: BackgroundRunRequest,
    delivery_context: DeliveryContext,
    *,
    receipt: object,
    delivery_status: str,
    committed_content: str,
    canonical_record: bool,
    transcript_route: bool,
    evidence_refs: tuple[str, ...],
    message_metadata: dict[str, object],
    assistant_commentaries: tuple[str, ...] = (),
    display_snapshot: dict[str, object] | None = None,
    outbox_frozen: bool = False,
    audit_refs_settled: bool = False,
) -> BackgroundDeliveryCommit:
    """Persist one owner-visible reply, independently of its transport outcome."""

    status = str(delivery_status or "").strip().lower()
    has_body = bool(str(committed_content or "").strip()) or bool(
        message_metadata.get("delivery_artifacts")
    )
    if status == "suppressed":
        # 投递层判定这是内部协议正文：既不外发，也不能落成用户消息。
        return BackgroundDeliveryCommit(
            content=committed_content,
            delivery_status=status,
            persisted=False,
            commit_kind="suppressed",
            transcript_route=transcript_route,
            outbox_frozen=outbox_frozen,
        )
    if status != "sent" and (not canonical_record or not has_body):
        # 没有可交付正文，或这条路线既不能外发也没有本地归属：只保留冻结重投事实。
        return BackgroundDeliveryCommit(
            content=committed_content,
            delivery_status=status,
            persisted=False,
            commit_kind="outbox_pending" if outbox_frozen else "none",
            transcript_route=transcript_route,
            outbox_frozen=outbox_frozen,
        )
    if display_snapshot:
        message_metadata = {
            **message_metadata,
            "background_transcript_request_id": display_snapshot.get("request_id", ""),
        }
    from ..turn_end import normalize_turn_end_reason

    end_reason = normalize_turn_end_reason((display_snapshot or {}).get("turn_end_reason"))
    delivery_key = _background_delivery_idempotency_key(request)
    _append_owner_commentaries(
        runtime.store,
        request,
        delivery_context,
        message_metadata=message_metadata,
        delivery_key=delivery_key,
        assistant_commentaries=assistant_commentaries,
    )
    message_request = {
        "thread_id": request.thread_id,
        "role": "assistant",
        "content": committed_content,
        "channel": delivery_context.channel,
        "metadata": {
            **message_metadata, "assistant_part_id": "final",
            **({"background_display_turn": display_snapshot} if display_snapshot else {}),
            **({"turn_end_reason": end_reason} if end_reason else {}),
        },
    }
    if delivery_key:
        message_entry = runtime.store.append_message_once(
            message_request,
            dedupe_key=f"owner_delivery:{delivery_key}",
        )
    else:
        message_entry = runtime.store.append_message(message_request)
    if audit_refs_settled:
        # 这次只是补本地落账：审计交付回执在真正外发成功时已经记过，绝不能重复记一遍。
        pass
    elif status == "sent":
        _record_delivered_audit_refs(runtime.agent, receipt)
    elif transcript_route and _audit_finding_report_event(request):
        # CLI/Gateway transcript routes have no provider receipt. Their local
        # transcript append is the durable commit and therefore the receipt.
        # 外发路线失败时绝不能冒领这条回执：审计台账只认真正的本地权威交付面。
        _record_transcript_audit_refs(
            runtime.agent,
            evidence_refs,
            message_entry=message_entry,
            channel=delivery_context.channel,
        )
    return BackgroundDeliveryCommit(
        content=committed_content,
        delivery_status=status,
        persisted=True,
        message_id=str(getattr(message_entry, "message_id", "") or ""),
        commit_kind="external_delivery" if status == "sent" else "canonical_record",
        transcript_route=transcript_route,
        outbox_frozen=outbox_frozen,
    )


# LLM: 过程回复与 final 共享同一批 metadata 和同一份投递幂等键；重投时必须逐条去重，
# 不能因为重放而把 commentary 追加第二遍。
# 函数用途: 按 typed part 顺序幂等追加一片后台工作的过程回复。
def _append_owner_commentaries(
    store: ConversationStore,
    request: BackgroundRunRequest,
    delivery_context: DeliveryContext,
    *,
    message_metadata: dict[str, object],
    delivery_key: str,
    assistant_commentaries: tuple[str, ...],
) -> None:
    for index, commentary in enumerate(assistant_commentaries, start=1):
        text = str(commentary or "").strip()
        if not text:
            continue
        commentary_request = {
            "thread_id": request.thread_id,
            "role": "assistant",
            "content": text,
            "channel": delivery_context.channel,
            "metadata": {
                **message_metadata,
                "assistant_part_id": f"commentary:{index}",
                "process": True,
            },
        }
        if delivery_key:
            store.append_message_once(
                commentary_request,
                dedupe_key=f"owner_delivery:{delivery_key}:commentary:{index}",
            )
        else:
            store.append_message(commentary_request)


def _background_evidence_refs(
    request: BackgroundRunRequest,
) -> tuple[str, ...]:
    wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    refs = wake.get("evidence_refs")
    if not isinstance(refs, list):
        return ()
    return tuple(
        dict.fromkeys(
            str(item).strip() for item in refs if isinstance(item, str) and str(item).strip()
        )
    )


def _background_delivery_evidence_refs(
    request: BackgroundRunRequest,
) -> tuple[str, ...]:
    wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    return _audit_finding_payload_delivery_refs(wake)


def _audit_finding_payload_delivery_refs(
    wake: dict[str, Any],
) -> tuple[str, ...]:
    metadata = wake.get("metadata") if isinstance(wake.get("metadata"), dict) else {}
    typed = metadata.get("delivery_evidence_refs")
    if isinstance(typed, list):
        refs = tuple(
            dict.fromkeys(
                str(item).strip() for item in typed if isinstance(item, str) and str(item).strip()
            )
        )
        if refs:
            return refs

    # Compatibility for durable wakes created before the typed delivery field
    # existed.  Only exact Audit refs for this finding's own watch may acquire
    # delivery authority; event ids, paths and URLs stay explanatory evidence.
    watch_id = str(metadata.get("watch_id") or "").strip()
    refs = wake.get("evidence_refs")
    if not watch_id or not isinstance(refs, list):
        return ()
    from ..ingestion.harvester import parse_audit_source_ref

    selected: list[str] = []
    for item in refs:
        source_ref = str(item).strip() if isinstance(item, str) else ""
        parsed = parse_audit_source_ref(source_ref)
        if parsed is not None and parsed[0] == watch_id:
            selected.append(source_ref)
    return tuple(dict.fromkeys(selected))


def _background_delivery_idempotency_key(request: BackgroundRunRequest) -> str:
    """Return the stable runtime identity for one background delivery attempt."""
    wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    wake_signal_id = str(wake.get("wake_signal_id") or "").strip()
    if wake_signal_id:
        return f"background-wake:{wake_signal_id}"
    metadata = wake.get("metadata") if isinstance(wake.get("metadata"), dict) else {}
    scheduler_run_id = str(metadata.get("scheduler_run_id") or "").strip()
    if scheduler_run_id:
        return f"scheduler-run:{scheduler_run_id}"
    return ""


def _record_delivered_audit_refs(agent: object, receipt: object) -> None:
    refs = tuple(getattr(receipt, "evidence_refs", ()) or ())
    owner_home = str(
        getattr(getattr(agent, "home_paths", None), "owner_home_dir", "") or ""
    ).strip()
    receipt_id = str(getattr(receipt, "receipt_id", "") or "").strip()
    if not refs or not owner_home or not receipt_id:
        return
    from ..ingestion.harvester import record_audit_delivery_refs

    record_audit_delivery_refs(
        Path(owner_home),
        refs,
        receipt_id=receipt_id,
        channel=str(getattr(receipt, "channel", "") or ""),
    )


def _record_transcript_audit_refs(
    agent: object,
    refs: tuple[str, ...],
    *,
    message_entry: object,
    channel: str,
) -> None:
    """Commit exact Audit refs after an owner-visible local transcript write."""

    owner_home = str(
        getattr(getattr(agent, "home_paths", None), "owner_home_dir", "") or ""
    ).strip()
    message_id = str(getattr(message_entry, "message_id", "") or "").strip()
    if not refs or not owner_home or not message_id:
        return
    from ..ingestion.harvester import record_audit_delivery_refs

    record_audit_delivery_refs(
        Path(owner_home),
        refs,
        receipt_id=message_id,
        channel=str(channel or "internal"),
    )


# LLM: 后台轮与前台轮必须复用同一个 public operation projection；不得在会话层重算工具终态。
# 函数用途: 从 AgentRunResult 提取已由 canonical operation ledger 生成的安全公开核验摘要。
def _public_result_operation_verification(result: object) -> dict[str, object]:
    from ..tooling.operation_verification import public_operation_verification

    projected = public_operation_verification(getattr(result, "operation_verification", None))
    return projected


def _message_tool_delivery_satisfied(
    request: BackgroundRunRequest,
    deliveries: tuple[dict[str, object], ...],
) -> bool:
    """A proactive turn needs one request-bound typed delivery path."""
    return any(_delivery_satisfies_request(request, item) for item in deliveries)


def _audit_finding_report_event(request: BackgroundRunRequest) -> bool:
    if str(request.reason or "").strip().lower() != "audit_finding":
        return False
    wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    metadata = wake.get("metadata") if isinstance(wake.get("metadata"), dict) else {}
    return (
        str(metadata.get("schema_version") or "") == "audit-finding-event.v1"
        and metadata.get("requires_llm_report") is True
        and bool(str(metadata.get("finding_id") or "").strip())
        and bool(_background_delivery_evidence_refs(request))
    )


def _audit_capacity_report_event(request: BackgroundRunRequest) -> bool:
    if str(request.reason or "").strip().lower() != "audit_capacity_alert":
        return False
    wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    metadata = wake.get("metadata") if isinstance(wake.get("metadata"), dict) else {}
    return (
        str(metadata.get("schema_version") or "") == "audit-capacity-event.v2"
        and bool(str(metadata.get("audit_id") or "").strip())
        and str(metadata.get("capacity_state") or "").strip().lower() in {"alert", "recovered"}
    )


def _audit_owner_report_event(request: BackgroundRunRequest) -> bool:
    """Return whether this wake is complete only after owner delivery commits."""

    return _audit_finding_report_event(request) or _audit_capacity_report_event(request)


# LLM: 冻结载荷的读取必须 fail-closed：只接受本协议 schema、reason/task_id 与唤醒完全一致、
# 且"有正文或有附件"的载荷（纯附件回复同样合法）。v1 只有正文，v2 才有整封 envelope。
# 函数用途: 读取一条唤醒上已冻结的 owner 交付载荷，不匹配返回 None。
def _cached_owner_delivery(signal: WakeSignal) -> dict[str, object] | None:
    raw_metadata = getattr(signal, "metadata", None)
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    delivery = metadata.get("owner_delivery")
    if not isinstance(delivery, dict):
        return None
    if (
        str(delivery.get("schema_version") or "")
        not in {"wake-owner-delivery.v1", "wake-owner-delivery.v2"}
        or str(delivery.get("reason") or "") != str(signal.reason or "")
        or str(delivery.get("task_id") or "") != str(signal.root_task_id or "")
    ):
        return None
    has_body = bool(str(delivery.get("content") or "").strip())
    artifacts = delivery.get("delivery_artifacts")
    has_attachments = bool(
        [item for item in artifacts if isinstance(item, dict)] if isinstance(artifacts, list) else []
    )
    if not has_body and not has_attachments:
        return None
    return dict(delivery)


def _delivery_satisfies_request(
    request: BackgroundRunRequest,
    delivery: dict[str, object],
) -> bool:
    if (
        str(delivery.get("delivery_status") or "").strip().lower() != "sent"
        or delivery.get("source_owner_delivery") is not True
        or not str(delivery.get("receipt_id") or "").strip()
    ):
        return False
    reason = str(request.reason or "").strip().lower()
    if reason == "scheduled_job_due":
        return True
    if reason != "audit_finding" or not _audit_finding_report_event(request):
        return False
    delivered_refs = tuple(
        dict.fromkeys(
            str(ref).strip()
            for ref in (
                delivery.get("evidence_refs")
                if isinstance(delivery.get("evidence_refs"), list)
                else []
            )
            if str(ref).strip()
        )
    )
    required_refs = _background_delivery_evidence_refs(request)
    return (
        bool(str(delivery.get("content") or "").strip())
        and len(delivered_refs) == len(required_refs)
        and set(delivered_refs) == set(required_refs)
    )


def _message_tool_delivery_reason(request: BackgroundRunRequest) -> str:
    if str(request.reason or "").strip().lower() == "audit_finding":
        return "audit_finding_report"
    return "scheduled_message_tool_delivery"


def _mirror_message_tool_deliveries(
    store: ConversationStore,
    request: BackgroundRunRequest,
    delivery_context: DeliveryContext,
    deliveries: tuple[dict[str, object], ...],
) -> str:
    """Mirror already-sent source replies exactly once without re-delivering them."""
    rendered: list[str] = []
    for item in deliveries:
        if not _delivery_satisfies_request(request, item):
            continue
        receipt_id = str(item.get("receipt_id") or "").strip()
        if not receipt_id:
            continue
        content = str(item.get("content") or "")
        attachments = [
            dict(ref)
            for ref in (
                item.get("attachments") if isinstance(item.get("attachments"), list) else []
            )
            if isinstance(ref, dict)
        ]
        store.append_message_once(
            {
                "thread_id": request.thread_id,
                "role": "assistant",
                "content": content,
                "channel": str(item.get("channel") or delivery_context.channel),
                "metadata": {
                    "reason": request.reason,
                    "task_id": request.task_id,
                    "delivery_artifacts": attachments,
                    "background_delivery_reason": _message_tool_delivery_reason(request),
                    "evidence_refs": [
                        str(ref).strip()
                        for ref in (
                            item.get("evidence_refs")
                            if isinstance(item.get("evidence_refs"), list)
                            else []
                        )
                        if str(ref).strip()
                    ],
                    "message_tool_delivery": {
                        "receipt_id": receipt_id,
                        "delivery_status": "sent",
                        "deduplicated": item.get("deduplicated") is True,
                    },
                },
            },
            dedupe_key=f"message_tool_delivery:{receipt_id}",
        )
        if content.strip():
            rendered.append(content)
    return "\n\n".join(rendered)


def _channel_attachments(
    artifacts: tuple[dict[str, object], ...],
) -> tuple[ChannelAttachment, ...]:
    attachments: list[ChannelAttachment] = []
    for item in artifacts:
        path = str(item.get("path") or "").strip()
        if not path or item.get("ok") is not True:
            continue
        try:
            size_bytes = max(0, int(item.get("size_bytes") or 0))
        except (TypeError, ValueError):
            size_bytes = 0
        attachments.append(
            ChannelAttachment(
                artifact_id=str(item.get("artifact_id") or ""),
                path=path,
                name=str(item.get("name") or Path(path).name),
                kind=str(item.get("kind") or "file"),
                sha256=str(item.get("sha256") or ""),
                size_bytes=size_bytes,
            )
        )
    return tuple(attachments)


# LLM: Goal continuation is a real 会话运行时 turn, so its terminal assistant message must enter
# the canonical owner transcript even while the Goal remains active. Child lifecycle/capability
# wakes are still internal control events; current child facts and unread envelopes, not the
# slice-start snapshot, determine whether the integration response is ready to publish.
# 函数用途: 决定后台模型回复是否进入用户会话；原开始快照不能吞掉长工作片已吸收结果后的最终汇报。
def _background_delivery_decision(
    agent: object,
    request: BackgroundRunRequest,
    *,
    store: ConversationStore | None = None,
    resolved_channel: str | None = None,
    resolved_route_supports_proactive: bool | None = None,
    resolved_route_supports_transcript: bool | None = None,
) -> tuple[bool, str]:
    """Keep partial child integration internal and publish one fresh terminal turn."""
    task_status = _background_task_link_status(agent, request, store=store)
    reason = str(request.reason or "").strip().lower()
    wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    metadata = wake.get("metadata") if isinstance(wake.get("metadata"), dict) else {}
    if _is_internal_audit_source_worker_lifecycle_metadata(
        reason=reason,
        root_task_id=str(wake.get("root_task_id") or request.task_id or ""),
        metadata=metadata,
    ):
        return False, "audit_source_worker_lifecycle_internal"
    goal_status = _matching_goal_status(store, request)
    if goal_status == "complete":
        return True, "thread_goal_completion"
    if goal_status in {"blocked", "budget_limited", "usage_limited"}:
        return True, f"thread_goal_{goal_status}"
    if task_status in {"abandoned", "cancelled", "interrupted", "superseded"}:
        return False, f"task_{task_status}"
    task_completed = task_status == "completed"
    if reason == "thread_goal_continue":
        if task_completed:
            return True, "thread_goal_completion"
        # 会话运行时 starts an ordinary turn when an active Goal continues while the thread is idle.
        # The turn may keep the Goal active, but its final assistant item still belongs to the
        # rollout and remains visible. Suppressing it here made the provider spend/output tokens
        # while the owner saw only a finished thinking block and an apparently vanished main row.
        return True, "thread_goal_progress"
    if goal_status == "active" and reason in SUBAGENT_LIFECYCLE_WAKE_REASONS:
        return False, "thread_goal_lifecycle_internal"
    if reason in {"subagent_capability_request_open", "subagent_capability_granted"}:
        # 会话运行时 treats child approvals as active-turn control events, not assistant replies.
        # Our durable wake still lets the parent route the request or continue the exact child,
        # but publishing that intermediate model draft races the child runner and can place an
        # already-stale "still waiting" paragraph below newer TUI activity.  The live child panel
        # remains the owner-visible progress source; the terminal runner wake owns the final report.
        return False, f"{reason}_internal"
    if reason == "user_guidance" and not task_completed:
        # /btw already has a deterministic control acknowledgement.  Applying the
        # guidance is an internal continuation; a second model status paragraph is
        # noisy and can be stale before the next task checkpoint.
        return False, "user_guidance_applied_internal"
    if reason in _SCHEDULED_WAKE_REASONS and _internal_subagent_continuation(request):
        # wait/自动巡场只是内部续推面，不是用户通知面。即使最后一个 child 恰好在本轮
        # 结束前转为终态，也不能把模型的调度碎碎念送进普通聊天；runner completion
        # 的 wake（或 observation fallback）才是首选整合入口。只有本轮 runtime 已把
        # 精确任务链接转为 completed，才发送模型的自然最终回复。
        if task_completed:
            return True, "internal_scheduled_completion"
        return False, "internal_scheduled_continuation"
    if reason == "audit_finding":
        valid_report_event = (
            str(metadata.get("schema_version") or "") == "audit-finding-event.v1"
            and metadata.get("requires_llm_report") is True
            and bool(str(metadata.get("finding_id") or "").strip())
            and bool(_background_delivery_evidence_refs(request))
        )
        transcript_route = (
            bool(
                resolved_channel is not None
                and supports_transcript_delivery(str(resolved_channel or ""))
            )
            if resolved_route_supports_transcript is None
            else resolved_route_supports_transcript
        )
        if valid_report_event and transcript_route:
            # 通道运行时's announce fallback and 会话运行时's local transcript commit
            # share the same invariant: a model-authored reply still needs one
            # durable owner-visible commit when no proactive provider exists.
            # Real IM routes keep the message-tool receipt requirement below;
            # only CLI/internal routes use the ordinary conversation append.
            return True, "audit_finding_transcript"
        if valid_report_event and resolved_route_supports_proactive is False:
            return False, "audit_finding_delivery_unavailable"
        return (
            (False, "audit_finding_message_tool_only")
            if valid_report_event
            else (False, "audit_finding_not_reportable")
        )
    if reason != "subagent_runner_finished":
        return True, "non_subagent_completion"
    status = str(metadata.get("status") or "").strip().upper()
    if status != "DONE":
        return True, "subagent_non_success_terminal"
    root_task_id = str(wake.get("root_task_id") or request.task_id or "").strip()
    if not root_task_id:
        return True, "subagent_root_unknown"
    related, state_error = _related_subagent_runs(agent, root_task_id)
    if state_error:
        # A broken or missing child record cannot prove that the current task tree
        # has settled.  Keep the model's integration turn internal unless the exact
        # durable root-task link already reached completed.  This mirrors the same
        # terminal-state gate used when all child rows are readable and prevents an
        # unrelated historical parse error from leaking partial child chatter.
        if task_completed:
            return True, f"root_task_completed_with_{state_error}"
        return False, state_error
    from ..subagents.models import SUBAGENT_ENDED_STATUSES, task_status_in

    if any(
        not task_status_in(getattr(task, "status", ""), SUBAGENT_ENDED_STATUSES) for task in related
    ):
        return False, "partial_subagent_success"
    return _terminal_subagent_delivery_decision(agent, store, wake, root_task_id)


# LLM: Owner delivery follows the same durable mailbox barrier as task closeout.
# Only the exact active batch is sampled; any other same-root lifecycle envelope
# suppresses this draft without interpreting child prose or terminal tree shape.
# 函数用途: 根据当前批次之外是否还有未读信封，决定抑制或发送最终汇总。
def _terminal_subagent_delivery_decision(
    agent: object,
    store: ConversationStore | None,
    wake: dict[str, Any],
    root_task_id: str,
) -> tuple[bool, str]:
    from .task_promotion import conversation_task_has_unseen_lifecycle_wakes

    pending = conversation_task_has_unseen_lifecycle_wakes(
        store or getattr(agent, "conversation_store", None),
        root_task_id,
        active_wake_signal_ids=_background_wake_signal_ids(wake),
    )
    return (
        (False, "subagent_completion_mailbox_pending")
        if pending
        else (True, "root_subagents_terminal")
    )


# LLM: 后台路线的归属只由"部署声明了什么"决定，不由"此刻能不能发"决定。
# - local: 权威会话就是 owner 的读取面（transcript 路线），canonical 提交即交付；
# - external: 部署声明过该通道且声明它支持 proactive 外发 ⇒ 这条路线欠 owner 一次真实外送，
#   adapter 掉线/凭据缺失/工厂失败都只影响"这次能不能发"，不能抹掉义务；
# - undeclared: 部署没有声明过这个通道 ⇒ 永不外发（fail-closed），canonical 是唯一归属。
_ROUTE_LOCAL = "local"
_ROUTE_EXTERNAL = "external"
_ROUTE_UNDECLARED = "undeclared"


# LLM: 声明与可用分离是本模块的硬边界：能力探测（supports_proactive）会被 adapter 生命周期影响，
# 归属判定必须读声明（declares_channel + declared_proactive）。改这里要同步检查
# delivery/registry.py::declares_channel 与 delivery/service.py 的同名投影。
# 函数用途: 判断一条后台路线的归属类别（本地/外部/未声明）。
def _background_route_ownership(channels: object, channel: str) -> str:
    key = str(channel or "").strip().lower()
    if not key or _route_supports_transcript(channels, key):
        return _ROUTE_LOCAL
    if not _channel_declares_transport(channels, key):
        return _ROUTE_UNDECLARED
    return _ROUTE_EXTERNAL if _channel_declares_proactive(channels, key) else _ROUTE_UNDECLARED


# LLM: 一条路线只有在"归属外部 + 有真实目标"时才欠用户一次外发。未声明通道、
# 无 target 的本地路线都不在此列，也不得被当成 IM 通道打开。
# 函数用途: 判断当前后台路线是否必须真正外发才算完成。
def _background_delivery_obligation(*, target: str, ownership: str) -> bool:
    return bool(str(target or "").strip() and ownership == _ROUTE_EXTERNAL)


# LLM: 一条答复的落账归属：transcript 路线或未声明通道由 canonical 承担交付，
# 只有"声明外发 + 有目标"的路线才把 canonical 让给外发（靠冻结重投防丢失）。
# 类用途: 承载一次后台投递的路线事实。
@dataclass(frozen=True)
class _OwnerDeliveryRoute:
    transcript_route: bool
    ownership: str
    obligation: bool
    canonical_record: bool


# LLM: 路线事实只能由"投递边界声明 + 可信 context"推出；禁止在调用点各自推算，
# 否则 canonical 归属、冻结触发和唤醒确认会各算一套。
# 函数用途: 解析一次后台投递的路线事实。
def _background_delivery_route(
    channels: object,
    delivery_context: DeliveryContext,
    *,
    route_supports_transcript: bool | None = None,
) -> _OwnerDeliveryRoute:
    transcript_route = bool(
        supports_transcript_delivery(delivery_context.channel)
        if route_supports_transcript is None
        else route_supports_transcript
    )
    ownership = _background_route_ownership(channels, delivery_context.channel)
    obligation = _background_delivery_obligation(
        target=delivery_context.target,
        ownership=ownership,
    )
    return _OwnerDeliveryRoute(
        transcript_route=transcript_route,
        ownership=ownership,
        obligation=obligation,
        canonical_record=bool(transcript_route or not obligation),
    )


# LLM: 没有可落账正文时的统一结果：正文只是给上层的投影，persisted 必须为 False。
# 函数用途: 构造一条未提交的投递结果（内部抑制/空正文/终态任务）。
def _uncommitted_delivery(content: str, delivery_status: str) -> BackgroundDeliveryCommit:
    return BackgroundDeliveryCommit(
        content=str(content or ""),
        delivery_status=delivery_status,
        persisted=False,
        commit_kind=delivery_status if delivery_status == "suppressed" else "none",
    )


# LLM: 唤醒确认是结构化事实判断，不再按事件类型默认放行：只有“外发成功”或“答复已经
# 落到自己的权威记录且这条路线本来就不欠外发”才算处理完成。既没送达也没记账的唤醒必须
# 留在队列里，由冻结重投路径补发正文，绝不重新调用模型。
# 函数用途: 判断一条后台唤醒是否已经真正完成了对 owner 的交付。
def _background_owner_delivery_committed(
    request: BackgroundRunRequest,
    *,
    target: str,
    ownership: str,
    commit: BackgroundDeliveryCommit,
    canonical_record: bool = False,
) -> bool:
    """Acknowledge owner-facing wakes only after their real delivery commit."""

    status = str(commit.delivery_status or "").strip().lower()
    if status == "sent":
        # 外发成功但这条路线本来要靠 canonical 承担交付时，本地落账失败同样不能确认：
        # 否则"用户读过的那份记录"永远缺一条，而且没人会再补。
        return bool(commit.persisted) or not canonical_record
    if status == "suppressed":
        # 投递层显式判定这是内部协议内容：交付义务归零，重投只会得到同样结论。
        # 审计类唤醒沿用原有更严格判定，避免未上报的发现被静默吞掉。
        return not _audit_owner_report_event(request)
    if _background_delivery_obligation(target=target, ownership=ownership):
        # 欠 owner 一次真实外发：canonical 记录只是安全网，不能替代送达。
        # 这里必须能"欠着不确认"，即使 adapter 当前不可用。
        return False
    if commit.persisted:
        return True
    # 既没有外发义务，也没有留下任何权威记录：唤醒不能确认，否则答复会静默消失。
    return False


# LLM: 生产与测试投递服务应显式声明 transcript 能力；旧测试替身没有该
# 方法时只回退到同一份内置路由声明，绝不从模型正文或 adapter 失败推断。
# 函数用途: 读取当前投递边界对本地权威会话交付的结构化能力。
def _route_supports_transcript(channels: object, channel: str) -> bool:
    probe = getattr(channels, "supports_transcript", None)
    if callable(probe):
        return bool(probe(channel))
    return supports_transcript_delivery(channel)


# LLM: 归属用的"部署声明"探测：优先问投递服务的 declares_channel；测试替身没有该方法时
# 回退到内置的 proactive 推送通道声明表，仍然只读声明、不探测 adapter 是否在线。
# 函数用途: 判断投递边界是否声明过该外发通道。
def _channel_declares_transport(channels: object, channel: str) -> bool:
    probe = getattr(channels, "declares_channel", None)
    if callable(probe):
        return bool(probe(channel))
    return str(channel or "").strip().lower() in PROACTIVE_PUSH_CHANNELS


# LLM: 声明能力与可用性分离：declared_proactive 读注册表里的能力声明，adapter 当前是否
# 构建成功、健康与否都不改变它。
# 函数用途: 判断该通道被声明为支持 proactive 外发。
def _channel_declares_proactive(channels: object, channel: str) -> bool:
    probe = getattr(channels, "declared_proactive", None)
    if callable(probe):
        return bool(probe(channel))
    return str(channel or "").strip().lower() in PROACTIVE_PUSH_CHANNELS


# LLM: 读取 exact wake goal/task；冲突和损坏是未知状态，不得当作没有目标或推断完成。
# 函数用途: 为后台结果投影读取真实目标状态，不修改生命周期。
def _matching_goal_status(
    store: ConversationStore | None,
    request: BackgroundRunRequest,
) -> str:
    if store is None:
        return ""
    try:
        wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
        metadata = wake.get("metadata") if isinstance(wake.get("metadata"), dict) else {}
        goal = store.load_goal(request.thread_id, goal_id=str(metadata.get("goal_id") or ""), task_id=str(request.task_id or "").strip())
    except Exception:
        return "state_conflict"
    if goal is None:
        return ""
    if str(getattr(goal, "task_id", "") or "").strip() != str(request.task_id or "").strip():
        return ""
    return str(getattr(goal, "status", "") or "").strip().lower()


def _background_task_link_status(
    agent: object,
    request: BackgroundRunRequest,
    *,
    store: ConversationStore | None = None,
) -> str:
    """Read the durable root-task state without inferring cancellation from model text."""
    task_id = str(request.task_id or "").strip()
    selected_store = store or getattr(agent, "conversation_store", None)
    if not task_id or selected_store is None:
        return ""
    try:
        links, load_errors = selected_store.task_links_report(request.thread_id)
    except Exception:
        return ""
    if load_errors:
        return ""
    for link in links:
        if str(getattr(link, "task_id", "") or "").strip() == task_id:
            return str(getattr(link, "status", "") or "").strip().lower()
    return ""


def _internal_subagent_continuation(request: BackgroundRunRequest) -> bool:
    wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    return str(wake.get("registered_by_tool") or "").strip() in {
        "goal_progress_continuation",
        # 历史数据兼容: wait 工具下架前登记的进度监督 policy 仍按内部续跑处理。
        "wait",
    }


def _goal_subagent_phase(agent: object, root_task_id: str) -> tuple[str, str]:
    """Return a structured child phase for one exact goal task."""
    related, state_error = _related_subagent_runs(agent, root_task_id)
    if state_error == "subagent_root_not_found":
        return "no_subagents", ""
    if state_error:
        return "subagent_state_unknown", state_error
    from ..subagents.models import SUBAGENT_ENDED_STATUSES, task_status_in

    active_count = sum(
        1
        for task in related
        if not task_status_in(getattr(task, "status", ""), SUBAGENT_ENDED_STATUSES)
    )
    if active_count:
        return "subagents_active", ""
    return "subagents_terminal", ""


def _wake_signal_is_stale(
    agent: object,
    store: ConversationStore,
    signal: WakeSignal,
    lifecycle_reason: str,
) -> bool:
    """Drop only wakes whose exact durable task/goal is already terminal."""
    reason = str(lifecycle_reason or "").strip().lower()
    task_id = str(getattr(signal, "root_task_id", "") or "").strip()
    if reason == "thread_goal_continue":
        metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
        try:
            goal = store.load_goal(
                signal.thread_id,
                goal_id=str(metadata.get("goal_id") or "").strip(),
            )
        except Exception:
            return False
        return (
            goal is None
            or str(getattr(goal, "goal_id", "") or "").strip()
            != str(metadata.get("goal_id") or "").strip()
            or str(getattr(goal, "task_id", "") or "").strip() != task_id
            or str(getattr(goal, "status", "") or "").strip().lower() != "active"
        )
    if not task_id:
        return False
    if reason in _SCHEDULED_WAKE_REASONS or reason == "task_ledger_resume":
        return _signal_task_link_is_terminal(agent, store, signal, reason)
    if reason not in SUBAGENT_LIFECYCLE_WAKE_REASONS:
        return False
    # LLM: 真机复现(2026-09-11, 受控场景)修复"父级已空闲、孩子等裁决"的双向等待：
    # 能力申请类唤醒进到这里时，父代理**自己那一轮已经 done**，于是
    # _signal_task_link_is_terminal 判"父 task terminal"→把唤醒当过期丢弃
    # （实测 created_at 与 handled_at 只差 0.8 秒、父代理零新回合，子代理永久 BLOCKED）。
    # 但"父轮次结束"恰恰是子代理在等它的前提：只有父代理真的再跑一轮才能裁决。
    # 因此只要源子代理仍有 OPEN 能力申请/gap，这类唤醒就不得按过期丢弃。
    # 判据只看结构化字段；不解析子代理正文，也不猜测阻塞原因。
    if reason in {
        "subagent_capability_request_open",
        "subagent_runner_finished",
    } and _source_child_awaits_parent_decision(agent, signal):
        return False
    return _signal_task_link_is_terminal(agent, store, signal, reason)


# LLM: 从唤醒信号的源子代理读结构化申请状态；读不到一律返回 False（保持原过期判定，不放宽唤醒）。
# 函数用途: 判断该唤醒的源子代理是否仍在等待父级裁决。
def _source_child_awaits_parent_decision(agent: object, signal: object) -> bool:
    manager = getattr(agent, "subagents", None)
    run_id = str(getattr(signal, "source_agent_id", "") or "").strip()
    if manager is None or not run_id:
        return False
    try:
        task = manager.load(run_id)
    except Exception:
        return False
    if task is None:
        return False
    from ..subagents.model_capabilities import capability_request_requires_parent_resolution

    for request in getattr(task, "capability_requests", []) or []:
        if capability_request_requires_parent_resolution(getattr(request, "status", "OPEN")):
            return True
    # 真机实测(2026-09-11)：route 完成后 request 会进入终态 GAP（"无匹配能力"），
    # 真正等待父级的是它留下的 OPEN capability_gap（需要 triage）。只查 request 会漏掉这一整类。
    return any(
        str(getattr(gap, "status", "")).strip().upper() == "OPEN"
        for gap in getattr(task, "capability_gaps", []) or []
    )


def _signal_task_link_is_terminal(
    agent: object,
    store: ConversationStore,
    signal: WakeSignal,
    reason: str,
) -> bool:
    task_id = str(getattr(signal, "root_task_id", "") or "").strip()
    if not task_id:
        return False
    status = _background_task_link_status(
        agent,
        BackgroundRunRequest(
            thread_id=signal.thread_id,
            task_id=task_id,
            reason=reason,
        ),
        store=store,
    )
    return status.upper() in _TASK_LINK_TERMINAL_STATUSES


def _goal_wake_waits_for_child_event(
    agent: object,
    store: ConversationStore,
    signal: WakeSignal,
    lifecycle_reason: str,
) -> bool:
    """A plain goal continuation does not poll while exact related children are active."""
    if str(lifecycle_reason or "").strip().lower() != "thread_goal_continue":
        return False
    metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
    if str(metadata.get("guidance_id") or "").strip():
        return False
    task_id = str(getattr(signal, "root_task_id", "") or "").strip()
    try:
        if store.pending_guidance("task", task_id, limit=1):
            return False
    except Exception:
        return False
    # LLM: 互等死锁修复(2026-09-11 真机实测)。子代理"等父级裁决"时在状态上仍是 active，
    # 若这里一律跳过父级唤醒，就会出现"父级等孩子结束、孩子等父级授权"的双向等待：
    # 实测父代理整轮卡死 20 分钟以上。因此只要存在**需要父级处理的 OPEN 能力申请**，
    # 就必须放行唤醒——这正是父级唯一能推进的动作。
    # 函数用途: 有孩子等待父级裁决时不再抑制父级唤醒。
    if _related_children_need_parent_decision(agent, task_id):
        return False
    phase, state_error = _goal_subagent_phase(agent, task_id)
    return not state_error and phase == "subagents_active"


# LLM: 判据只看结构化事实——孩子的 capability_requests 里是否有需要父级裁决的 OPEN 项；
# 不解析子代理正文，也不猜测阻塞原因。
# 函数用途: 判断某个 goal 任务下是否有子代理正在等待父级裁决。
def _related_children_need_parent_decision(agent: object, root_task_id: str) -> bool:
    try:
        related, state_error = _related_subagent_runs(agent, root_task_id)
    except Exception:
        return False
    if state_error:
        return False
    from ..subagents.model_capabilities import capability_request_requires_parent_resolution

    for task in related:
        for request in getattr(task, "capability_requests", []) or []:
            if capability_request_requires_parent_resolution(getattr(request, "status", "OPEN")):
                return True
        if any(str(getattr(gap, "status", "")) == "OPEN" for gap in getattr(task, "capability_gaps", []) or []):
            return True
    return False


def _wake_signal_should_skip(
    agent: object,
    store: ConversationStore,
    signal: WakeSignal,
    lifecycle_reason: str,
) -> bool:
    return _wake_signal_is_stale(
        agent,
        store,
        signal,
        lifecycle_reason,
    ) or _goal_wake_waits_for_child_event(agent, store, signal, lifecycle_reason)


def _is_audit_source_worker_lifecycle_metadata(
    *,
    reason: str,
    root_task_id: str,
    metadata: object,
) -> bool:
    """Validate a machine-authored Audit source-worker lifecycle marker."""
    if str(reason or "").strip().lower() != "subagent_runner_finished":
        return False
    if not isinstance(metadata, dict) or metadata.get("audit_source_worker") is not True:
        return False
    audit_id = str(metadata.get("audit_id") or "").strip()
    source_id = str(metadata.get("source_id") or "").strip()
    phase = str(metadata.get("audit_source_worker_phase") or "").strip().lower()
    watch_id = str(metadata.get("watch_id") or "").strip()
    worker_key = str(metadata.get("worker_key") or "").strip()
    if not audit_id or audit_id != str(root_task_id or "").strip():
        return False
    if phase == "binding_pending":
        return bool(source_id) and not watch_id and not worker_key
    if phase not in {"", "bound"}:
        return False
    from ..common.audit_activation import audit_source_worker_key

    return bool(watch_id) and worker_key == audit_source_worker_key(
        audit_id,
        watch_id,
    )


def _is_audit_source_worker_quota_lifecycle_metadata(
    *,
    reason: str,
    root_task_id: str,
    metadata: object,
) -> bool:
    """Identify the one source-worker failure that needs owner notification."""
    if not _is_audit_source_worker_lifecycle_metadata(
        reason=reason,
        root_task_id=root_task_id,
        metadata=metadata,
    ):
        return False
    from ..subagents.models import FailureType

    return (
        str(metadata.get("failure_type") or "").strip()
        == FailureType.PROVIDER_QUOTA_EXHAUSTED.value
    )


def _is_internal_audit_source_worker_lifecycle_metadata(
    *,
    reason: str,
    root_task_id: str,
    metadata: object,
) -> bool:
    """Identify lifecycle facts already owned by the Audit supervisor."""
    if not _is_audit_source_worker_lifecycle_metadata(
        reason=reason,
        root_task_id=root_task_id,
        metadata=metadata,
    ) or not isinstance(metadata, dict):
        return False
    failure_type = str(metadata.get("failure_type") or "").strip()
    from ..subagents.models import FailureType

    # The durable Audit supervisor owns all source-worker lifecycle states.
    # Owner-visible findings and aggregate capacity changes use separate typed
    # events, so replaying a child BLOCKED/FAILED/DONE row through the model can
    # only create stale progress chatter. Account quota remains owner-facing
    # because it requires an explicit operator action.
    return failure_type != FailureType.PROVIDER_QUOTA_EXHAUSTED.value


def _wake_payload_is_audit_provider_quota(wake: object) -> bool:
    if not isinstance(wake, dict):
        return False
    metadata = wake.get("metadata")
    return _is_audit_source_worker_quota_lifecycle_metadata(
        reason=str(wake.get("reason") or "subagent_runner_finished"),
        root_task_id=str(wake.get("root_task_id") or ""),
        metadata=metadata,
    )


def _legacy_audit_source_worker_lifecycle_task_matches(
    agent: object,
    signal: WakeSignal,
) -> bool:
    """Validate a pre-phase-marker wake against its durable source task.

    Candidate upgrades can leave already-persisted binding-pending wakes whose
    metadata predates ``audit_source_worker_phase``.  Do not trust the partial
    wake marker alone and do not send it to the owner model: resolve the exact
    child task id and validate its typed Audit attributes instead.
    """

    metadata = getattr(signal, "metadata", None)
    if not isinstance(metadata, dict) or metadata.get("audit_source_worker") is not True:
        return False
    if str(metadata.get("audit_source_worker_phase") or "").strip():
        return False
    reason = str(getattr(signal, "reason", "") or "").strip().lower()
    root_task_id = str(getattr(signal, "root_task_id", "") or "").strip()
    audit_id = str(metadata.get("audit_id") or "").strip()
    source_agent_id = str(getattr(signal, "source_agent_id", "") or "").strip()
    if (
        reason != "subagent_runner_finished"
        or not root_task_id
        or audit_id != root_task_id
        or not source_agent_id
    ):
        return False
    manager = getattr(agent, "subagents", None)
    if manager is None:
        return False
    try:
        task = manager.load(source_agent_id)
    except Exception:
        return False
    attrs = getattr(task, "attributes", None)
    from ..common.audit_activation import structured_audit_supervised_worker_attributes
    from .authority import CONVERSATION_REQUEST_ID_ATTR

    return bool(
        structured_audit_supervised_worker_attributes(attrs)
        and str(attrs.get(CONVERSATION_REQUEST_ID_ATTR) or "").strip() == root_task_id
    )


def _is_internal_audit_source_worker_lifecycle_signal(
    signal: WakeSignal,
    agent: object | None = None,
) -> bool:
    metadata = getattr(signal, "metadata", None)
    reason = str(getattr(signal, "reason", "") or "")
    root_task_id = str(getattr(signal, "root_task_id", "") or "")
    if _is_internal_audit_source_worker_lifecycle_metadata(
        reason=reason,
        root_task_id=root_task_id,
        metadata=metadata,
    ):
        return True
    if agent is None or not _legacy_audit_source_worker_lifecycle_task_matches(
        agent,
        signal,
    ):
        return False
    from ..subagents.models import FailureType

    return (
        str(metadata.get("failure_type") or "").strip()
        != FailureType.PROVIDER_QUOTA_EXHAUSTED.value
    )


def _is_audit_source_worker_lifecycle_signal(signal: WakeSignal) -> bool:
    return _is_audit_source_worker_lifecycle_metadata(
        reason=str(getattr(signal, "reason", "") or ""),
        root_task_id=str(getattr(signal, "root_task_id", "") or ""),
        metadata=getattr(signal, "metadata", None),
    )


def _is_legacy_per_source_audit_capacity_signal(signal: WakeSignal) -> bool:
    """Retire pre-aggregate capacity wakes already persisted by the candidate."""
    if str(getattr(signal, "reason", "") or "").strip().lower() != "audit_capacity_alert":
        return False
    metadata = getattr(signal, "metadata", None)
    return (
        isinstance(metadata, dict)
        and str(metadata.get("schema_version") or "") == "audit-capacity-event.v1"
        and bool(str(metadata.get("watch_id") or "").strip())
    )


# LLM: Runtime lifecycle checks prefer the indexed root lookup that reloads exact canonical runs.
# Legacy/fake managers without that adapter retain one full-scan compatibility path; production
# scheduling must never deepcopy unrelated historical trees on each readiness tick.
# 函数用途: 读取一个根任务的真实子代理状态，供完成合批、投递和续跑判断复用。
def _related_subagent_runs(agent: object, root_task_id: str) -> tuple[list[object], str]:
    if not root_task_id:
        return [], "subagent_root_unknown"
    manager = getattr(agent, "subagents", None)
    if manager is None:
        return [], "subagent_state_unavailable"
    try:
        root_reader = getattr(manager, "list_runs_for_root_report", None)
        if callable(root_reader):
            report = root_reader(root_task_id)
            if list(getattr(report, "load_errors", []) or []):
                return [], "subagent_state_load_error"
            tasks = list(getattr(report, "runs", []) or [])
        elif callable(getattr(manager, "list_runs_report", None)):
            report = manager.list_runs_report()
            if list(getattr(report, "load_errors", []) or []):
                return [], "subagent_state_load_error"
            tasks = list(getattr(report, "runs", []) or [])
        else:
            tasks = list(manager.list_runs())
    except Exception:
        return [], "subagent_state_load_error"
    related = [
        task
        for task in tasks
        if str(getattr(task, "id", "") or "") == root_task_id
        or str(getattr(task, "root_id", "") or "") == root_task_id
    ]
    return (related, "") if related else ([], "subagent_root_not_found")


# 后台主代理产出的投递路由。只有"内部/无真实外部路由"(子代理事件叫回、定时巡检默认走 internal)才
# 尝试升级成主动外呼:若该会话绑过可主动外呼的通道(飞书)就投到那个通道,这样"叫回来产出的汇总"才发
# 得到用户所在真渠道、不进内部黑洞。显式外部路由(feishu/wechat/qq/chat…进度策略或入站消息带来的)一律
# 原样尊重,不改既有语义;没有可外呼绑定则保持原路由(internal → 单机/CLI 行为不变)。
_INTERNAL_ROUTE_CHANNELS = frozenset({"internal", ""})


# LLM: internal 路由只能按 delivery registry 的 proactive capability 升级；显式外部路由保持原样。
# 函数用途: 为后台回复选择结构化通道和目标。
def _resolve_delivery_route(
    thread: object,
    request: BackgroundRunRequest,
    *,
    supports_proactive: Callable[[str], bool] | None = None,
) -> tuple[str, str]:
    channel = str(getattr(request, "route_channel", "") or "")
    route_target = str(getattr(request, "route_target", "") or "")
    if channel in _INTERNAL_ROUTE_CHANNELS:
        binding = _latest_proactive_binding(thread, supports_proactive=supports_proactive)
        if binding is not None:
            # 飞书 send_message 用 receive_id_type=open_id,需要用户 open_id(=binding.channel_user_id);
            # 缺失才回落 channel_conversation_id。
            return binding.channel, (binding.channel_user_id or binding.channel_conversation_id)
    return channel, route_target or default_route_target(thread, channel)


# LLM: 候选绑定按结构化 capability 和目标存在性过滤，再以 last_active_at 选最近通道。
# 函数用途: 返回会话中最近可主动外呼的通道绑定。
def _latest_proactive_binding(
    thread: object,
    *,
    supports_proactive: Callable[[str], bool] | None = None,
):
    capability_check = supports_proactive or _supports_builtin_proactive
    candidates = [
        binding
        for binding in getattr(thread, "channel_bindings", ()) or ()
        if capability_check(str(getattr(binding, "channel", "") or ""))
        and (
            getattr(binding, "channel_user_id", "")
            or getattr(binding, "channel_conversation_id", "")
        )
    ]
    if not candidates:
        return None
    return max(
        candidates, key=lambda binding: float(getattr(binding, "last_active_at", 0.0) or 0.0)
    )


# LLM: 只在没有注入真实 DeliveryService 时使用内置默认，生产运行优先读取 registry 能力。
# 函数用途: 判断内置默认通道是否支持主动外呼。
def _supports_builtin_proactive(channel: str) -> bool:
    return str(channel or "").strip().lower() in PROACTIVE_PUSH_CHANNELS


def _run_request(kwargs: dict[str, Any]) -> BackgroundRunRequest:
    current = now(kwargs.get("now"))
    return BackgroundRunRequest(
        thread_id=str(kwargs.get("thread_id") or ""),
        task_id=str(kwargs.get("task_id") or ""),
        reason=str(kwargs.get("reason") or "scheduled_progress_report"),
        route_channel=str(kwargs.get("route_channel") or "internal"),
        route_target=str(kwargs.get("route_target") or ""),
        now=current,
        wake_signal=wake_signal_payload(kwargs.get("wake_signal")),
    )


# LLM: 后台轮的 run_id 必须优先绑定明确 task_id；线程级兜底身份只能用于无任务事件，
# 否则同一 thread 的后续任务会误复用上一任务的 AgentRun 权威链。
# 函数用途: 把一次后台唤醒转换成主代理运行参数，并保留任务、工具和投递边界。
# LLM: 会话历史的"范围裁决"与"展示摘要"必须分开：
# - 权威历史 = store 里全部未压缩 canonical 行（长会话不能被展示索引截短）；
# - 范围裁决只对 **显式 detached named task** 生效，规则完全复用既有创建锚点 + 精确 lineage；
# - context_bundle 的 recent_limit 只是有界 operational 展示，绝不作为"哪些历史有资格进模型"的白名单。
# 关键：裁决输入必须来自**已经读成功的同一份 scope 事实**（scoped["tasks"]），不得再单独读一次
# task link——那次读失败会被当成"没有 detached task"而放行全部历史（fail-open），把读取失败洗成普通会话。
# 普通连续会话（无 task / 非 detached）完整继承未压缩历史。
# 函数用途: 用已加载的 scope 事实过滤权威未压缩行，返回可进入模型的历史行。
def _history_scope_rows(
    decision: _TaskScopeDecision,
    rows: list[MessageLogEntry],
) -> list[MessageLogEntry]:
    if not rows:
        return []
    # 只有"本轮 exact task 确实是 detached named"才应用锚点/lineage；
    # 普通轮、无 task 轮（含 bundle 里恰好存在旧 Goal）必须完整继承历史。
    if not decision.detached or not decision.exact_link:
        return list(rows)
    # 同一份算法：把权威行投影成 dict 交给既有 detached 行选择器，再按 message_id 映射回对象，
    # 避免 operational 摘要与 native seed 各维护一份锚点/lineage 规则而长期漂移。
    by_id = {str(getattr(row, "message_id", "") or ""): row for row in rows}
    payload = [row.to_dict() for row in rows]
    selected_ids = {
        str(row.get("message_id") or "")
        for row in _detached_task_rows(payload, dict(decision.exact_link), set(decision.task_ids))
    }
    return [row for message_id, row in by_id.items() if message_id in selected_ids]


# LLM: 解析种子并把"历史读不到"升级成 typed 错误：读不到时不许退回有界摘要继续跑模型，
# 那会把"读取失败"伪装成"上下文骤降"，让模型在缺历史时作答。抛错 → 本片失败、唤醒不确认、可重试。
# 函数用途: 取得本片可用的历史种子，或抛出 BackgroundHistoryUnavailableError。
def _background_history_seed_or_raise(
    agent: object,
    store: ConversationStore | None,
    thread: ConversationThread | None,
    request: BackgroundRunRequest,
    *,
    proactive_delivery_available: bool | None = None,
) -> object | None:
    result = _background_conversation_history_seed(
        agent,
        store,
        thread,
        request,
        _tool_policy_request(
            agent,
            request,
            proactive_delivery_available=proactive_delivery_available,
        ),
    )
    if result.status == "unreadable":
        raise BackgroundHistoryUnavailableError(
            "background conversation history is unreadable",
            load_errors=list(result.load_errors),
            detail=result.detail,
        )
    return result.seed


# LLM: 后台历史不可读是可恢复的运行事实，不是"空历史"。抛这个 typed 错误让上层：
# ① 不确认唤醒（保留 pending，可重试）；② 把 load_errors 写进失败诊断；③ 绝不带缺失历史继续调用模型。
# 类用途: 表示后台工作片所需的会话历史读取/解析失败。
class BackgroundHistoryUnavailableError(RuntimeError):
    # LLM: 错误码与结构化 load_errors 必须可被上层读取，禁止只留一句自然语言。
    # 函数用途: 构造一个带 load_errors 与细节的后台历史不可用错误。
    def __init__(
        self,
        message: str,
        *,
        load_errors: list[dict[str, Any]] | None = None,
        detail: str = "",
    ) -> None:
        super().__init__(message)
        self.error_code = "BACKGROUND_HISTORY_UNAVAILABLE"
        self.load_errors = list(load_errors or [])
        self.detail = str(detail or "")


# LLM: 后台历史种子的结果必须区分三态，不能把"读不到"和"确实没有"混成同一个 None：
# - ready: 拿到权威行并投影成功（可能为空历史，那是合法空）；
# - unreadable: 历史读取/解析失败（load_errors 非空或抛错）→ 调用方必须按 typed 错误处理，
#   保留可恢复状态（唤醒不确认、可重试），**不得**退回有界摘要继续跑模型——那会把"历史读取失败"
#   伪装成"上下文骤降"；
# - disabled: 窄范围审计事件等按设计不使用会话历史。
# 调用方（_run_background_main_turn_with_compact）读 status 决定是否继续，load_errors 一路带出去。
# 类用途: 承载后台历史种子的三态结果与结构化读取错误。
@dataclass(frozen=True)
class _BackgroundHistorySeedResult:
    status: str
    seed: object | None = None
    load_errors: tuple[dict[str, Any], ...] = ()
    detail: str = ""


# LLM: 后台工作片必须与前台共用同一份 canonical 历史投影与同一个 Compact 权威：
# Gateway 走 conversation_history_seed（任务范围行 + provider 消息投影），后台之前不带 seed，
# _native_provider_history_messages 直接得到空历史，只剩一份有界摘要副本——那不是"续接同一会话"，
# 而是换了套丢工具细节的摘要。这里复用同一实现，同时：
#   ① 行选择必须复用既有结构化任务范围（detached named task 的创建锚点 + 精确 lineage），
#      不能直接吞全 thread 未压缩行（会把后来别的任务的消息带进 detached 工作）；
#   ② 读取失败给 typed 结果，绝不静默退回摘要；
#   ③ 只读：不在后台切片里另起一次压缩（压缩由本片 context_overflow 路径与前台 Compact 负责）。
# 函数用途: 为后台工作片构造与前台同源的会话历史种子（三态结果）。
def _background_conversation_history_seed(
    agent: object,
    store: ConversationStore | None,
    thread: ConversationThread | None,
    request: BackgroundRunRequest,
    policy_request: BackgroundToolPolicyRequest | None = None,
) -> _BackgroundHistorySeedResult:
    if store is None or thread is None:
        return _BackgroundHistorySeedResult("unreadable", detail="conversation store unavailable")
    thread_id = str(getattr(thread, "thread_id", "") or "").strip()
    if not thread_id:
        return _BackgroundHistorySeedResult("unreadable", detail="thread identity unavailable")
    # 窄范围审计事件（finding/capacity）是"一条结构化事件"，不是会话续接：
    # 它们必须只看到事件事实与审计目标，不能把 owner 的旧聊天历史带进模型输入。
    if _narrow_audit_event_reason(getattr(request, "reason", "")):
        return _BackgroundHistorySeedResult("disabled", detail="narrow audit event")
    load_errors: list[dict[str, Any]] = []
    scope_state = _BackgroundContextLoad(
        agent,
        store,
        thread,
        str(getattr(request, "task_id", "") or "").strip(),
        getattr(agent, "config", None),
        policy_request,
        load_errors,
    )
    try:
        scoped = _context_bundle(scope_state)
    except Exception as exc:
        load_errors.append(runtime_error_report(exc, context="background_history.scope"))
        return _BackgroundHistorySeedResult(
            "unreadable",
            load_errors=tuple(load_errors),
            detail=f"context bundle failed: {type(exc).__name__}",
        )
    if load_errors:
        # 读取/解析错误必须上报，不得被"空历史"掩盖。
        return _BackgroundHistorySeedResult(
            "unreadable",
            load_errors=tuple(load_errors),
            detail="conversation context reported load errors",
        )
    try:
        from ..agent_core.runtime.context_compactor import runtime_compact_policy
        from ..gateway_parts.request_execution import _gateway_conversation_history_rows
        from .compact import _uncompacted_conversation_rows
        from .models import ConversationHistorySeed
        from .native_history import provider_history_messages_from_rows

        rows = _uncompacted_conversation_rows(store, thread)
        # 权威历史 = 全部未压缩行；范围裁决用与 operational 摘要**同一份** decision
        # （来自本轮显式 task 身份 + 已加载 bundle），既不重读盘、也不按第几个 task 猜身份。
        scoped_rows = _history_scope_rows(
            _task_scope_decision(scope_state, scoped),
            rows,
        )
        budget = int(getattr(runtime_compact_policy(agent), "trigger_tokens", 0) or 0)
        # 只用历史投影两步（行选择 + provider 消息），不牵入 recent_artifacts 等与续接无关的投影。
        selected_rows = _gateway_conversation_history_rows(
            agent,
            thread_id,
            "",
            load_errors,
            rows=tuple(scoped_rows),
            token_budget=budget,
        )
        history = tuple((row.role, row.content) for row in selected_rows)
        canonical_history = provider_history_messages_from_rows(selected_rows)
    except InterruptedError:
        raise
    except Exception as exc:
        load_errors.append(runtime_error_report(exc, context="background_history.projection"))
        return _BackgroundHistorySeedResult(
            "unreadable",
            load_errors=tuple(load_errors),
            detail=f"history projection failed: {type(exc).__name__}",
        )
    if load_errors:
        return _BackgroundHistorySeedResult(
            "unreadable",
            load_errors=tuple(load_errors),
            detail="history projection reported load errors",
        )
    scoped_thread = scoped.get("thread") if isinstance(scoped.get("thread"), dict) else {}
    return _BackgroundHistorySeedResult(
        "ready",
        seed=ConversationHistorySeed(
            # detached named task 的 summary/代次按既有投影口径（创建锚点之前的摘要才继承）。
            compact_summary=str(scoped_thread.get("summary") or ""),
            compact_generation=max(0, int(scoped_thread.get("compact_generation", 0) or 0)),
            messages=tuple(history),
            canonical_messages=tuple(canonical_history),
        ),
    )


# LLM: 后台执行使用精确持久任务作为输入回合编号；定时任务保留自己的 run ID，attempt 仍逐次独立。
# 函数用途: 构造后台模型执行参数，让普通插话、消费回执、归档和续跑共享同一任务身份。
def _run_params(
    thread_id: str,
    request: BackgroundRunRequest,
    agent: object | None = None,
    *,
    goal_context: GoalRuntimeContext | None = None,
    proactive_delivery_available: bool | None = None,
    thread: ConversationThread | None = None,
    history_seed: object | None = None,
) -> RunParams:
    config = getattr(agent, "config", None)
    conversation_store = getattr(agent, "conversation_store", None)
    resolved_goal_context = goal_context or (
        _goal_runtime_context(agent, conversation_store, request)
        if agent is not None and conversation_store is not None
        else GoalRuntimeContext()
    )
    scheduler_run_id = _scheduler_run_id(request)
    task_attributes = _background_task_attributes(
        thread_id,
        request,
        agent,
        sampled_subagent_phase=resolved_goal_context.subagent_phase,
        thread=thread,
    )
    if resolved_goal_context.goal is not None and isinstance(task_attributes, dict):
        task_attributes["thread_goal_id"] = resolved_goal_context.goal.goal_id
    carried_tool_calls = _background_active_turn_tool_calls(
        request,
        resolved_goal_context,
    )
    _extend_background_slice_tool_budget(task_attributes, len(carried_tool_calls))
    return RunParams(
        save=False,
        source="background_main_agent",
        request_id=scheduler_run_id or request.task_id,
        run_id=scheduler_run_id or request.task_id or f"bg-main-{thread_id}",
        # A conversation thread identifies where this turn runs; it is not a
        # durable task.  Internal events without an exact task must stay
        # taskless, otherwise a tool such as ``wait`` can bind the thread id as
        # a new task and recursively create a progress-watch task.  Scheduled
        # jobs already carry their own structured run identity.
        task_id=request.task_id or scheduler_run_id,
        # /audit 保证档跨后台轮延续:后台唤醒轮的 prompt 是机器拼的、不带 /audit 词元,若主代理
        # 在后台轮里新派判读子代理,继承需从结构化标志读——从 owner 已有的保证档 watch(持久棘轮)
        # 反推本任务树在保证档,盖回 task_attributes,让新派子代理照样继承(治残留边界:委派发生在
        # 后台轮时词元/前台 task_attributes 都不在)。owner 无保证档 watch 则不动(默认档不误开)。
        # 后台轮与前台轮必须携带同一份结构化会话任务引用。否则 FinalizationService
        # 不知道该关闭哪条 active task link，完成
        # 的任务会被定时 policy 反复叫醒。只在 request 有明确 task_id 时绑定，
        # 普通无任务后台消息不会被误升格成任务。
        task_attributes=task_attributes,
        allowed_tools=_background_run_allowed_tools(
            config,
            BackgroundToolPolicyRequest(
                reason=request.reason,
                wake_signal=request.wake_signal,
                config=config,
                owner_policy=getattr(agent, "owner_policy", None),
                policy_snapshot=_policy_snapshot_from_request(request),
                active_goal=resolved_goal_context.goal is not None,
                goal_subagent_phase=resolved_goal_context.subagent_phase,
                proactive_delivery_available=proactive_delivery_available,
            ),
        ),
        root_user_prompt=(
            resolved_goal_context.task_objective
            if _is_active_turn_lifecycle_continuation(
                request,
                resolved_goal_context.task_objective,
            )
            else ""
        ),
        carried_archive_tool_calls=carried_tool_calls,
        # 与前台同源的会话历史：后台切片不再只带一份有界摘要指针。
        conversation_history_seed=history_seed,
    )


# LLM: Only child lifecycle events continue the originating active turn. Restore rows by the
# child envelope's conversation request id; the durable task id is only a legacy fallback.
# 函数用途: 为子代理完成后的主代理工作片恢复原用户回合的工具调用、去重键和执行轨迹。
def _background_active_turn_tool_calls(
    request: BackgroundRunRequest,
    context: GoalRuntimeContext,
) -> list[dict[str, object]]:
    if not _is_active_turn_lifecycle_continuation(request, context.task_objective):
        return []
    task_id = str(request.task_id or "").strip()
    task_path = str(context.task_path or "").strip()
    if not task_id or not task_path:
        return []
    try:
        from ..memory_archive.compact_tool_output_refs import carried_tool_call_records

        active_turn_request_ids = _background_active_turn_request_ids(request)
        scope: dict[str, object] = (
            {"conversation_request_id": active_turn_request_ids}
            if active_turn_request_ids
            else {"run_id": task_id, "task_id": task_id}
        )
        return carried_tool_call_records(
            Path(task_path).expanduser().resolve(strict=False) / "work",
            scope,
        )
    except (OSError, RuntimeError, ValueError):
        return []


# LLM: A durable task id and an originating conversation turn id are distinct. Child wake
# metadata owns the latter; batched wakes may name several exact turns and must never infer
# one from the task id or completion prose.
# 函数用途: 从子代理完成信封中读取本轮工具历史所属的普通用户请求编号，供后台续接精确恢复。
def _background_active_turn_request_ids(
    request: BackgroundRunRequest,
) -> tuple[str, ...]:
    wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    metadata = wake.get("metadata") if isinstance(wake.get("metadata"), dict) else {}
    envelopes: list[dict[str, object]] = [metadata]
    events = metadata.get("events")
    for event in events if isinstance(events, list) else ():
        if not isinstance(event, dict):
            continue
        event_metadata = event.get("metadata")
        if isinstance(event_metadata, dict):
            envelopes.append(event_metadata)
    values = [
        str(envelope.get(CONVERSATION_REQUEST_ID_ATTR) or "").strip() for envelope in envelopes
    ]
    return tuple(dict.fromkeys(value for value in values if value))


# LLM: One background turn may sample a coalesced mailbox slice. These ids are
# delivery/finalization authority and must come only from the typed wake envelope;
# the primary id is retained even if older records lack the explicit batch list.
# 函数用途: 读取当前后台轮已经纳入提示的精确唤醒编号，供事件去重和任务收口使用。
def _background_wake_signal_ids(wake: object) -> tuple[str, ...]:
    row = wake if isinstance(wake, dict) else {}
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    raw_batch = metadata.get("batched_wake_signal_ids")
    values = raw_batch if isinstance(raw_batch, list) else []
    primary = str(row.get("wake_signal_id") or "").strip()
    selected = [str(item).strip() for item in values if str(item).strip()]
    if primary and primary not in selected:
        selected.insert(0, primary)
    return tuple(dict.fromkeys(selected))


# LLM: Capture every same-root lifecycle envelope that predates this sampling
# boundary. Items omitted from the active batch remain durable for a later full
# turn and must not be consumed by the slim mid-turn event injection path.
# 函数用途: 冻结后台轮开始时已经排队的同任务生命周期信封编号。
def _background_lifecycle_wake_snapshot_ids(
    agent: object | None,
    task_id: str,
) -> tuple[str, ...]:
    store = getattr(agent, "conversation_store", None)
    selected = str(task_id or "").strip()
    if store is None or not selected:
        return ()
    try:
        loader = getattr(store, "pending_wake_signals_report", None)
        if callable(loader):
            signals, load_errors = loader(limit=0)
            if load_errors:
                return ()
        else:
            signals = store.pending_wake_signals(limit=0)
    except Exception:
        return ()
    return tuple(
        str(signal.wake_signal_id)
        for signal in signals
        if str(getattr(signal, "root_task_id", "") or "").strip() == selected
        and str(getattr(signal, "reason", "") or "").strip().lower()
        in SUBAGENT_LIFECYCLE_WAKE_REASONS
        and str(getattr(signal, "wake_signal_id", "") or "").strip()
    )


# LLM: Background wake identity has one authoritative projection into per-turn
# attributes. Keep generic scheduler ids singular; lifecycle mail additionally
# carries the active batch and pre-sampling queue fence used by closeout/safe points.
# 函数用途: 把当前后台唤醒及其生命周期邮箱快照写入运行参数。
def _apply_background_wake_attributes(
    attributes: dict[str, object],
    request: BackgroundRunRequest,
    agent: object | None,
) -> None:
    wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    task_id = str(request.task_id or "").strip()
    lifecycle_reason = str(request.reason or "").strip().lower()
    wake_signal_id = str(wake.get("wake_signal_id") or "").strip()
    if not wake_signal_id:
        return
    attributes["background_wake_signal_id"] = wake_signal_id
    if not task_id or lifecycle_reason not in SUBAGENT_LIFECYCLE_WAKE_REASONS:
        return
    attributes[CONVERSATION_BACKGROUND_WAKE_SIGNAL_IDS_ATTR] = list(
        _background_wake_signal_ids(wake)
    )
    attributes[CONVERSATION_BACKGROUND_WAKE_SNAPSHOT_IDS_ATTR] = list(
        _background_lifecycle_wake_snapshot_ids(agent, task_id)
    )


# LLM: background_max_tool_rounds is a per-slice allowance. Carried calls count as the
# reconstructed baseline, so extend the absolute limit by the same count to preserve the
# configured number of fresh rounds without resetting same-turn history.
# 函数用途: 恢复旧工具历史后，把后台工作片的新增轮数额度保持为原配置值。
def _extend_background_slice_tool_budget(
    attributes: dict[str, object] | None,
    carried_count: int,
) -> None:
    if not isinstance(attributes, dict) or carried_count <= 0:
        return
    try:
        current = int(attributes.get("max_tool_rounds") or 0)
    except (TypeError, ValueError):
        return
    if current > 0:
        attributes["max_tool_rounds"] = current + carried_count


def _scheduler_run_id(request: BackgroundRunRequest) -> str:
    if str(request.reason or "").strip().lower() != "scheduled_job_due":
        return ""
    wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    metadata = wake.get("metadata") if isinstance(wake.get("metadata"), dict) else {}
    return str(metadata.get("scheduler_run_id") or "").strip()


def _background_run_allowed_tools(
    config: object | None,
    request: BackgroundToolPolicyRequest,
) -> list[str] | None:
    # A durable user schedule is a fresh turn of the same owner agent, not the
    # internal wait/progress lane.  通道运行时 follows the same full-agent-turn
    # model; registry owner policy still removes disabled tools fail-closed.
    if str(request.reason or "").strip().lower() == "scheduled_job_due":
        return None
    return background_allowed_tools(config, request=request)


# LLM: Every durable wake keeps the original task identity and typed mode facts;
# child lifecycle turns also freeze the pre-sampling tree phase so a stale
# partial-progress turn cannot close a tree that settled while sampling.
# 函数用途: 为后台续跑构造任务身份和子代理阶段快照，让工作区、事件与 Audit 账本延续同一任务。
def _background_task_attributes(
    thread_id: str,
    request: BackgroundRunRequest,
    agent: object | None,
    *,
    sampled_subagent_phase: str = "",
    thread: ConversationThread | None = None,
) -> dict[str, object] | None:
    attributes: dict[str, object] = {}
    wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    metadata = wake.get("metadata") if isinstance(wake.get("metadata"), dict) else {}
    scheduler_run_id = str(metadata.get("scheduler_run_id") or "").strip()
    task_id = str(request.task_id or "").strip()
    lifecycle_reason = str(request.reason or "").strip().lower()
    _apply_internal_background_tool_budget(attributes, request, agent)
    if _narrow_audit_event_reason(request.reason):
        attributes[CONVERSATION_BACKGROUND_EVENT_REASON_ATTR] = (
            str(request.reason or "").strip().lower()
        )
    if thread_id and (task_id or scheduler_run_id):
        attributes["conversation_thread_id"] = str(thread_id).strip()
    _apply_background_wake_attributes(attributes, request, agent)
    if _audit_finding_report_event(request):
        attributes["background_delivery_evidence_refs"] = list(
            _background_delivery_evidence_refs(request)
        )
    if task_id:
        # Background slices commit their model-authored commentary/final through
        # _commit_background_response instead of Agent.run(save=True).  They still
        # own the exact ConversationThread and must advance its canonical Compact
        # generation while working; save=False only avoids a duplicate reply write.
        attributes[CONVERSATION_TRANSCRIPT_AUTHORITATIVE_ATTR] = True
        active_turn_request_ids = _background_active_turn_request_ids(request)
        attributes.update(
            {
                "conversation_task_id": task_id,
                # A durable background turn is another execution of this exact
                # task, not a new lineage. Descendants and audit ledgers must
                # keep the original task id across wakeups.
                CONVERSATION_REQUEST_ID_ATTR: (
                    active_turn_request_ids[0] if active_turn_request_ids else task_id
                ),
                # The scheduler acquired the thread claim before constructing
                # these params, so this background turn is the current task's
                # live executor rather than a competing executor.  Keep that
                # ownership as typed, per-turn state; the normal execution
                # blocker still rejects every other turn that lacks this flag.
                CONVERSATION_TASK_TURN_ACTIVE_ATTR: True,
            }
        )
        if agent is not None and lifecycle_reason in SUBAGENT_LIFECYCLE_WAKE_REASONS:
            attributes[CONVERSATION_BACKGROUND_SUBAGENT_PHASE_ATTR] = (
                _sampled_or_current_subagent_phase(agent, task_id, sampled_subagent_phase)
            )
        _apply_background_task_link_attributes(
            attributes,
            agent=agent,
            thread_id=str(thread_id or "").strip(),
            task_id=task_id,
        )
        if str(request.reason or "").strip().lower() == "thread_goal_continue":
            goal_id = str(metadata.get("goal_id") or "").strip()
            if goal_id:
                attributes["thread_goal_id"] = goal_id
    if scheduler_run_id:
        attributes.update(
            {
                "scheduler_job_id": str(metadata.get("scheduler_job_id") or "").strip(),
                "scheduler_run_id": scheduler_run_id,
                "scheduler_trigger": str(metadata.get("scheduler_trigger") or "").strip(),
            }
        )
        skill_refs = metadata.get("scheduler_skill_refs")
        if isinstance(skill_refs, list) and skill_refs:
            attributes["skill_snapshot_refs"] = skill_refs
    _apply_background_thread_workspace_attributes(attributes, thread=thread, agent=agent)
    return attributes or None


# LLM: 后台与前台共享可信 thread cwd，未指定时使用 canonical owner home；run_workspace 只是归档身份。
# 显式外部 cwd 仍由既有 Tool Gateway owner/Full Access 门裁决，本入口不从 task path 扩展权限。
# 函数用途: 后台唤醒后保持用户实际工作位置，不再把内部运行记录目录当成相对路径起点。
def _apply_background_thread_workspace_attributes(
    attributes: dict[str, object],
    *,
    thread: ConversationThread | None,
    agent: object | None,
) -> None:
    if not str(attributes.get("conversation_thread_id") or "").strip():
        return
    cwd = str(getattr(thread, "cwd", "") or "").strip()
    if not cwd and agent is not None and getattr(agent, "root", None) is not None:
        from ..user_space.runtime_paths import runtime_owner_root

        cwd = str(runtime_owner_root(agent).expanduser().resolve(strict=False))
    if not cwd:
        return
    roots: list[str] = []
    for value in (cwd, *(getattr(thread, "runtime_workspace_roots", ()) or ())):
        text = str(value or "").strip()
        if text and text not in roots:
            roots.append(text)
    attributes[CONVERSATION_EXECUTION_CWD_ATTR] = cwd
    attributes[CONVERSATION_RUNTIME_WORKSPACE_ROOTS_ATTR] = roots


# LLM: sampled phase 存在时必须原样复用；只有没有上层快照的直接调用才允许读取当前树。
# 函数用途: 为后台运行参数选择本轮冻结的子代理阶段，并兼容无快照调用方。
def _sampled_or_current_subagent_phase(
    agent: object,
    task_id: str,
    sampled_subagent_phase: str,
) -> str:
    phase = str(sampled_subagent_phase or "").strip()
    if phase:
        return phase
    phase, _state_error = _goal_subagent_phase(agent, task_id)
    return phase


# LLM: exact task link 只恢复运行归档、标题和结构化工作属性，不能授予权限或选择用户文件的执行 cwd。
# 函数用途: 让后台唤醒继续使用原任务记录，用户工作位置由独立的 thread/home 入口处理。
def _apply_background_task_link_attributes(
    attributes: dict[str, object],
    *,
    agent: object | None,
    thread_id: str,
    task_id: str,
) -> None:
    link = _background_conversation_task_link(agent, thread_id, task_id)
    if link is None:
        return
    goal = str(getattr(link, "goal", "") or "").strip()
    work_name = str(getattr(link, "work_name", "") or "").strip()
    if work_name:
        attributes["task_title"] = work_name
    elif goal:
        attributes["task_title"] = goal
    task_path = str(getattr(link, "task_path", "") or "").strip()
    if task_path:
        root = Path(task_path).expanduser().resolve(strict=False)
        if root.exists():
            # 这些路径只供内部归档和运行恢复使用，不作为用户工具的 cwd 或权限根。
            attributes["run_workspace"] = {
                "task_root": str(root),
                "output_dir": str(root / "output"),
                "work_dir": str(root / "work"),
            }
    for field, attr_name in (
        ("work_kind", "conversation_work_kind"),
        ("work_name", "conversation_work_name"),
        ("duration_seconds", "conversation_work_duration_seconds"),
        ("cancellation_scope", "conversation_cancellation_scope"),
    ):
        value = getattr(link, field, None)
        if value not in {None, ""}:
            attributes[attr_name] = value
    if str(getattr(link, "work_kind", "") or "") == "audit":
        _apply_background_audit_link_attributes(attributes, link, goal=goal)


def _apply_background_audit_link_attributes(
    attributes: dict[str, object],
    link: object,
    *,
    goal: str,
) -> None:
    from ..common.audit_activation import (
        AUDIT_ATTR,
        AUDIT_DEADLINE_ATTR,
        AUDIT_OBJECTIVE_ATTR,
        AUDIT_RUN_EPOCH_ATTR,
        AUDIT_SOURCE_BINDINGS_ATTR,
        AUDIT_WINDOW_ATTR,
    )

    attributes[AUDIT_ATTR] = True
    if goal:
        attributes[AUDIT_OBJECTIVE_ATTR] = goal
    duration = getattr(link, "duration_seconds", None)
    if duration is not None:
        attributes[AUDIT_WINDOW_ATTR] = int(duration)
    expires_at = getattr(link, "expires_at", None)
    if expires_at is not None and float(expires_at) > 0:
        attributes[AUDIT_DEADLINE_ATTR] = float(expires_at)
    attributes[AUDIT_RUN_EPOCH_ATTR] = max(
        0,
        int(getattr(link, "run_epoch", 0) or 0),
    )
    bindings = getattr(link, "effective_source_bindings", ()) or ()
    attributes[AUDIT_SOURCE_BINDINGS_ATTR] = [
        dict(item) for item in bindings if isinstance(item, dict)
    ]


def _apply_internal_background_tool_budget(
    attributes: dict[str, object],
    request: BackgroundRunRequest,
    agent: object | None,
) -> None:
    """Bound one internal wake slice with the existing typed tool-loop limits."""
    if agent is None:
        return
    reason = str(request.reason or "").strip().lower()
    if reason in {"incoming_channel_message", "scheduled_job_due"}:
        return
    policy = getattr(agent, "runtime_guard_policy", None)
    rounds = runtime_guard_int(
        "background_max_tool_rounds",
        32,
        policy=policy,
    )
    calls = runtime_guard_int(
        "background_max_tool_calls_per_round",
        4,
        policy=policy,
    )
    if rounds > 0:
        attributes["max_tool_rounds"] = rounds
    if calls > 0:
        attributes["max_tool_calls_per_round"] = calls


def _background_conversation_task_link(agent: object | None, thread_id: str, task_id: str):
    store = getattr(agent, "conversation_store", None) if agent is not None else None
    if store is None or not thread_id or not task_id:
        return None
    try:
        links, errors = store.task_links_report(thread_id)
    except Exception:
        return None
    if errors:
        return None
    return next(
        (item for item in links if str(getattr(item, "task_id", "") or "") == task_id), None
    )


def ledger_open_progress_item_count(agent: object | None, task_id: str) -> int:
    """Return unfinished work from the one durable task-progress ledger.

    ``items`` and ``coverage.targets`` are two projections of the same plan.
    Earlier code counted only coverage, so item-only plans silently lost their
    continuation wake after children reached terminal states.
    """
    if agent is None or not str(task_id or "").strip():
        return 0
    try:
        from ..agent_core.runtime.owner_roots import runtime_owner_root
        from ..task_progress import read_task_progress

        progress = read_task_progress(runtime_owner_root(agent), str(task_id).strip())
        item_counts = progress.get("counts") if isinstance(progress, dict) else None
        open_items = 0
        if isinstance(item_counts, dict):
            open_items = sum(
                max(0, int(item_counts.get(status) or 0))
                for status in ("pending", "in_progress", "blocked", "unknown")
            )
        coverage = progress.get("coverage") if isinstance(progress, dict) else None
        counts = coverage.get("counts") if isinstance(coverage, dict) else None
        open_targets = (
            max(0, int(counts.get("targets_incomplete") or 0)) if isinstance(counts, dict) else 0
        )
        return max(open_items, open_targets)
    except Exception:
        return 0


def _policy_snapshot_from_request(request: BackgroundRunRequest) -> dict[str, Any]:
    wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    snapshot = wake.get("policy_snapshot")
    return dict(snapshot) if isinstance(snapshot, dict) else {}


# Conversation runtime context
from dataclasses import dataclass

from ..agent_core.agent_tree.status import agent_tree_status_payload
from ..artifacts.registry import latest_artifact_records
from ..settings.defaults import default_config_value
from .context_budget import (
    BackgroundContextPayloadRequest,
    background_context_budget_from_config,
    bounded_background_context_payload,
)


@dataclass(frozen=True)
class _BackgroundContextLoad:
    agent: object
    store: ConversationStore
    thread: ConversationThread
    task_id: str
    config: object | None
    policy_request: BackgroundToolPolicyRequest
    load_errors: list[dict[str, Any]]


# LLM: 后台上下文会投给模型，active/pending wake 必须先做模型可见净化；
# 原始持久事件仍留在 ConversationStore，不能在这里改写权威账本。
# 函数用途: 组装一次后台唤醒轮的有界 Markdown 上下文。
def context_markdown(
    *,
    agent: object,
    store: ConversationStore,
    thread: ConversationThread,
    request,
    proactive_delivery_available: bool | None = None,
    include_recent_messages: bool = True,
) -> str:
    policy_request = _tool_policy_request(
        agent,
        request,
        proactive_delivery_available=proactive_delivery_available,
    )
    task_id = str(getattr(request, "task_id", "") or "").strip()
    active_wake_signal = _model_visible_wake_signal(getattr(request, "wake_signal", None))
    bounded = _bounded_context(
        agent,
        store,
        thread,
        task_id,
        policy_request,
        active_wake_signal=active_wake_signal,
    )
    policy_decision = background_tool_policy_decision(
        getattr(agent, "config", None), request=policy_request
    )
    sections = [
        ("Active Wake Signal", bounded.get("active_wake_signal") or {}),
        (
            "Subagent Completion Inputs",
            bounded.get("subagent_completions") or {},
        ),
        ("Conversation Thread", bounded.get("thread") or {}),
        ("Runtime Load Errors", bounded.get("load_errors") or []),
        ("Task Runtime State", bounded.get("task_runtime_state") or {}),
        ("Bound Tasks", bounded.get("tasks") or []),
        ("Channel Bindings", bounded.get("channel_bindings") or []),
        ("Recent Observations", bounded.get("observations") or []),
        ("Guidance", bounded.get("guidance") or []),
        ("Pending Wake Signals", bounded.get("pending_wake_signals") or []),
        ("Recovery Snapshot", bounded.get("recovery_snapshot") or {}),
        ("Agent Tree Snapshot", bounded.get("agent_tree") or {}),
        ("Background Context Projection", bounded.get("_projection") or {}),
        ("Control Action Policy", policy_decision.to_dict()),
    ]
    if include_recent_messages:
        # 只有"没有 canonical 历史种子"的历史遗留路径才需要这份有界摘要副本。
        sections.insert(5, ("Recent Messages", bounded.get("messages") or []))
    if _narrow_audit_event_reason(getattr(request, "reason", "")):
        # A finding or capacity wake is one exact owner-facing event, not a
        # general supervision tick. Giving the model sibling counts, old chat
        # turns or the full task ledger can override the current typed facts.
        # Project only the authoritative event plus the exact Audit objective;
        # durable ledgers remain available through tools.
        sections = [
            ("Active Wake Signal", bounded.get("active_wake_signal") or {}),
            (
                "Audit Task Objective",
                _audit_event_task_objective(
                    bounded.get("tasks"),
                    task_id=task_id,
                    wake_signal=bounded.get("active_wake_signal"),
                ),
            ),
            ("Runtime Load Errors", bounded.get("load_errors") or []),
            (
                "Background Context Projection",
                bounded.get("_projection") or {},
            ),
            ("Control Action Policy", policy_decision.to_dict()),
        ]
    lines = _context_header(request, thread)
    for title, payload in sections:
        lines.extend(["", f"## {title}", json_block(payload)])
    lines.extend(
        [
            "",
            "## Available Control Actions",
            *background_control_action_lines(
                getattr(agent, "config", None), request=policy_request
            ),
            "[/background-main-agent-context]",
        ]
    )
    return "\n".join(lines)


def _narrow_audit_event_reason(reason: object) -> bool:
    return str(reason or "").strip().lower() in {
        "audit_finding",
        "audit_capacity_alert",
    }


def _audit_event_task_objective(
    tasks: object,
    *,
    task_id: str,
    wake_signal: object = None,
) -> dict[str, object]:
    """Project exact run and source facts without a stale cross-source summary."""

    selected_id = str(task_id or "").strip()
    if not selected_id:
        return {}
    for row in _dict_rows(tasks):
        if str(row.get("task_id") or "").strip() != selected_id:
            continue
        projected = {
            key: row[key]
            for key in (
                "task_id",
                "work_kind",
                "work_name",
                "run_prompt",
                "effective_revision",
                "created_at",
            )
            if key in row
        }
        source_ids = _audit_event_source_ids(wake_signal)
        bindings = [
            dict(item)
            for item in _dict_rows(row.get("effective_source_bindings"))
            if str(item.get("source_id") or "").strip() in source_ids
        ]
        if bindings:
            projected["active_source_bindings"] = bindings
        return projected
    return {"task_id": selected_id}


def _audit_event_source_ids(wake_signal: object) -> set[str]:
    wake = dict(wake_signal) if isinstance(wake_signal, dict) else {}
    metadata = wake.get("metadata") if isinstance(wake.get("metadata"), dict) else {}
    source_ids = {
        str(metadata.get("source_id") or "").strip(),
    }
    findings = metadata.get("findings") if isinstance(metadata.get("findings"), list) else []
    for row in findings:
        if not isinstance(row, dict):
            continue
        item_metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        source_ids.add(str(item_metadata.get("source_id") or "").strip())
    source_ids.discard("")
    return source_ids


def _bounded_context(
    agent: object,
    store: ConversationStore,
    thread: ConversationThread,
    task_id: str,
    policy_request: BackgroundToolPolicyRequest,
    *,
    active_wake_signal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = getattr(agent, "config", None)
    load_errors: list[dict[str, Any]] = []
    state = _BackgroundContextLoad(
        agent, store, thread, task_id, config, policy_request, load_errors
    )
    narrow_audit_event = _narrow_audit_event_reason(policy_request.reason)
    visible_run_ids = [] if narrow_audit_event else _thread_active_task_ids(state)
    agent_tree = {} if narrow_audit_event else _agent_tree_payload(state, visible_run_ids)
    bundle = _context_bundle(state)
    if narrow_audit_event:
        bundle = _narrow_audit_event_bundle(bundle, task_id=task_id)
    pending_wake_signals = [] if narrow_audit_event else _pending_wake_signals(state)
    recovery_snapshot = (
        {} if narrow_audit_event else _safe_recovery_snapshot(state, visible_run_ids)
    )
    task_state = (
        {}
        if narrow_audit_event
        else task_runtime_state(
            agent=agent,
            store=store,
            thread_id=thread.thread_id,
            task_id=task_id,
            load_errors=load_errors,
        )
    )
    subagent_completions = (
        {} if narrow_audit_event else _background_subagent_completion_context(state)
    )
    return bounded_background_context_payload(
        BackgroundContextPayloadRequest(
            bundle=bundle,
            active_wake_signal=active_wake_signal,
            subagent_completions=subagent_completions,
            pending_wake_signals=pending_wake_signals,
            task_runtime_state=task_state,
            agent_tree=agent_tree,
            recovery_snapshot=recovery_snapshot,
            load_errors=load_errors,
            budget=background_context_budget_from_config(config),
        )
    )


# LLM: 会话运行时 keeps each child final answer in the parent session history. This runtime adapts that
# contract to the durable observation ledger: every later background slice for the same exact root
# gets the current direct-child completion projection, not only the one lifecycle wake that arrived.
# 函数用途: 读取当前根任务的直属子代理完成信封，供定时进度轮和恢复轮继续整合精确结果。
def _background_subagent_completion_context(
    state: _BackgroundContextLoad,
) -> dict[str, object]:
    task_id = str(state.task_id or "").strip()
    if not task_id:
        return {}
    try:
        observations, load_errors = state.store.recent_observations_report(
            state.thread.thread_id,
            limit=0,
            include_handled=True,
        )
        state.load_errors.extend(load_errors)
    except Exception as exc:
        state.load_errors.append(
            runtime_error_report(
                exc,
                context="background_context.subagent_completions",
            )
        )
        return {}
    context, issues = subagent_completion_context_from_observations(
        observations,
        root_task_ids={task_id},
        workspace_task_id=task_id,
        visible_limit=DEFAULT_VISIBLE_SUBAGENT_COMPLETIONS,
    )
    state.load_errors.extend(
        runtime_error_report(
            ValueError(issue),
            context="background_context.subagent_completions",
        )
        for issue in issues
    )
    return context


def _narrow_audit_event_bundle(
    bundle: object,
    *,
    task_id: str,
) -> dict[str, object]:
    """Keep stale conversation prose out of one typed Audit event turn.

    The total-context reducer budgets the full bundle before ``context_markdown``
    chooses visible sections.  Merely hiding Recent Messages at render time can
    therefore still shrink the current wake metadata.  Build the narrow bundle
    before budgeting so current structured facts keep priority over history.
    """

    row = dict(bundle) if isinstance(bundle, dict) else {}
    selected_id = str(task_id or "").strip()
    tasks = [
        item
        for item in _dict_rows(row.get("tasks"))
        if str(item.get("task_id") or "").strip() == selected_id
    ]
    return {
        "thread": row.get("thread") or {},
        "messages": [],
        "tasks": tasks,
        "channel_bindings": [],
        "observations": [],
        "guidance": [],
    }


def _context_bundle(state: _BackgroundContextLoad) -> dict[str, Any]:
    """Load one authoritative thread ledger and project the current task view."""
    try:
        if callable(getattr(state.store, "context_bundle_report", None)):
            bundle, load_errors = state.store.context_bundle_report(
                state.thread.thread_id,
                recent_limit=_config_int(state.config, "conversation_context_recent_limit"),
            )
            state.load_errors.extend(load_errors)
        else:
            bundle = state.store.context_bundle(
                state.thread.thread_id,
                recent_limit=_config_int(state.config, "conversation_context_recent_limit"),
            )
        return _task_scoped_operational_context(state, bundle)
    except Exception as exc:
        state.load_errors.append(
            runtime_error_report(exc, context="background_context.context_bundle")
        )
        return _minimal_context_bundle(state.thread)


# LLM: 任务范围裁决只能由"本轮显式 task 身份 + 已加载的同一份 bundle"形成一次，
# 供 operational 摘要与 native 历史种子共用。禁止按"第几个/最新一个 task"猜身份，
# 也禁止把全部 scoped task 的父/root id 无差别并成 lineage（那会放宽范围）。
# 类用途: 承载一次任务范围裁决的结构化事实。
@dataclass(frozen=True)
class _TaskScopeDecision:
    task_id: str
    task_ids: frozenset[str]
    exact_link: dict[str, Any] | None
    detached: bool


# LLM: exact_link 必须按本轮 task_id 精确匹配；无 task_id 的普通事件不做任何范围收缩。
# 函数用途: 用已加载的 bundle 与本轮 task_id 形成唯一范围裁决。
def _task_scope_decision(
    state: _BackgroundContextLoad,
    bundle: dict[str, Any],
) -> _TaskScopeDecision:
    task_id = str(state.task_id or "").strip()
    task_rows = _dict_rows(bundle.get("tasks"))
    if not task_id:
        return _TaskScopeDecision("", frozenset(), None, False)
    exact_link = next(
        (row for row in task_rows if str(row.get("task_id") or "").strip() == task_id),
        None,
    )
    return _TaskScopeDecision(
        task_id=task_id,
        task_ids=frozenset(_task_context_ids(state)),
        exact_link=exact_link,
        detached=_is_detached_named_task_link(exact_link),
    )


def _task_scoped_operational_context(
    state: _BackgroundContextLoad,
    bundle: dict[str, Any],
) -> dict[str, Any]:
    """Project one task from the shared ledger using only persisted identities.

    Ordinary foreground work keeps the continuous conversation history.  A
    detached named Audit/Goal is different: like a 会话运行时/模型助手 background
    fork, it may see the conversation that existed when it was created and
    later rows attributed to its exact task lineage, but not unrelated future
    user turns.  This remains one transcript and one compact authority; the
    projection neither copies history nor classifies natural-language text.
    """
    decision = _task_scope_decision(state, bundle)
    if not decision.task_id:
        return bundle
    task_ids = set(decision.task_ids)
    task_rows = _dict_rows(bundle.get("tasks"))
    scoped = dict(bundle)
    scoped["tasks"] = [row for row in task_rows if _context_row_matches_task_ids(row, task_ids)]
    scoped["observations"] = [
        row
        for row in _dict_rows(bundle.get("observations"))
        if _context_row_matches_task_ids(row, task_ids)
    ]
    scoped["goals"] = [
        row
        for row in _dict_rows(bundle.get("goals"))
        if _context_row_matches_task_ids(row, task_ids)
    ]
    if decision.detached:
        return _detached_named_task_context(state, scoped, decision.exact_link or {}, task_ids)
    return scoped


def _is_detached_named_task_link(link: dict[str, Any] | None) -> bool:
    if not isinstance(link, dict):
        return False
    return (
        str(link.get("cancellation_scope") or "").strip().lower() == "detached"
        and str(link.get("work_kind") or "").strip().lower() in {"audit", "goal"}
        and bool(str(link.get("work_name") or "").strip())
    )


def _detached_named_task_context(
    state: _BackgroundContextLoad,
    scoped: dict[str, Any],
    link: dict[str, Any],
    task_ids: set[str],
) -> dict[str, Any]:
    """Return the creation snapshot plus exact later task traffic.

    Raw messages stay append-only in the shared thread.  Reading the ledger and
    applying its persisted anchor/task ids is intentionally fail-closed: if an
    old anchor cannot be found, only rows explicitly attributed to this task are
    exposed.
    """
    projected = dict(scoped)
    projected["messages"] = _detached_task_messages(state, link, task_ids)
    projected["guidance"] = _detached_task_rows(
        _dict_rows(scoped.get("guidance")),
        link,
        task_ids,
    )
    thread = dict(scoped.get("thread")) if isinstance(scoped.get("thread"), dict) else {}
    if not _detached_summary_precedes_task(thread, link):
        for key, empty in (
            ("summary", ""),
            ("compact_operation_evidence", {}),
            ("compacted_through_message_id", ""),
            ("compacted_through_byte_offset", 0),
            ("compact_generation", 0),
            ("compact_updated_at", 0.0),
            ("compact_source_messages", 0),
            ("compact_checkpoint_id", ""),
        ):
            thread[key] = empty
    thread["task_ids"] = [state.task_id]
    thread["active_task_ids"] = (
        [state.task_id] if str(link.get("status") or "").strip().lower() == "active" else []
    )
    if str(thread.get("workspace_task_id") or "").strip() != state.task_id:
        thread["workspace_task_id"] = ""
    projected["thread"] = thread
    return projected


def _detached_task_messages(
    state: _BackgroundContextLoad,
    link: dict[str, Any],
    task_ids: set[str],
) -> list[dict[str, Any]]:
    try:
        rows, errors = state.store.recent_messages_report(
            state.thread.thread_id,
            limit=0,
        )
        state.load_errors.extend(errors)
        payload = [row.to_dict() for row in rows]
    except Exception as exc:
        state.load_errors.append(
            runtime_error_report(exc, context="background_context.detached_messages")
        )
        payload = []
    selected = _detached_task_rows(payload, link, task_ids)
    limit = _config_int(state.config, "conversation_context_recent_limit")
    return selected if limit <= 0 else selected[-limit:]


def _detached_task_rows(
    rows: list[dict[str, Any]],
    link: dict[str, Any],
    task_ids: set[str],
) -> list[dict[str, Any]]:
    anchor_id = str(link.get("context_anchor_message_id") or "").strip()
    if not any(str(row.get("message_id") or "").strip() for row in rows):
        anchor_id = ""
    try:
        created_at = float(link.get("created_at") or 0.0)
    except (TypeError, ValueError):
        created_at = 0.0
    before_anchor = bool(anchor_id)
    anchor_found = not anchor_id
    selected: list[dict[str, Any]] = []
    for row in rows:
        exact_task_row = _context_row_matches_task_ids(row, task_ids)
        snapshot_row = False
        if anchor_id and before_anchor:
            snapshot_row = True
            if str(row.get("message_id") or "").strip() == anchor_id:
                before_anchor = False
                anchor_found = True
        elif not anchor_id:
            try:
                snapshot_row = float(row.get("created_at") or 0.0) <= created_at
            except (TypeError, ValueError):
                snapshot_row = False
        if exact_task_row or snapshot_row:
            selected.append(row)
    if anchor_id and not anchor_found:
        return [row for row in rows if _context_row_matches_task_ids(row, task_ids)]
    return selected


def _detached_summary_precedes_task(
    thread: dict[str, Any],
    link: dict[str, Any],
) -> bool:
    if not str(thread.get("summary") or "").strip():
        return True
    try:
        task_created_at = float(link.get("created_at") or 0.0)
        compact_updated_at = float(thread.get("compact_updated_at") or 0.0)
        thread_updated_at = float(thread.get("updated_at") or 0.0)
    except (TypeError, ValueError):
        return False
    summary_updated_at = compact_updated_at if compact_updated_at > 0 else thread_updated_at
    return task_created_at > 0 and 0 < summary_updated_at <= task_created_at


def _dict_rows(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(row) for row in value if isinstance(row, dict)]


_TASK_CONTEXT_ID_KEYS = (
    "task_id",
    "root_task_id",
    "conversation_task_id",
    "gateway_request_id",
)


def _context_row_matches_task_ids(row: dict[str, Any], task_ids: set[str]) -> bool:
    if any(str(row.get(key) or "").strip() in task_ids for key in _TASK_CONTEXT_ID_KEYS):
        return True
    metadata = row.get("metadata")
    if not isinstance(metadata, dict):
        return False
    if any(str(metadata.get(key) or "").strip() in task_ids for key in _TASK_CONTEXT_ID_KEYS):
        return True
    attributes = metadata.get("task_attributes")
    return isinstance(attributes, dict) and any(
        str(attributes.get(key) or "").strip() in task_ids for key in _TASK_CONTEXT_ID_KEYS
    )


def _task_context_ids(state: _BackgroundContextLoad) -> set[str]:
    """Resolve one task's persisted subagent lineage without using prompt text."""
    task_id = str(state.task_id or "").strip()
    if not task_id:
        return set()
    task_ids = {task_id}
    manager = getattr(state.agent, "subagents", None)
    if manager is None:
        return task_ids
    try:
        current = manager.load(task_id)
    except Exception:
        current = None
    root_id = str(getattr(current, "root_id", "") or "").strip() or task_id
    task_ids.add(root_id)
    try:
        runs = manager.list_runs()
    except Exception:
        return task_ids
    for run in runs:
        run_id = str(getattr(run, "id", "") or "").strip()
        run_root_id = str(getattr(run, "root_id", "") or "").strip()
        if run_id and (run_id == root_id or run_root_id == root_id):
            task_ids.add(run_id)
    return task_ids


# LLM: pending wake 读取失败要留 load_error；成功行只净化模型视图，不改原事件。
# 函数用途: 读取当前线程待处理唤醒，并剔除其它 task 和已退役工具提示。
def _pending_wake_signals(state: _BackgroundContextLoad) -> list[dict[str, Any]]:
    try:
        if callable(getattr(state.store, "pending_wake_signals_report", None)):
            signals, load_errors = state.store.pending_wake_signals_report(
                limit=_config_int(state.config, "background_pending_wake_prompt_limit"),
            )
            state.load_errors.extend(load_errors)
            payload = [
                _model_visible_wake_signal(item.to_dict()) or {}
                for item in signals
                if item.thread_id == state.thread.thread_id
            ]
        else:
            payload = [
                _model_visible_wake_signal(item) or {}
                for item in pending_wake_payload(
                    state.store,
                    state.thread.thread_id,
                    limit=_config_int(
                        state.config,
                        "background_pending_wake_prompt_limit",
                    ),
                )
            ]
        if not state.task_id:
            return payload
        task_ids = _task_context_ids(state)
        return [row for row in payload if _context_row_matches_task_ids(row, task_ids)]
    except Exception as exc:
        state.load_errors.append(
            runtime_error_report(exc, context="background_context.pending_wake_signals")
        )
        return []


def _agent_tree_payload(
    state: _BackgroundContextLoad,
    visible_run_ids: list[str],
) -> dict[str, Any]:
    try:
        return agent_tree_status_payload(
            state.agent,
            {
                "visible_run_ids": visible_run_ids,
                "allowed_tools": background_allowed_tools(
                    state.config, request=state.policy_request
                ),
            },
        )
    except Exception as exc:
        report = runtime_error_report(exc, context="background_context.agent_tree")
        state.load_errors.append(report)
        return {
            "schema_version": "agent_tree_status.v1",
            "effect": "read_only",
            "nodes": [],
            "edges": [],
            "warnings": ["agent_tree_load_error"],
            "load_error": report,
        }


def _safe_recovery_snapshot(
    state: _BackgroundContextLoad,
    visible_run_ids: list[str],
) -> dict[str, Any]:
    try:
        return _recovery_snapshot(state.agent, state.store, state.thread.thread_id, visible_run_ids)
    except Exception as exc:
        report = runtime_error_report(exc, context="background_context.recovery_snapshot")
        state.load_errors.append(report)
        return {
            "schema_version": "background_recovery_snapshot.v1",
            "effect": "read_only",
            "does_not_block": True,
            "load_error": report,
        }


def _tool_policy_request(
    agent: object,
    request: object,
    *,
    proactive_delivery_available: bool | None = None,
) -> BackgroundToolPolicyRequest:
    return BackgroundToolPolicyRequest(
        reason=str(getattr(request, "reason", "") or ""),
        wake_signal=getattr(request, "wake_signal", None),
        config=getattr(agent, "config", None),
        owner_policy=getattr(agent, "owner_policy", None),
        policy_snapshot=_policy_snapshot_from_request(request),
        proactive_delivery_available=proactive_delivery_available,
    )


def _policy_snapshot_from_request(request: object) -> dict[str, Any]:
    wake = getattr(request, "wake_signal", None)
    if isinstance(wake, dict) and isinstance(wake.get("policy_snapshot"), dict):
        return dict(wake["policy_snapshot"])
    return {}


def _config_int(config: object | None, key: str) -> int:
    if config is None:
        return max(0, int(default_config_value(key)))
    try:
        return max(0, int(getattr(config, key)))
    except (TypeError, ValueError):
        return max(0, int(default_config_value(key)))


def _context_header(request, thread: ConversationThread) -> list[str]:
    return [
        "[background-main-agent-context]",
        f"reason: {request.reason}",
        f"thread_id: {thread.thread_id}",
        f"task_id: {request.task_id or ''}",
        (
            "authority: current typed runtime state and current tool results "
            "override earlier assistant, child, summary, and artifact prose"
        ),
    ]


def _thread_active_task_ids(state: _BackgroundContextLoad) -> list[str]:
    if state.task_id:
        return sorted(_task_context_ids(state))
    latest = _latest_thread_with_load_error(state)
    source = latest or state.thread
    return [str(item or "").strip() for item in source.active_task_ids if str(item or "").strip()]


def _latest_thread_with_load_error(state: _BackgroundContextLoad) -> ConversationThread | None:
    try:
        if not callable(getattr(state.store, "load_thread_report", None)):
            return state.store.load_thread(state.thread.thread_id)
        latest, load_error = state.store.load_thread_report(state.thread.thread_id)
        if load_error is not None:
            load_error["consumer_context"] = "background_context.thread_active_tasks"
            state.load_errors.append(load_error)
        return latest
    except Exception as exc:
        state.load_errors.append(
            runtime_error_report(exc, context="background_context.thread_active_tasks")
        )
        return None


# LLM: 最小模型上下文与完整 bundle 使用相同的显示排除边界；读取失败不能把 UI 遥测写进 prompt。
# 函数用途: 提供缺少其它会话材料时的最小运行上下文，不包含 Context 展示数字。
# LLM: 精简模型上下文与完整上下文同样排除纯显示遥测，避免每次数字刷新破坏缓存前缀。
# 函数用途: 构造无历史时的会话上下文，不把终端统计条传给模型。
def _minimal_context_bundle(thread: ConversationThread) -> dict[str, Any]:
    thread_payload = thread.to_dict()
    thread_payload.pop("model_context_usage", None)
    thread_payload.pop("model_metrics", None)
    return {
        "thread": thread_payload,
        "messages": [],
        "tasks": [],
        "channel_bindings": [item.to_dict() for item in thread.channel_bindings],
        "observations": [],
        "guidance": [],
    }


def _recovery_snapshot(
    agent: object, store: ConversationStore, thread_id: str, visible_run_ids: list[str]
) -> dict[str, Any]:
    claim = store.load_background_run_claim(thread_id)
    previous = claim.get("previous_claim") if isinstance(claim.get("previous_claim"), dict) else {}
    tree = agent_tree_status_payload(agent, {"visible_run_ids": visible_run_ids})
    records = latest_artifact_records(getattr(agent, "root", "."))
    payload = {
        "schema_version": "background_recovery_snapshot.v1",
        "effect": "read_only",
        "does_not_block": True,
        "current_claim_status": str(claim.get("status") or ""),
        "current_claim_id": str(claim.get("claim_id") or ""),
        "current_claim_reason": str(claim.get("reason") or ""),
        "previous_claim_status": str(previous.get("status") or ""),
        "previous_claim_error": previous.get("last_error")
        if isinstance(previous.get("last_error"), dict)
        else {},
        "takeover": claim.get("takeover") if isinstance(claim.get("takeover"), dict) else {},
        "tree_status_buckets": tree.get("status_buckets")
        if isinstance(tree.get("status_buckets"), dict)
        else {},
        "artifact_registry_count": len(records),
        "artifact_registry_status_counts": _artifact_status_counts(records),
        "takeover_advice": "接手前先核对 claim、任务树和产物登记；不要把模型文本里的完成声明当成事实。",
    }
    if isinstance(claim.get("load_error"), dict):
        payload["claim_load_error"] = claim["load_error"]
    return payload


def _artifact_status_counts(records: dict[str, object]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records.values():
        status = str(getattr(record, "status", "") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


# Conversation runtime scheduler
import logging
import threading
from typing import TYPE_CHECKING

from ..settings.defaults import default_config_int
from .models import ProgressPolicy

# 后台 claim 心跳是 daemon 线程，其异常必须结构化落日志而非裸崩 stderr 杀线程。
_HEARTBEAT_LOGGER = logging.getLogger("agent.conversation.background_claim_heartbeat")

# 任务续跑 wake 冷却(秒):距上次落 wake 不足此值时不再重复催促——子代理正在跑/
# 主代理 turn 还在推进的窗口内,重复拉起只有模型调用成本没有新信息(真机 celery
# 46 分钟 18 次 task_ledger_resume)。死停最长 cooldown 内被发现(H 批前是 6h)。
_TASK_RESUME_COOLDOWN_SECONDS = 900
# 三源对账降频(2026-08-17 扫描治理): 全量扫任务档案只作低频兜底(5 分钟),
# 热层每 tick 只消费 wake_queue 到期字条(索引查询)。长期助手 分频同款。
_WAKE_RECONCILE_INTERVAL_SECONDS = 300.0
# R1-03 孤儿 attempt 回收频率：tick ~2min 一次，回收 5min 一趟
_ORPHAN_RECLAIM_INTERVAL_SECONDS = 300

_TASK_LINK_TERMINAL_STATUSES = frozenset(
    {
        "ABANDONED",
        "CANCELLED",
        "CHANNEL_ERROR",
        "COMPLETED",
        "DONE",
        "FAILED",
        "INTERRUPTED",
        "SUPERSEDED",
        "TAKEN_OVER",
        "TIMEOUT",
    }
)

# policy 退休用终态(⊇ 上面那套):BLOCKED 的子代理不会再自动推进,watch 它只会
# 每 interval 空转一次唤醒(真机 2026-08-09:BLOCKED fixer 的 300s policy 等到
# 2h stale 窗口才退休)。不能并入 _TASK_LINK_TERMINAL_STATUSES:那里还被 wake
# 信号过期判定复用,BLOCKED 子代理的 lifecycle 事件必须唤醒父代理去处理,
# 误判终态会把「子代理卡住」的信号当 stale 丢弃。BLOCKED 只在这里(=有 policy
# 挂它)当终态:父代理靠 BLOCKED 事件链唤醒,不靠 watch policy。
_POLICY_RETIRE_TERMINAL_STATUSES = frozenset(_TASK_LINK_TERMINAL_STATUSES | {"BLOCKED"})
_MIN_PROGRESS_POLICY_CATCHUP_SECONDS = 7200
_MAX_PROGRESS_POLICY_CATCHUP_INTERVALS = 4

# 陈旧账本 gc 节奏:tick ~2 分钟一次,这里最多 6 小时跑一趟归档(问题8)。
_LEDGER_GC_INTERVAL_SECONDS = 6 * 3600

# 失败续跑记账(问题6):失败 run 后 policy 退避 5min×2^(n-1)、上限 1h;抖动按
# policy_id 确定性派生(纯函数,可测,不引入 random);连续 3 次失败退休(等用户)。
_POLICY_FAILURE_BASE_BACKOFF_SECONDS = 300
_POLICY_FAILURE_MAX_BACKOFF_SECONDS = 3600
_POLICY_FAILURE_RETIRE_AFTER = 3

if TYPE_CHECKING:
    from ..collaboration import CollaborationStore


# 供应断供的 tick 层长退避(治真机"429 限流断供把后台消费永久冻死"):短期限流在
# turn 内 auto_resume 短链用尽后会抛回消费循环；套餐额度耗尽则应立刻进入本长退避，
# 等显式模型/凭据切换或额度重置，不能在 turn 内无效重试。旧行为
# 两宗罪:①异常中断整个 tick——一个撞限流的会话把同 tick 的其他唤醒/观察/判读全部队头阻塞;
# ②下一 poll(秒级)立刻重打已限流的模型,额度按分钟/小时刷新,秒级猛打只会加重限流。
# 这里按 thread 记内存态指数退避:第 n 次失败等 base*2^(n-1) 秒(封顶 max),到点自动重试;
# 成功即清零。信号/policy 不被标记消费,退避只是"本轮跳过",供应恢复后自动续跑,无需人肉。
# 进程重启态丢失=重启后立刻重试一次,无害。判据只认 typed ProviderTransientError,不做文本匹配。
class _ProviderSupplyBackoff:
    def __init__(self, *, base_seconds: float = 30.0, max_seconds: float = 900.0):
        self._base = max(1.0, float(base_seconds))
        self._cap = max(self._base, float(max_seconds))
        self._streaks: dict[str, int] = {}
        self._next_attempt_at: dict[str, float] = {}

    def should_attempt(self, thread_id: str, now: float) -> bool:
        return now >= self._next_attempt_at.get(str(thread_id), 0.0)

    def record_failure(self, thread_id: str, now: float) -> dict[str, object]:
        key = str(thread_id)
        streak = self._streaks.get(key, 0) + 1
        self._streaks[key] = streak
        delay = min(self._base * (2 ** (streak - 1)), self._cap)
        self._next_attempt_at[key] = now + delay
        return {
            "thread_id": key,
            "consecutive_failures": streak,
            "retry_delay_seconds": delay,
            "next_attempt_at": now + delay,
        }

    def record_success(self, thread_id: str) -> int:
        key = str(thread_id)
        self._next_attempt_at.pop(key, None)
        return self._streaks.pop(key, 0)


def _consume_with_supply_guard(backoff: _ProviderSupplyBackoff, thread_id: str, now: float, run):
    """带供应退避护栏跑一个后台消费 turn(唤醒/观察/盯守判读共用)。
    冷却中 → 不消费返回 None(信号/policy 留 pending,到点自动重试);
    供应错 → 吸收进退避返回 None,放行其余会话;非供应异常原样上抛;
    成功拿到 report → 清退避计数(供应恢复)。"""
    if not backoff.should_attempt(thread_id, now):
        return None
    started = time.monotonic()
    try:
        report = run()
    except Exception as exc:
        # 退避锚点=失败真实时刻(tick 逻辑时刻 + turn 实际耗时)。turn 内 auto_resume 短链
        # 本身要跑几分钟,若锚在 tick 起点,next_attempt_at 在 turn 结束时早已过期 → 退避
        # 形同虚设、下一 poll 立刻猛打(隔离演练请求账实锤)。monotonic 差不受注入时钟影响。
        failed_at = now + (time.monotonic() - started)
        if not _absorb_provider_supply_failure(backoff, thread_id, failed_at, exc):
            raise
        return None
    if report is not None:
        _note_supply_recovery(backoff, thread_id)
    return report


def _absorb_provider_supply_failure(
    backoff: _ProviderSupplyBackoff, thread_id: str, now: float, exc: BaseException
) -> bool:
    """临时供应错(限流/断供/超载)专属吸收:记长退避+打点,放行本 tick 其余会话的消费
    (治队头阻塞);其他异常一律不吸、照旧上抛走 [gateway-loop-error] 兜底(真 bug 不掩盖)。
    套餐额度耗尽有独立的单次通知+人工恢复路线，绝不能进入本自动重试环。
    判据只认 typed provider supply error；不匹配错误文本。"""
    if not is_provider_transient_error(exc):
        return False
    payload = backoff.record_failure(thread_id, now)
    payload["error_type"] = exc.__class__.__name__
    payload["error"] = compact_error_message(exc)
    _print_supply_event("provider_supply_backoff", payload)
    return True


def _note_supply_recovery(backoff: _ProviderSupplyBackoff, thread_id: str) -> None:
    failed_attempts = backoff.record_success(thread_id)
    if failed_attempts:
        _print_supply_event(
            "provider_supply_resumed",
            {"thread_id": thread_id, "failed_attempts": failed_attempts},
        )


def _print_supply_event(event: str, payload: dict[str, object]) -> None:
    body = json.dumps({"event": event, **payload}, ensure_ascii=False, sort_keys=True)
    print(f"[gateway-supply-backoff] {body}", flush=True)


def _supply_backoff_from_agent(agent: object) -> _ProviderSupplyBackoff:
    guard_policy = getattr(agent, "runtime_guard_policy", None)
    return _ProviderSupplyBackoff(
        base_seconds=runtime_guard_int(
            "provider_supply_backoff_base_seconds", 30, policy=guard_policy
        ),
        max_seconds=runtime_guard_int(
            "provider_supply_backoff_max_seconds", 900, policy=guard_policy
        ),
    )


# 三条后台消费车道(唤醒信号/观察批/到点 policy)。都过 _consume_with_supply_guard:
# 供应断供时按会话退避而不是中断整个 tick,恢复后自动续跑。
def _is_scheduler_wake_signal(signal: WakeSignal) -> bool:
    return str(getattr(signal, "reason", "") or "").strip().lower() == "scheduled_job_due"


# LLM: pending wake 的 runnable limit 必须在 recovery-block 过滤后计算，且被
# unknown 挡住的信号不可标 handled 或 attempted。
# 函数用途: 消费本轮可运行的 durable wake，并保留等待恢复的旧信号。
def _consume_pending_wake_signals(
    scheduler: BackgroundMainAgentScheduler,
    reports: list[BackgroundMainAgentReport],
    current: float,
    *,
    target_thread_id: str = "",
) -> set[str]:
    reported: set[str] = set()
    handled: set[str] = set()
    attempted: set[str] = set()
    wake_limit = scheduler._config_limit("conversation_pending_wake_limit")
    # Store 本来就会读取全部 pending 后再切片；这里保留完整队列，令前排被
    # recovery block 保留的旧事件不占掉新任务的消费窗口。
    wake_signals = scheduler.store.pending_wake_signals(limit=0)
    for signal in wake_signals:
        if target_thread_id and signal.thread_id != target_thread_id:
            continue
        if wake_limit > 0 and len(attempted) >= wake_limit:
            break
        if _skip_pending_wake_signal(
            scheduler,
            signal,
            current=current,
            reported=reported,
            handled=handled,
            attempted=attempted,
        ):
            continue
        _consume_wake_signal_batch(
            scheduler,
            signal,
            wake_signals,
            reports,
            reported,
            handled,
            attempted,
            current,
        )
    return reported


# LLM: 跳过只决定本 tick 不运行；除明确 stale/已投递分支外不得消费持久信号。
# 函数用途: 在模型调用前筛掉冷却、重复、恢复阻塞或已失效的 wake。
def _skip_pending_wake_signal(
    scheduler: BackgroundMainAgentScheduler,
    signal: WakeSignal,
    *,
    current: float,
    reported: set[str],
    handled: set[str],
    attempted: set[str],
) -> bool:
    if current < scheduler._wake_retry_after.get(signal.wake_signal_id, 0.0):
        return True
    if signal.wake_signal_id in handled or signal.wake_signal_id in attempted:
        return True
    if (
        _background_authority_recovery_block(scheduler, str(signal.root_task_id or "").strip())
        is not None
    ):
        return True
    if signal.thread_id in reported and not _is_scheduler_wake_signal(signal):
        # One model turn already owns this thread for the current tick.
        return True
    if _reported_audit_finding_wake(scheduler, signal):
        # Provider receipts are the delivery authority after a publish crash.
        scheduler._mark_signal(signal, current, handled)
        return True
    if _successful_completion_waiting_for_batch(scheduler, signal, current):
        return True
    from .process_events import process_completion_delivery_state

    if (not _wake_survives_inactive_root(signal)
        and _wake_signal_root_is_inactive(scheduler.store, signal)):
        completion = process_completion_delivery_state(scheduler.runtime.agent, signal)
        if completion:
            return completion != "ready"
        # Ordinary late child lifecycle signals cannot revive an inactive root.
        scheduler._mark_signal(signal, current, handled)
        return True
    return False
# LLM: 批次执行与确认仍走 scheduler 的耐久 wake 合同；runtime 已提交 canonical 回复，这里不能另存正文。
# 函数用途: 运行一批同源唤醒、登记调度结果并处理确认或退避，不创建第二条通知主链。
def _consume_wake_signal_batch(
    scheduler: BackgroundMainAgentScheduler,
    signal: WakeSignal,
    wake_signals: list[WakeSignal],
    reports: list[BackgroundMainAgentReport],
    reported: set[str],
    handled: set[str],
    attempted: set[str],
    current: float,
) -> None:
    wake_batch = _select_wake_batch(scheduler, signal, wake_signals)
    execution_signal = _batched_wake_signal(wake_batch)
    attempted.update(member.wake_signal_id for member in wake_batch)
    report = _consume_with_supply_guard(
        scheduler._supply_backoff,
        execution_signal.thread_id,
        current,
        partial(scheduler._run_wake_signal, execution_signal, now=current),
    )
    if report is None:
        _defer_failed_wake_batch(scheduler, wake_batch, execution_signal, current)
        return
    # 正文由 runtime 统一提交；所有后台车道只把调度报告放进返回列表。
    reports.append(report)
    if _is_scheduler_wake_signal(signal):
        return
    reported.add(report.thread_id)
    scheduler._mark_sibling_signals(
        list(wake_batch),
        signal,
        current,
        handled,
        sampled_at=report.created_at,
    )
    if len(wake_batch) <= 1:
        return
    for member in wake_batch:
        if member.wake_signal_id not in handled:
            scheduler._mark_signal(member, current, handled)
    if _typed_audit_finding_signal(signal):
        from .task_promotion import complete_named_audit_task_if_settled

        complete_named_audit_task_if_settled(
            scheduler.runtime.agent,
            str(signal.root_task_id or "").strip(),
        )


def _defer_failed_wake_batch(
    scheduler: BackgroundMainAgentScheduler,
    wake_batch: tuple[WakeSignal, ...],
    execution_signal: WakeSignal,
    current: float,
) -> None:
    if len(wake_batch) <= 1:
        return
    # One batch is one delivery attempt, so every member shares its retry edge.
    retry_at = max(
        current + 30.0,
        scheduler._wake_retry_after.get(execution_signal.wake_signal_id, 0.0),
    )
    for member in wake_batch:
        scheduler._wake_retry_after[member.wake_signal_id] = retry_at


def _typed_audit_finding_signal(signal: WakeSignal) -> bool:
    metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
    return (
        str(signal.reason or "").strip().lower() == "audit_finding"
        and str(metadata.get("schema_version") or "") == "audit-finding-event.v1"
        and metadata.get("requires_llm_report") is True
        and bool(str(metadata.get("finding_id") or "").strip())
        and bool(_audit_finding_signal_delivery_refs(signal))
    )


def _wake_survives_inactive_root(signal: WakeSignal) -> bool:
    """Keep committed owner-facing events durable past task settlement.

    A child lifecycle notice is stale once its root task is no longer active.
    A typed Audit finding is different: the durable finding already exists and
    its evidence refs plus provider receipt form an outstanding delivery
    obligation.  Task settlement must not silently acknowledge that obligation.
    """

    return _typed_audit_finding_signal(signal)


def _audit_finding_signal_delivery_refs(signal: WakeSignal) -> tuple[str, ...]:
    return _audit_finding_payload_delivery_refs(signal.to_dict())


def _reported_audit_finding_wake(
    scheduler: BackgroundMainAgentScheduler,
    signal: WakeSignal,
) -> bool:
    if not _typed_audit_finding_signal(signal):
        return False
    agent = getattr(scheduler.runtime, "agent", None)
    owner_home = str(
        getattr(getattr(agent, "home_paths", None), "owner_home_dir", "") or ""
    ).strip()
    if not owner_home:
        return False
    from ..ingestion.harvester import audit_source_refs_reported

    return audit_source_refs_reported(
        Path(owner_home),
        _audit_finding_signal_delivery_refs(signal),
    )


def _same_audit_finding_batch(primary: WakeSignal, sibling: WakeSignal) -> bool:
    if not _typed_audit_finding_signal(primary) or not _typed_audit_finding_signal(sibling):
        return False
    if (
        primary.thread_id != sibling.thread_id
        or primary.root_task_id != sibling.root_task_id
        or _cached_owner_delivery(sibling) is not None
    ):
        return False
    primary_metadata = primary.metadata if isinstance(primary.metadata, dict) else {}
    sibling_metadata = sibling.metadata if isinstance(sibling.metadata, dict) else {}
    primary_epoch = _nonnegative_int(primary_metadata.get("run_epoch"))
    sibling_epoch = _nonnegative_int(sibling_metadata.get("run_epoch"))
    primary_audit_id = str(primary_metadata.get("audit_id") or "").strip()
    return (
        bool(primary_audit_id)
        and primary_audit_id == str(sibling_metadata.get("audit_id") or "").strip()
        and primary_epoch >= 0
        and primary_epoch == sibling_epoch
    )


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return -1


def _select_audit_finding_batch(
    scheduler: BackgroundMainAgentScheduler,
    primary: WakeSignal,
    signals: list[WakeSignal],
) -> tuple[WakeSignal, ...]:
    """Take one bounded same-Audit mailbox batch without losing event identity.

    会话运行时 drains pending inter-agent mail together and 终端交互 drains queued
    commands of the same mode together.  Audit findings need the same queue
    property, while 长期助手 producer identities and receipts still remain
    one row per finding.  The existing background context budget bounds the
    batch; this adds no Audit-specific semantic classification.
    """

    if not _typed_audit_finding_signal(primary) or _cached_owner_delivery(primary) is not None:
        return (primary,)
    config = getattr(getattr(scheduler.runtime, "agent", None), "config", None)
    item_limit = _agent_config_int(config, "background_pending_wake_prompt_limit") or 20
    context_tokens = _agent_config_int(config, "background_context_max_total_tokens") or 8000
    # Keep half the bounded background projection for the fixed prompt, exact
    # source/task facts, and the model-authored report.  Whole wake envelopes
    # are admitted or deferred; no finding is truncated across batches.
    batch_token_budget = max(1024, context_tokens // 2)
    from ..memory_archive.tokens import estimate_tokens

    selected: list[WakeSignal] = [primary]
    projected: list[dict[str, Any]] = [primary.to_dict()]
    primary_seen = False
    for candidate in signals:
        if len(selected) >= max(1, item_limit):
            break
        if candidate.wake_signal_id == primary.wake_signal_id:
            primary_seen = True
            continue
        if not primary_seen:
            continue
        if not _same_audit_finding_batch(primary, candidate):
            continue
        if _reported_audit_finding_wake(scheduler, candidate):
            # A sibling may have a durable delivery receipt while its wake file
            # is still pending after a crash.  Leave it for the outer loop to
            # archive, but never include it in a new owner message.
            continue
        candidate_payload = candidate.to_dict()
        next_projected = [*projected, candidate_payload]
        if selected and estimate_tokens(next_projected) > batch_token_budget:
            break
        selected.append(candidate)
        projected = next_projected
    return tuple(selected)


def _select_wake_batch(
    scheduler: BackgroundMainAgentScheduler,
    primary: WakeSignal,
    signals: list[WakeSignal],
) -> tuple[WakeSignal, ...]:
    if _typed_audit_finding_signal(primary):
        return _select_audit_finding_batch(scheduler, primary, signals)
    if _successful_completion_signal(primary):
        return _select_successful_completion_batch(scheduler, primary, signals)
    return _select_same_reason_wake_batch(scheduler, primary, signals)


# LLM: This selector takes one bounded mailbox slice of typed DONE events from one
# exact root. Deferred siblings remain durable and the conversation closeout gate
# keeps the root active until later slices consume them; never acknowledge a wake
# merely because the canonical child tree is terminal. Failures/Audit stay separate.
# 函数用途: 按条数和提示预算读取一批成功完成信封，剩余信封留待后续后台轮。
def _select_successful_completion_batch(
    scheduler: BackgroundMainAgentScheduler,
    primary: WakeSignal,
    signals: list[WakeSignal],
) -> tuple[WakeSignal, ...]:
    config = getattr(getattr(scheduler.runtime, "agent", None), "config", None)
    item_limit = _agent_config_int(config, "background_pending_wake_prompt_limit") or 20
    context_tokens = _agent_config_int(config, "background_context_max_total_tokens") or 8000
    batch_token_budget = max(1536, (context_tokens * 3) // 4)
    from ..memory_archive.tokens import estimate_tokens

    selected: list[WakeSignal] = [primary]
    projected: list[dict[str, Any]] = [primary.to_dict()]
    primary_seen = False
    for candidate in signals:
        if len(selected) >= max(1, item_limit):
            break
        if candidate.wake_signal_id == primary.wake_signal_id:
            primary_seen = True
            continue
        if not primary_seen or not _same_successful_completion_batch(primary, candidate):
            continue
        candidate_payload = candidate.to_dict()
        next_projected = [*projected, candidate_payload]
        if estimate_tokens(next_projected) > batch_token_budget:
            break
        selected.append(candidate)
        projected = next_projected
    return tuple(selected)


def _same_reason_wake_batch(primary: WakeSignal, sibling: WakeSignal) -> bool:
    """Batch only typed queue events proved to share one task and reason."""

    reason = str(primary.reason or "").strip().lower()
    if (
        not reason
        or reason in {"scheduled_job_due", "subagent_runner_finished"}
        or primary.wake_signal_id == sibling.wake_signal_id
        or primary.thread_id != sibling.thread_id
        or primary.root_task_id != sibling.root_task_id
        or reason != str(sibling.reason or "").strip().lower()
        or _cached_owner_delivery(primary) is not None
        or _cached_owner_delivery(sibling) is not None
    ):
        return False
    return not _typed_audit_finding_signal(primary) and not _typed_audit_finding_signal(sibling)


def _select_same_reason_wake_batch(
    scheduler: BackgroundMainAgentScheduler,
    primary: WakeSignal,
    signals: list[WakeSignal],
) -> tuple[WakeSignal, ...]:
    """Project a bounded same-task event group into one model turn.

    Every member stays individually durable.  The active wake contains each
    exact envelope, and members are acknowledged only after that one turn is
    committed.  Unrelated same-thread events are never swept up implicitly.
    """

    config = getattr(getattr(scheduler.runtime, "agent", None), "config", None)
    item_limit = _agent_config_int(config, "background_pending_wake_prompt_limit") or 20
    context_tokens = _agent_config_int(config, "background_context_max_total_tokens") or 8000
    batch_token_budget = max(1024, context_tokens // 2)
    from ..memory_archive.tokens import estimate_tokens

    selected: list[WakeSignal] = [primary]
    projected: list[dict[str, Any]] = [primary.to_dict()]
    primary_seen = False
    for candidate in signals:
        if len(selected) >= max(1, item_limit):
            break
        if candidate.wake_signal_id == primary.wake_signal_id:
            primary_seen = True
            continue
        if not primary_seen or not _same_reason_wake_batch(primary, candidate):
            continue
        candidate_payload = candidate.to_dict()
        next_projected = [*projected, candidate_payload]
        if estimate_tokens(next_projected) > batch_token_budget:
            break
        selected.append(candidate)
        projected = next_projected
    return tuple(selected)


def _batched_wake_signal(signals: tuple[WakeSignal, ...]) -> WakeSignal:
    if _typed_audit_finding_signal(signals[0]):
        return _batched_audit_finding_signal(signals)
    return _batched_same_reason_wake_signal(signals)


def _batched_same_reason_wake_signal(signals: tuple[WakeSignal, ...]) -> WakeSignal:
    primary = signals[0]
    if len(signals) == 1:
        return primary
    evidence_refs = tuple(
        dict.fromkeys(ref for signal in signals for ref in signal.evidence_refs if ref)
    )
    metadata = dict(primary.metadata or {})
    metadata.update(
        {
            "event_count": len(signals),
            "batched_wake_signal_ids": [signal.wake_signal_id for signal in signals],
            "events": [signal.to_dict() for signal in signals],
        }
    )
    return WakeSignal(
        wake_signal_id=primary.wake_signal_id,
        thread_id=primary.thread_id,
        observation_id=primary.observation_id,
        urgency=primary.urgency,
        severity=primary.severity,
        reason=primary.reason,
        source_agent_id=primary.source_agent_id,
        parent_agent_id=primary.parent_agent_id,
        root_task_id=primary.root_task_id,
        summary=f"{len(signals)} structured events share this task and reason.",
        evidence_refs=evidence_refs,
        created_at=primary.created_at,
        handled_at=primary.handled_at,
        status=primary.status,
        dedupe_key=primary.dedupe_key,
        metadata=metadata,
    )


def _batched_audit_finding_signal(signals: tuple[WakeSignal, ...]) -> WakeSignal:
    primary = signals[0]
    evidence_refs = tuple(
        dict.fromkeys(ref for signal in signals for ref in signal.evidence_refs if ref)
    )
    delivery_evidence_refs = tuple(
        dict.fromkeys(
            ref for signal in signals for ref in _audit_finding_signal_delivery_refs(signal) if ref
        )
    )
    finding_rows = [
        {
            "wake_signal_id": signal.wake_signal_id,
            "summary": signal.summary,
            "evidence_refs": list(signal.evidence_refs),
            "delivery_evidence_refs": list(_audit_finding_signal_delivery_refs(signal)),
            "source_agent_id": signal.source_agent_id,
            "metadata": dict(signal.metadata or {}),
            "created_at": signal.created_at,
        }
        for signal in signals
    ]
    metadata = dict(primary.metadata or {})
    metadata["report_scope"] = "incremental"
    metadata["delivery_evidence_refs"] = list(delivery_evidence_refs)
    if len(signals) > 1:
        metadata.update(
            {
                "finding_id": f"batch:{primary.wake_signal_id}",
                "finding_count": len(signals),
                "batched_wake_signal_ids": [signal.wake_signal_id for signal in signals],
                "findings": finding_rows,
            }
        )
    return WakeSignal(
        wake_signal_id=primary.wake_signal_id,
        thread_id=primary.thread_id,
        observation_id=primary.observation_id,
        urgency=primary.urgency,
        severity=primary.severity,
        reason=primary.reason,
        source_agent_id=primary.source_agent_id,
        parent_agent_id=primary.parent_agent_id,
        root_task_id=primary.root_task_id,
        summary=(
            f"{len(signals)} 个同一 Audit 的待汇报发现。" if len(signals) > 1 else primary.summary
        ),
        evidence_refs=evidence_refs,
        created_at=primary.created_at,
        handled_at=primary.handled_at,
        status=primary.status,
        dedupe_key=primary.dedupe_key,
        metadata=metadata,
    )


# LLM: 合批只读事件创建时间与配置，不能用同树活跃孩子阻挡已完成结果。
#   就绪扫描与消费必须共用本入口；确认仍由实际采样信封的耐久回执负责。
# 函数用途: 短暂合并连续完成通知，到期即可交父级，不等最慢的兄弟。
def _successful_completion_waiting_for_batch(
    scheduler: BackgroundMainAgentScheduler,
    signal: WakeSignal,
    current: float,
) -> bool:
    if not _successful_completion_signal(signal):
        return False
    delay = scheduler._config_limit("background_completion_coalesce_seconds")
    created_at = float(signal.created_at or 0.0)
    return delay > 0 and 0 < created_at <= current < created_at + delay


# LLM: Successful completion batching reads only reason, typed status and the
# Audit worker marker; natural-language child output never enters this decision.
# 函数用途: 判断一条 wake 是否是可与同树兄弟合并的普通成功完成事件。
def _successful_completion_signal(signal: WakeSignal) -> bool:
    if str(signal.reason or "").strip().lower() != "subagent_runner_finished":
        return False
    metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
    return (
        str(metadata.get("status") or "").strip().upper() == "DONE"
        and metadata.get("audit_source_worker") is not True
    )


# LLM: 这里只比较事件身份和终态类型；能否随本轮一起确认还必须由调用方检查采样时间边界。
# 函数用途: 判断两条完成通知是否属于同一会话、同一根任务的成功完成批次。
def _same_successful_completion_batch(primary: WakeSignal, sibling: WakeSignal) -> bool:
    """Match DONE notices from the same task tree without deciding freshness."""

    if primary.wake_signal_id == sibling.wake_signal_id:
        return False
    if primary.thread_id != sibling.thread_id or primary.root_task_id != sibling.root_task_id:
        return False
    if str(primary.reason or "").strip().lower() != "subagent_runner_finished":
        return False
    if str(sibling.reason or "").strip().lower() != "subagent_runner_finished":
        return False
    return _successful_completion_signal(primary) and _successful_completion_signal(sibling)


def _observation_batch_semantics(
    observations: list[ObservationEvent],
) -> tuple[str, dict[str, object] | None]:
    event_types = {str(item.event_type or "").strip() for item in observations}
    if len(event_types) != 1:
        return "observation_requires_main_agent", None
    reason = next(iter(event_types))
    if reason not in SUBAGENT_LIFECYCLE_WAKE_REASONS:
        return "observation_requires_main_agent", None
    statuses = [
        str((item.metadata or {}).get("status") or "").strip().upper() for item in observations
    ]
    status = (
        "DONE"
        if statuses and all(item == "DONE" for item in statuses)
        else next(
            (item for item in statuses if item and item != "DONE"),
            "",
        )
    )
    return reason, {
        "kind": "observation_fallback",
        "reason": reason,
        "root_task_id": first_root_task_id(observations),
        "source_agent_ids": [item.source_agent_id for item in observations if item.source_agent_id],
        "observation_ids": [item.observation_id for item in observations],
        "metadata": {
            "status": status,
            "task_ids": [
                str((item.metadata or {}).get("task_id") or "")
                for item in observations
                if str((item.metadata or {}).get("task_id") or "")
            ],
        },
    }


def _wake_signal_root_is_inactive(store: object, signal: WakeSignal) -> bool:
    root_task_id = str(getattr(signal, "root_task_id", "") or "").strip()
    if not root_task_id:
        return False
    try:
        links, _load_errors = store.task_links_report(signal.thread_id)
    except Exception:
        return False
    return any(
        str(getattr(link, "task_id", "") or "") == root_task_id
        and str(getattr(link, "status", "") or "").strip().lower() != "active"
        for link in links
    )


# LLM: observation 必须先按 task 隔离再计算 runnable limit；unknown task 的
# 事件原样保留；已提交回复由 canonical 消息流显示，调度器不重复写正文或确认其它 task。
# 函数用途: 批量消费没有可用 wake 的观察事件并登记调度结果，不另建回复账本。
def _consume_observation_batches(
    scheduler: BackgroundMainAgentScheduler,
    reports: list[BackgroundMainAgentReport],
    reported: set[str],
    current: float,
    *,
    target_thread_id: str = "",
) -> None:
    observation_limit = scheduler._config_limit("conversation_unhandled_observation_limit")
    pending_observations = scheduler.store.unhandled_observations_requiring_main(limit=0)
    pending_observations = [
        observation
        for observation in pending_observations
        if not _observation_waits_for_linked_wake(scheduler.store, observation)
    ]
    admitted = 0
    for (thread_id, task_id), thread_observations in observations_by_thread_and_task(
        pending_observations
    ).items():
        if target_thread_id and thread_id != target_thread_id:
            continue
        if observation_limit > 0 and admitted >= observation_limit:
            break
        if thread_id in reported:
            continue
        if _background_authority_recovery_block(scheduler, task_id) is not None:
            continue
        selected = thread_observations
        if observation_limit > 0:
            selected = selected[: observation_limit - admitted]
        admitted += len(selected)
        report = _consume_with_supply_guard(
            scheduler._supply_backoff,
            thread_id,
            current,
            partial(scheduler._run_observation_batch, thread_id, selected, now=current),
        )
        if report is not None:
            reports.append(report)
            reported.add(report.thread_id)


def _observation_waits_for_linked_wake(
    store: object,
    observation: ObservationEvent,
) -> bool:
    """Keep one event on its durable wake lane until delivery succeeds.

    An observation is the fallback only when its paired wake is absent. If a
    transient channel failure leaves the wake pending, running the observation
    batch in parallel creates a second model turn, duplicates the report and
    can mark the observation handled before durable delivery succeeds.
    """

    wake_signal_id = str(getattr(observation, "wake_signal_id", "") or "").strip()
    if not wake_signal_id:
        return False
    loader = getattr(store, "pending_wake_signal", None)
    if not callable(loader):
        return True
    try:
        return loader(wake_signal_id) is not None
    except Exception:
        # A temporarily unreadable wake ledger is not permission to fork a
        # second delivery path. Leave the observation pending for a later tick.
        return True


# LLM: policy 的 unknown 权威阻塞只暂停调度，不退休、不顺延；显式恢复后
# 原排期自然重新获得准入。回复提交留在 runtime，策略调度不复制最终正文。
# 函数用途: 消费本轮到期且拥有执行权的进度策略，收集结果供上层观察。
def _consume_due_policies(
    scheduler: BackgroundMainAgentScheduler,
    reports: list[BackgroundMainAgentReport],
    reported: set[str],
    current: float,
    *,
    target_thread_id: str = "",
) -> None:
    enabled, load_errors = scheduler.store.list_progress_policies_report(enabled_only=True)
    scheduler.last_progress_policy_load_errors = load_errors
    scheduler.last_progress_policy_suppressed = []
    policies = [
        policy
        for policy in enabled
        if policy.next_due_at <= current
        and (not target_thread_id or policy.thread_id == target_thread_id)
    ]
    agent = getattr(scheduler.runtime, "agent", None)
    runnable, suppressed = _runnable_due_policies(
        scheduler.store, policies, now=current, agent=agent
    )
    scheduler.last_progress_policy_suppressed = _snooze_suppressed_policies(
        scheduler.store, suppressed, now=current, agent=agent
    )
    for policy in runnable:
        if policy.thread_id in reported:
            continue
        recovery_block = _background_authority_recovery_block(scheduler, policy.task_id)
        if recovery_block is not None:
            _record_recovery_blocked_policy(scheduler, policy, recovery_block)
            continue
        report = _consume_with_supply_guard(
            scheduler._supply_backoff,
            policy.thread_id,
            current,
            partial(scheduler._run_due_policy, policy, now=current),
        )
        if report:
            reports.append(report)


# LLM: Ready-thread discovery may inspect durable sources but must not claim, consume, or run them;
# the per-thread execution lane remains authoritative for every state transition.
# 函数用途: 按持久 wake、observation 和到期 policy 找出需要后台续跑的会话。
def _ready_background_thread_ids(
    scheduler: BackgroundMainAgentScheduler,
    *,
    current: float,
    limit: int = 0,
) -> tuple[str, ...]:
    ready: list[str] = []
    seen: set[str] = set()

    def append(thread_id: object, task_id: object = "") -> None:
        normalized = str(thread_id or "").strip()
        if not normalized or normalized in seen:
            return
        if limit > 0 and len(ready) >= limit:
            return
        if not scheduler._supply_backoff.should_attempt(normalized, current):
            return
        if (
            _background_authority_recovery_block(
                scheduler,
                str(task_id or "").strip(),
            )
            is not None
        ):
            return
        seen.add(normalized)
        ready.append(normalized)

    for signal in scheduler.store.pending_wake_signals(limit=0):
        if current < scheduler._wake_retry_after.get(signal.wake_signal_id, 0.0):
            continue
        if _successful_completion_waiting_for_batch(scheduler, signal, current):
            continue
        append(signal.thread_id, signal.root_task_id)
    for observation in scheduler.store.unhandled_observations_requiring_main(limit=0):
        if _observation_waits_for_linked_wake(scheduler.store, observation):
            continue
        append(observation.thread_id, observation.root_task_id)
    enabled, _load_errors = scheduler.store.list_progress_policies_report(enabled_only=True)
    due = [policy for policy in enabled if policy.next_due_at <= current]
    runnable, _suppressed = _runnable_due_policies(
        scheduler.store,
        due,
        now=current,
        agent=getattr(scheduler.runtime, "agent", None),
    )
    for policy in runnable:
        append(policy.thread_id, policy.task_id)
    return tuple(ready)


# LLM: This mixin owns the public synchronous tick and Gateway's per-thread
# execution projection; maintenance implementation remains in the tick mixin below.
# 类用途: 把 owner 级后台消费拆成可独立运行的会话车道。
class _BackgroundSchedulerThreadLaneMixin:
    # LLM: Direct/CLI ticks retain synchronous all-thread behavior; Gateway uses
    # prepare_tick + ready_thread_ids + tick_thread to isolate independent sessions.
    # 函数用途: 同步处理当前 owner 的全部就绪后台事件。
    def tick(self, *, now: float | None = None) -> list[BackgroundMainAgentReport]:
        current = now if now is not None else __import__("time").time()
        self.prepare_tick(now=current)
        return self._consume_ready_sources(now=current)

    # LLM: Preparation is model-free and is called only by the supervisor thread.
    # Gateway must disable inline orphan supervision because its independent
    # reconciler owns that potentially slow scan; direct schedulers retain it as
    # a crash-recovery fallback. All other durable enqueue work remains unchanged.
    # 函数用途: 在分会话并发前完成低频维护和到期入队；按宿主形态选择是否同步扫描孤儿。
    def prepare_tick(
        self,
        *,
        now: float | None = None,
        include_orphan_supervision: bool = True,
    ) -> None:
        from .process_events import reconcile_process_completions

        current = now if now is not None else __import__("time").time()
        reconcile_process_completions(self.runtime.agent)
        self._maybe_gc_ledger(now=current)
        self._process_collaboration_cases(now=current)
        if include_orphan_supervision:
            _maybe_supervise_orphans(self, current)
        self._enqueue_scheduler_runs(now=current)
        self._reclaim_orphaned_attempts(now=current)
        self._enqueue_unfinished_task_resume_wakes(now=current)

    # LLM: This read-only plan exposes conversation identities, never model content
    # or a second scheduling authority; durable claims still decide who executes.
    # 函数用途: 给单 Gateway 列出可独立提交的会话车道。
    def ready_thread_ids(
        self,
        *,
        now: float | None = None,
        limit: int = 0,
    ) -> tuple[str, ...]:
        current = now if now is not None else __import__("time").time()
        return _ready_background_thread_ids(self, current=current, limit=max(0, int(limit)))

    # LLM: One worker may consume only one durable thread; its existing run claim
    # serializes foreground, wake, policy, and scheduled continuations for that thread.
    # 函数用途: 处理一个会话的后台事件，不阻塞同 owner 的其它会话。
    def tick_thread(
        self,
        thread_id: str,
        *,
        now: float | None = None,
    ) -> list[BackgroundMainAgentReport]:
        normalized = str(thread_id or "").strip()
        if not normalized:
            return []
        current = now if now is not None else __import__("time").time()
        return self._consume_ready_sources(now=current, target_thread_id=normalized)

    # LLM: Consumption order stays wake -> observation -> policy, while an optional
    # thread filter turns the former owner-wide loop into a 会话运行时 session lane.
    # 函数用途: 按既有优先级消费全部来源，或只消费指定会话。
    def _consume_ready_sources(
        self,
        *,
        now: float,
        target_thread_id: str = "",
    ) -> list[BackgroundMainAgentReport]:
        reports: list[BackgroundMainAgentReport] = []
        reported = _consume_pending_wake_signals(
            self,
            reports,
            now,
            target_thread_id=target_thread_id,
        )
        _consume_observation_batches(
            self,
            reports,
            reported,
            now,
            target_thread_id=target_thread_id,
        )
        _consume_due_policies(
            self,
            reports,
            reported,
            now,
            target_thread_id=target_thread_id,
        )
        return reports


# LLM: Maintenance and recovery helpers stay model-free until a lane consumer
# explicitly calls the wake, observation, or policy execution path.
# 类用途: 管理后台调度的维护、恢复和持久入队。
class _BackgroundSchedulerTickMixin:
    """Tick maintenance and durable scheduling helpers."""

    def _maybe_gc_ledger(self, *, now: float) -> None:
        """低频归档陈旧账本(disabled policy / finished claim),治目录无限累积。
        tick 每 ~2 分钟一次,这里最多 6 小时跑一趟;归档是移动非删除,可回滚。"""
        last = getattr(self, "_ledger_last_gc_at", 0.0)
        if now - last < _LEDGER_GC_INTERVAL_SECONDS:
            return
        self._ledger_last_gc_at = now
        try:
            summary = self.store.gc_stale_ledger_records(now=now)
        except Exception:  # noqa: BLE001 - 归档是增强,失败绝不影响 tick 主流程
            return
        if summary.get("archived_policies") or summary.get("archived_claims"):
            logging.getLogger("agent.conversation.runtime").info(
                "ledger gc archived policies=%s claims=%s",
                summary.get("archived_policies"),
                summary.get("archived_claims"),
            )

    def _process_collaboration_cases(self, *, now: float) -> None:
        if self.collaboration_store is None:
            return
        from ..collaboration import CollaborationCoordinator

        CollaborationCoordinator(
            store=self.collaboration_store, conversation_store=self.store
        ).tick(now=now)

    def _reclaim_orphaned_attempts(self, *, now: float) -> None:
        """R1-03 孤儿 attempt 兜底：锁过期超宽限且 run 非终态 → 按矩阵收口。

        低频（tick ~2min 一次，这里每 5 分钟一趟）——正常 worker 每工具轮
        续租，孤儿 = 崩溃/kill 遗留（kill-9 后旧锁残留，锁过期+持主判死
        才可接管）。reclaim 内部有副作用门（有外部副作用且无 effect_key →
        停手交人工，fail-closed），普通 wake tick 绝不构成绕过。
        """
        last = getattr(self, "_orphan_reclaim_last_at", 0.0)
        if now - last < _ORPHAN_RECLAIM_INTERVAL_SECONDS:
            return
        self._orphan_reclaim_last_at = now
        home = getattr(getattr(self, "runtime", None), "agent", None)
        owner_home = getattr(getattr(home, "home_paths", None), "owner_home_dir", None)
        if not owner_home:
            return
        try:
            from ..owner_wake_discovery import _runtime_repo_for_owner
            from ..runtime_db.operations import RuntimeConflictError

            repo = _runtime_repo_for_owner(Path(owner_home))
            if repo is None:
                return
            for attempt in repo.find_orphaned_attempts(now=now):
                try:
                    result = repo.reclaim_orphaned_attempt(
                        str(attempt["attempt_id"]),
                        operator="wake-tick-orphan-reclaim",
                        reason="orphan_reclaim_tick",
                    )
                    if result.get("reclaimed"):
                        _HEARTBEAT_LOGGER.info(
                            "orphan attempt reclaimed: %s -> %s",
                            attempt["attempt_id"],
                            result.get("status"),
                        )
                except RuntimeConflictError as exc:
                    # 竞态：刚被接管/已 settle——下一趟自然跳过，不重试不刷屏
                    _HEARTBEAT_LOGGER.info("orphan reclaim skipped: %s", exc)
        except Exception:
            _HEARTBEAT_LOGGER.warning("orphan reclaim scan failed", exc_info=True)

    def _enqueue_unfinished_task_resume_wakes(self, *, now: float) -> None:
        """扫描治理(2026-08-17 owner 拍板): 热层消费到期字条 + 低频三源对账。

        热层(每 tick): pop wake_queue 到期行(索引查询, 不翻任务目录)——每行
        先核线程档案(thread_for_task), 档案在 → raise_wake_signal 唤醒; 不在
        → 任务已终结, 清字条不唤醒。唤醒即清行(一次性闹钟; 执行链本身有
        claim/lease/orphan 兜底)。

        低频兜底(每 _WAKE_RECONCILE_INTERVAL_SECONDS): unfinished_task_ids
        三源全量对账一次——未完成任务没字条 → upsert(冷却后到期); 字条在但
        任务已终结 → 清字条(僵尸不累积)。

        WK-INT: 每 tick 先回收 lease 过期的 claimed 字条(崩溃/卡死恢复)。
        """
        self._reclaim_expired_wake_leases(now=now)
        self._consume_due_wake_queue(now=now)
        last = getattr(self, "_wake_reconcile_last_at", 0.0)
        if now - last >= _WAKE_RECONCILE_INTERVAL_SECONDS:
            self._wake_reconcile_last_at = now
            self._reconcile_wake_queue(now=now)

    def _reclaim_expired_wake_leases(self, *, now: float) -> None:
        """WK-INT: 回收 lease 过期的 claimed 字条(崩溃/卡死恢复, 回 pending 重试)。"""
        try:
            agent = getattr(getattr(self, "runtime", None), "agent", None)
            repo = getattr(getattr(agent, "subagents", None), "runtime_db", None)
            if repo is None or not callable(getattr(repo, "reclaim_expired_wakes", None)):
                return
            reclaimed = repo.reclaim_expired_wakes(now=now)
            if reclaimed:
                _HEARTBEAT_LOGGER.info("wake lease reclaim: %s", reclaimed)
        except Exception:
            _HEARTBEAT_LOGGER.warning("wake lease reclaim scan failed", exc_info=True)

    def _consume_due_wake_queue(self, *, now: float) -> None:
        """热层: 到期字条 → 核档案 → 唤醒/结算。

        WK-INT(2026-08-20): 状态机化——claim(带 lease) → 执行 → 成功
        complete / 失败 release(带退避重试)。wake 不再"pop 即没"：
        - 执行抛异常 → release(5 秒退避)回 pending 重试(不丢)
        - 进程崩溃 → lease 过期由 _reclaim_expired_wake_leases 回收重试
        - 429 由调用方以更长 retry_after release(冻结不丢)
        """
        try:
            agent = getattr(getattr(self, "runtime", None), "agent", None)
            repo = getattr(getattr(agent, "subagents", None), "runtime_db", None)
            if repo is None or not callable(getattr(repo, "pop_due_wakes", None)):
                return
            for wake in repo.pop_due_wakes(now=now, limit=64):
                task_id = str(wake.get("root_task_id") or "").strip()
                wake_id = str(wake.get("wake_id") or "").strip()
                if not task_id or not wake_id:
                    continue
                try:
                    thread = self.store.thread_for_task(task_id)
                    if thread is None:
                        # 档案没了(任务已终结/未挂线程) → 清字条, 不唤醒
                        repo.complete_wake(wake_id)
                        continue
                    link_status = self._task_link_status(thread.thread_id, task_id)
                    if link_status in {"terminal", "missing"}:
                        # DESIGN_LEDGER 铁律: 排队的定时/生命周期 wake 在任务
                        # 终态后直接作废, 不能复活任务。
                        repo.complete_wake(wake_id)
                        continue
                    self.store.raise_wake_signal(
                        {
                            "thread_id": thread.thread_id,
                            "urgency": "normal",
                            "reason": "wake_queue_due",
                            "root_task_id": task_id,
                            "source_agent_id": "wake-queue",
                            "summary": task_id,
                            "dedupe_key": f"wake-queue:{task_id}",
                        }
                    )
                    repo.complete_wake(wake_id)
                except Exception:
                    _HEARTBEAT_LOGGER.warning(
                        "wake queue consume failed task=%s", task_id, exc_info=True
                    )
                    # WK-INT: 失败不丢 wake——回 pending 5 秒后重试
                    if callable(getattr(repo, "release_wake", None)):
                        repo.release_wake(
                            wake_id,
                            retry_after=float(now) + 5.0,
                            last_error="consume_failed",
                        )
        except Exception:
            _HEARTBEAT_LOGGER.warning("wake queue consume scan failed", exc_info=True)

    def _reconcile_wake_queue(self, *, now: float) -> None:
        """低频三源对账: 同步"等待者名单"(wake_queue)与任务档案。"""
        try:
            agent = getattr(getattr(self, "runtime", None), "agent", None)
            owner_home = getattr(getattr(agent, "home_paths", None), "owner_home_dir", None)
            repo = getattr(getattr(agent, "subagents", None), "runtime_db", None)
            if not owner_home or repo is None or not callable(getattr(repo, "upsert_wake", None)):
                return
            from ..owner_wake_discovery import unfinished_task_ids

            unfinished = set(unfinished_task_ids(Path(owner_home)))
            pending = repo.list_pending_wakes(limit=1000)
            pending_tasks = {str(row.get("root_task_id") or "") for row in pending}
            # EXEC-39(owner 拍板): 普通任务不自动续跑——字条只给有 active goal
            # 授权的任务补(goal 任务自动续跑合法, 与 _auto_resume_authorized
            # 同源); 普通任务只在模型自己 sleep 时短期待机, 不靠对账自动唤醒。
            # audit 任务另有观察/audit 唤醒通道, goal_tick 会双重拉起主代理
            # (H 批实锤: capacity wake 冻结回复被二次模型调用打破)。
            # 已有 pending 字条的任务不重写到期时间——否则每 5min 对账都会把
            # 闹钟往后推, 永远不响(对账只补缺失, 不续命)。
            for task_id in unfinished:
                if task_id in pending_tasks:
                    continue
                if not self._task_has_active_goal(task_id):
                    continue
                if self._task_work_kind(task_id) == "audit":
                    continue
                repo.upsert_wake(
                    root_task_id=task_id,
                    next_due_at=now + _TASK_RESUME_COOLDOWN_SECONDS,
                    kind="goal_tick",
                )
            for row in pending:
                tid = str(row.get("root_task_id") or "")
                if tid and tid not in unfinished:
                    repo.cancel_wakes_for_task(tid)  # 已终结 → 清字条(僵尸不累积)
        except Exception:
            _HEARTBEAT_LOGGER.warning("wake queue reconcile failed", exc_info=True)

    def _task_has_active_goal(self, task_id: str) -> bool:
        """EXEC-39 同源: 任务是否持有 active goal(自动续跑授权)。"""
        try:
            thread = self.store.thread_for_task(task_id)
            if thread is None:
                return False
            goal = self.store.load_goal(thread.thread_id, task_id=task_id)
            return (
                goal is not None
                and str(getattr(goal, "status", "") or "").strip().lower() == "active"
            )
        except Exception:  # noqa: BLE001 读不到=fail-closed 不自动补字条
            return False

    def _task_work_kind(self, task_id: str) -> str:
        """任务的 work_kind(link 权威); 读不到返回空串(fail-open 补字条)。"""
        try:
            thread = self.store.thread_for_task(task_id)
            if thread is None:
                return ""
            links, _errors = self.store.task_links_report(thread.thread_id)
            for link in links:
                if str(getattr(link, "task_id", "") or "") == task_id:
                    return str(getattr(link, "work_kind", "") or "").strip().lower()
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _task_link_status(self, thread_id: str, task_id: str) -> str:
        """热层唤醒前核 link 状态: active/terminal/missing/unknown。

        terminal(completed/cancelled/interrupted/abandoned/superseded)与
        missing(link 不存在)都作废闹钟——DESIGN_LEDGER 铁律: 排队的定时或
        生命周期 wake 在任务终态后直接作废, 不能复活任务。unknown(账本读
        失败)不在这里作废, 唤醒信号照发, 由消费链 _wake_signal_root_is_
        inactive 再核(fail-open, 读失败不丢闹钟)。"""
        try:
            links, _errors = self.store.task_links_report(thread_id)
        except Exception:  # noqa: BLE001
            return "unknown"
        status = None
        for link in links:
            if str(getattr(link, "task_id", "") or "") == task_id:
                status = str(getattr(link, "status", "") or "").strip().lower()
                break
        if status is None:
            return "missing"
        if status == "active":
            return "active"
        return "terminal"

    def _enqueue_scheduler_runs(self, *, now: float) -> None:
        if self.scheduler_service is None:
            return
        try:
            self.scheduler_service.reconcile_waiting_runs(now=now)
            self.scheduler_service.enqueue_ready_runs(
                now=now,
                limit=self._config_limit("conversation_pending_wake_limit"),
            )
        except Exception:
            _HEARTBEAT_LOGGER.warning("owner scheduler enqueue failed", exc_info=True)


@dataclass(frozen=True)
class _WakeClaimState:
    claim: object | None = None
    heartbeat: object | None = None
    stop: bool = False


@dataclass(frozen=True)
class _WakeExecution:
    report: BackgroundMainAgentReport | None = None
    terminal: bool = False


class _BackgroundSchedulerWakeMixin:
    """Run and durably settle one wake without owning tick orchestration."""

    def _run_wake_signal(
        self, signal: WakeSignal, *, now: float
    ) -> BackgroundMainAgentReport | None:
        if not isinstance(getattr(self, "_quota_fallback_wakes", None), set):
            self._quota_fallback_wakes = set()
        lifecycle_reason = str(getattr(signal, "reason", "") or "").strip()
        reason = lifecycle_reason or (
            "urgent_wake_signal" if signal.urgency == "urgent" else "wake_signal"
        )
        claim_state = _claim_scheduler_wake(self, signal, now=now)
        if claim_state.stop:
            return None
        try:
            execution = _execute_wake_signal(
                self,
                signal,
                lifecycle_reason=lifecycle_reason,
                reason=reason,
                claim=claim_state.claim,
                now=now,
            )
            if execution.terminal:
                return None
            report = execution.report
        except Exception as exc:
            if is_provider_quota_exhausted_error(exc):
                report = _quota_wake_report_after_error(
                    self,
                    signal,
                    lifecycle_reason=lifecycle_reason,
                )
            else:
                _handle_nonquota_wake_error(
                    self,
                    signal,
                    lifecycle_reason=lifecycle_reason,
                    claim=claim_state.claim,
                    error=exc,
                )
                raise
        finally:
            if claim_state.heartbeat is not None:
                claim_state.heartbeat.stop()
        report = _finish_scheduler_wake_claim(
            self,
            signal,
            report,
            claim=claim_state.claim,
            now=now,
        )
        return _complete_wake_report(
            self,
            signal,
            report,
            lifecycle_reason=lifecycle_reason,
            now=now,
        )


def _claim_scheduler_wake(
    scheduler: BackgroundMainAgentScheduler,
    signal: WakeSignal,
    *,
    now: float,
) -> _WakeClaimState:
    if not _is_scheduler_wake_signal(signal):
        return _WakeClaimState()
    if scheduler.scheduler_service is None:
        return _WakeClaimState(stop=True)
    result = scheduler.scheduler_service.claim_wake(
        signal,
        lease_seconds=scheduler.claim_ttl_seconds,
        now=now,
    )
    if result.status == "stale":
        scheduler.store.mark_wake_signal_handled(signal.wake_signal_id, now=now)
        return _WakeClaimState(stop=True)
    if result.status != "claimed" or result.claim is None:
        return _WakeClaimState(stop=True)
    heartbeat = scheduler.scheduler_service.heartbeat(
        result.claim,
        lease_seconds=scheduler.claim_ttl_seconds,
    )
    heartbeat.start()
    return _WakeClaimState(claim=result.claim, heartbeat=heartbeat)


def _execute_wake_signal(
    scheduler: BackgroundMainAgentScheduler,
    signal: WakeSignal,
    *,
    lifecycle_reason: str,
    reason: str,
    claim: object | None,
    now: float,
) -> _WakeExecution:
    if _wake_signal_should_skip(
        scheduler.runtime.agent,
        scheduler.store,
        signal,
        lifecycle_reason,
    ):
        if claim is not None:
            scheduler.scheduler_service.release(claim, now=now)
        scheduler.store.mark_wake_signal_handled(signal.wake_signal_id, now=now)
        return _WakeExecution(terminal=True)
    if _is_legacy_per_source_audit_capacity_signal(signal):
        scheduler.store.mark_wake_signal_handled(signal.wake_signal_id, now=now)
        return _WakeExecution(terminal=True)
    if signal.wake_signal_id in scheduler._quota_fallback_wakes:
        return _WakeExecution(_provider_quota_fallback_report(scheduler, signal, now=now))
    scheduler._pre_wake_capability_sweep(lifecycle_reason, signal)
    cached = _cached_owner_delivery(signal)
    if cached is not None:
        channel, target = scheduler._observation_route(signal.thread_id)
        return _WakeExecution(
            scheduler.runtime.redeliver_cached_wake(
                signal,
                channel=channel,
                target=target,
                now=now,
            )
        )
    if _is_internal_audit_source_worker_lifecycle_signal(
        signal,
        scheduler.runtime.agent,
    ):
        from .task_promotion import complete_named_audit_task_if_settled

        complete_named_audit_task_if_settled(
            scheduler.runtime.agent,
            str(signal.root_task_id or "").strip(),
        )
        scheduler.store.mark_wake_signal_handled(signal.wake_signal_id, now=now)
        return _WakeExecution(terminal=True)
    channel, target = scheduler._observation_route(signal.thread_id)
    _HEARTBEAT_LOGGER.info(
        "WAKE_SIGNAL_RUN thread=%s reason=%s route_channel=%s route_target=%s",
        signal.thread_id,
        reason,
        channel,
        target,
    )
    return _WakeExecution(
        scheduler._run_claimed(
            {
                "thread_id": signal.thread_id,
                "task_id": signal.root_task_id,
                "reason": reason,
                "route_channel": channel,
                "route_target": target,
                "now": now,
                "wake_signal": signal,
            }
        )
    )


def _quota_wake_report_after_error(
    scheduler: BackgroundMainAgentScheduler,
    signal: WakeSignal,
    *,
    lifecycle_reason: str,
) -> BackgroundMainAgentReport:
    if lifecycle_reason == "thread_goal_continue":
        scheduler._stop_thread_goal_after_error(signal, status="usage_limited")
    report = _provider_quota_fallback_report(scheduler, signal, now=time.time())
    if report.wake_handled:
        scheduler._quota_fallback_wakes.discard(signal.wake_signal_id)
    else:
        scheduler._quota_fallback_wakes.add(signal.wake_signal_id)
    return report


def _handle_nonquota_wake_error(
    scheduler: BackgroundMainAgentScheduler,
    signal: WakeSignal,
    *,
    lifecycle_reason: str,
    claim: object | None,
    error: BaseException,
) -> None:
    observed_at = time.time()
    if claim is not None:
        if is_provider_transient_error(error):
            scheduler.scheduler_service.release(claim, now=observed_at)
        else:
            terminal = scheduler.scheduler_service.finish(
                claim,
                status="failed",
                error_code=type(error).__name__.upper(),
                error_message=compact_error_message(error),
                now=observed_at,
            )
            if terminal is not None:
                scheduler.store.mark_wake_signal_handled(
                    signal.wake_signal_id,
                    now=observed_at,
                )
    if lifecycle_reason != "thread_goal_continue":
        return
    status = "usage_limited" if is_provider_usage_limit_error(error) else "blocked"
    scheduler._stop_thread_goal_after_error(signal, status=status)
    scheduler.store.mark_wake_signal_handled(signal.wake_signal_id, now=observed_at)


def _finish_scheduler_wake_claim(
    scheduler: BackgroundMainAgentScheduler,
    signal: WakeSignal,
    report: BackgroundMainAgentReport | None,
    *,
    claim: object | None,
    now: float,
) -> BackgroundMainAgentReport | None:
    if claim is None:
        return report
    if report is None:
        scheduler.scheduler_service.release(claim, now=time.time())
        return None
    if not report.wake_handled:
        scheduler.scheduler_service.release(claim, now=time.time())
        scheduler._wake_retry_after[signal.wake_signal_id] = now + 30.0
        return None
    if str(report.task_status or "").strip().lower() == "active":
        waiting = scheduler.scheduler_service.park_waiting(
            claim,
            response=report.response,
            delivery_status=report.delivery_status,
            delivery_reason=report.delivery_reason,
            now=time.time(),
        )
        return report if waiting is not None else None
    terminal = scheduler.scheduler_service.finish(
        claim,
        status="done",
        response=report.response,
        delivery_status=report.delivery_status,
        delivery_reason=report.delivery_reason,
        now=time.time(),
    )
    return report if terminal is not None else None


def _complete_wake_report(
    scheduler: BackgroundMainAgentScheduler,
    signal: WakeSignal,
    report: BackgroundMainAgentReport | None,
    *,
    lifecycle_reason: str,
    now: float,
) -> BackgroundMainAgentReport | None:
    if report is None:
        return None
    if not report.wake_handled:
        scheduler._wake_retry_after[signal.wake_signal_id] = now + 30.0
        return None
    scheduler._quota_fallback_wakes.discard(signal.wake_signal_id)
    scheduler._wake_retry_after.pop(signal.wake_signal_id, None)
    scheduler.store.mark_wake_signal_handled(signal.wake_signal_id, now=now)
    if lifecycle_reason in {"audit_finding", "audit_capacity_alert"}:
        from .task_promotion import complete_named_audit_task_if_settled

        complete_named_audit_task_if_settled(
            scheduler.runtime.agent,
            str(signal.root_task_id or "").strip(),
        )
    if lifecycle_reason == "thread_goal_continue":
        scheduler._continue_thread_goal(signal, report=report, now=now)
    if lifecycle_reason == "subagent_runner_finished" and report.goal_continuation_allowed:
        _ensure_goal_progress_wake_chain(scheduler, signal, now=now)
    if scheduler.scheduler_service is not None:
        scheduler.scheduler_service.reconcile_waiting_run(
            str(signal.root_task_id or ""),
            now=now,
        )
    return report


def _provider_quota_fallback_report(
    scheduler: BackgroundMainAgentScheduler,
    signal: WakeSignal,
    *,
    now: float,
) -> BackgroundMainAgentReport:
    """Deliver one model-independent quota notice through the normal channel."""
    metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
    is_audit = _is_audit_source_worker_quota_lifecycle_metadata(
        reason=str(signal.reason or ""),
        root_task_id=str(signal.root_task_id or ""),
        metadata=metadata,
    )
    work_name = _quota_audit_work_name(scheduler, signal) if is_audit else ""
    subject = f"Audit“{work_name}”" if work_name else ("当前 Audit" if is_audit else "当前后台任务")
    content = (
        f"{subject}因模型供应商额度耗尽已经暂停。已接收的数据、游标、已完成结果和待处理队列都已保留，"
        "系统不会继续使用同一额度配置反复重试。请由管理员恢复额度或切换到可用模型后，再显式恢复运行。"
    )
    channel, target = scheduler._observation_route(signal.thread_id)
    supports_proactive = _channel_supports_proactive(scheduler, channel, target)
    delivery_status, wake_handled = _deliver_quota_fallback(
        scheduler,
        signal,
        metadata,
        content=content,
        channel=channel,
        target=target,
        supports_proactive=supports_proactive,
    )
    if wake_handled:
        _record_quota_fallback_message(scheduler, signal, content, channel)
    return BackgroundMainAgentReport(
        thread_id=signal.thread_id,
        task_id=signal.root_task_id,
        reason="provider_quota_exhausted",
        response=content,
        route_channel=channel,
        route_target=target,
        created_at=now,
        delivery_status=delivery_status,
        delivery_reason="provider_quota_system_fallback",
        wake_handled=wake_handled,
    )


def _quota_audit_work_name(
    scheduler: BackgroundMainAgentScheduler,
    signal: WakeSignal,
) -> str:
    try:
        link = scheduler.store.load_task_link(str(signal.root_task_id or ""))
    except Exception:
        link = None
    return str(getattr(link, "work_name", "") or "").strip()


def _channel_supports_proactive(
    scheduler: BackgroundMainAgentScheduler,
    channel: str,
    target: str,
) -> bool:
    try:
        return bool(target and scheduler.runtime.channels.supports_proactive(channel))
    except Exception:
        return False


def _deliver_quota_fallback(
    scheduler: BackgroundMainAgentScheduler,
    signal: WakeSignal,
    metadata: dict[str, Any],
    *,
    content: str,
    channel: str,
    target: str,
    supports_proactive: bool,
) -> tuple[str, bool]:
    if not supports_proactive:
        return "recorded", True
    receipt = scheduler.runtime.channels.deliver(
        DeliveryContext(
            channel=channel,
            target=target,
            mode="proactive",
            thread_id=signal.thread_id,
            task_id=signal.root_task_id,
            idempotency_key=f"provider-quota:{signal.wake_signal_id}",
        ),
        ReplyEnvelope(
            content=content,
            evidence_refs=tuple(
                item
                for item in (
                    str(metadata.get("task_id") or "").strip(),
                    str(metadata.get("watch_id") or "").strip(),
                )
                if item
            ),
        ),
    )
    status = str(getattr(receipt, "delivery_status", "") or "failed")
    return status, status == "sent"


def _record_quota_fallback_message(
    scheduler: BackgroundMainAgentScheduler,
    signal: WakeSignal,
    content: str,
    channel: str,
) -> None:
    scheduler.store.append_message(
        {
            "thread_id": signal.thread_id,
            "role": "assistant",
            "content": content,
            "channel": channel,
            "metadata": {
                "reason": "provider_quota_exhausted_fallback",
                "system_fallback": True,
                "failure_type": "provider_quota_exhausted",
                "wake_signal_id": signal.wake_signal_id,
                "task_id": signal.root_task_id,
            },
        }
    )


class _BackgroundSchedulerGoalMixin:
    """Goal continuation, lifecycle preprocessing, and owner delivery routing."""

    # LLM: Turn errors and typed provider usage limits are system-owned goal stops, matching 会话运行时.
    # 函数用途: 目标后台轮异常时原子停住同一目标与任务，等待用户恢复。
    def _stop_thread_goal_after_error(self, signal: WakeSignal, *, status: str) -> None:
        metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
        with self.store.goal_transition_guard(signal.thread_id):
            goal = self.store.load_goal(
                signal.thread_id,
                goal_id=str(metadata.get("goal_id") or "").strip(),
            )
            if (
                goal is None
                or goal.status != "active"
                or goal.goal_id != str(metadata.get("goal_id") or "")
                or goal.task_id != str(signal.root_task_id or "")
            ):
                return
            elapsed = self.store.take_goal_elapsed_seconds(goal)
            if elapsed:
                goal = (
                    self.store.account_goal_usage(
                        {
                            "thread_id": goal.thread_id,
                            "goal_id": goal.goal_id,
                            "time_delta_seconds": elapsed,
                            "mode": "active_only",
                        }
                    )
                    or goal
                )
            updated = self.store.update_goal(
                {
                    "thread_id": goal.thread_id,
                    "goal_id": goal.goal_id,
                    "status": status,
                    "expected_status": "active",
                }
            )
            if updated is None:
                return
            self.store.update_task_status({"task_id": goal.task_id, "status": "interrupted"})
            registry = getattr(
                getattr(getattr(self.runtime, "agent", None), "local_store", None),
                "task_registry",
                None,
            )
            if registry is not None:
                registry.register_task(goal.task_id, status="blocked", goal=updated.objective)

    # LLM: 精确 Goal 状态决定续跑；工具计数只是统计，不能成为运行授权。子代理等待和任务中断仍优先。
    # 函数用途: 持续目标一轮结束后同步终态，仍可运行则发布一个去重续跑事件，不依赖 Todo 或工具次数。
    def _continue_thread_goal(
        self,
        signal: WakeSignal,
        *,
        report: BackgroundMainAgentReport,
        now: float,
    ) -> None:
        """Reconcile one goal turn and enqueue exactly one next turn while active."""
        if not report.goal_continuation_allowed:
            return
        try:
            metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
            goal = self.store.load_goal(
                signal.thread_id,
                goal_id=str(metadata.get("goal_id") or "").strip(),
            )
            if (
                goal is None
                or goal.goal_id != str(metadata.get("goal_id") or "")
                or goal.task_id != str(signal.root_task_id or "")
            ):
                return
            task_status = _background_task_link_status(
                self.runtime.agent,
                BackgroundRunRequest(
                    thread_id=signal.thread_id,
                    task_id=goal.task_id,
                    reason="thread_goal_continue",
                ),
                store=self.store,
            )
            if goal.status != "active":
                if goal.status in {"blocked", "usage_limited", "budget_limited"}:
                    self.store.update_task_status(
                        {"task_id": goal.task_id, "status": "interrupted"}
                    )
                return
            if task_status != "active":
                return
            subagent_phase, state_error = _goal_subagent_phase(
                self.runtime.agent,
                goal.task_id,
            )
            if subagent_phase == "subagents_active" or state_error:
                # Child lifecycle events are the continuation authority while
                # related work is active. Do not create a polling wake loop.
                return
            from .goal_runtime import raise_goal_continuation_wake

            raise_goal_continuation_wake(
                self.store,
                goal,
                channel=str(metadata.get("channel") or ""),
                conversation_id=str(metadata.get("conversation_id") or ""),
                now=now,
            )
        except Exception:
            _HEARTBEAT_LOGGER.warning("thread goal continuation failed", exc_info=True)

    # LLM: 子代理生命周期唤醒进 LLM 整合轮之前的机制层预处理(§5.1 头号靶的 wake 端半边):
    #   常规能力申请自动批 + BLOCKED/孤儿候选全量续派,全部确定性动作,不依赖模型调
    #   resolve_capability_requests / 系统自动恢复。失败静默记日志,唤醒轮照常进行。
    def _pre_wake_capability_sweep(self, lifecycle_reason: str, signal: WakeSignal) -> None:
        from ..agent_core.orchestration.dispatch.capability_auto_sweep import (
            auto_capability_sweep,
            sweep_applies_to_reason,
        )

        if not sweep_applies_to_reason(lifecycle_reason):
            return
        try:
            auto_capability_sweep(self.runtime.agent, signal)
        except Exception:
            _HEARTBEAT_LOGGER.warning("pre-wake capability sweep failed", exc_info=True)

    def _run_observation_batch(
        self, thread_id: str, observations: list[ObservationEvent], *, now: float
    ) -> BackgroundMainAgentReport | None:
        # 真机根 bug:此路原来不传 route_channel/route_target → BackgroundRunRequest 默认
        # route_channel="internal" → 主代理被真事件叫回后即便自然汇报,也只发到 internal、到不了用户
        # 通道。对比 _run_due_policy 是带 route 的。修:从线程 channel binding 取真实投递路由传进去,
        # 让原生"叫回→主代理自然汇报"直达 owner 通道；内部 findings 账不直接出站。
        route_channel, route_target = self._observation_route(thread_id)
        # 结构化探针:真机确认原生"叫回→上报"是否触发 + 路由落在哪个通道(只读日志即可核实,
        # 不必反复重启验证)。route_channel!=internal 即证明第3/5层路由修复生效、报告直达 owner。
        _HEARTBEAT_LOGGER.info(
            "OBSERVATION_BATCH_RUN thread=%s obs=%d route_channel=%s route_target=%s",
            thread_id,
            len(observations),
            route_channel,
            route_target,
        )
        reason, wake_signal = _observation_batch_semantics(observations)
        report = self._run_claimed(
            {
                "thread_id": thread_id,
                "task_id": first_root_task_id(observations),
                "reason": reason,
                "route_channel": route_channel,
                "route_target": route_target,
                "now": now,
                "wake_signal": wake_signal,
            }
        )
        if report is not None:
            self.store.mark_observations_handled(
                [item.observation_id for item in observations], now=now
            )
        return report

    def _observation_route(self, thread_id: str) -> tuple[str, str]:
        """真事件上报要直达 owner 通道。取路由三档优先级(由最具体到最兜底):
        ① 观察所在【线程自带的真实外呼 binding】(PROACTIVE_PUSH_CHANNELS,如 feishu)——最具体,
           直取其 channel_user_id(飞书=open_id,适配器 receive_id_type=open_id)。
        ② 无外呼 binding 时(真机第3层实锤:urgent 观察常挂【子代理线程】bg-main-thread,无
           channel binding,按线程取会回落 internal 发不出去)→ 按 owner-scoped agent 的 owner 身份取
           (飞书/open_id):owner 身份就是那个飞书用户。open_id 优先从 owner home 路径解析(不依赖属性
           是否设置),再退 home_paths 属性。**owner 身份必须是真外呼通道(PROACTIVE_PUSH_CHANNELS)才用**
           ——单租户 owner_provider="local" 不是外呼通道,不能拿它当路由(否则绕过 internal 投递、发不出)。
        ③ 都取不到 → 回落线程 binding(单机 internal 绑定)或 (internal, "")。
        ⚠️ ①在②之前:owner_id 是 provider 的 user_id,生产环境恰等于 open_id,但概念上不等于线程
           binding 的 channel_user_id;②排前面会让已绑定线程错发到 owner_id 而非 open_id(实锤)。"""
        try:
            thread = self.store.load_thread(thread_id)
        except Exception:
            thread = None
        bindings = list(getattr(thread, "channel_bindings", ()) or ())
        # ① 线程自带真实外呼 binding(最具体)→ 直取 channel_user_id(飞书 open_id)
        for binding in reversed(bindings):
            channel = str(getattr(binding, "channel", "") or "")
            target = str(
                getattr(binding, "channel_user_id", "")
                or getattr(binding, "channel_conversation_id", "")
                or ""
            )
            if channel in PROACTIVE_PUSH_CHANNELS and target:
                return channel, target
        # ② 无外呼 binding(子代理线程)→ owner 身份(仅真外呼通道;"local"/"internal" 不算)
        provider, open_id = self._owner_from_home_path()
        if provider in PROACTIVE_PUSH_CHANNELS and open_id:
            return provider, open_id
        home = getattr(getattr(getattr(self, "runtime", None), "agent", None), "home_paths", None)
        owner_channel = str(getattr(home, "owner_provider", "") or "").strip()
        owner_id = str(getattr(home, "owner_id", "") or "").strip()
        if owner_channel in PROACTIVE_PUSH_CHANNELS and owner_id:
            return owner_channel, owner_id
        # ③ 回落线程 binding(单机 internal 绑定)或 internal
        if bindings:
            binding = bindings[-1]
            channel = str(getattr(binding, "channel", "") or "internal")
            target = str(
                getattr(binding, "channel_user_id", "")
                or getattr(binding, "channel_conversation_id", "")
                or ""
            )
            return channel, target or default_route_target(thread, channel)
        return "internal", ""

    def _owner_from_home_path(self) -> tuple[str, str]:
        """从 owner home 路径解析 (provider, open_id):.my-agent/owners/providers/<provider>/users/<id>。
        scoped scheduler 的 store/agent 根落在 owner home 子树,据此取投递路由最稳(不依赖属性是否设置)。"""
        import re

        sources = [
            getattr(getattr(getattr(self, "runtime", None), "agent", None), "home_paths", None),
            getattr(self, "store", None),
        ]
        for src in sources:
            for attr in ("owner_home", "owner_home_dir", "root"):
                text = str(getattr(src, attr, "") or "")
                match = re.search(r"owners/providers/([^/]+)/(?:users|groups)/([^/]+)", text)
                if match:
                    return match.group(1), match.group(2)
        return "", ""


def _background_claim_scope_id(
    store: object,
    thread_id: str,
    task_id: str,
) -> str:
    """Use a task lane only when the exact durable link declares detachment."""
    selected_thread = str(thread_id or "").strip()
    selected_task = str(task_id or "").strip()
    if not selected_thread or not selected_task:
        return ""
    try:
        if callable(getattr(store, "task_links_report", None)):
            links, errors = store.task_links_report(selected_thread)
            if errors:
                return ""
        else:
            links = store.task_links(selected_thread)
    except Exception:
        return ""
    link = next(
        (
            item
            for item in links
            if str(getattr(item, "task_id", "") or "").strip() == selected_task
        ),
        None,
    )
    if (
        link is None
        or str(getattr(link, "cancellation_scope", "") or "").strip().lower() != "detached"
    ):
        return ""
    from .run_claim import detached_task_claim_scope_id

    return detached_task_claim_scope_id(selected_thread, selected_task)


# LLM: BackgroundMainAgentRuntime is the root conversation executor. Exact ids
# that resolve in the canonical SubAgentManager belong to their child thread
# and runner, even when a stale policy/wake incorrectly points at the root
# conversation. Do not infer this boundary from id prefixes or prose.
# 函数用途: 判断某个任务是否由子代理 runner 独占续跑，防止主代理权限串进 child 工作区。
def _subagent_runner_owns_task(agent: object | None, task_id: object) -> bool:
    selected = str(task_id or "").strip()
    manager = getattr(agent, "subagents", None)
    if not selected or manager is None or not callable(getattr(manager, "load", None)):
        return False
    try:
        task = manager.load(selected)
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return False
    return str(getattr(task, "id", "") or "").strip() == selected


# LLM: This model-free acknowledgement is the final fail-closed guard for any
# child-bound source that bypassed source-specific filtering. It lets wake and
# observation ledgers settle without invoking the root model or exposing root
# tools; the child runner/supervisor remains the sole continuation authority.
# 函数用途: 把误投给主代理的子代理后台来源标成已由 child runner 接管，不消耗模型调用。
def _subagent_owned_background_report(kwargs: dict) -> BackgroundMainAgentReport:
    return BackgroundMainAgentReport(
        thread_id=str(kwargs.get("thread_id") or ""),
        task_id=str(kwargs.get("task_id") or ""),
        reason=str(kwargs.get("reason") or "subagent_runner_owned"),
        response="",
        route_channel=str(kwargs.get("route_channel") or "internal"),
        route_target=str(kwargs.get("route_target") or ""),
        created_at=now(kwargs.get("now")),
        delivery_status="suppressed",
        delivery_reason="subagent_runner_owns_continuation",
        wake_handled=True,
    )


class _BackgroundSchedulerExecutionMixin:
    """Claimed progress-policy execution, heartbeats, and runtime facts."""

    # LLM: 旧目标周期策略一次迁移为 canonical Goal wake；普通用户定时策略保留原调度语义。
    # 函数用途: 运行到期策略，旧 Goal 轮询只迁移事件而不直接再调用模型。
    def _run_due_policy(
        self, policy: ProgressPolicy, *, now: float
    ) -> BackgroundMainAgentReport | None:
        metadata = policy.metadata if isinstance(policy.metadata, dict) else {}
        if metadata.get("tool") == "goal_progress_continuation" and metadata.get("kind") == "subagent_progress_watch":
            ensure_goal_progress_continuation(
                self.runtime.agent, task_id=policy.task_id, thread_id=policy.thread_id, store=self.store, now=now,
            )
            self.store.disable_progress_policy(policy.policy_id)
            return None
        if str(metadata.get("kind") or "") == "subagent_progress_watch":
            watched = str(metadata.get("watch_run_id") or "").strip()
            if watched:
                # 陈旧 wait policy 退休:watch 目标已终态(subagent BLOCKED/完成等)
                # 的 policy 不再调度——它只证明「盯过」,不证明任何 run 在执行,
                # 留着只会每 interval 空转一次唤醒(真机 2026-08-09:已 BLOCKED 的
                # fixer 仍有 enabled watch policy,300s 一轮空转;且这类 policy 的
                # task_id 直接就是被 watch 的子代理 ID,watch 目标≠「调度归属
                # 之外的对象」——判定只看 watch 目标终态,不看它是否等于 task_id)。
                # self-watch(watch_run_id 空)是 interval 唤醒自己,不在此列。
                # 终态判定复用后台准入的同一把尺,不引自然语言。
                status = _background_task_link_status(
                    self.runtime.agent,
                    BackgroundRunRequest(
                        thread_id=policy.thread_id,
                        task_id=watched,
                        reason="progress_policy_watch_retire",
                    ),
                    store=self.store,
                )
                if status.upper() in _POLICY_RETIRE_TERMINAL_STATUSES:
                    try:
                        self.store.disable_progress_policy(policy.policy_id)
                    except Exception:
                        pass
                    return None
        report = self._run_claimed(
            {
                "thread_id": policy.thread_id,
                "task_id": policy.task_id,
                "reason": _progress_policy_run_reason(policy),
                "route_channel": policy.route_channel,
                "route_target": policy.route_target,
                "now": now,
                "wake_signal": _progress_policy_wake_payload(policy),
            }
        )
        if report is not None:
            metadata_updates = {}
            # 问题6:成功 run 清零失败账(failure_count=0),退避/退休账目复原——
            # 一旦恢复,policy 回正常 interval,绝不带着旧失败历史继续减速。
            metadata_updates["failure_count"] = 0
            self.store.mark_progress_reported(
                policy.policy_id,
                now=now,
                no_progress_streak=_next_no_progress_streak(policy, report),
                metadata_updates=metadata_updates,
            )
        return report

    # LLM: claim 后必须重查 terminal 与 recovery block，封住 preflight 到执行间竞态；
    # recovery block 只关闭本 claim，不消费来源。
    # 函数用途: 领取一个后台执行 lane，通过最终准入后运行带心跳的主代理回合。
    def _run_claimed(self, kwargs: dict) -> BackgroundMainAgentReport | None:
        if _subagent_runner_owns_task(
            getattr(self.runtime, "agent", None),
            kwargs.get("task_id"),
        ):
            return _subagent_owned_background_report(kwargs)
        claim_scope_id = _background_claim_scope_id(
            self.store,
            str(kwargs.get("thread_id") or ""),
            str(kwargs.get("task_id") or ""),
        )
        claim = self.store.claim_background_run(
            {
                "thread_id": kwargs.get("thread_id", ""),
                "claim_scope_id": claim_scope_id,
                "task_id": kwargs.get("task_id", ""),
                "reason": kwargs.get("reason", ""),
                "lease_seconds": self.claim_ttl_seconds,
                "now": kwargs.get("now"),
            }
        )
        if claim is None:
            return None
        # A wake/policy may pass its earlier eligibility check and then race
        # `/stop` or foreground completion before the background claim is
        # acquired.  Re-read the exact task link after claiming and before any
        # model/tool work; terminal state wins and the stale source is retired.
        if _claimed_background_task_is_terminal(self.runtime.agent, self.store, kwargs):
            _finish_nonexecuted_background_claim(
                self.store,
                kwargs,
                claim_scope_id=claim_scope_id,
                claim_id=str(claim.get("claim_id") or ""),
                admission="terminal_task_link",
            )
            _retire_terminal_background_source(self.store, kwargs)
            return None
        recovery_block = _background_authority_recovery_block(
            self, str(kwargs.get("task_id") or "").strip()
        )
        if recovery_block is not None:
            _finish_nonexecuted_background_claim(
                self.store,
                kwargs,
                claim_scope_id=claim_scope_id,
                claim_id=str(claim.get("claim_id") or ""),
                admission="authority_recovery_required",
                recovery_block=recovery_block,
            )
            return None
        return self._run_with_heartbeat(
            str(claim.get("claim_id") or ""),
            kwargs,
            claim_scope_id=claim_scope_id,
        )

    def _run_with_heartbeat(
        self,
        claim_id: str,
        kwargs: dict,
        *,
        claim_scope_id: str = "",
    ) -> BackgroundMainAgentReport | None:
        heartbeat = self._start_heartbeat(
            claim_id,
            kwargs["thread_id"],
            claim_scope_id=claim_scope_id,
        )
        status = "finished"
        error: BaseException | None = None
        try:
            task_id = str(kwargs.get("task_id") or "").strip()
            if task_id:
                with register_interruptible(conversation_request_interrupt_name(task_id)):
                    return self.runtime.run_once(kwargs)
            return self.runtime.run_once(kwargs)
        except InterruptedError:
            # `/stop` is an expected user control transition.  The durable task
            # link is already marked interrupted by the control service, so the
            # background lease must close quietly instead of becoming a failed
            # run that recovery code may try to take over.
            status = "cancelled"
            return None
        except _BackgroundCompactSliceYield:
            # The exact wake/observation/policy remains unhandled. Closing only this bounded
            # execution claim lets the next scheduler slice resume from canonical checkpoints
            # without logging a fake crash or incrementing policy failure counters.
            return None
        except BaseException as exc:
            status = "failed"
            error = exc
            raise
        finally:
            heartbeat.stop()
            self.store.finish_background_run(
                {
                    "thread_id": kwargs["thread_id"],
                    "claim_scope_id": claim_scope_id,
                    "claim_id": claim_id,
                    "task_id": kwargs.get("task_id", ""),
                    "status": status,
                    "error": error,
                    "runtime_facts": self._runtime_facts(),
                    "now": now(),
                }
            )
            if status == "failed" and error is not None and not is_provider_transient_error(error):
                # 问题6:失败 run 的异常在 _consume_with_supply_guard 被吸收 → policy
                # next_due_at 不动 → 下个 tick 又 due = 失败无限重试。这里把失败事实
                # 落账(退避顺延/连续失败退休);非 policy 来源的失败不动账,行为不变。
                # 供应类错误(429/限流/超载)不记失败账:它们走 _ProviderSupplyBackoff
                # 专属退避,计 failure_count 会连 3 次 429 就把 policy 退休(错杀);
                # 恢复后照常排期。判据只认 typed provider error,不匹配错误文本。
                self._record_policy_failure(kwargs)

    def _record_policy_failure(self, kwargs: dict) -> None:
        """失败续跑记账:policy 退避 + 连续失败退休(见 _policy_failure_backoff)。

        只认结构化信号:kwargs 的 wake_signal 里 policy_id(policy 触发 run 时由
        _progress_policy_wake_payload 注入)。无 policy_id/policy 已不存在 → 跳过。
        退休(连续 3 次失败)后 policy 离开 due 扫描,任务保持失败态等用户,绝不
        无限重试;账目保留在 metadata,成功路径 mark_progress_reported 清零复原。
        """
        try:
            wake = kwargs.get("wake_signal")
            policy_id = ""
            if isinstance(wake, dict):
                policy_id = str(wake.get("policy_id") or "")
            elif wake is not None:
                policy_id = str(getattr(wake, "policy_id", "") or "")
            if not policy_id:
                return
            policy = self.store.get_progress_policy(policy_id)
            if policy is None:
                return
            metadata = policy.metadata if isinstance(policy.metadata, dict) else {}
            failures = max(0, int(metadata.get("failure_count") or 0)) + 1
            backoff = _policy_failure_backoff(failures, policy_id)
            # 失败时刻与 run 的调度时刻对齐(kwargs["now"],与 claim/finish 同一时钟);
            # 缺失时回落真实时钟。测试用注入时钟可精确断言,生产行为不变。
            try:
                recorded_at = float(kwargs.get("now") or 0.0) or now()
            except (TypeError, ValueError):
                recorded_at = now()
            self.store.mark_progress_failed(
                policy_id,
                now=recorded_at,
                backoff_seconds=backoff,
                failure_count=failures,
                retire_after=_POLICY_FAILURE_RETIRE_AFTER,
            )
            if failures >= _POLICY_FAILURE_RETIRE_AFTER:
                _HEARTBEAT_LOGGER.warning(
                    "progress policy retired after %s failures: policy_id=%s backoff=%s",
                    failures,
                    policy_id,
                    backoff,
                )
        except Exception:
            # 记账失败绝不能让主流程连带崩:失败本身已由 finish_background_run 记录,
            # 这里只是补 policy 退避账,最坏情况退回旧行为(下次仍 due)。
            _HEARTBEAT_LOGGER.warning("record policy failure failed", exc_info=True)

    def _start_heartbeat(
        self,
        claim_id: str,
        thread_id: str,
        *,
        claim_scope_id: str = "",
    ) -> ConversationRunClaimHeartbeat:
        heartbeat = ConversationRunClaimHeartbeat(
            {
                "store": self.store,
                "thread_id": thread_id,
                "claim_scope_id": claim_scope_id,
                "claim_id": claim_id,
                "lease_seconds": self.claim_ttl_seconds,
                "interval_seconds": self.claim_heartbeat_interval_seconds,
            }
        )
        heartbeat.start()
        return heartbeat

    def _mark_signal(self, signal: WakeSignal, current: float, handled: set[str]) -> None:
        self.store.mark_wake_signal_handled(signal.wake_signal_id, now=current)
        handled.add(signal.wake_signal_id)

    # LLM: sampled_at 是本次模型轮开始的结构化时刻；晚于它创建的 sibling 是新事实，
    #   必须留在 pending 队列触发下一轮，不能被当前回复顺带确认。
    # 函数用途: 同批确认采样前已经存在的成功通知，并保留采样期间新到的完成通知。
    def _mark_sibling_signals(
        self,
        signals: list[WakeSignal],
        primary: WakeSignal,
        current: float,
        handled: set[str],
        *,
        sampled_at: float,
    ) -> None:
        for signal in signals:
            created_at = float(getattr(signal, "created_at", 0.0) or 0.0)
            if (
                signal.wake_signal_id not in handled
                and not _is_scheduler_wake_signal(signal)
                and _same_successful_completion_batch(primary, signal)
                and 0 < created_at <= sampled_at
            ):
                self._mark_signal(signal, current, handled)

    def _config_limit(self, key: str) -> int:
        return _agent_config_int(getattr(getattr(self.runtime, "agent", None), "config", None), key)

    def _runtime_facts(self) -> dict[str, object]:
        agent = getattr(self.runtime, "agent", None)
        tree = agent_tree_status_payload(agent, {}) if agent is not None else {}
        return {
            "current_tool": str(getattr(agent, "_current_tool", "") or ""),
            "last_progress_at": float(getattr(agent, "_last_progress_at", 0.0) or 0.0),
            "last_progress_summary": str(getattr(agent, "_last_progress_summary", "") or ""),
            "tree_status_buckets": tree.get("status_buckets")
            if isinstance(tree.get("status_buckets"), dict)
            else {},
            "progress_policy_load_errors": list(self.last_progress_policy_load_errors),
            "progress_policy_suppressed": list(self.last_progress_policy_suppressed),
        }


class BackgroundMainAgentScheduler(
    _BackgroundSchedulerThreadLaneMixin,
    _BackgroundSchedulerTickMixin,
    _BackgroundSchedulerWakeMixin,
    _BackgroundSchedulerGoalMixin,
    _BackgroundSchedulerExecutionMixin,
):
    """Single facade over background tick, goal routing, and claimed execution."""

    # LLM: Diagnostic rows belong to the current background worker thread so
    # concurrent conversation lanes cannot leak policy facts into one another.
    # 函数用途: 读取当前会话车道这一轮的 policy 加载错误。
    @property
    def last_progress_policy_load_errors(self) -> list[dict[str, object]]:
        return list(getattr(self._diagnostics, "policy_load_errors", []))

    # LLM: Assignment replaces only this worker's bounded diagnostic snapshot.
    # 函数用途: 为当前后台车道保存 policy 加载错误。
    @last_progress_policy_load_errors.setter
    def last_progress_policy_load_errors(self, value: list[dict[str, object]]) -> None:
        self._diagnostics.policy_load_errors = list(value or [])

    # LLM: Suppression diagnostics are thread-local for the same reason as load errors.
    # 函数用途: 读取当前会话车道本轮被暂停的 policy 事实。
    @property
    def last_progress_policy_suppressed(self) -> list[dict[str, object]]:
        rows = getattr(self._diagnostics, "policy_suppressed", None)
        if not isinstance(rows, list):
            rows = []
            self._diagnostics.policy_suppressed = rows
        return rows

    # LLM: Keep a mutable per-thread list because existing policy gates append rows in place.
    # 函数用途: 替换当前后台车道的 policy 暂停诊断快照。
    @last_progress_policy_suppressed.setter
    def last_progress_policy_suppressed(self, value: list[dict[str, object]]) -> None:
        self._diagnostics.policy_suppressed = list(value or [])

    # LLM: scheduler 的恢复阻塞缓存只负责同状态日志去重；真实暂停状态始终从
    # RuntimeRepository 重读，进程重启不能改变准入结论。
    # 函数用途: 组装单 Gateway 的后台事件消费者及其进程内退避/观测状态。
    def __init__(self, config: dict):
        self.runtime = config["runtime"]
        self.store = config["store"]
        self._diagnostics = threading.local()
        self.collaboration_store = config.get("collaboration_store") or getattr(
            self.runtime.agent,
            "collaboration_store",
            None,
        )
        self.scheduler_service = config.get("scheduler_service") or getattr(
            self.runtime.agent,
            "scheduler_service",
            None,
        )
        agent_config = getattr(getattr(self.runtime, "agent", None), "config", None)
        claim_ttl = config.get(
            "claim_ttl_seconds",
            _agent_config_int(agent_config, "background_claim_ttl_seconds"),
        )
        heartbeat_interval = config.get(
            "claim_heartbeat_interval_seconds",
            _agent_config_int(agent_config, "background_claim_heartbeat_interval_seconds"),
        )
        self.claim_ttl_seconds = max(1, int(claim_ttl or 1))
        self.claim_heartbeat_interval_seconds = claim_heartbeat_interval_seconds(
            ttl_seconds=self.claim_ttl_seconds,
            configured_interval_seconds=heartbeat_interval,
        )
        self.last_progress_policy_load_errors: list[dict[str, object]] = []
        self.last_progress_policy_suppressed: list[dict[str, object]] = []
        self._last_supervision_at = 0.0
        self._supply_backoff = _supply_backoff_from_agent(self.runtime.agent)
        self._wake_retry_after: dict[str, float] = {}
        self._quota_fallback_wakes: set[str] = set()
        self._authority_recovery_blocks: dict[str, str] = {}


# 函数用途: 把到点的 progress policy 摊开成 Active Wake Signal 载荷——被唤醒的模型要能看到
#   "这是我自己登记的提醒 + 当时写下的原因(wait_reason)",而不是一个没头没尾的定时汇报。
# §6-B4 无进展退避判据(纯结构化信号,不做任何文本判断):本唤醒轮没有成功的
# mutating/dangerous 工具事实=无物质进展,streak+1；读文件、查树、读 task_progress
# 即使成功也不能把 streak 清零。streak 由 store 按 2^streak 拉长间隔(封顶)，
# 让只读空转自动让出调度资源；一旦真正写入/调度/落账成功即复原。
def _next_no_progress_streak(policy: ProgressPolicy, report: BackgroundMainAgentReport) -> int:
    if report.material_progress_count > 0:
        return 0
    metadata = policy.metadata if isinstance(policy.metadata, dict) else {}
    try:
        previous = int(metadata.get("no_progress_streak") or 0)
    except (TypeError, ValueError):
        previous = 0
    return max(0, previous) + 1


def _progress_policy_wake_payload(policy: ProgressPolicy) -> dict[str, object]:
    metadata = policy.metadata if isinstance(policy.metadata, dict) else {}
    payload: dict[str, object] = {
        "kind": "progress_policy_due",
        "reason": "scheduled_progress_report",
        "policy_id": policy.policy_id,
        "task_id": policy.task_id,
        "interval_seconds": policy.interval_seconds,
        "wait_reason": str(metadata.get("reason") or ""),
        "registered_by_tool": str(metadata.get("tool") or ""),
        "watch_run_id": str(metadata.get("watch_run_id") or ""),
    }
    conversation_request_id = str(metadata.get(CONVERSATION_REQUEST_ID_ATTR) or "").strip()
    if conversation_request_id:
        # task_id 是持久调度身份，conversation_request_id 是同一普通用户回合的
        # Todo/工具轨迹展示代次。二者必须并存，不能拿 task-path 或 task_id 冒充后者。
        payload["metadata"] = {
            CONVERSATION_REQUEST_ID_ATTR: conversation_request_id,
        }
    return payload


def _progress_policy_run_reason(policy: ProgressPolicy) -> str:
    return "scheduled_progress_report"


def _policy_failure_backoff(failures: int, policy_id: str) -> float:
    """失败退避 5min×2^(n-1) 上限 1h,抖动 0.90~1.10 按 policy_id 确定性派生。

    纯函数(无 random):同一 policy 每次失败算出同一退避,跨进程/重启可复现,测试
    可精确断言区间;抖动只做跨 policy 错峰(防多个失败 policy 同秒齐醒),不改变
    退避的量级结构。
    """

    base = min(
        _POLICY_FAILURE_BASE_BACKOFF_SECONDS * (2 ** max(0, int(failures) - 1)),
        _POLICY_FAILURE_MAX_BACKOFF_SECONDS,
    )
    digest = hashlib.md5(str(policy_id).encode("utf-8")).hexdigest()
    ratio = 0.9 + (int(digest[:4], 16) % 2000) / 10000.0
    return round(base * ratio, 3)


# LLM: 子代理终态只结束一次整合回合；后续运行仍以精确 active Goal 为准，不读取 Todo 文本或数量。
# 函数用途: 子代理返回后的目标整合轮结束时补一个去重续跑事件，普通任务不自动续跑。
def _ensure_goal_progress_wake_chain(
    scheduler: BackgroundMainAgentScheduler,
    signal: WakeSignal,
    *,
    now: float,
) -> None:
    try:
        task_id = str(getattr(signal, "root_task_id", "") or "").strip()
        thread_id = str(getattr(signal, "thread_id", "") or "").strip()
        agent = getattr(scheduler.runtime, "agent", None)
        ensure_goal_progress_continuation(
            agent,
            task_id=task_id,
            thread_id=thread_id,
            store=scheduler.store,
            now=now,
        )
    except Exception:
        _HEARTBEAT_LOGGER.warning("goal-progress wake chain ensure failed", exc_info=True)


# LLM: 只认 active Goal 与 active task；沿唯一 goal wake 去重，不再创建由 Todo 驱动的第二套周期策略。
# 函数用途: 在安全回合边界为持久目标安排下一轮；子代理尚在工作时留给生命周期事件接续。
def ensure_goal_progress_continuation(
    agent: object | None,
    *,
    task_id: str,
    thread_id: str = "",
    store: ConversationStore | None = None,
    now: float | None = None,
    goal_id: str = "",
) -> bool:
    """保持显式目标的单一事件续跑链，普通任务不会因此获得执行权。"""
    task_id = str(task_id or "").strip()
    if agent is None or not task_id:
        return False
    selected_store = store or getattr(agent, "conversation_store", None)
    if selected_store is None:
        return False
    thread_id = str(thread_id or "").strip() or _thread_id_for_task(selected_store, task_id)
    if not thread_id:
        return False
    try:
        goal = selected_store.load_goal(thread_id, goal_id=goal_id, task_id=task_id)
    except Exception:
        return False
    if (
        goal is None
        or str(getattr(goal, "task_id", "") or "").strip() != task_id
        or str(getattr(goal, "status", "") or "").strip().lower() != "active"
    ):
        return False
    request = BackgroundRunRequest(thread_id=thread_id, task_id=task_id, reason="thread_goal_continue")
    if _background_task_link_status(agent, request, store=selected_store) != "active":
        return False
    phase, state_error = _goal_subagent_phase(agent, task_id)
    if phase == "subagents_active" or state_error:
        return False
    from .goal_runtime import raise_goal_continuation_wake

    raise_goal_continuation_wake(selected_store, goal, now=now)
    return True


def _thread_id_for_task(store: ConversationStore, task_id: str) -> str:
    try:
        thread = store.thread_for_task(task_id)
    except Exception:
        return ""
    return str(getattr(thread, "thread_id", "") or "").strip()


# LLM: 周期性孤儿 supervision(worker-pool self-healing 的 reconcile 环,零 LLM 成本):
#   事件唤醒只覆盖"有人发信号"的死亡;宿主进程被 SIGKILL/断电类静默死亡不发任何 wake,
#   而定时提醒策略只有模型调过 wait 才存在——这里按 orphan_supervision_interval_seconds
#   (默认 60s,0=关)在调度器 tick 里兜底巡查:盯守死岗补建接管 + durable 复活可派孤儿。
#   无候选即 no-op(list_runs 有 mtime 缓存,近零开销);绝不外抛。
def _maybe_supervise_orphans(scheduler: BackgroundMainAgentScheduler, now: float) -> None:
    interval = scheduler._config_limit("orphan_supervision_interval_seconds")
    if interval <= 0 or (now - scheduler._last_supervision_at) < interval:
        return
    scheduler._last_supervision_at = now
    try:
        from ..agent_core.orchestration.dispatch.capability_auto_sweep import (
            supervise_stalled_orphans,
        )

        supervise_stalled_orphans(scheduler.runtime.agent)
    except Exception:
        _HEARTBEAT_LOGGER.debug("orphan supervision sweep failed", exc_info=True)


def _prefer_progress_policy(first: ProgressPolicy, second: ProgressPolicy) -> ProgressPolicy:
    first_score = (first.last_report_at, first.next_due_at, first.policy_id)
    second_score = (second.last_report_at, second.next_due_at, second.policy_id)
    return second if second_score > first_score else first


def _runnable_due_policies(
    store,
    policies: list[ProgressPolicy],
    *,
    now: float,
    agent: object | None = None,
) -> tuple[list[ProgressPolicy], list[tuple[ProgressPolicy, str]]]:
    selected_by_key: dict[tuple[str, str, str, str], ProgressPolicy] = {}
    suppressed: list[tuple[ProgressPolicy, str]] = []
    for policy in policies:
        reason = _progress_policy_suppression_reason(store, policy, now=now, agent=agent)
        if reason:
            suppressed.append((policy, reason))
            continue
        key = (policy.thread_id, policy.task_id, policy.route_channel, policy.route_target)
        existing = selected_by_key.get(key)
        if existing is None:
            selected_by_key[key] = policy
            continue
        selected = _prefer_progress_policy(existing, policy)
        skipped = existing if selected is policy else policy
        selected_by_key[key] = selected
        suppressed.append((skipped, "duplicate_policy"))
    return list(selected_by_key.values()), suppressed


def _progress_policy_suppression_reason(
    store, policy: ProgressPolicy, *, now: float, agent: object | None = None
) -> str:
    if _is_removed_ordinary_task_resume_policy(policy):
        return "removed_ordinary_task_resume_policy"
    if _is_removed_dispatch_supervision_policy(policy):
        return "removed_dispatch_supervision_policy"
    if _is_legacy_child_watch_backstop_policy(policy):
        return "child_watch_backstop_policy"
    if _is_legacy_audit_root_poll_policy(policy):
        return "audit_root_poll_policy"
    if _is_running_durable_audit_root_policy(store, policy):
        return "durable_audit_root_policy"
    if _subagent_runner_owns_task(agent, policy.task_id):
        return "subagent_runner_owned_policy"
    if _policy_task_link_is_terminal(store, policy):
        return "terminal_task_link"
    if _progress_policy_is_stale(policy, now=now):
        return "stale_missed_interval"
    return ""


def _is_legacy_child_watch_backstop_policy(policy: ProgressPolicy) -> bool:
    """Retire the exact marker written by the removed child-bound Audit route."""
    metadata = policy.metadata if isinstance(policy.metadata, dict) else {}
    return (
        str(metadata.get("kind") or "") == "subagent_progress_watch"
        and str(metadata.get("tool") or "") == "watch_backlog_backstop"
    )


# LLM: 旧版 create_subagents 自动登记的模型巡场 policy 已从主链移除；
# 只按结构化 tool 标记识别并退休，不能从说明文字猜测。
# 函数用途: 找出部署升级前残留的自动子代理轮询任务，避免它继续消耗模型调用。
def _is_removed_dispatch_supervision_policy(policy: ProgressPolicy) -> bool:
    metadata = policy.metadata if isinstance(policy.metadata, dict) else {}
    return str(metadata.get("tool") or "") == "dispatch_supervision_auto"


# LLM: Ordinary task auto-resume was removed in favor of 会话运行时 explicit
# events and a separate Goal driver. Match only the old typed marker and retire
# it; never infer migration state from prose, task titles, or timestamps.
# 函数用途: 识别升级前遗留的普通任务自动续跑策略，让调度器一次性退休而不执行模型。
def _is_removed_ordinary_task_resume_policy(policy: ProgressPolicy) -> bool:
    metadata = policy.metadata if isinstance(policy.metadata, dict) else {}
    return str(metadata.get("kind") or "") == "ordinary_task_resume"


def _is_legacy_audit_root_poll_policy(policy: ProgressPolicy) -> bool:
    """Retire the removed periodic coordinator poll for durable Audit workers."""
    metadata = policy.metadata if isinstance(policy.metadata, dict) else {}
    return (
        str(metadata.get("kind") or "") == "named_work_progress"
        and str(metadata.get("tool") or "") == "audit_durable_backstop"
    )


def _is_running_durable_audit_root_policy(store: object, policy: ProgressPolicy) -> bool:
    """Retire coordinator polling once a named Audit has durable source workers.

    Source workers, their leases, and their typed finding/capacity/lifecycle
    events are the continuation mechanism for a running Audit.  A generic
    root ``wait`` policy would only wake the owner model in parallel with that
    mechanism.  Prepare-only Audits remain untouched: the run epoch, finite
    window, and effective source bindings must all be present.
    """

    loader = getattr(store, "load_task_link", None)
    if not callable(loader):
        return False
    try:
        link = loader(str(policy.task_id or "").strip())
    except Exception:
        return False
    return bool(
        link is not None
        and str(getattr(link, "task_id", "") or "") == policy.task_id
        and str(getattr(link, "work_kind", "") or "").strip().lower() == "audit"
        and str(getattr(link, "status", "") or "").strip().lower() == "active"
        and int(getattr(link, "run_epoch", 0) or 0) > 0
        and int(getattr(link, "duration_seconds", 0) or 0) > 0
        and bool(getattr(link, "effective_source_bindings", ()) or ())
    )


def _policy_task_link_is_terminal(store, policy: ProgressPolicy) -> bool:
    if not policy.task_id:
        return False
    try:
        links = store.task_links(policy.thread_id)
    except Exception:
        return False
    for link in links:
        if (
            link.task_id == policy.task_id
            and str(link.status or "").upper() in _POLICY_RETIRE_TERMINAL_STATUSES
        ):
            return True
    return False


# LLM: claim 后重查精确任务；自然收尾后的受管进程补报与队列筛选共用权威校验，不复活停止任务。
# 函数用途: 拦截过期后台工作，同时保留已经核实但尚欠用户的命令结束通知。
def _claimed_background_task_is_terminal(
    agent: object,
    store: object,
    kwargs: dict,
) -> bool:
    """Recheck exact terminal state at the final background-run admission edge."""
    thread_id = str(kwargs.get("thread_id") or "").strip()
    task_id = str(kwargs.get("task_id") or "").strip()
    if not thread_id or not task_id:
        return False
    from .process_events import process_completion_delivery_state

    signal = kwargs.get("wake_signal")
    if (isinstance(signal, WakeSignal)
        and signal.thread_id == thread_id and signal.root_task_id == task_id
        and signal.reason == kwargs.get("reason")
        and process_completion_delivery_state(agent, signal) == "ready"):
        return False
    status = _background_task_link_status(
        agent,
        BackgroundRunRequest(
            thread_id=thread_id,
            task_id=task_id,
            reason=str(kwargs.get("reason") or ""),
        ),
        store=store,
    )
    return status.upper() in _TASK_LINK_TERMINAL_STATUSES


# LLM: unknown 权威只暂停自动调度，不消费 wake/observation/policy；状态来自
# RuntimeRepository 的结构化投影，禁止捕获 RuntimeConflictError 后解析中文文案。
# 函数用途: 判断后台来源是否必须等人工恢复，并对同一阻塞状态只记一次日志。
def _background_authority_recovery_block(
    scheduler: object,
    task_id: str,
) -> dict[str, str] | None:
    normalized_task_id = str(task_id or "").strip()
    if not normalized_task_id:
        return None
    agent = getattr(getattr(scheduler, "runtime", None), "agent", None)
    repo = getattr(getattr(agent, "subagents", None), "runtime_db", None)
    checker = getattr(repo, "main_agent_recovery_block_for_task", None)
    if not callable(checker):
        return None
    try:
        block = checker(normalized_task_id)
    except Exception as exc:  # noqa: BLE001 权威不可读时也不能放行模型/副作用
        block = {
            "schema_version": "main-agent-recovery-block.v1",
            "reason": "authority_state_unreadable",
            "task_id": normalized_task_id,
            "error_type": exc.__class__.__name__,
        }
    cache = getattr(scheduler, "_authority_recovery_blocks", None)
    if not isinstance(cache, dict):
        cache = {}
        scheduler._authority_recovery_blocks = cache
    if block is None:
        if normalized_task_id in cache:
            cache.pop(normalized_task_id, None)
            _HEARTBEAT_LOGGER.info(
                "BACKGROUND_AUTHORITY_RECOVERY_RESUMED task=%s", normalized_task_id
            )
        return None
    fingerprint = json.dumps(block, ensure_ascii=False, sort_keys=True)
    if cache.get(normalized_task_id) != fingerprint:
        cache[normalized_task_id] = fingerprint
        _HEARTBEAT_LOGGER.warning("BACKGROUND_AUTHORITY_RECOVERY_BLOCK %s", fingerprint)
    return block


# LLM: terminal 与 unknown 的最终准入失败都只能关闭本次 claim；是否消费触发源
# 由调用方按原因决定，不能在这个账本 helper 里猜。
# 函数用途: 关闭一次未执行模型/工具的后台 claim，并记录结构化准入原因。
def _finish_nonexecuted_background_claim(
    store: object,
    kwargs: dict,
    *,
    claim_scope_id: str,
    claim_id: str,
    admission: str,
    recovery_block: dict[str, str] | None = None,
) -> None:
    runtime_facts: dict[str, object] = {"admission": admission}
    if recovery_block is not None:
        runtime_facts["recovery_block"] = dict(recovery_block)
    store.finish_background_run(
        {
            "thread_id": kwargs.get("thread_id", ""),
            "claim_scope_id": claim_scope_id,
            "claim_id": claim_id,
            "task_id": kwargs.get("task_id", ""),
            "status": "cancelled",
            "runtime_facts": runtime_facts,
            "now": now(),
        }
    )


# LLM: recovery-blocked policy 只能进入本轮观测投影，不得 disable 或改写 next_due_at。
# 函数用途: 记录一条因主代理 unknown 而暂停的到期策略。
def _record_recovery_blocked_policy(
    scheduler: object,
    policy: ProgressPolicy,
    recovery_block: dict[str, str],
) -> None:
    scheduler.last_progress_policy_suppressed.append(
        {
            "policy_id": policy.policy_id,
            "thread_id": policy.thread_id,
            "task_id": policy.task_id,
            "reason": "authority_recovery_required",
            "recovery_reason": str(recovery_block.get("reason") or ""),
        }
    )


def _retire_terminal_background_source(store: object, kwargs: dict) -> None:
    """Consume the exact stale wake/policy that lost a race with task termination."""
    wake = kwargs.get("wake_signal")
    if isinstance(wake, WakeSignal):
        try:
            store.mark_wake_signal_handled(wake.wake_signal_id, now=now())
        except Exception:
            pass
        return
    if not isinstance(wake, dict):
        return
    policy_id = str(wake.get("policy_id") or "").strip()
    if not policy_id:
        return
    try:
        store.disable_progress_policy(policy_id, now=now())
    except Exception:
        pass


# 被抑制后应"退休"(disable)而非"续命"的原因:被观察任务已终态,或策略早已 stale(错过整个
# 追赶窗口=任务多半已死/无可挽回)。这两类若继续 mark_progress_reported 续命,会被无限复活、
# 每个间隔唤醒后台主代理发一次 LLM 进度汇报,占满 gateway worker(churn 根因)。
# duplicate_policy 不退休(只是本轮去重,真身仍活),继续续命留作后备。
_RETIRE_SUPPRESSION_REASONS = frozenset(
    {
        "terminal_task_link",
        "stale_missed_interval",
        "child_watch_backstop_policy",
        "audit_root_poll_policy",
        "durable_audit_root_policy",
        "removed_dispatch_supervision_policy",
        "removed_ordinary_task_resume_policy",
        "subagent_runner_owned_policy",
    }
)


def _suppressed_policy_action(agent: object | None, policy: ProgressPolicy, reason: str) -> str:
    """被抑制 policy 的处置裁决(纯结构信号):retire=退休 / renew=续命推进排期。
    g8 问题B·stale 不杀活任务:错过追赶窗常见于唤醒轮长期领不到 claim/网关中断,任务本身
    可能还活着——清单还有未闭环项时不 disable,只把排期推到 now+interval 继续追(账没对完
    唤醒链不许死,与收口退休守卫同一原则)。churn 有界:每 interval 至多一轮 + 无进展退避
    8× 封顶;任务终态走 terminal_task_link 照常退休,清单全闭后收口自动退休——终点都在。
    无清单/读账失败按 0,行为与旧版完全一致。"""
    if reason not in _RETIRE_SUPPRESSION_REASONS:
        return "renew"
    if (
        reason == "stale_missed_interval"
        and ledger_open_progress_item_count(agent, policy.task_id) > 0
    ):
        return "renew"
    return "retire"


def _apply_suppressed_policy(store, policy: ProgressPolicy, action: str, *, now: float) -> None:
    try:
        if action == "retire":
            store.disable_progress_policy(policy.policy_id, now=now)
        else:
            store.mark_progress_reported(policy.policy_id, now=now)
    except Exception:
        pass


def _snooze_suppressed_policies(
    store,
    suppressed: list[tuple[ProgressPolicy, str]],
    *,
    now: float,
    agent: object | None = None,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for policy, reason in suppressed:
        rows.append(
            {
                "policy_id": policy.policy_id,
                "thread_id": policy.thread_id,
                "task_id": policy.task_id,
                "reason": reason,
            }
        )
        _apply_suppressed_policy(
            store, policy, _suppressed_policy_action(agent, policy, reason), now=now
        )
    return rows


def _progress_policy_is_stale(policy: ProgressPolicy, *, now: float) -> bool:
    if policy.next_due_at <= 0:
        return False
    interval = max(1, int(policy.interval_seconds or 1))
    catchup_window = max(
        _MIN_PROGRESS_POLICY_CATCHUP_SECONDS, interval * _MAX_PROGRESS_POLICY_CATCHUP_INTERVALS
    )
    return now - policy.next_due_at > catchup_window


def _agent_config_int(config: object | None, key: str) -> int:
    if config is None:
        return default_config_int(key, minimum=0)
    try:
        return max(0, int(getattr(config, key)))
    except (TypeError, ValueError):
        return default_config_int(key, minimum=0)


# ---------------------------------------------------------------------------
# Goal/手动续跑共享 gate：只判断一轮的结构化收口原因是否允许继续，
# 不授予普通任务自动调度权；是否真正续跑仍由显式 Goal 或用户命令决定。


# 可继续收口 reason 白名单。普通模式只据此给出诚实阶段状态；显式 Goal
# 可以据此排下一轮，用户显式 resume 也可沿同一任务继续。


def resume_prompt_for(*, continuation_reason: str, continuation_seq: int, user_task: str) -> str:
    """续跑提示：只引用结构化 continuation_reason + 原任务，不含验收语义。

    2026-08-14 设计 v2(审查意见4)：提示词不得出现「代码规模/测试达标」
    等专项验收词——完成判断只由结构化收口信号 + 预算决定。唯一权威在
    conversation 层(gate 层): CLI resume_loop 与 gateway handoff 接管
    用同一构造(cli.resume_contract 反向 import, 避免 RUNTIME_IMPORTS_CLI)。
    """
    return (
        f"【系统续跑 #{continuation_seq}】上一轮因 {continuation_reason} 收口，"
        f"任务尚未完成。请基于已有进度继续推进原任务：{user_task}"
    )


CONTINUABLE_REASONS = frozenset(
    {
        "TASK_PROGRESS_OPEN",
        "TOOL_ROUND_LIMIT_REACHED",
        "REPEATED_TOOL_FAILURE",
        # 2026-08-15 3×3 真机: 模型输出未闭合 [TOOL_CALL] 纯格式错误——
        # 整轮零执行已保证安全(J.5 不变), 任务级允许续跑(重发完整工具块);
        # 仅 response_decision 对全 TOOL_CALL_UNCLOSED violations 产生此
        # reason, 其他协议违规仍 blocked fail-closed。
        "TOOL_CALL_UNCLOSED",
        # 只有工具轮硬边界、协议截断或上下文溢出这类宿主无法自然完成的工作片
        # 才能自动续跑。单个工具失败已经作为 typed result 返回同一模型；模型给出
        # plain final 后不得再由机器覆盖结论或另起返工轮。
        "MODEL_RESPONSE_TRUNCATED",
        # EXEC-28 阶段二 ma-b 真机: 写码 92 轮后工具上下文 398K 字符溢出,
        # preflight 收口 context_overflow RC=2 不在白名单 → 直接退出, 已写
        # 20 文件白费。对照 会话运行时 自动 compact 后任务继续。上下文溢出同属
        # 返工门(compact→续跑), 应自动续跑。
        "CONTEXT_OVERFLOW",
    }
)


def should_continue_task(final_response: object) -> tuple[bool, str]:
    """判断一次收口是否允许由 Goal 或显式 resume 继续(结构化)。

    返回 (should, reason): should=True 只表示技术上可继续，不代表普通任务会
    自动调度。blocked/协议违规/UNKNOWN effect 等一律 False。

    显式 Goal 的 finalization 与 CLI 手动/Goal 驱动共用这条精确条件。
    """
    reason = str(getattr(final_response, "runtime_reason", "") or "").strip().upper()
    source = str(getattr(final_response, "runtime_source", "") or "").strip()
    status = str(getattr(final_response, "runtime_status", "") or "").strip().lower()
    if reason in CONTINUABLE_REASONS:
        # 双席 seq1989 结构门: TOOL_CALL_UNCLOSED 只由协议适配器在
        # unfinished 收口产生——补 source/status 精确约束, 防其他路径误标
        # 同一 reason 被放行续跑(生产路径由 host 结构化赋值, 此处防御性)。
        if reason == "TOOL_CALL_UNCLOSED" and not (
            source == "tool_protocol_adapter" and status == "unfinished"
        ):
            return False, reason or "not_continuable"
        if reason == "MODEL_RESPONSE_TRUNCATED" and not (
            source == "tool_loop" and status == "unfinished"
        ):
            return False, reason or "not_continuable"
        if reason == "CONTEXT_OVERFLOW" and not (
            source == "preflight" and status == "context_overflow"
        ):
            return False, reason or "not_continuable"
        return True, reason
    return False, reason or "not_continuable"
