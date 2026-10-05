# LLM: Shell 后台语法判断的唯一入口；只解释外层与可识别 Shell 的字面 -c 程序，不分析任意脚本或展开变量，进程回收仍由执行层负责。
#   字面程序提取与 heredoc 剔除已下沉到 contracts/gates/shell_source（唯一提取器），本模块只做后台操作符判断。
# 模块用途: 阻止命令用 Shell 后台操作符绕开受管会话，同时保留引用文本、重定向和 heredoc 数据；联测 shell background 与 r223 regressions。
from __future__ import annotations

from ..contracts.gates.command_policy import analyze_command
from ..contracts.gates.shell_source import (
    shell_command_source as _shell_command_source,
)
from ..contracts.gates.shell_source import (
    shell_syntax_without_heredoc_bodies as _shell_syntax_without_heredoc_bodies,
)

_LITERAL_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})


# LLM: 只展开既有命令分析器识别的 Shell 命令位置，不能把 echo/Python 参数当程序；无 IO、无权限或状态变更。
# 函数用途: 检查外层和逐层字面 Shell 程序是否包含不受管理的后台启动。
def contains_unmanaged_background_operator(command: str) -> bool:
    pending = [str(command or "")]
    while pending:
        syntax = _shell_syntax_without_heredoc_bodies(pending.pop())
        if _outer_background_operator(syntax):
            return True
        for segment in analyze_command(syntax).segments:
            if segment.executable in _LITERAL_SHELLS:
                source = _shell_command_source(segment.argv[1:])
                if source is not None:
                    pending.append(source)
    return False


# LLM: 这里只扫描一层真实语法的独立 &；嵌套 Shell 由唯一公开入口展开，字符串和 fd 重定向仍是数据。
# 函数用途: 保留引号、转义和注释信息，避免把字符串内的 & 或文件描述符重定向当成后台启动。
def _outer_background_operator(syntax: str) -> bool:
    quote, index = "", 0
    while index < len(syntax):
        char = syntax[index]
        if char == "\\" and quote != "'":
            index += 2
            continue
        if quote:
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            index += 1
            continue
        if char == "#" and (index == 0 or syntax[index - 1].isspace()):
            newline = syntax.find("\n", index)
            index = len(syntax) if newline < 0 else newline + 1
            continue
        if char == "&":
            previous = syntax[index - 1] if index else ""
            following = syntax[index + 1] if index + 1 < len(syntax) else ""
            if following == "&":
                index += 2
                continue
            if previous not in {"<", ">", "|"} and following != ">":
                return True
        index += 1
    return False
