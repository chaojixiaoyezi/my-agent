# LLM: Shell body-read detection extracts product-path reads from run_command without owning policy.
# 模块用途: 识别 `cat/tail/head/sed/rg file` 这类 shell 正文读取，让委托期 guard 能复用统一策略。

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

_SHELL_BODY_READ_COMMANDS = {"awk", "cat", "grep", "head", "less", "more", "nl", "rg", "sed", "tail"}
_SHELL_BODY_READ_WRAPPERS = {"bash", "sh", "zsh"}
_SHELL_PATH_HINT_RE = re.compile(
    r"(^|/)(artifacts?|build|deliverables|dist|output|outputs)/|"
    r"[\w.-]+\.(?:html|css|js|mjs|cjs|ts|tsx|jsx|py|md|json|yaml|yml|txt|csv|vue|svelte)$",
    re.IGNORECASE,
)


# LLM: ShellBodyReadPathRequest bundles command text with the root needed for relative paths.
# 类用途: 保存 shell 命令和 workspace root，避免 shell 解析模块依赖完整 agent 对象。
@dataclass(frozen=True)
class ShellBodyReadPathRequest:
    command: str
    root: str


# LLM: shell_body_read_paths returns candidate product paths read by shell body commands.
# 函数用途: 从 run_command 文本里提取可能被 cat/tail/head/sed/rg 读取的业务文件路径。
def shell_body_read_paths(request: ShellBodyReadPathRequest) -> list[Path]:
    command = str(request.command or "").strip()
    if not command:
        return []
    return _shell_command_read_paths(command, root=request.root, depth=0)


# LLM: _shell_command_read_paths handles simple shell wrappers without becoming a full shell parser.
# 函数用途: 识别 `bash -lc "tail file"` 和直接 `tail file` 两类常见正文读取命令。
def _shell_command_read_paths(command: str, *, root: str, depth: int) -> list[Path]:
    if depth > 2:
        return []
    segments = _shell_segments(command)
    if len(segments) > 1:
        return _shell_chain_read_paths(segments, root=root, depth=depth)
    tokens = _shell_tokens(command)
    if not tokens:
        return []
    command_name = Path(tokens[0]).name
    wrapped = _wrapped_shell_command(tokens, command_name)
    if wrapped:
        return _shell_command_read_paths(wrapped, root=root, depth=depth + 1)
    if command_name not in _SHELL_BODY_READ_COMMANDS:
        return []
    return [_path_from_shell_token(raw, root=root) for raw in _shell_path_candidates(tokens)]


# LLM: _shell_chain_read_paths carries simple cd context across shell command segments.
# 函数用途: 识别 `cd deliverables && grep index.html` 这类常见绕读形式，同时不执行 shell。
def _shell_chain_read_paths(segments: list[str], *, root: str, depth: int) -> list[Path]:
    cwd = str(root or ".")
    paths: list[Path] = []
    for segment in segments:
        tokens = _shell_tokens(segment)
        if not tokens:
            continue
        command_name = Path(tokens[0]).name
        if command_name == "cd":
            cwd = _shell_cd_root(tokens, cwd)
            continue
        paths.extend(_shell_command_read_paths(segment, root=cwd, depth=depth + 1))
    return paths


# LLM: _shell_segments splits common shell chains without trying to be a full shell interpreter.
# 函数用途: 按未引用的 &&、||、;、换行切分命令段，让 guard 能继续分析后续读文件命令。
def _shell_segments(command: str) -> list[str]:
    try:
        return _shell_segments_with_shlex(command)
    except ValueError:
        return _shell_segments_fallback(command)


# LLM: _shell_segments_with_shlex avoids a hand-rolled nested shell scanner for common chains.
# 函数用途: 用 shlex 保留未引用的 &&、||、;、换行分隔效果，并把每段重新拼成可再次解析的命令。
def _shell_segments_with_shlex(command: str) -> list[str]:
    lexer = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    lexer.commenters = ""
    segments: list[str] = []
    current: list[str] = []
    for token in lexer:
        if token in {"&&", "||", ";"}:
            _append_shell_segment(segments, current)
            current = []
            continue
        current.append(token)
    _append_shell_segment(segments, current)
    return segments


# LLM: _shell_segments_fallback keeps malformed shell text non-fatal for the guard.
# 函数用途: shlex 因引号不完整失败时，按最保守的普通分隔符切分。
def _shell_segments_fallback(command: str) -> list[str]:
    raw_segments = re.split(r"\s*(?:&&|\|\||;|\n)\s*", command)
    return [segment.strip() for segment in raw_segments if segment.strip()]


# LLM: _append_shell_segment keeps empty command separators from producing fake reads.
# 函数用途: 标准化命令段切分结果，去掉空白段。
def _append_shell_segment(segments: list[str], current: list[str]) -> None:
    segment = " ".join(current).strip()
    if segment:
        segments.append(segment)


# LLM: _shell_tokens tolerates malformed model shell text and keeps guard failure non-fatal.
# 函数用途: 用 shlex 拆命令；模型输出引号不完整时回退空格拆分，避免 guard 自己崩溃。
def _shell_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


# LLM: _wrapped_shell_command extracts inner commands from sh/bash/zsh -c/-lc invocations.
# 函数用途: 让 shell 包装命令仍受同一正文读取保护，不需要在工具层新增特殊分支。
def _wrapped_shell_command(tokens: list[str], command_name: str) -> str:
    if command_name not in _SHELL_BODY_READ_WRAPPERS:
        return ""
    for idx, token in enumerate(tokens[:-1]):
        if token in {"-c", "-lc"}:
            return tokens[idx + 1]
    return ""


# LLM: _shell_cd_root resolves a simple cd target for later relative body-read checks.
# 函数用途: 处理命令链里的工作目录切换；复杂 shell 展开不猜测，失败时保留原 cwd。
def _shell_cd_root(tokens: list[str], root: str) -> str:
    target = _shell_cd_target(tokens)
    if not target:
        return root
    path = Path(target).expanduser()
    if not path.is_absolute():
        path = Path(str(root or ".")).expanduser() / path
    try:
        return str(path.resolve(strict=False))
    except (OSError, RuntimeError):
        return str(path)


# LLM: _shell_cd_target extracts the directory operand while ignoring simple cd flags.
# 函数用途: 从 `cd dir` 或 `cd -- dir` 中取目录；无法确定时返回空字符串。
def _shell_cd_target(tokens: list[str]) -> str:
    for token in tokens[1:]:
        if token == "--":
            continue
        if token.startswith("-"):
            return ""
        return token.strip("'\"")
    return ""


# LLM: _shell_path_candidates keeps command parsing conservative and path-focused.
# 函数用途: 只把像路径或产物文件名的 token 当候选，避免 grep pattern/sed script 被误判为文件。
def _shell_path_candidates(tokens: list[str]) -> list[str]:
    candidates: list[str] = []
    for token in tokens[1:]:
        if not token or token.startswith("-"):
            continue
        cleaned = token.strip("'\"")
        if _SHELL_PATH_HINT_RE.search(cleaned):
            candidates.append(cleaned)
    return candidates


# LLM: _path_from_shell_token normalizes shell path tokens without executing or expanding shell syntax.
# 函数用途: 从 shell token 生成绝对 Path；不处理 glob/变量展开，避免把 guard 变成 shell 解释器。
def _path_from_shell_token(raw: str, *, root: str) -> Path:
    token = str(raw or "").strip().strip("'\"")
    candidate = Path(token).expanduser()
    if not candidate.is_absolute():
        candidate = Path(str(root or ".")).expanduser() / candidate
    try:
        return candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        return candidate
