# LLM: 只读本 gateway owner 的托管后台进程登记表，并复用既有 ProcessSessionStore.request_stop
#   冻结停止意图；不新增登记表、不按进程名杀进程、不解析命令正文。实际回收由原 host 的
#   terminate_process_tree 按进程组完成，孙进程随之结束（只给登记 pid 发信号会漏掉孙进程）。
#   会话级 /stop（本地与 Gateway 两个入口）也从这里取"本会话遗留"的选择与回执渲染；Gateway 入口走任务停止同一个
#   freeze_process_stop/cleanup_process_stop，不另造停止路径。
# 模块用途: 为 gateway stop 与会话内 /stop 提供"列出仍在运行的后台进程"与"按显式要求一并停止"的结构化事实。
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from ..tooling.process_resource_stop import (
    FrozenProcessStop,
    cleanup_process_stop,
    freeze_process_stop,
)
from ..tooling.process_scope import ProcessExecutionScope
from ..tooling.process_session_records import (
    MANAGED_PROCESS_SESSION_SCHEMAS,
    PROCESS_TERMINAL_STATUSES,
)
from ..tooling.process_session_store import ProcessSessionStore, process_session_store_root

# 停止意图落到记录后，原 host 的监控循环按自己的轮询间隔发现；等待回收要留足余量。
DEFAULT_BACKGROUND_STOP_TIMEOUT_SECONDS = 10.0
_POLL_SECONDS = 0.1


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
) -> SessionLeftoverStop:
    running, errors = list_running_background_processes(background_store_root(owner_home, owner_home))
    if errors:
        raise ValueError("后台进程登记表存在读不出的记录")
    selected = tuple(
        row for row in session_background_processes(running, thread_id)
        if str(row.get("root_task_id") or "") not in protected_root_task_ids
    )
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


__all__ = [
    "DEFAULT_BACKGROUND_STOP_TIMEOUT_SECONDS",
    "SessionLeftoverStop",
    "background_process_facts",
    "background_resource_lines",
    "background_store_root",
    "cleanup_session_leftover_processes",
    "freeze_session_leftover_processes",
    "list_running_background_processes",
    "session_background_processes",
    "stop_background_processes",
]
