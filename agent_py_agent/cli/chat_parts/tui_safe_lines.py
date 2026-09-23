# LLM: 已净化行是仅用于渲染的不可变值；构造器始终检查正文，外部事件/普通 tuple 不能自报可信。
# 模块用途: 让稳定行及不可变行组复用已检查的终端文字，组合/切片不重复扫描正文；样式和鼠标回调保持原语义。
from __future__ import annotations

from itertools import chain

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


# LLM: 只有逐行净化后的 immutable 行组才能成为本类型；拼接和切片只重组已有安全行，不能引入未经检查的正文。
# 类用途: 让整段稳定历史通过最终安全出口时直接复用，新增或被装饰的行组仍逐行检查。
class SafeFormattedLines(tuple):
    __slots__ = ()

    # LLM: 普通 tuple/list 必须经过 SafeFormattedLine；已检查行组只按对象身份复用，无外部副作用。
    # 函数用途: 将行组逐行净化并冻结，已有安全行组不重扫。
    def __new__(cls, lines):
        if isinstance(lines, cls):
            return lines
        return super().__new__(cls, (SafeFormattedLine(line) for line in lines))

    # LLM: 所有输入先经构造器检查；仅在组合已确认安全的行组时走 tuple 原语，不接受外部信任标记。
    # 函数用途: 拼接稳定前缀与新增行组，避免再次过滤旧正文。
    @classmethod
    def join(cls, *groups):
        checked = tuple(cls(group) for group in groups)
        return tuple.__new__(cls, chain.from_iterable(checked))

    # LLM: slice 只返回当前已验证行的子集；索引保持原行对象，不重新解释或改写 fragment。
    # 函数用途: 保留安全行组的切片身份，供稳定前缀截取。
    def __getitem__(self, index):
        result = super().__getitem__(index)
        return tuple.__new__(type(self), result) if isinstance(index, slice) else result
