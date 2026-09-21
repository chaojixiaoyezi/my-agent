# LLM: 控制层先关闭原执行权，再调用冻结；worker 只消费固定 receipt，不重新按任务选择资源或借 current 身份。
# 模块用途: 把任务控制交给原后台 Store 和单 session 清理入口，保留部分冻结及每个资源的未知结果。
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .process_scope import ProcessExecutionScope
from .process_session_cleanup import ProcessSessionCleanupError, stop_process_session
from .process_session_commit import ProcessSessionCommitPendingError, ProcessSessionCommitReceipt
from .process_session_store import ProcessSessionStore, process_session_store_root
from .pty_sessions import pty_session_registry


# LLM: receipt 是原 Store 提交的固定集合；不保存 agent、任务查询回调或能够扩大停止范围的选择器。
# 类用途: 把已落盘的停止清单交给锁外清理，恢复的新资源不会被旧 worker 纳入。
@dataclass(frozen=True)
class FrozenProcessStop:
    store_root: Path
    receipt: ProcessSessionCommitReceipt
    pty_session_ids: tuple[str, ...] = ()
    background_freeze_error: str = ""
    pty_request_error: str = ""


# LLM: owner/执行范围已由宿主校验；只消费本次 request_stop 的回执，其它 redo 恢复失败单列错误。
# 函数用途: 冻结后台清单并请求原 PTY 回收；部分失败保留已提交清单，不把未确认写成未发生。
def freeze_process_stop(scope: ProcessExecutionScope) -> FrozenProcessStop:
    if not scope.owner_home or not scope.run_id:
        raise ValueError("后台停止必须提供原 owner 和 run 身份")
    root = process_session_store_root(scope.owner_home, scope.owner_home)
    store = ProcessSessionStore(root)
    receipt = ProcessSessionCommitReceipt("", ())
    background_error, pty_error, terminals = "", "", ()
    try:
        if root.exists():
            with store.transaction() as transaction:
                try:
                    receipt = transaction.request_stop(scope)
                except ProcessSessionCommitPendingError as exc:
                    receipt = exc.receipt
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        background_error = type(exc).__name__
    try:
        terminals = pty_session_registry.request_stop(
            owner_home=scope.owner_home, thread_id=scope.thread_id, task_id=scope.root_task_id,
            run_id=scope.run_id, attempt_id=scope.attempt_id,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        pty_error = type(exc).__name__
    return FrozenProcessStop(root, receipt, terminals, background_error, pty_error)


# LLM: 每条清理沿原 receipt 的身份，持久恢复仍由 Store 完成；异常不扩大选择范围，不声称未启动。
# 函数用途: 锁外逐条清理固定后台资源；确认只针对后台，PTY 异步请求不代表已退出。
def cleanup_process_stop(batch: FrozenProcessStop) -> dict[str, object]:
    store = ProcessSessionStore(batch.store_root)
    results = []
    for record in batch.receipt.records:
        try:
            result = stop_process_session(store, record)
            results.append({
                "session_id": record["session_id"], "confirmed": result.confirmed,
                "status": result.record["status"],
            })
        except ProcessSessionCleanupError as exc:
            results.append({
                "session_id": record["session_id"], "confirmed": False,
                "status": "unknown", "error": exc.report,
            })
    report = {
        "transaction_id": batch.receipt.transaction_id,
        "background_confirmed": not batch.background_freeze_error and all(row["confirmed"] for row in results),
        "background_freeze_error": batch.background_freeze_error,
        "sessions": results,
        "pty": {
            "status": "unknown" if batch.pty_request_error else "requested" if batch.pty_session_ids else "not_selected",
            "session_ids": batch.pty_session_ids,
            "error_type": batch.pty_request_error,
        },
    }
    if not report["background_confirmed"] or batch.pty_request_error:
        logging.getLogger(__name__).warning("后台资源停止仍有未确认项: %s", report)
    return report
