# LLM: host 仅执行已批准 argv；stdio 直接继承管道并绑定 launcher，日志后台沿旧默认寿命，原进程账仍唯一。
# 模块用途: 独立托管命令并记录真实终态，分开日志与协议字节，不转发数据或伪造退出成功。
from __future__ import annotations

import argparse
import math
import os
import subprocess
import time
from contextlib import contextmanager, nullcontext
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
    MANAGED_PROCESS_SESSION_SCHEMAS,
    PROCESS_TERMINAL_STATUSES,
    validate_session_id,
)
from .process_session_store import ProcessSessionStore

_HOST_POLL_SECONDS = 0.05


# LLM: 只接收当前 v4 完整通道与寿命字段；环境只继承父进程，旧信封不补模式默认值，stdio 禁止日志预算。
# 函数用途: 验证一次性交接文件及通道设置，损坏或通道寿命矛盾时不启动命令。
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
    deadline = spec.get("deadline_monotonic")
    if (type(deadline) not in {int, float} or not math.isfinite(deadline) or deadline < 0
            or type(spec.get("stop_on_launcher_exit")) is not bool):
        raise ValueError("managed background lifetime invalid")
    mode = spec.get("io_mode")
    if not isinstance(mode, str) or mode not in {"log", "stdio"}:
        raise ValueError("managed background I/O mode invalid")
    if mode == "stdio" and (not spec["stop_on_launcher_exit"] or spec["max_log_bytes"] != 0):
        raise ValueError("managed stdio requires attached lifetime without log output")
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


# LLM: 持锁检查 v2/v3 原启动事实和 launcher 实例；不能替代启动端的任务/插件权威复查，不迁移旧记录。
# 函数用途: 拒绝被停止、已进入创建或失去启动者的预留。
def _require_reservation(record: dict[str, object] | None) -> None:
    if record is None or record["schema"] not in MANAGED_PROCESS_SESSION_SCHEMAS:
        raise ValueError("managed background reservation missing")
    if record["stop_requested"] or record["child_launch_started"] or record["status"] != "starting":
        raise RuntimeError("managed background reservation unavailable")
    birth = capture_process_birth_token(record["launcher_pid"])
    if birth != record["launcher_birth_token"] or _process_instance_terminated(
        record["launcher_pid"], birth
    ):
        raise RuntimeError("managed background launcher unavailable")


# LLM: 创建标记先提交，Popen 与 child 绑定同锁；stdio 继承后释放 host 端点，异常仍按真实副作用清理。
# 函数用途: 启动并监控精确归属命令，让协议管道直接连到 child，保存同一资源账的终态。
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
            if spec["deadline_monotonic"] and time.monotonic() >= spec["deadline_monotonic"]:
                raise TimeoutError("managed background deadline")
            if (spec["io_mode"] == "stdio") != (record["output_file"] == ""):
                raise ValueError("managed background output binding conflict")
            record = transaction.write({**record, "child_launch_started": True})
            # 留住未回收 child；短命令的出生身份必须先于任何 poll/wait 采集。
            with _child_stdio(spec, record) as streams:
                child = subprocess.Popen(
                    spec["command_argv"], shell=False, cwd=record["cwd"], **streams,
                    env=dict(os.environ), start_new_session=os.name != "nt",
                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0,
                )
            if spec["io_mode"] == "stdio":
                _release_host_pipes()
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
        return _monitor_child(store, session_id, child, birth, spec["max_log_bytes"],
                              deadline_monotonic=spec["deadline_monotonic"],
                              stop_on_launcher_exit=spec["stop_on_launcher_exit"])
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


# LLM: stdio 的 None 继承标准端点；日志关闭可抛错，调用方必须在退出上下文前拿到 Popen，保留精确清理权。
# 函数用途: 为已校验模式准备子进程端点，继承后关闭 host 日志句柄，不转发或解码字节。
@contextmanager
def _child_stdio(spec: dict[str, object], record: dict[str, object]):
    stdio = spec["io_mode"] == "stdio"
    with (nullcontext() if stdio else Path(record["output_file"]).open("ab")) as log:
        yield {"stdin": None if stdio else subprocess.DEVNULL,
               "stdout": None if stdio else log, "stderr": None if stdio else subprocess.STDOUT}


# LLM: 仅独立 host 在 child 继承后调用；释放重复端点使 EOF 由 child 决定，保留有效标准 fd 避免后续诊断写入协议流。
# 函数用途: 将 host 自己的标准流换到空设备，实际通信端点仅留给 child。
def _release_host_pipes() -> None:
    with open(os.devnull, "r+b", buffering=0) as sink:
        for fd in (0, 1, 2):
            os.dup2(sink.fileno(), fd)


# LLM: 不提前回收组长；自然退出也核对后代。只有宿主显式要求时，handoff 后仍受 launcher 和原期限约束。
# 函数用途: 持续检查停止、寿命和日志上限，按真实树退出回执持久化结果。
def _monitor_child(
    store: ProcessSessionStore,
    session_id: str,
    child: subprocess.Popen,
    birth: str,
    max_log_bytes: int,
    *,
    deadline_monotonic: float = 0.0,
    stop_on_launcher_exit: bool = False,
) -> int:
    while True:
        report = store.load(session_id)
        if report.load_error or not report.record:
            raise OSError("managed background authority unreadable")
        record = report.record
        reason = _stop_reason(record, max_log_bytes, deadline_monotonic=deadline_monotonic,
                              stop_on_launcher_exit=stop_on_launcher_exit)
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


# LLM: 控制只读结构化事实及同一 monotonic 期限，不解析正文；既有长期后台仍允许 launcher 交接后退出。
# 函数用途: 判断是否收回精确 child，准备进程可因宿主消失或到期而停止。
def _stop_reason(record: dict[str, object], max_log_bytes: int, *,
                 deadline_monotonic: float = 0.0, stop_on_launcher_exit: bool = False) -> str:
    if record["stop_requested"]:
        return "stop_requested"
    if deadline_monotonic and time.monotonic() >= deadline_monotonic:
        return "deadline_exceeded"
    if stop_on_launcher_exit or not record["handoff_confirmed"]:
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
