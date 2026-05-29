# LLM: shell_delete_policy validates explicit delete targets for workspace shell access.
# 模块用途: 在 workspace-write 模式下只防止 rm/rmdir/unlink 删到工作区外，不限制普通命令执行。

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

from agent_py_agent.agent.contracts.gates.command_policy import command_name
from agent_py_agent.agent.path_access_policy import PathAccessPolicy


# LLM: DeleteCommandCheck bundles one rm/rmdir/unlink target scan.
# 类用途: 保存删除命令扫描所需 argv、位置、cwd、roots 和访问策略，避免宽参数 helper。
@dataclass(frozen=True)
class DeleteCommandCheck:
    argv: list[str]
    position: int
    cwd: Path
    roots: list[Path]
    path_access_policy: PathAccessPolicy
    access_mode: str


@dataclass(frozen=True)
class DeleteAccessRequest:
    command: str
    cwd: Path
    roots: list[Path]
    access_mode: str
    path_access_policy: PathAccessPolicy | None = None


def delete_target_access_error(request: DeleteAccessRequest) -> str:
    command = request.command
    access_mode = request.access_mode
    if _normalize_access_mode(access_mode) == "full-access":
        return ""
    tokens = _shell_tokens(command)
    for command_index in _delete_command_indexes(tokens):
        error = _delete_command_error(
            DeleteCommandCheck(
                argv=tokens,
                position=command_index,
                cwd=request.cwd,
                roots=request.roots,
                path_access_policy=request.path_access_policy or PathAccessPolicy.from_values(),
                access_mode=_normalize_access_mode(access_mode),
            )
        )
        if error:
            return error
    return ""


def _delete_command_indexes(argv: list[str]) -> list[int]:
    return [
        position
        for position, token in enumerate(argv)
        if command_name(token) in {"rm", "rmdir", "unlink"}
    ]


def _delete_command_error(check: DeleteCommandCheck) -> str:
    for raw_target in _delete_targets(check.argv[check.position + 1 :]):
        error = _delete_target_error(raw_target, check)
        if error:
            return error
    return ""


def _delete_target_error(raw_target: str, check: DeleteCommandCheck) -> str:
    resolved = _delete_target_path(raw_target, check.cwd)
    if _path_inside_any_root(resolved, check.roots):
        return ""
    if check.access_mode != "workspace-write":
        return f"COMMAND_ACCESS_DENIED: delete target outside workspace roots: {raw_target}"
    decision = check.path_access_policy.check(resolved)
    return "" if decision.allowed else f"COMMAND_ACCESS_DENIED: {decision.message}"


def _delete_target_path(raw_target: str, cwd: Path) -> Path:
    target = Path(raw_target).expanduser()
    return target.resolve(strict=False) if target.is_absolute() else (cwd / target).resolve(strict=False)


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


__all__ = ["DeleteAccessRequest", "delete_target_access_error"]
