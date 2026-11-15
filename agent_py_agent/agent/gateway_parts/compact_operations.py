# LLM: 闲置维护只复用原摘要/检查点/线程CAS；不占长前台车道，活动变化让位且不计模型失败。
# 模块用途: 核验用户静默、容量、运行资源和活动代后执行一次低优先级闲置压缩。
from __future__ import annotations

import math
import stat
import threading
import time
from collections import OrderedDict, deque
from contextlib import nullcontext
from types import SimpleNamespace

from ..common.cancellation import ToolCancelled
from ..common.json_io import read_json_object_report
from ..conversation.background_process_view import list_background_processes
from ..conversation.claim_activity_projection import claim_activity_state
from ..conversation.compact import (
    ConversationCompactOptions,
    inspect_conversation_context,
    prepare_conversation_context,
)
from ..conversation.compact_threshold_policy import (
    compact_policy_scope,
    resolve_thread_compact_policy,
)
from ..conversation.models import THREAD_TASK_LINK_INACTIVE_STATUSES
from ..conversation.run_claim import user_input_turn_on_lane
from ..conversation.store_threads import IdleCompactActivityChanged
from ..io.bounded_directory import DirectoryCursor
from ..settings.config_layers import compact_idle_parameters
from ..settings.defaults import default_config_int
from ..settings.model_scope import selected_model_scope
from ..user_space.directory_paging import directory_page
from .background_resource_report import session_background_processes


class _IdleApprovalScanPolicy:
    """内部审批目录有界恢复扫描参数（非外部用户配置）。"""

    batch_size: int = 512
    max_cursors: int = 256
    max_tracked_subdirs: int = 4096
    clean_stamp_ttl_seconds: float = 300.0


_IDLE_APPROVAL_SCAN_LOCK = threading.Lock()
_IDLE_APPROVAL_SCAN_CURSORS: OrderedDict[tuple[str, str, str], dict[str, object]] = OrderedDict()


def _root_is_settled_terminal_history(agent, store, root_task_id: str) -> bool:
    """历史根任务已终态落盘且无未决子代理或能力申请时，免于计入活跃目录溢出预算。"""
    tasks_api = getattr(store, "tasks", None)
    load_report = getattr(tasks_api, "load_report", None)
    if not callable(load_report):
        return False
    try:
        link, error = load_report(root_task_id)
    except Exception:
        return False
    if error is not None or link is None:
        return False
    status = str(getattr(getattr(link, "status", None), "value", getattr(link, "status", "")) or "").strip().lower()
    if status not in THREAD_TASK_LINK_INACTIVE_STATUSES:
        return False
    subagents = getattr(agent, "subagents", None)
    list_runs = getattr(subagents, "list_runs_for_root_report", None)
    if not callable(list_runs):
        return True
    try:
        run_report = list_runs(root_task_id)
    except Exception:
        return False
    if getattr(run_report, "load_errors", None):
        return False
    for run in getattr(run_report, "runs", ()) or ():
        run_status = str(getattr(getattr(run, "status", None), "value", getattr(run, "status", "")) or "").strip().upper()
        if run_status not in {"DONE", "FAILED", "CANCELLED"}:
            return False
        for req in getattr(run, "capability_requests", ()) or ():
            req_status = str(
                getattr(getattr(req, "status", None), "value", getattr(req, "status", "OPEN")) or "OPEN"
            ).strip().upper()
            if req_status == "OPEN":
                return False
    return True


def _next_in_progress_approval_thread(
    storage_root: object, owner_key: str, checked_keys: set[tuple[str, str]]
) -> tuple[str | None, int]:
    """返回尚未收口且未命中的审批扫描线程，避免跨 >256 线程轮转时被 LRU 提前淘汰。"""
    prefix_root = str(storage_root)
    with _IDLE_APPROVAL_SCAN_LOCK:
        for cursor_key, state in _IDLE_APPROVAL_SCAN_CURSORS.items():
            if cursor_key[0] != prefix_root or cursor_key[1] != owner_key:
                continue
            cand_thread_id = cursor_key[2]
            if (owner_key, cand_thread_id) in checked_keys:
                continue
            if (
                state.get("phase") != "done"
                and state.get("hit_path") is None
                and not state.get("subdir_overflow")
            ):
                ticks = state.get("ticks", 0)
                return cand_thread_id, ticks if isinstance(ticks, int) else 0
    return None, 0


def _drop_in_progress_approval_cursor(storage_root: object, owner_key: str, thread_id: str) -> None:
    cursor_key = (str(storage_root), owner_key, thread_id)
    with _IDLE_APPROVAL_SCAN_LOCK:
        _IDLE_APPROVAL_SCAN_CURSORS.pop(cursor_key, None)


def _retire_stale_approval_cursor(
    storage_root: object, owner_key: str, thread_id: str, *, ticks_before: int
) -> None:
    """若优先续扫未触发审批游标推进，则清除陈旧未完成记录以防阻塞其余线程。"""
    cursor_key = (str(storage_root), owner_key, thread_id)
    with _IDLE_APPROVAL_SCAN_LOCK:
        state = _IDLE_APPROVAL_SCAN_CURSORS.get(cursor_key)
        if state is None:
            return
        ticks_raw = state.get("ticks", 0)
        ticks_after = ticks_raw if isinstance(ticks_raw, int) else 0
        if (
            state.get("phase") != "done"
            and state.get("hit_path") is None
            and not state.get("subdir_overflow")
            and ticks_after <= ticks_before
        ):
            _IDLE_APPROVAL_SCAN_CURSORS.pop(cursor_key, None)


# LLM: 只读任务/claim/子代理的含错报告及原审批账；任何坏读失败关闭，状态正文不参与裁决。
# 函数用途: 按原run属性定位同会话运行/待审批，未知归属保守让位，不续租或清理审批。
def idle_compact_busy_state(agent, store, thread):
    if user_input_turn_on_lane(store, thread.thread_id) or thread.active_task_ids:
        return "foreground_active"
    claim, error = store.claims.load_report(thread.thread_id)
    if error:
        return "activity_unknown"
    if claim.get("status") in {"running", "claimed"}:
        return "foreground_active"
    claim_state = claim_activity_state(store.storage, thread.thread_id)
    if claim_state:
        return claim_state
    try:
        _tasks, task_errors = store.tasks.active_report(thread.thread_id)
        report = _idle_subagent_report(agent.subagents, thread)
        if task_errors or report.load_errors:
            return "activity_unknown"
        for run in report.runs:
            status = getattr(run, "status", "")
            status = getattr(status, "value", status)
            attributes = getattr(run, "attributes", {}) or {}
            run_thread = str(getattr(run, "conversation_thread_id", "") or getattr(run, "thread_id", "")
                             or attributes.get("conversation_thread_id", ""))
            if not run_thread or run_thread == thread.thread_id:
                requests = getattr(run, "capability_requests", ()) or ()
                if any(getattr(getattr(r, "status", ""), "value", getattr(r, "status", "")) == "OPEN" for r in requests):
                    return "pending_approval"
                if status == "WAITING_APPROVAL":
                    return "pending_approval"
                if status == "BLOCKED":
                    return "subagent_blocked"
                if status in {"RUNNING", "PENDING", "PLANNING"}:
                    return "subagent_active"
        pending = _idle_pending_approval_state(agent, store, thread)
        if pending:
            return pending
        report = list_background_processes(agent.home_paths.owner_home_dir, thread_id=thread.thread_id, active_only=True)
        if not report["projection_ok"]:
            return "activity_unknown"
        if session_background_processes(report["items"], thread.thread_id):
            return "background_process_active"
    except (AttributeError, OSError, ValueError, TypeError, RuntimeError):
        return "activity_unknown"
    return ""


# LLM: 真实管理器只读运行索引的恢复集合与当前义务，再逐 id 回读 canonical；坏索引/坏行原样进 load_errors。
#   旧适配器（无索引读口）沿原全量报告；状态与审批判据仍由调用方按原规则逐条裁决。
# 函数用途: 为闲置判定取得相关当前子代理，不遍历线程历史任务谱系。
def _idle_subagent_report(manager, thread):
    indexed = getattr(manager, "list_recovery_runs_report", None)
    if not callable(indexed):
        return manager.list_runs_report()
    runs, errors = {}, []
    reports = [indexed()]
    current = getattr(manager, "list_current_runs_report", None)
    if callable(current):
        # 恢复集不含终态待批；无绑定线程的义务仍须让所有会话保守让位。
        reports.append(current(statuses=("BLOCKED", "WAITING_APPROVAL"), thread_id=thread.thread_id,
                               include_unbound_thread=True, include_open_capabilities=True))
    for report in reports:
        errors.extend(report.load_errors)
        runs.update((str(getattr(run, "id", "") or ""), run) for run in report.runs)
    return SimpleNamespace(runs=list(runs.values()), load_errors=errors)


def _idle_approval_scan_batch(agent) -> int:
    configured = getattr(getattr(agent, "config", None), "retention_scan_slice_entries", None)
    if type(configured) is int and configured > 0:
        return configured
    return _IdleApprovalScanPolicy.batch_size


def _approval_dir_stamp(directory):
    try:
        info = directory.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise OSError("approval directory is not a regular directory")
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns)


def _save_idle_scan_state(key: tuple[str, str, str], state: dict[str, object]) -> None:
    with _IDLE_APPROVAL_SCAN_LOCK:
        _IDLE_APPROVAL_SCAN_CURSORS.pop(key, None)
        while len(_IDLE_APPROVAL_SCAN_CURSORS) >= _IdleApprovalScanPolicy.max_cursors:
            _IDLE_APPROVAL_SCAN_CURSORS.popitem(last=False)
        _IDLE_APPROVAL_SCAN_CURSORS[key] = state
        _IDLE_APPROVAL_SCAN_CURSORS.move_to_end(key)


def _round_stamps_match(state, approvals_dir, confirmations_dir, current_approvals_stamp, current_confirmations_stamp) -> bool:
    if state["approvals_stamp"] != current_approvals_stamp or state["confirmations_stamp"] != current_confirmations_stamp:
        return False
    active_root = state.get("active_root")
    if active_root is not None and _approval_dir_stamp(approvals_dir / active_root) != state["active_root_stamp"]:
        return False
    for subdir, saved_stamp in state["subdir_stamps"].items():
        if _approval_dir_stamp(approvals_dir / subdir) != saved_stamp:
            return False
    return True


def _inspect_approval_record(path, thread_id: str) -> str:
    if path.name.startswith(".") or not path.name.endswith(".json"):
        return ""
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise OSError("approval symlink rejected during idle scan")
    if not stat.S_ISREG(info.st_mode):
        return ""
    report = read_json_object_report(path, context="conversation.idle.approvals")
    row = next((v for k, v in vars(report).items() if k != "load_error"), None)
    if report.load_error or not isinstance(row, dict) or not row:
        return "activity_unknown"
    if row.get("status") not in {"pending", "active", "waiting_input", "waiting_confirmation"}:
        return ""
    expires = row.get("expires_at")
    if expires is not None:
        if type(expires) not in {int, float} or not math.isfinite(expires):
            return "activity_unknown"
        if expires <= time.time():
            return ""
    scope = row.get("scope", {})
    tid = row.get("thread_id") or (scope.get("thread_id") if isinstance(scope, dict) else None)
    if not tid:
        return "activity_unknown"
    if tid == thread_id:
        return "pending_approval"
    return ""


def _new_idle_scan_state(approvals_stamp, confirmations_stamp) -> dict[str, object]:
    return {
        "phase": "approvals",
        "approvals_stamp": approvals_stamp,
        "roots_cursor": {},
        "roots_complete": approvals_stamp is None,
        "pending_roots": deque(),
        "active_root": None,
        "active_root_stamp": None,
        "active_root_cursor": {},
        "active_root_complete": False,
        "pending_files": deque(),
        "subdir_stamps": {},
        "subdir_overflow": False,
        "confirmations_stamp": confirmations_stamp,
        "confirmations_cursor": {},
        "confirmations_complete": confirmations_stamp is None,
        "visited_count": 0,
        "ticks": 0,
        "hit_path": None,
        "completed_at": None,
    }


def _consume_pending_approval_files(folder, thread_id: str, state: dict[str, object], remaining: int) -> tuple[str | None, int]:
    pending_files = state["pending_files"]
    while remaining > 0 and pending_files:
        name = pending_files.popleft()
        remaining -= 1
        state["visited_count"] += 1
        if name.startswith(".") or not name.endswith(".json"):
            continue
        candidate = folder / name
        verdict = _inspect_approval_record(candidate, thread_id)
        if verdict == "activity_unknown":
            return "activity_unknown", remaining
        if verdict == "pending_approval":
            state["hit_path"] = candidate
            return "pending_approval", remaining
    return None, remaining


# LLM: 按目录分页与LRU续点有界扫描审批与私卡路径；未扫完绝不判空，目录变动、坏记录或超时失败关闭，零句柄泄漏。
# 函数用途: 保护仍可处理的审批/确认；明确到期只跳过，坏期限/未知归属失败关闭，不消费或清理记录。
def _idle_pending_approval_state(agent, store, thread):
    from ..path_access_policy import PRIVATE_INTERACTIONS_OWNER_PARTS

    storage_root = store.storage.root
    owner_home_dir = agent.home_paths.owner_home_dir
    approvals_dir = storage_root / "subagent_tool_approvals"
    confirmations_dir = owner_home_dir.joinpath(*PRIVATE_INTERACTIONS_OWNER_PARTS)
    key = (str(storage_root), str(owner_home_dir), str(thread.thread_id))
    scan_budget = _idle_approval_scan_batch(agent)

    with _IDLE_APPROVAL_SCAN_LOCK:
        state = _IDLE_APPROVAL_SCAN_CURSORS.pop(key, None)

    try:
        approvals_stamp = _approval_dir_stamp(approvals_dir)
        confirmations_stamp = _approval_dir_stamp(confirmations_dir)
        if state is not None and not _round_stamps_match(
            state, approvals_dir, confirmations_dir, approvals_stamp, confirmations_stamp
        ):
            state = None

        if state is not None and state["phase"] == "done":
            completed_at = state.get("completed_at")
            if (
                not state["subdir_overflow"]
                and completed_at is not None
                and time.monotonic() - completed_at <= _IdleApprovalScanPolicy.clean_stamp_ttl_seconds
            ):
                _save_idle_scan_state(key, state)
                return ""
            state = None

        if state is None:
            state = _new_idle_scan_state(approvals_stamp, confirmations_stamp)

        hit_path = state.get("hit_path")
        if hit_path is not None:
            try:
                recheck = _inspect_approval_record(hit_path, thread.thread_id)
            except FileNotFoundError:
                state = _new_idle_scan_state(approvals_stamp, confirmations_stamp)
                hit_path = None
            else:
                if recheck == "pending_approval":
                    if not _round_stamps_match(
                        state, approvals_dir, confirmations_dir,
                        _approval_dir_stamp(approvals_dir), _approval_dir_stamp(confirmations_dir),
                    ):
                        return "activity_unknown"
                    _save_idle_scan_state(key, state)
                    return "pending_approval"
                if recheck == "activity_unknown":
                    return "activity_unknown"
                state["hit_path"] = None

        state["ticks"] = int(state.get("ticks", 0)) + 1
        remaining = scan_budget
        while remaining > 0:
            if state["phase"] == "approvals":
                active_root = state["active_root"]
                if active_root is not None:
                    active_dir = approvals_dir / active_root
                    verdict, remaining = _consume_pending_approval_files(
                        active_dir, thread.thread_id, state, remaining
                    )
                    if verdict == "activity_unknown":
                        return "activity_unknown"
                    if verdict == "pending_approval":
                        if not _round_stamps_match(
                            state, approvals_dir, confirmations_dir,
                            _approval_dir_stamp(approvals_dir), _approval_dir_stamp(confirmations_dir),
                        ):
                            return "activity_unknown"
                        _save_idle_scan_state(key, state)
                        return "pending_approval"
                    if not state["pending_files"] and not state["active_root_complete"]:
                        if remaining <= 0:
                            break
                        names, cur, done = directory_page(
                            storage_root,
                            ("subagent_tool_approvals", active_root),
                            state["active_root_cursor"],
                            remaining,
                            require_dir_fd=False,
                        )
                        state["active_root_cursor"] = cur
                        state["active_root_complete"] = done
                        state["pending_files"].extend(names)
                        if not names and not done:
                            _save_idle_scan_state(key, state)
                            return "activity_unknown"
                        continue
                    if not state["pending_files"] and state["active_root_complete"]:
                        if _approval_dir_stamp(active_dir) != state["active_root_stamp"]:
                            return "activity_unknown"
                        if not _root_is_settled_terminal_history(agent, store, active_root):
                            if len(state["subdir_stamps"]) < _IdleApprovalScanPolicy.max_tracked_subdirs:
                                state["subdir_stamps"][active_root] = state["active_root_stamp"]
                            else:
                                state["subdir_overflow"] = True
                        state["active_root"] = None
                        state["active_root_stamp"] = None
                        state["active_root_cursor"] = {}
                        state["active_root_complete"] = False
                    continue

                pending_roots = state["pending_roots"]
                if not pending_roots and not state["roots_complete"]:
                    if _approval_dir_stamp(approvals_dir) is None:
                        state["roots_complete"] = True
                    else:
                        names, cur, done = directory_page(
                            storage_root,
                            ("subagent_tool_approvals",),
                            state["roots_cursor"],
                            remaining,
                            require_dir_fd=False,
                        )
                        state["roots_cursor"] = cur
                        state["roots_complete"] = done
                        pending_roots.extend(names)
                        if not names and not done:
                            _save_idle_scan_state(key, state)
                            return "activity_unknown"
                        continue
                if pending_roots:
                    subdir = pending_roots.popleft()
                    remaining -= 1
                    state["visited_count"] += 1
                    if subdir.startswith("."):
                        continue
                    subdir_path = approvals_dir / subdir
                    info = subdir_path.lstat()
                    if stat.S_ISLNK(info.st_mode):
                        return "activity_unknown"
                    if not stat.S_ISDIR(info.st_mode):
                        continue
                    state["active_root"] = subdir
                    state["active_root_stamp"] = _approval_dir_stamp(subdir_path)
                    state["active_root_cursor"] = {}
                    state["active_root_complete"] = False
                    state["pending_files"].clear()
                    continue
                state["phase"] = "confirmations"
                continue

            if state["phase"] == "confirmations":
                verdict, remaining = _consume_pending_approval_files(
                    confirmations_dir, thread.thread_id, state, remaining
                )
                if verdict == "activity_unknown":
                    return "activity_unknown"
                if verdict == "pending_approval":
                    if not _round_stamps_match(
                        state, approvals_dir, confirmations_dir,
                        _approval_dir_stamp(approvals_dir), _approval_dir_stamp(confirmations_dir),
                    ):
                        return "activity_unknown"
                    _save_idle_scan_state(key, state)
                    return "pending_approval"
                if not state["pending_files"] and not state["confirmations_complete"]:
                    if remaining <= 0:
                        break
                    if _approval_dir_stamp(confirmations_dir) is None:
                        state["confirmations_complete"] = True
                    else:
                        names, cur, done = directory_page(
                            owner_home_dir,
                            PRIVATE_INTERACTIONS_OWNER_PARTS,
                            state["confirmations_cursor"],
                            remaining,
                            require_dir_fd=False,
                        )
                        state["confirmations_cursor"] = cur
                        state["confirmations_complete"] = done
                        state["pending_files"].extend(names)
                        if not names and not done:
                            _save_idle_scan_state(key, state)
                            return "activity_unknown"
                        continue
                if not state["pending_files"] and state["confirmations_complete"]:
                    break

        if not _round_stamps_match(
            state, approvals_dir, confirmations_dir,
            _approval_dir_stamp(approvals_dir), _approval_dir_stamp(confirmations_dir),
        ):
            return "activity_unknown"

        if (
            state["phase"] == "confirmations"
            and state["confirmations_complete"]
            and not state["pending_files"]
        ):
            if state["subdir_overflow"] and int(state.get("ticks", 0)) > 1:
                return "activity_unknown"
            if state["visited_count"] > scan_budget and not state["subdir_overflow"]:
                state["phase"] = "done"
                state["completed_at"] = time.monotonic()
                _save_idle_scan_state(key, state)
            return ""

        _save_idle_scan_state(key, state)
        return "activity_unknown"
    except (OSError, ValueError, TypeError, RuntimeError):
        return "activity_unknown"


# LLM: 禁用先返回；模型scope显式禁止旧线程模型迁移，容量及其它只读资格不写线程；政策沿同次scope冻结。
# 函数用途: 为一次空闲压缩建立只读基线，只有真正提交摘要才更新线程。
def maybe_auto_compact_idle_session(agent, store, thread, *, now=None):
    if not all(compact_idle_parameters(getattr(agent, "config", None))):
        return {"thread_id": thread.thread_id, "compacted": False, "reason": "disabled"}
    latest = store.threads.require(thread.thread_id)
    with selected_model_scope(agent, thread_id=latest.thread_id, active=False, initialize_thread=False), compact_policy_scope(agent, latest):
        return _compact_idle_candidate(agent, store, latest, now=now)


# LLM: 用户代与规范消息指纹联合幂等；摘要/计量/成功及失败写口均重核，存储CAS拒绝统一转干净让位，不清零旧失败。
# 函数用途: 确认只读安全门后沿原摘要链压缩；直接追加或同长改写也会废弃旧候选。
def _compact_idle_candidate(agent, store, thread, *, now=None):
    policy = resolve_thread_compact_policy(agent, thread)
    idle_threshold, idle_seconds = compact_idle_parameters(getattr(agent, "config", None))
    result = {"thread_id": thread.thread_id, "compacted": False}
    if not idle_threshold or not idle_seconds:
        return {**result, "reason": "disabled"}
    latest = store.threads.require(thread.thread_id)
    metadata = latest.metadata or {}
    generation = metadata.get("user_activity_generation", 0)
    timestamp = metadata.get("last_user_activity_at", latest.updated_at)
    if (type(generation) is not int or generation < 0 or type(timestamp) not in {int, float}
            or not math.isfinite(timestamp)):
        return {**result, "reason": "activity_unknown"}
    current = float(time.time() if now is None else now)
    if not math.isfinite(current):
        return {**result, "reason": "activity_unknown"}
    if current - timestamp < idle_seconds:
        return {**result, "reason": "not_idle"}
    busy = idle_compact_busy_state(agent, store, latest)
    if busy:
        return {**result, "reason": busy}
    marker, errors = store.messages.tail_marker_report(latest.thread_id)
    if errors:
        return {**result, "reason": "activity_unknown"}
    previous = metadata.get("idle_auto_compact", {})
    if (previous.get("last_idle_compacted_activity_gen") == generation and previous.get("message_tail") == marker):
        return {**result, "reason": "already_compacted"}
    usage = inspect_conversation_context(agent, store, latest)
    used, window = usage.projected_tokens, usage.context_window_tokens
    # 闲置水位相对于同次普通压缩的生效预算，整数比较保留精确边界。
    effective_trigger = policy["effective_trigger_percent"]
    if window <= 0 or used * 10000 < window * effective_trigger * idle_threshold:
        return {**result, "reason": "below_threshold"}
    if (used * 100 >= window * policy["effective_trigger_percent"]
            and metadata.get("last_compacted_user_activity_gen") == generation
            and metadata.get("last_compacted_message_tail") == marker):
        return {**result, "reason": "regular_compacted"}

    # LLM: 摘要前后及最终提交共用活动代与完整消息指纹；坏读或变更立即让位，不占长执行车道。
    # 函数用途: 为原压缩链提供可取消检查和最终短提交检查。
    def interrupted():
        row = store.threads.require(latest.thread_id)
        current_tail, errors = store.messages.tail_marker_report(latest.thread_id)
        return (row.metadata.get("user_activity_generation", 0) != generation
                or bool(errors) or current_tail != marker
                or bool(idle_compact_busy_state(agent, store, row)))

    try:
        compacted = prepare_conversation_context(agent, store, latest, options=ConversationCompactOptions(
            force=True, idle_compact=True, expected_user_activity_generation=generation, expected_message_tail=marker,
            interrupt_check=interrupted, commit_guard=interrupted, trigger_source="idle"))
    except (IdleCompactActivityChanged, InterruptedError, ToolCancelled):
        return {**result, "reason": "activity_changed", "cas_aborted": True, "aborted_by_concurrent_activity": True}
    return {**result, "compacted": bool(compacted.compacted), "reason": "compacted" if compacted.compacted else "no_source"}


# LLM: 易失目录流只选规范线程，实际条目读取与业务检查各守配置预算；不迁移模型，不新建冷Agent。
# 函数用途: 有界维护基础及已加载owner的空闲会话，坏记录只计失败，不阻挡后续线程。
def idle_compact_maintenance_tick(base_agent, *, cursor, now=None):
    pool = getattr(base_agent, "_owner_pool", None)
    candidates = [
        item
        for item in [base_agent, *(pool.active_agents() if pool is not None else [])]
        if getattr(getattr(item, "home_paths", None), "owner_home_dir", None) is not None
    ]
    scans = cursor.setdefault("directories", {})
    for key in scans.keys() - {str(item.home_paths.owner_home_dir) for item in candidates}:
        scans.pop(key).close()
    if not candidates:
        return {"checked": 0, "compacted": 0, "failed": 0}
    agents = list({str(item.home_paths.owner_home_dir): item for item in candidates}.values())
    position = int(cursor.get("owner", 0))
    config = getattr(base_agent, "config", None)
    remaining = getattr(config, "retention_scan_slice_entries", default_config_int("retention_scan_slice_entries"))
    deadline = time.monotonic() + getattr(config, "retention_scan_slice_ms", default_config_int("retention_scan_slice_ms")) / 1000
    report = {"checked": 0, "compacted": 0, "failed": 0}
    maximum = getattr(getattr(base_agent, "config", None), "memory_compact_idle_max_per_tick", 2)
    maximum = max(0, maximum) if type(maximum) is int else 2
    checked_keys: set[tuple[str, str]] = set()
    for _ in range(maximum):
        agent = agents[position % len(agents)]
        position += 1
        store = getattr(agent, "conversation_store", None)
        if store is None:
            continue
        key = str(agent.home_paths.owner_home_dir)
        storage_root = getattr(getattr(store, "storage", None), "root", None)
        cand_thread_id, ticks_before = (
            _next_in_progress_approval_thread(storage_root, key, checked_keys)
            if storage_root is not None
            else (None, 0)
        )
        is_continuation = cand_thread_id is not None
        if is_continuation:
            if remaining <= 0 or time.monotonic() >= deadline:
                break
            remaining -= 1
            thread_id = cand_thread_id
        else:
            scan = scans.setdefault(key, DirectoryCursor())
            while remaining > 0 and time.monotonic() < deadline:
                remaining -= 1
                entry = scan.next_entry(store.storage.threads_dir)
                if entry is None or entry.name.startswith("thread-") and entry.name.endswith(".json"):
                    break
            else:
                break
            if entry is None:
                continue
            thread_id = (store.storage.threads_dir / entry.name).stem
        checked_keys.add((key, thread_id))
        row, error = store.threads.load_report(thread_id)
        report["checked"] += 1
        if error or row is None or row.owner_home and row.owner_home != key:
            if is_continuation and storage_root is not None:
                _drop_in_progress_approval_cursor(storage_root, key, thread_id)
            report["failed"] += 1
            continue
        try:
            pin = pool.pin(agent, touch=False) if pool is not None and agent is not base_agent else nullcontext()
            with pin:
                outcome = maybe_auto_compact_idle_session(agent, store, row, now=now)
            report["compacted"] += bool(outcome["compacted"])
            if is_continuation and storage_root is not None:
                _retire_stale_approval_cursor(storage_root, key, thread_id, ticks_before=ticks_before)
        except Exception:
            if is_continuation and storage_root is not None:
                _drop_in_progress_approval_cursor(storage_root, key, thread_id)
            report["failed"] += 1
    cursor["owner"] = position % len(agents)
    return report
