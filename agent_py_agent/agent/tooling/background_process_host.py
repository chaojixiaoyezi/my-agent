# LLM: host 仅执行已批准 argv；从原 Store 绑定自己及 child，交接前检查 launcher，交接后只服从精确停止和日志上限。
# 模块用途: 独立持有后台命令并保存真实终态，使工作片结束不误杀资源，异常或未确认退出不伪装成功。
from __future__ import annotations

import argparse
import os
import subprocess
import time
from dataclasses import asdict
from pathlib import Path

from ..common.json_io import read_json_object_report
from .background_process_launch import LAUNCH_SPEC_SCHEMA
from .process_registry import (
    _process_instance_terminated,
    capture_process_birth_token,
    terminate_process_tree,
)
from .process_session_records import (
    PROCESS_SESSION_SCHEMA,
    PROCESS_TERMINAL_STATUSES,
    validate_session_id,
)
from .process_session_store import ProcessSessionStore

_HOST_POLL_SECONDS = 0.05


# LLM: 只从受保护的原 session 启动文件读 argv；cwd/output 来自不可变预留，环境只由父进程继承。
# 函数用途: 验证一次性交接文件及句柄，损坏时不执行命令。
def _read_launch_spec(
    store: ProcessSessionStore, session_id: str, spec_path: Path
) -> dict[str, object]:
    if spec_path != store.root / ".launches" / f"{session_id}.json":
        raise ValueError("managed background launch path conflict")
    report = read_json_object_report(spec_path, context="background_process_host.launch")
    spec = report.payload
    argv = spec.get("command_argv")
    if (
        report.load_error
        or spec.get("schema") != LAUNCH_SPEC_SCHEMA
        or spec.get("session_id") != session_id
    ):
        raise ValueError("managed background launch identity conflict")
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(arg, str) and arg for arg in argv)
    ):
        raise ValueError("managed background argv invalid")
    if type(spec.get("max_log_bytes")) is not int or spec["max_log_bytes"] < 0:
        raise ValueError("managed background log limit invalid")
    return spec


# LLM: 绑定只能发生一次；启动者消失、停止或未知阶段不得创建 child，不能用进程名恢复授权。
# 函数用途: 在原预留中登记当前 host，确认仍有可交接的启动者。
def _bind_host(store: ProcessSessionStore, session_id: str) -> dict[str, object]:
    pid, birth = os.getpid(), capture_process_birth_token(os.getpid())
    if not birth:
        raise OSError("managed host identity unavailable")
    with store.transaction() as transaction:
        record = transaction.load(session_id)
        _require_reservation(record)
        if record["pid"]:
            raise RuntimeError("managed host already bound")
        return transaction.write({**record, "pid": pid, "pid_birth_token": birth})


# LLM: 该检查只验证持锁读取的启动事实与 launcher 实例；不能替代启动端原 RuntimeDB 权限复查。
# 函数用途: 拒绝被停止、已进入创建或失去启动者的预留。
def _require_reservation(record: dict[str, object] | None) -> None:
    if record is None or record["schema"] != PROCESS_SESSION_SCHEMA:
        raise ValueError("managed background reservation missing")
    if record["stop_requested"] or record["child_launch_started"] or record["status"] != "starting":
        raise RuntimeError("managed background reservation unavailable")
    birth = capture_process_birth_token(record["launcher_pid"])
    if birth != record["launcher_birth_token"] or _process_instance_terminated(
        record["launcher_pid"], birth
    ):
        raise RuntimeError("managed background launcher unavailable")


# LLM: 创建标记先提交，Popen 与 child 绑定同锁；外力崩溃窗口保持 unknown，不将可能的副作用降为未启动。
# 函数用途: 启动并监控一个独立命令，任何异常只清理当前 host 真正创建的子树。
def run_background_process_host(
    store: ProcessSessionStore, session_id: str, spec_path: Path
) -> int:
    child, birth = None, ""
    try:
        spec = _read_launch_spec(store, session_id, spec_path)
        spec_path.unlink()
        _bind_host(store, session_id)
        with store.transaction() as transaction:
            record = transaction.load(session_id)
            _require_reservation(record)
            record = transaction.write({**record, "child_launch_started": True})
            # 留住未回收 child；短命令的出生身份必须先于任何 poll/wait 采集。
            with Path(record["output_file"]).open("ab") as log:
                child = subprocess.Popen(
                    spec["command_argv"],
                    shell=False,
                    cwd=record["cwd"],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=dict(os.environ),
                    start_new_session=os.name != "nt",
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    if os.name == "nt"
                    else 0,
                )
            birth = capture_process_birth_token(child.pid)
            if not birth:
                raise OSError("managed child identity unavailable")
            record = transaction.write(
                {
                    **record,
                    "child_pid": child.pid,
                    "child_pid_birth_token": birth,
                    "started_at": time.time(),
                    "status": "running",
                }
            )
        return _monitor_child(store, session_id, child, birth, spec["max_log_bytes"])
    except Exception as exc:
        receipt = (
            terminate_process_tree(
                child.pid, child, expected_birth_token=birth or None, grace_seconds=0.5
            )
            if child
            else None
        )
        _publish_failure(store, session_id, receipt, type(exc).__name__)
        return 1
    finally:
        try:
            spec_path.unlink(missing_ok=True)
        except OSError:
            pass


# LLM: 不提前回收组长；自然退出也先核对遗留后代。handoff 后 launcher 的退出与旧 token 不再影响此资源。
# 函数用途: 持续检查精确停止和日志上限，按真实树退出回执持久化终态或未知状态。
def _monitor_child(
    store: ProcessSessionStore,
    session_id: str,
    child: subprocess.Popen,
    birth: str,
    max_log_bytes: int,
) -> int:
    while True:
        report = store.load(session_id)
        if report.load_error or not report.record:
            raise OSError("managed background authority unreadable")
        record = report.record
        reason = _stop_reason(record, max_log_bytes)
        ended = _process_instance_terminated(child.pid, birth)
        if reason or ended:
            receipt = terminate_process_tree(
                child.pid, child, expected_birth_token=birth, grace_seconds=0.5
            )
            with store.transaction() as transaction:
                current = transaction.load(session_id)
                if current is None:
                    raise OSError("managed background authority missing")
                stopped = reason or current["stop_requested"]
                status = ("killed" if stopped else "exited") if receipt.confirmed else "unknown"
                transaction.write(
                    {
                        **current,
                        "status": status,
                        "finished_at": time.time() if receipt.confirmed else None,
                        "exit_code": receipt.return_code if receipt.confirmed else None,
                        "reason": reason or "command_exited",
                        "termination": asdict(receipt),
                    }
                )
            return receipt.return_code if receipt.return_code is not None else 1
        time.sleep(_HOST_POLL_SECONDS)


# LLM: 控制依据仅来自结构化停止事实、launcher 出生身份和文件字节数，不解析命令或日志正文。
# 函数用途: 决定当前 host 是否应收回业务子进程，正常无事时返回空原因。
def _stop_reason(record: dict[str, object], max_log_bytes: int) -> str:
    if record["stop_requested"]:
        return "stop_requested"
    if not record["handoff_confirmed"]:
        pid, birth = record["launcher_pid"], record["launcher_birth_token"]
        if capture_process_birth_token(pid) != birth or _process_instance_terminated(pid, birth):
            return "launcher_unavailable"
    try:
        if max_log_bytes > 0 and Path(record["output_file"]).stat().st_size > max_log_bytes:
            return "log_limit_exceeded"
    except OSError:
        pass
    return ""


# LLM: 只发布属于本 host 的结果；清理未确认或进入创建却没有 child 身份时保持未知，不能擦掉旧终态。
# 函数用途: 保存启动/监控异常的类型与实际清理回执；权威故障时退出，后续查询继续保留未知。
def _publish_failure(
    store: ProcessSessionStore, session_id: str, receipt: object, error_type: str
) -> None:
    try:
        with store.transaction() as transaction:
            current = transaction.load(session_id)
            if (
                not current
                or current["pid"] != os.getpid()
                or current["status"] in PROCESS_TERMINAL_STATUSES
            ):
                return
            status = "unknown"
            if not current["child_launch_started"]:
                status = "not_started"
            elif current["child_pid"] and receipt is not None and receipt.confirmed:
                status = "killed"
            transaction.write(
                {
                    **current,
                    "status": status,
                    "error_type": error_type,
                    "exit_code": receipt.return_code if status == "killed" else None,
                    "finished_at": time.time() if status in PROCESS_TERMINAL_STATUSES else None,
                    "termination": asdict(receipt) if receipt is not None else {"confirmed": False},
                }
            )
    except (OSError, RuntimeError, TypeError, ValueError):
        pass


# LLM: 内部 CLI 只接原 Store、session 和一次性 spec；没有模型可指定的进程实例或任意状态路径。
# 函数用途: 运行独立后台托管入口。
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--store", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--spec", required=True)
    args = parser.parse_args(argv)
    return run_background_process_host(
        ProcessSessionStore(args.store),
        validate_session_id(args.session),
        Path(args.spec).resolve(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
