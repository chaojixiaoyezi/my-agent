# LLM: 本模块只处理已验证 session 的精确资源；launcher 是存活证据，永远不是清理目标；不得按任务重扫。
# 模块用途: 在原 Store 提交停止意图后，按冻结的 host/child 出生身份清理并保存可信结果，未确认保持未知。
from __future__ import annotations

import subprocess
import time
from dataclasses import asdict, dataclass

from .process_registry import (
    ProcessTerminationReceipt,
    _process_instance_terminated,
    capture_process_birth_token,
    terminate_process_tree,
)
from .process_session_commit import ProcessSessionCommitPendingError
from .process_session_records import (
    PROCESS_SESSION_SCHEMA,
    PROCESS_TERMINAL_STATUSES,
    merge_process_record,
)
from .process_session_store import ProcessSessionStore


# LLM: confirmed 只说明冻结的已知实例清理；业务文件或外部副作用不因此回滚，不可据此自动重放命令。
# 类用途: 向启动失败和显式停止入口返回真实清理结果及最新持久记录。
@dataclass(frozen=True)
class ProcessSessionCleanup:
    record: dict[str, object]
    confirmed: bool
    terminations: tuple[ProcessTerminationReceipt, ...] = ()


# LLM: 已提交意图与已经发过信号均不可降为未发生；回执只包含原 session，不重新选择资源。
# 类用途: 向控制/工具入口报告清理过程中的持久化故障及已知终止结果。
class ProcessSessionCleanupError(RuntimeError):
    # LLM: 异常不包含原始记录正文；pending commit 的固定 ID 和实际终止回执继续向上传递。
    # 函数用途: 保存故障阶段、原记录和已确认副作用。
    def __init__(
        self,
        cause: Exception,
        record: dict[str, object],
        receipts: tuple[ProcessTerminationReceipt, ...],
        committed: bool,
    ) -> None:
        super().__init__("managed process cleanup outcome unresolved")
        self.record = record
        pending = isinstance(cause, ProcessSessionCommitPendingError)
        pending_record = (
            next(
                (
                    row
                    for row in cause.receipt.records
                    if row["session_id"] == record["session_id"] and row["stop_requested"]
                ),
                None,
            )
            if pending
            else None
        )
        self.report = {
            "error_type": type(cause).__name__,
            "committed": committed or pending_record is not None,
            "recovery_required": pending,
            "session_ids": [record["session_id"]],
            "termination_receipts": [asdict(receipt) for receipt in receipts],
        }
        if pending:
            self.report["recovery_transaction_id"] = cause.receipt.transaction_id
            if pending_record is not None:
                self.report["transaction_id"] = cause.receipt.transaction_id
                self.record = pending_record


# LLM: 首次读改写固定同一句柄身份，调用方已验证访问权或任务执行范围；停止意图落盘前不发信号。
# 函数用途: 停止一个已选定的 v2 session，不扩大为同任务的其它资源或启动者进程。
def stop_process_session(
    store: ProcessSessionStore,
    selected: dict[str, object],
    *,
    host_process: subprocess.Popen | None = None,
) -> ProcessSessionCleanup:
    if selected.get("schema") != PROCESS_SESSION_SCHEMA:
        raise ValueError("managed session cleanup requires v2 authority")
    frozen, receipts, committed = selected, (), False
    try:
        with store.transaction() as transaction:
            current = transaction.load(str(selected["session_id"]))
            if current is None:
                return ProcessSessionCleanup(selected, False)
            merge_process_record(selected, current)
            frozen = transaction.write({**current, "stop_requested": True})
            committed = True
        receipts = _terminate_frozen_instances(frozen, host_process)
        with store.transaction() as transaction:
            current = transaction.load(str(frozen["session_id"]))
            if current is None:
                return ProcessSessionCleanup(frozen, False, receipts)
            merge_process_record(frozen, current)
            known_terminal = current["status"] in PROCESS_TERMINAL_STATUSES
            child_known = bool(current["child_pid"]) or not current["child_launch_started"]
            instances_gone = all(
                not current[pid_key]
                or _process_instance_terminated(current[pid_key], current[birth_key])
                for pid_key, birth_key in (
                    ("pid", "pid_birth_token"),
                    ("child_pid", "child_pid_birth_token"),
                )
            )
            no_host_confirmed = (
                not current["pid"]
                and current["status"] == "not_started"
                and (current.get("termination") or {}).get("confirmed") is True
            )
            confirmed = (
                no_host_confirmed
                or bool(current["pid"])
                and child_known
                and instances_gone
                and (known_terminal or bool(receipts))
                and all(receipt.confirmed for receipt in receipts)
            )
            if not known_terminal:
                status = (
                    ("killed" if current["child_pid"] else "not_started")
                    if confirmed
                    else "unknown"
                )
                current = transaction.write(
                    {
                        **current,
                        "status": status,
                        "finished_at": time.time() if confirmed else None,
                        "exit_code": None,
                        "termination": {
                            "confirmed": confirmed,
                            "instances": [asdict(r) for r in receipts],
                        },
                    }
                )
            return ProcessSessionCleanup(current, confirmed, receipts)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ProcessSessionCleanupError(exc, frozen, receipts, committed) from exc


# LLM: 出生标识在终止原语采集快照后再次核对；旧 PID 不授予新树，启动者绝不在此列表里。
# 函数用途: 在 Store 锁外尝试清理已冻结的托管进程和业务子进程，逐项保留未确认回执。
def _terminate_frozen_instances(
    record: dict[str, object],
    host_process: subprocess.Popen | None,
) -> tuple[ProcessTerminationReceipt, ...]:
    receipts = []
    for pid_key, birth_key in (("pid", "pid_birth_token"), ("child_pid", "child_pid_birth_token")):
        pid, birth = record[pid_key], record[birth_key]
        if not pid:
            continue
        # 未回收组长即使已退出，也要保留其出生身份来检查仍占用同组的后代。
        if capture_process_birth_token(pid) != birth and _process_instance_terminated(pid, birth):
            continue
        process = (
            host_process
            if pid_key == "pid" and host_process is not None and host_process.pid == pid
            else None
        )
        receipts.append(
            terminate_process_tree(pid, process, expected_birth_token=birth, grace_seconds=0.5)
        )
    return tuple(receipts)
