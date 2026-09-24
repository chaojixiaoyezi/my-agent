# LLM: 本模块负责后台模型上下文准备，复用唯一会话与任务范围事实，不调度执行或外发消息。
# task_runtime_state 的既有进度对账可能写任务账；保持调用顺序，修改时同步历史隔离、读取错误和展示排除测试。
# 模块用途: 一次读取后台事实后纯渲染有界上下文，候选不重复进度对账或读取持久状态。
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..agent_core.agent_tree.status import agent_tree_status_payload
from ..artifacts.registry import latest_artifact_records
from ..contracts.subagent_completion import (
    DEFAULT_VISIBLE_SUBAGENT_COMPLETIONS,
    subagent_completion_context_from_observations,
)
from ..runtime_errors import runtime_error_report
from ..settings.defaults import default_config_value
from ..subagents.role_templates import active_model_subagent_tools
from .background_tool_policy import (
    BackgroundToolPolicyRequest,
    background_allowed_tools,
    background_control_action_lines,
    background_tool_policy_decision,
    tool_names,
)
from .context_budget import (
    BackgroundContextPayloadRequest,
    background_context_budget_from_config,
    bounded_background_context_payload,
)
from .models import ConversationThread
from .store import ConversationStore
from .task_runtime_state import task_runtime_state


# LLM: 这是上下文准备的结构化读取接口，不保存状态、不要求生产请求继承；调用方提供已裁决的身份与唤醒。
# 类用途: 明确准备上下文所需的四个请求字段，避免该模块依赖调度执行器或完整 Agent 请求对象。
class BackgroundContextRequest(Protocol):
    thread_id: str
    task_id: str
    reason: str
    wake_signal: dict[str, Any] | None


# LLM: 仅格式化给模型的既有投影，保持键排序与 Unicode；不决定字段可信度或持久状态。
# 函数用途: 把已筛选的结构化事实显示为稳定 JSON 段，便于复用请求前缀。
def json_block(value: Any) -> str:
    return "```json\n" + json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n```"


# LLM: 只读指定线程的待处理事件，limit 沿 store 合同；不消费或确认唤醒。
# 函数用途: 为仍使用基本读取接口的调用方取得唤醒副本，身份筛选不依赖提示文字。
def pending_wake_payload(
    store: ConversationStore, thread_id: str, *, limit: int
) -> list[dict[str, Any]]:
    return [
        item.to_dict()
        for item in store.wakes.pending(limit=limit)
        if item.thread_id == thread_id
    ]


# LLM: 持久 wake 账本可以保留旧策略快照用于审计，但投给模型的副本必须删掉
# 已退役控制工具，避免模型从 Active/Pending Wake Signal 里重新学会轮询或手工派工。
# 函数用途: 生成不含退役子代理工具名的模型可见 wake signal 副本。
def model_visible_wake_signal(signal: object) -> dict[str, Any] | None:
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


# LLM: 承载一次上下文准备的显式依赖与错误集合，不缓存跨片状态；policy_request 在历史读取路径可缺省。
# 类用途: 把 store、线程和任务范围交给各投影函数，共享本次读取错误。
@dataclass(frozen=True)
class BackgroundContextLoad:
    agent: object
    store: ConversationStore
    thread: ConversationThread
    task_id: str
    config: object | None
    policy_request: BackgroundToolPolicyRequest | None
    load_errors: list[dict[str, Any]]


# LLM: 仅保存一次已读取的后台材料；不授予身份或执行权，不进入持久状态，候选渲染不能重新触发进度写账。
# 类用途: 把副作用准备与纯格式化分开，保留原预算、控制策略及窄审计隔离事实。
@dataclass(frozen=True)
class PreparedBackgroundContext:
    payload: BackgroundContextPayloadRequest = field(repr=False)
    header: tuple[str, ...]
    control_policy: dict[str, Any]
    control_actions: tuple[str, ...]
    task_id: str
    narrow_audit_event: bool
    include_recent_messages: bool

    # LLM: 复制嵌套事实以隔离原store/调用方后续修改；不读盘、不创建新持久快照。
    # 函数用途: 固定本次准备内容，保证重复纯渲染不会受借用容器变化影响。
    def __post_init__(self):
        object.__setattr__(self, "payload", deepcopy(self.payload))
        object.__setattr__(self, "control_policy", deepcopy(self.control_policy))


# LLM: 原一次性调用保持准备与渲染的顺序；需要重复投影的宿主应持有prepare返回值，不能重复调用本入口。
# 函数用途: 为普通后台调用生成完整上下文，实际读取和可能的进度写入只在prepare阶段发生。
def context_markdown(
    *, agent: object, store: ConversationStore, thread: ConversationThread,
    request: BackgroundContextRequest, proactive_delivery_available: bool | None = None,
    include_recent_messages: bool = True,
) -> str:
    return render_background_context(prepare_background_context(
        agent=agent, store=store, thread=thread, request=request,
        proactive_delivery_available=proactive_delivery_available, include_recent_messages=include_recent_messages,
    ))


# LLM: 原wake净化和task_runtime_state对账仅执行一次；宿主可交同次历史已读bundle避免重读范围，返回预算/策略不在候选间刷新。
# 函数用途: 收集后台请求所需事实；可能更新原进度账，后续render无副作用。
def prepare_background_context(
    *, agent: object, store: ConversationStore, thread: ConversationThread,
    request: BackgroundContextRequest, proactive_delivery_available: bool | None = None,
    include_recent_messages: bool = True, context_bundle: dict[str, Any] | None = None,
) -> PreparedBackgroundContext:
    policy_request = tool_policy_request(agent, request, proactive_delivery_available=proactive_delivery_available)
    task_id = str(getattr(request, "task_id", "") or "").strip()
    payload = _prepare_context_payload(
        agent, store, thread, task_id, policy_request,
        active_wake_signal=model_visible_wake_signal(getattr(request, "wake_signal", None)),
        context_bundle=context_bundle,
    )
    return PreparedBackgroundContext(
        payload=payload, header=tuple(_context_header(request, thread)),
        control_policy=background_tool_policy_decision(getattr(agent, "config", None), request=policy_request).to_dict(),
        control_actions=tuple(background_control_action_lines(getattr(agent, "config", None), request=policy_request)),
        task_id=task_id, narrow_audit_event=is_narrow_audit_event(getattr(request, "reason", "")),
        include_recent_messages=include_recent_messages,
    )


# LLM: 后台上下文节表是"标题 → payload 键 → 缺省值"的唯一来源；渲染与预算估算都由它派生，估算的节必须等于渲染的节。
# Recent Messages 只在没有历史种子（include_recent_messages）时渲染；审计窄事件只渲染唤醒、精确目标与错误。
_BACKGROUND_SECTIONS: tuple[tuple[str, str, type], ...] = (
    ("Active Wake Signal", "active_wake_signal", dict),
    ("Subagent Completion Inputs", "subagent_completions", dict),
    ("Conversation Thread", "thread", dict),
    ("Runtime Load Errors", "load_errors", list),
    ("Task Runtime State", "task_runtime_state", dict),
    ("Recent Messages", "messages", list),
    ("Bound Tasks", "tasks", list),
    ("Channel Bindings", "channel_bindings", list),
    ("Recent Observations", "observations", list),
    ("Guidance", "guidance", list),
    ("Pending Wake Signals", "pending_wake_signals", list),
    ("Recovery Snapshot", "recovery_snapshot", dict),
    ("Agent Tree Snapshot", "agent_tree", dict),
)
_NARROW_AUDIT_KEYS = frozenset({"active_wake_signal", "tasks", "load_errors"})


# LLM: 只由结构化渲染开关决定，不看字段是否为空；预算与渲染共用这一结果。
# 函数用途: 返回本次后台上下文将渲染的 payload 键集合。
def background_rendered_payload_keys(*, include_recent_messages: bool, narrow_audit_event: bool) -> frozenset[str]:
    if narrow_audit_event:
        return _NARROW_AUDIT_KEYS
    return frozenset(key for _title, key, _empty in _BACKGROUND_SECTIONS if include_recent_messages or key != "messages")


# LLM: 只消费冻结材料并调用原预算器；不得读取agent/store/时间，不以展示标题裁决身份或范围。
# 预算只估算将渲染的键（background_rendered_payload_keys），不渲染的节不再挤占总预算。
# 函数用途: 纯渲染完整后台上下文，重复候选使用同一输入时输出一致。
def render_background_context(prepared: PreparedBackgroundContext) -> str:
    keys = background_rendered_payload_keys(
        include_recent_messages=prepared.include_recent_messages, narrow_audit_event=prepared.narrow_audit_event)
    bounded = bounded_background_context_payload(prepared.payload, rendered_keys=keys)
    if prepared.narrow_audit_event:
        # 审计发现或容量事件只投影当前权威事实与精确审计目标，防止无关历史干扰。
        # 完整持久账仍可通过原工具读取，这里不修改它。
        sections = [
            ("Active Wake Signal", bounded.get("active_wake_signal") or {}),
            (
                "Audit Task Objective",
                _audit_event_task_objective(
                    bounded.get("tasks"),
                    task_id=prepared.task_id,
                    wake_signal=bounded.get("active_wake_signal"),
                ),
            ),
            ("Runtime Load Errors", bounded.get("load_errors") or []),
            (
                "Background Context Projection",
                bounded.get("_projection") or {},
            ),
            ("Control Action Policy", prepared.control_policy),
        ]
    else:
        sections = [(title, bounded.get(key) or empty()) for title, key, empty in _BACKGROUND_SECTIONS if key in keys]
        sections.extend([
            ("Background Context Projection", bounded.get("_projection") or {}),
            ("Control Action Policy", prepared.control_policy),
        ])
    lines = list(prepared.header)
    for title, payload in sections:
        lines.extend(["", f"## {title}", json_block(payload)])
    lines.extend(["", "## Available Control Actions", *prepared.control_actions, "[/background-main-agent-context]"])
    return "\n".join(lines)


# LLM: 只识别既有结构化事件原因；此判据同时用于上下文与原生历史隔离，修改须联测两条入口。
# 函数用途: 判断是否只应读取本次审计事实，避免把普通对话混进独立通知。
def is_narrow_audit_event(reason: object) -> bool:
    return str(reason or "").strip().lower() in {
        "audit_finding",
        "audit_capacity_alert",
    }


# LLM: 以 exact task_id 和 source_id 选择事实，不使用旧摘要推断归属；输出仅是投影。
# 函数用途: 从已加载任务中提取当前审计事件的目标和来源绑定。
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


# LLM: 来源身份只取本次唤醒的结构化 metadata，空值排除；不得解析描述或标题。
# 函数用途: 归集事件及其发现条目携带的来源编号，供精确过滤使用。
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


# LLM: 维持原读取与进度对账顺序，返回唯一预算器的输入；task_runtime_state可能写账，错误保留供render展示。
# 函数用途: 一次准备后台事实与预算，复用宿主已读bundle；进度对账可能写盘，后续纯渲染不重做。
def _prepare_context_payload(
    agent: object,
    store: ConversationStore,
    thread: ConversationThread,
    task_id: str,
    policy_request: BackgroundToolPolicyRequest,
    *,
    active_wake_signal: dict[str, Any] | None = None,
    context_bundle: dict[str, Any] | None = None,
) -> BackgroundContextPayloadRequest:
    config = getattr(agent, "config", None)
    load_errors: list[dict[str, Any]] = []
    state = BackgroundContextLoad(
        agent, store, thread, task_id, config, policy_request, load_errors
    )
    narrow_audit_event = is_narrow_audit_event(policy_request.reason)
    visible_run_ids = [] if narrow_audit_event else _thread_active_task_ids(state)
    agent_tree = {} if narrow_audit_event else _agent_tree_payload(state, visible_run_ids)
    bundle = load_context_bundle(state) if context_bundle is None else deepcopy(context_bundle)
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
    return BackgroundContextPayloadRequest(
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


# LLM: 从持久观察账投影精确根任务的直属子代理完成结果；每个续片都应可读，不能只依赖当前一次唤醒。
# 函数用途: 读取当前根任务的直属子代理完成信封，供定时进度轮和恢复轮继续整合精确结果。
def _background_subagent_completion_context(
    state: BackgroundContextLoad,
) -> dict[str, object]:
    task_id = str(state.task_id or "").strip()
    if not task_id:
        return {}
    try:
        observations, load_errors = state.store.observations.recent_report(
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


# LLM: 窄事件先按 exact task 过滤再做预算，旧对话不能挤掉当前事件；不改原 bundle 或存储。
# 函数用途: 为单次审计通知准备精简事实，普通任务仍沿完整会话入口。
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


# LLM: 有任务先延后正文，读取同次任务事实后按原范围补齐；不重读link，失败进入load_errors阻止缺历史继续。
# 函数用途: 加载当前会话的任务视图，保留读取失败供调用方按合同处理。
def load_context_bundle(state: BackgroundContextLoad) -> dict[str, Any]:
    """Load one authoritative thread ledger and project the current task view."""
    try:
        messages_deferred = False
        if callable(getattr(state.store, "context_bundle_report", None)):
            messages_deferred = bool(str(state.task_id or "").strip())
            bundle, load_errors = state.store.context_bundle_report(
                state.thread.thread_id,
                recent_limit=_config_int(state.config, "conversation_context_recent_limit"),
                **({"include_messages": False} if messages_deferred else {}),
            )
            state.load_errors.extend(load_errors)
        else:
            bundle = state.store.context_bundle(
                state.thread.thread_id,
                recent_limit=_config_int(state.config, "conversation_context_recent_limit"),
            )
        return _task_scoped_operational_context(state, bundle, messages_deferred=messages_deferred)
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
class TaskScopeDecision:
    task_id: str
    task_ids: frozenset[str]
    exact_link: dict[str, Any] | None
    detached: bool


# LLM: exact_link 必须按本轮 task_id 精确匹配；无 task_id 的普通事件不做任何范围收缩。
# 函数用途: 用已加载的 bundle 与本轮 task_id 形成唯一范围裁决。
def task_scope_decision(
    state: BackgroundContextLoad,
    bundle: dict[str, Any],
) -> TaskScopeDecision:
    task_id = str(state.task_id or "").strip()
    task_rows = _dict_rows(bundle.get("tasks"))
    if not task_id:
        return TaskScopeDecision("", frozenset(), None, False)
    exact_link = next(
        (row for row in task_rows if str(row.get("task_id") or "").strip() == task_id),
        None,
    )
    return TaskScopeDecision(
        task_id=task_id,
        task_ids=frozenset(_task_context_ids(state)),
        exact_link=exact_link,
        detached=_is_detached_named_task_link(exact_link),
    )


# LLM: 任务过滤由一次结构化裁决决定；延后正文在此补齐，普通任务沿原窗口，detached沿创建锚点，错误不空历史。
# 函数用途: 把共享会话投影为本次任务视图，避免后来的无关消息串入独立任务。
def _task_scoped_operational_context(
    state: BackgroundContextLoad,
    bundle: dict[str, Any],
    *,
    messages_deferred: bool = False,
) -> dict[str, Any]:
    """Project one task from the shared ledger using only persisted identities.

    Ordinary foreground work keeps the continuous conversation history.  A
    detached named Audit/Goal is different: like a 会话运行时/模型助手 background
    fork, it may see the conversation that existed when it was created and
    later rows attributed to its exact task lineage, but not unrelated future
    user turns.  This remains one transcript and one compact authority; the
    projection neither copies history nor classifies natural-language text.
    """
    decision = task_scope_decision(state, bundle)
    if messages_deferred and not decision.detached:
        rows, errors = state.store.messages.recent_report(
            state.thread.thread_id, limit=_config_int(state.config, "conversation_context_recent_limit"),
        )
        state.load_errors.extend(errors)
        bundle = {**bundle, "messages": [row.to_dict() for row in rows]}
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


# LLM: 独立范围只读 cancellation_scope/work_kind/work_name，名称正文不提供授权；同步任务隔离回归。
# 函数用途: 识别已有合同中的具名独立任务，普通任务不缩窄对话历史。
def _is_detached_named_task_link(link: dict[str, Any] | None) -> bool:
    if not isinstance(link, dict):
        return False
    return (
        str(link.get("cancellation_scope") or "").strip().lower() == "detached"
        and str(link.get("work_kind") or "").strip().lower() in {"audit", "goal"}
        and bool(str(link.get("work_name") or "").strip())
    )


# LLM: 只生成创建快照和精确任务 lineage 的副本；摘要晚于任务时不继承，不推进真实 Compact 代次。
# 函数用途: 为独立任务筛选消息和摘要，原始共享会话保持不变。
def _detached_named_task_context(
    state: BackgroundContextLoad,
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
    projected["guidance"] = detached_task_rows(
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


# LLM: 固定完整消息边界后流式按锚点筛选，最后施加recent limit；读取错误必须进入本片错误集合。
# 函数用途: 固定完整canonical尾界后筛选独立任务可见消息，只在最后应用原展示窗口，避免整份正文加载。
def _detached_task_messages(
    state: BackgroundContextLoad,
    link: dict[str, Any],
    task_ids: set[str],
) -> list[dict[str, Any]]:
    from .message_selection import select_message_snapshot

    try:
        decision = TaskScopeDecision(state.task_id, frozenset(task_ids), dict(link), True)
        selected = select_message_snapshot(
            state.store.messages, state.thread.thread_id,
            selector_factory=history_scope_selector_factory(decision),
            retain_limit=_config_int(state.config, "conversation_context_recent_limit"),
        )
        return [row.to_dict() for row in selected]
    except Exception as exc:
        state.load_errors.append(runtime_error_report(exc, context="background_context.detached_messages"))
        return []


# LLM: operational 与 native 历史必须共用此算法；锚点缺失时仅保留精确身份行，不能放开整段会话。
# 函数用途: 按创建锚点或创建时间，加上本任务后续记录，筛选一组历史行。
def detached_task_rows(
    rows: list[dict[str, Any]],
    link: dict[str, Any],
    task_ids: set[str],
) -> list[dict[str, Any]]:
    positions: dict[str, int] = {}
    for index, row in enumerate(rows):
        message_id = str(row.get("message_id") or "").strip()
        if message_id:
            positions.setdefault(message_id, index)
    predicate = detached_row_selector(link, task_ids, positions)
    return [row for index, row in enumerate(rows) if predicate(row, index)]


# LLM: 编译同一次创建锚点/时间与精确lineage；positions来自完整输入，缺锚点只允许精确任务，无正文关键词裁决。
# 函数用途: 让operational列表和canonical流式读取共用唯一逐行范围规则，编译不读取持久状态。
def detached_row_selector(link, task_ids, positions):
    anchor_id = str(link.get("context_anchor_message_id") or "").strip() if positions else ""
    anchor_position = next((position for key, position in positions.items() if str(key).strip() == anchor_id), None)
    frozen_ids = frozenset(task_ids)
    try:
        created_at = float(link.get("created_at") or 0.0)
    except (TypeError, ValueError):
        created_at = 0.0

    # LLM: 快照前缀必须有已找到的锚点；无消息ID的guidance沿原创建时间，后续正文仅按精确身份放行。
    # 函数用途: 对一行结构化消息及其原序号判断是否属于任务创建快照或本任务后续流量。
    def includes(row, index):
        if anchor_id:
            snapshot = anchor_position is not None and index <= anchor_position
        else:
            try:
                snapshot = float(row.get("created_at") or 0.0) <= created_at
            except (TypeError, ValueError):
                snapshot = False
        return snapshot or _context_row_matches_task_ids(row, frozen_ids)

    return includes


# LLM: 原TaskScopeDecision来自成功的同次bundle；编译器只读完整身份位置，不重读link或复制全量正文。
# 函数用途: 将原后台范围规则适配成逐行canonical选择，锚点被摘要覆盖仍能准确限定创建快照。
def history_scope_selector_factory(decision: TaskScopeDecision):
    frozen = deepcopy(decision)

    # LLM: positions是当前固定EOF内全部非display消息的只读位置，缺锚点不能退到时间扩大范围。
    # 函数用途: 只编译一次原范围谓词，将消息元数据直接投影给唯一任务筛选算法。
    def compile_selector(positions):
        if not frozen.detached or not frozen.exact_link:
            return lambda _row: True
        predicate = detached_row_selector(frozen.exact_link, frozen.task_ids, positions)
        return lambda row: predicate({
            "message_id": row.message_id, "created_at": row.created_at, "metadata": row.metadata,
        }, positions[row.message_id])

    return compile_selector


# LLM: 摘要继承只比较持久时间；无效时间不能放行未来摘要，不以摘要文字判断任务关系。
# 函数用途: 确认一份摘要是否形成于独立任务创建前，供任务视图筛选。
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


# LLM: 仅复制合法字典行，保留未知字段；不把自然语言或其他类型转换为协议记录。
# 函数用途: 把已加载的行集合整理为可筛选的浅副本。
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


# LLM: 归属仅查已定义的结构化身份字段及 task_attributes；修改须同时检查 operational 和 native 过滤。
# 函数用途: 判断消息、任务或观察行是否属于当前任务 lineage。
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


# LLM: 从持久子代理树取得根与子运行编号，读取失败保留已证明的身份；不猜其他任务或按名字合并。
# 函数用途: 解析本次任务的合法 lineage 集合，供历史和事件过滤使用。
def _task_context_ids(state: BackgroundContextLoad) -> set[str]:
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
def _pending_wake_signals(state: BackgroundContextLoad) -> list[dict[str, Any]]:
    try:
        if callable(getattr(getattr(state.store, 'wakes', None), 'pending_report', None)):
            signals, load_errors = state.store.wakes.pending_report(
                limit=_config_int(state.config, "background_pending_wake_prompt_limit"),
            )
            state.load_errors.extend(load_errors)
            payload = [
                model_visible_wake_signal(item.to_dict()) or {}
                for item in signals
                if item.thread_id == state.thread.thread_id
            ]
        else:
            payload = [
                model_visible_wake_signal(item) or {}
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


# LLM: 只读 canonical agent-tree 投影；查询错误保留结构化事实，不能由空列表推断任务结束。
# 函数用途: 为后台模型补充当前可见的代理树与可用工具范围。
def _agent_tree_payload(
    state: BackgroundContextLoad,
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


# LLM: 恢复快照只提供诊断，读取异常写入本片错误集合，不触发接管、重试或权限改变。
# 函数用途: 包装恢复信息读取，使上下文带上明确诊断。
def _safe_recovery_snapshot(
    state: BackgroundContextLoad,
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


# LLM: 从宿主请求、配置和 owner policy 构造纯策略输入；快照与执行身份保持原合同，不从提示提权。
# 函数用途: 给上下文和历史准备统一的后台工具策略参数。
def tool_policy_request(
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
        policy_snapshot=policy_snapshot_from_request(request),
        proactive_delivery_available=proactive_delivery_available,
    )


# LLM: 只复制本次结构化 wake 的 policy_snapshot；本函数是唯一提取位置，不回读其他任务策略。
# 函数用途: 读取唤醒携带的策略快照，缺省时返回空字典。
def policy_snapshot_from_request(request: object) -> dict[str, Any]:
    wake = getattr(request, "wake_signal", None)
    if isinstance(wake, dict) and isinstance(wake.get("policy_snapshot"), dict):
        return dict(wake["policy_snapshot"])
    return {}


# LLM: 有效配置与既有默认值仍为唯一来源，零值语义不变；类型错误才使用默认值。
# 函数用途: 读取上下文条数上限，避免在投影函数中散落常量。
def _config_int(config: object | None, key: str) -> int:
    if config is None:
        return max(0, int(default_config_value(key)))
    try:
        return max(0, int(getattr(config, key)))
    except (TypeError, ValueError):
        return max(0, int(default_config_value(key)))


# LLM: 标题字段只来自宿主已裁决请求与线程；输出为模型上下文，不能反向承担机器授权。
# 函数用途: 标明本次后台上下文的事件、线程和任务编号。
def _context_header(request: BackgroundContextRequest, thread: ConversationThread) -> list[str]:
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


# LLM: 有显式 task 时按其 lineage 收窄，否则读取最新线程活动集合；错误沿本片集合上报。
# 函数用途: 确定代理树与恢复快照需要展示哪些任务。
def _thread_active_task_ids(state: BackgroundContextLoad) -> list[str]:
    if state.task_id:
        return sorted(_task_context_ids(state))
    latest = _latest_thread_with_load_error(state)
    source = latest or state.thread
    return [str(item or "").strip() for item in source.active_task_ids if str(item or "").strip()]


# LLM: 只读取 canonical 线程，保持结构化 load_error；失败不构造新的线程或控制状态。
# 函数用途: 取得最新线程以生成上下文，同时保留无法读取的原因。
def _latest_thread_with_load_error(state: BackgroundContextLoad) -> ConversationThread | None:
    try:
        if not callable(getattr(getattr(state.store, 'threads', None), 'load_report', None)):
            return state.store.threads.load(state.thread.thread_id)
        latest, load_error = state.store.threads.load_report(state.thread.thread_id)
        if load_error is not None:
            load_error["consumer_context"] = "background_context.thread_active_tasks"
            state.load_errors.append(load_error)
        return latest
    except Exception as exc:
        state.load_errors.append(
            runtime_error_report(exc, context="background_context.thread_active_tasks")
        )
        return None


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


# LLM: 只汇总 claim、代理树和产物登记的现有事实，不消费 claim、确认唤醒或修改恢复状态。
# 函数用途: 为后台接续提供只读恢复诊断，提示模型先核对真实证据。
def _recovery_snapshot(
    agent: object, store: ConversationStore, thread_id: str, visible_run_ids: list[str]
) -> dict[str, Any]:
    claim = store.claims.load(thread_id)
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


# LLM: 按登记对象的结构化 status 计数，未知状态保持可见；计数不成为交付或完成门。
# 函数用途: 把产物登记状态汇总为紧凑诊断，减少上下文体积。
def _artifact_status_counts(records: dict[str, object]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records.values():
        status = str(getattr(record, "status", "") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts
