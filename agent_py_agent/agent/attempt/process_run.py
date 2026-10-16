# LLM: attempt 同步执行共用进程回收、每流有界捕获与显式 cwd 传递；复用 Shell 的采集器，不含能力包或目录授权裁决。
#   回调与超时串行且幂等；退出前撤销回调、等在途清理完成，不能让旧取消控制后续命令。联测 cancellation 与 shell_stdin。
# 模块用途: 有界捕获沙箱批处理输出、响应取消并回收本次进程组，不接管其它宿主进程。
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..common.cancellation import (
    cancellation_requested,
    raise_if_cancelled,
    register_cancellation_callback,
)
from ..tooling.process_output_capture import ProcessOutputCapture

# 取消可能仅来自外部结构化检查（没有 token.cancel 回调），每 0.1 秒复核，不能等到整个命令超时。
_CANCELLATION_POLL_SECONDS = 0.1
# 回收后仅有界排空管道；脱组进程仍持有描述符时，不无限等 EOF。
_PIPE_DRAIN_SECONDS = 1.0
# TERM 后的宽限期里每 0.05 秒看一次组是否已全部退出；退了就不再等满宽限（取消回调在 /stop 线程里同步执行）。
_GROUP_EXIT_POLL_SECONDS = 0.05


# LLM: Popen 由 start_new_session 创建，pid 即已冻结组号；不能取消时再 getpgid（组长可能已被 wait 回收）。
#   lock 只串行本次命令的 TERM/KILL 与关闭；不跨任务、不查询全局进程表。
# 类用途: 让超时和取消竞争时只回收一次，并在返回前等这次回收完成。
class _ProcessRun:
    # LLM: 不启动进程或 reader，仅保存 Popen/宽限和本次 capture 槽；关闭标志防已撤销回调再发信号。
    # 函数用途: 为刚启动的独立进程组建立一次性回收句柄。
    def __init__(self, proc: subprocess.Popen, grace_seconds: float):
        self.proc = proc
        self.grace_seconds = grace_seconds
        self.capture: ProcessOutputCapture | None = None
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


# LLM: 只承载已裁决的启动/等待及宿主凭据来源上界；cwd=None 保留命名空间的 --chdir，不重新授权目录。
# 类用途: 将同步命令的期限、回收宽限、捕获与工作目录打包，避免接口参数超过四个。
@dataclass(frozen=True)
class SandboxProcessOptions:
    timeout: float
    grace_seconds: float
    capture_output: bool
    cwd: Path | None
    host_credential_env_names: frozenset[str] = field(default_factory=frozenset, repr=False)


# LLM: argv/cwd 已由唯一沙箱入口校验，Popen 必须显式接收 cwd，不继承一个白名单外的宿主目录；不在此裁决权限。
#   取消前后都检查；二进制管道仅由通用采集器读取，不能再 communicate；DEVNULL、独立会话及 143 合同保持不变。
#   环境沿主线统一擦洗，宿主 SecretRef 来源不进入命令；不增加新的启动或登记入口。
# 函数用途: 启动一次非交互命令，注册取消回收并等待终态；取消抛共用 ToolCancelled，超时仍返回 143。
def run_sandbox_process(argv: list[str], options: SandboxProcessOptions) -> subprocess.CompletedProcess:
    from ..tooling._subprocess_env_scrub import scrub_subprocess_env

    raise_if_cancelled()
    proc = subprocess.Popen(argv, cwd=options.cwd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE if options.capture_output else None,
                            stderr=subprocess.PIPE if options.capture_output else None,
                            start_new_session=True,
                            env=scrub_subprocess_env(dict(os.environ), host_credential_env_names=options.host_credential_env_names))
    state = _ProcessRun(proc, options.grace_seconds)
    try:
        if options.capture_output:
            state.capture = ProcessOutputCapture(proc)
        with register_cancellation_callback(state.stop):
            return _wait_for_process(state, argv, options.timeout)
    except BaseException:
        state.stop()
        _drain_stopped_process(state)
        raise
    finally:
        state.close()
        if state.capture is not None:
            state.capture.finish(0)
        _close_process_pipes(proc)


# LLM: 每次短 wait 不改变总 timeout；输出线程同时排空。只有组长退出且管道结束才返回，取消仍优先于结果。
# 函数用途: 用单调期限等待命令，分别交回正常结果、超时结果或取消异常。
def _wait_for_process(state: _ProcessRun, argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        _raise_if_run_cancelled(state)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _timeout_result(state, argv)
        if _wait_slice(state, min(remaining, _CANCELLATION_POLL_SECONDS)):
            _raise_if_run_cancelled(state)
            return _completed_result(state, argv, state.proc.returncode)


# LLM: 无回调 external_check 也先幂等回收组；统一取消异常由 run 外层捕获，完成有界排空/reap 后才交回调用方。
# 函数用途: 检查当前 run 是否已取消，确保命令被回收后再退出等待。
def _raise_if_run_cancelled(state: _ProcessRun) -> None:
    if cancellation_requested():
        state.stop()
        raise_if_cancelled()


# LLM: TimeoutExpired 只代表观察片未完成；管道 reader 是唯一读取者，wait 不读取或无限累积输出。
# 函数用途: 有界等待组长与管道末尾，未完成时留给外层继续判断总期限和取消。
def _wait_slice(state: _ProcessRun, seconds: float) -> bool:
    try:
        state.proc.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        return False
    if state.capture is not None and not state.capture.done():
        time.sleep(min(0.01, seconds))
        return False
    return True


# LLM: 保持 CompletedProcess 的 argv/退出码/字符串形状及通用换行；capture 字段与 Shell 同源，逐流字节数不按解码后的字符算。
# 函数用途: 将有限输出和结构化截断事实交给同步入口调用方；不捕获时 stdout/stderr 仍为 None。
def _completed_result(state: _ProcessRun, argv: list[str], code: int) -> subprocess.CompletedProcess:
    stdout, stderr, facts = state.capture.result() if state.capture is not None else (None, None, {})
    stdout = _text_newlines(stdout)
    stderr = _text_newlines(stderr)
    completed = subprocess.CompletedProcess(argv, code, stdout, stderr)
    completed.capture = facts
    return completed


# LLM: 采集器沿 Shell 的 UTF-8 replacement 解码；这里只恢复同步 text=True 原有的通用换行，不影响原始字节计数。
# 函数用途: 把同步命令输出的 CRLF/CR 换成换行；未捕获时保留 None。
def _text_newlines(text: str | None) -> str | None:
    return text.replace("\r\n", "\n").replace("\r", "\n") if text is not None else None


# LLM: 保持超时 143 与原 stderr 回收说明；有限 stdout 和捕获事实仍返回，取消交错时不能被降为超时。
# 函数用途: 到总期限后回收完整组，并返回带回收说明的超时结果。
def _timeout_result(state: _ProcessRun, argv: list[str]) -> subprocess.CompletedProcess:
    state.stop()
    _drain_stopped_process(state)
    raise_if_cancelled()
    completed = _completed_result(state, argv, 143)
    completed.stderr = f"timeout after TERM->grace({state.grace_seconds}s)->KILL"
    return completed


# LLM: 组回收后管道仍未 EOF，可能是脱组后代持有描述符，也可能是组信号被拒、组长还活着。沿用改造前的合同：补杀组长、
#   有界 reap，不向调用方抛 TimeoutExpired（超时路径仍返回 143）；组长被 SIGKILL 后仍 reap 不到时交给 Popen 自身回收。
# 函数用途: 有界等待组长退出并排空有限输出；管道后代不退出也不会无限等待。
def _drain_stopped_process(state: _ProcessRun) -> None:
    try:
        state.proc.wait(timeout=_PIPE_DRAIN_SECONDS)
    except subprocess.TimeoutExpired:
        state.proc.kill()
        _reap_killed_process(state.proc)
    if state.capture is not None:
        state.capture.finish(_PIPE_DRAIN_SECONDS)


# LLM: 只 reap 本次组长，SIGKILL 后依然未退出时不无限等待；保持原有超时兜底合同，不吞其它等待错误。
# 函数用途: 在补杀组长后给操作系统有限的回收时间。
def _reap_killed_process(proc: subprocess.Popen) -> None:
    try:
        proc.wait(timeout=_PIPE_DRAIN_SECONDS)
    except subprocess.TimeoutExpired:
        pass


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
