# LLM: attempt 同步执行共用进程回收与显式 cwd 传递，不含能力包业务或目录授权判定；TERM→宽限→KILL 只作用于本次新建会话的组。
#   回调与超时串行且幂等；退出前撤销回调、等在途清理完成，不能让旧取消控制后续命令。联测 cancellation 与 shell_stdin。
# 模块用途: 等待沙箱批处理命令时响应取消，并回收本次命令的完整进程组，不接管其它宿主进程。
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from ..common.cancellation import (
    cancellation_requested,
    raise_if_cancelled,
    register_cancellation_callback,
)

# 取消可能仅来自外部结构化检查（没有 token.cancel 回调），每 0.1 秒复核，不能等到整个命令超时。
_CANCELLATION_POLL_SECONDS = 0.1
# 回收后仅有界排空管道；脱组进程仍持有描述符时，不无限等 EOF。
_PIPE_DRAIN_SECONDS = 1.0
# TERM 后的宽限期里每 0.05 秒看一次组是否已全部退出；退了就不再等满宽限（取消回调在 /stop 线程里同步执行）。
_GROUP_EXIT_POLL_SECONDS = 0.05


# LLM: Popen 由 start_new_session 创建，pid 即已冻结组号；不能取消时再 getpgid（组长可能已被 communicate 回收）。
#   lock 只串行本次命令的 TERM/KILL 与关闭；不跨任务、不查询全局进程表。
# 类用途: 让超时和取消竞争时只回收一次，并在返回前等这次回收完成。
class _ProcessRun:
    # LLM: 不启动进程，仅保存本次 Popen 和宽限；关闭标志防止已经撤销的旧回调再发信号。
    # 函数用途: 为刚启动的独立进程组建立一次性回收句柄。
    def __init__(self, proc: subprocess.Popen, grace_seconds: float):
        self.proc = proc
        self.grace_seconds = grace_seconds
        self._lock = threading.Lock()
        self._stopped = False
        self._closed = False

    # LLM: stop 可由取消线程或等待线程调用，必须幂等；整个 TERM/宽限/KILL 持锁，close 因此能等待在途回收。
    # 函数用途: 回收本次命令组，避免超时和 /stop 重复杀组或只杀组长。
    def stop(self) -> None:
        with self._lock:
            if self._closed or self._stopped:
                return
            self._stopped = True
            _terminate_group(self.proc, self.grace_seconds)

    # LLM: 退出等待范围后关闭句柄；cancel 已复制出来的迟到回调也必须成为无操作。
    # 函数用途: 等在途回收完成并禁止旧回调继续发信号。
    def close(self) -> None:
        with self._lock:
            self._closed = True


# LLM: 只承载已经过沙箱入口裁决的启动/等待参数；cwd=None 保留命名空间的 --chdir，不在此补根或猜 cwd。
# 类用途: 将同步命令的期限、回收宽限、捕获与工作目录打包，避免接口参数超过四个。
@dataclass(frozen=True)
class SandboxProcessOptions:
    timeout: float
    grace_seconds: float
    capture_output: bool
    cwd: Path | None


# LLM: argv/cwd 已由唯一沙箱入口校验，Popen 必须显式接收 cwd，不继承一个白名单外的宿主目录；不在此裁决权限。
#   取消前后都检查，DEVNULL stdin、独立会话、回调撤销及 143 超时合同保持不变。
# 函数用途: 启动一次非交互命令，注册取消回收并等待终态；取消抛共用 ToolCancelled，超时仍返回 143。
def run_sandbox_process(argv: list[str], options: SandboxProcessOptions) -> subprocess.CompletedProcess:
    raise_if_cancelled()
    proc = subprocess.Popen(argv, cwd=options.cwd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE if options.capture_output else None,
                            stderr=subprocess.PIPE if options.capture_output else None,
                            text=options.capture_output, start_new_session=True)
    state = _ProcessRun(proc, options.grace_seconds)
    try:
        with register_cancellation_callback(state.stop):
            return _wait_for_process(state, argv, options.timeout)
    finally:
        state.close()
        _close_process_pipes(proc)


# LLM: 每次短 communicate 不改变总 timeout；外部取消无回调时也能停止，取消优先于超时或已捕获的成功输出。
# 函数用途: 用单调期限等待命令，分别交回正常结果、超时结果或取消异常。
def _wait_for_process(state: _ProcessRun, argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        _raise_if_run_cancelled(state)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _timeout_result(state, argv)
        output = _communicate_slice(state.proc, min(remaining, _CANCELLATION_POLL_SECONDS))
        if output is not None:
            _raise_if_run_cancelled(state)
            return subprocess.CompletedProcess(argv, state.proc.returncode, *output)


# LLM: 无回调的 external_check 也走同一次幂等组回收；回收后排空且 reap 组长，再传播统一取消异常。
# 函数用途: 检查当前 run 是否已取消，确保命令被回收后再退出等待。
def _raise_if_run_cancelled(state: _ProcessRun) -> None:
    if cancellation_requested():
        state.stop()
        _drain_stopped_process(state.proc)
        raise_if_cancelled()


# LLM: TimeoutExpired 只代表本次观察片未完成，不代表总超时；其它错误不吞掉。
# 函数用途: 有界排空一次 stdout/stderr，未完成时交给等待循环继续判断取消和总期限。
def _communicate_slice(proc: subprocess.Popen, seconds: float) -> tuple | None:
    try:
        return proc.communicate(timeout=seconds)
    except subprocess.TimeoutExpired:
        return None


# LLM: 保持原沙箱同步 run 的超时 143 合同；取消与超时交错时不能被降为普通超时。
# 函数用途: 到总期限后回收完整组，并返回带回收说明的超时结果。
def _timeout_result(state: _ProcessRun, argv: list[str]) -> subprocess.CompletedProcess:
    state.stop()
    output = _drain_stopped_process(state.proc)
    raise_if_cancelled()
    return subprocess.CompletedProcess(argv, 143, output[0], f"timeout after TERM->grace({state.grace_seconds}s)->KILL")


# LLM: 组回收后管道仍未 EOF，可能是脱组后代持有描述符，也可能是组信号被拒、组长还活着。沿用改造前的合同：补杀组长、
#   有界 reap，不向调用方抛 TimeoutExpired（超时路径仍返回 143）；组长被 SIGKILL 后仍 reap 不到时交给 Popen 自身回收。
# 函数用途: 有界收集回收后的输出并结束本次组长，不等待脱组进程的管道。
def _drain_stopped_process(proc: subprocess.Popen) -> tuple:
    output = _communicate_slice(proc, _PIPE_DRAIN_SECONDS)
    if output is not None:
        return output
    proc.kill()
    try:
        proc.wait(timeout=_PIPE_DRAIN_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    return (None, None)


# LLM: 只关闭本次 Popen 创建的管道，stdin 是 DEVNULL；即使取消抛异常也不把管道引用留给后续命令。
# 函数用途: 释放本次捕获输出的两个文件描述符。
def _close_process_pipes(proc: subprocess.Popen) -> None:
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
            pipe.close()


# LLM: 组号是 Popen(start_new_session=True) 时冻结的 pid；宽限期内轮询组是否已退出，组里还有忽略 TERM 的后代时等满宽限再 KILL，
#   不能漏掉持管道的后代。9b 复核要求：进程收到 TERM 就退出时，cancel() 不能被宽限期拖住。
# 函数用途: 对本次命令组发送 TERM，组提前退出就返回，宽限到期仍有成员才发送 KILL。
def _terminate_group(proc: subprocess.Popen, grace_seconds: float) -> None:
    group = proc.pid
    if not _signal_group(group, signal.SIGTERM):
        return
    if not _group_exited_within(proc, group, grace_seconds):
        _signal_group(group, signal.SIGKILL)


# LLM: 每轮先 poll 一次 reap 已退出的组长（僵尸组长仍算组成员，killpg(group, 0) 会一直成功），再用信号 0 探测整组。
# 函数用途: 在宽限期内等本次命令组全部退出，退出了返回 True，到期仍有成员返回 False。
def _group_exited_within(proc: subprocess.Popen, group: int, grace_seconds: float) -> bool:
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while True:
        proc.poll()
        if not _signal_group(group, 0):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_GROUP_EXIT_POLL_SECONDS, remaining))


# LLM: 只发指定组信号（sig=0 只探测）。组不存在视为已经退出；权限被拒沿用改造前的合同：不抛给调用方、不再继续发组信号，
#   由 _drain_stopped_process 补杀组长兜住，脱组后代的写边界仍由平台沙箱保护。macOS 实测：组里只剩没 reap 的僵尸组长时
#   killpg（含信号 0）返回 EPERM 而不是 ESRCH，命令恰好自己退出时取消/超时就会撞上，所以这里不能把 PermissionError 往外抛。
# 函数用途: 向本次命令组发一个回收信号，返回是否真正发出。
def _signal_group(group: int, sig: int) -> bool:
    try:
        os.killpg(group, sig)
    except (ProcessLookupError, PermissionError):
        return False
    return True
