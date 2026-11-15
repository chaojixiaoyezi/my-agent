# LLM: 只读本 gateway owner 的托管后台进程登记表，并复用既有 ProcessSessionStore.request_stop
#   冻结停止意图；不新增登记表、不按进程名杀进程、不解析命令正文。实际回收由原 host 的
#   terminate_process_tree 按进程组完成，孙进程随之结束（只给登记 pid 发信号会漏掉孙进程）。
#   会话级 /stop（本地与 Gateway 两个入口）也从这里取"本会话遗留"的选择与回执渲染；Gateway 入口走任务停止同一个
#   freeze_process_stop/cleanup_process_stop，不另造停止路径。
#   内存诊断只供宿主显式采样，不自动启动线程或改变资源终态；仅计数和位置/字节数，不含对象正文。
# 模块用途: 为 gateway stop 与会话内 /stop 提供"列出仍在运行的后台进程"与"按显式要求一并停止"的结构化事实。
from __future__ import annotations

import gc
import json
import math
import os
import sqlite3
import stat
import sys
import threading
import time
import tracemalloc
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..conversation.models import THREAD_TASK_LINK_INACTIVE_STATUSES
from ..conversation.store import ConversationStore
from ..conversation.task_promotion import conversation_thread_execution_state
from ..io.sqlite_probe import require_plain_sqlite_files, sqlite_readonly_probe_uri
from ..runtime_db.operations import (
    AGENT_RUN_TERMINAL_STATUSES,
    ATTEMPT_STATUS_RECOVERED,
    ATTEMPT_STATUS_UNKNOWN,
    exec_lock_scope,
)
from ..runtime_db.schema import runtime_db_path
from ..scheduler.store_snapshot import read_atomic_store_bytes
from ..tooling.process_resource_stop import (
    FrozenProcessStop,
    cleanup_process_stop,
    freeze_process_stop,
)
from ..tooling.process_scope import ProcessExecutionScope
from ..tooling.process_session_cleanup import ProcessSessionCleanupError, stop_process_session
from ..tooling.process_session_records import (
    MANAGED_PROCESS_SESSION_SCHEMAS,
    PROCESS_SESSION_SCHEMA,
    PROCESS_TERMINAL_STATUSES,
    TASK_PROCESS_SESSION_SCHEMA,
)
from ..tooling.process_session_store import ProcessSessionStore, process_session_store_root

# 停止意图落到记录后，原 host 的监控循环按自己的轮询间隔发现；等待回收要留足余量。
DEFAULT_BACKGROUND_STOP_TIMEOUT_SECONDS = 10.0
# 资源报告轮询间隔 0.1 秒：接近实时又不过度占 CPU。
_POLL_SECONDS = 0.1
# tracemalloc是进程全局设施；本入口串行短快照，不能互相停止对方的追踪窗口。
_MEMORY_DIAGNOSTICS_LOCK = threading.RLock()


def _allocation_tracing_unavailable():
    enabled = getattr(sys, "_is_gil_enabled", None)
    return enabled is not None and enabled() is False


def _unavailable_tracing_metadata():
    return {"tracemalloc_scope": "unavailable_free_threaded",
            "tracemalloc_unavailable_reason": "free_threaded_runtime",
            "tracemalloc_message": "当前free-threaded运行时未提供分配追踪",
            "tracemalloc_started_at": None}


class GatewayMemoryTracing:
    """本Gateway生命周期的追踪所有权；只同步进程内诊断状态，不保留Agent或配置。"""

    def __init__(self):
        self.owned = False
        self.closed = False
        self.metadata = None

    def refresh(self, config, *, before_agent=False):
        with _MEMORY_DIAGNOSTICS_LOCK:
            if self.closed:
                raise RuntimeError("gateway memory tracing is closed")
            enabled = bool(getattr(config, "enable_gateway_memory_diagnostics", False))
            if _allocation_tracing_unavailable():
                # 诊断不能让宿主在分配器/导入交错中死锁，也不接管外部-X追踪。
                self.metadata = _unavailable_tracing_metadata() if enabled else None
                return self.metadata
            existing = tracemalloc.is_tracing()
            if not enabled and self.owned:
                tracemalloc.stop()
                self.owned = existing = False
                self.metadata = None
            if enabled and not existing:
                tracemalloc.start(1)
                self.owned = True
                self.metadata = {
                    "tracemalloc_scope": "gateway_before_agent" if before_agent else "gateway_live_window",
                    "tracemalloc_started_at": time.time(),
                }
            elif existing and not self.owned:
                self.metadata = {"tracemalloc_scope": "existing_trace", "tracemalloc_started_at": None}
            elif not existing:
                self.metadata = None
            return self.metadata

    def report(self, *, owner_pool=None):
        # 开关关闭与整份快照串行；关闭后到达的旧事件不能偷偷重开追踪或采空窗口。
        with _MEMORY_DIAGNOSTICS_LOCK:
            if self.closed or self.metadata is None:
                return None
            report = collect_runtime_memory_diagnostics(owner_pool=owner_pool, include_tracemalloc=False)
            metadata = self.metadata if "tracemalloc_scope" in report else {"tracemalloc_scope": "not_tracing"}
            return {**report, **metadata}

    def close(self):
        with _MEMORY_DIAGNOSTICS_LOCK:
            if self.closed:
                return
            self.closed = True
            if self.owned and not _allocation_tracing_unavailable():
                tracemalloc.stop()
            self.owned = False
            self.metadata = None


@contextmanager
def gateway_memory_tracing(config: object):
    """启动前打开追踪，并把原生命周期内的热开关所有权交给同一个控制器。"""
    state = GatewayMemoryTracing()
    state.refresh(config, before_agent=True)
    try:
        yield state
    finally:
        state.close()


# LLM: 仅宿主显式调用；ru_maxrss是历史峰值而非当前RSS，显式追踪只覆盖本次窗口，原已开启追踪不关闭。
# 函数用途: 采集内存峰值、GC和池计数，可选或按enable_gateway_memory_diagnostics配置给出不含正文的分配位置统计。
def collect_runtime_memory_diagnostics(
    *,
    owner_pool: object | None = None,
    config: object | None = None,
    include_tracemalloc: bool | None = None,
    top_n: int = 15,
) -> dict[str, object]:
    effective_config = (
        config
        if config is not None
        else (getattr(owner_pool, "_config", None) or getattr(owner_pool, "_base_config", None))
    )
    effective_include_tracemalloc = (
        bool(getattr(effective_config, "enable_gateway_memory_diagnostics", False))
        if include_tracemalloc is None
        else bool(include_tracemalloc)
    )
    with _MEMORY_DIAGNOSTICS_LOCK:
        if _allocation_tracing_unavailable():
            # 不调用任何tracemalloc API；外部追踪仍由原启动者负责。
            return {"rss_peak_bytes": _rss_peak_bytes(), "rss_source": "resource.ru_maxrss (peak)",
                    "gc_counts": list(gc.get_count()), "owner_pool": _owner_pool_memory_gauges(owner_pool),
                    **_unavailable_tracing_metadata()}
        already_tracing = tracemalloc.is_tracing()
        started_here = effective_include_tracemalloc and not already_tracing
        if started_here:
            tracemalloc.start()
        try:
            report: dict[str, object] = {
                "rss_peak_bytes": _rss_peak_bytes(),
                "rss_source": "resource.ru_maxrss (peak)",
                "gc_counts": list(gc.get_count()),
                "owner_pool": _owner_pool_memory_gauges(owner_pool),
            }
            if effective_include_tracemalloc or already_tracing:
                report["tracemalloc_scope"] = "existing_trace" if already_tracing else "snapshot_window"
                current, peak = tracemalloc.get_traced_memory()
                report["tracemalloc_current_bytes"] = current
                report["tracemalloc_peak_bytes"] = peak
                report["tracemalloc_overhead_bytes"] = tracemalloc.get_tracemalloc_memory()
                report["tracemalloc_frame_limit"] = tracemalloc.get_traceback_limit()
                report["tracemalloc_top"] = [
                    {"location": f"{Path(stat.traceback[0].filename).name}:{stat.traceback[0].lineno}",
                     "size_bytes": stat.size, "block_count": stat.count}
                    for stat in tracemalloc.take_snapshot().statistics("lineno")[:max(0, int(top_n))]
                ]
            return report
        finally:
            if started_here:
                tracemalloc.stop()


# LLM: resource在Windows可能不可用；不可采样返回None而非伪造0，macOS字节与其余POSIX的KiB统一为字节。
# 函数用途: 返回当前进程生命周期的RSS峰值字节数，不把峰值称为实时内存。
def _rss_peak_bytes() -> int | None:
    try:
        import resource
    except ImportError:
        return None
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return int(usage.ru_maxrss * (1 if sys.platform == "darwin" else 1024))


# LLM: 原池锁只尝试获取；诊断不得等待业务锁或在锁内调用公开property。忙时缺省计数表示未知，不能伪造0。
# 函数用途: 投影可取得的hard、soft、retired和pinned数量，缺失指标不猜成0。
def _owner_pool_memory_gauges(owner_pool: object | None) -> dict[str, int]:
    result: dict[str, int] = {}
    fields = (
        ("hard_count", "_hard_agents"), ("soft_count", "_soft_agents"),
        ("retired_count", "_retired_agents"), ("pinned_count", "_pins"),
    )
    lock = getattr(owner_pool, "_lock", None)
    if lock is not None and not lock.acquire(blocking=False):
        return result
    try:
        for name, attribute in fields:
            values = getattr(owner_pool, attribute, None)
            if isinstance(values, Mapping):
                result[name] = (sum(1 for count in values.values() if count > 0)
                                if name == "pinned_count" else len(values))
    finally:
        if lock is not None:
            lock.release()
    for name, _attribute in fields:
        if name not in result:
            value = getattr(owner_pool, name, None)
            if type(value) is int:
                result[name] = value
    return result


# LLM: 每个后台进程记录只投影结构化事实（归属、pid、启动时间、状态），不含命令正文、cwd 或输出路径。
# 类用途: 保存一条可打印、可供调用方判断的后台进程事实。
def background_process_facts(record: dict[str, object]) -> dict[str, object]:
    scope = record.get("execution_scope") if isinstance(record.get("execution_scope"), dict) else {}
    return {
        "session_id": str(record.get("session_id") or ""),
        "status": str(record.get("status") or ""),
        "pid": int(record.get("child_pid") or 0),
        "host_pid": int(record.get("pid") or 0),
        "started_at": record.get("started_at"),
        "thread_id": str(scope.get("thread_id") or ""),
        "root_task_id": str(scope.get("root_task_id") or ""),
        "run_id": str(scope.get("run_id") or ""),
        "attempt_id": str(scope.get("attempt_id") or ""),
    }


# LLM: 只列给定托管会话根目录下、仍非终态、且属于托管 schema 的记录；坏记录随事实一起返回，
#   由调用方决定是否照常打印——不静默当成"没有遗留进程"。根目录由调用方按同一条
#   process_session_store_root 口径解析，本模块不重复推导。
# 函数用途: 返回本 gateway 登记且仍在运行的后台进程事实与读取错误。
def list_running_background_processes(
    store_root: str | Path,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    store = ProcessSessionStore(Path(store_root))
    records, errors = store.list_records()
    running = [
        background_process_facts(record)
        for record in records
        if record.get("schema") in MANAGED_PROCESS_SESSION_SCHEMAS
        and record.get("status") not in PROCESS_TERMINAL_STATUSES
    ]
    return running, errors


# LLM: 停止只按已登记的精确执行身份冻结意图（owner + thread/root task 或 run 维度），不构造通配目标；
#   随后等待原 host 回收终态，按真实终态报告结果——不把"已请求"说成"已停止"。
# 函数用途: 停止给定的已登记后台进程，返回每个会话的停止结果。
def stop_background_processes(
    store_root: str | Path,
    processes: list[dict[str, object]],
    *,
    timeout_seconds: float = DEFAULT_BACKGROUND_STOP_TIMEOUT_SECONDS,
) -> list[dict[str, object]]:
    store = ProcessSessionStore(Path(store_root))
    owner_home = _owner_home(store)
    results: list[dict[str, object]] = []
    for facts in processes:
        session_id = str(facts.get("session_id") or "")
        target = ProcessExecutionScope(
            owner_home=owner_home,
            thread_id=str(facts.get("thread_id") or ""),
            root_task_id=str(facts.get("root_task_id") or ""),
            run_id=str(facts.get("run_id") or ""),
            attempt_id=str(facts.get("attempt_id") or ""),
        )
        # 空目标不能成为跨任务通配符：只停能满足精确归属校验的记录。
        if not target.matches(target):
            results.append({"session_id": session_id, "stopped": False, "reason": "scope_incomplete"})
            continue
        try:
            receipt = store.request_stop(target)
        except Exception as exc:
            results.append({"session_id": session_id, "stopped": False, "reason": type(exc).__name__})
            continue
        session_ids = tuple(str(row.get("session_id") or "") for row in receipt.records) or (session_id,)
        stopped = _wait_all_terminal(store, session_ids, timeout_seconds)
        results.append({
            "session_id": session_id,
            "stopped": stopped,
            "reason": "" if stopped else "still_running_after_request",
        })
    return results


# LLM: owner 根直接取自已登记记录的执行范围，不从 store 路径反推、不另读配置。
# 函数用途: 返回本 store 记录使用的 owner home，无记录时返回空串。
def _owner_home(store: ProcessSessionStore) -> str:
    records, _errors = store.list_records()
    for record in records:
        scope = record.get("execution_scope")
        if isinstance(scope, dict) and scope.get("owner_home"):
            return str(scope["owner_home"])
    return ""


# LLM: 只按会话记录的终态字段判定；超时即报未停止，不猜测进程已经退出。
# 函数用途: 等待给定的会话记录全部进入终态。
def _wait_all_terminal(
    store: ProcessSessionStore, session_ids: tuple[str, ...], timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while True:
        if all(_is_terminal(store, session_id) for session_id in session_ids):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_SECONDS)


# LLM: 读单条记录用同一 load 契约；缺失或读取错误都按"尚未终态"处理，避免把不可读当成已完成。
# 函数用途: 判断一条托管会话是否已进入终态。
def _is_terminal(store: ProcessSessionStore, session_id: str) -> bool:
    report = store.load(session_id)
    record = report.record
    if not record:
        return False
    return record.get("status") in PROCESS_TERMINAL_STATUSES


# LLM: 会话内停止入口只筛出属于本会话的记录，再交给上面对话级逻辑停止；不新增第二种停止路径，
#   也不把"会话"放宽成 owner 全量——thread_id 必须精确相等，避免误停同 owner 其它窗口的资源。
# 函数用途: 从已列出的运行中资源里筛出指定会话的那一批，保持原记录顺序。
def session_background_processes(
    processes: list[dict[str, object]], thread_id: str,
) -> list[dict[str, object]]:
    target = str(thread_id or "").strip()
    if not target:
        return []
    return [row for row in processes if str(row.get("thread_id") or "").strip() == target]


# LLM: 受管后台登记表地址只由产品那一个函数决定（写入端见 tooling/shell.py）；cli 层不得直接依赖
#   tooling，因此由本模块（gateway_parts）转发同一个函数，避免第二套地址推导。
# 函数用途: 返回受管后台进程登记表根目录，供 CLI 与本地控制使用。
def background_store_root(workspace_root: str | Path, owner_home: object = "") -> str:
    return str(process_session_store_root(workspace_root, owner_home))


# LLM: 用户要能看见"到底停了谁"，回执逐行列 pid 与 task/run 归属。只读传入事实、不改状态；processes 是
#   session_background_processes 过滤后的行，results 按 session_id 配对（本地 /stop 传等待回收的结果，Gateway /stop 只传
#   没冻结成的那些）；配对上且 stopped 为假的行标"尚未确认退出"，配对不上的行不编造停止状态。本地与 Gateway 两个 /stop
#   入口共用这一份渲染（原在 cli/chat_parts/control_runtime.py，C12a 起移到这里，cli 层改为导入）。
# 函数用途: 生成本次已停/未确认后台资源的可读明细行。
def background_resource_lines(
    processes: list[dict[str, object]], results: list[dict[str, object]],
) -> list[str]:
    stopped_by_session = {
        str(row.get("session_id") or ""): bool(row.get("stopped")) for row in results
    }
    lines: list[str] = []
    for facts in processes:
        session_id = str(facts.get("session_id") or "")
        try:
            pid = int(facts.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        task = str(facts.get("root_task_id") or "")
        run = str(facts.get("run_id") or "")
        owner = " / ".join(
            part for part in (f"task {task}" if task else "", f"run {run}" if run else "") if part
        )
        line = f"- pid {pid}" if pid > 0 else "- pid 未知"
        if owner:
            line += f"（{owner}）"
        if session_id in stopped_by_session and not stopped_by_session[session_id]:
            line += "，尚未确认退出"
        lines.append(line)
    return lines


# LLM: 一次会话遗留资源停止的冻结结果。batches 是原 Store 已落盘的停止清单，锁外清理只消费它们、不重扫；
#   processes 是被选中进程的结构化事实（回执列 pid 与 task/run 归属）；failed 是没能冻结的会话和原因
#   （stopped=False），调用方必须如实报未确认，不能说成已停。
# 类用途: 把"选中了谁、冻结了哪些批次、哪些没冻结成"一起交给 Gateway 的 /stop 控制入口。
@dataclass(frozen=True)
class SessionLeftoverStop:
    processes: tuple[dict[str, object], ...] = ()
    batches: tuple[FrozenProcessStop, ...] = ()
    failed: tuple[dict[str, object], ...] = ()

    # LLM: 只说冻结阶段是否完整（有没冻结成的记录，或某批后台/PTY 冻结报错），不代表进程已经退出。
    # 函数用途: 让控制回执区分"已受理"和"部分停止尚未确认"。
    @property
    def unconfirmed(self) -> bool:
        return bool(self.failed) or any(
            batch.background_freeze_error or batch.pty_request_error for batch in self.batches
        )


# LLM: C12a（2026-10-01）Gateway `/stop` 没有运行中回合时的会话级出口。只选本会话（精确 thread_id）仍非终态的托管记录，
#   排除 protected_root_task_ids（调用方给出的仍活着、由别的控制入口管的任务，如审计、分离任务或执行状态里仍在跑的任务）；
#   每组按记录自己的 (thread, root_task, run) 精确归属走任务停止同一个冻结入口 freeze_process_stop，不构造通配目标、
#   不按进程名或命令正文选。缺 run 归属的记录不停，记进 failed。登记表有读不出的记录就整体抛错，由调用方报未确认、
#   一个都不停。owner_home 必须是已 resolve 的路径（与登记记录里的 owner_home 逐字相等才匹配）。
#   副作用：为选中记录写停止意图（stop_requested），同归属的 PTY 会话一并请求回收。改口径同步 test_gateway_stop_session_leftovers.py。
# 函数用途: 冻结本会话遗留后台进程的停止意图，返回选中的进程事实、已冻结批次和没冻结成的记录。
def freeze_session_leftover_processes(
    owner_home: str, thread_id: str, protected_root_task_ids: frozenset[str],
    *, task_link_report: Callable[[str], tuple[object | None, dict | None]],
) -> SessionLeftoverStop:
    running, errors = list_running_background_processes(background_store_root(owner_home, owner_home))
    if errors:
        raise ValueError("后台进程登记表存在读不出的记录")
    mine = session_background_processes(running, thread_id)
    protected = set(protected_root_task_ids)
    # 活动索引不包含已取消的任务；停止前仍须核对资源实际引用的准确链接。
    # 先核完全部来源再写任何停止意图，坏账不能变成“任务已无主”。
    for task_id in dict.fromkeys(str(row.get("root_task_id") or "") for row in mine):
        if not task_id or task_id in protected:
            continue
        link, error = task_link_report(task_id)
        if error is not None or (link is not None and getattr(link, "thread_id", "") != thread_id):
            raise ValueError("后台资源的任务归属暂不可确认")
        if link is not None and getattr(link, "status", "") == "active":
            protected.add(task_id)
    selected = tuple(row for row in mine if str(row.get("root_task_id") or "") not in protected)
    owners = dict.fromkeys((str(row.get("root_task_id") or ""), str(row.get("run_id") or "")) for row in selected)
    batches = tuple(
        freeze_process_stop(ProcessExecutionScope(
            owner_home=owner_home, thread_id=thread_id, root_task_id=root_task_id, run_id=run_id,
        ))
        for root_task_id, run_id in owners
        if run_id
    )
    failed = tuple(
        {"session_id": str(row.get("session_id") or ""), "stopped": False, "reason": "scope_incomplete"}
        for row in selected
        if not row.get("run_id")
    )
    return SessionLeftoverStop(selected, batches, failed)


# LLM: 锁外逐批清理，只消费 freeze_session_leftover_processes 交回的固定批次（按进程组终止并核对）；单批异常不影响
#   其它批次、不扩大选择范围。结果只回给调用方（Gateway 在后台线程里调用，不等它），持久停止意图已在冻结时落盘。
# 函数用途: 按进程组回收已冻结的会话遗留后台进程，返回每批清理报告。
def cleanup_session_leftover_processes(frozen: SessionLeftoverStop) -> list[dict[str, object]]:
    reports: list[dict[str, object]] = []
    for batch in frozen.batches:
        try:
            reports.append(cleanup_process_stop(batch))
        except Exception as exc:  # noqa: BLE001 单批清理失败只记未知，不影响其它批次
            reports.append({"status": "unknown", "error_type": type(exc).__name__})
    return reports


_EXECUTION_PROCESS_SESSION_SCHEMAS = frozenset({PROCESS_SESSION_SCHEMA, TASK_PROCESS_SESSION_SCHEMA})
# 调度里 "unknown" 不能视作明确终态，必须单独保留。
_SCHEDULER_SETTLED_STATUSES = frozenset({"done", "failed", "cancelled", "skipped"})
_SCHEDULER_ACTIVE_STATUSES = frozenset({"queued", "claimed", "running", "waiting"})
_WORKFLOW_SETTLED_STATES = frozenset({
    "succeeded", "completed", "done", "failed", "cancelled", "aborted", "retired", "settled", "skipped",
})
_WAKE_ACTIVE_STATUSES = frozenset({"pending", "claimed", "waiting", "queued", "running"})
_ATTEMPT_TERMINAL_STATUSES = frozenset({*AGENT_RUN_TERMINAL_STATUSES, ATTEMPT_STATUS_RECOVERED})


@dataclass(frozen=True)
class _RuntimeDbSnapshot:
    error: str = ""
    task_runs_by_id: dict[str, dict[str, Any]] = None  # type: ignore[assignment]
    task_runs_by_task_id: dict[str, list[dict[str, Any]]] = None  # type: ignore[assignment]
    agent_runs_by_id: dict[str, dict[str, Any]] = None  # type: ignore[assignment]
    agent_runs_by_run_id: dict[str, list[dict[str, Any]]] = None  # type: ignore[assignment]
    agent_runs_by_task_run_id: dict[str, list[dict[str, Any]]] = None  # type: ignore[assignment]
    children_by_parent_id: dict[str, list[str]] = None  # type: ignore[assignment]
    attempts_by_agent_run_id: dict[str, list[dict[str, Any]]] = None  # type: ignore[assignment]
    locks_by_scope: dict[str, list[float]] = None  # type: ignore[assignment]
    locks_by_attempt_id: dict[str, list[float]] = None  # type: ignore[assignment]
    active_wakes_by_task_id: frozenset[str] = frozenset()
    active_wakes_by_run_id: frozenset[str] = frozenset()


@dataclass(frozen=True)
class _SchedulerSnapshot:
    error: str = ""
    runs_by_run_id: dict[str, list[tuple[str, object]]] = None  # type: ignore[assignment]
    runs_by_task_id: dict[str, list[tuple[str, object]]] = None  # type: ignore[assignment]


@dataclass(frozen=True)
class _WorkflowSnapshot:
    error: str = ""
    unsettled_run_ids: frozenset[str] = frozenset()
    unsettled_task_ids: frozenset[str] = frozenset()
    settled_run_ids: frozenset[str] = frozenset()
    settled_task_ids: frozenset[str] = frozenset()
    runs_dir: Path | None = None
    settled_ended_at_by_run_id: dict[str, list[object]] = field(default_factory=dict)
    settled_ended_at_by_task_id: dict[str, list[object]] = field(default_factory=dict)
    indexed_settled_skipped: frozenset[str] = frozenset()
    skipped_run_cache: dict[str, tuple[str, object]] = field(default_factory=dict)


@dataclass(frozen=True)
class _ConversationSnapshot:
    stores: tuple[tuple[Path, ConversationStore], ...] = ()
    discovery_error: str = ""
    wake_error: str = ""
    active_wake_task_ids: frozenset[str] = frozenset()
    active_wake_run_ids: frozenset[str] = frozenset()


def _valid_settled_timestamp(raw: object, *, effective_now: float) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    if not math.isfinite(value) or value <= 0.0:
        return None
    if value > effective_now + 1.0e-6:
        return None
    return min(value, effective_now)


def _optional_lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None


def _empty_runtime_snapshot(error: str = "") -> _RuntimeDbSnapshot:
    return _RuntimeDbSnapshot(
        error=error,
        task_runs_by_id={},
        task_runs_by_task_id={},
        agent_runs_by_id={},
        agent_runs_by_run_id={},
        agent_runs_by_task_run_id={},
        children_by_parent_id={},
        attempts_by_agent_run_id={},
        locks_by_scope={},
        locks_by_attempt_id={},
    )


def _load_runtime_db_snapshot(owner_home: Path) -> _RuntimeDbSnapshot:
    db_path = runtime_db_path(owner_home)
    try:
        if _optional_lstat(db_path) is None:
            return _empty_runtime_snapshot()
        require_plain_sqlite_files(db_path, beneath=owner_home)
        with closing(sqlite3.connect(sqlite_readonly_probe_uri(db_path), uri=True, timeout=1.0)) as conn:
            conn.row_factory = sqlite3.Row
            tables = {
                str(row[0])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            task_runs_by_id: dict[str, dict[str, Any]] = {}
            task_runs_by_task_id: dict[str, list[dict[str, Any]]] = {}
            if "task_runs" in tables:
                for row in conn.execute("SELECT task_run_id, task_id, status, closed_at FROM task_runs"):
                    item = {
                        "task_run_id": str(row["task_run_id"] or ""),
                        "task_id": str(row["task_id"] or ""),
                        "status": str(row["status"] or "").strip().lower(),
                        "closed_at": row["closed_at"],
                    }
                    if item["task_run_id"]:
                        task_runs_by_id[item["task_run_id"]] = item
                    if item["task_id"]:
                        task_runs_by_task_id.setdefault(item["task_id"], []).append(item)

            agent_runs_by_id: dict[str, dict[str, Any]] = {}
            agent_runs_by_run_id: dict[str, list[dict[str, Any]]] = {}
            agent_runs_by_task_run_id: dict[str, list[dict[str, Any]]] = {}
            children_by_parent_id: dict[str, list[str]] = {}
            if "agent_runs" in tables:
                for row in conn.execute(
                    "SELECT agent_run_id, task_run_id, parent_agent_run_id, run_id, current_attempt_id, status, updated_at FROM agent_runs"
                ):
                    item = {
                        "agent_run_id": str(row["agent_run_id"] or ""),
                        "task_run_id": str(row["task_run_id"] or ""),
                        "parent_agent_run_id": str(row["parent_agent_run_id"] or ""),
                        "run_id": str(row["run_id"] or ""),
                        "current_attempt_id": str(row["current_attempt_id"] or ""),
                        "status": str(row["status"] or "").strip().lower(),
                        "updated_at": row["updated_at"],
                    }
                    if item["agent_run_id"]:
                        agent_runs_by_id[item["agent_run_id"]] = item
                    if item["run_id"]:
                        agent_runs_by_run_id.setdefault(item["run_id"], []).append(item)
                    if item["task_run_id"]:
                        agent_runs_by_task_run_id.setdefault(item["task_run_id"], []).append(item)
                    if item["parent_agent_run_id"] and item["agent_run_id"]:
                        children_by_parent_id.setdefault(item["parent_agent_run_id"], []).append(item["agent_run_id"])

            attempts_by_agent_run_id: dict[str, list[dict[str, Any]]] = {}
            if "agent_attempts" in tables:
                for row in conn.execute("SELECT attempt_id, agent_run_id, status, ended_at FROM agent_attempts"):
                    item = {
                        "attempt_id": str(row["attempt_id"] or ""),
                        "agent_run_id": str(row["agent_run_id"] or ""),
                        "status": str(row["status"] or "").strip().lower(),
                        "ended_at": row["ended_at"],
                    }
                    if item["agent_run_id"]:
                        attempts_by_agent_run_id.setdefault(item["agent_run_id"], []).append(item)

            locks_by_scope: dict[str, list[float]] = {}
            locks_by_attempt_id: dict[str, list[float]] = {}
            if "resource_locks" in tables:
                for row in conn.execute("SELECT canonical_scope, attempt_id, lease_expires_at FROM resource_locks"):
                    scope_key = str(row["canonical_scope"] or "")
                    attempt_key = str(row["attempt_id"] or "")
                    lease = float(row["lease_expires_at"] or 0.0)
                    if scope_key:
                        locks_by_scope.setdefault(scope_key, []).append(lease)
                    if attempt_key:
                        locks_by_attempt_id.setdefault(attempt_key, []).append(lease)

            wake_tasks: set[str] = set()
            wake_runs: set[str] = set()
            if "wake_queue" in tables:
                for row in conn.execute("SELECT root_task_id, root_run_id, status FROM wake_queue"):
                    status = str(row["status"] or "").strip().lower()
                    if status in _WAKE_ACTIVE_STATUSES:
                        t_id = str(row["root_task_id"] or "")
                        r_id = str(row["root_run_id"] or "")
                        if t_id:
                            wake_tasks.add(t_id)
                        if r_id:
                            wake_runs.add(r_id)

        return _RuntimeDbSnapshot(
            task_runs_by_id=task_runs_by_id,
            task_runs_by_task_id=task_runs_by_task_id,
            agent_runs_by_id=agent_runs_by_id,
            agent_runs_by_run_id=agent_runs_by_run_id,
            agent_runs_by_task_run_id=agent_runs_by_task_run_id,
            children_by_parent_id=children_by_parent_id,
            attempts_by_agent_run_id=attempts_by_agent_run_id,
            locks_by_scope=locks_by_scope,
            locks_by_attempt_id=locks_by_attempt_id,
            active_wakes_by_task_id=frozenset(wake_tasks),
            active_wakes_by_run_id=frozenset(wake_runs),
        )
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        return _empty_runtime_snapshot(error=f"runtime_db_{type(exc).__name__}")


def _load_scheduler_snapshot(owner_home: Path) -> _SchedulerSnapshot:
    sched_path = owner_home / "data" / "scheduler" / "store.json"
    try:
        raw = read_atomic_store_bytes(sched_path)
    except OSError as exc:
        return _SchedulerSnapshot(
            error=f"scheduler_store_{type(exc).__name__}",
            runs_by_run_id={},
            runs_by_task_id={},
        )
    if raw is None:
        return _SchedulerSnapshot(runs_by_run_id={}, runs_by_task_id={})
    try:
        sched_doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return _SchedulerSnapshot(error="scheduler_store_invalid_json", runs_by_run_id={}, runs_by_task_id={})
    if not isinstance(sched_doc, dict):
        return _SchedulerSnapshot(error="scheduler_store_invalid_shape", runs_by_run_id={}, runs_by_task_id={})
    raw_runs = sched_doc.get("runs")
    if raw_runs is None:
        return _SchedulerSnapshot(runs_by_run_id={}, runs_by_task_id={})
    items: Sequence[tuple[str, object]]
    if isinstance(raw_runs, dict):
        items = [(str(key), value) for key, value in raw_runs.items()]
    elif isinstance(raw_runs, list):
        items = [("", value) for value in raw_runs]
    else:
        return _SchedulerSnapshot(error="scheduler_runs_invalid_shape", runs_by_run_id={}, runs_by_task_id={})
    runs_by_run_id: dict[str, list[tuple[str, object]]] = {}
    runs_by_task_id: dict[str, list[tuple[str, object]]] = {}
    for fallback_key, entry in items:
        if not isinstance(entry, dict):
            return _SchedulerSnapshot(error="scheduler_run_entry_invalid", runs_by_run_id={}, runs_by_task_id={})
        status = str(entry.get("status") or "").strip().lower()
        ended_at = entry.get("ended_at")
        r_id = str(entry.get("run_id") or fallback_key or "").strip()
        if r_id:
            runs_by_run_id.setdefault(r_id, []).append((status, ended_at))
        seen_task_ids: set[str] = set()
        for key in ("root_task_id", "task_id"):
            t_id = str(entry.get(key) or "").strip()
            if t_id and t_id not in seen_task_ids:
                seen_task_ids.add(t_id)
                runs_by_task_id.setdefault(t_id, []).append((status, ended_at))
    return _SchedulerSnapshot(runs_by_run_id=runs_by_run_id, runs_by_task_id=runs_by_task_id)


def _extract_workflow_refs(workflow_doc: object, run_ids: set[str], task_ids: set[str]) -> None:
    if not isinstance(workflow_doc, dict):
        return
    for key in ("workflow_run_id", "run_id", "agent_run_id"):
        val = str(workflow_doc.get(key) or "").strip()
        if val:
            run_ids.add(val)
    for key in ("task_id", "root_task_id"):
        val = str(workflow_doc.get(key) or "").strip()
        if val:
            task_ids.add(val)
    for nested_key in ("business_context", "escalation_result", "result"):
        nested = workflow_doc.get(nested_key)
        if isinstance(nested, dict):
            _extract_workflow_refs(nested, run_ids, task_ids)


def _read_workflow_dir_refs(run_dir: Path, run_ids: set[str], task_ids: set[str]) -> tuple[str, object]:
    run_file = run_dir / "run.json"
    try:
        run_st = _optional_lstat(run_file)
        if run_st is None:
            return "", None
        if stat.S_ISLNK(run_st.st_mode):
            return "workflow_run_symlink", None
        run_doc = json.loads(run_file.read_text(encoding="utf-8"))
        if not isinstance(run_doc, dict):
            return "workflow_run_invalid", None
        status = str(run_doc.get("status") or "").strip().lower()
        ended_at = run_doc.get("ended_at")
        _extract_workflow_refs(run_doc, run_ids, task_ids)
        steps_dir = run_dir / "steps"
        steps_st = _optional_lstat(steps_dir)
        if steps_st is not None:
            if stat.S_ISLNK(steps_st.st_mode):
                return "workflow_steps_dir_symlink", None
            if stat.S_ISDIR(steps_st.st_mode):
                with os.scandir(steps_dir) as entries:
                    step_names = sorted(
                        entry.name for entry in entries if entry.name.endswith(".json")
                    )
                for step_name in step_names:
                    step_file = steps_dir / step_name
                    step_st = os.lstat(step_file)
                    if stat.S_ISLNK(step_st.st_mode):
                        return "workflow_step_symlink", None
                    step_doc = json.loads(step_file.read_text(encoding="utf-8"))
                    if not isinstance(step_doc, dict):
                        return "workflow_step_invalid", None
                    _extract_workflow_refs(step_doc, run_ids, task_ids)
        return f"status:{status}", ended_at
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return f"workflow_dir_{type(exc).__name__}", None


def _read_skipped_workflow_run(workflow: _WorkflowSnapshot, run_id: str) -> tuple[str, object]:
    if not run_id or workflow.runs_dir is None:
        return "", None
    cached = workflow.skipped_run_cache.get(run_id)
    if cached is not None:
        return cached
    if "/" in run_id or "\\" in run_id or run_id in {".", ".."} or ".." in Path(run_id).parts:
        result: tuple[str, object] = ("", None)
        workflow.skipped_run_cache[run_id] = result
        return result
    run_dir = workflow.runs_dir / run_id
    try:
        dir_st = _optional_lstat(run_dir)
        if dir_st is None:
            result = ("", None)
        elif stat.S_ISLNK(dir_st.st_mode):
            result = ("workflow_run_dir_symlink", None)
        elif not stat.S_ISDIR(dir_st.st_mode):
            result = ("", None)
        else:
            result = _read_workflow_dir_refs(run_dir, {run_id}, set())
    except OSError as exc:
        result = (f"workflow_dir_{type(exc).__name__}", None)
    workflow.skipped_run_cache[run_id] = result
    return result


def _load_workflow_snapshot(owner_home: Path, *, max_settled_dir_scan: int = 256) -> _WorkflowSnapshot:
    index_db = owner_home / "data" / "workflow_run_index.sqlite3"
    runs_dir = owner_home / "data" / "workflow_runs"
    unsettled_run_ids: set[str] = set()
    unsettled_task_ids: set[str] = set()
    settled_run_ids: set[str] = set()
    settled_task_ids: set[str] = set()
    indexed_settled: set[str] = set()
    settled_ended_at_by_run_id: dict[str, list[object]] = {}
    settled_ended_at_by_task_id: dict[str, list[object]] = {}
    scanned_dir_names: set[str] = set()

    try:
        index_st = _optional_lstat(index_db)
    except OSError as exc:
        return _WorkflowSnapshot(error=f"workflow_index_{type(exc).__name__}")
    if index_st is not None:
        try:
            require_plain_sqlite_files(index_db, beneath=owner_home)
            with closing(sqlite3.connect(sqlite_readonly_probe_uri(index_db), uri=True, timeout=1.0)) as conn:
                tables = {
                    str(row[0])
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                }
                if not {"runs", "pending", "legacy"}.issubset(tables):
                    return _WorkflowSnapshot(error="workflow_index_incomplete")
                for wf_run_id, raw_status in conn.execute("SELECT run_id, status FROM runs"):
                    run_id = str(wf_run_id or "").strip()
                    status = str(raw_status or "").strip().lower()
                    if not run_id:
                        continue
                    if status in _WORKFLOW_SETTLED_STATES:
                        settled_run_ids.add(run_id)
                        indexed_settled.add(run_id)
                    else:
                        unsettled_run_ids.add(run_id)
                for (wf_run_id,) in conn.execute("SELECT run_id FROM pending"):
                    if wf_run_id:
                        unsettled_run_ids.add(str(wf_run_id).strip())
                for (wf_run_id,) in conn.execute("SELECT run_id FROM legacy"):
                    if wf_run_id:
                        unsettled_run_ids.add(str(wf_run_id).strip())
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            return _WorkflowSnapshot(error=f"workflow_index_{type(exc).__name__}")

    try:
        runs_dir_st = _optional_lstat(runs_dir)
    except OSError as exc:
        return _WorkflowSnapshot(error=f"workflow_runs_stat_{type(exc).__name__}")
    if runs_dir_st is not None:
        if stat.S_ISLNK(runs_dir_st.st_mode):
            return _WorkflowSnapshot(error="workflow_runs_dir_symlink")
        if stat.S_ISDIR(runs_dir_st.st_mode):
            try:
                dir_names: list[str] = []
                with os.scandir(runs_dir) as entries:
                    for entry in entries:
                        entry_st = os.lstat(runs_dir / entry.name)
                        if stat.S_ISLNK(entry_st.st_mode):
                            return _WorkflowSnapshot(error="workflow_run_dir_symlink")
                        if stat.S_ISDIR(entry_st.st_mode):
                            dir_names.append(entry.name)
                dir_names.sort()
            except OSError as exc:
                return _WorkflowSnapshot(error=f"workflow_runs_scan_{type(exc).__name__}")
            settled_scanned = 0
            for dir_name in dir_names:
                run_dir = runs_dir / dir_name
                is_known_settled = dir_name in indexed_settled and dir_name not in unsettled_run_ids
                if is_known_settled and settled_scanned >= max_settled_dir_scan:
                    continue
                scanned_dir_names.add(dir_name)
                r_ids: set[str] = {dir_name}
                t_ids: set[str] = set()
                outcome, wf_ended_at = _read_workflow_dir_refs(run_dir, r_ids, t_ids)
                if outcome and not outcome.startswith("status:"):
                    return _WorkflowSnapshot(error=outcome)
                status = outcome.removeprefix("status:") if outcome else ""
                if dir_name in unsettled_run_ids or (outcome and status not in _WORKFLOW_SETTLED_STATES):
                    unsettled_run_ids.update(r_ids)
                    unsettled_task_ids.update(t_ids)
                    settled_run_ids.difference_update(r_ids)
                elif status in _WORKFLOW_SETTLED_STATES:
                    settled_run_ids.update(r_ids)
                    settled_task_ids.update(t_ids)
                    for rid in r_ids:
                        settled_ended_at_by_run_id.setdefault(rid, []).append(wf_ended_at)
                    for tid in t_ids:
                        settled_ended_at_by_task_id.setdefault(tid, []).append(wf_ended_at)
                    settled_scanned += 1
            settled_run_ids.difference_update(unsettled_run_ids)
            settled_task_ids.difference_update(unsettled_task_ids)
            for rid in unsettled_run_ids:
                settled_ended_at_by_run_id.pop(rid, None)
            for tid in unsettled_task_ids:
                settled_ended_at_by_task_id.pop(tid, None)

    return _WorkflowSnapshot(
        unsettled_run_ids=frozenset(unsettled_run_ids),
        unsettled_task_ids=frozenset(unsettled_task_ids),
        settled_run_ids=frozenset(settled_run_ids),
        settled_task_ids=frozenset(settled_task_ids),
        runs_dir=runs_dir,
        settled_ended_at_by_run_id=settled_ended_at_by_run_id,
        settled_ended_at_by_task_id=settled_ended_at_by_task_id,
        indexed_settled_skipped=frozenset((indexed_settled - unsettled_run_ids - scanned_dir_names) & settled_run_ids),
    )


def _discover_conversation_snapshot(
    owner_home: Path,
    extra_roots: Sequence[str | Path] | None,
    record_roots: Sequence[Path],
) -> _ConversationSnapshot:
    candidates: list[Path] = [
        owner_home / "conversations",
        owner_home / "data" / "conversations",
    ]
    discovery_error = ""
    try:
        ws_parent = owner_home / "workspace" / "runtime" / "workspaces"
        ws_parent_st = _optional_lstat(ws_parent)
        if ws_parent_st is not None:
            if stat.S_ISLNK(ws_parent_st.st_mode):
                ws_parent_st = os.stat(ws_parent)
            if stat.S_ISDIR(ws_parent_st.st_mode):
                with os.scandir(ws_parent) as ws_entries:
                    for ws_name in sorted(entry.name for entry in ws_entries):
                        candidates.append(ws_parent / ws_name / "conversations")
    except OSError as exc:
        discovery_error = f"conversation_discovery_{type(exc).__name__}"
    for extra in extra_roots or ():
        candidates.append(Path(extra).expanduser().resolve(strict=False))
    candidates.extend(record_roots)

    seen: set[Path] = set()
    stores: list[tuple[Path, ConversationStore]] = []
    wake_tasks: set[str] = set()
    wake_runs: set[str] = set()
    wake_error = ""
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        if resolved in seen:
            continue
        try:
            cand_st = _optional_lstat(resolved)
            if cand_st is not None and stat.S_ISLNK(cand_st.st_mode):
                cand_st = os.stat(resolved)
        except OSError as exc:
            discovery_error = discovery_error or f"conversation_discovery_{type(exc).__name__}"
            continue
        if cand_st is None or not stat.S_ISDIR(cand_st.st_mode):
            continue
        seen.add(resolved)
        stores.append((resolved, ConversationStore(resolved, initialize=False)))
        for kind in ("urgent", "normal"):
            wake_dir = resolved / "wake_queue" / kind
            try:
                wake_st = _optional_lstat(wake_dir)
                if wake_st is not None and stat.S_ISLNK(wake_st.st_mode):
                    wake_error = wake_error or "conversation_wake_symlink"
                    continue
                if wake_st is None or not stat.S_ISDIR(wake_st.st_mode):
                    continue
                with os.scandir(wake_dir) as entries:
                    wake_names = sorted(
                        entry.name for entry in entries if entry.name.endswith(".json")
                    )
                for wake_name in wake_names:
                    wake_file = wake_dir / wake_name
                    wake_file_st = os.lstat(wake_file)
                    if stat.S_ISLNK(wake_file_st.st_mode):
                        wake_error = wake_error or "conversation_wake_symlink"
                        continue
                    wake_doc = json.loads(wake_file.read_text(encoding="utf-8"))
                    if not isinstance(wake_doc, dict):
                        wake_error = wake_error or "conversation_wake_invalid"
                        continue
                    for t_key in ("root_task_id", "task_id"):
                        val = str(wake_doc.get(t_key) or "").strip()
                        if val:
                            wake_tasks.add(val)
                    for r_key in ("root_run_id", "run_id"):
                        val = str(wake_doc.get(r_key) or "").strip()
                        if val:
                            wake_runs.add(val)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
                wake_error = wake_error or f"conversation_wake_{type(exc).__name__}"

    return _ConversationSnapshot(
        stores=tuple(stores),
        discovery_error=discovery_error,
        wake_error=wake_error,
        active_wake_task_ids=frozenset(wake_tasks),
        active_wake_run_ids=frozenset(wake_runs),
    )


def _runtime_tree_verdict(
    runtime: _RuntimeDbSnapshot,
    *,
    root_task_id: str,
    run_id: str,
    effective_now: float,
) -> tuple[list[str], list[str], bool, float | None, bool]:
    active_reasons: list[str] = []
    unknown_reasons: list[str] = []
    matched_task_runs: dict[str, dict[str, Any]] = {}
    matched_agent_runs: dict[str, dict[str, Any]] = {}

    for row in runtime.task_runs_by_task_id.get(root_task_id, ()):
        matched_task_runs[row["task_run_id"]] = row
    if root_task_id in runtime.task_runs_by_id:
        row = runtime.task_runs_by_id[root_task_id]
        matched_task_runs[row["task_run_id"]] = row
    for row in runtime.agent_runs_by_run_id.get(run_id, ()):
        matched_agent_runs[row["agent_run_id"]] = row
    if run_id in runtime.agent_runs_by_id:
        row = runtime.agent_runs_by_id[run_id]
        matched_agent_runs[row["agent_run_id"]] = row
    for tr_id in list(matched_task_runs):
        for row in runtime.agent_runs_by_task_run_id.get(tr_id, ()):
            matched_agent_runs[row["agent_run_id"]] = row

    # 沿 parent_agent_run_id 向下展开全量子代理树并回溯上级 task_run/parent。
    queue: deque[str] = deque(matched_agent_runs)
    while queue:
        ar_id = queue.popleft()
        row = runtime.agent_runs_by_id.get(ar_id)
        if row:
            tr_id = row["task_run_id"]
            if tr_id and tr_id not in matched_task_runs and tr_id in runtime.task_runs_by_id:
                matched_task_runs[tr_id] = runtime.task_runs_by_id[tr_id]
                for sibling in runtime.agent_runs_by_task_run_id.get(tr_id, ()):
                    if sibling["agent_run_id"] not in matched_agent_runs:
                        matched_agent_runs[sibling["agent_run_id"]] = sibling
                        queue.append(sibling["agent_run_id"])
            parent_id = row["parent_agent_run_id"]
            if parent_id and parent_id not in matched_agent_runs and parent_id in runtime.agent_runs_by_id:
                matched_agent_runs[parent_id] = runtime.agent_runs_by_id[parent_id]
                queue.append(parent_id)
        for child_id in runtime.children_by_parent_id.get(ar_id, ()):
            if child_id not in matched_agent_runs and child_id in runtime.agent_runs_by_id:
                matched_agent_runs[child_id] = runtime.agent_runs_by_id[child_id]
                queue.append(child_id)

    for tr in matched_task_runs.values():
        if tr["status"] not in AGENT_RUN_TERMINAL_STATUSES:
            active_reasons.append(f"runtime_task_run_{tr['status'] or 'non_terminal'}")

    for ar_id, ar in matched_agent_runs.items():
        if ar["status"] not in AGENT_RUN_TERMINAL_STATUSES:
            active_reasons.append(f"runtime_agent_run_{ar['status'] or 'non_terminal'}")
        for lease in runtime.locks_by_scope.get(exec_lock_scope(ar_id), ()):
            if lease > effective_now:
                active_reasons.append("runtime_resource_lock_active")
        for attempt in runtime.attempts_by_agent_run_id.get(ar_id, ()):
            att_status = attempt["status"]
            if att_status == ATTEMPT_STATUS_UNKNOWN:
                unknown_reasons.append("runtime_attempt_status_unknown")
            elif att_status not in _ATTEMPT_TERMINAL_STATUSES:
                active_reasons.append(f"runtime_attempt_{att_status or 'active'}")
            for lease in runtime.locks_by_attempt_id.get(attempt["attempt_id"], ()):
                if lease > effective_now:
                    active_reasons.append("runtime_attempt_lock_active")

    if root_task_id in runtime.active_wakes_by_task_id or run_id in runtime.active_wakes_by_run_id:
        active_reasons.append("runtime_wake_pending")

    settled_candidates: list[float] = []
    ts_complete = True
    matched_attempt_count = 0
    for tr in matched_task_runs.values():
        ts = _valid_settled_timestamp(tr.get("closed_at"), effective_now=effective_now)
        if ts is None:
            ts_complete = False
        else:
            settled_candidates.append(ts)

    for ar_id in matched_agent_runs:
        for attempt in runtime.attempts_by_agent_run_id.get(ar_id, ()):
            matched_attempt_count += 1
            ts = _valid_settled_timestamp(attempt.get("ended_at"), effective_now=effective_now)
            if ts is None:
                ts_complete = False
            else:
                settled_candidates.append(ts)

    if not matched_task_runs and matched_attempt_count == 0:
        for ar in matched_agent_runs.values():
            ts = _valid_settled_timestamp(ar.get("updated_at"), effective_now=effective_now)
            if ts is None:
                ts_complete = False
            else:
                settled_candidates.append(ts)

    affirmed = bool(matched_task_runs or matched_agent_runs)
    runtime_settled_at = max(settled_candidates) if (affirmed and ts_complete and settled_candidates) else None
    return active_reasons, unknown_reasons, affirmed, runtime_settled_at, ts_complete


def _classify_execution_record(
    record: dict[str, object],
    *,
    owner_home_path: Path,
    owner_home_str: str,
    runtime: _RuntimeDbSnapshot,
    scheduler: _SchedulerSnapshot,
    workflow: _WorkflowSnapshot,
    conversations: _ConversationSnapshot,
    min_settled_age_seconds: int,
    effective_now: float,
) -> dict[str, Any]:
    facts = background_process_facts(record)
    if record.get("schema") not in _EXECUTION_PROCESS_SESSION_SCHEMAS or record.get("activation_scope") is not None:
        return {**facts, "classification": "unknown", "reason": "activation_or_non_execution_schema"}

    scope_raw = record.get("execution_scope")
    if not isinstance(scope_raw, dict):
        return {**facts, "classification": "unknown", "reason": "scope_incomplete"}

    rec_owner = str(scope_raw.get("owner_home") or "").strip()
    if not rec_owner:
        return {**facts, "classification": "unknown", "reason": "owner_scope_missing"}
    if str(Path(rec_owner).expanduser().resolve(strict=False)) != owner_home_str:
        return {**facts, "classification": "unknown", "reason": "owner_scope_mismatch"}

    thread_id = facts["thread_id"]
    root_task_id = facts["root_task_id"]
    run_id = facts["run_id"]
    if not (thread_id and root_task_id and run_id):
        return {**facts, "classification": "unknown", "reason": "scope_incomplete"}

    completion_target = record.get("completion_target")
    if isinstance(completion_target, dict) and completion_target.get("store_root"):
        ct_root = Path(str(completion_target["store_root"])).expanduser().resolve(strict=False)
        if not ct_root.is_relative_to(owner_home_path):
            return {**facts, "classification": "unknown", "reason": "completion_target_outside_owner"}

    active_reasons: list[str] = []
    unknown_reasons: list[str] = []
    affirmed_sources: list[str] = []

    if root_task_id in conversations.active_wake_task_ids or run_id in conversations.active_wake_run_ids:
        active_reasons.append("conversation_wake_pending")

    for _store_path, store in conversations.stores:
        try:
            thread_obj, thread_err = store.threads.load_report(thread_id)
            if thread_err is not None and thread_err.get("error_type") != "FileNotFoundError":
                unknown_reasons.append("conversation_thread_unreadable")
            if thread_obj is not None:
                exec_state = conversation_thread_execution_state(store, thread_id)
                if not exec_state.get("state_available") or exec_state.get("load_errors"):
                    unknown_reasons.append("thread_execution_state_unreadable")
                elif root_task_id in (exec_state.get("running_task_ids") or ()):
                    active_reasons.append("thread_execution_state_active")

                active_links, link_errors = store.tasks.active_report(thread_id)
                if link_errors:
                    unknown_reasons.append("conversation_active_links_unreadable")
                elif any(getattr(link, "task_id", "") == root_task_id for link in active_links):
                    active_reasons.append("conversation_task_active")

            link, link_err = store.tasks.load_report(root_task_id)
            if link_err is not None:
                unknown_reasons.append("conversation_task_link_unreadable")
            elif link is not None:
                link_thread = str(getattr(link, "thread_id", "") or "")
                if link_thread and link_thread != thread_id:
                    unknown_reasons.append("task_link_thread_mismatch")
                else:
                    link_status = str(getattr(link, "status", "") or "").strip().lower()
                    if link_status in THREAD_TASK_LINK_INACTIVE_STATUSES:
                        affirmed_sources.append("conversation_task_link")
                    else:
                        active_reasons.append(f"conversation_task_link_{link_status or 'active'}")
        except Exception as exc:  # noqa: BLE001 会话存储读取异常保守判未知
            unknown_reasons.append(f"conversation_store_{type(exc).__name__}")

    rt_active, rt_unknown, rt_affirmed, runtime_settled_at, runtime_ts_complete = _runtime_tree_verdict(
        runtime, root_task_id=root_task_id, run_id=run_id, effective_now=effective_now,
    )
    active_reasons.extend(rt_active)
    unknown_reasons.extend(rt_unknown)
    if rt_affirmed and not rt_active and not rt_unknown:
        affirmed_sources.append("runtime_db")

    sched_entries = [
        *scheduler.runs_by_run_id.get(run_id, ()),
        *scheduler.runs_by_task_id.get(root_task_id, ()),
    ]
    if sched_entries:
        sched_active = [s for s, _ in sched_entries if s in _SCHEDULER_ACTIVE_STATUSES]
        sched_unknown = [s for s, _ in sched_entries if s not in _SCHEDULER_ACTIVE_STATUSES and s not in _SCHEDULER_SETTLED_STATUSES]
        if sched_active:
            active_reasons.append(f"scheduler_run_{sched_active[0]}")
        elif sched_unknown:
            unknown_reasons.append(f"scheduler_run_{sched_unknown[0] or 'unknown'}")
        else:
            affirmed_sources.append("scheduler")

    wf_raw_ended_ats: list[object] = []
    wf_missing_ts = False
    if run_id in workflow.unsettled_run_ids or root_task_id in workflow.unsettled_task_ids:
        active_reasons.append("workflow_run_unsettled")
    elif run_id in workflow.settled_run_ids or root_task_id in workflow.settled_task_ids:
        wf_affirmed = True
        if run_id in workflow.indexed_settled_skipped:
            skip_outcome, skip_ended_at = _read_skipped_workflow_run(workflow, run_id)
            if skip_outcome and not skip_outcome.startswith("status:"):
                unknown_reasons.append(skip_outcome)
                wf_affirmed = False
            elif skip_outcome and skip_outcome.removeprefix("status:") not in _WORKFLOW_SETTLED_STATES:
                active_reasons.append("workflow_run_unsettled")
                wf_affirmed = False
            elif skip_outcome:
                wf_raw_ended_ats.append(skip_ended_at)
            else:
                wf_missing_ts = True
        wf_raw_ended_ats.extend(workflow.settled_ended_at_by_run_id.get(run_id, ()))
        wf_raw_ended_ats.extend(workflow.settled_ended_at_by_task_id.get(root_task_id, ()))
        if wf_affirmed:
            affirmed_sources.append("workflow")

    if active_reasons:
        return {**facts, "classification": "active", "reason": active_reasons[0]}

    for owner_err in (
        runtime.error,
        scheduler.error,
        workflow.error,
        conversations.discovery_error,
        conversations.wake_error,
    ):
        if owner_err:
            unknown_reasons.append(owner_err)
    if unknown_reasons:
        return {**facts, "classification": "unknown", "reason": unknown_reasons[0]}
    if not affirmed_sources:
        return {**facts, "classification": "unknown", "reason": "root_state_unknown"}

    dedup_sources = list(dict.fromkeys(affirmed_sources))
    settled_timestamps: list[float] = []
    authority_ts_complete = True
    if "runtime_db" in dedup_sources:
        if not runtime_ts_complete or runtime_settled_at is None:
            authority_ts_complete = False
        else:
            settled_timestamps.append(runtime_settled_at)
    if "scheduler" in dedup_sources:
        for _, raw_ts in sched_entries:
            ts = _valid_settled_timestamp(raw_ts, effective_now=effective_now)
            if ts is None:
                authority_ts_complete = False
            else:
                settled_timestamps.append(ts)
    if "workflow" in dedup_sources:
        if wf_missing_ts or not wf_raw_ended_ats:
            authority_ts_complete = False
        for raw_ts in wf_raw_ended_ats:
            ts = _valid_settled_timestamp(raw_ts, effective_now=effective_now)
            if ts is None:
                authority_ts_complete = False
            else:
                settled_timestamps.append(ts)

    settled_at: float | None = None
    settled_age_seconds: float | None = None
    settled_age_known = False
    if authority_ts_complete and settled_timestamps:
        settled_at = max(settled_timestamps)
        started_ts = _valid_settled_timestamp(record.get("started_at"), effective_now=effective_now)
        if started_ts is not None and started_ts > settled_at:
            settled_at = started_ts
        settled_age_seconds = max(0.0, effective_now - settled_at)
        settled_age_known = True

    if min_settled_age_seconds > 0:
        if not settled_age_known or settled_age_seconds is None:
            return {
                **facts,
                "classification": "settled",
                "reason": "settled_at_unknown",
                "affirmed_sources": dedup_sources,
                "settled_age_seconds": None,
                "settled_age_known": False,
                "eligible_to_stop": False,
                "stopped": False,
            }
        if settled_age_seconds < min_settled_age_seconds:
            return {
                **facts,
                "classification": "settled",
                "reason": "younger_than_min_settled_age",
                "affirmed_sources": dedup_sources,
                "settled_at": settled_at,
                "settled_age_seconds": settled_age_seconds,
                "settled_age_known": True,
                "age_seconds": settled_age_seconds,
                "eligible_to_stop": False,
                "stopped": False,
            }

    settled_item: dict[str, Any] = {
        **facts,
        "classification": "settled",
        "reason": "settled_terminal",
        "affirmed_sources": dedup_sources,
        "settled_age_seconds": settled_age_seconds,
        "settled_age_known": settled_age_known,
        "eligible_to_stop": True,
    }
    if settled_at is not None:
        settled_item["settled_at"] = settled_at
        settled_item["age_seconds"] = settled_age_seconds
    return settled_item


def _stop_settled_batch(
    *,
    store: ProcessSessionStore,
    resolved_store_root: Path,
    default_store_root: Path,
    owner_home_str: str,
    store_has_errors: bool,
    all_scope_keys_by_scope: dict[tuple[str, str, str], set[str]],
    eligible_entries: list[tuple[dict[str, object], dict[str, Any]]],
) -> list[dict[str, Any]]:
    by_scope: dict[tuple[str, str, str], list[tuple[dict[str, object], dict[str, Any]]]] = {}
    for record, item in eligible_entries:
        scope_key = (item["thread_id"], item["root_task_id"], item["run_id"])
        by_scope.setdefault(scope_key, []).append((record, item))

    stopped_rows: list[dict[str, Any]] = []
    for scope_key, group in by_scope.items():
        group_sids = {str(item["session_id"]) for _, item in group}
        attempt_ids = {str(item.get("attempt_id") or "").strip() for _, item in group}
        unique_attempt_id = next(iter(attempt_ids)) if len(attempt_ids) == 1 else ""
        # 只有整批 settled 记录共享同一个非空 attempt_id 时才走整域冻结，避免误波及并发重建的新 attempt。
        whole_scope_eligible = (
            bool(unique_attempt_id)
            and not store_has_errors
            and resolved_store_root == default_store_root
            and all_scope_keys_by_scope.get(scope_key, set()).issubset(group_sids)
        )
        handled_via_freeze = False
        if whole_scope_eligible:
            thread_id, root_task_id, run_id = scope_key
            try:
                batch = freeze_process_stop(ProcessExecutionScope(
                    owner_home=owner_home_str,
                    thread_id=thread_id,
                    root_task_id=root_task_id,
                    run_id=run_id,
                    attempt_id=unique_attempt_id,
                ))
                if not batch.background_freeze_error and batch.receipt.records:
                    cleanup = cleanup_process_stop(batch)
                    sessions_by_id = {
                        str(row.get("session_id") or ""): row
                        for row in cleanup.get("sessions", ())
                        if isinstance(row, dict)
                    }
                    # 只有冻结回执覆盖全部目标会话时才认整批完成；否则退回逐条精确收口。
                    if group_sids.issubset(sessions_by_id):
                        for _, item in group:
                            s_row = sessions_by_id[str(item["session_id"])]
                            stopped = bool(s_row.get("confirmed"))
                            item.update({
                                "stopped": stopped,
                                "terminal_status": str(s_row.get("status") or ""),
                                "reason": "settled_and_stopped" if stopped else "stop_unconfirmed",
                            })
                            if s_row.get("error"):
                                item["stop_error"] = s_row["error"]
                            stopped_rows.append(item)
                        handled_via_freeze = True
            except (OSError, RuntimeError, TypeError, ValueError):
                handled_via_freeze = False

        if handled_via_freeze:
            continue

        for record, item in group:
            try:
                result = stop_process_session(store, record)
                stopped = bool(result.confirmed)
                item.update({
                    "stopped": stopped,
                    "terminal_status": str(result.record.get("status") or ""),
                    "reason": "settled_and_stopped" if stopped else "stop_unconfirmed",
                })
            except ProcessSessionCleanupError as exc:
                item.update({
                    "stopped": False,
                    "terminal_status": "unknown",
                    "reason": "stop_failed",
                    "stop_error": exc.report,
                })
            stopped_rows.append(item)

    return stopped_rows


# LLM: 按三态合同（active / unknown / settled）只回收"所属 root/thread/task/run/workflow/scheduler 全链路已证实终态
#   且年龄达到阈值"的托管执行进程；绝不按命令正文/进程名/端口猜测，绝不停止 unknown（坏记录、缺范围、状态未明或激活域进程）。
# 函数用途: 分类并按需冻结/停止已证实终态的遗留后台进程，返回含三态明细与计数的结构化报告。
def freeze_settled_leftover_processes(
    owner_home: str | Path,
    *,
    min_settled_age_seconds: int = 0,
    now_ts: float | None = None,
    store_root: str | Path | None = None,
    conversation_roots: Sequence[str | Path] | None = None,
) -> dict[str, Any]:
    effective_now = float(time.time() if now_ts is None else now_ts)
    min_age = max(0, int(min_settled_age_seconds or 0))
    owner_text = str(owner_home or "").strip()
    if not owner_text:
        return {
            "owner_home": "",
            "store_root": "",
            "min_settled_age_seconds": min_age,
            "now_ts": effective_now,
            "active_processes": [],
            "settled_processes": [],
            "stopped_processes": [],
            "deferred_settled_processes": [],
            "unknown_processes": [],
            "counts": {
                "active": 0, "settled": 0, "stopped": 0,
                "deferred_settled": 0, "stop_failed": 0, "unknown": 0,
            },
        }
    owner_home_path = Path(owner_text).expanduser().resolve(strict=False)
    owner_home_str = str(owner_home_path)
    default_store_root = Path(background_store_root(owner_home_path, owner_home_path))
    resolved_store_root = (
        Path(store_root).expanduser().resolve(strict=False)
        if store_root is not None
        else default_store_root
    )

    active_processes: list[dict[str, Any]] = []
    settled_processes: list[dict[str, Any]] = []
    deferred_settled: list[dict[str, Any]] = []
    unknown_processes: list[dict[str, Any]] = []

    try:
        store_root_st = _optional_lstat(resolved_store_root)
        if store_root_st is not None and stat.S_ISLNK(store_root_st.st_mode):
            store_root_st = os.stat(resolved_store_root)
    except OSError as exc:
        unknown_processes.append({
            "session_id": "",
            "classification": "unknown",
            "reason": f"store_root_{type(exc).__name__}",
            "error": {
                "error_type": type(exc).__name__,
                "detail": str(exc),
                "path": str(resolved_store_root),
            },
        })
        return {
            "owner_home": owner_home_str,
            "store_root": str(resolved_store_root),
            "min_settled_age_seconds": min_age,
            "now_ts": effective_now,
            "active_processes": [],
            "settled_processes": [],
            "stopped_processes": [],
            "deferred_settled_processes": [],
            "unknown_processes": unknown_processes,
            "counts": {
                "active": 0, "settled": 0, "stopped": 0,
                "deferred_settled": 0, "stop_failed": 0, "unknown": len(unknown_processes),
            },
        }
    if store_root_st is None:
        return {
            "owner_home": owner_home_str,
            "store_root": str(resolved_store_root),
            "min_settled_age_seconds": min_age,
            "now_ts": effective_now,
            "active_processes": [],
            "settled_processes": [],
            "stopped_processes": [],
            "deferred_settled_processes": [],
            "unknown_processes": [],
            "counts": {
                "active": 0, "settled": 0, "stopped": 0,
                "deferred_settled": 0, "stop_failed": 0, "unknown": 0,
            },
        }

    store = ProcessSessionStore(resolved_store_root)
    records, errors = store.list_records()
    for err in errors:
        err_dict = err if isinstance(err, dict) else {"detail": str(err)}
        err_path = str(err_dict.get("path") or "")
        sid = Path(err_path).stem if err_path else str(err_dict.get("session_id") or "")
        unknown_processes.append({
            "session_id": sid,
            "classification": "unknown",
            "reason": str(err_dict.get("error_type") or err_dict.get("context") or "record_load_error"),
            "error": err_dict,
        })

    non_terminal = [
        rec for rec in records
        if rec.get("status") not in PROCESS_TERMINAL_STATUSES
    ]
    if not non_terminal:
        return {
            "owner_home": owner_home_str,
            "store_root": str(resolved_store_root),
            "min_settled_age_seconds": min_age,
            "now_ts": effective_now,
            "active_processes": [],
            "settled_processes": [],
            "stopped_processes": [],
            "deferred_settled_processes": [],
            "unknown_processes": unknown_processes,
            "counts": {
                "active": 0, "settled": 0, "stopped": 0,
                "deferred_settled": 0, "stop_failed": 0, "unknown": len(unknown_processes),
            },
        }

    record_conv_roots: list[Path] = []
    for rec in non_terminal:
        ct = rec.get("completion_target")
        if isinstance(ct, dict) and ct.get("store_root"):
            cand = Path(str(ct["store_root"])).expanduser().resolve(strict=False)
            if cand.is_relative_to(owner_home_path):
                record_conv_roots.append(cand)

    runtime = _load_runtime_db_snapshot(owner_home_path)
    scheduler = _load_scheduler_snapshot(owner_home_path)
    workflow = _load_workflow_snapshot(owner_home_path)
    conversations = _discover_conversation_snapshot(owner_home_path, conversation_roots, record_conv_roots)

    eligible_entries: list[tuple[dict[str, object], dict[str, Any]]] = []
    all_scope_keys_by_scope: dict[tuple[str, str, str], set[str]] = {}
    for rec in non_terminal:
        scope_raw = rec.get("execution_scope")
        if isinstance(scope_raw, dict):
            key = (
                str(scope_raw.get("thread_id") or "").strip(),
                str(scope_raw.get("root_task_id") or "").strip(),
                str(scope_raw.get("run_id") or "").strip(),
            )
            all_scope_keys_by_scope.setdefault(key, set()).add(str(rec.get("session_id") or ""))

    for rec in non_terminal:
        item = _classify_execution_record(
            rec,
            owner_home_path=owner_home_path,
            owner_home_str=owner_home_str,
            runtime=runtime,
            scheduler=scheduler,
            workflow=workflow,
            conversations=conversations,
            min_settled_age_seconds=min_age,
            effective_now=effective_now,
        )
        verdict = item["classification"]
        if verdict == "active":
            active_processes.append(item)
        elif verdict == "unknown":
            unknown_processes.append(item)
        else:
            settled_processes.append(item)
            if item.get("eligible_to_stop", False):
                eligible_entries.append((rec, item))
            else:
                deferred_settled.append(item)

    stopped_processes = _stop_settled_batch(
        store=store,
        resolved_store_root=resolved_store_root,
        default_store_root=default_store_root,
        owner_home_str=owner_home_str,
        store_has_errors=bool(errors),
        all_scope_keys_by_scope=all_scope_keys_by_scope,
        eligible_entries=eligible_entries,
    )
    stopped_count = sum(1 for row in stopped_processes if row.get("stopped"))
    stop_failed_count = sum(1 for row in stopped_processes if not row.get("stopped"))

    return {
        "owner_home": owner_home_str,
        "store_root": str(resolved_store_root),
        "min_settled_age_seconds": min_age,
        "now_ts": effective_now,
        "active_processes": active_processes,
        "settled_processes": settled_processes,
        "stopped_processes": stopped_processes,
        "deferred_settled_processes": deferred_settled,
        "unknown_processes": unknown_processes,
        "counts": {
            "active": len(active_processes),
            "settled": len(settled_processes),
            "stopped": stopped_count,
            "deferred_settled": len(deferred_settled),
            "stop_failed": stop_failed_count,
            "unknown": len(unknown_processes),
        },
    }


__all__ = [
    "DEFAULT_BACKGROUND_STOP_TIMEOUT_SECONDS",
    "SessionLeftoverStop",
    "background_process_facts",
    "background_resource_lines",
    "background_store_root",
    "cleanup_session_leftover_processes",
    "collect_runtime_memory_diagnostics",
    "freeze_session_leftover_processes",
    "freeze_settled_leftover_processes",
    "gateway_memory_tracing",
    "GatewayMemoryTracing",
    "list_running_background_processes",
    "session_background_processes",
    "stop_background_processes",
]
