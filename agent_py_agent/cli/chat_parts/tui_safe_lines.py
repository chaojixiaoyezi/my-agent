# LLM: 已净化行是仅用于渲染的不可变值；构造器始终检查正文，外部事件/普通 tuple 不能自报可信。
# 模块用途: 让稳定块复用已检查的终端文字，避免每帧扫描整段历史；样式和鼠标回调保持原语义。
from __future__ import annotations

from .tui_markdown import FormattedLine, sanitize_terminal_text


# LLM: tuple 的不可变性保证检查后正文不会变化；新建任何实例都必须经过相同终端字符过滤。
# 类用途: 标记已经检查过的显示行，重复通过最终安全出口时直接复用，不持有全局正文缓存。
class SafeFormattedLine(tuple):
    __slots__ = ()

    # LLM: 仅修改 fragment 的正文，原样保留样式及 mouse handler；无 I/O、状态或外部副作用。
    # 函数用途: 检查并冻结一行；已有安全行不再分配相同内容或重新扫描字符。
    def __new__(cls, line: FormattedLine):
        if isinstance(line, cls):
            return line
        return super().__new__(cls, (
            (fragment[0], sanitize_terminal_text(fragment[1]), *fragment[2:])
            for fragment in line
        ))
