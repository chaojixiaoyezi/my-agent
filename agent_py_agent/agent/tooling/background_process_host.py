# LLM: host 仅执行已批准 argv；stdio 直接继承管道并绑定 launcher，日志后台沿旧默认寿命，原进程账仍唯一。
# 模块用途: 独立托管命令并记录真实终态，分开日志与协议字节，不转发数据或伪造退出成功。
from __future__ import annotations

import argparse
import ctypes
import math
import os
import stat
import subprocess
import sys
import time
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

from ..common.json_io import read_json_object_report
from .background_process_launch import LAUNCH_SPEC_SCHEMA
from .listen_scope import (
    LISTEN_SCOPE_VIOLATION,
    normalize_listen_scope,
    observe_tree_listeners,
    scope_violations,
)
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

# 后台进程启动首秒保持50ms观察；之后由退避函数降低空闲唤醒，仍不提前回收组长。
_HOST_POLL_SECONDS = 0.05
# 监听范围核对的间隔：读 socket 表有成本，不必跟 50ms 的存活轮询同频。
_LISTEN_CHECK_SECONDS = 2.0


# LLM: 只决定本host的睡眠间隔，不调用poll/wait，不影响出生身份、原记录投影与终止顺序。
# 函数用途: 首秒50ms、十秒内150ms、长期250ms观察一次，保留短命令灵敏度。
def _monitor_poll_interval(elapsed_s: float) -> float:
    return _HOST_POLL_SECONDS if elapsed_s < 1.0 else 0.15 if elapsed_s < 10.0 else 0.25


# LLM: 监控期只读的结构化策略，全部来自已校验的启动交接文件；不从记录正文或命令推断。
# 类用途: 把 child 的寿命上限、launcher 绑定和监听范围声明打成一个不可变参数，供监控循环使用。
@dataclass(frozen=True)
class _MonitorPolicy:
    deadline_monotonic: float = 0.0
    stop_on_launcher_exit: bool = False
    listen_scope: str = "loopback"
    listen_scope_enforce: bool = True


# LLM: 只接收当前 v6 完整字段（含 listen_scope/listen_scope_enforce）；激活引用必须明确有值或 null，环境只继承父进程，
#   旧信封不补默认值。
# 函数用途: 验证一次性交接文件、通道寿命与监听范围声明，畸形或缺少字段时不启动命令。
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
        or "activation" not in spec
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
    if "listen_scope" not in spec or type(spec.get("listen_scope_enforce")) is not bool:
        raise ValueError("managed background listen scope declaration missing")
    spec["listen_scope"] = normalize_listen_scope(spec["listen_scope"])
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


# LLM: 原资源锁内复查安装权威后才提交创建标记和 Popen；撤销后的冻结可看到所有已准入 child，异常保留真实清理事实。
# 函数用途: 核对激活并启动精确归属命令，协议管道直连 child，终态仍写原资源账。
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
            _require_activation(spec, record, store)
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
        return _monitor_child(store, session_id, child, birth, spec["max_log_bytes"], _MonitorPolicy(
            deadline_monotonic=spec["deadline_monotonic"], stop_on_launcher_exit=spec["stop_on_launcher_exit"],
            listen_scope=spec["listen_scope"], listen_scope_enforce=spec["listen_scope_enforce"],
        ))
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


# LLM: 只读原安装表，不取其写锁；信封引用须匹配原 session 和原 Store，空引用不能替共享连接放行。
# 函数用途: 在实际 child 创建前拒绝已停用、换代、跨用户或损坏的插件启动。
def _require_activation(spec: dict, record: dict, store: ProcessSessionStore) -> None:
    payload, scope = spec["activation"], record.get("activation_scope")
    if payload is None:
        if scope is not None:
            raise ValueError("managed background activation reference missing")
        return
    from ..plugin_activation_ref import PluginActivationRef
    from .process_session_store import process_session_store_root

    activation = PluginActivationRef.from_payload(payload)
    owner = activation.owner()
    if scope != asdict(activation.scope) or store.root != process_session_store_root(owner.home_dir, owner.home_dir):
        raise ValueError("managed background activation binding conflict")
    activation.require(allow_preparing=True)


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


# LLM: 延迟绑定Darwin libc的sysctl，进程内复用原生句柄，不创建子进程；不可用保持未知。
# 函数用途: 取得带明确ctypes签名的sysctl函数入口。
@lru_cache(maxsize=1)
def _darwin_libc():
    if sys.platform != "darwin":
        return None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                               ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
        libc.sysctl.restype = ctypes.c_int
        return libc
    except (AttributeError, OSError):
        return None


# LLM: 仅Darwin直接child使用；kinfo_proc偏移36的p_stat为5表示僵尸，只观察不回收，失败交原身份路径。
# 函数用途: 在无waitid的macOS解释器上用两阶段sysctl零fork观察直接子进程退出。
def _darwin_child_is_zombie(pid: int) -> bool | None:
    if sys.platform != "darwin" or pid <= 0:
        return None
    try:
        libc = _darwin_libc()
        if libc is None:
            return None
        mib = (ctypes.c_int * 4)(1, 14, 1, int(pid))
        size = ctypes.c_size_t(0)
        if libc.sysctl(mib, 4, None, ctypes.byref(size), None, 0) != 0 or size.value == 0:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 4, buf, ctypes.byref(size), None, 0) != 0 or size.value < 37:
            return None
        return bool(buf.raw[36] == 5)
    except Exception:
        return None


# LLM: 仅真实根目录与普通记录可缓存；redo存在、权限错误或链接都返回None，不把无法确认当成稳定。
# 函数用途: 读取根与记录的lstat五元组，并确认没有待恢复redo。
def _monitored_record_stamp(store: ProcessSessionStore, session_id: str) -> tuple | None:
    try:
        path = store.record_path(session_id)
        root, record = store.root.lstat(), path.lstat()
        if not stat.S_ISDIR(root.st_mode) or not stat.S_ISREG(record.st_mode):
            return None
        try:
            (store.root / ".process-sessions.redo.json").lstat()
        except FileNotFoundError:
            pass
        else:
            return None
        fields = ("st_dev", "st_ino", "st_mtime_ns", "st_ctime_ns", "st_size")
        stamps = tuple(tuple(getattr(info, name) for name in fields) for info in (root, record))
        return (store.root, path, *stamps)
    except OSError:
        return None


# LLM: 每个monitor独享只读成功投影；两五元组及路径不变、无redo且距上次完整读取不足0.5秒才复用。
# 类用途: 避免50ms循环反复抢记录锁并解析同一JSON，同时保持有界强制全读。
class _HostRecordProjection:
    # LLM: 初始无可信内容，缓存命中不刷新完整读取时间。
    # 函数用途: 初始化本monitor的单条记录投影。
    def __init__(self) -> None:
        self._report = None
        self._stamp = None
        self._loaded_at = 0.0

    # LLM: load前后记录五元组及根dev/inode必须一致；根mtime允许原load自身锁目录改变，命中仍比完整根五元组。
    #   不缓存错误或缺失，读取中记录变化不发布投影，异常前先废弃旧缓存；monotonic倒退也强制重读。
    # 函数用途: 复用稳定的短期成功报告，失效时立即走原Store读取和redo恢复。
    def load(self, store: ProcessSessionStore, session_id: str):
        now = time.monotonic()
        before = _monitored_record_stamp(store, session_id)
        if (before is not None and before == self._stamp and self._report is not None
                and 0 <= now - self._loaded_at < 0.5):
            return self._report
        self._report = self._stamp = None
        report = store.load(session_id)
        after = _monitored_record_stamp(store, session_id)
        if (not report.load_error and report.record and before is not None and after is not None
                and before[:2] == after[:2] and before[2][:2] == after[2][:2] and before[3] == after[3]):
            self._report, self._stamp, self._loaded_at = report, after, now
        return report


# LLM: 仅用于本host的直接child；WNOWAIT保留组长僵尸态与出生身份，实际回收仍归原terminate_process_tree。
# 函数用途: 优先waitid，无该API的Darwin走sysctl；均失败则返回None交原身份检查。
def _direct_child_exited_unreaped(child: subprocess.Popen[bytes]) -> bool | None:
    required = ("waitid", "P_PID", "WEXITED", "WNOHANG", "WNOWAIT")
    if os.name != "posix":
        return None
    if not all(hasattr(os, key) for key in required):
        return _darwin_child_is_zombie(child.pid)
    try:
        result = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except InterruptedError:
        return False
    except OSError:
        return None
    return bool(result is not None and getattr(result, "si_pid", 0) == child.pid)


# LLM: 先以WNOWAIT观察直接child，不支持时才查原出生身份；终止树验证前不得poll/wait回收组长，自然退出也核对后代。
#   只有宿主显式要求时，handoff后仍受launcher和原期限约束，监听与终态账不变。
#   成功记录仅在stat稳定、无redo且半秒以内复用，不缓存停止裁决或绕过终止前的原权威事务。
#   仅睡眠按本host单调已运行时长退避；首秒仍保持原观察间隔，绝不以Popen.wait提前回收组长。
# 函数用途: 持续检查停止、寿命和日志上限，用不回收的退出观察驱动原终止树入口及结果持久化。
def _monitor_child(
    store: ProcessSessionStore,
    session_id: str,
    child: subprocess.Popen,
    birth: str,
    max_log_bytes: int,
    policy: _MonitorPolicy | None = None,
) -> int:
    policy = policy or _MonitorPolicy()
    projection = _HostRecordProjection()
    started_monotonic = next_listen_check = time.monotonic()
    while True:
        report = projection.load(store, session_id)
        if report.load_error or not report.record:
            raise OSError("managed background authority unreadable")
        record = report.record
        reason = _stop_reason(record, max_log_bytes, deadline_monotonic=policy.deadline_monotonic,
                              stop_on_launcher_exit=policy.stop_on_launcher_exit)
        if not reason and policy.listen_scope != "lan" and time.monotonic() >= next_listen_check:
            next_listen_check = time.monotonic() + _LISTEN_CHECK_SECONDS
            reason = _listen_scope_check(store, session_id, child.pid, policy.listen_scope, policy.listen_scope_enforce)
        ended = _direct_child_exited_unreaped(child)
        if ended is None:
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
        time.sleep(_monitor_poll_interval(time.monotonic() - started_monotonic))


# LLM: 用真实 socket 表核对 child 进程树（listen_scope 模块：Linux /proc、其它 POSIX lsof），越出 loopback 的监听按 enforce 决定
#   是回收（返回 reason=listen_scope_violation）还是只记一次 listener_warning；证据（地址、端口、PID、观测码）写进会话记录，
#   不解析命令正文。观测不到（unsupported/lsof 缺失）不算越界。
# 函数用途: 声明只允许本机回环时，发现服务绑到了局域网地址就回收它，并把证据留在记录里供 status 查看。
def _listen_scope_check(
    store: ProcessSessionStore, session_id: str, child_pid: int, listen_scope: str, enforce: bool,
) -> str:
    bindings, observation = observe_tree_listeners(child_pid)
    violations = scope_violations(listen_scope, bindings)
    if not violations:
        return ""
    field = "listener_violation" if enforce else "listener_warning"
    evidence = {
        "observed_at": time.time(), "observation": observation, "listen_scope": listen_scope,
        "bindings": violations, "enforced": enforce,
    }
    with store.transaction() as transaction:
        current = transaction.load(session_id)
        if current is not None and not current.get(field):
            transaction.write({**current, field: evidence})
    return LISTEN_SCOPE_VIOLATION if enforce else ""


# LLM: reservation已在启动时核过launcher出生身份；仍是直接父进程时PID不会在该父子关系中复用，非直接父关系回退原出生核对。
# 函数用途: 正常POSIX父子关系用getppid零子进程开销确认启动方存活，否则核对原出生令牌和终止状态。
def _launcher_instance_alive(record: dict[str, object]) -> bool:
    pid = int(record["launcher_pid"])
    birth = str(record["launcher_birth_token"])
    if os.name == "posix" and pid > 0 and os.getppid() == pid:
        return True
    return capture_process_birth_token(pid) == birth and not _process_instance_terminated(pid, birth)


# LLM: 控制只读结构化事实及同一monotonic期限，不解析正文；launcher只在仍为直属父时短路身份探测，既有长期后台仍允许交接后退出。
# 函数用途: 判断是否收回精确child；停止和期限保持优先，需附着launcher时沿安全亲子快路径或原出生核对。
def _stop_reason(record: dict[str, object], max_log_bytes: int, *,
                 deadline_monotonic: float = 0.0, stop_on_launcher_exit: bool = False) -> str:
    if record["stop_requested"]:
        return "stop_requested"
    if deadline_monotonic and time.monotonic() >= deadline_monotonic:
        return "deadline_exceeded"
    if stop_on_launcher_exit or not record["handoff_confirmed"]:
        if not _launcher_instance_alive(record):
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
