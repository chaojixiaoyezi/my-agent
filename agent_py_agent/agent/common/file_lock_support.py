"""无 fcntl 平台(Windows)文件锁降级的一次性告警——把静默 pass 变成显式可见。"""

from __future__ import annotations

import logging
import threading

_warned = False
_guard = threading.Lock()


def warn_file_lock_unavailable_once() -> None:
    """fcntl 不可用(Windows 等)时跨进程文件锁降级为仅线程锁——告警一次,不再静默。

    同进程多线程仍由 threading.Lock 串行化;但多进程并发 append 同一文件(审计/记录/
    守护元数据)会失去 OS 级排他,可能撕行。多进程部署在这类平台需改用 msvcrt/portalocker。"""
    global _warned
    with _guard:
        if _warned:
            return
        _warned = True
    logging.getLogger("agent.concurrency").warning(
        "fcntl 不可用(可能是 Windows):跨进程文件锁降级为仅线程锁——同进程并发写安全,"
        "但多进程并发写同一文件可能撕行;多进程部署请改用 msvcrt/portalocker。"
    )
