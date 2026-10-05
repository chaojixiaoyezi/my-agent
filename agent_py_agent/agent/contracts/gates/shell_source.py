# LLM: Shell 字面程序的唯一提取器（contracts 层纯函数，无执行、无 IO）：从 argv 里读 -c/组合短选项的后续参数，
#   并剔除 heredoc 正文。tooling/shell_syntax 与 contracts/gates/command_policy 都从这里导入，
#   保证「哪段文本会被 shell 当程序执行」只有一个权威判断（一个概念一个权威位置）。
# 模块用途: 提供 shell -c 程序提取与 heredoc 正文剔除的纯函数，供命令策略与后台语法检查共用。
from __future__ import annotations

import shlex

_SHELL_VALUE_OPTIONS = frozenset({"-o", "+o", "-O", "+O", "--rcfile", "--init-file"})


# LLM: 仅从 Shell 选项位置读取 -c/组合短选项的后续参数；值选项与 -- 不能误判，动态变量不在这里执行。
# 函数用途: 取得已经作为 Shell 程序传入的字面文本，供调用方逐层处理。
def shell_command_source(args: tuple[str, ...]) -> str | None:
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
def shell_syntax_without_heredoc_bodies(command: str) -> str:
    syntax_lines: list[str] = []
    pending: list[tuple[str, bool]] = []
    for line in str(command or "").splitlines():
        if pending:
            _consume_heredoc_line(line, pending)
            continue
        syntax_lines.append(line)
        pending.extend(_heredoc_delimiters_from_shell_line(line))
    return "\n".join(syntax_lines)


# 函数用途: 消费一行 heredoc 正文；命中结束标记时弹出它，正文行不进入语法行。
def _consume_heredoc_line(line: str, pending: list[tuple[str, bool]]) -> None:
    delimiter, strip_tabs = pending[0]
    candidate = line.lstrip("\t") if strip_tabs else line
    if candidate == delimiter:
        pending.pop(0)


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
