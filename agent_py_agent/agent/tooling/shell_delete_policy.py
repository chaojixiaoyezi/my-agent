# LLM: shell_delete_policy validates explicit delete targets for workspace shell access.
# 模块用途: 在 workspace-write 模式下只防止 rm/rmdir/unlink 删到工作区外，不限制普通命令执行。

from __future__ import annotations

import shlex
from pathlib import Path

from agent_py_agent.agent.contracts.gates.command_policy import command_name


def delete_target_access_error(command: str, cwd: Path, roots: list[Path], access_mode: str) -> str:
    if _normalize_access_mode(access_mode) == "full-access":
        return ""
    for command_index in _delete_command_indexes(_shell_tokens(command)):
        error = _delete_command_error(_shell_tokens(command), command_index, cwd, roots)
        if error:
            return error
    return ""


def _delete_command_indexes(argv: list[str]) -> list[int]:
    return [
        position
        for position, token in enumerate(argv)
        if command_name(token) in {"rm", "rmdir", "unlink"}
    ]


def _delete_command_error(argv: list[str], position: int, cwd: Path, roots: list[Path]) -> str:
    for raw_target in _delete_targets(argv[position + 1 :]):
        target = Path(raw_target).expanduser()
        resolved = target.resolve(strict=False) if target.is_absolute() else (cwd / target).resolve(strict=False)
        if not _path_inside_any_root(resolved, roots):
            return f"COMMAND_ACCESS_DENIED: delete target outside workspace roots: {raw_target}"
    return ""


def _shell_tokens(command: str) -> list[str]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return []


def _delete_targets(args: list[str]) -> list[str]:
    targets: list[str] = []
    for arg in args:
        if arg and set(arg).issubset({"&", "|", ";", ">", "<"}):
            break
        if arg == "--" or (arg.startswith("-") and arg != "-"):
            continue
        targets.append(arg)
    return targets


def _path_inside_any_root(path: Path, roots: list[Path]) -> bool:
    resolved = path.expanduser().resolve()
    for root in roots:
        try:
            resolved.relative_to(root.expanduser().resolve())
            return True
        except ValueError:
            continue
    return False


def _normalize_access_mode(access_mode: str) -> str:
    return str(access_mode or "").strip().lower().replace("_", "-")


__all__ = ["delete_target_access_error"]
