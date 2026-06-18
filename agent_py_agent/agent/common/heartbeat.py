
from __future__ import annotations

"""通用进程心跳 + 存活检测,供常驻进程(daemon/gateway/采集器)崩溃可检测、可自愈。

对照五项目最优解:
- 工具运行时 flock(心跳文件 mtime + 60s stale 检测 + .breaker 竞争清理)
- 通道运行时 gateway-lock(PID + port + cmdline 三重活性检测,30s 老化夺锁)
- 会话运行时 pidfile(/proc start_time 防 PID 复用误判"活")

核心洞察:仅靠"PID 是否存在"判活会被 PID 复用骗——老进程死了,系统把同号分配给新进程,
误判"还活着"。这里把"进程启动时刻"一起当指纹:PID 在 且 start_time 匹配,才算"还是当初那个进程"。
"""

import os
import subprocess
import time


def _read_proc_starttime(proc_stat: str) -> str | None:
    with open(proc_stat, "r", encoding="utf-8", errors="ignore") as fh:
        after = fh.read().rsplit(")", 1)[-1].split()  # comm 可能含空格/括号,从最后一个 ) 切
    return after[19] if len(after) > 19 else None  # ) 之后第 20 个 = stat 第 22 域 starttime


def _read_ps_starttime(pid: int) -> str | None:
    out = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=5
    )
    return out.stdout.strip() or None


def process_start_time(pid: int) -> str | None:
    """读进程启动时刻指纹(字符串,只要同进程稳定可比即可,不要求跨平台统一格式)。
    Linux 读 /proc/<pid>/stat 的 starttime 域;其它(macOS 等)退回 ps -o lstart。取不到返回 None。"""
    if pid <= 0:
        return None
    proc_stat = f"/proc/{pid}/stat"
    if os.path.exists(proc_stat):
        try:
            return _read_proc_starttime(proc_stat)
        except OSError:
            return None
    try:
        return _read_ps_starttime(pid)
    except (OSError, subprocess.SubprocessError):
        return None


def process_alive(pid: int, *, start_time: str | None = None) -> bool:
    """PID 是否对应活进程;若给了 start_time,还要求当前启动指纹与之匹配(防 PID 复用)。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)  # 不发信号只探活:ESRCH=无此进程,EPERM=存在但无权(仍算活)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    if start_time is None:
        return True
    current = process_start_time(pid)
    return current is None or current == start_time  # 取不到指纹时不否决(保守判活,避免误杀)


def heartbeat_record(pid: int | None = None) -> dict:
    """生成一条心跳记录:pid + 启动指纹 + 时间戳。常驻进程周期写出,看门狗读入判活。"""
    pid = pid if pid is not None else os.getpid()
    return {"pid": pid, "start_time": process_start_time(pid), "heartbeat_at": time.time()}


def is_dead(record: dict, *, now: float, max_age: float) -> bool:
    """据心跳记录判进程是否已死(看门狗据此决定重启):进程不在/指纹不符,或心跳超过 max_age 没刷新。"""
    pid = int(record.get("pid", 0) or 0)
    if not process_alive(pid, start_time=record.get("start_time")):
        return True
    last = float(record.get("heartbeat_at", 0) or 0)
    return (now - last) > max_age
