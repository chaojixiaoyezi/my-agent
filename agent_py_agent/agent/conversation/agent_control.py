"""Owner-scoped view, guidance, approval, and stop operations for delegated agents."""

# LLM: TUI/Web/IM 共用已授权的精确子代理控制；插话按 admission→creation 写原账本，启动在 creation 锁外。
# 模块用途: 让用户查看、插话或停止自己任务树中的孩子，状态变更仍经正式生命周期服务。

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass

from ..common.id_generator import new_id
from ..runtime_db.operations import RuntimeConflictError, RuntimeExecutionBusyError
from ..runtime_errors import DataCorruptionError, runtime_error_report
from ..subagents.authorization_gate import OperationRequest, authorize_operation
from ..subagents.cancellation import (
    CancelSubagentTaskRequest,
    cancel_subagent_tree,
    cleanup_subagent_tree,
    prepare_subagent_stops,
)
from ..subagents.models import SUBAGENT_ENDED_STATUSES, task_status_in
from .agent_activity import conversation_agent_view
from .agent_tool_approval import resolve_agent_tool_approval
from .store import ConversationStore
from .store_guidance_records import GuidanceOnceReceipt

MAX_AGENT_GUIDANCE_CHARS = 1 << 20

_AGENT_STOP_LOCK = threading.Lock()
_AGENT_STOP_ACTIVE: set[tuple[str, str]] = set()
_AGENT_GUIDANCE_LOCKS_GUARD = threading.Lock()
_AGENT_GUIDANCE_LOCKS: dict[tuple[str, str], tuple[threading.Lock, int]] = {}


# LLM: Structured errors preserve an HTTP status and stable code without
# exposing internal paths, prompts, or raw exceptions to thin clients.
# 类用途: 把子代理控制拒绝转换成统一的 HTTP 错误响应。
@dataclass
class AgentControlError(RuntimeError):
    status: int
    error_code: str
    message: str

    # LLM: String conversion returns only the public message; callers must read
    # status/error_code fields for machine decisions.
    # 函数用途: 给日志或兜底响应提供不含内部对象的错误文字。
    def __str__(self) -> str:
        return self.message


# LLM: This internal decision binds one user message either to the exact active
# attempt or to one durable pending successor on the same AgentRun. It carries
# only structured runtime facts; display labels and model prose never select a
# turn or authorize resumption.
# 类用途: 表示子代理插话应进入哪个执行轮，以及是否需要把同一个代理重新拉起。
@dataclass(frozen=True)
class _AgentGuidanceTurn:
    expected_turn_id: str = ""
    agent_run_id: str = ""
    resume_required: bool = False
    queue_successor: bool = False


# LLM: One immutable submission keeps the authenticated scope, canonical store,
# refreshed task and client id together across first delivery and HTTP replay.
# Attempt identity remains an explicit argument because recovery may rebind it.
# 类用途: 汇总一次子代理插话的固定身份，避免首次投递和重放函数携带过多易错参数。
@dataclass(frozen=True)
class _AgentGuidanceSubmission:
    scope: object
    store: ConversationStore
    thread: object
    task: object
    content: str
    stable_id: str
    dedupe_key: str


# LLM: 锁内交接已持久回执与准确 pending ID；执行器在 creation guard 外启动，不能丢 ID 后借用后来 current。
# 类用途: 把插话排队与启动分开，防止进程探测或新线程反过来堵住整个子代理创建锁。
@dataclass(frozen=True)
class _AgentGuidanceDelivery:
    payload: dict[str, object]
    resume_task: object | None = None
    expected_attempt_id: str = ""


# LLM: The single Gateway serializes only competing submissions for the same
# owner/run. Reference counts remove idle locks, so unrelated users never share
# a slow admission edge and long-lived task trees do not leak one lock per run.
# 函数用途: 给同一用户的同一子代理加一把短锁，避免并发消息重复启动且不阻塞其他用户。
@contextmanager
def _agent_guidance_admission(owner_id: str, run_id: str):
    key = (str(owner_id or "").strip(), str(run_id or "").strip())
    with _AGENT_GUIDANCE_LOCKS_GUARD:
        lock, references = _AGENT_GUIDANCE_LOCKS.get(
            key,
            (threading.Lock(), 0),
        )
        _AGENT_GUIDANCE_LOCKS[key] = (lock, references + 1)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        with _AGENT_GUIDANCE_LOCKS_GUARD:
            current_lock, references = _AGENT_GUIDANCE_LOCKS.get(
                key,
                (lock, 1),
            )
            if current_lock is lock and references <= 1:
                _AGENT_GUIDANCE_LOCKS.pop(key, None)
            elif current_lock is lock:
                _AGENT_GUIDANCE_LOCKS[key] = (lock, references - 1)


# LLM: View is read-only after exact owner/root authorization. Cursor controls
# only the optional display-event page and has no lifecycle meaning.
# 函数用途: 读取一个可见子代理的详情页数据。
def read_agent_view(
    agent: object,
    *,
    scope: object,
    run_id: str,
    after: int = 0,
    session_task_id: str = "",
    bound_root_id: str = "",
) -> dict[str, object]:
    store, _thread, task = _authorized_agent_target(
        agent,
        scope=scope,
        run_id=run_id,
        operation="view",
        session_task_id=session_task_id,
        bound_root_id=bound_root_id,
    )
    return conversation_agent_view(
        agent,
        store,
        str(getattr(task, "id", "") or ""),
        after=max(0, int(after or 0)),
    )


# LLM: Bounded panel read gathers authorized foreground/delegated tasks and subagent nodes for Feishu cards within 1s.
# 函数用途: 投影会话当前授权的任务花名册与选中子代理视图，不执行全量目录或全历史扫描。
def list_authorized_task_panel(
    agent: object,
    *,
    scope: object,
    session_task_id: str = "",
    bound_root_id: str = "",
    selected_run_id: str = "",
    event_after: int = 0,
    task_limit: int = 8,
    node_limit: int = 32,
) -> dict[str, object]:
    store = getattr(agent, "conversation_store", None)
    if not isinstance(store, ConversationStore):
        raise AgentControlError(503, "AGENT_STORE_UNAVAILABLE", "代理状态存储不可用。")
    conversation_id = str(getattr(scope, "conversation_id", "") or "").strip()
    user_id = str(getattr(scope, "user_id", "") or "").strip()
    channel = str(getattr(scope, "channel", "") or "").strip()
    if not conversation_id or not user_id or not channel:
        raise AgentControlError(400, "AGENT_SCOPE_INVALID", "当前会话身份不完整。")
    try:
        sender_thread, load_error = store.threads.resolve_report(
            channel=channel,
            channel_conversation_id=conversation_id,
            channel_user_id=user_id,
        )
    except Exception as exc:
        raise AgentControlError(503, "AGENT_THREAD_UNAVAILABLE", "当前会话暂时不可读取。") from exc
    if sender_thread is None or load_error is not None:
        raise AgentControlError(404, "AGENT_THREAD_NOT_FOUND", "当前会话还没有可查看的代理任务。")

    owner_id = str(
        getattr(getattr(agent, "home_paths", None), "owner_id", "") or ""
    ).strip()
    clean_st_id = str(session_task_id or "").strip()
    clean_root_id = str(bound_root_id or "").strip()
    clamped_task_limit = max(1, min(20, int(task_limit or 8)))
    clamped_node_limit = max(1, min(64, int(node_limit or 32)))

    selected_task_dict: dict[str, object] | None = None
    target_thread: object | None = None
    verified_root_id = ""

    if clean_st_id:
        session_tasks = getattr(store, "session_tasks", None)
        if session_tasks is None or not hasattr(session_tasks, "load"):
            raise AgentControlError(503, "AGENT_TASK_UNAVAILABLE", "会话任务存储不可用。")
        try:
            st = session_tasks.load(clean_st_id)
        except (OSError, ValueError) as exc:
            raise AgentControlError(404, "AGENT_TASK_NOT_FOUND", "目标后台任务不存在或不可读。") from exc
        if st is None:
            raise AgentControlError(404, "AGENT_TASK_NOT_FOUND", "目标后台任务不存在。")
        if str(getattr(st, "sender_thread_id", "") or "").strip() != str(getattr(sender_thread, "thread_id", "") or "").strip():
            raise AgentControlError(403, "AGENT_OUTSIDE_CONVERSATION", "目标后台任务不属于当前会话。")

        target_thread_id = str(getattr(st, "target_thread_id", "") or "").strip()
        try:
            target_thread, target_err = store.threads.load_report(target_thread_id)
        except Exception as exc:
            raise AgentControlError(503, "AGENT_THREAD_UNAVAILABLE", "目标后台执行会话暂时不可读取。") from exc
        if target_thread is None or target_err is not None:
            raise AgentControlError(404, "AGENT_THREAD_NOT_FOUND", "目标后台执行会话不可读取。")

        if owner_id and getattr(target_thread, "owner_id", "") and str(target_thread.owner_id).strip() != owner_id:
            raise AgentControlError(403, "AGENT_OUTSIDE_CONVERSATION", "目标后台任务超出当前用户。")

        from .execution_threads import IM_TASK_EXECUTION_KEY, execution_origin_thread
        policy = (getattr(target_thread, "metadata", None) or {}).get(IM_TASK_EXECUTION_KEY)
        if isinstance(policy, dict):
            try:
                origin_thread = execution_origin_thread(store.threads.require, target_thread)
            except (DataCorruptionError, KeyError, FileNotFoundError) as exc:
                raise AgentControlError(403, "AGENT_OUTSIDE_CONVERSATION", "后台执行来源会话无效。") from exc
            if str(getattr(origin_thread, "thread_id", "") or "").strip() != str(getattr(sender_thread, "thread_id", "") or "").strip():
                raise AgentControlError(403, "AGENT_OUTSIDE_CONVERSATION", "后台执行来源会话与当前会话不匹配。")
            if str(policy.get("session_task_id") or "") not in {"", st.task_id}:
                raise AgentControlError(403, "AGENT_OUTSIDE_CONVERSATION", "后台执行策略中的任务引用不匹配。")
            verified_root_id = str(policy.get("root_task_id") or getattr(target_thread, "workspace_task_id", "") or "").strip()
        else:
            verified_root_id = clean_root_id or str(getattr(target_thread, "workspace_task_id", "") or "").strip()

        if clean_root_id and verified_root_id and clean_root_id != verified_root_id:
            raise AgentControlError(403, "AGENT_OUTSIDE_CONVERSATION", "卡片绑定的根任务与后台任务不一致。")

        st_title = str(getattr(st, "goal", "") or getattr(st, "title", "") or "后台协调任务").strip()[:160]
        st_status = str(getattr(st, "status", "") or "queued").strip().lower()
        st_updated_at = float(getattr(st, "updated_at", 0.0) or 0.0)
        selected_task_dict = {
            "task_ref": f"stask:{st.task_id}",
            "task_kind": "delegated_session_task",
            "session_task_id": st.task_id,
            "bound_root_id": verified_root_id or clean_root_id,
            "target_thread_id": target_thread_id,
            "title": st_title,
            "status": st_status,
            "updated_at": st_updated_at,
        }

    elif clean_root_id:
        try:
            link, link_err = store.tasks.load_report(clean_root_id)
        except Exception as exc:
            raise AgentControlError(503, "AGENT_ROOT_UNAVAILABLE", "当前主任务状态暂时不可读取。") from exc
        if link is None or link_err is not None:
            raise AgentControlError(404, "AGENT_ROOT_NOT_FOUND", "当前卡片绑定的主任务不存在。")
        if str(getattr(link, "thread_id", "") or "").strip() != str(getattr(sender_thread, "thread_id", "") or "").strip():
            raise AgentControlError(403, "AGENT_OUTSIDE_CONVERSATION", "目标主任务不属于当前会话。")
        verified_root_id = clean_root_id
        target_thread = sender_thread
        fg_title = str(getattr(link, "title", "") or getattr(link, "goal", "") or getattr(sender_thread, "title", "") or "当前会话主任务").strip()[:160]
        fg_status = str(getattr(link, "status", "") or "running").strip().lower()
        fg_updated_at = float(getattr(link, "updated_at", getattr(sender_thread, "updated_at", 0.0)) or 0.0)
        selected_task_dict = {
            "task_ref": f"root:{clean_root_id}",
            "task_kind": "foreground_root",
            "session_task_id": "",
            "bound_root_id": clean_root_id,
            "target_thread_id": str(getattr(sender_thread, "thread_id", "") or "").strip(),
            "title": fg_title,
            "status": fg_status,
            "updated_at": fg_updated_at,
        }

    else:
        fg_root_id = str(getattr(sender_thread, "workspace_task_id", "") or "").strip()
        if fg_root_id:
            try:
                link, link_err = store.tasks.load_report(fg_root_id)
            except Exception:
                link, link_err = None, "read_error"
            if link is not None and link_err is None and str(getattr(link, "thread_id", "") or "").strip() == str(getattr(sender_thread, "thread_id", "") or "").strip():
                verified_root_id = fg_root_id
                target_thread = sender_thread
                fg_title = str(getattr(link, "title", "") or getattr(link, "goal", "") or getattr(sender_thread, "title", "") or "当前会话主任务").strip()[:160]
                fg_status = str(getattr(link, "status", "") or "running").strip().lower()
                fg_updated_at = float(getattr(link, "updated_at", getattr(sender_thread, "updated_at", 0.0)) or 0.0)
                selected_task_dict = {
                    "task_ref": f"root:{fg_root_id}",
                    "task_kind": "foreground_root",
                    "session_task_id": "",
                    "bound_root_id": fg_root_id,
                    "target_thread_id": str(getattr(sender_thread, "thread_id", "") or "").strip(),
                    "title": fg_title,
                    "status": fg_status,
                    "updated_at": fg_updated_at,
                }

    task_map: dict[str, dict[str, object]] = {}
    if selected_task_dict is not None:
        task_map[str(selected_task_dict["task_ref"])] = selected_task_dict

    session_tasks = getattr(store, "session_tasks", None)
    if session_tasks is not None and hasattr(session_tasks, "list_page"):
        try:
            page = session_tasks.list_page(limit=32, cursor=None)
            items = page.get("items") or []
            sender_id = str(getattr(sender_thread, "thread_id", "") or "").strip()
            for item in items:
                if str(getattr(item, "sender_thread_id", "") or "").strip() == sender_id:
                    st_id = str(getattr(item, "task_id", "") or "").strip()
                    if not st_id:
                        continue
                    ref = f"stask:{st_id}"
                    if ref not in task_map:
                        task_map[ref] = {
                            "task_ref": ref,
                            "task_kind": "delegated_session_task",
                            "session_task_id": st_id,
                            "bound_root_id": "",
                            "target_thread_id": str(getattr(item, "target_thread_id", "") or "").strip(),
                            "title": str(getattr(item, "goal", "") or getattr(item, "title", "") or "后台协调任务").strip()[:160],
                            "status": str(getattr(item, "status", "") or "queued").strip().lower(),
                            "updated_at": float(getattr(item, "updated_at", 0.0) or 0.0),
                        }
        except Exception:
            pass

    all_tasks = sorted(
        task_map.values(),
        key=lambda t: float(t.get("updated_at") or 0.0),
        reverse=True,
    )[:clamped_task_limit]

    if selected_task_dict is None and all_tasks:
        selected_task_dict = all_tasks[0]
        if selected_task_dict.get("task_kind") == "delegated_session_task" and session_tasks is not None:
            clean_st_id = str(selected_task_dict.get("session_task_id") or "")
            try:
                st = session_tasks.load(clean_st_id)
                if st is not None:
                    target_thread, _ = store.threads.load_report(str(st.target_thread_id or "").strip())
                    if target_thread is not None:
                        from .execution_threads import IM_TASK_EXECUTION_KEY
                        policy = (getattr(target_thread, "metadata", None) or {}).get(IM_TASK_EXECUTION_KEY)
                        if isinstance(policy, dict):
                            verified_root_id = str(policy.get("root_task_id") or getattr(target_thread, "workspace_task_id", "") or "").strip()
                        else:
                            verified_root_id = str(getattr(target_thread, "workspace_task_id", "") or "").strip()
                        selected_task_dict["bound_root_id"] = verified_root_id
            except Exception:
                pass

    nodes: list[dict[str, object]] = []
    if selected_task_dict is not None:
        main_status = str(selected_task_dict.get("status") or "").strip().lower()
        main_terminal = main_status in {"done", "failed", "cancelled"}
        if selected_task_dict.get("task_kind") == "delegated_session_task":
            main_node = {
                "node_id": f"main:{selected_task_dict['session_task_id']}",
                "node_kind": "background_main",
                "session_task_id": selected_task_dict["session_task_id"],
                "target_thread_id": selected_task_dict["target_thread_id"],
                "bound_root_id": selected_task_dict["bound_root_id"],
                "run_id": "",
                "parent_run_id": "",
                "depth": 0,
                "title": "后台协调主代理",
                "status": main_status,
                "terminal": main_terminal,
                "can_view": False,
                "can_guide": False,
                "can_stop_branch": False,
            }
        else:
            main_node = {
                "node_id": f"main:{selected_task_dict['bound_root_id']}",
                "node_kind": "foreground_main",
                "session_task_id": "",
                "target_thread_id": selected_task_dict["target_thread_id"],
                "bound_root_id": selected_task_dict["bound_root_id"],
                "run_id": "",
                "parent_run_id": "",
                "depth": 0,
                "title": "当前会话主任务",
                "status": main_status,
                "terminal": main_terminal,
                "can_view": False,
                "can_guide": False,
                "can_stop_branch": False,
            }
        nodes.append(main_node)

        if verified_root_id:
            manager = getattr(agent, "subagents", None)
            if manager is not None:
                local_store = getattr(manager, "local_store", None)
                tree_reader = getattr(local_store, "list_agent_tree", None)
                selected_reader = getattr(manager, "list_runs_by_ids_report", None)

                subagent_tasks: list[object] = []
                if callable(tree_reader) and callable(selected_reader):
                    try:
                        tree = tree_reader(verified_root_id)
                        run_records = list(getattr(tree, "runs", ()) or ())
                        matching_run_ids = [
                            str(getattr(r, "run_id", "") or "").strip()
                            for r in run_records
                            if str(getattr(r, "root_task_id", "") or getattr(r, "root_id", "") or "").strip() in {"", verified_root_id}
                            and str(getattr(r, "run_id", "") or "").strip()
                        ]
                        if matching_run_ids:
                            report = selected_reader(matching_run_ids[:clamped_node_limit])
                            subagent_tasks = list(report.runs or ())
                    except Exception:
                        subagent_tasks = []
                else:
                    try:
                        all_runs_fn = getattr(manager, "list", None)
                        if callable(all_runs_fn):
                            raw_runs = all_runs_fn()
                            subagent_tasks = [
                                r for r in raw_runs
                                if str(getattr(r, "root_id", "") or "").strip() == verified_root_id
                            ]
                    except Exception:
                        subagent_tasks = []

                for stask in subagent_tasks[: max(0, clamped_node_limit - 1)]:
                    sub_id = str(getattr(stask, "id", "") or "").strip()
                    if not sub_id:
                        continue
                    sub_status = str(getattr(stask, "status", "") or "PENDING").strip().upper()
                    sub_term = task_status_in(sub_status, SUBAGENT_ENDED_STATUSES)
                    sub_title = str(
                        getattr(stask, "agent_name", "")
                        or getattr(stask, "role", "")
                        or getattr(stask, "goal", "")
                        or getattr(stask, "description", "")
                        or "subagent"
                    ).strip()[:80]
                    sub_depth = max(1, int(getattr(stask, "depth", 1) or 1))
                    sub_parent = str(getattr(stask, "parent_id", "") or "").strip()
                    sub_lifecycle = str(getattr(stask, "lifecycle_phase", "") or sub_status).strip().lower()
                    sub_activity = str(
                        getattr(stask, "activity", "")
                        or getattr(stask, "goal", "")
                        or getattr(stask, "description", "")
                        or ""
                    ).strip()[:160]

                    nodes.append({
                        "node_id": f"run:{sub_id}",
                        "node_kind": "subagent",
                        "run_id": sub_id,
                        "parent_run_id": sub_parent,
                        "root_id": verified_root_id,
                        "depth": sub_depth,
                        "title": sub_title,
                        "status": sub_status,
                        "lifecycle_phase": sub_lifecycle,
                        "activity": sub_activity,
                        "terminal": sub_term,
                        "can_view": True,
                        "can_guide": True,
                        "can_stop_branch": True,
                    })

    clean_sel_id = str(selected_run_id or "").strip()
    selected_node_id = "all_summary"
    actual_selected_run_id = ""
    selected_agent_view = None

    if clean_sel_id and clean_sel_id != "all_summary":
        if clean_sel_id.startswith("main:"):
            selected_node_id = clean_sel_id
        else:
            raw_run_id = clean_sel_id[4:] if clean_sel_id.startswith("run:") else clean_sel_id
            selected_node_id = f"run:{raw_run_id}"
            actual_selected_run_id = raw_run_id
            selected_agent_view = read_agent_view(
                agent,
                scope=scope,
                run_id=raw_run_id,
                after=max(0, int(event_after or 0)),
                session_task_id=session_task_id,
                bound_root_id=bound_root_id,
            )

    return {
        "schema_version": "feishu_task_panel.v1",
        "ok": True,
        "sender_thread_id": str(getattr(sender_thread, "thread_id", "") or "").strip(),
        "selected_task": selected_task_dict,
        "tasks": all_tasks,
        "nodes": nodes,
        "selected_node_id": selected_node_id,
        "selected_run_id": actual_selected_run_id,
        "selected_agent_view": selected_agent_view,
        "warnings": [],
    }


# LLM: Child input uses one caller-supplied stable message id and one exact
# AgentRun. A live runner receives it in-place; an idle/waiting nonterminal
# agent gets one pending successor attempt on that same run and is resumed.
# Acceptance still means mailbox + launch admission only; provider consumption
# is a later durable state. Terminal children remain read-only.
# 函数用途: 把普通用户消息精确排进当前或下一子代理回合，等待中的代理会立即被同 run 唤醒。
def send_agent_guidance(
    agent: object,
    *,
    scope: object,
    run_id: str,
    message: str,
    message_id: str,
    session_task_id: str = "",
    bound_root_id: str = "",
) -> dict[str, object]:
    store, thread, task = _authorized_agent_target(
        agent,
        scope=scope,
        run_id=run_id,
        operation="send_guidance",
        session_task_id=session_task_id,
        bound_root_id=bound_root_id,
    )
    content = str(message or "").strip()
    stable_id = str(message_id or "").strip()
    if not content or not stable_id:
        raise AgentControlError(400, "AGENT_GUIDANCE_INVALID", "消息和 message_id 不能为空。")
    if len(content) > MAX_AGENT_GUIDANCE_CHARS:
        raise AgentControlError(413, "AGENT_GUIDANCE_TOO_LARGE", "补充消息超过长度上限，未发送。")
    owner_id = str(
        getattr(getattr(agent, "home_paths", None), "owner_id", "") or ""
    ).strip()
    with _agent_guidance_admission(
        owner_id,
        str(getattr(task, "id", "") or ""),
    ):
        return _send_agent_guidance_admitted(
            agent,
            scope=scope,
            store=store,
            thread=thread,
            task=task,
            content=content,
            stable_id=stable_id,
        )


# LLM: 调用方持单 run admission，再取原 creation 锁完成 fresh task/attempt、预留和邮箱提交；启动在 creation 锁外。
# 函数用途: 将插话可靠排入一个执行轮，再放开树协调锁启动孩子，避免停止与预留交错。
def _send_agent_guidance_admitted(
    agent: object,
    *,
    scope: object,
    store: ConversationStore,
    thread: object,
    task: object,
    content: str,
    stable_id: str,
) -> dict[str, object]:
    with agent.subagents.creation_guard():
        task = _reload_guidance_task(agent, task)
        submission = _AgentGuidanceSubmission(
            scope=scope, store=store, thread=thread, task=task, content=content,
            stable_id=stable_id, dedupe_key=f"user-agent-guidance:{stable_id}",
        )
        prior = _read_guidance_receipt(submission)
        delivery = (
            _replay_agent_guidance(agent, submission, prior)
            if prior is not None else _submit_new_agent_guidance(agent, submission)
        )
    if delivery.resume_task is not None:
        try:
            delivery.payload["resume"] = _resume_agent_for_guidance(
                agent, delivery.resume_task, expected_attempt_id=delivery.expected_attempt_id,
            )
        except AgentControlError:
            raise
        except Exception as exc:
            raise AgentControlError(
                503, "AGENT_RESUME_UNAVAILABLE",
                "子代理消息已经保存，但处理轮暂未启动；系统会按同一消息重试。",
            ) from exc
    return delivery.payload


# LLM: Admission always refreshes the task after taking the per-run lock; an
# authorization snapshot from before the lock cannot prove current lifecycle.
# 函数用途: 重新读取子代理权威状态并确认它仍有可投递的会话身份。
def _reload_guidance_task(agent: object, task: object) -> object:
    try:
        task = agent.subagents.load(str(getattr(task, "id", "") or ""))
    except Exception as exc:
        raise AgentControlError(
            503,
            "AGENT_ATTEMPT_UNAVAILABLE",
            "暂时无法确认子代理当前状态，消息未发送。",
        ) from exc
    child_thread_id = str(getattr(task, "agent_thread_id", "") or "").strip()
    if not child_thread_id:
        raise AgentControlError(
            503,
            "AGENT_THREAD_UNAVAILABLE",
            "暂时无法确认子代理会话，消息未发送。",
        )
    return task


# LLM: 插话持久操作经 guidance 领域组件； Receipt lookup is fail-closed because an unreadable existing stable id
# makes first-delivery versus replay unknowable. No attempt may be reserved.
# 函数用途: 读取稳定消息 ID 的已有回执；存储不可读时明确返回未知而不重复投递。
def _read_guidance_receipt(submission: _AgentGuidanceSubmission) -> object | None:
    try:
        return submission.store.guidance.receipt(submission.dedupe_key)
    except (DataCorruptionError, OSError) as exc:
        raise AgentControlError(
            503,
            "AGENT_GUIDANCE_STORE_UNAVAILABLE",
            "子代理消息回执暂时不可读取，投递结果未知。",
        ) from exc


# LLM: 调用方持 creation 锁；首次投递只解析并持久预留回合、写邮箱，返回结构化启动需求，不启动线程。
# 函数用途: 在同一短事务中完成首次插话排队，交给锁外启动路径继续。
def _submit_new_agent_guidance(
    agent: object,
    submission: _AgentGuidanceSubmission,
) -> _AgentGuidanceDelivery:
    task = submission.task
    if _task_is_terminal(task):
        raise AgentControlError(409, "AGENT_ALREADY_TERMINAL", "这个子代理已经结束；当前详情页只读。")
    turn, expected_turn_id = _reserve_guidance_turn(agent, task)
    entry = _append_guidance_entry(submission, expected_turn_id)
    return _AgentGuidanceDelivery({
        "ok": True,
        "delivery": "queued",
        "status": "pending",
        "operation_id": submission.stable_id,
        "guidance_id": entry.guidance_id,
        "expected_turn_id": expected_turn_id,
        "run_id": str(getattr(task, "id", "") or ""),
        "resume": {"status": "active_turn", "run_ids": []},
    }, task if turn.resume_required else None, expected_turn_id)


# LLM: Attempt reservation reads only RuntimeDB lifecycle and may create one
# pending successor. The provider and runner launch remain outside this helper.
# 函数用途: 决定插话进入当前轮还是同一代理的新 pending 轮，并返回精确 attempt id。
def _reserve_guidance_turn(
    agent: object,
    task: object,
) -> tuple[_AgentGuidanceTurn, str]:
    try:
        turn = _agent_guidance_turn(agent, task)
    except Exception as exc:
        raise AgentControlError(
            503,
            "AGENT_ATTEMPT_UNAVAILABLE",
            "暂时无法确认子代理当前执行回合，消息未发送。",
        ) from exc
    if not turn.expected_turn_id:
        raise AgentControlError(
            409,
            "AGENT_NOT_RUNNING",
            "子代理正在切换执行回合，消息未发送；请稍后重试。",
        )
    expected_turn_id = turn.expected_turn_id
    if turn.queue_successor:
        try:
            queued = agent.subagents.runtime_db.queue_pending_attempt(
                turn.agent_run_id,
                source="user_agent_guidance",
            )
            expected_turn_id = str(queued["attempt_id"] or "").strip()
        except (KeyError, RuntimeConflictError, RuntimeExecutionBusyError) as exc:
            raise AgentControlError(
                503,
                "AGENT_ATTEMPT_UNAVAILABLE",
                "子代理正在切换执行回合，消息尚未排队；系统会按同一消息重试。",
            ) from exc
    if not expected_turn_id:
        raise AgentControlError(
            503,
            "AGENT_ATTEMPT_UNAVAILABLE",
            "暂时无法预留子代理下一执行回合，消息未发送。",
        )
    return turn, expected_turn_id


# LLM: 插话持久操作经 guidance 领域组件； One builder and one store append path enforce the same stable-id payload
# contract for first delivery and replay. Conflicts never become another row.
# 函数用途: 按稳定消息 ID 写入子代理消息箱，并把冲突和磁盘错误转成统一控制错误。
def _append_guidance_entry(
    submission: _AgentGuidanceSubmission,
    expected_turn_id: str,
) -> object:
    try:
        return submission.store.guidance.append_once(
            _agent_guidance_request(
                scope=submission.scope,
                thread=submission.thread,
                task=submission.task,
                content=submission.content,
                stable_id=submission.stable_id,
                expected_turn_id=expected_turn_id,
            ),
            dedupe_key=submission.dedupe_key,
        )
    except DataCorruptionError as exc:
        raise AgentControlError(
            409,
            "AGENT_GUIDANCE_ID_CONFLICT",
            "这条消息的身份与先前内容冲突，未重复发送。",
        ) from exc
    except OSError as exc:
        raise AgentControlError(
            503,
            "AGENT_GUIDANCE_STORE_UNAVAILABLE",
            "子代理消息箱暂时不可用，投递结果未知。",
        ) from exc


# LLM: Host-owned attempt routing is the only field that may differ after a
# crash/retry rebind. All user/scope/thread fields stay in this canonical
# request builder so append_guidance_once can detect stable-id conflicts.
# 函数用途: 生成一条子代理插话的标准幂等请求，供首次提交和网络重放共用。
def _agent_guidance_request(
    *,
    scope: object,
    thread: object,
    task: object,
    content: str,
    stable_id: str,
    expected_turn_id: str,
) -> dict[str, object]:
    return {
        "target_type": "agent_run",
        "target_id": str(getattr(task, "id", "") or ""),
        "message": content,
        "sender": f"user:{getattr(scope, 'user_id', '') or 'owner'}",
        "priority": "normal",
        "delivery": "next_turn",
        "metadata": {
            "kind": "active_turn_user_input",
            "record_in_transcript": True,
            "thread_id": str(getattr(task, "agent_thread_id", "") or ""),
            "channel": str(getattr(scope, "channel", "") or "chat"),
            "conversation_id": str(getattr(scope, "conversation_id", "") or ""),
            "channel_message_id": stable_id,
            "conversation_thread_id": str(getattr(thread, "thread_id", "") or ""),
            "agent_run_id": str(getattr(task, "id", "") or ""),
            "expected_turn_id": expected_turn_id,
            "source": "user_agent_control",
        },
    }


# LLM: 调用方持 creation 锁；旧 pending 必须在原 mailbox 锁内复读，只有仍 pending 才预留，启动交给锁外调用者。
# 函数用途: 返回网络重试的原投递事实，已提交或消费的消息不重复启动。
def _replay_agent_guidance(
    agent: object,
    submission: _AgentGuidanceSubmission,
    prior: object,
) -> _AgentGuidanceDelivery:
    prior_entry = getattr(prior, "entry", None)
    prior_metadata = (
        getattr(prior_entry, "metadata", {})
        if isinstance(getattr(prior_entry, "metadata", {}), dict)
        else {}
    )
    expected_turn_id = str(prior_metadata.get("expected_turn_id") or "").strip()
    if not prior_entry or not expected_turn_id:
        raise AgentControlError(
            503,
            "AGENT_GUIDANCE_STORE_UNAVAILABLE",
            "子代理消息回执缺少执行轮身份，投递结果未知。",
        )
    _append_guidance_entry(submission, expected_turn_id)
    resume_required = False
    try:
        if prior.status == "pending":
            receipt, resume_required = _resume_pending_guidance_replay(
                agent, submission=submission, expected_turn_id=expected_turn_id,
            )
        else:
            receipt = submission.store.guidance.receipt(submission.dedupe_key)
        if receipt is None:
            raise DataCorruptionError("guidance replay receipt is unavailable")
    except AgentControlError:
        raise
    except Exception as exc:
        raise AgentControlError(
            503, "AGENT_RESUME_UNAVAILABLE",
            "子代理消息已经保存，但处理轮暂未启动；系统会按同一消息重试。",
        ) from exc
    entry = receipt.entry
    expected_turn_id = str(entry.metadata.get("expected_turn_id") or "")
    receipt_status = receipt.status
    if receipt_status == "rejected":
        raise AgentControlError(
            409,
            "AGENT_GUIDANCE_REJECTED",
            "这条子代理消息已被执行轮明确拒绝，未重复发送。",
        )
    resume: dict[str, object] = {
        "status": f"replay_{receipt_status or 'recorded'}",
        "run_ids": [],
    }
    if receipt_status == "pending" and not resume_required:
        resume = {"status": "active_turn", "run_ids": []}
    return _AgentGuidanceDelivery({
        "ok": True,
        "delivery": "queued",
        "status": receipt_status or "pending",
        "operation_id": submission.stable_id,
        "guidance_id": str(getattr(entry, "guidance_id", "") or ""),
        "expected_turn_id": expected_turn_id,
        "run_id": str(getattr(submission.task, "id", "") or ""),
        "resume": resume,
        "replayed": True,
    }, submission.task if resume_required else None, expected_turn_id)


# LLM: 调用方持 creation；先铸造尚未落盘的新 ID 供 mailbox 排序锁定，再在 fresh pending 回执下按原 DB 身份预留。
# 函数用途: 为确实未提交的消息准备同一代理接续，返回最新回执；消费竞争中不多排轮次。
def _resume_pending_guidance_replay(
    agent: object,
    *,
    submission: _AgentGuidanceSubmission,
    expected_turn_id: str,
) -> tuple[GuidanceOnceReceipt, bool]:
    task = submission.task
    turn = _agent_guidance_turn(agent, task)
    target_turn_id = new_id("attempt_id") if turn.queue_successor else turn.expected_turn_id or expected_turn_id

    # LLM: mailbox 已持旧/新 turn 与原 receipt；只做短 fresh/DB 准入，不重入邮箱、不启动或等待任何 runner。
    # 函数用途: 在消息仍 pending 的同一边界中确认恢复资格，并持久预留外层锁定的新轮。
    def reserve_turn() -> None:
        current = agent.subagents.load(task.id)
        if _task_is_terminal(current):
            raise AgentControlError(409, "AGENT_ALREADY_TERMINAL", "这个子代理已经结束；当前详情页只读。")
        if not turn.expected_turn_id or turn != _agent_guidance_turn(agent, current):
            raise AgentControlError(503, "AGENT_ATTEMPT_UNAVAILABLE", "子代理当前执行轮仍在切换；请按同一消息重试。")
        if target_turn_id != expected_turn_id and not _guidance_attempt_is_rebindable(
            agent.subagents.runtime_db, turn.agent_run_id, expected_turn_id,
        ):
            raise AgentControlError(
                503, "AGENT_ATTEMPT_UNAVAILABLE",
                "子代理消息的旧执行轮尚不能安全接续；系统会按同一消息重试。",
            )
        if turn.queue_successor:
            agent.subagents.runtime_db.queue_pending_attempt(
                turn.agent_run_id, source="user_agent_guidance_replay",
                expected_current_attempt_id=turn.expected_turn_id,
                pending_attempt_id=target_turn_id,
            )

    receipt, prepared = submission.store.guidance.recovery.prepare_pending_replay(
        submission.dedupe_key, expected_turn_id=expected_turn_id,
        recovered_turn_id=target_turn_id, reserve_turn=reserve_turn,
    )
    return receipt, prepared and turn.resume_required


# LLM: Rebind authority comes only from RuntimeDB ancestry and a terminal
# attempt status. Receipt text, task projection, or timing cannot prove death.
# 函数用途: 确认旧插话轮确属同一代理且已经安全结束，才允许把未提交消息接到新轮。
def _guidance_attempt_is_rebindable(
    repo: object,
    agent_run_id: str,
    attempt_id: str,
) -> bool:
    reader = getattr(repo, "get_attempt", None)
    if not callable(reader):
        return False
    row = reader(str(attempt_id or "").strip())
    return bool(
        row is not None
        and str(row["agent_run_id"] or "").strip() == str(agent_run_id or "").strip()
        and str(row["status"] or "").strip().lower()
        in {"done", "failed", "cancelled", "recovered"}
    )


# LLM: Active attempts keep the historical projection fence. Only canonical
# PLANNING/PENDING task states may reserve/resume an idle attempt; this mirrors
# 会话运行时 send_input starting a turn on an existing agent thread without making
# terminal or blocked runs writable.
# 函数用途: 根据 runtime.db 和任务状态决定插话是进入当前轮，还是给同一代理排下一轮。
def _agent_guidance_turn(agent: object, task: object) -> _AgentGuidanceTurn:
    manager = getattr(agent, "subagents", None)
    repo = getattr(manager, "runtime_db", None)
    projected_attempt_id = str(
        getattr(task, "runner_active_attempt_id", "") or ""
    ).strip()
    if repo is None:
        return _AgentGuidanceTurn(expected_turn_id=projected_attempt_id)
    run_id = str(getattr(task, "id", "") or "").strip()
    agent_run = repo.agent_run_for_run_id(run_id)
    if agent_run is None:
        return _AgentGuidanceTurn()
    agent_run_id = str(agent_run["agent_run_id"] or "").strip()
    current = repo.current_attempt(agent_run_id)
    if current is None:
        return _AgentGuidanceTurn()
    attempt_status = str(current["status"] or "").strip().lower()
    attempt_id = str(current["attempt_id"] or "").strip()
    if not attempt_id:
        return _AgentGuidanceTurn()
    if attempt_status == "running":
        if projected_attempt_id != attempt_id:
            return _AgentGuidanceTurn()
        return _AgentGuidanceTurn(
            expected_turn_id=attempt_id,
            agent_run_id=agent_run_id,
        )
    idle_task = task_status_in(
        str(getattr(task, "status", "") or ""),
        {"PLANNING", "PENDING"},
    )
    if not idle_task or projected_attempt_id:
        return _AgentGuidanceTurn()
    if attempt_status == "pending":
        return _AgentGuidanceTurn(
            expected_turn_id=attempt_id,
            agent_run_id=agent_run_id,
            resume_required=True,
        )
    if attempt_status in {"done", "failed", "cancelled", "recovered"}:
        return _AgentGuidanceTurn(
            expected_turn_id=attempt_id,
            agent_run_id=agent_run_id,
            resume_required=True,
            queue_successor=True,
        )
    return _AgentGuidanceTurn()


# LLM: 原创建锁内复读控制终态、解除 typed wait 并复用已有启动；auto_start 的启动与探测必须在该锁外，失败保留原插话回执。
# 函数用途: 在确认孩子仍可运行后启动刚排队的用户消息，停止已生效时不再启动。
def _resume_agent_for_guidance(
    agent: object, task: object, *, expected_attempt_id: str,
) -> dict[str, object]:
    manager = getattr(agent, "subagents", None)
    run_id = str(getattr(task, "id", "") or "").strip()
    if manager is None or not run_id:
        raise AgentControlError(
            503,
            "AGENT_RESUME_UNAVAILABLE",
            "子代理消息已经保存，但当前无法启动处理；系统会按同一消息重试。",
        )
    from ..subagents.direct_parent_lifecycle import (
        release_parent_wait_for_user_guidance,
    )

    with manager.creation_guard():
        from ..subagents.runner_start import assert_expected_runner_attempt

        assert_expected_runner_attempt(manager, run_id, expected_attempt_id)
        task = _reload_guidance_task(agent, task)
        if _task_is_terminal(task):
            raise AgentControlError(
                409, "AGENT_ALREADY_TERMINAL", "这个子代理已经结束；当前详情页只读。",
            )
        released_child_ids = release_parent_wait_for_user_guidance(manager, run_id)
        refreshed = manager.load(run_id)
        from ..agent_core.runner.dispatch import candidate_policy, runner_launch_in_progress
        from ..subagents.runner_session_liveness import has_fresh_runner_session

        if runner_launch_in_progress(refreshed, candidate_policy()) or has_fresh_runner_session(
            refreshed
        ):
            return {
                "status": "already_starting",
                "run_ids": [run_id],
                "released_child_wait_run_ids": list(released_child_ids),
            }
    from ..agent_core.orchestration.background.dispatch import auto_start_tasks

    result = auto_start_tasks(agent, [refreshed], {}, expected_attempt_ids={run_id: expected_attempt_id})
    status = str(result.get("status") or "").strip()
    if status != "started":
        raise AgentControlError(
            503,
            "AGENT_RESUME_UNAVAILABLE",
            "子代理消息已经保存，但处理轮暂未启动；系统会按同一消息重试。",
        )
    return {
        "status": status,
        "run_ids": list(result.get("run_ids") or []),
        "launch_id": str(result.get("launch_id") or ""),
        "released_child_wait_run_ids": list(released_child_ids),
    }


def _persist_stop_receipt(
    agent: object,
    run_id: str,
    task: object,
    receipt: dict[str, object],
) -> None:
    # LLM: 停止回执更新至运行态属性并持久化，供后续网络重试直接识别已终态和已确认清理。
    attributes = getattr(task, "attributes", None)
    if isinstance(attributes, dict):
        attributes["user_agent_stop_receipt"] = dict(receipt)
    elif attributes is None:
        try:
            task.attributes = {"user_agent_stop_receipt": dict(receipt)}
        except Exception:
            pass

    manager = getattr(agent, "subagents", None)
    mutate_fn = getattr(manager, "mutate", None)
    if callable(mutate_fn):
        def _reducer(current_task: object) -> None:
            current_attrs = getattr(current_task, "attributes", None)
            if not isinstance(current_attrs, dict):
                current_attrs = {}
                current_task.attributes = current_attrs
            current_attrs["user_agent_stop_receipt"] = dict(receipt)
        try:
            mutate_fn(run_id, _reducer)
        except Exception:
            pass


# LLM: 先核精确子树授权，已终态也可能有遗留资源；业务状态由取消域保留，不能直接早退略过资源。
# 函数用途: 按用户明确控制停止当前子代理分支，并返回真实清理结果。
def stop_agent(
    agent: object,
    *,
    scope: object,
    run_id: str,
    operation_id: str,
    session_task_id: str = "",
    bound_root_id: str = "",
) -> dict[str, object]:
    _store, _thread, task = _authorized_agent_target(
        agent,
        scope=scope,
        run_id=run_id,
        operation="cancel",
        session_task_id=session_task_id,
        bound_root_id=bound_root_id,
    )
    stable_id = str(operation_id or "").strip()
    if not stable_id:
        raise AgentControlError(400, "AGENT_STOP_ID_REQUIRED", "停止操作缺少 operation_id。")
    try:
        result = _cancel_user_controlled_agent_tree(agent, task, parent_thread=_thread)
    except OSError as exc:
        raise AgentControlError(
            503,
            "AGENT_STOP_UNAVAILABLE",
            "停止请求暂时无法确认；系统没有自动重复执行。",
        ) from exc
    return {
        "ok": result.get("ok", False),
        "operation_id": stable_id,
        "run_id": str(getattr(task, "id", "") or ""),
        "status": "cancelled" if result.get("cancel_status") == "CANCELLED" else "preserved",
        "result": result,
    }


# LLM: 接纳前在 C 内固定原树与资源；异步只做清理和父级通知，同 run 去重不授予读取新代次的权限。
# 函数用途: 让 TUI/Web 在原停止准备完成后立即返回，不等待慢进程退出。
def enqueue_agent_stop(
    agent: object,
    *,
    scope: object,
    run_id: str,
    operation_id: str,
    session_task_id: str = "",
    bound_root_id: str = "",
) -> dict[str, object]:
    _store, _thread, task = _authorized_agent_target(
        agent,
        scope=scope,
        run_id=run_id,
        operation="cancel",
        session_task_id=session_task_id,
        bound_root_id=bound_root_id,
    )
    stable_id = str(operation_id or "").strip()
    if not stable_id:
        raise AgentControlError(400, "AGENT_STOP_ID_REQUIRED", "停止操作缺少 operation_id。")
    stable_run_id = str(getattr(task, "id", "") or "").strip()
    owner_id = str(
        getattr(getattr(agent, "home_paths", None), "owner_id", "") or ""
    ).strip()
    active_key = (owner_id, stable_run_id)
    with _AGENT_STOP_LOCK:
        already_active = active_key in _AGENT_STOP_ACTIVE
        if not already_active:
            _AGENT_STOP_ACTIVE.add(active_key)

    if not already_active:
        existing_receipt = (getattr(task, "attributes", None) or {}).get("user_agent_stop_receipt")
        if isinstance(existing_receipt, dict) and existing_receipt.get("cleanup_confirmed") is True:
            with _AGENT_STOP_LOCK:
                _AGENT_STOP_ACTIVE.discard(active_key)
            status_str = str(getattr(task, "status", "") or "").strip().upper()
            return {
                "ok": True,
                "operation_id": stable_id,
                "run_id": stable_run_id,
                "status": "confirmed" if status_str == "CANCELLED" else "already_terminal",
                "already_active": False,
                "replayed": str(existing_receipt.get("operation_id") or "") == stable_id,
            }

        was_non_cancel_terminal = _task_is_terminal(task) and str(getattr(task, "status", "") or "").strip().upper() != "CANCELLED"

        try:
            batch = prepare_subagent_stops(agent, [CancelSubagentTaskRequest(
                task, "用户从代理详情页按 Esc 停止", source="user_agent_control",
            )])
        except Exception as exc:
            with _AGENT_STOP_LOCK:
                _AGENT_STOP_ACTIVE.discard(active_key)
            raise AgentControlError(503, "AGENT_STOP_UNAVAILABLE", "停止准备未确认，请重新查看状态。") from exc

        _persist_stop_receipt(agent, stable_run_id, task, {
            "operation_id": stable_id,
            "cleanup_confirmed": False,
            "requested_at": time.time(),
        })

        # LLM: worker 只消费已固定 batch；父通知在控制锁外，finally 释放本次临时去重键。
        # 函数用途: 清理用户点名的原分支，并把实际取消结果交给直属父级。
        def cancel() -> None:
            try:
                result = cleanup_subagent_tree(batch, stable_run_id)
                if result.get("ok"):
                    _persist_stop_receipt(agent, stable_run_id, task, {
                        "operation_id": stable_id,
                        "cleanup_confirmed": True,
                        "requested_at": time.time(),
                    })
                if result.get("cancel_status") == "CANCELLED":
                    _deliver_stopped_agent_to_parent(agent, stable_run_id, parent_thread=_thread)
            finally:
                with _AGENT_STOP_LOCK:
                    _AGENT_STOP_ACTIVE.discard(active_key)

        threading.Thread(target=cancel, name=f"agent-stop:{stable_run_id}", daemon=True).start()
        if batch.unconfirmed:
            raise AgentControlError(503, "AGENT_STOP_UNCONFIRMED", "部分停止准备未确认；已选中的资源继续清理。")

        return {
            "ok": True,
            "operation_id": stable_id,
            "run_id": stable_run_id,
            "status": "already_terminal" if was_non_cancel_terminal else "accepted",
            "already_active": False,
        }

    return {
        "ok": True,
        "operation_id": stable_id,
        "run_id": stable_run_id,
        "status": "accepted",
        "already_active": already_active,
    }


# LLM: TUI/Web stop closes the entire authorized branch before publishing one terminal handoff.
# This is the control-plane equivalent of the runner's normal completion callback; model-owned
# cancel_subagents and root /stop keep their existing in-turn semantics and do not gain a duplicate
# wake. Parent delivery failure never rolls back an already durable cancellation.
# 函数用途: 完整停止用户点名的子代理分支，再用同一终态链通知或恢复它的直属父代理。
def _cancel_user_controlled_agent_tree(
    agent: object,
    task: object,
    *,
    parent_thread: object,
) -> dict[str, object]:
    result = cancel_subagent_tree(
        agent,
        CancelSubagentTaskRequest(
            task=task,
            reason="用户从代理详情页按 Esc 停止",
            kill_process=True,
            source="user_agent_control",
        ),
    )
    run_id = str(getattr(task, "id", "") or "").strip()
    return {
        **result,
        "parent_delivery": (
            _deliver_stopped_agent_to_parent(agent, run_id, parent_thread=parent_thread)
            if result.get("cancel_status") == "CANCELLED" else {"status": "preserved"}
        ),
    }


# LLM: 终态通知器只绑定原 Store、load 和 save；A root child uses the canonical conversation completion wake, while a nested child releases
# only its exact direct parent's wait marker and starts that durable parent through the ordinary
# idempotent dispatcher. The periodic reconciler remains the crash fallback for delivery errors.
# 函数用途: 把已取消节点交给正确的直属父级，让主代理或协调代理马上知道它已经停止。
def _deliver_stopped_agent_to_parent(
    agent: object,
    run_id: str,
    *,
    parent_thread: object,
) -> dict[str, object]:
    manager = getattr(agent, "subagents", None)
    try:
        task = manager.load(run_id)
        from ..agent_core.orchestration.dispatch.capability_auto_sweep import (
            auto_start_orphan_run,
        )
        from ..subagents.direct_parent_lifecycle import (
            reconcile_parent_wait_for_child,
        )
        from ..subagents.runner_completion_wake import RunnerCompletionNotifier

        store = manager.conversation_store
        notifier = RunnerCompletionNotifier(
            tasks=store.tasks if store is not None else None,
            wakes=store.wakes if store is not None else None,
            load_task=manager.load, save_task=manager.save,
        )
        notifier.notify_controlled_cancel(task, parent_thread=parent_thread)
        decision = reconcile_parent_wait_for_child(manager, run_id)
        start_result: dict[str, object] = {}
        if decision.should_resume:
            started = auto_start_orphan_run(agent, decision.parent_run_id)
            if isinstance(started, dict):
                start_result = dict(started)
        return {
            "status": "delivered",
            "parent_run_id": decision.parent_run_id,
            "reason": decision.reason,
            "resume_requested": decision.should_resume,
            "resume_result": start_result,
        }
    except Exception as exc:
        return {
            "status": "deferred",
            **runtime_error_report(
                exc,
                context="agent_control.deliver_stopped_agent_to_parent",
            ),
        }


# LLM: 主/子审批决定先过 owner/conversation/root 门，再比对原请求与当前执行；标签、行号和客户端片段不能授权。
# 函数用途: 将 TUI/Web 对主/子后台具体工具调用的批准或拒绝写回原等待记录。
def resolve_agent_permission(
    agent: object,
    *,
    scope: object,
    run_id: str,
    request: Mapping[str, object],
    decision: Mapping[str, object],
) -> dict[str, object]:
    _store, thread, task = _authorized_agent_target(
        agent,
        scope=scope,
        run_id=run_id,
        operation="resolve_tool_approval",
    )
    is_main = str(run_id or "").strip() == str(thread.workspace_task_id or "")
    if not is_main and _task_is_terminal(task):
        raise AgentControlError(
            409,
            "AGENT_ALREADY_TERMINAL",
            "这个子代理已经结束；迟到的工具决定不会生效。",
        )
    try:
        return resolve_agent_tool_approval(
            agent,
            run_id=str(run_id).strip() if is_main else str(getattr(task, "id", "") or ""),
            request_value=request,
            decision_value=decision,
        )
    except FileNotFoundError as exc:
        raise AgentControlError(
            409,
            "AGENT_APPROVAL_STALE",
            "这条工具审批已经结束或不再等待。",
        ) from exc
    except (TypeError, ValueError) as exc:
        raise AgentControlError(
            409,
            "AGENT_APPROVAL_MISMATCH",
            "工具审批身份不匹配，决定未写入。",
        ) from exc
    except OSError as exc:
        raise AgentControlError(
            503,
            "AGENT_APPROVAL_UNAVAILABLE",
            "工具审批暂时无法写回；系统没有自动重复批准。",
        ) from exc


# LLM: 先解析认证会话与 canonical root；仅工具审批允许 root 自身。下级先过历史读取授权，运行中写操作再校验当前 attempt。
# 函数用途: 校验用户可操作的精确代理范围，不因增加主代理审批而放开子代理查看、插话或停止入口。
def _authorized_agent_target(
    agent: object,
    *,
    scope: object,
    run_id: str,
    operation: str,
    session_task_id: str = "",
    bound_root_id: str = "",
) -> tuple[ConversationStore, object, object]:
    store = getattr(agent, "conversation_store", None)
    if not isinstance(store, ConversationStore):
        raise AgentControlError(503, "AGENT_STORE_UNAVAILABLE", "代理状态存储不可用。")
    conversation_id = str(getattr(scope, "conversation_id", "") or "").strip()
    user_id = str(getattr(scope, "user_id", "") or "").strip()
    channel = str(getattr(scope, "channel", "") or "").strip()
    if not conversation_id or not user_id or not channel:
        raise AgentControlError(400, "AGENT_SCOPE_INVALID", "当前会话身份不完整。")
    try:
        sender_thread, load_error = store.threads.resolve_report(
            channel=channel,
            channel_conversation_id=conversation_id,
            channel_user_id=user_id,
        )
    except Exception as exc:
        raise AgentControlError(503, "AGENT_THREAD_UNAVAILABLE", "当前会话暂时不可读取。") from exc
    if sender_thread is None or load_error is not None:
        raise AgentControlError(404, "AGENT_THREAD_NOT_FOUND", "当前会话还没有可查看的代理任务。")

    owner_id = str(
        getattr(getattr(agent, "home_paths", None), "owner_id", "") or ""
    ).strip()
    clean_st_id = str(session_task_id or "").strip()
    clean_root_id = str(bound_root_id or "").strip()

    if not clean_st_id and not clean_root_id:
        root_id = str(getattr(sender_thread, "workspace_task_id", "") or "").strip()
        try:
            link, link_error = store.tasks.load_report(root_id)
        except Exception as exc:
            raise AgentControlError(503, "AGENT_ROOT_UNAVAILABLE", "当前主任务状态暂时不可读取。") from exc
        if (
            not root_id
            or link is None
            or link_error is not None
            or str(getattr(link, "thread_id", "") or "").strip()
            != str(getattr(sender_thread, "thread_id", "") or "").strip()
        ):
            raise AgentControlError(404, "AGENT_ROOT_NOT_FOUND", "当前会话没有可操作的主任务树。")
        verified_root_id = root_id
        owning_thread = sender_thread

    elif clean_st_id:
        session_tasks = getattr(store, "session_tasks", None)
        if session_tasks is None or not hasattr(session_tasks, "load"):
            raise AgentControlError(503, "AGENT_TASK_UNAVAILABLE", "会话任务存储不可用。")
        try:
            st = session_tasks.load(clean_st_id)
        except (OSError, ValueError) as exc:
            raise AgentControlError(404, "AGENT_TASK_NOT_FOUND", "目标后台任务不存在或不可读。") from exc
        if st is None:
            raise AgentControlError(404, "AGENT_TASK_NOT_FOUND", "目标后台任务不存在。")
        if str(getattr(st, "sender_thread_id", "") or "").strip() != str(getattr(sender_thread, "thread_id", "") or "").strip():
            raise AgentControlError(403, "AGENT_OUTSIDE_CONVERSATION", "目标后台任务不属于当前会话。")

        target_thread_id = str(getattr(st, "target_thread_id", "") or "").strip()
        try:
            target_thread, target_err = store.threads.load_report(target_thread_id)
        except Exception as exc:
            raise AgentControlError(503, "AGENT_THREAD_UNAVAILABLE", "目标后台执行会话暂时不可读取。") from exc
        if target_thread is None or target_err is not None:
            raise AgentControlError(404, "AGENT_THREAD_NOT_FOUND", "目标后台执行会话不可读取。")

        if owner_id and getattr(target_thread, "owner_id", "") and str(target_thread.owner_id).strip() != owner_id:
            raise AgentControlError(403, "AGENT_OUTSIDE_CONVERSATION", "目标后台任务超出当前用户。")

        from .execution_threads import IM_TASK_EXECUTION_KEY, execution_origin_thread
        policy = (getattr(target_thread, "metadata", None) or {}).get(IM_TASK_EXECUTION_KEY)
        if isinstance(policy, dict):
            try:
                origin_thread = execution_origin_thread(store.threads.require, target_thread)
            except (DataCorruptionError, KeyError, FileNotFoundError) as exc:
                raise AgentControlError(403, "AGENT_OUTSIDE_CONVERSATION", "后台执行来源会话无效。") from exc
            if str(getattr(origin_thread, "thread_id", "") or "").strip() != str(getattr(sender_thread, "thread_id", "") or "").strip():
                raise AgentControlError(403, "AGENT_OUTSIDE_CONVERSATION", "后台执行来源会话与当前会话不匹配。")
            if str(policy.get("session_task_id") or "") not in {"", st.task_id}:
                raise AgentControlError(403, "AGENT_OUTSIDE_CONVERSATION", "后台执行策略中的任务引用不匹配。")
            verified_root_id = str(policy.get("root_task_id") or getattr(target_thread, "workspace_task_id", "") or "").strip()
        else:
            verified_root_id = clean_root_id or str(getattr(target_thread, "workspace_task_id", "") or "").strip()

        if clean_root_id and verified_root_id and clean_root_id != verified_root_id:
            raise AgentControlError(403, "AGENT_OUTSIDE_CONVERSATION", "卡片绑定的根任务与后台任务不一致。")

        if not verified_root_id:
            raise AgentControlError(404, "AGENT_ROOT_NOT_FOUND", "目标后台任务尚未建立子代理主任务树。")

        try:
            link, link_err = store.tasks.load_report(verified_root_id)
        except Exception as exc:
            raise AgentControlError(503, "AGENT_ROOT_UNAVAILABLE", "目标后台任务状态暂时不可读取。") from exc
        if (
            link is None
            or link_err is not None
            or str(getattr(link, "thread_id", "") or "").strip() != str(getattr(target_thread, "thread_id", "") or "").strip()
        ):
            raise AgentControlError(404, "AGENT_ROOT_NOT_FOUND", "目标后台任务尚未建立子代理主任务树。")
        owning_thread = target_thread

    else:
        try:
            link, link_err = store.tasks.load_report(clean_root_id)
        except Exception as exc:
            raise AgentControlError(503, "AGENT_ROOT_UNAVAILABLE", "当前主任务状态暂时不可读取。") from exc
        if link is None or link_err is not None:
            raise AgentControlError(404, "AGENT_ROOT_NOT_FOUND", "当前卡片绑定的主任务不存在。")
        if str(getattr(link, "thread_id", "") or "").strip() != str(getattr(sender_thread, "thread_id", "") or "").strip():
            raise AgentControlError(403, "AGENT_OUTSIDE_CONVERSATION", "目标主任务不属于当前会话。")
        verified_root_id = clean_root_id
        owning_thread = sender_thread

    if operation == "resolve_tool_approval" and str(run_id or "").strip() == verified_root_id:
        return store, owning_thread, link

    manager = getattr(agent, "subagents", None)
    if manager is None:
        raise AgentControlError(503, "AGENT_MANAGER_UNAVAILABLE", "子代理管理器不可用。")
    try:
        task = manager.load(str(run_id or "").strip())
    except (FileNotFoundError, ValueError) as exc:
        raise AgentControlError(404, "AGENT_NOT_FOUND", "目标子代理不存在。") from exc

    if str(getattr(task, "root_id", "") or "").strip() != verified_root_id:
        raise AgentControlError(403, "AGENT_OUTSIDE_CONVERSATION", "目标子代理不属于当前会话。")

    owner_id = str(
        getattr(getattr(agent, "home_paths", None), "owner_id", "") or ""
    ).strip()
    try:
        task = authorize_operation(
            manager,
            OperationRequest(
                operation="view",
                run_id=str(getattr(task, "id", "") or ""),
                requester_owner=owner_id,
                requester_run_id=verified_root_id,
            ),
            target=task,
        )
    except PermissionError as exc:
        raise AgentControlError(403, "AGENT_CONTROL_DENIED", "无权操作这个子代理。") from exc

    if operation != "view" and not _task_is_terminal(task):
        try:
            task = authorize_operation(
                manager,
                OperationRequest(
                    operation=operation,
                    run_id=str(getattr(task, "id", "") or ""),
                    requester_owner=owner_id,
                    requester_run_id=verified_root_id,
                ),
                target=task,
            )
        except PermissionError as exc:
            raise AgentControlError(
                403,
                "AGENT_CONTROL_DENIED",
                "无权操作这个子代理。",
            ) from exc

    return store, owning_thread, task


# LLM: Terminal checks use the canonical status enum only; result prose and UI
# labels can never make an agent writable or stoppable.
# 函数用途: 判断详情页是否已经进入只读终态。
def _task_is_terminal(task: object) -> bool:
    return task_status_in(
        str(getattr(task, "status", "") or "").strip(),
        SUBAGENT_ENDED_STATUSES,
    )


__all__ = [
    "AgentControlError",
    "enqueue_agent_stop",
    "list_authorized_task_panel",
    "read_agent_view",
    "resolve_agent_permission",
    "send_agent_guidance",
    "stop_agent",
]
