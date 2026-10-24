# LLM: 仅显式install后注册进程级faulthandler/GC探针；本模块独占dump_traceback_later，调用方负责每拍重置及退出close。
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


# LLM: 只有本模块有打开句柄时才操作进程级C定时器；调用方不得另行共享同一faulthandler定时器。
# 函数用途: 安全取消一次性看门狗，并报告是否已解除对文件描述符的借用。
def _cancel_watchdog() -> bool:
    try:
        faulthandler.cancel_dump_traceback_later()
    except (OSError, RuntimeError, ValueError):
        return False
    return True


# LLM: 与close同锁保护C层借用fd；只包在途dispatch tick，不在心跳或idle拍续期；失败撤旧timer，绝不退出进程。
# 函数用途: 为本次派发调用设置一次性全线程导出期限。
def arm_stall_watchdog(stall_seconds: float) -> None:
    with _lifecycle_lock:
        if _handle is None:
            return
        if not math.isfinite(stall_seconds) or stall_seconds <= 0:
            _cancel_watchdog()
            return
        try:
            faulthandler.dump_traceback_later(stall_seconds, repeat=False, file=_handle, exit=False)
        except (OSError, OverflowError, RuntimeError, ValueError):
            _cancel_watchdog()


# LLM: 与arm/close同锁且只有持有句柄才取消，不能影响未安装诊断的调用进程。
# 函数用途: 派发tick的finally撤销一次性看门狗。
def disarm_stall_watchdog() -> None:
    with _lifecycle_lock:
        if _handle is not None:
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


# LLM: 必须先取消C定时器/注销自身信号再关fd；撤销失败保留句柄供重试，不能让旧C引用写入复用后的fd。
# 函数用途: 幂等关闭诊断并移除全部自身GC回调，不改其他信号和业务状态。
def close() -> None:
    global _handle, _gc_started, _registered_signal, _installed_root
    with _lifecycle_lock:
        while _gc_probe in gc.callbacks:
            gc.callbacks.remove(_gc_probe)
        _gc_started = None
        if _handle is None or not _cancel_watchdog():
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
