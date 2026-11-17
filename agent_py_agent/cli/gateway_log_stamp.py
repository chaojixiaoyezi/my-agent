"""Millisecond ISO-8601 timestamps for every line the gateway daemon writes to gateway.log."""
# 模块用途: 后台 Gateway 的 stdout/stderr 逐行加本地时区毫秒时间前缀，并为 agent_py_agent 日志装统一脱敏格式器。
from __future__ import annotations

import io
import logging
import sys
import threading
from datetime import datetime

from ..agent.common.log_redaction import RedactingFormatter

_handler_name = "my-agent-gateway-daemon-stderr"


# LLM: 只读本机时钟；格式固定为带时区偏移的ISO-8601毫秒，供跨日志按时间窗grep。
# 函数用途: 返回当前本地时间的毫秒级ISO-8601字符串。
def iso_timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


class TimestampedLineWriter(io.TextIOBase):
    """Text stream that stamps the start of every line and forwards to the original stream."""

    # LLM: 只包装原流对象，不复制fd；轮转dup2后原流仍写同一fd号，flush/fileno一律委托原流。
    # 函数用途: 记录被包装的原流和行首状态。
    def __init__(self, inner):
        super().__init__()
        self._inner = inner
        self._lock = threading.Lock()
        self._at_line_start = True

    # LLM: 同一次write内按行切分，行首才加前缀；锁内整段写出，保证并发线程的前缀不插进别人行中间。
    # 函数用途: 写入文本并为每个新行加时间前缀，返回原文本长度。
    def write(self, text):
        if not isinstance(text, str):
            raise TypeError(f"write() argument must be str, not {type(text).__name__}")
        if not text:
            return 0
        with self._lock:
            stamp = iso_timestamp() + " "
            parts = []
            for piece in text.splitlines(keepends=True):
                if self._at_line_start:
                    parts.append(stamp)
                parts.append(piece)
                self._at_line_start = piece.endswith(("\n", "\r"))
            self._inner.write("".join(parts))
        return len(text)

    # LLM: 委托原流，保持print(flush=True)与轮转前flush的原语义。
    # 函数用途: 刷新原流。
    def flush(self):
        self._inner.flush()

    # LLM: faulthandler与dup2按fd工作，必须拿到原流真实fd。
    # 函数用途: 返回原流文件描述符。
    def fileno(self):
        return self._inner.fileno()

    # LLM: 只读委托，不改变原流能力判断。
    # 函数用途: 报告原流是否为终端。
    def isatty(self):
        return self._inner.isatty()

    # LLM: 文本流协议要求可写；原流决定真实能力。
    # 函数用途: 声明本流可写。
    def writable(self):
        return True

    # LLM: 只读委托原流编码，供依赖encoding属性的调用方使用。
    # 函数用途: 返回原流编码。
    @property
    def encoding(self):
        return getattr(self._inner, "encoding", "utf-8")

    # LLM: 只读委托原流错误策略。
    # 函数用途: 返回原流编码错误处理方式。
    @property
    def errors(self):
        return getattr(self._inner, "errors", "strict")


# LLM: 仅后台守护进程（启动方设置了日志路径环境）才安装；重复调用幂等，不重复包流或叠加handler，前台/测试进程输出不变。
# 函数用途: 为后台Gateway安装逐行时间前缀与脱敏日志handler，返回是否已生效。
def install_gateway_log_timestamps(daemon_log_path: str | None) -> bool:
    if not daemon_log_path:
        return False
    if not isinstance(sys.stdout, TimestampedLineWriter):
        sys.stdout = TimestampedLineWriter(sys.stdout)
    if not isinstance(sys.stderr, TimestampedLineWriter):
        sys.stderr = TimestampedLineWriter(sys.stderr)
    logger = logging.getLogger("agent_py_agent")
    if not any(handler.get_name() == _handler_name for handler in logger.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.set_name(_handler_name)
        handler.setFormatter(RedactingFormatter("%(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
    return True


__all__ = ["TimestampedLineWriter", "install_gateway_log_timestamps", "iso_timestamp"]
