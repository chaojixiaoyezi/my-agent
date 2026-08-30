
from __future__ import annotations

import json
from typing import Any

from ...conversation.active_turn_input import append_active_turn_user_input, packet_from_guidance
from ...conversation.authority import (
    CONVERSATION_BACKGROUND_WAKE_SIGNAL_IDS_ATTR,
    CONVERSATION_BACKGROUND_WAKE_SNAPSHOT_IDS_ATTR,
)
from ...conversation.models import SUBAGENT_LIFECYCLE_WAKE_REASONS, WakeSignal
from ...runtime_errors import runtime_error_report
from ...subagents.models import SUBAGENT_ENDED_STATUSES, task_status_in
from ..runner.context import current_subagent_run_id
from .task_identity import durable_task_id

_TASK_EVENT_LIMIT = 20
_DIRECT_CHILDREN_MARKER = "[RUNTIME_DIRECT_CHILDREN]"


# LLM: This volatile suffix is rebuilt from canonical direct-child rows at every provider safe
# point. Keep it small, deterministic and free of prose-derived lifecycle decisions so compacted
# history can never outrank current typed state or invalidate the stable prompt prefix needlessly.
# 函数用途: 在每次模型调用前刷新直属子代理状态；状态不变时字节不变，变化时原位替换旧快照。
def refresh_runtime_direct_children_snapshot(agent: object, params: object) -> bool:
    runtime_injections = getattr(params, "runtime_injections", None)
    if not isinstance(runtime_injections, list):
        return False
    snapshot = _render_runtime_direct_children(agent, params)
    indexes = [
        index
        for index, item in enumerate(runtime_injections)
        if str(item or "").startswith(_DIRECT_CHILDREN_MARKER)
    ]
    previous = list(runtime_injections)
    if snapshot:
        if indexes:
            runtime_injections[indexes[0]] = snapshot
            for index in reversed(indexes[1:]):
                runtime_injections.pop(index)
        else:
            runtime_injections.append(snapshot)
    else:
        for index in reversed(indexes):
            runtime_injections.pop(index)
    return runtime_injections != previous


# LLM: Parent identity comes only from the runner scope or durable task id. Canonical run rows are
# filtered by exact parent_id; root lineage, names, goals and summaries never establish ownership.
# 函数用途: 读取当前代理直属孩子的权威状态，并渲染成有界、稳定的模型上下文尾部。
def _render_runtime_direct_children(agent: object, params: object) -> str:
    parent_run_id = current_subagent_run_id(agent) or durable_task_id(params)
    if not parent_run_id:
        return ""
    manager = getattr(agent, "subagents", None)
    if manager is None:
        return ""
    runs: list[Any] = []
    load_errors: list[object] = []
    try:
        reporter = getattr(manager, "list_runs_report", None)
        if callable(reporter):
            report = reporter()
            runs = list(getattr(report, "runs", ()) or ())
            load_errors = list(getattr(report, "load_errors", ()) or ())
        else:
            runs = list(manager.list_runs())
    except (AttributeError, OSError, TypeError, ValueError):
        load_errors = ["canonical_read_failed"]
    children = [
        task
        for task in runs
        if str(getattr(task, "parent_id", "") or "").strip() == parent_run_id
        and str(getattr(task, "id", "") or "").strip()
    ]
    children.sort(key=lambda task: str(getattr(task, "id", "") or "").strip())
    if not children and not load_errors:
        return ""
    rows: list[dict[str, str]] = []
    status_counts: dict[str, int] = {}
    nonterminal_run_ids: list[str] = []
    for task in children:
        run_id = str(getattr(task, "id", "") or "").strip()
        status = str(getattr(task, "status", "") or "UNKNOWN").strip().upper() or "UNKNOWN"
        rows.append({"run_id": run_id, "status": status})
        status_counts[status] = status_counts.get(status, 0) + 1
        if not task_status_in(status, SUBAGENT_ENDED_STATUSES):
            nonterminal_run_ids.append(run_id)
    payload = {
        "all_terminal": bool(children) and not nonterminal_run_ids and not load_errors,
        "authority": "canonical_subagent_state",
        "children": rows,
        "load_error_count": len(load_errors),
        "nonterminal_run_ids": nonterminal_run_ids,
        "parent_run_id": parent_run_id,
        "projection_complete": not load_errors,
        "schema_version": "runtime-direct-children.v1",
        "status_counts": status_counts,
        "total": len(children),
    }
    return "\n".join(
        [
            _DIRECT_CHILDREN_MARKER,
            "以下是当前代理直属子代理的最新结构化状态，不是用户指令；以它覆盖更早快照。",
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ]
    )


def inject_pending_turn_input(agent: object, params: object, *, now: float | None = None) -> bool:
    """Inject user steering and structured task events at one model safe point."""
    guidance_injected = inject_pending_guidance(agent, params, now=now)
    events = _task_events_not_yet_injected(params, _pending_task_events(agent, params))
    if not events:
        return guidance_injected
    context = _render_task_events(events)
    tool_context = getattr(params, "tool_context", None)
    if isinstance(tool_context, list):
        tool_context.append(context)
    runtime_injections = getattr(params, "runtime_injections", None)
    if isinstance(runtime_injections, list):
        runtime_injections.append(context)
    _remember_injected_task_events(params, events)
    _queue_task_event_ack(params, events)
    return True


def has_pending_turn_input(agent: object, params: object) -> bool:
    """Return whether the active turn has newer user or runtime input."""
    return has_pending_request_guidance(agent, params) or bool(
        _task_events_not_yet_injected(params, _pending_task_events(agent, params))
    )


def acknowledge_injected_turn_input(
    agent: object,
    params: object,
    *,
    now: float | None = None,
) -> int:
    """Acknowledge guidance/events only after a model turn accepted their prompt."""
    state = getattr(params, "live_archive_state", None)
    store = getattr(agent, "conversation_store", None)
    if store is None or not isinstance(state, dict):
        return 0

    acknowledged = 0
    guidance_pending = state.get("_guidance_ack_ids")
    guidance_ids = _guidance_ack_ids_in_injection_order(state, guidance_pending)
    if guidance_ids:
        entries = _guidance_ack_entries(state, guidance_ids)
        acknowledged_entries = [
            entries[guidance_id]
            for guidance_id in guidance_ids
            if entries.get(guidance_id) is not None
        ]
        delivered_ids: list[str] = []
        if acknowledged_entries:
            turn_id = str(getattr(params, "request_id", "") or "").strip() or durable_task_id(
                params
            )
            def consume() -> list[str]:
                return list(
                    store.consume_submitted_guidance_for_turn(
                        turn_id,
                        acknowledged_entries,
                        provider_call_id=str(
                            state.get("_guidance_submission_id") or ""
                        ).strip(),
                        now=now,
                    )
                )

            delivered_ids = list(
                _run_active_turn_transition(params, "acknowledge", consume)
            )
            consumed_entries = [
                entry
                for entry in acknowledged_entries
                if str(getattr(entry, "guidance_id", "") or "") in delivered_ids
            ]
            for entry in consumed_entries:
                _persist_guidance_transcript(store, entry)
            _complete_active_turn_user_reply_segment(
                params,
                _guidance_client_message_ids(consumed_entries),
            )
        if delivered_ids:
            guidance_pending.difference_update(delivered_ids)
            _forget_guidance_ack_entries(state, delivered_ids)
            state.pop("_guidance_submission_id", None)
            acknowledged += len(delivered_ids)

    event_pending = state.get("_task_event_ack_ids")
    event_ids = (
        sorted(str(item) for item in event_pending if str(item or "").strip())
        if isinstance(event_pending, set)
        else []
    )
    for event_id in event_ids:
        store.mark_wake_signal_handled(event_id, now=now)
    if isinstance(event_pending, set):
        event_pending.difference_update(event_ids)
    acknowledged += len(event_ids)
    return acknowledged


# LLM: Prompt assembly only reserves guidance. This explicit edge advances the exact batch to
# submitted immediately before a provider call, separating safe pre-call crashes from unknown I/O.
# 函数用途: 在模型请求发出前，将当前尝试已注入的补充消息批量标记为开始提交。
def mark_injected_turn_input_submitted(
    agent: object,
    params: object,
    *,
    provider_call_id: str = "",
    now: float | None = None,
) -> int:
    state = getattr(params, "live_archive_state", None)
    store = getattr(agent, "conversation_store", None)
    if store is None or not isinstance(state, dict):
        return 0
    guidance_pending = state.get("_guidance_ack_ids")
    guidance_ids = _guidance_ack_ids_in_injection_order(state, guidance_pending)
    entries = _guidance_ack_entries(state, guidance_ids)
    submitted_entries = [
        entries[guidance_id]
        for guidance_id in guidance_ids
        if entries.get(guidance_id) is not None
    ]
    if not submitted_entries:
        return 0
    turn_id = str(getattr(params, "request_id", "") or "").strip() or durable_task_id(params)
    attempt_id = (
        str(getattr(params, "attempt_id", "") or "").strip()
        or str(getattr(params, "run_id", "") or "").strip()
        or turn_id
    )
    def submit() -> int:
        count = len(
            store.mark_guidance_entries_submitted(
                turn_id,
                submitted_entries,
                attempt_id=attempt_id,
                provider_call_id=provider_call_id,
                now=now,
            )
        )
        if count and str(provider_call_id or "").strip():
            state["_guidance_submission_id"] = str(provider_call_id).strip()
        return count

    return int(_run_active_turn_transition(params, "submit", submit))


# LLM: A typed provider context rejection proves no prompt execution. Only that caller may restore
# this attempt's submitted rows to reserved for a smaller retry; transport failures never call it.
# 函数用途: 模型明确拒绝当前上下文时，将本批补充消息恢复为同一尝试可再次提交。
def restore_injected_turn_input_for_provider_retry(agent: object, params: object) -> int:
    state = getattr(params, "live_archive_state", None)
    store = getattr(agent, "conversation_store", None)
    if store is None or not isinstance(state, dict):
        return 0
    pending = state.get("_guidance_ack_ids")
    guidance_ids = _guidance_ack_ids_in_injection_order(state, pending)
    entries = _guidance_ack_entries(state, guidance_ids)
    prepared = [entries[item] for item in guidance_ids if entries.get(item) is not None]
    if not prepared:
        return 0
    turn_id = str(getattr(params, "request_id", "") or "").strip() or durable_task_id(params)
    attempt_id = (
        str(getattr(params, "attempt_id", "") or "").strip()
        or str(getattr(params, "run_id", "") or "").strip()
        or turn_id
    )
    provider_call_id = str(state.get("_guidance_submission_id") or "").strip()
    def restore() -> int:
        return len(
            store.restore_submitted_guidance_for_retry(
                turn_id,
                prepared,
                attempt_id=attempt_id,
                provider_call_id=provider_call_id,
            )
        )

    restored = int(_run_active_turn_transition(params, "restore", restore))
    if restored:
        state.pop("_guidance_submission_id", None)
    return restored


# LLM: A compact continuation starts a fresh execution attempt. After the old attempt has returned,
# reserved rows are provably pre-provider and must go back to pending instead of carrying bare text.
# 函数用途: 在 Compact 重开模型循环前释放本尝试未提交的补充消息，并返回其结构化 ID。
def release_reserved_turn_input_after_attempt(
    agent: object,
    params: object,
) -> tuple[str, ...]:
    state = getattr(params, "live_archive_state", None)
    pending = state.get("_guidance_ack_ids") if isinstance(state, dict) else None
    guidance_ids = tuple(
        _guidance_ack_ids_in_injection_order(
            state if isinstance(state, dict) else {},
            pending,
        )
    )
    # No receipt was reserved in this attempt, so acquiring the Gateway turn lock would turn a
    # normal compact into an exact-turn lifecycle error in direct/fake runtimes.
    if not guidance_ids:
        return ()
    store = getattr(agent, "conversation_store", None)
    release = getattr(store, "release_reserved_guidance_for_turn", None)
    if not callable(release):
        return ()
    turn_id = str(getattr(params, "request_id", "") or "").strip() or durable_task_id(params)
    if not turn_id:
        return ()
    attempt_id = (
        str(getattr(params, "attempt_id", "") or "").strip()
        or str(getattr(params, "run_id", "") or "").strip()
        or turn_id
    )

    def release_reserved() -> tuple[str, ...]:
        summary = release(turn_id, dead_attempt_id=attempt_id)
        ids = summary.get("released_guidance_ids", []) if isinstance(summary, dict) else []
        return tuple(str(item) for item in ids if str(item or "").strip())

    return tuple(_run_active_turn_transition(params, "release", release_reserved))


def inject_pending_guidance(agent: object, params: object, *, now: float | None = None) -> bool:
    store = getattr(agent, "conversation_store", None)
    if store is None:
        return False
    entries = []
    request_id = str(getattr(params, "request_id", "") or "").strip()
    run_id = str(getattr(params, "run_id", "") or "").strip()
    task_id = durable_task_id(params)
    if request_id:
        entries.extend(store.pending_guidance("request", request_id, limit=20))
    if task_id and task_id != request_id:
        # A steer can arrive while the foreground gateway request is still live,
        # before its durable task binding becomes the selected control target.
        # A later durable turn has a new request id but keeps the original task
        # identity. Read the original request inbox by that exact task id so a
        # crash or turn boundary cannot strand an unacknowledged steer.
        entries.extend(store.pending_guidance("request", task_id, limit=20))
    if run_id:
        entries.extend(store.pending_guidance("agent_run", run_id, limit=20))
    if task_id:
        # /btw 与 会话运行时 steer 一致：绑定当前执行中的持久任务，按 FIFO 在下一安全点
        # 作为新输入投递一次。未被消费前可跨崩溃保留；一旦 delivered，不再当作
        # 新输入回放；确认后由同一 thread transcript 在后续 turn 中保留。
        entries.extend(store.pending_guidance("task", task_id, limit=20))
    thread_id, thread_lookup_error = _thread_id_for_task(store, task_id)
    if thread_id:
        entries.extend(store.pending_guidance("thread", thread_id, limit=20))
    entries = _guidance_not_yet_injected(params, _dedupe_guidance(entries))
    warning = _render_guidance_lookup_error(thread_lookup_error)
    turn_id = request_id or task_id
    tool_context = getattr(params, "tool_context", None)
    if entries and turn_id:
        attempt_id = (
            str(getattr(params, "attempt_id", "") or "").strip()
            or run_id
            or turn_id
        )
        def reserve_and_inject() -> list[Any]:
            with store.guidance_turn_transition_guard(turn_id):
                claimed = _claim_guidance_for_active_turn(
                    store,
                    entries,
                    turn_id,
                    attempt_id,
                )
                _inject_claimed_guidance(params, claimed, tool_context)
                return claimed

        entries = list(_run_active_turn_transition(params, "reserve", reserve_and_inject))
    if warning and isinstance(tool_context, list):
        tool_context.append(warning)
    runtime_injections = getattr(params, "runtime_injections", None)
    if warning and isinstance(runtime_injections, list):
        runtime_injections.append(warning)
    return bool(entries or warning)


# LLM: Gateway provides the outer exact-turn lock while ConversationStore owns the inner mailbox
# lock. Keeping this one callback boundary enforces T -> M for both reserve and submit.
# 函数用途: 在宿主提供的精确回合转换锁内执行补充消息状态变更；普通本地运行直接执行。
def _run_active_turn_transition(params: object, phase: str, operation):
    callback = getattr(params, "active_turn_transition_callback", None)
    if callable(callback):
        return callback(str(phase), operation)
    return operation()


# LLM: Caller holds the exact turn transition guard from receipt claim through every prompt/state
# mutation. Terminalization cannot reject or archive between claim and this local admission edge.
# 函数用途: 把已认领补充消息加入当前模型提示，并登记稍后的消费确认。
def _inject_claimed_guidance(
    params: object,
    entries: list[Any],
    tool_context: object,
) -> None:
    if not entries:
        return
    user_input = _render_guidance_user_input(entries)
    if user_input:
        # 会话运行时 steer is a real user turn, not a system/runtime hint.  Keep one
        # chronological text marker for the text protocol and one provider-neutral
        # UserTurn for native messages.  The latter remains visible after later tool
        # rounds instead of disappearing after the first sampling request.
        if isinstance(tool_context, list):
            tool_context.append(f"[ACTIVE_TURN_USER_INPUT]\n{user_input}")
        from ..native_tool_protocol import native_tool_use_active

        if native_tool_use_active(params):
            from ..tool_ir_history import record_user_turn_ir

            record_user_turn_ir(params, user_input)
        append_active_turn_user_input(params, packet_from_guidance(entries, user_input))
        _begin_active_turn_user_reply_segment(
            params,
            _guidance_client_message_ids(entries),
        )
    _remember_injected_guidance(params, entries)
    _queue_guidance_ack(params, entries)


# LLM: A request/task steer arriving during model generation invalidates that stale model action.
# 函数用途：检查当前一次请求或持久任务是否有新引导，不消费、不改变提示账本。
def has_pending_request_guidance(agent: object, params: object) -> bool:
    store = getattr(agent, "conversation_store", None)
    request_id = str(getattr(params, "request_id", "") or "").strip()
    task_id = durable_task_id(params)
    if store is None or not (request_id or task_id):
        return False
    try:
        entries = []
        if request_id:
            entries.extend(store.pending_guidance("request", request_id, limit=1))
        if task_id and task_id != request_id:
            entries.extend(store.pending_guidance("request", task_id, limit=1))
        if task_id:
            entries.extend(store.pending_guidance("task", task_id, limit=1))
        candidates = _guidance_not_yet_injected(params, _dedupe_guidance(entries))
        turn_id = request_id or task_id
        return any(
            store.guidance_available_for_turn(entry, expected_turn_id=turn_id)
            for entry in candidates
        )
    except Exception:
        return False


# LLM: Claim is the durable equivalent of 会话运行时 appending into the exact active turn_state under
# its active-turn lock. Rejected or stale-turn receipts are filtered before any prompt mutation.
# 函数用途: 在模型安全点原子认领属于当前精确回合的补充消息。
def _claim_guidance_for_active_turn(
    store: object,
    entries: list[Any],
    turn_id: str,
    attempt_id: str,
) -> list[Any]:
    claimed: list[Any] = []
    for entry in entries:
        if store.claim_guidance_once_for_turn(
            entry,
            expected_turn_id=turn_id,
            attempt_id=attempt_id,
        ):
            claimed.append(entry)
    return claimed


# LLM: User steering may arrive after the run's first commentary; only the Gateway stream
# sink owns whether another user-visible model segment can be emitted.
# 函数用途：通知当前输出流“这是新的真实用户输入”，让当前轮可再自然回复一次。
def _begin_active_turn_user_reply_segment(
    params: object,
    client_message_ids: tuple[str, ...],
) -> None:
    sink = getattr(params, "effective_on_chunk", None)
    if sink is None:
        sink = getattr(params, "on_chunk", None)
    begin = getattr(sink, "begin_active_turn_input", None)
    if callable(begin):
        begin(client_message_ids)


# LLM: The consumed UI event belongs to the provider-accepted prompt boundary, not the earlier
# prompt assembly/claim edge. Only the sink may publish that exact client correlation event.
# 函数用途: 模型确认收到补充输入后，通知输出流发布真正的已消费事件。
def _complete_active_turn_user_reply_segment(
    params: object,
    client_message_ids: tuple[str, ...],
) -> None:
    sink = getattr(params, "effective_on_chunk", None)
    if sink is None:
        sink = getattr(params, "on_chunk", None)
    complete = getattr(sink, "complete_active_turn_input", None)
    if callable(complete):
        complete(client_message_ids)


# LLM: Client correlation reads only the structured channel_message_id written by ingress;
# guidance text and ordering labels never become identity.
# 函数用途: 提取本批已注入补充消息的客户端 ID，供同一 TUI 精确收起等待提示。
def _guidance_client_message_ids(entries: list[Any]) -> tuple[str, ...]:
    result: list[str] = []
    for entry in entries:
        metadata = getattr(entry, "metadata", None)
        metadata = metadata if isinstance(metadata, dict) else {}
        message_id = str(metadata.get("channel_message_id") or "").strip()
        if message_id and message_id not in result:
            result.append(message_id)
    return tuple(result)


# LLM: Parent lifecycle wakes are an exact receiver mailbox, matching 会话运行时's direct
# parent-thread delivery. Task-local/control-plane/isolated turns may share root lineage but
# must never inspect or acknowledge the root main agent's completion queue.
# 函数用途: 读取只属于当前主代理会话任务的子代理完成事件，防止运行中的兄弟子代理偷走通知。
def _pending_task_events(agent: object, params: object) -> list[WakeSignal]:
    store = getattr(agent, "conversation_store", None)
    task_id = _parent_lifecycle_mailbox_task_id(params)
    if store is None or not task_id:
        return []
    try:
        # Filter by the exact durable task before applying the prompt batch cap;
        # another task's backlog must not hide this turn's event behind a global limit.
        signals, load_errors = store.pending_wake_signals_report(limit=0)
    except Exception:
        return []
    if load_errors:
        return []
    excluded_ids = _active_background_wake_signal_ids(params)
    matching = [
        signal
        for signal in signals
        if signal.root_task_id == task_id
        and str(signal.reason or "").strip().lower() in SUBAGENT_LIFECYCLE_WAKE_REASONS
        and signal.wake_signal_id not in excluded_ids
    ]
    return matching[:_TASK_EVENT_LIMIT]


# LLM: Root lineage identifies ancestry, not mailbox ownership. Only a main conversation
# execution owns root-task lifecycle input; direct child steering remains independently routed
# by the agent_run guidance mailbox and is intentionally unaffected by this guard.
# 函数用途: 判断本轮是否有权读取父级任务收件箱，并返回其唯一持久任务编号。
def _parent_lifecycle_mailbox_task_id(params: object) -> str:
    scope = str(getattr(params, "context_scope", "") or "default").strip().lower()
    if scope not in {"", "default", "conversation"}:
        return ""
    return durable_task_id(params)


# LLM: A coalesced background turn already owns every typed id in its batch;
# reinjecting siblings as "new" task events duplicates mail and may consume the
# same completion twice. Legacy turns still expose only the singular id.
# 函数用途: 读取本轮已纳入提示的唤醒编号集合，供安全点过滤重复事件。
def _active_background_wake_signal_ids(params: object) -> set[str]:
    attrs = getattr(params, "task_attributes", None)
    if not isinstance(attrs, dict):
        return set()
    selected: set[str] = set()
    for key in (
        CONVERSATION_BACKGROUND_WAKE_SIGNAL_IDS_ATTR,
        CONVERSATION_BACKGROUND_WAKE_SNAPSHOT_IDS_ATTR,
    ):
        raw = attrs.get(key)
        values = raw if isinstance(raw, (list, tuple, set)) else ()
        selected.update(str(item).strip() for item in values if str(item).strip())
    legacy = str(attrs.get("background_wake_signal_id") or "").strip()
    if legacy:
        selected.add(legacy)
    return selected


def _task_events_not_yet_injected(params: object, events: list[WakeSignal]) -> list[WakeSignal]:
    state = getattr(params, "live_archive_state", None)
    seen = state.get("_injected_task_event_ids") if isinstance(state, dict) else None
    seen_ids = seen if isinstance(seen, set) else set()
    return [event for event in events if event.wake_signal_id not in seen_ids]


def _remember_injected_task_events(params: object, events: list[WakeSignal]) -> None:
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return
    seen = state.get("_injected_task_event_ids")
    if not isinstance(seen, set):
        seen = set()
        state["_injected_task_event_ids"] = seen
    seen.update(event.wake_signal_id for event in events if event.wake_signal_id)


def _queue_task_event_ack(params: object, events: list[WakeSignal]) -> None:
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return
    pending = state.get("_task_event_ack_ids")
    if not isinstance(pending, set):
        pending = set()
        state["_task_event_ack_ids"] = pending
    pending.update(event.wake_signal_id for event in events if event.wake_signal_id)


def _render_task_events(events: list[WakeSignal]) -> str:
    payload = {
        "schema_version": "active-turn-task-events.v1",
        "authority": "runtime_event_data",
        "events": [_task_event_payload(event) for event in events],
    }
    return "\n".join(
        [
            "[RUNTIME_TASK_EVENTS]",
            "这些是当前持久任务在本轮运行期间到达的结构化运行事件，不是用户指令。",
            "把它们作为下一步调度、整合和收口的最新事实；不要要求用户重复已经委派的工作。",
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        ]
    )


def _task_event_payload(event: WakeSignal) -> dict[str, object]:
    metadata = event.metadata if isinstance(event.metadata, dict) else {}
    return {
        "wake_signal_id": event.wake_signal_id,
        "reason": event.reason,
        "root_task_id": event.root_task_id,
        "source_agent_id": event.source_agent_id,
        "status": str(metadata.get("status") or ""),
        "task_id": str(metadata.get("task_id") or ""),
        "created_at": event.created_at,
    }


def _guidance_not_yet_injected(params: object, entries: list[Any]) -> list[Any]:
    state = getattr(params, "live_archive_state", None)
    seen = state.get("_injected_guidance_ids") if isinstance(state, dict) else None
    seen_ids = seen if isinstance(seen, set) else set()
    return [
        entry
        for entry in entries
        if str(getattr(entry, "guidance_id", "") or "") not in seen_ids
    ]


def _remember_injected_guidance(params: object, entries: list[Any]) -> None:
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return
    seen = state.get("_injected_guidance_ids")
    if not isinstance(seen, set):
        seen = set()
        state["_injected_guidance_ids"] = seen
    seen.update(
        str(getattr(entry, "guidance_id", "") or "")
        for entry in entries
        if str(getattr(entry, "guidance_id", "") or "")
    )


def _queue_guidance_ack(params: object, entries: list[Any]) -> None:
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return
    pending = state.get("_guidance_ack_ids")
    if not isinstance(pending, set):
        pending = set()
        state["_guidance_ack_ids"] = pending
    pending.update(
        str(getattr(entry, "guidance_id", "") or "")
        for entry in entries
        if str(getattr(entry, "guidance_id", "") or "")
    )
    ack_entries = state.get("_guidance_ack_entries")
    if not isinstance(ack_entries, dict):
        ack_entries = {}
        state["_guidance_ack_entries"] = ack_entries
    ack_entries.update(
        {
            str(getattr(entry, "guidance_id", "") or ""): entry
            for entry in entries
            if str(getattr(entry, "guidance_id", "") or "")
        }
    )


# LLM: The pending set is membership authority while the insertion-ordered entry
# map preserves the FIFO sequence injected into the provider prompt. Unknown
# legacy ids are appended deterministically and cannot reorder known user input.
# 函数用途: 按实际注入顺序取出待提交或待确认的插话 ID。
def _guidance_ack_ids_in_injection_order(
    state: dict[str, object],
    pending: object,
) -> list[str]:
    if not isinstance(pending, (set, list, tuple)):
        return []
    pending_ids = {
        str(item or "").strip()
        for item in pending
        if str(item or "").strip()
    }
    if not pending_ids:
        return []
    entries = state.get("_guidance_ack_entries")
    ordered = (
        [str(item) for item in entries if str(item) in pending_ids]
        if isinstance(entries, dict)
        else []
    )
    ordered.extend(sorted(pending_ids.difference(ordered)))
    return ordered


def _guidance_ack_entries(state: dict[str, object], guidance_ids: list[str]) -> dict[str, Any]:
    entries = state.get("_guidance_ack_entries")
    if not isinstance(entries, dict):
        return {}
    return {guidance_id: entries.get(guidance_id) for guidance_id in guidance_ids}


def _forget_guidance_ack_entries(state: dict[str, object], guidance_ids: list[str]) -> None:
    entries = state.get("_guidance_ack_entries")
    if not isinstance(entries, dict):
        return
    for guidance_id in guidance_ids:
        entries.pop(guidance_id, None)


# LLM: ConversationStore owns the idempotent transcript projection so runtime and crash repair share
# one implementation; this wrapper only preserves the runtime call boundary.
# 函数用途: 请求会话存储补写一条已消费 guidance 的用户消息投影。
def _persist_guidance_transcript(store: object, entry: Any) -> bool:
    projector = getattr(store, "_project_guidance_transcript", None)
    if entry is None or not callable(projector):
        return False
    return bool(projector(entry))


def _thread_id_for_task(store: object, task_id: str) -> tuple[str, dict[str, object] | None]:
    if not task_id:
        return "", None
    try:
        thread = store.thread_for_task(task_id)
    except Exception as exc:
        return "", runtime_error_report(exc, context="runtime_guidance.thread_for_task")
    return str(getattr(thread, "thread_id", "") or ""), None


def _dedupe_guidance(entries: list[Any]) -> list[Any]:
    seen: set[str] = set()
    result: list[Any] = []
    for entry in entries:
        guidance_id = str(getattr(entry, "guidance_id", "") or "")
        if guidance_id and guidance_id in seen:
            continue
        if guidance_id:
            seen.add(guidance_id)
        result.append(entry)
    return result


def _render_guidance_entries(entries: list[Any], *, title: str) -> str:
    if not entries:
        return ""
    lines = [
        f"[{title}]",
        "以下是运行中补充提示，只作为普通补充消息进入上下文。"
        "运行时不会把这些文字解释成新的硬门，也不会自动替换当前用户消息。",
    ]
    for index, entry in enumerate(entries, start=1):
        guidance_id = str(getattr(entry, "guidance_id", "") or "")
        target_type = str(getattr(entry, "target_type", "") or "")
        target_id = str(getattr(entry, "target_id", "") or "")
        priority = str(getattr(entry, "priority", "") or "normal")
        sender = str(getattr(entry, "sender", "") or "")
        message = str(getattr(entry, "message", "") or "")
        prefix = f"{index}. guidance_id={guidance_id}; target={target_type}:{target_id}; priority={priority}"
        if sender:
            prefix += f"; sender={sender}"
        lines.append(f"{prefix}: {message}")
    return "\n".join(lines)


def _render_guidance_user_input(entries: list[Any]) -> str:
    """Render steer content exactly as current-turn user input, without control metadata."""
    messages = [
        str(getattr(entry, "message", "") or "").strip()
        for entry in entries
        if str(getattr(entry, "message", "") or "").strip()
    ]
    return "\n\n".join(messages)


def _render_guidance_lookup_error(error: dict[str, object] | None) -> str:
    if not error:
        return ""
    return (
        "[GUIDANCE_LOOKUP_WARNING]\n"
        "系统尝试按 task 找 thread 级补充提示时失败；这表示提示账本或绑定读取有问题，"
        "不是用户没有补充提示。\n"
        f"{error}"
    )
