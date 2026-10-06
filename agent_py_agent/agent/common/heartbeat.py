
from __future__ import annotations

"""通用进程心跳 + 存活检测,供常驻进程(daemon/gateway/采集器)崩溃可检测、可自愈。

核心洞察:仅靠"PID 是否存在"判活会被 PID 复用骗——老进程死了,系统把同号分配给新进程,
误判"还活着"。这里把"进程启动时刻"一起当指纹:PID 在 且 start_time 匹配,才算"还是当初那个进程"。
"""

# LLM: 进程身份必须联合 PID 与启动时间判断；调用方不能把普通心跳年龄当成进程退出证据。
# 模块用途: 为后台服务保存心跳与精确进程身份，避免 PID 复用导致错误判活。

import math
import os
import subprocess
import time


def _read_proc_starttime(proc_stat: str) -> str | None:
    with open(proc_stat, encoding="utf-8", errors="ignore") as fh:
        after = fh.read().rsplit(")", 1)[-1].split()  # comm 可能含空格/括号,从最后一个 ) 切
    return after[19] if len(after) > 19 else None  # ) 之后第 20 个 = stat 第 22 域 starttime


# LLM: ps 的返回码是"能否核验"的一部分：非零返回码、超时、空输出一律返回 None（不可核验），
#   不得把带错误输出的调用当成有效指纹。
# 函数用途: 用 ps 读进程启动时刻（macOS 等无 /proc 平台）；读不到返回 None。
def _read_ps_starttime(pid: int) -> str | None:
    try:
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
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


# LLM: 启动指纹三态比较的唯一实现（3a 10-05 收口，ds6 初审发现旧实现在“数字 vs macOS lstart”时判死）：
#   旧落盘记录可能是 /proc ticks 数字或数字字符串，新记录是 process_start_time 的字符串。
#   same：两侧同格式且相等（数字按数值比，“12345.0”与“12345”相同）；different：两侧同格式、都可读且确证不同；
#   unverifiable：任一侧缺失/空串/非有限数字，或格式不可比（数字 vs 字符串指纹）。调用方只能凭 different 判死。
# 函数用途: 比较记录的启动指纹与当前读取，返回 "same" / "different" / "unverifiable"。
def start_time_relation(recorded: object, current: object) -> str:
    left, right = _start_fingerprint(recorded), _start_fingerprint(current)
    if left is None or right is None or left[0] != right[0]:
        return "unverifiable"
    return "same" if left[1] == right[1] else "different"


# LLM: 只做格式归一，不读进程；布尔、空串、NaN/inf、未知类型一律 None（不可核验），不能当成可比较的指纹。
# 函数用途: 把启动指纹归一成（种类, 值）：数字统一为 float，其余字符串去首尾空白。
def _start_fingerprint(value: object) -> tuple[str, object] | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return ("number", number) if math.isfinite(number) else None
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        number = float(text)
    except ValueError:
        return ("text", text)
    return ("number", number) if math.isfinite(number) else None


# LLM: 启动指纹的形态校验，与 start_time_relation 共用 _start_fingerprint 的归一规则：有限数字、数字串或非空文本为合法；
#   None、布尔、空串、NaN/inf（含字符串 "NaN"）、其它类型都不合法。只看形态，不读进程；是否允许“读不到”（None）由调用方决定。
# 函数用途: 判断一个值能否当作启动指纹参与比较（供执行器记录在破坏性操作前校验）。
def start_fingerprint_is_valid(value: object) -> bool:
    return _start_fingerprint(value) is not None


# LLM: 布尔口径的兼容入口：只有确证不同才返回 False，其余（相同或不可核验）返回 True，调用方不得据此误杀活进程。
# 函数用途: 判断记录中的启动指纹与当前读取是否视为同一进程（不可核验按同一进程处理）。
def start_time_matches(recorded: object, current: str | None) -> bool:
    return start_time_relation(recorded, current) != "different"


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
