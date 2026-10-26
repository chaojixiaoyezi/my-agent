# LLM: install后独占进程级诊断；长tick由单一常驻线程延迟升级C定时器，短期限及替身保留同步合同；close与晋升共锁。
# 模块用途: 向Gateway专用私有追加日志写GC暂停、线程栈及监护快照，不读消息正文、不改变业务状态或重启进程。
from __future__ import annotations

import faulthandler
import gc
import json
import math
import os
import signal
import stat
import threading
import time
from collections import Counter
from pathlib import Path
from typing import TextIO

GATEWAY_HANG_DUMP_FILENAME = "gateway_hang_dump.log"
# 触发网关 GC 停顿诊断日志的耗时阈值。
GC_PAUSE_LOG_SECONDS = 1.0
_PROC_ROOT = Path("/proc")
_STATUS_FIELDS = frozenset({"State", "VmRSS", "RssAnon", "RssFile", "VmSwap", "Threads", "SigCgt"})
_handle: TextIO | None = None
_installed_root: Path | None = None
_gc_started: float | None = None
_registered_signal: int | None = None
_lifecycle_lock = threading.RLock()
_NATIVE_DUMP_LATER = getattr(faulthandler, "dump_traceback_later", None)
_NATIVE_CANCEL_LATER = getattr(faulthandler, "cancel_dump_traceback_later", None)
# 延迟升级到原生C看门狗的最小卡死观察窗口（秒）。
_DEFERRED_C_WATCHDOG_PROMOTE_SECONDS = 1.0
_pending_deadline: float | None = None
_pending_promote_at = 0.0
_pending_timeout = 0.0
_pending_generation = 0
_c_watchdog_active = False
_stall_promoter: _StallPromoter | None = None


# LLM: 两个函数身份必须同时保持导入时原件，任何替换均回原同步路径，不能让原有诊断替身漏调用。
# 函数用途: 区分生产C接口与测试/嵌入方的替代实现。
def _is_native_faulthandler() -> bool:
    return (getattr(faulthandler, "dump_traceback_later", None) is _NATIVE_DUMP_LATER
            and getattr(faulthandler, "cancel_dump_traceback_later", None) is _NATIVE_CANCEL_LATER)


# LLM: 调用方持_lifecycle_lock；使已经取到旧deadline的晋升失效，不唤醒仍在有限等待的线程。
# 函数用途: 仅清内存中的待晋升看门狗，不操作C线程或文件。
def _clear_pending_watchdog() -> None:
    global _pending_deadline, _pending_promote_at, _pending_timeout, _pending_generation
    _pending_deadline = None
    _pending_promote_at = _pending_timeout = 0.0
    _pending_generation += 1


# LLM: 与arm/disarm/close共用同一RLock；仅代次仍有效且句柄打开时借fd，失败仍沿原取消安全门。
# 函数用途: 真正卡住一段时间后，为剩余期限启用一次C全线程导出。
def _promote_stall_watchdog(generation: int) -> None:
    global _pending_deadline, _c_watchdog_active
    if generation != _pending_generation or _pending_deadline is None:
        return
    deadline, _pending_deadline = _pending_deadline, None
    if _handle is None or _handle.closed:
        return
    _c_watchdog_active = True
    try:
        faulthandler.dump_traceback_later(max(1e-6, deadline - time.monotonic()),
            repeat=False, file=_handle, exit=False)
    except (OSError, OverflowError, RuntimeError, ValueError):
        _cancel_watchdog()


# LLM: 每进程惰性创建一个daemon；无轮询/逐tick Timer，Condition与生命周期共锁，取消后最多原等待窗口再睡眠。
# 类用途: 仅在派发持续超过观察窗口时升级C看门狗，避免快速tick反复创建和join原生线程。
class _StallPromoter(threading.Thread):
    # LLM: 不在构造器启动，调用方须持生命周期锁；idle只在真正无限等待时为真。
    # 函数用途: 建立一个可跨多轮安装复用的诊断线程。
    def __init__(self) -> None:
        super().__init__(name="gateway-stall-promoter", daemon=True)
        self.condition = threading.Condition(_lifecycle_lock)
        self.idle = False

    # LLM: 不持锁睡眠；唤醒前已取消也保留合并窗口，不立即重返逐tick可通知状态；同锁复核代次与fd。
    # 函数用途: 阻塞等待待办，仅在真实晋升时调用C定时器。
    def run(self) -> None:
        with self.condition:
            while True:
                while _pending_deadline is None:
                    self.idle = True
                    self.condition.wait()
                    self.idle = False
                    if _pending_deadline is None:
                        self.condition.wait(_DEFERRED_C_WATCHDOG_PROMOTE_SECONDS)
                remaining = _pending_promote_at - time.monotonic()
                if remaining > 0:
                    self.condition.wait(remaining)
                    continue
                _promote_stall_watchdog(_pending_generation)


# LLM: 只在arm所持生命周期锁内调用；已有有限等待者不逐tick唤醒，原等待点早于重武装的新晋升点。
# 函数用途: 记录长期限并惰性启动或唤醒唯一晋升线程。
def _defer_stall_watchdog(stall_seconds: float) -> None:
    global _pending_deadline, _pending_promote_at, _pending_timeout, _stall_promoter
    if _c_watchdog_active and not _cancel_watchdog():
        return
    now = time.monotonic()
    _pending_deadline = now + stall_seconds
    _pending_promote_at = now + _DEFERRED_C_WATCHDOG_PROMOTE_SECONDS
    _pending_timeout = stall_seconds
    if _stall_promoter is None or not _stall_promoter.is_alive():
        _stall_promoter = _StallPromoter()
        try:
            _stall_promoter.start()
        except RuntimeError:
            _clear_pending_watchdog()
            return
    if _stall_promoter.idle:
        _stall_promoter.condition.notify()


# LLM: 仅非阻塞打开指定root内固定日志；追加0600且拒绝叶子符号链接/硬链接和非常规文件，失败不触碰进程。
# 函数用途: 返回UTF-8私有追加句柄，避免线程栈暴露或覆写别的文件。
def _open_dump(gateway_root: Path) -> TextIO:
    gateway_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = (os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    descriptor = os.open(gateway_root / GATEWAY_HANG_DUMP_FILENAME, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("diagnostic log must be a private regular file")
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        return os.fdopen(descriptor, "a", encoding="utf-8")
    except BaseException:
        os.close(descriptor)
        raise


# LLM: 默认重装先撤旧C引用；无信号的同根生产安装幂等，不能清掉派发线程先挂的watchdog；不改变原信号注册语义。
# 函数用途: 安装持久诊断，允许不抢占原SIGUSR1注册，重复安装不泄漏资源。
def install(gateway_root: Path, *, register_signal: bool = True) -> bool:
    global _handle, _registered_signal, _installed_root
    with _lifecycle_lock:
        root = Path(gateway_root)
        if (not register_signal and _handle is not None and not _handle.closed
                and _installed_root == root and _registered_signal is None):
            return True
        close()
        if _handle is not None:
            return False  # 旧C引用尚未成功撤销，不能换fd。
        try:
            _handle = _open_dump(root)
            _installed_root = root
        except (OSError, ValueError):
            return False
        gc.callbacks.append(_gc_probe)
        if not register_signal:
            return True
        signum = getattr(signal, "SIGUSR1", None) if os.name == "posix" else None
        if signum is None:
            return False
        try:
            faulthandler.register(signum, file=_handle, all_threads=True, chain=False)
        except (OSError, RuntimeError, ValueError):
            return False
        _registered_signal = int(signum)
        return True


# LLM: 同锁下仅在确有C借用或同步替身路径调用；成功才清active，失败保留fd借用事实，调用方不得共享C定时器。
# 函数用途: 安全取消一次性看门狗，并报告是否已解除对文件描述符的借用。
def _cancel_watchdog() -> bool:
    global _c_watchdog_active
    try:
        faulthandler.cancel_dump_traceback_later()
    except (OSError, RuntimeError, ValueError):
        return False
    _c_watchdog_active = False
    return True


# LLM: 与close/晋升共锁；原生长tick只登记内存，短期限/替身仍原样同步；失败只撤本模块计时器，不退出进程。
# 函数用途: 为派发设置一次性导出期限，正常快速tick不再创建C线程。
def arm_stall_watchdog(stall_seconds: float) -> None:
    global _c_watchdog_active
    with _lifecycle_lock:
        if _handle is None:
            return
        _clear_pending_watchdog()
        if not math.isfinite(stall_seconds) or stall_seconds <= 0:
            if _c_watchdog_active or not _is_native_faulthandler():
                _cancel_watchdog()
            return
        if _is_native_faulthandler() and stall_seconds >= _DEFERRED_C_WATCHDOG_PROMOTE_SECONDS:
            _defer_stall_watchdog(stall_seconds)
            return
        _c_watchdog_active = True
        try:
            faulthandler.dump_traceback_later(stall_seconds, repeat=False, file=_handle, exit=False)
        except (OSError, OverflowError, RuntimeError, ValueError):
            _cancel_watchdog()


# LLM: 与arm/close/晋升同锁；先使代次失效，仅真正借过C句柄或原同步替身路径才cancel。
# 函数用途: 快速tick仅清内存；确已晋升的tick撤销C看门狗。
def disarm_stall_watchdog() -> None:
    with _lifecycle_lock:
        _clear_pending_watchdog()
        if _handle is not None and (_c_watchdog_active or not _is_native_faulthandler()):
            _cancel_watchdog()


# LLM: 仅序列化调用方的结构化白名单事实，flush先于发信号；混合日志里的栈由faulthandler自行追加。
# 函数用途: 追加一条可即时读取的JSON记录。
def _write_record(handle: TextIO, record: dict[str, object]) -> None:
    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    handle.flush()


# LLM: GC回调不等生命周期锁，避免stop-the-world期间等待其他线程；关闭交错只丢本条诊断，不向GC抛异常。
# 函数用途: 仅记录有配对start且达到阈值的gen2暂停，0时刻也是有效起点。
def _gc_probe(phase: str, info: dict) -> None:
    global _gc_started
    if info.get("generation") != 2:
        return
    if phase == "start":
        _gc_started = time.monotonic()
        return
    if phase != "stop" or _gc_started is None:
        return
    elapsed, _gc_started = time.monotonic() - _gc_started, None
    handle = _handle
    if elapsed >= GC_PAUSE_LOG_SECONDS and handle is not None:
        try:
            _write_record(handle, {"event": "gc_gen2_pause", "at": time.time(),
                "seconds": round(elapsed, 3), "collected": info.get("collected", 0)})
        except (OSError, ValueError):
            pass


# LLM: 同锁清pending代次并移GC回调，再撤真实C借用/同步替身与信号；取消失败保留fd，常驻线程空闲阻塞供重装复用。
# 函数用途: 幂等释放诊断资源；不关闭仍被C借用的fd，不改其他信号或业务状态。
def close() -> None:
    global _handle, _gc_started, _registered_signal, _installed_root
    with _lifecycle_lock:
        _clear_pending_watchdog()
        while _gc_probe in gc.callbacks:
            gc.callbacks.remove(_gc_probe)
        _gc_started = None
        if _handle is None:
            return
        if (_c_watchdog_active or not _is_native_faulthandler()) and not _cancel_watchdog():
            return
        if _registered_signal is not None:
            try:
                faulthandler.unregister(_registered_signal)
            except (OSError, RuntimeError, ValueError):
                return
            _registered_signal = None
        handle, _handle = _handle, None
        _installed_root = None
        try:
            handle.close()
        except (OSError, ValueError):
            pass


# LLM: 只读内核公开诊断文件；退出、权限或解码异常返回空值，不探测其他路径或读取cmdline/environ。
# 函数用途: 尽力读取状态/等待点文本，pressure只取首行。
def _proc_text(path: Path, *, first_line: bool = False) -> str:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            return handle.readline().strip() if first_line else handle.read().strip()
    except OSError:
        return ""


# LLM: 状态只允许七个结构化字段，原文本和Name等额外信息不进入日志。
# 函数用途: 从指定PID的status提取内存、状态、线程数和捕获信号位。
def _proc_status(pid: int) -> dict[str, str]:
    fields = {}
    for line in _proc_text(_PROC_ROOT / str(pid) / "status").splitlines():
        key, separator, value = line.partition(":")
        if separator and key in _STATUS_FIELDS:
            fields[key] = value.strip()
    return fields


# LLM: 线程可在遍历期间退出；只读数字tid的wchan并汇总前10项，不保留线程正文和完整清单。
# 函数用途: 输出有界的高频内核等待点。
def _wchan_top(pid: int) -> list[dict[str, object]]:
    try:
        tasks = tuple((_PROC_ROOT / str(pid) / "task").iterdir())
    except OSError:
        return []
    counts = Counter(value for task in tasks if task.name.isdecimal()
                     if (value := _proc_text(task / "wchan")))
    return [{"wchan": name, "count": count} for name, count in counts.most_common(10)]


# LLM: 只读取固定memory/io首行；缺失和权限不足不构成主机压力为零的证明。
# 函数用途: 取得可用的内存与IO压力观测。
def _pressure_facts() -> dict[str, str]:
    return {kind: value for kind in ("memory", "io")
            if (value := _proc_text(_PROC_ROOT / "pressure" / kind, first_line=True))}


# LLM: 只有可解析的非负内核SigCgt位可许可SIGUSR1；平台名、PID存活和本模块曾安装都不能代替内核证据。
# 函数用途: 拒绝向未捕获该信号的旧进程发送默认会终止它的SIGUSR1。
def _caught_sigusr1(status: dict[str, str]) -> int | None:
    signum = getattr(signal, "SIGUSR1", None) if os.name == "posix" else None
    try:
        mask = int(status.get("SigCgt", ""), 16)
    except (ValueError, TypeError):
        return None
    return int(signum) if signum is not None and mask >= 0 and (mask >> (int(signum) - 1)) & 1 else None


# LLM: 只为正整数PID读/proc；先flush再请求栈。显式禁信号的自身采集直接同步dump到本次私有fd，不借内核位猜自身handler。
# 函数用途: 保全结构化现场和线程栈；无/proc仍可记录本进程栈，不改变远端安全信号门。
def capture_gateway_hang_facts(paths_or_root: object, pid: int, *, stage: str = "before_term", send_sigusr1: bool = True) -> dict[str, object]:
    if type(pid) is not int or pid <= 0:
        raise ValueError("hang capture requires a positive integer PID")
    root = Path(paths_or_root if isinstance(paths_or_root, (str, os.PathLike)) else getattr(paths_or_root, "root", paths_or_root))
    status = _proc_status(pid)
    facts: dict[str, object] = {"event": "supervisor_hang_capture", "stage": stage, "pid": pid,
        "at": time.time(), "status": status, "wchan_top": _wchan_top(pid) if status else [],
        "pressure": _pressure_facts() if status else {}}
    try:
        with _open_dump(root) as handle:
            _write_record(handle, facts)
            if pid == os.getpid() and not send_sigusr1:
                faulthandler.dump_traceback(file=handle, all_threads=True)
    except (OSError, ValueError) as error:
        return {**facts, "sigusr1_sent": False, "write_error": type(error).__name__}
    facts["sigusr1_sent"] = False
    signum = _caught_sigusr1(status) if send_sigusr1 else None
    if signum is not None:
        try:
            os.kill(pid, signum)
        except OSError as error:
            facts["sigusr1_error"] = type(error).__name__
        else:
            facts["sigusr1_sent"] = True
    return facts
