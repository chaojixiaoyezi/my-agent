
from __future__ import annotations

"""后台进程注册表 —— 让模型能管住 run_command(run_in_background=true) 起的后台进程。

学标杆 长期助手 tools/process_registry.py 的 ProcessSession + ProcessRegistry 设计
(输出滚动缓冲、状态轮询 poll()、按 ID 杀进程组、列表),裁剪适配 my-agent:

适配差异(为什么不照搬 长期助手):
  - my-agent 的 run_command 后台路径已经把 stdout/stderr 落到 .background_jobs/*.log
    日志文件(handle 写盘),不像 长期助手 用 stdout=PIPE + 后台 reader 线程实时读。
    所以这里"滚动输出缓冲"= 查询时惰性读日志文件尾部,不另起常驻 reader 线程
    (零额外线程,CI 友好,不引入 reader 卡死/孤儿线程那一类坑)。
  - 状态更新惰性化:不起后台轮询线程,而是在 list/status/kill 被调用时用
    Popen.poll() 收割退出码并落状态。后台进程独立会话启动(start_new_session),
    父进程 Popen 句柄仍在内存里,poll() 可直接拿退出码;父进程重启后句柄丢失则
    退化为按 pid 存活探测(状态可知、退出码不可知,与 长期助手 detached 语义一致)。
  - 不做 watch_patterns / 通知队列 / 崩溃恢复 checkpoint(那些是 长期助手 网关常驻
    场景的能力,my-agent 当前后台机制是进程内单实例,先覆盖"登记/查/杀"最小完整集)。

杀进程组(避免杀父留子):后台 Popen 用 start_new_session=True 建独立进程组
(POSIX setsid),kill 时对整个进程组发信号(os.killpg(os.getpgid(pid))),
SIGTERM 宽限后再 SIGKILL;Windows 用 taskkill /T /F 杀进程树。
"""

import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_IS_WINDOWS = os.name == "nt"

# 杀进程时 SIGTERM 到 SIGKILL 的宽限秒数。先礼(SIGTERM 让进程自己清理)后兵
# (还活着就 SIGKILL 硬杀整组),避免留下孤儿子进程。
_KILL_GRACE_SECONDS = 3.0
# 滚动输出缓冲:查询时从日志文件尾部最多读这么多字符,够看进度/结论又不撑爆 prompt。
_OUTPUT_TAIL_CHARS = 4000
# 最多保留多少条已结束的进程记录,超了按启动时间淘汰最老的(防内存无界增长)。
_MAX_FINISHED = 128


@dataclass
class BackgroundProcess:
    """一个被登记的后台进程。对标 长期助手 ProcessSession,裁剪到 my-agent 够用的字段。"""

    session_id: str
    command: str
    pid: int
    started_at: float
    cwd: str = ""
    output_file: str = ""
    process: subprocess.Popen | None = None  # 父进程 Popen 句柄(同进程内才有)
    status: str = "running"  # running / exited / killed
    exit_code: int | None = None
    finished_at: float | None = None

    def is_terminal(self) -> bool:
        return self.status in {"exited", "killed"}

    def to_summary(self, *, include_output: bool = False, output_tail_chars: int = _OUTPUT_TAIL_CHARS) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "session_id": self.session_id,
            "pid": self.pid,
            "command": self.command[:200],
            "status": self.status,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.started_at)),
            "uptime_seconds": int((self.finished_at or time.time()) - self.started_at),
            "output_file": self.output_file,
        }
        if self.exit_code is not None:
            summary["exit_code"] = self.exit_code
        if include_output:
            summary["output_tail"] = _read_log_tail(self.output_file, output_tail_chars)
        return summary


def _read_log_tail(output_file: str, max_chars: int) -> str:
    """惰性读日志文件尾部作为滚动输出缓冲。日志读不到不报错(返回空串),
    因为进程已启动是事实,日志缺失只影响观测不影响管控。"""
    if not output_file or max_chars <= 0:
        return ""
    try:
        path = Path(output_file)
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > max_chars:
                handle.seek(size - max_chars)
            raw = handle.read()
    except OSError:
        return ""
    text = raw.decode("utf-8", errors="replace")
    if size > max_chars:
        # 从中间截断的,丢掉第一行残片避免半个字符/半行误导。
        newline = text.find("\n")
        if newline != -1:
            text = text[newline + 1 :]
        text = f"...[日志已截断,只显示尾部 {len(text)} 字符]...\n{text}"
    return text


class ProcessRegistry:
    """进程内单实例后台进程注册表。线程安全(run_command 与查询/杀工具可能并发)。"""

    def __init__(self) -> None:
        self._processes: dict[str, BackgroundProcess] = {}
        self._lock = threading.Lock()
        self._counter = 0

    def _next_session_id(self) -> str:
        # 进程内单调自增 + 时间戳后缀,人可读且不和历史会话撞;不用 uuid 省得日志里一长串。
        self._counter += 1
        return f"bg-{self._counter}-{int(time.time())}"

    def register(
        self,
        *,
        command: str,
        pid: int,
        output_file: str,
        process: subprocess.Popen | None = None,
        cwd: str = "",
        session_id: str | None = None,
    ) -> BackgroundProcess:
        """登记一个刚启动的后台进程,返回其记录(含 session_id 供模型后续查/杀)。"""
        with self._lock:
            sid = session_id or self._next_session_id()
            record = BackgroundProcess(
                session_id=sid,
                command=command,
                pid=pid,
                started_at=time.time(),
                cwd=cwd,
                output_file=output_file,
                process=process,
            )
            self._processes[sid] = record
            self._prune_finished_locked()
            return record

    def get(self, session_id: str) -> BackgroundProcess | None:
        with self._lock:
            return self._processes.get(session_id)

    def _refresh_locked(self, record: BackgroundProcess) -> None:
        """惰性更新单条记录状态:用 Popen.poll() 收退出码,句柄丢失则按 pid 存活探测。
        必须在持锁下调用。"""
        if record.is_terminal():
            return
        proc = record.process
        if proc is not None:
            return_code = proc.poll()
            if return_code is not None:
                record.status = "exited"
                record.exit_code = return_code
                record.finished_at = time.time()
            return
        # 没有 Popen 句柄(理论上当前不会发生,父进程内登记一定带句柄;留作健壮性)
        # —— 按 pid 存活探测,死了只能标 exited,退出码不可知。
        if not _pid_alive(record.pid):
            record.status = "exited"
            record.exit_code = None
            record.finished_at = time.time()

    def status(self, session_id: str) -> dict[str, Any] | None:
        """查单个进程状态 + 最近输出(日志尾部)。不存在返回 None。"""
        with self._lock:
            record = self._processes.get(session_id)
            if record is None:
                return None
            self._refresh_locked(record)
            return record.to_summary(include_output=True)

    def list(self) -> list[dict[str, Any]]:
        """列出所有登记的后台进程 + 状态(运行中的会先惰性刷新)。"""
        with self._lock:
            records = list(self._processes.values())
            for record in records:
                self._refresh_locked(record)
            # 运行中的排前面,其次按启动时间倒序(新的在前)。
            records.sort(key=lambda item: (item.is_terminal(), -item.started_at))
            return [record.to_summary(include_output=False) for record in records]

    def kill(self, session_id: str) -> dict[str, Any] | None:
        """SIGTERM→(宽限超时)SIGKILL 杀进程组,更新状态。不存在返回 None;
        已结束返回 already_exited。"""
        with self._lock:
            record = self._processes.get(session_id)
            if record is None:
                return None
            self._refresh_locked(record)
            if record.is_terminal():
                return {
                    "session_id": record.session_id,
                    "status": "already_exited" if record.status == "exited" else record.status,
                    "exit_code": record.exit_code,
                    "message": "进程已经结束,无需终止。",
                }
            pid = record.pid
            proc = record.process

        # 真正杀进程在锁外做(killpg + 宽限 + wait 可能耗时,不长占锁阻塞 list/status)。
        killed_signal = _terminate_process_tree(pid, proc)

        with self._lock:
            record = self._processes.get(session_id)
            if record is not None:
                record.status = "killed"
                record.finished_at = time.time()
                if record.process is not None:
                    code = record.process.poll()
                    record.exit_code = code if code is not None else -signal.SIGTERM
                else:
                    record.exit_code = -signal.SIGTERM
                summary = record.to_summary(include_output=False)
                summary["signal"] = killed_signal
                summary["message"] = "已向进程组发送终止信号。"
                return summary
        return None

    def _prune_finished_locked(self) -> None:
        """已结束记录超上限时淘汰最老的。必须持锁调用。"""
        finished = [(sid, rec) for sid, rec in self._processes.items() if rec.is_terminal()]
        overflow = len(finished) - _MAX_FINISHED
        if overflow <= 0:
            return
        finished.sort(key=lambda item: item[1].finished_at or item[1].started_at)
        for sid, _ in finished[:overflow]:
            self._processes.pop(sid, None)

    def clear(self) -> None:
        """清空注册表(测试隔离用;不杀进程,只丢记录)。"""
        with self._lock:
            self._processes.clear()
            self._counter = 0


def _pid_alive(pid: int) -> bool:
    """跨平台进程存活探测。pid<=0 视为无效。"""
    if not pid or pid <= 0:
        return False
    if _IS_WINDOWS:
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return str(pid) in (out.stdout or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但无权限发信号,仍算存活
    except OSError:
        return False
    return True


def _terminate_process_tree(pid: int, proc: subprocess.Popen | None) -> str:
    """杀进程组/进程树。返回最终用到的终止方式描述。

    POSIX:后台进程用 start_new_session 起,自成进程组(pgid==pid)。对进程组发
      SIGTERM(让组内所有进程,含子孙,有机会清理),宽限 _KILL_GRACE_SECONDS 后
      若进程还活着再发 SIGKILL 硬杀整组 —— 避免杀了父留下孤儿子进程。
    Windows:taskkill /T /F 杀整棵进程树(/T 含子进程,/F 强制)。
    """
    if pid <= 0:
        return "noop"
    if _IS_WINDOWS:
        return _terminate_windows_tree(pid, proc)

    # POSIX:优先按进程组发信号(拿不到组就退化到单进程)。
    if not _posix_signal(pid, signal.SIGTERM):
        return "already_gone"

    # 宽限等待直接子进程退出;还活着则 SIGKILL 硬杀整组。
    if _wait_process_gone(pid, proc, _KILL_GRACE_SECONDS):
        return "SIGTERM"
    _posix_signal(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    if proc is not None:
        try:
            proc.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
    return "SIGTERM->SIGKILL"


def _terminate_windows_tree(pid: int, proc: subprocess.Popen | None) -> str:
    """Windows:taskkill /T /F 杀整棵进程树(/T 含子进程,/F 强制);失败退到 Popen.kill。"""
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return "taskkill/T/F"
    except (OSError, subprocess.SubprocessError):
        _safe_popen_kill(proc)
        return "popen.kill"


def _safe_popen_kill(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        proc.kill()
    except OSError:
        pass


def _posix_signal(pid: int, sig: int) -> bool:
    """对 pid 所在进程组发信号;组拿不到时退化到单进程。进程已不存在返回 False。"""
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        pgid = pid
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        pass
    try:
        os.kill(pid, sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _wait_process_gone(pid: int, proc: subprocess.Popen | None, grace_seconds: float) -> bool:
    """在宽限期内等待进程退出。有 Popen 句柄按 poll(),否则按 pid 存活探测。"""
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if proc is not None:
            if proc.poll() is not None:
                return True
        elif not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return False


# 进程内单实例。run_command 后台路径登记到这里,list/status/kill 工具从这里读。
process_registry = ProcessRegistry()


__all__ = ["BackgroundProcess", "ProcessRegistry", "process_registry"]
