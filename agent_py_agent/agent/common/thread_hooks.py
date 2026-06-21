"""后台线程未捕获异常可见化(审计 #19 之 excepthook 部分):守护线程静默死掉 → 落日志可告警。

全仓 30+ threading.Thread(supervisor / worker / 心跳 / reaper / 唤醒器)。Python 默认
threading.excepthook 只把未捕获异常打到 stderr,生产 daemon 里 stderr 常被吞 → 后台线程死了、
任务不再被消费,却毫无信号,违背"永不停机"。注册 hook 把后台线程未捕获异常落 logging(可被
日志聚合/告警消费)。学 代理运行时/nanoclaw 的 process.on('uncaughtException') 兜底。

注:这是 #19(可观测/成本)里能独立落地的一小块;LLM/工具热路径 metrics+span 与 USD 计价预算
是更大的改动,留专项。
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger("agent.threads")
_installed = False


def install_thread_excepthook() -> None:
    """注册 threading.excepthook,把后台线程未捕获异常落日志(幂等,进程启动时调一次即可)。"""
    global _installed
    if _installed:
        return
    threading.excepthook = _thread_excepthook
    _installed = True


def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
    if args.exc_type is SystemExit:
        return  # 线程正常 SystemExit 退出,不当异常告警
    thread = args.thread
    name = thread.name if thread is not None else "unknown"
    logger.error(
        "后台线程 '%s' 未捕获异常退出: %s: %s",
        name,
        args.exc_type.__name__ if args.exc_type else "?",
        args.exc_value,
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )
