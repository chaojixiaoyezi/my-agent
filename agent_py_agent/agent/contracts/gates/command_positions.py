# LLM: command_positions resolves shell wrapper commands before command policy checks.
# 模块用途: 解析 sudo/env/timeout/nice 等包装器后的真实命令位置，避免灾难命令绕过策略。

from __future__ import annotations

from pathlib import Path

_COMMAND_WRAPPERS = frozenset({"sudo", "command", "exec", "builtin", "nohup", "time"})


def command_name(value: object) -> str:
    return Path(str(value or "")).name.lower()


def effective_command_positions(argv: tuple[str, ...]) -> list[int]:
    positions: list[int] = []
    for position in command_positions(argv):
        effective = _unwrap_command_position(argv, position)
        if effective not in positions:
            positions.append(effective)
    return positions


def command_positions(argv: tuple[str, ...]) -> list[int]:
    positions: list[int] = []
    expect_command = True
    for index, token in enumerate(argv):
        if is_shell_operator_token(token):
            expect_command = True
            continue
        if expect_command and looks_like_assignment(token):
            continue
        if expect_command:
            positions.append(index)
            expect_command = False
    return positions


def is_shell_operator_token(token: str) -> bool:
    return bool(token) and set(token).issubset({"&", "|", ";", ">", "<"})


def looks_like_assignment(token: str) -> bool:
    name, separator, _value = token.partition("=")
    return bool(separator and name.replace("_", "").isalnum() and not name[0].isdigit())


def _unwrap_command_position(argv: tuple[str, ...], position: int) -> int:
    current = position
    while current < len(argv):
        executable = command_name(argv[current])
        if executable in _COMMAND_WRAPPERS:
            current = _skip_simple_wrapper(argv, current, executable)
            continue
        if executable == "env":
            current = _skip_env_wrapper(argv, current)
            continue
        if executable == "timeout":
            current = _skip_timeout_wrapper(argv, current)
            continue
        if executable == "nice":
            current = _skip_nice_wrapper(argv, current)
            continue
        return current
    return position


def _skip_simple_wrapper(argv: tuple[str, ...], current: int, executable: str) -> int:
    current += 1
    if executable == "sudo":
        while current < len(argv) and argv[current].startswith("-"):
            current += 1
    return current


def _skip_env_wrapper(argv: tuple[str, ...], current: int) -> int:
    current += 1
    while current < len(argv) and (argv[current].startswith("-") or looks_like_assignment(argv[current])):
        current += 1
    return current


def _skip_timeout_wrapper(argv: tuple[str, ...], current: int) -> int:
    current += 1
    while current < len(argv) and argv[current].startswith("-"):
        current += 1
    return current + 1 if current < len(argv) else current


def _skip_nice_wrapper(argv: tuple[str, ...], current: int) -> int:
    current += 1
    if current < len(argv) and argv[current] == "-n":
        return current + 2
    if current < len(argv) and argv[current].startswith("-"):
        return current + 1
    return current


__all__ = [
    "command_name",
    "command_positions",
    "effective_command_positions",
    "is_shell_operator_token",
    "looks_like_assignment",
]
