# LLM: preflight 先解析 canonical thread；完整准备须在原车道内按 repair、索引、Compact、历史、任务取快照，修改联测压缩与恢复。
# 模块用途: 准备本轮会话和目录事实，必要时补交历史、更新索引并调用模型压缩；任务执行仍由编排层负责。
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..concurrency.interrupt import is_interrupted
from ..contracts.subagent_completion import (
    DEFAULT_VISIBLE_SUBAGENT_COMPLETIONS,
    subagent_completion_context_from_observations,
)
from ..conversation import history_projection
from ..conversation.compact import (
    ConversationCompactOptions,
    ConversationScope,
    conversation_scope,
    prepare_conversation_context,
)
from ..conversation.compact_provider_surface import ConversationCompactModelSurface
from ..conversation.native_history import (
    provider_history_messages_from_rows,
)
from ..runtime_errors import runtime_error_report
from . import request_binding, request_history
from .stream_events import CONTEXT_USAGE_SCHEMA
from .workspace_scope import gateway_request_workspace_scope, path_is_within

if TYPE_CHECKING:
    from ...core import SimpleAgent


_MAX_RECENT_TASK_WORKSPACES = 4


# LLM: 当前请求持有同一个 agent、队列记录与流 sink；字段只传递已解析输入，不创建存储或执行权。
# 类用途: 将请求路径、身份与计时容器交给编排、绑定和历史组件，保证它们操作同一轮。
@dataclass(frozen=True)
class GatewayAskRunContext:
    agent: SimpleAgent
    request: dict
    request_path: Path
    response_path: Path
    request_id: str
    on_chunk: object
    stages: dict[str, float] | None = None


# LLM: This is an eligible exact/Goal/active workspace selection, not proof that the current turn
# has started task execution; lifecycle activation still happens at the first promoting tool.
# 类用途: 保存本轮具有结构化执行权的确切任务目录，普通历史投影不会自动进入这里。
@dataclass(frozen=True)
class GatewayWorkspaceSelection:
    task_id: str
    status: str
    goal: str
    task_path: str
    execution_running: bool = False
    execution_state_available: bool = True
    execution_sources: tuple[str, ...] = ()


# LLM: Gateway projects one owner/thread history plus an optional authorized execution workspace;
# internal run records never become model-visible task choices.
# 类用途: 保存一个持续 thread 的历史，以及本轮可继承的精确工作目录。
@dataclass(frozen=True)
class GatewayConversationContext:
    thread_id: str = ""
    cwd: str = ""
    runtime_workspace_roots: tuple[str, ...] = ()
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
    canonical_history_messages: tuple[dict[str, object], ...] = ()
    recent_artifacts: tuple[dict[str, object], ...] = ()
    workspace_task: GatewayWorkspaceSelection | None = None
    # LLM: These exact owner/thread completed-task refs are model-visible candidates only;
    # they never select cwd, grant write authority, or bypass exact-path runtime rebinding.
    # 字段用途: 保存同一会话最近完成任务的精确目录候选，让模型能识别“回到上个项目”而不靠猜路径。
    recent_task_workspaces: tuple[dict[str, str], ...] = ()
    # LLM: These are bounded, exact-root terminal child deliveries from the observation ledger;
    # prompt rendering must keep them separate from chat prose and omit runner-private payloads.
    # 字段用途: 保存当前根任务直属子代理的完成回复和精确产物引用，供普通后续轮直接整合。
    subagent_completions: dict[str, object] = field(default_factory=dict)
    thread_goal: dict[str, object] | None = None
    named_work: tuple[dict[str, str], ...] = ()
    load_errors: tuple[dict, ...] = ()


# LLM: The gateway preflight carries only host-resolved request inputs plus any one-call deferred
# tool surface retained after a provider overflow; it cannot infer authorization from chat prose.
# 类用途: 保存 Gateway 加载会话与 Compact 所需的请求快照，确保溢出恢复沿用同一工具缓存面。
@dataclass(frozen=True)
class GatewayConversationLoadRequest:
    agent: SimpleAgent
    request: dict
    request_id: str
    prompt: str
    on_chunk: object | None = None
    loaded_tool_names: tuple[str, ...] = ()


# LLM: Compact start saves numeric telemetry in the exact thread before publication, using the
# captured generation CAS. This is display-only, not history, billing or Compact authority.
# 函数用途: 压缩开始先保存同一会话的新容量再通知界面，避免后台刷新恢复旧模型数字；遥测失败不打断压缩。
def _gateway_compact_progress_callback(
    on_chunk: object,
    *,
    store: object | None = None,
    thread: object | None = None,
) -> Callable[[dict[str, object]], object] | None:
    writer = getattr(on_chunk, "write_conversation_compact_progress", None)
    if not callable(writer):
        return None

    # LLM: Same-generation numeric writes happen before the public event; failed telemetry does
    # not change the original compact callback or result, and does not add model calls.
    # 函数用途: 先落当前线程的显示快照，再送出进度，原聊天历史与成本账本不变。
    def publish(payload: dict[str, object]) -> object:
        usage_writer = getattr(on_chunk, "write_context_usage", None)
        if payload.get("phase") == "started" and payload.get("context_window_tokens") and callable(usage_writer):
            usage = {
                "schema": CONTEXT_USAGE_SCHEMA,
                "estimated": True,
                "protocol": "unknown",
                "context_window_tokens": payload["context_window_tokens"],
                "compact_trigger_tokens": payload.get("trigger_tokens", 0),
                "current_tokens": payload.get("before_tokens", 0),
            }
            from ..conversation.context_usage import save_context_usage_snapshot

            save_context_usage_snapshot(store, thread, usage)
            usage_writer(usage)
        return writer(payload)

    return publish


# LLM: 此预检查只解析耐久 thread，领取执行车道后必须重新加载上下文，不能复用过期历史。
# 函数用途: 在选模型和等候车道之前确认线程身份及目录，报告原始持久化问题。
def preflight_gateway_conversation(
    inputs: GatewayConversationLoadRequest,
) -> GatewayConversationContext:
    """Resolve only the durable thread identity before reserving its run lane."""
    spec = inputs.request.get("conversation")
    if not isinstance(spec, dict):
        return GatewayConversationContext()
    store = getattr(inputs.agent, "conversation_store", None)
    if store is None:
        return GatewayConversationContext(
            load_errors=({"error_code": "conversation_store_unavailable"},)
        )
    thread, error = _load_gateway_thread(inputs, store, spec)
    if error is not None or thread is None:
        return GatewayConversationContext(
            load_errors=(error or {"error_code": "thread_unavailable"},)
        )
    return GatewayConversationContext(
        thread_id=str(thread.thread_id or ""),
        cwd=str(getattr(thread, "cwd", "") or ""),
        runtime_workspace_roots=tuple(
            str(item)
            for item in (getattr(thread, "runtime_workspace_roots", ()) or ())
            if str(item or "").strip()
        ),
        compact_generation=max(0, int(getattr(thread, "compact_generation", 0) or 0)),
    )


# LLM: One thread context combines canonical chat history, exact-root child completion inputs and
# recent artifacts. Runner-private payloads and sibling roots never enter the foreground prompt.
# 函数用途: 组装本轮 Gateway 对话所需的权威历史、直属子代理交付、工作目录和产物上下文。
def gateway_conversation_context(
    inputs: GatewayConversationLoadRequest,
    *,
    force_compact: bool = False,
) -> GatewayConversationContext:
    agent = inputs.agent
    spec = inputs.request.get("conversation")
    if not isinstance(spec, dict):
        return GatewayConversationContext()
    store = getattr(agent, "conversation_store", None)
    if store is None:
        return GatewayConversationContext(
            load_errors=({"error_code": "conversation_store_unavailable"},)
        )
    load_errors: list[dict] = []
    thread, thread_error = _load_gateway_thread(inputs, store, spec)
    if thread_error is not None or thread is None:
        return GatewayConversationContext(
            load_errors=(thread_error or {"error_code": "thread_unavailable"},)
        )
    request_history.repair_gateway_conversation_messages(store, thread.thread_id, load_errors)
    scope = conversation_scope(agent, thread, spec)
    request_history.ensure_gateway_conversation_index(agent, store, thread.thread_id)
    thread, history_rows, history_token_budget, recent_operation_evidence = (
        _load_gateway_compact_context(
            inputs,
            store,
            thread,
            force=force_compact,
        )
    )
    history, canonical_history_messages, recent_artifacts = _gateway_conversation_refs(
        agent,
        thread.thread_id,
        inputs.request_id,
        load_errors,
        history_rows=history_rows,
        history_token_budget=history_token_budget,
        work_scope=request_binding.gateway_message_work_scope(inputs.request),
    )
    thread_goal = _gateway_thread_goal(store, thread.thread_id, load_errors)
    named_work = _gateway_named_work(store, thread.thread_id, load_errors)
    workspace_task = _gateway_workspace_task(
        store,
        thread,
        thread_goal,
        load_errors,
        request=inputs.request,
        request_id=inputs.request_id,
    )
    recent_task_workspaces = _gateway_recent_task_workspaces(
        store,
        thread,
        workspace_task,
        load_errors,
        request_id=inputs.request_id,
        owner_run_root=getattr(getattr(agent, "home_paths", None), "owner_runs_dir", ""),
    )
    subagent_completions = _gateway_subagent_completion_context(
        store,
        thread.thread_id,
        workspace_task,
        load_errors,
    )
    return GatewayConversationContext(
        thread_id=thread.thread_id,
        cwd=str(getattr(thread, "cwd", "") or ""),
        runtime_workspace_roots=tuple(
            str(item)
            for item in (getattr(thread, "runtime_workspace_roots", ()) or ())
            if str(item or "").strip()
        ),
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
        canonical_history_messages=canonical_history_messages,
        recent_artifacts=recent_artifacts,
        workspace_task=workspace_task,
        recent_task_workspaces=recent_task_workspaces,
        subagent_completions=subagent_completions,
        thread_goal=thread_goal,
        named_work=named_work,
        load_errors=tuple(load_errors),
    )


# LLM: 与控制入口共用 workspace_scope 校验，目录错误先失败；存储错误仍独立报告，不混入 prompt。
# 函数用途: 校验普通请求的执行目录并读取或绑定 canonical 会话线程。
def _load_gateway_thread(
    inputs: GatewayConversationLoadRequest,
    store: object,
    spec: dict,
) -> tuple[object | None, dict | None]:
    """Load the scoped thread without mixing persistence errors into prompt assembly."""
    cwd, runtime_workspace_roots = gateway_request_workspace_scope(
        inputs.agent,
        inputs.request,
    )
    try:
        thread = store.threads.get_or_create(
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
                "cwd": cwd,
                "runtime_workspace_roots": runtime_workspace_roots,
            }
        )
    except Exception as exc:
        return None, runtime_error_report(exc, context="gateway.conversation.thread")
    return thread, None


# LLM: Gateway preflight shares the registered request interrupt with Compact. User stop propagates
# unchanged; compact failures keep their own typed error code and cannot become transcript corruption.
# 函数用途: 加载并按需压缩 Gateway 会话；停止立即退出，压缩失败保留原记录并单独报错。
def _load_gateway_compact_context(
    inputs: GatewayConversationLoadRequest,
    store: object,
    thread: object,
    *,
    force: bool = False,
) -> tuple[object, object, int, dict[str, object]]:
    """Prepare compact state and preserve the original thread on a reported failure."""
    try:
        compact = prepare_conversation_context(
            inputs.agent,
            store,
            thread,
            options=ConversationCompactOptions(
                current_prompt=inputs.prompt,
                exclude_request_id=inputs.request_id,
                force=force,
                progress_callback=_gateway_compact_progress_callback(
                    inputs.on_chunk, store=store, thread=thread,
                ),
                interrupt_check=is_interrupted,
                model_surface=ConversationCompactModelSurface(
                    prompt_files=tuple(
                        str(item)
                        for item in inputs.request.get("prompt_files", [])
                        if str(item or "").strip()
                    ),
                    context_scope="conversation",
                    loaded_tool_names=tuple(inputs.loaded_tool_names),
                ),
            ),
        )
    except InterruptedError:
        raise
    except Exception as exc:
        from ..conversation.compact_guard import ConversationCompactError, compact_exception_code

        # 压缩失败不是 transcript 损坏；保留独立错误码且让原始异常留在诊断链。
        raise ConversationCompactError(
            "上下文压缩未完成，原始会话记录保留不变", code=compact_exception_code(exc)
        ) from exc
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


# LLM: 当前目标由任务执行范围决定，不以名字或后台剩余数量选取；独立 Goal 仍由精确身份续跑。
# 函数用途: 给前台注入唯一属于前台的未结束目标，避免后台只剩一项时误占前台或导致加载失败。
def _gateway_thread_goal(
    store: object, thread_id: str, load_errors: list[dict]
) -> dict[str, object] | None:
    try:
        goals, error = store.goals.list_report(thread_id)
    except Exception as exc:
        load_errors.append(runtime_error_report(exc, context="gateway.conversation.goal"))
        return None
    if error is not None:
        load_errors.append(error)
        return None
    unfinished = [goal for goal in goals if str(getattr(goal, "status", "") or "") != "complete"]
    try:
        unfinished = [
            goal for goal in unfinished
            if str(getattr(store.tasks.load(goal.task_id), "cancellation_scope", "") or "") != "detached"
        ]
    except Exception as exc:
        load_errors.append(runtime_error_report(exc, context="gateway.conversation.goal_task"))
        return None
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


# LLM: 名称投影只来自本 thread 的任务/目标记录，不能据剩余数量或文字替当前回合选目标。
# 函数用途: 读取仍未结束的命名工作供模型理解指代；读取失败保留结构化错误。
def _gateway_named_work(
    store: object,
    thread_id: str,
    load_errors: list[dict],
) -> tuple[dict[str, str], ...]:
    """Project only active user-visible names, never an implicit current objective."""
    try:
        links, link_errors = store.tasks.list_report(thread_id)
        goals, goal_error = store.goals.list_report(thread_id)
    except Exception as exc:
        load_errors.append(runtime_error_report(exc, context="gateway.conversation.named_work"))
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


# LLM: Foreground continuation consumes the same bounded completion envelope that child lifecycle
# wakes publish. Exact root/parent ids select direct children; prose never selects ownership, and
# runner_result_json/output_json remain private even when present in the durable observation.
# 函数用途: 从会话观察账本提取当前根任务直属子代理的最终回复和交付引用，让后续聊天无需猜目录。
def _gateway_subagent_completion_context(
    store: object,
    thread_id: str,
    workspace_task: GatewayWorkspaceSelection | None,
    load_errors: list[dict],
) -> dict[str, object]:
    workspace_task_id = str(getattr(workspace_task, "task_id", "") or "").strip()
    root_task_ids = _gateway_workspace_lineage_task_ids(
        store,
        thread_id,
        workspace_task,
        load_errors,
    )
    if not root_task_ids:
        return {}
    try:
        observations, errors = store.observations.recent_report(
            thread_id,
            limit=0,
            include_handled=True,
        )
    except Exception as exc:
        load_errors.append(runtime_error_report(exc, context="gateway.conversation.subagent_completions"))
        return {}
    load_errors.extend(error for error in errors if isinstance(error, dict))
    context, issues = subagent_completion_context_from_observations(
        observations,
        root_task_ids=root_task_ids,
        workspace_task_id=workspace_task_id,
        visible_limit=DEFAULT_VISIBLE_SUBAGENT_COMPLETIONS,
    )
    load_errors.extend(
        runtime_error_report(
            ValueError(issue),
            context="gateway.conversation.subagent_completions",
        )
        for issue in issues
    )
    return context


# LLM: A continued foreground turn gets a new task id while retaining the same canonical task
# workspace. Exact task-link path equality is the only lineage join; prompt prose and cwd do not
# participate, and detached named work remains outside the ordinary lineage.
# 函数用途: 找出当前持续工作目录在同一 thread 中使用过的结构化主任务 id，供后续轮接回早先 child 交付。
def _gateway_workspace_lineage_task_ids(
    store: object,
    thread_id: str,
    workspace_task: GatewayWorkspaceSelection | None,
    load_errors: list[dict],
) -> set[str]:
    current_task_id = str(getattr(workspace_task, "task_id", "") or "").strip()
    task_path = _existing_gateway_workspace_path(getattr(workspace_task, "task_path", ""))
    if not current_task_id or not task_path:
        return set()
    try:
        links, errors = store.tasks.list_report(thread_id)
    except Exception as exc:
        load_errors.append(
            runtime_error_report(exc, context="gateway.conversation.subagent_completion_lineage")
        )
        return {current_task_id}
    load_errors.extend(error for error in errors if isinstance(error, dict))
    selected = {
        str(getattr(link, "task_id", "") or "").strip()
        for link in links
        if str(getattr(link, "task_id", "") or "").strip()
        if str(getattr(link, "cancellation_scope", "") or "").strip().lower() != "detached"
        if _existing_gateway_workspace_path(getattr(link, "task_path", "")) == task_path
    }
    selected.add(current_task_id)
    return selected


# LLM: Workspace execution follows an exact bound request, unfinished Goal, or currently active
# task. A terminal sticky pointer is display/navigation state and must not select a new turn.
# 函数用途: 为本轮选择仍有执行权的精确任务目录；普通终态任务不会自动吸附下一项工作。
def _gateway_workspace_task(
    store: object,
    thread: object,
    thread_goal: dict[str, object] | None,
    load_errors: list[dict],
    *,
    request: object = None,
    request_id: str = "",
) -> GatewayWorkspaceSelection | None:
    thread_id = str(getattr(thread, "thread_id", "") or "").strip()
    try:
        links, errors = store.tasks.list_report(thread_id)
    except Exception as exc:
        load_errors.append(runtime_error_report(exc, context="gateway.conversation.workspace_task"))
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
    selected, strict_selection = _select_gateway_workspace_link(
        selectable=selectable,
        thread=thread,
        thread_goal=thread_goal,
        request=request,
        request_id=request_id,
    )
    if selected is None:
        return None
    configured_task_path = str(getattr(selected, "task_path", "") or "").strip()
    task_path = _existing_gateway_workspace_path(configured_task_path)
    if not task_path:
        # 会话运行时 keeps a Goal as a thread overlay and does not require it to materialize a
        # workspace before the first real file/tool action. An exact Goal link with an
        # empty path is therefore valid and the next foreground turn stays in the thread cwd.
        # A non-empty path that disappeared is still an objective persistence failure.
        if strict_selection and configured_task_path:
            load_errors.append(
                runtime_error_report(
                    ValueError(
                        "selected conversation workspace path is missing or not a directory: "
                        f"{getattr(selected, 'task_id', '')}"
                    ),
                    context="gateway.conversation.workspace_task",
                )
            )
        return None
    from ..conversation.task_promotion import conversation_task_execution_state

    execution = conversation_task_execution_state(
        store,
        thread_id,
        str(getattr(selected, "task_id", "") or ""),
    )
    return GatewayWorkspaceSelection(
        task_id=str(getattr(selected, "task_id", "") or ""),
        status=str(getattr(selected, "status", "") or ""),
        goal=str(getattr(selected, "goal", "") or ""),
        task_path=task_path,
        execution_running=execution.get("running") is True,
        execution_state_available=execution.get("state_available") is True,
        execution_sources=tuple(str(item) for item in execution.get("sources", []) if str(item)),
    )


# LLM: 精确请求可恢复原现场；新消息只绑定 active Goal，不将暂停目标或历史 sticky 指针视作执行授权。
# 函数用途: 按结构化任务归属选择工作区，暂停后的普通聊天仍能进行且不恢复目标。
def _select_gateway_workspace_link(
    *,
    selectable: list[object],
    thread: object,
    thread_goal: dict[str, object] | None,
    request: object,
    request_id: str,
) -> tuple[object | None, bool]:
    allowed = {
        str(getattr(link, "task_id", "") or "").strip(): link for link in selectable
    }
    exact_request_task_id = request_binding.gateway_bound_request_task_id(
        request,
        request_id=request_id,
        thread_id=str(getattr(thread, "thread_id", "") or ""),
    )
    if exact_request_task_id:
        return _required_gateway_workspace_link(
            allowed,
            exact_request_task_id,
            source="bound request",
        ), True
    goal_task_id = str((thread_goal or {}).get("task_id") or "").strip()
    if goal_task_id and (thread_goal or {}).get("status") == "active":
        return _required_gateway_workspace_link(
            allowed,
            goal_task_id,
            source="thread goal",
        ), True
    if goal_task_id:
        allowed.pop(goal_task_id, None)
        selectable = [link for link in selectable if getattr(link, "task_id", "") != goal_task_id]
    sticky_id = str(getattr(thread, "workspace_task_id", "") or "").strip()
    sticky = allowed.get(sticky_id)
    if sticky is not None and _gateway_workspace_link_active(sticky):
        return sticky, True
    active = [link for link in selectable if _gateway_workspace_link_active(link)]
    return (active[0], True) if len(active) == 1 else (None, False)


# LLM: 必需任务绑定不可用是执行状态冲突，不是聊天历史损坏；不能换目录猜测恢复。
# 函数用途: 精确链接缺失或已取消时返回独立诊断，避免误报会话记录不可读。
def _required_gateway_workspace_link(
    allowed: dict[str, object],
    task_id: str,
    *,
    source: str,
) -> object | None:
    selected = allowed.get(str(task_id or "").strip())
    if selected is not None:
        return selected
    from .request_errors import ConversationTaskBindingError

    raise ConversationTaskBindingError(f"{source} workspace task is missing or not reusable: {task_id}")


# LLM: Only active means an ordinary new turn still belongs to a live execution. Interrupted and
# completed are terminal here; they require exact request/Goal binding or an exact mutation path.
# 函数用途: 判断任务链接是否仍在执行中，避免完成或中断任务自动吸附普通新消息。
def _gateway_workspace_link_active(link: object) -> bool:
    return str(getattr(link, "status", "") or "").strip().lower() == "active"


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


# LLM: This bounded projection exposes only canonical completed-task directories from the same
# owner/thread. It is discovery context, never workspace selection or execution authority; exact
# tool paths still pass scope checks. Canonical internal run records are not business directories.
# 函数用途: 列出同会话真实业务目录候选，排除内部执行记录，避免续作时去 runs 里误找源码。
def _gateway_recent_task_workspaces(
    store: object,
    thread: object,
    workspace_task: GatewayWorkspaceSelection | None,
    load_errors: list[dict],
    *,
    request_id: str,
    owner_run_root: object = "",
) -> tuple[dict[str, str], ...]:
    thread_id = str(getattr(thread, "thread_id", "") or "").strip()
    owner_home_text = str(getattr(thread, "owner_home", "") or "").strip()
    try:
        owner_home = Path(owner_home_text).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return ()
    if not thread_id or not owner_home.is_dir():
        return ()
    try:
        links, errors = store.tasks.list_report(thread_id)
    except Exception as exc:
        load_errors.append(runtime_error_report(exc, context="gateway.conversation.recent_task_workspaces"))
        return ()
    load_errors.extend(error for error in errors if isinstance(error, dict))
    current_task_id = str(getattr(workspace_task, "task_id", "") or "").strip()
    current_path = _existing_gateway_workspace_path(
        getattr(workspace_task, "task_path", "")
    )
    rows: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    run_root = _existing_gateway_workspace_path(owner_run_root)
    ordered = sorted(
        links,
        key=lambda link: (
            float(getattr(link, "created_at", 0.0) or 0.0),
            str(getattr(link, "task_id", "") or ""),
        ),
        reverse=True,
    )
    for link in ordered:
        task_id = str(getattr(link, "task_id", "") or "").strip()
        status = str(getattr(link, "status", "") or "").strip().lower()
        if status != "completed" or task_id in {request_id, current_task_id}:
            continue
        if str(getattr(link, "cancellation_scope", "") or "").strip().lower() == "detached":
            continue
        task_path = _existing_gateway_workspace_path(getattr(link, "task_path", ""))
        if not task_path or task_path == current_path or task_path in seen_paths:
            continue
        if not path_is_within(Path(task_path), owner_home):
            continue
        # 内部执行记录不是用户业务项目；不要把 runs/<日期>/<hash> 当成旧代码所在地。
        if run_root and path_is_within(Path(task_path), Path(run_root)):
            continue
        goal = " ".join(str(getattr(link, "goal", "") or "").split())[:160]
        rows.append(
            {
                "task_id": task_id,
                "task_path": task_path,
                "status": status,
                "goal": goal,
            }
        )
        seen_paths.add(task_path)
        if len(rows) >= _MAX_RECENT_TASK_WORKSPACES:
            break
    return tuple(rows)


# LLM: Visible prose, canonical provider replay and artifact refs come from one bounded thread
# row selection but remain three typed outputs; paths and native envelopes never enter prose.
# 函数用途: 一次读取本轮共用的有界正文、原生消息前缀和近期产物引用。
def _gateway_conversation_refs(
    agent: SimpleAgent,
    thread_id: str,
    request_id: str,
    load_errors: list[dict],
    *,
    history_rows: object = None,
    history_token_budget: int = 0,
    work_scope: dict[str, object] | None = None,
) -> tuple[
    tuple[tuple[str, str], ...],
    tuple[dict[str, object], ...],
    tuple[dict[str, object], ...],
]:
    selected_rows = history_projection.conversation_history_rows(
        agent,
        thread_id,
        request_id,
        load_errors,
        rows=history_rows,
        token_budget=history_token_budget,
        work_scope=work_scope,
    )
    history = tuple((row.role, row.content) for row in selected_rows)
    canonical_history = provider_history_messages_from_rows(selected_rows)
    artifacts = request_history.gateway_recent_artifacts(agent, thread_id, request_id, load_errors)
    return history, canonical_history, artifacts
