# Conversation runtime utilities
from __future__ import annotations

import json
import time
from functools import partial
from pathlib import Path
from typing import Any

from ..backends.errors import (
    is_provider_quota_exhausted_error,
    is_provider_transient_error,
    is_provider_usage_limit_error,
)
from ..concurrency.interrupt import register_interruptible
from ..runtime_errors import compact_error_message
from ..settings.runtime_guard_config import runtime_guard_int
from .authority import (
    CONVERSATION_BACKGROUND_EVENT_REASON_ATTR,
    CONVERSATION_REQUEST_ID_ATTR,
    CONVERSATION_TASK_TURN_ACTIVE_ATTR,
)
from .control_commands import conversation_request_interrupt_name
from .models import (
    SUBAGENT_LIFECYCLE_WAKE_REASONS,
    ConversationThread,
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
    "A subagent lifecycle event resumed this same task. Inspect the typed wake "
    "signal, agent tree, objective, persisted artifacts, tool records, and "
    "evidence references. A child status or prose is evidence, not authority "
    "that the objective is complete. Decide the next action from current facts "
    "and available capabilities. The runtime does not require a particular "
    "child count, role, review path, integration order, or tool. Current Task "
    "Runtime State and current tool results are authoritative for task identity, "
    "source identity, counts, coverage, and terminal state; never substitute those "
    "facts from child prose, an earlier assistant message, or a derived artifact. "
    "If the structured projection says rows were omitted, inspect the current "
    "task-scoped tools before reporting them. Avoid duplicate work and polling. "
    "Report completion only when acceptance evidence supports it, and describe "
    "unresolved limitations truthfully."
)


_GOAL_SUBAGENTS_ACTIVE_PROMPT = (
    "\n\nStructured runtime fact: one or more subagents related to this exact goal task are still "
    "nonterminal. Their lifecycle events will wake this same goal again. Treat this as current "
    "state, not as a required workflow: decide whether to continue parent work, inspect, guide, "
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


def background_prompt(
    reason: str,
    *,
    goal: object | None = None,
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

        prompt = continuation_prompt(goal)
        if goal_subagent_phase == "subagents_active":
            return prompt + _GOAL_SUBAGENTS_ACTIVE_PROMPT
        if goal_subagent_phase == "subagents_terminal":
            return prompt + "\n\n" + _SUBAGENT_INTEGRATION_WAKE_PROMPT + f"\n唤醒原因:{reason}"
        return prompt
    if normalized_reason == "thread_goal_continue":
        return "Continue working toward the active thread goal. Call get_goal first."
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


def observations_by_thread(
    observations: list[ObservationEvent],
) -> dict[str, list[ObservationEvent]]:
    grouped: dict[str, list[ObservationEvent]] = {}
    for observation in observations:
        grouped.setdefault(observation.thread_id, []).append(observation)
    return grouped


def first_root_task_id(observations: list[ObservationEvent]) -> str:
    return next((item.root_task_id for item in observations if item.root_task_id), "")


def now(value: float | None = None) -> float:
    return float(time.time() if value is None else value)


# Conversation runtime channel snapshots
from .models import ConversationThread
from .store import ConversationStore


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
from typing import Any

# Background wake turns are normal continuations of the same Agent.  This is
# the common capability surface before owner/task policy applies reductions.
_BACKGROUND_WORK_TOOLS = (
    "read_file",
    "list_files",
    "search_text",
    "write_file",
    "edit_file",
    "run_command",
    "watch_stream",
    "record_finding",
    "task_progress",
    "resolve_capability_requests",
    "cancel_subagents",
    "authorize_network_host",
)

DEFAULT_BACKGROUND_ALLOWED_TOOLS = (
    "wait",
    "inspect_agent_tree",
    "raise_event",
    "raise_collaboration",
    "inspect_collaboration",
    "submit_collaboration_result",
    "update_collaboration",
    "dispatch_subagents",
    "send_guidance",
    "create_subagents",
    *_BACKGROUND_WORK_TOOLS,
)

# A typed finding is already durable before it wakes the owner Agent.  The
# reporting turn may inspect, coordinate, investigate and deliver it, but must
# not record the same event again under model-chosen prose or a second id.  This
# is the same authority split as 会话运行时's event/delivery lifecycle: the producer
# owns persistence, while the consumer owns acknowledgement and presentation.
# Proactive delivery uses the common typed send_message receipt; final text is
# internal coordination output and is never a second sender.
AUDIT_FINDING_ALLOWED_TOOLS = (
    *(tool for tool in DEFAULT_BACKGROUND_ALLOWED_TOOLS if tool != "record_finding"),
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
    "wait": "登记到点自动唤醒你的非阻塞提醒；等子代理进度、盯持续变化的数据/文件都用它，不要原地轮询。",
    "inspect_agent_tree": "只读查看主/子/孙代理状态树。",
    "raise_event": "记录普通进展、阻塞或需要主代理处理的事件。",
    "raise_collaboration": "发起协作；没有 case_id 时开 case，有 question/target 时同步发 request。",
    "inspect_collaboration": "只读查看协作 case 或待处理协作请求。",
    "submit_collaboration_result": "提交协作命中、未命中、证据引用和限制说明。",
    "update_collaboration": "更新协作 case 或 request；带 target_agent_ids 可改派请求。",
    "dispatch_subagents": "只有需要推进、恢复或调度时才调用。",
    "send_guidance": "给正在运行的代理追加软提示。",
    "create_subagents": "创建并启动新的下级代理。",
    "read_file": "读取子代理产出的文件/产物,用于整合与验收。",
    "list_files": "查看子代理在工作区写了哪些产物。",
    "search_text": "在子代理产物里检索内容。",
    "write_file": "写最终交付物,或把子代理产物整合成成品。",
    "edit_file": "修订/整合已有交付文件。",
    "run_command": "运行 import/测试做交付前自检。",
    "watch_stream": "读取有持久游标和覆盖账的数据源；Audit 模式按完整记录和 ack/source_ref 对账。",
    "record_finding": "可选地把一条带证据引用的任务事实写入耐久账本。",
    "task_progress": "更新任务清单进展。",
    "resolve_capability_requests": "批准或拒绝子代理的能力申请,让它能继续干。",
    "cancel_subagents": "了结救不回来的子代理(重派/给提示都无效时),别让空壳拖住整个任务收尾。",
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
    # observation_requires_main_agent = 子代理 raise_event(urgent) 的真事件被观察批叫回主代理。
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
from pathlib import Path
from typing import Any

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
from .models import BackgroundMainAgentReport, WakeSignal
from .store import ConversationStore


@dataclass(frozen=True)
class BackgroundRunRequest:
    thread_id: str
    task_id: str = ""
    reason: str = "scheduled_progress_report"
    route_channel: str = "internal"
    route_target: str = ""
    now: float = 0.0
    wake_signal: dict[str, Any] | None = None


@dataclass(frozen=True)
class GoalRuntimeContext:
    goal: object | None = None
    subagent_phase: str = ""
    state_error: str = ""


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


def _goal_runtime_context(
    agent: object,
    store: ConversationStore,
    request: BackgroundRunRequest,
) -> GoalRuntimeContext:
    """Resolve one exact active goal and its child phase from durable state only."""
    task_id = str(request.task_id or "").strip()
    if not task_id:
        return GoalRuntimeContext()
    try:
        goal = store.load_goal(request.thread_id, task_id=task_id)
    except Exception:
        return GoalRuntimeContext(state_error="goal_state_load_error")
    if (
        goal is None
        or str(getattr(goal, "task_id", "") or "").strip() != task_id
        or str(getattr(goal, "status", "") or "").strip().lower() != "active"
    ):
        return GoalRuntimeContext()
    phase, state_error = _goal_subagent_phase(agent, task_id)
    return GoalRuntimeContext(goal=goal, subagent_phase=phase, state_error=state_error)


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
        (
            response,
            tool_call_count,
            tool_success_count,
            material_progress_count,
            delivery_artifacts,
            message_tool_deliveries,
            operation_verification,
        ) = self._run_agent(
            thread,
            request,
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
        if _message_tool_delivery_satisfied(request, message_tool_deliveries):
            # 通道运行时's cron runner treats committed message-tool delivery as the
            # source reply and skips its announce fallback. Mirror the payload into
            # the same transcript, but never call the channel a second time.
            reported_content = _mirror_message_tool_deliveries(
                self.store,
                request,
                delivery_context,
                message_tool_deliveries,
            )
            delivery_status = "sent"
            delivery_reason = _message_tool_delivery_reason(request)
            wake_handled = True
        else:
            reported_content, delivery_status = self._record_response(
                request,
                delivery_context,
                response,
                delivery_artifacts=delivery_artifacts,
                operation_verification=operation_verification,
                deliver=deliver,
                delivery_reason=delivery_reason,
                route_supports_transcript=route_supports_transcript,
            )
            wake_handled = _background_owner_delivery_committed(
                request,
                channel=channel,
                target=target,
                route_supports_proactive=route_supports_proactive,
                route_supports_transcript=route_supports_transcript,
                delivery_status=delivery_status,
                content=reported_content,
            )
        return BackgroundMainAgentReport(
            thread_id=request.thread_id,
            task_id=request.task_id,
            reason=request.reason,
            response=reported_content,
            route_channel=channel,
            route_target=target,
            created_at=request.now,
            tool_call_count=tool_call_count,
            tool_success_count=tool_success_count,
            material_progress_count=material_progress_count,
            delivery_status=delivery_status,
            delivery_reason=delivery_reason,
            wake_handled=wake_handled,
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
        route_supports_proactive = bool(target and self.channels.supports_proactive(channel))
        route_supports_transcript = _route_supports_transcript(self.channels, channel)
        content, delivery_status = self._record_response(
            request,
            delivery_context,
            str(prepared.get("content") or ""),
            deliver=True,
            delivery_reason="cached_owner_delivery_retry",
            route_supports_transcript=route_supports_transcript,
        )
        wake_handled = _background_owner_delivery_committed(
            request,
            channel=channel,
            target=target,
            route_supports_proactive=route_supports_proactive,
            route_supports_transcript=route_supports_transcript,
            delivery_status=delivery_status,
            content=content,
        )
        return BackgroundMainAgentReport(
            thread_id=signal.thread_id,
            task_id=signal.root_task_id,
            reason=signal.reason,
            response=content,
            route_channel=channel,
            route_target=target,
            created_at=now,
            delivery_status=delivery_status,
            delivery_reason="cached_owner_delivery_retry",
            wake_handled=wake_handled,
        )

    def _run_agent(
        self,
        thread,
        request: BackgroundRunRequest,
        *,
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
    ]:
        goal_context = _goal_runtime_context(self.agent, self.store, request)
        result = self.agent.run(
            background_prompt(
                request.reason,
                goal=goal_context.goal,
                goal_subagent_phase=goal_context.subagent_phase,
                wake_signal=request.wake_signal,
                proactive_delivery_available=proactive_delivery_available,
                transcript_delivery_available=transcript_delivery_available,
            ),
            params=_run_params(
                thread.thread_id,
                request,
                self.agent,
                goal_context=goal_context,
                proactive_delivery_available=proactive_delivery_available,
            ),
            inject=[
                context_markdown(
                    agent=self.agent,
                    store=self.store,
                    thread=thread,
                    request=request,
                    proactive_delivery_available=proactive_delivery_available,
                )
            ],
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
        return (
            str(getattr(result, "response", "") or ""),
            len(calls),
            successes,
            material_progress,
            artifacts,
            deliveries,
            operation_verification,
        )

    def _record_response(
        self,
        request: BackgroundRunRequest,
        delivery_context: DeliveryContext,
        internal_content: str,
        *,
        delivery_artifacts: tuple[dict[str, object], ...] = (),
        operation_verification: dict[str, object] | None = None,
        deliver: bool,
        delivery_reason: str,
        route_supports_transcript: bool | None = None,
    ) -> tuple[str, str]:
        # 内部协议仍交给真实 DeliveryService 做主动消息抑制，但普通 transcript/report
        # 只能保存用户投影，否则下一轮 compact 和 owner-local 搜索会被机器协议污染。
        projection = project_user_reply(internal_content)
        if not deliver:
            return projection.content, "suppressed"
        terminal_status = _background_task_link_status(self.agent, request, store=self.store)
        goal_terminal_delivery = delivery_reason in {
            "thread_goal_blocked",
            "thread_goal_budget_limited",
            "thread_goal_usage_limited",
        }
        if (
            terminal_status in {"abandoned", "cancelled", "interrupted", "superseded"}
            and not goal_terminal_delivery
        ):
            return projection.content, "suppressed"
        # ReplyEnvelope is a user-content envelope, not an internal protocol carrier.
        # Sending the already projected text also keeps the real DeliveryService from
        # having to distinguish a valid completion signal from other internal signals.
        attachments = _channel_attachments(delivery_artifacts)
        evidence_refs = (
            _background_delivery_evidence_refs(request)
            if _audit_finding_report_event(request)
            else _background_evidence_refs(request)
        )
        if not projection.content.strip() and not attachments:
            return "", "suppressed"
        envelope = ReplyEnvelope(
            content=projection.content,
            attachments=attachments,
            evidence_refs=evidence_refs,
        )
        message_metadata: dict[str, object] = {
            "reason": request.reason,
            "task_id": request.task_id,
            "delivery_artifacts": [dict(item) for item in delivery_artifacts],
            "projection_status": projection.projection_status,
            "background_delivery_reason": delivery_reason,
            "evidence_refs": list(evidence_refs),
        }
        if operation_verification is not None:
            message_metadata["operation_verification"] = operation_verification
        if _audit_capacity_report_event(request) and not attachments:
            wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
            wake_signal_id = str(wake.get("wake_signal_id") or "").strip()
            if wake_signal_id:
                self.store.cache_pending_wake_delivery(
                    wake_signal_id,
                    {
                        "schema_version": "wake-owner-delivery.v1",
                        "reason": request.reason,
                        "task_id": request.task_id,
                        "content": projection.content,
                        "evidence_refs": list(evidence_refs),
                        "created_at": time.time(),
                    },
                )
        receipt = self.channels.deliver(delivery_context, envelope)
        delivery_status = str(getattr(receipt, "delivery_status", "") or "sent")
        # The delivery service owns final user-boundary redaction.  Persist the
        # exact content recorded by its receipt so external IM and local
        # transcript never diverge; simple test/legacy receipts without content
        # retain the already-sanitized projection as a compatibility fallback.
        committed_content = str(getattr(receipt, "content", projection.content) or "")
        transcript_record = bool(
            (
                supports_transcript_delivery(delivery_context.channel)
                if route_supports_transcript is None
                else route_supports_transcript
            )
            and delivery_status == "not_applicable"
        )
        _commit_background_response(
            self,
            request,
            delivery_context,
            receipt=receipt,
            delivery_status=delivery_status,
            committed_content=committed_content,
            transcript_record=transcript_record,
            evidence_refs=evidence_refs,
            message_metadata=message_metadata,
        )
        return committed_content, delivery_status


def _commit_background_response(
    runtime: BackgroundMainAgentRuntime,
    request: BackgroundRunRequest,
    delivery_context: DeliveryContext,
    *,
    receipt: object,
    delivery_status: str,
    committed_content: str,
    transcript_record: bool,
    evidence_refs: tuple[str, ...],
    message_metadata: dict[str, object],
) -> None:
    """Persist only a provider-committed or authoritative local delivery."""

    if delivery_status != "sent" and not transcript_record:
        return
    message_request = {
        "thread_id": request.thread_id,
        "role": "assistant",
        "content": committed_content,
        "channel": delivery_context.channel,
        "metadata": message_metadata,
    }
    delivery_key = _background_delivery_idempotency_key(request)
    if delivery_key:
        message_entry = runtime.store.append_message_once(
            message_request,
            dedupe_key=f"owner_delivery:{delivery_key}",
        )
    else:
        message_entry = runtime.store.append_message(message_request)
    if delivery_status == "sent":
        _record_delivered_audit_refs(runtime.agent, receipt)
    elif _audit_finding_report_event(request):
        # CLI/Gateway transcript routes have no provider receipt. Their local
        # transcript append is the durable commit and therefore the receipt.
        _record_transcript_audit_refs(
            runtime.agent,
            evidence_refs,
            message_entry=message_entry,
            channel=delivery_context.channel,
        )


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


def _cached_owner_delivery(signal: WakeSignal) -> dict[str, object] | None:
    raw_metadata = getattr(signal, "metadata", None)
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    delivery = metadata.get("owner_delivery")
    if not isinstance(delivery, dict):
        return None
    if (
        str(delivery.get("schema_version") or "") != "wake-owner-delivery.v1"
        or str(delivery.get("reason") or "") != str(signal.reason or "")
        or str(delivery.get("task_id") or "") != str(signal.root_task_id or "")
        or not str(delivery.get("content") or "").strip()
    ):
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


def _background_delivery_decision(
    agent: object,
    request: BackgroundRunRequest,
    *,
    store: ConversationStore | None = None,
    resolved_channel: str | None = None,
    resolved_route_supports_proactive: bool | None = None,
    resolved_route_supports_transcript: bool | None = None,
) -> tuple[bool, str]:
    """Keep partial child integration internal until durable state proves completion."""
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
        return False, "thread_goal_continuation_internal"
    if goal_status == "active" and reason in SUBAGENT_LIFECYCLE_WAKE_REASONS:
        return False, "thread_goal_lifecycle_internal"
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
    if not task_completed:
        return False, "root_task_still_active"
    return True, "root_subagents_terminal"


def _background_owner_delivery_committed(
    request: BackgroundRunRequest,
    *,
    channel: str,
    target: str,
    route_supports_proactive: bool,
    route_supports_transcript: bool,
    delivery_status: str,
    content: str,
) -> bool:
    """Acknowledge owner-facing wakes only after their real delivery commit."""

    if not _audit_owner_report_event(request):
        return True
    status = str(delivery_status or "").strip().lower()
    if route_supports_transcript:
        # ``_record_response`` appends this exact model-authored content before
        # returning ``not_applicable``.  Empty/suppressed drafts never count.
        return bool(str(content or "").strip()) and status in {"not_applicable", "sent"}
    return bool(target) and route_supports_proactive and status == "sent"


# LLM: 生产与测试投递服务应显式声明 transcript 能力；旧测试替身没有该
# 方法时只回退到同一份内置路由声明，绝不从模型正文或 adapter 失败推断。
# 函数用途: 读取当前投递边界对本地权威会话交付的结构化能力。
def _route_supports_transcript(channels: object, channel: str) -> bool:
    probe = getattr(channels, "supports_transcript", None)
    if callable(probe):
        return bool(probe(channel))
    return supports_transcript_delivery(channel)


def _matching_goal_status(
    store: ConversationStore | None,
    request: BackgroundRunRequest,
) -> str:
    if store is None:
        return ""
    try:
        goal = store.load_goal(request.thread_id, task_id=str(request.task_id or "").strip())
    except Exception:
        return ""
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
        "dispatch_supervision_auto",
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
    if reason in _SCHEDULED_WAKE_REASONS:
        return _signal_task_link_is_terminal(agent, store, signal, reason)
    if reason not in SUBAGENT_LIFECYCLE_WAKE_REASONS:
        return False
    return _signal_task_link_is_terminal(agent, store, signal, reason)


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
    phase, state_error = _goal_subagent_phase(agent, task_id)
    return not state_error and phase == "subagents_active"


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


def _related_subagent_runs(agent: object, root_task_id: str) -> tuple[list[object], str]:
    if not root_task_id:
        return [], "subagent_root_unknown"
    manager = getattr(agent, "subagents", None)
    if manager is None:
        return [], "subagent_state_unavailable"
    try:
        if callable(getattr(manager, "list_runs_report", None)):
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


def _run_params(
    thread_id: str,
    request: BackgroundRunRequest,
    agent: object | None = None,
    *,
    goal_context: GoalRuntimeContext | None = None,
    proactive_delivery_available: bool | None = None,
) -> RunParams:
    config = getattr(agent, "config", None)
    conversation_store = getattr(agent, "conversation_store", None)
    resolved_goal_context = goal_context or (
        _goal_runtime_context(agent, conversation_store, request)
        if agent is not None and conversation_store is not None
        else GoalRuntimeContext()
    )
    scheduler_run_id = _scheduler_run_id(request)
    return RunParams(
        save=False,
        source="background_main_agent",
        request_id=scheduler_run_id,
        run_id=scheduler_run_id or f"bg-main-{thread_id}",
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
        task_attributes=_background_task_attributes(thread_id, request, agent),
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
    )


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


# LLM: Every durable wake keeps the original task identity and typed mode facts; a scheduler request id is not a new task.
# 函数用途: 为后台续跑构造当前任务属性，让子代理、工作区和 Audit 账本跨唤醒继续同一条任务。
def _background_task_attributes(
    thread_id: str,
    request: BackgroundRunRequest,
    agent: object | None,
) -> dict[str, object] | None:
    attributes: dict[str, object] = {}
    wake = request.wake_signal if isinstance(request.wake_signal, dict) else {}
    metadata = wake.get("metadata") if isinstance(wake.get("metadata"), dict) else {}
    scheduler_run_id = str(metadata.get("scheduler_run_id") or "").strip()
    task_id = str(request.task_id or "").strip()
    _apply_internal_background_tool_budget(attributes, request, agent)
    if _narrow_audit_event_reason(request.reason):
        attributes[CONVERSATION_BACKGROUND_EVENT_REASON_ATTR] = (
            str(request.reason or "").strip().lower()
        )
    if thread_id and (task_id or scheduler_run_id):
        attributes["conversation_thread_id"] = str(thread_id).strip()
    wake_signal_id = str(wake.get("wake_signal_id") or "").strip()
    if wake_signal_id:
        # The scheduler acknowledges the event that started this turn. The
        # active-turn inbox only consumes newer events arriving mid-turn.
        attributes["background_wake_signal_id"] = wake_signal_id
    if _audit_finding_report_event(request):
        attributes["background_delivery_evidence_refs"] = list(
            _background_delivery_evidence_refs(request)
        )
    if task_id:
        attributes.update(
            {
                "conversation_task_id": task_id,
                # A durable background turn is another execution of this exact
                # task, not a new lineage. Descendants and audit ledgers must
                # keep the original task id across wakeups.
                CONVERSATION_REQUEST_ID_ATTR: task_id,
                # The scheduler acquired the thread claim before constructing
                # these params, so this background turn is the current task's
                # live executor rather than a competing executor.  Keep that
                # ownership as typed, per-turn state; the normal execution
                # blocker still rejects every other turn that lacks this flag.
                CONVERSATION_TASK_TURN_ACTIVE_ATTR: True,
            }
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
    return attributes or None


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
from typing import Any

from ..agent_core.agent_tree.status import agent_tree_status_payload
from ..artifacts.registry import latest_artifact_records
from ..runtime_errors import runtime_error_report
from ..settings.defaults import default_config_value
from .context_budget import (
    BackgroundContextPayloadRequest,
    background_context_budget_from_config,
    bounded_background_context_payload,
)
from .models import ConversationThread
from .store import ConversationStore


@dataclass(frozen=True)
class _BackgroundContextLoad:
    agent: object
    store: ConversationStore
    thread: ConversationThread
    task_id: str
    config: object | None
    policy_request: BackgroundToolPolicyRequest
    load_errors: list[dict[str, Any]]


def context_markdown(
    *,
    agent: object,
    store: ConversationStore,
    thread: ConversationThread,
    request,
    proactive_delivery_available: bool | None = None,
) -> str:
    policy_request = _tool_policy_request(
        agent,
        request,
        proactive_delivery_available=proactive_delivery_available,
    )
    task_id = str(getattr(request, "task_id", "") or "").strip()
    active_wake_signal = request.wake_signal if isinstance(request.wake_signal, dict) else None
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
        ("Conversation Thread", bounded.get("thread") or {}),
        ("Runtime Load Errors", bounded.get("load_errors") or []),
        ("Task Runtime State", bounded.get("task_runtime_state") or {}),
        ("Recent Messages", bounded.get("messages") or []),
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
    return bounded_background_context_payload(
        BackgroundContextPayloadRequest(
            bundle=bundle,
            active_wake_signal=active_wake_signal,
            pending_wake_signals=pending_wake_signals,
            task_runtime_state=task_state,
            agent_tree=agent_tree,
            recovery_snapshot=recovery_snapshot,
            load_errors=load_errors,
            budget=background_context_budget_from_config(config),
        )
    )


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
    if not state.task_id:
        return bundle
    task_ids = _task_context_ids(state)
    task_rows = _dict_rows(bundle.get("tasks"))
    exact_link = next(
        (row for row in task_rows if str(row.get("task_id") or "").strip() == state.task_id),
        None,
    )
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
    if _is_detached_named_task_link(exact_link):
        return _detached_named_task_context(state, scoped, exact_link or {}, task_ids)
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


def _pending_wake_signals(state: _BackgroundContextLoad) -> list[dict[str, Any]]:
    try:
        if callable(getattr(state.store, "pending_wake_signals_report", None)):
            signals, load_errors = state.store.pending_wake_signals_report(
                limit=_config_int(state.config, "background_pending_wake_prompt_limit"),
            )
            state.load_errors.extend(load_errors)
            payload = [
                item.to_dict() for item in signals if item.thread_id == state.thread.thread_id
            ]
        else:
            payload = pending_wake_payload(
                state.store,
                state.thread.thread_id,
                limit=_config_int(state.config, "background_pending_wake_prompt_limit"),
            )
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


def _minimal_context_bundle(thread: ConversationThread) -> dict[str, Any]:
    return {
        "thread": thread.to_dict(),
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

from ..agent_core.agent_tree.status import agent_tree_status_payload
from ..settings.defaults import default_config_int
from .models import BackgroundMainAgentReport, ObservationEvent, ProgressPolicy, WakeSignal

# 后台 claim 心跳是 daemon 线程，其异常必须结构化落日志而非裸崩 stderr 杀线程。
_HEARTBEAT_LOGGER = logging.getLogger("agent.conversation.background_claim_heartbeat")

# 任务续跑 wake 冷却(秒):距上次落 wake 不足此值时不再重复催促——子代理正在跑/
# 主代理 turn 还在推进的窗口内,重复拉起只有模型调用成本没有新信息(真机 celery
# 46 分钟 18 次 task_ledger_resume)。死停最长 cooldown 内被发现(H 批前是 6h)。
_TASK_RESUME_COOLDOWN_SECONDS = 900

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

# 失败续跑记账(问题6):失败 run 后 policy 退避 5min×2^(n-1)、上限 1h;抖动按
# policy_id 确定性派生(纯函数,可测,不引入 random);连续 3 次失败退休(等用户)。
_POLICY_FAILURE_BASE_BACKOFF_SECONDS = 300
_POLICY_FAILURE_MAX_BACKOFF_SECONDS = 3600
_POLICY_FAILURE_RETIRE_AFTER = 3
from .store import ConversationStore

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


def _consume_pending_wake_signals(
    scheduler: BackgroundMainAgentScheduler,
    reports: list[BackgroundMainAgentReport],
    current: float,
) -> set[str]:
    reported: set[str] = set()
    handled: set[str] = set()
    attempted: set[str] = set()
    wake_signals = scheduler.store.pending_wake_signals(
        limit=scheduler._config_limit("conversation_pending_wake_limit")
    )
    for signal in wake_signals:
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
    if signal.thread_id in reported and not _is_scheduler_wake_signal(signal):
        # One model turn already owns this thread for the current tick.
        return True
    if _reported_audit_finding_wake(scheduler, signal):
        # Provider receipts are the delivery authority after a publish crash.
        scheduler._mark_signal(signal, current, handled)
        return True
    if _successful_completion_waiting_for_batch(scheduler, signal, current):
        return True
    if not _wake_survives_inactive_root(signal) and _wake_signal_root_is_inactive(
        scheduler.store, signal
    ):
        # Ordinary late child lifecycle signals cannot revive an inactive root.
        scheduler._mark_signal(signal, current, handled)
        return True
    return False


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
    reports.append(report)
    if _is_scheduler_wake_signal(signal):
        return
    reported.add(report.thread_id)
    scheduler._mark_sibling_signals(wake_signals, signal, current, handled)
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
    return _select_same_reason_wake_batch(scheduler, primary, signals)


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


def _successful_completion_waiting_for_batch(
    scheduler: BackgroundMainAgentScheduler,
    signal: WakeSignal,
    current: float,
) -> bool:
    """Briefly debounce successful sibling completions; failures and blockers stay immediate."""
    if str(signal.reason or "").strip().lower() != "subagent_runner_finished":
        return False
    metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
    if str(metadata.get("status") or "").strip().upper() != "DONE":
        return False
    delay = scheduler._config_limit("background_completion_coalesce_seconds")
    created_at = float(signal.created_at or 0.0)
    return delay > 0 and 0 < created_at <= current < created_at + delay


def _same_successful_completion_batch(primary: WakeSignal, sibling: WakeSignal) -> bool:
    """Only coalesce DONE notices proved to describe the same settled task tree."""

    if primary.wake_signal_id == sibling.wake_signal_id:
        return False
    if primary.thread_id != sibling.thread_id or primary.root_task_id != sibling.root_task_id:
        return False
    if str(primary.reason or "").strip().lower() != "subagent_runner_finished":
        return False
    if str(sibling.reason or "").strip().lower() != "subagent_runner_finished":
        return False
    primary_metadata = primary.metadata if isinstance(primary.metadata, dict) else {}
    sibling_metadata = sibling.metadata if isinstance(sibling.metadata, dict) else {}
    return (
        str(primary_metadata.get("status") or "").strip().upper() == "DONE"
        and str(sibling_metadata.get("status") or "").strip().upper() == "DONE"
    )


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


def _consume_observation_batches(
    scheduler: BackgroundMainAgentScheduler,
    reports: list[BackgroundMainAgentReport],
    reported: set[str],
    current: float,
) -> None:
    pending_observations = scheduler.store.unhandled_observations_requiring_main(
        limit=scheduler._config_limit("conversation_unhandled_observation_limit")
    )
    pending_observations = [
        observation
        for observation in pending_observations
        if not _observation_waits_for_linked_wake(scheduler.store, observation)
    ]
    for thread_id, thread_observations in observations_by_thread(pending_observations).items():
        if thread_id in reported:
            continue
        report = _consume_with_supply_guard(
            scheduler._supply_backoff,
            thread_id,
            current,
            partial(scheduler._run_observation_batch, thread_id, thread_observations, now=current),
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


def _consume_due_policies(
    scheduler: BackgroundMainAgentScheduler,
    reports: list[BackgroundMainAgentReport],
    reported: set[str],
    current: float,
) -> None:
    enabled, load_errors = scheduler.store.list_progress_policies_report(enabled_only=True)
    scheduler.last_progress_policy_load_errors = load_errors
    scheduler.last_progress_policy_suppressed = []
    policies = [policy for policy in enabled if policy.next_due_at <= current]
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
        report = _consume_with_supply_guard(
            scheduler._supply_backoff,
            policy.thread_id,
            current,
            partial(scheduler._run_due_policy, policy, now=current),
        )
        if report:
            reports.append(report)


class _BackgroundSchedulerTickMixin:
    """Tick orchestration and one durable wake-signal execution."""

    def tick(self, *, now: float | None = None) -> list[BackgroundMainAgentReport]:
        current = now if now is not None else __import__("time").time()
        self._process_collaboration_cases(now=current)
        _maybe_supervise_orphans(self, current)
        self._enqueue_scheduler_runs(now=current)
        self._enqueue_unfinished_task_resume_wakes(now=current)
        reports: list[BackgroundMainAgentReport] = []
        reported = _consume_pending_wake_signals(self, reports, current)
        _consume_observation_batches(self, reports, reported, current)
        _consume_due_policies(self, reports, reported, current)
        return reports

    def _process_collaboration_cases(self, *, now: float) -> None:
        if self.collaboration_store is None:
            return
        from ..collaboration import CollaborationCoordinator

        CollaborationCoordinator(
            store=self.collaboration_store, conversation_store=self.store
        ).tick(now=now)

    def _enqueue_unfinished_task_resume_wakes(self, *, now: float) -> None:
        """未完成任务(link 权威)→ 落一条 dedupe wake,让主代理续跑(claim/执行链全复用)。

        真机:celery 复刻 RUNNING 6h 无人驱动——发现层已把 RUNNING 任务判为硬事实进池,
        但 tick 只消费 wake/policy,没有消费者会把账本变成待驱动信号,主代理永远不被
        拉起。这里把每个未完成任务 id 反查会话线程(store 的 tasks/<id>.json 链接),
        按 dedupe_key 落 wake:同一任务同一轮不重复写(已有 pending wake 命中返回),
        消费后隔 cooldown 再写 → 按节奏续跑直到任务终态。

        cooldown:子代理正在跑/主代理刚被拉起干活的窗口内不重复催促(真机 celery
        46 分钟 18 次 task_ledger_resume,大多落在主代理 turn 还在推进的空档,只有
        模型调用成本没有新信息)。距上次落 wake 不足 cooldown 的跳过,死停最长
        cooldown 内被发现(对比 H 批前 6h 无人驱动)。"""
        try:
            home = getattr(getattr(self, "runtime", None), "agent", None)
            owner_home = getattr(getattr(home, "home_paths", None), "owner_home_dir", None)
            if not owner_home:
                return
            from ..owner_wake_discovery import unfinished_task_ids

            last_raised = getattr(self, "_task_resume_last_raised", None)
            if last_raised is None:
                last_raised = {}
                self._task_resume_last_raised = last_raised
            for task_id in unfinished_task_ids(Path(owner_home)):
                try:
                    raised_at = last_raised.get(task_id)
                    if raised_at is not None and now - raised_at < _TASK_RESUME_COOLDOWN_SECONDS:
                        continue  # 冷却期:不重复催促(子代理在跑/刚催过都算在窗口内)
                    thread = self.store.thread_for_task(task_id)
                    if thread is None:
                        continue  # 会话链接未建(任务没挂到线程)→ 下轮再试
                    links, _link_errors = self.store.task_links_report(thread.thread_id)
                    if _link_errors:
                        continue
                    if any(
                        str(link.work_kind or "").strip().lower() in {"audit", "goal"}
                        for link in links
                        if str(link.task_id or "").strip() == task_id
                    ):
                        continue  # audit/goal 有自己的唤醒通道,续跑 wake 会双重拉起主代理
                    self.store.raise_wake_signal(
                        {
                            "thread_id": thread.thread_id,
                            "urgency": "normal",
                            "reason": "task_ledger_resume",
                            "root_task_id": task_id,
                            "source_agent_id": "task-ledger-resume",
                            "summary": task_id,
                            "dedupe_key": f"task-resume:{task_id}",
                        }
                    )
                    last_raised[task_id] = now
                except Exception:
                    _HEARTBEAT_LOGGER.warning("task resume wake enqueue failed", exc_info=True)
        except Exception:
            _HEARTBEAT_LOGGER.warning("task resume wake scan failed", exc_info=True)

    def _enqueue_scheduler_runs(self, *, now: float) -> None:
        if self.scheduler_service is None:
            return
        try:
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
    if lifecycle_reason == "subagent_runner_finished":
        _ensure_goal_progress_wake_chain(scheduler, signal, now=now)
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

    # LLM: Reconcile exact goal/task identity after a turn, then publish at most one deduplicated next wake.
    # 函数用途: 持续目标一轮结束后同步终态，仍 active 则继续推进同一目标。
    def _continue_thread_goal(
        self,
        signal: WakeSignal,
        *,
        report: BackgroundMainAgentReport,
        now: float,
    ) -> None:
        """Reconcile one goal turn and enqueue exactly one next turn while active."""
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
            if task_status != "active" or report.tool_call_count == 0:
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
    #   resolve_capability_requests / dispatch_subagents。失败静默记日志,唤醒轮照常进行。
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


class _BackgroundSchedulerExecutionMixin:
    """Claimed progress-policy execution, heartbeats, and runtime facts."""

    def _run_due_policy(
        self, policy: ProgressPolicy, *, now: float
    ) -> BackgroundMainAgentReport | None:
        metadata = policy.metadata if isinstance(policy.metadata, dict) else {}
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
        signature = _automatic_supervision_signature(self.runtime.agent, policy)
        previous_signature = str((policy.metadata or {}).get("material_signature") or "")
        if signature and previous_signature and signature == previous_signature:
            self.store.mark_progress_checked(
                policy.policy_id,
                now=now,
                metadata_updates={"material_signature": signature},
            )
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
            latest_signature = (
                _automatic_supervision_signature(self.runtime.agent, policy) or signature
            )
            metadata_updates = {}
            if latest_signature:
                metadata_updates["material_signature"] = latest_signature
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

    def _run_claimed(self, kwargs: dict) -> BackgroundMainAgentReport | None:
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
            self.store.finish_background_run(
                {
                    "thread_id": kwargs.get("thread_id", ""),
                    "claim_scope_id": claim_scope_id,
                    "claim_id": str(claim.get("claim_id") or ""),
                    "task_id": kwargs.get("task_id", ""),
                    "status": "cancelled",
                    "runtime_facts": {"admission": "terminal_task_link"},
                    "now": now(),
                }
            )
            _retire_terminal_background_source(self.store, kwargs)
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

    def _mark_sibling_signals(
        self,
        signals: list[WakeSignal],
        primary: WakeSignal,
        current: float,
        handled: set[str],
    ) -> None:
        for signal in signals:
            if (
                signal.wake_signal_id not in handled
                and not _is_scheduler_wake_signal(signal)
                and _same_successful_completion_batch(primary, signal)
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
    _BackgroundSchedulerTickMixin,
    _BackgroundSchedulerWakeMixin,
    _BackgroundSchedulerGoalMixin,
    _BackgroundSchedulerExecutionMixin,
):
    """Single facade over background tick, goal routing, and claimed execution."""

    def __init__(self, config: dict):
        self.runtime = config["runtime"]
        self.store = config["store"]
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


def _automatic_supervision_signature(agent: object, policy: ProgressPolicy) -> str:
    """Only automatic subagent supervision may skip an unchanged LLM turn."""
    metadata = policy.metadata if isinstance(policy.metadata, dict) else {}
    if str(metadata.get("tool") or "") != "dispatch_supervision_auto":
        return ""
    from .progress_fingerprint import subagent_material_signature

    return subagent_material_signature(
        agent,
        task_id=policy.task_id,
        watched_run_ids=metadata.get("watched_run_ids"),
    )


def _progress_policy_wake_payload(policy: ProgressPolicy) -> dict[str, object]:
    metadata = policy.metadata if isinstance(policy.metadata, dict) else {}
    return {
        "kind": "progress_policy_due",
        "reason": "scheduled_progress_report",
        "policy_id": policy.policy_id,
        "task_id": policy.task_id,
        "interval_seconds": policy.interval_seconds,
        "wait_reason": str(metadata.get("reason") or ""),
        "registered_by_tool": str(metadata.get("tool") or ""),
        "watch_run_id": str(metadata.get("watch_run_id") or ""),
    }


def _progress_policy_run_reason(policy: ProgressPolicy) -> str:
    return "scheduled_progress_report"


def _policy_failure_backoff(failures: int, policy_id: str) -> float:
    """失败退避 5min×2^(n-1) 上限 1h,抖动 0.90~1.10 按 policy_id 确定性派生。

    纯函数(无 random):同一 policy 每次失败算出同一退避,跨进程/重启可复现,测试
    可精确断言区间;抖动只做跨 policy 错峰(防多个失败 policy 同秒齐醒),不改变
    退避的量级结构。
    """
    import hashlib

    base = min(
        _POLICY_FAILURE_BASE_BACKOFF_SECONDS * (2 ** max(0, int(failures) - 1)),
        _POLICY_FAILURE_MAX_BACKOFF_SECONDS,
    )
    digest = hashlib.md5(str(policy_id).encode("utf-8")).hexdigest()
    ratio = 0.9 + (int(digest[:4], 16) % 2000) / 10000.0
    return round(base * ratio, 3)


# LLM: A plain task checklist is memory, not a lifecycle.  Only an exact active /goal may
# schedule another model turn after a child-finished wake.
# 函数用途: 显式持续目标仍有开放计划时补续跑；普通任务清单不会自行唤醒或劫持后续聊天。
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


def ensure_goal_progress_continuation(
    agent: object | None,
    *,
    task_id: str,
    thread_id: str = "",
    store: ConversationStore | None = None,
    now: float | None = None,
    due_now: bool = False,
) -> bool:
    """Keep one explicit persistent goal alive while its plan is unfinished.

    The exact goal record and task-progress ledger are both required.  An
    ordinary task can leave open progress notes without creating a future turn.
    """
    task_id = str(task_id or "").strip()
    if agent is None or not task_id:
        return False
    selected_store = store or getattr(agent, "conversation_store", None)
    if selected_store is None or ledger_open_progress_item_count(agent, task_id) <= 0:
        return False
    thread_id = str(thread_id or "").strip() or _thread_id_for_task(selected_store, task_id)
    if not thread_id:
        return False
    try:
        goal = selected_store.load_goal(thread_id, task_id=task_id)
    except Exception:
        return False
    if (
        goal is None
        or str(getattr(goal, "task_id", "") or "").strip() != task_id
        or str(getattr(goal, "status", "") or "").strip().lower() != "active"
    ):
        return False
    matching = [
        policy
        for policy in selected_store.list_progress_policies(enabled_only=True)
        if policy.task_id == task_id
    ]
    current = now if now is not None else time.time()
    if matching:
        if due_now:
            selected_store.expedite_progress_policy(
                matching[0].policy_id,
                due_at=current,
                reason="typed_unfinished_foreground",
                now=current,
            )
        return True
    interval = int(
        getattr(
            getattr(agent, "config", None),
            "dispatch_supervision_reminder_seconds",
            0,
        )
        or 0
    )
    policy = selected_store.set_progress_policy(
        {
            "thread_id": thread_id,
            "task_id": task_id,
            "interval_seconds": max(60, interval) if interval > 0 else 180,
            "route_channel": "internal",
            "route_target": "",
            "now": current,
            "metadata": {
                "kind": "subagent_progress_watch",
                "tool": "goal_progress_continuation",
                "scope": "own_task_tree",
                "reason": "显式 /goal 仍有未闭环计划项，到点继续推进；普通任务清单不创建此提醒",
                "watch_run_id": task_id,
            },
        }
    )
    if due_now:
        selected_store.expedite_progress_policy(
            policy.policy_id,
            due_at=current,
            reason="typed_unfinished_foreground",
            now=current,
        )
    return True


# LLM: A plain task (no /goal) can also leave open work at a tool-round limit or a
# soft repeated-failure closeout.  The closeout prompt promises "the runtime keeps
# the same task and continues on persistent progress"; this function makes that
# promise real for ordinary tasks by renting the same progress-policy lane, bounded
# by a resume budget so a stuck task cannot auto-burn forever.  Goal tasks stay on
# their own unbounded lane (an explicit /goal is the user's standing authorization).
# 函数用途: 普通任务(无 /goal)轮限/失败软收口后调度自动续跑,预算耗尽后退休 policy
# 并如实停等用户回复「继续」。
def ensure_ordinary_task_resume(
    agent: object | None,
    *,
    task_id: str,
    thread_id: str = "",
    store: ConversationStore | None = None,
    now: float | None = None,
    due_now: bool = False,
    limit: int | None = None,
) -> bool:
    """Schedule bounded automatic resumption for an ordinary unfinished task.

    Unlike :func:`ensure_goal_progress_continuation` this lane does not require
    a goal record nor a non-empty progress ledger: the conversation history is
    enough to resume.  The budget (``resume_used`` vs ``resume_limit``) caps
    automatic resumptions; once exhausted the policy is disabled and the
    closeout prompt tells the user to say 继续 explicitly.
    """
    task_id = str(task_id or "").strip()
    if agent is None or not task_id:
        return False
    selected_store = store or getattr(agent, "conversation_store", None)
    if selected_store is None:
        return False
    thread_id = str(thread_id or "").strip() or _thread_id_for_task(selected_store, task_id)
    if not thread_id:
        return False
    current = now if now is not None else time.time()
    resume_limit = _ordinary_task_resume_limit(agent, limit)
    matching = [
        policy
        for policy in selected_store.list_progress_policies(enabled_only=True)
        if policy.task_id == task_id
        and str((policy.metadata or {}).get("kind") or "") == "ordinary_task_resume"
    ]
    used = 0
    if matching:
        try:
            used = int((matching[0].metadata or {}).get("resume_used") or 0)
        except (TypeError, ValueError):
            used = 0
        if used >= resume_limit:
            # 预算耗尽:退休 policy,调度器不再每间隔拉起;由用户显式「继续」驱动。
            selected_store.disable_progress_policy(matching[0].policy_id, now=current)
            return False
    if matching:
        # 先顺延再 expedite 到 now:mark_progress_reported 记账 resume_used+1,
        # expedite 单调提前,最终 due=now 立即拉起。
        selected_store.mark_progress_reported(
            matching[0].policy_id,
            now=current,
            metadata_updates={"resume_used": used + 1},
        )
        selected_store.expedite_progress_policy(
            matching[0].policy_id,
            due_at=current,
            reason="ordinary_task_round_resume",
            now=current,
        )
        return True
    interval = int(
        getattr(
            getattr(agent, "config", None),
            "dispatch_supervision_reminder_seconds",
            0,
        )
        or 0
    )
    policy = selected_store.set_progress_policy(
        {
            "thread_id": thread_id,
            "task_id": task_id,
            "interval_seconds": max(60, interval) if interval > 0 else 180,
            "route_channel": "internal",
            "route_target": "",
            "now": current,
            "metadata": {
                "kind": "ordinary_task_resume",
                "tool": "task_round_resume",
                "resume_used": 1,
                "resume_limit": resume_limit,
                "reason": "普通任务轮限/失败软收口自动续跑",
            },
        }
    )
    if due_now:
        selected_store.expedite_progress_policy(
            policy.policy_id,
            due_at=current,
            reason="ordinary_task_round_resume",
            now=current,
        )
    return True


def _ordinary_task_resume_limit(agent: object | None, explicit: int | None = None) -> int:
    if explicit is not None:
        try:
            return max(1, int(explicit))
        except (TypeError, ValueError):
            pass
    try:
        configured = int(
            getattr(getattr(agent, "config", None), "ordinary_task_resume_limit", 0) or 0
        )
    except (TypeError, ValueError):
        configured = 0
    return configured if configured > 0 else 3


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
    if _is_legacy_child_watch_backstop_policy(policy):
        return "child_watch_backstop_policy"
    if _is_legacy_audit_root_poll_policy(policy):
        return "audit_root_poll_policy"
    if _is_running_durable_audit_root_policy(store, policy):
        return "durable_audit_root_policy"
    if _is_durable_audit_source_worker_policy(agent, policy):
        return "durable_audit_source_worker_policy"
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


def _is_durable_audit_source_worker_policy(
    agent: object | None,
    policy: ProgressPolicy,
) -> bool:
    """Retire generic periodic LLM polling for lease-backed Audit workers."""

    metadata = policy.metadata if isinstance(policy.metadata, dict) else {}
    if str(metadata.get("tool") or "") != "dispatch_supervision_auto":
        return False
    watched = metadata.get("watched_run_ids")
    run_ids = [str(item) for item in watched] if isinstance(watched, list) else []
    manager = getattr(agent, "subagents", None)
    loader = getattr(manager, "load", None)
    if not run_ids or not callable(loader):
        return False
    from ..common.audit_activation import AUDIT_SOURCE_WORKER_ATTR

    for run_id in run_ids:
        try:
            task = loader(run_id)
        except Exception:
            return False
        attrs = getattr(task, "attributes", None)
        if not isinstance(attrs, dict) or attrs.get(AUDIT_SOURCE_WORKER_ATTR) is not True:
            return False
    return True


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
        "durable_audit_source_worker_policy",
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
