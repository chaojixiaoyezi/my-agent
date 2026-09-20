# LLM: Shell 后台语法判断的唯一入口；只解释外层与可识别 Shell 的字面 -c 程序，不分析任意脚本或展开变量，进程回收仍由执行层负责。
# 模块用途: 阻止命令用 Shell 后台操作符绕开受管会话，同时保留引用文本、重定向和 heredoc 数据；联测 shell background 与 r223 regressions。
from __future__ import annotations

import shlex

from ..contracts.gates.command_policy import analyze_command

_LITERAL_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_SHELL_VALUE_OPTIONS = frozenset({"-o", "+o", "-O", "+O", "--rcfile", "--init-file"})


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


# LLM: 仅从 Shell 选项位置读取 -c/组合短选项的后续参数；值选项与 -- 不能误判，动态变量不在这里执行。
# 函数用途: 取得已经作为 Shell 程序传入的字面文本，供同一语法检查逐层处理。
def _shell_command_source(args: tuple[str, ...]) -> str | None:
    index = 0
    while index < len(args):
        option = args[index]
        if option == "--" or not option.startswith(("-", "+")):
            return None
        if option in _SHELL_VALUE_OPTIONS:
            index += 2
            continue
        if option.startswith("-") and not option.startswith("--") and "c" in option[1:]:
            return args[index + 1] if index + 1 < len(args) else None
        index += 1
    return None


# LLM: heredoc 正文属于输入数据；只移除正文，保留开头和后续语法；调用方与 background parser 测试必须保持这个边界。
# 函数用途: 剔除 heredoc 正文，保留真正会由 shell 解释的命令行。
def _shell_syntax_without_heredoc_bodies(command: str) -> str:
    syntax_lines: list[str] = []
    pending: list[tuple[str, bool]] = []
    for line in str(command or "").splitlines():
        if pending:
            delimiter, strip_tabs = pending[0]
            candidate = line.lstrip("\t") if strip_tabs else line
            if candidate == delimiter:
                pending.pop(0)
            continue
        syntax_lines.append(line)
        pending.extend(_heredoc_delimiters_from_shell_line(line))
    return "\n".join(syntax_lines)


# LLM: 仅在语法行上识别有引号的 heredoc 标记；不执行内容，不把正文里的运算符当 Shell 后台控制。
# 函数用途: 读取一条 shell 命令里按出现顺序声明的 heredoc 结束标记。
def _heredoc_delimiters_from_shell_line(line: str) -> list[tuple[str, bool]]:
    try:
        lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = "#"
        tokens = tuple(lexer)
    except ValueError:
        return []
    delimiters: list[tuple[str, bool]] = []
    for index, token in enumerate(tokens[:-1]):
        if token != "<<":
            continue
        delimiter = str(tokens[index + 1] or "")
        strip_tabs = delimiter.startswith("-")
        if strip_tabs:
            delimiter = delimiter[1:]
        if delimiter and delimiter not in {";", "&", "|", "<", ">"}:
            delimiters.append((delimiter, strip_tabs))
    return delimiters


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
