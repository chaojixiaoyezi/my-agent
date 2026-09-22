# LLM: 预留、host 绑定和交接共用原 Store；stdio 管道绑定 launcher 寿命，日志后台沿原默认独立，联测两种通道。
# 模块用途: 冻结归属与寿命，托管启动并交出管道；失败关闭未交接管道且保留真实清理结果。
from __future__ import annotations

import math
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..common.cancellation import raise_if_cancelled
from ..common.json_io import write_json_file_atomic
from .process_registry import (
    _process_instance_terminated,
    capture_process_birth_token,
    terminate_process_tree,
)
from .process_scope import ProcessAccessScope, ProcessExecutionScope
from .process_session_cleanup import ProcessSessionCleanupError, stop_process_session
from .process_session_records import PROCESS_SESSION_SCHEMA
from .process_session_store import ProcessSessionStore

if TYPE_CHECKING:
    from ..plugin_activation_ref import PluginActivationRef

BACKGROUND_START_SETTLE_SECONDS = 0.5
LAUNCH_SPEC_SCHEMA = "background_process_launch.v5"


# LLM: 参数来自已授权宿主，env 不写磁盘；stdio 绑定 launcher，显式保留只控制记录裁剪，不改变执行归属或进程寿命。
# 类用途: 汇总一次托管启动的命令、归属、字节通道和权限，启动前拒绝相互矛盾的寿命或输出设置。
@dataclass(frozen=True)
class BackgroundLaunchRequest:
    argv: list[str]
    command: str
    cwd: Path
    log_path: Path | None
    env: dict[str, str]
    max_log_bytes: int
    store_root: Path
    access_scope: ProcessAccessScope
    execution_scope: ProcessExecutionScope
    completion_target: dict[str, str] = field(default_factory=dict)
    authority_check: Callable[[], None] | None = field(default=None, repr=False, compare=False)
    deadline_monotonic: float = 0.0
    stop_on_launcher_exit: bool = False
    io_mode: str = "log"
    activation: PluginActivationRef | None = None
    retain_until_consumed: bool = False

    # LLM: 零期限不限制寿命；保留策略须为宿主布尔值，共享资源附原激活引用，不能以展示字段跳过复查。
    # 函数用途: 在创建记录前验证寿命、管道、保留策略与可信激活引用；任务启动不加载插件存储。
    def __post_init__(self) -> None:
        if (type(self.deadline_monotonic) not in {int, float}
                or not math.isfinite(self.deadline_monotonic) or self.deadline_monotonic < 0
                or type(self.stop_on_launcher_exit) is not bool
                or type(self.retain_until_consumed) is not bool):
            raise ValueError("managed background lifetime invalid")
        if not isinstance(self.io_mode, str) or self.io_mode not in {"log", "stdio"}:
            raise ValueError("managed background I/O mode invalid")
        if self.io_mode == "stdio":
            if (not self.stop_on_launcher_exit or self.log_path is not None
                    or type(self.max_log_bytes) is not int or self.max_log_bytes != 0):
                raise ValueError("managed stdio requires attached lifetime without log output")
        elif self.log_path is None:
            raise ValueError("managed background log path required")
        if self.activation is not None:
            from ..plugin_activation_ref import PluginActivationRef
            from .process_session_store import process_session_store_root

            if not isinstance(self.activation, PluginActivationRef):
                raise ValueError("managed background activation reference invalid")
            owner = self.activation.owner()
            if self.store_root != process_session_store_root(owner.home_dir, owner.home_dir):
                raise ValueError("managed background activation store conflict")


# LLM: session 已在 Store 交接；process 的 stdio 管道交给调用方关闭，句柄不充当另一份生命周期权威。
# 类用途: 把同一个持久会话、本地托管句柄和可选字节管道交给调用方，不重新登记 ID。
@dataclass(frozen=True)
class HostedBackgroundProcess:
    process: subprocess.Popen
    record: dict[str, object]
    store_root: Path


# LLM: 启动错误保留已有会话和清理事实；child 创建已准入后不可宣称未发生，更不能据异常自动重放。
# 类用途: 向 Shell 返回明确失败类型、原 session 和清理是否确认，不包含环境变量或密钥。
class BackgroundLaunchError(RuntimeError):
    # LLM: 原异常仅用于分类，消息不展开私有启动参数；记录只交宿主投影。
    # 函数用途: 建立带精确启动事实的失败回执。
    def __init__(
        self,
        cause: Exception,
        record: dict[str, object],
        confirmed: bool,
        cleanup_error: dict[str, object] | None = None,
    ) -> None:
        super().__init__(f"managed background launch failed: {type(cause).__name__}")
        self.cause = cause
        self.record = record
        self.cleanup_confirmed = confirmed
        self.cleanup_error = cleanup_error


# LLM: 准入与交接在原 Store 锁内；Popen 只执行一次，提交后异常恢复原 session；未交出的 stdio 由本入口关闭。
# 函数用途: 预留资源并启动独立 host，交接成功后返回原句柄及管道，失败精确清理。
def start_background_process(
    request: BackgroundLaunchRequest, *, startup_timeout_seconds: float = 3.0
) -> HostedBackgroundProcess:
    if not request.argv or not all(isinstance(item, str) and item for item in request.argv):
        raise ValueError("managed background argv is invalid")
    store = ProcessSessionStore(request.store_root)
    session_id = f"bg-{int(time.time())}-{uuid.uuid4().hex[:16]}"
    record = _reservation(request, session_id)
    process, birth = None, ""
    handed_off = False
    spec_path = store.root / ".launches" / f"{session_id}.json"
    try:
        with store.transaction() as transaction:
            _require_admission(request)
            record = transaction.write(record)
            spec_path.parent.mkdir(mode=0o700, exist_ok=True)
            spec_path.parent.chmod(0o700)
            write_json_file_atomic(
                spec_path,
                {
                    "schema": LAUNCH_SPEC_SCHEMA,
                    "session_id": session_id,
                    "command_argv": request.argv,
                    "max_log_bytes": request.max_log_bytes,
                    "deadline_monotonic": request.deadline_monotonic,
                    "stop_on_launcher_exit": request.stop_on_launcher_exit,
                    "io_mode": request.io_mode,
                    "activation": request.activation.to_payload() if request.activation is not None else None,
                },
            )
            spec_path.chmod(0o600)
        with store.transaction() as transaction:
            _require_admission(request)
            current = transaction.load(session_id)
            if current is None or current["stop_requested"] or current["status"] != "starting":
                raise RuntimeError("managed background reservation revoked")
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    "-m",
                    "agent_py_agent.agent.tooling.background_process_host",
                    "--store",
                    str(store.root),
                    "--session",
                    session_id,
                    "--spec",
                    str(spec_path),
                ],
                stdin=subprocess.PIPE if request.io_mode == "stdio" else subprocess.DEVNULL,
                stdout=subprocess.PIPE if request.io_mode == "stdio" else subprocess.DEVNULL,
                stderr=subprocess.PIPE if request.io_mode == "stdio" else subprocess.DEVNULL,
                env=dict(request.env),
                start_new_session=os.name != "nt",
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                if os.name == "nt"
                else 0,
            )
            birth = capture_process_birth_token(process.pid)
            if not birth:
                raise OSError("managed background host identity unavailable")
        _observe_startup(store, session_id, process.pid, birth, startup_timeout_seconds)
        with store.transaction() as transaction:
            _require_admission(request)
            current = transaction.load(session_id)
            if (
                current is None
                or current["stop_requested"]
                or current["status"] not in {"running", "exited"}
            ):
                raise RuntimeError("managed background handoff unavailable")
            if current["pid"] != process.pid or current["pid_birth_token"] != birth:
                raise RuntimeError("managed background host identity conflict")
            if current["status"] == "running" and _process_instance_terminated(process.pid, birth):
                raise RuntimeError("managed background host disappeared before handoff")
            raise_if_cancelled()
            record = transaction.write({**current, "handoff_confirmed": True})
        handed_off = True
        return HostedBackgroundProcess(process, record, store.root)
    except Exception as exc:
        report = store.load(session_id)
        if report.record.get("handoff_confirmed") and process is not None:
            handed_off = True
            return HostedBackgroundProcess(process, report.record, store.root)
        try:
            record, confirmed = _abort_launch(store, report.record or record, process, birth)
        except ProcessSessionCleanupError as cleanup_exc:
            raise BackgroundLaunchError(exc, cleanup_exc.record, False, cleanup_exc.report) from exc
        raise BackgroundLaunchError(exc, record, confirmed) from exc
    finally:
        if process is not None and not handed_off and request.io_mode == "stdio":
            _close_unhanded_pipes(process)
        try:
            spec_path.unlink(missing_ok=True)
        except OSError:
            pass


# LLM: 启动失败时尚无外部 reader/writer，只有本入口拥有管道；关闭不代表未知进程已清理。
# 函数用途: 回收没有交给调用方的三路字节管道，避免失败重试留下文件描述符。
def _close_unhanded_pipes(process: subprocess.Popen) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


# LLM: 任务/激活及记录保留策略由可信宿主冻结；不从访问、通知或目录推导身份，保留不扩大执行权限。
# 函数用途: 创建尚无 host/child 的 v4 预留；共享连接不借用任务身份，stdio 不登记虚假日志。
def _reservation(request: BackgroundLaunchRequest, session_id: str) -> dict[str, object]:
    launcher_pid = os.getpid()
    birth = capture_process_birth_token(launcher_pid)
    if not birth:
        raise OSError("managed background launcher identity unavailable")
    return {
        "schema": PROCESS_SESSION_SCHEMA,
        "session_id": session_id,
        "revision": 0,
        "access_scope": asdict(request.access_scope),
        "execution_scope": asdict(request.execution_scope),
        "activation_scope": asdict(request.activation.scope) if request.activation is not None else None,
        "retain_until_consumed": request.retain_until_consumed,
        "launcher_pid": launcher_pid,
        "launcher_birth_token": birth,
        "pid": 0,
        "pid_birth_token": "",
        "child_pid": 0,
        "child_pid_birth_token": "",
        "reserved_at": time.time(),
        "started_at": 0,
        "finished_at": None,
        "exit_code": None,
        "status": "starting",
        "stop_requested": False,
        "handoff_confirmed": False,
        "child_launch_started": False,
        "command": request.command,
        "cwd": str(request.cwd),
        "output_file": str(request.log_path) if request.log_path is not None else "",
        "host_state_file": "",
        "completion_target": dict(request.completion_target),
        "completion_notice_id": "",
    }


# LLM: 在原资源锁内复读同一激活，不取插件写锁；撤销先提交安装表再冻结资源，不能反向嵌套两把写锁。
# 函数用途: 核对取消、原执行权、激活和期限，拒绝撤销后的预留、启动及交接。
def _require_admission(request: BackgroundLaunchRequest) -> None:
    raise_if_cancelled()
    if request.deadline_monotonic and time.monotonic() >= request.deadline_monotonic:
        raise TimeoutError("managed background deadline")
    if request.authority_check is not None:
        request.authority_check()
    if request.activation is not None:
        request.activation.require(allow_preparing=True)
    raise_if_cancelled()


# LLM: 缺失/starting/unknown 不能报告成功；观察期取消尚未交接，必须由启动失败路径回收。
# 函数用途: 等待 host 绑定并观察半秒真实业务状态，保留短命令的实际退出结果。
def _observe_startup(
    store: ProcessSessionStore, session_id: str, host_pid: int, birth: str, timeout: float
) -> None:
    deadline = time.monotonic() + max(0.1, timeout)
    settled_at = None
    while True:
        raise_if_cancelled()
        report = store.load(session_id)
        if report.load_error or not report.record:
            raise OSError("managed background authority unreadable")
        record = report.record
        if record["stop_requested"] or record["status"] in {"unknown", "not_started", "killed"}:
            raise RuntimeError("managed background startup revoked or unresolved")
        if record["status"] == "exited":
            return
        now = time.monotonic()
        if record["status"] == "running":
            settled_at = settled_at or now + BACKGROUND_START_SETTLE_SECONDS
            if now >= settled_at:
                return
        elif now >= deadline:
            raise TimeoutError("managed background startup timeout")
        if _process_instance_terminated(host_pid, birth):
            raise RuntimeError("managed background host exited without terminal authority")
        time.sleep(0.02)


# LLM: 只重读并停止原 ID，不重新按任务选择；写回失败不抹去可能的启动副作用，launcher 永不进入终止列表。
# 函数用途: 撤销尚未交接的启动，清理已知实例，保留未知或确认未启动的原记录。
def _abort_launch(
    store: ProcessSessionStore,
    record: dict[str, object],
    process: subprocess.Popen | None,
    birth: str,
) -> tuple[dict[str, object], bool]:
    confirmed = False
    try:
        with store.transaction() as transaction:
            current = transaction.load(record["session_id"])
            if current is None:
                return record, process is None
            record = transaction.write({**current, "stop_requested": True})
        if record["pid"]:
            cleanup = stop_process_session(store, record, host_process=process)
            return cleanup.record, cleanup.confirmed
        confirmed = process is None
        if process is not None:
            receipt = terminate_process_tree(
                process.pid, process, expected_birth_token=birth, grace_seconds=0.5
            )
            confirmed = receipt.confirmed
        with store.transaction() as transaction:
            current = transaction.load(record["session_id"])
            if current is not None and not current["child_launch_started"]:
                record = transaction.write(
                    {
                        **current,
                        "status": "not_started",
                        "finished_at": time.time(),
                        "termination": {"confirmed": confirmed, "method": "launch_aborted"},
                    }
                )
        return record, confirmed
    except ProcessSessionCleanupError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ProcessSessionCleanupError(
            exc, record, (), bool(record.get("stop_requested"))
        ) from exc
