# LLM: 只读本 gateway owner 的托管后台进程登记表，并复用既有 ProcessSessionStore.request_stop
#   冻结停止意图；不新增登记表、不按进程名杀进程、不解析命令正文。实际回收由原 host 的
#   terminate_process_tree 按进程组完成，孙进程随之结束（只给登记 pid 发信号会漏掉孙进程）。
# 模块用途: 为 gateway stop 提供"列出仍在运行的后台进程"与"按显式要求一并停止"的结构化事实。
from __future__ import annotations

import time
from pathlib import Path

from ..tooling.process_scope import ProcessExecutionScope
from ..tooling.process_session_records import (
    MANAGED_PROCESS_SESSION_SCHEMAS,
    PROCESS_TERMINAL_STATUSES,
)
from ..tooling.process_session_store import ProcessSessionStore

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


__all__ = [
    "DEFAULT_BACKGROUND_STOP_TIMEOUT_SECONDS",
    "background_process_facts",
    "list_running_background_processes",
    "stop_background_processes",
]
