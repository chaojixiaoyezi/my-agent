"""Authoritative per-invocation workspace selection for registry tools."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from ..common.nofollow_fs import (
    NoFollowPathError,
    _portable_path,
    _same_identity,
    _supports_dir_fd,
    _validate_relative_parts,
    open_readonly_file_beneath,
)


@dataclass(frozen=True, slots=True)
class WorkspaceRef:
    """受理时的目录身份，只定义相对路径起点。"""

    workspace_ref: str
    root: Path
    root_identity: tuple[int, int]
    roots: tuple[Path, ...] = ()
    root_identities: tuple[tuple[int, int], ...] = ()

    def to_record(self) -> dict[str, object]:
        return {"workspace_ref": self.workspace_ref, "root": str(self.root),
                "root_identity": list(self.root_identity), "roots": [str(root) for root in self.roots],
                "root_identities": [list(value) for value in self.root_identities]}

    def is_current(self) -> bool:
        return self.identity_for(self.root) is not None

    def includes_replaced_root(self, path: Path) -> bool:
        return any(path.is_relative_to(root) and self.identity_for(root) is None
                   for root in self.roots if root != self.root)

    def identity_for(self, root: Path) -> tuple[int, int] | None:
        expected = self.root_identity if root == self.root else dict(zip(self.roots, self.root_identities)).get(root)
        if expected is None:
            return None
        try:
            info = root.lstat()
            return expected if (info.st_dev, info.st_ino) == expected else None
        except (OSError, ValueError):
            return None


def _workspace_id(root: Path, identity: tuple[int, int]) -> str:
    return sha256(f"{root}\0{identity[0]}\0{identity[1]}".encode()).hexdigest()


def freeze_workspace_ref(root: object, *, roots=()) -> WorkspaceRef | None:
    if not isinstance(root, (str, Path)) or not str(root).strip():
        return None
    try:
        path = Path(root).expanduser().resolve(strict=True)
        info = path.stat()
        if not path.is_dir():
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    identity = info.st_dev, info.st_ino
    selected = [path]
    identities = [identity]
    for value in roots:
        if not isinstance(value, (str, Path)) or not str(value).strip():
            continue
        try:
            candidate = Path(value).expanduser().resolve(strict=True)
            if candidate.is_dir() and candidate not in selected:
                candidate_info = candidate.stat()
                selected.append(candidate)
                identities.append((candidate_info.st_dev, candidate_info.st_ino))
        except (OSError, RuntimeError, ValueError):
            continue
    return WorkspaceRef(_workspace_id(path, identity), path, identity, tuple(selected), tuple(identities))


def read_workspace_ref(record: object) -> WorkspaceRef | None:
    if not isinstance(record, dict):
        return None
    root, identity = record.get("root"), record.get("root_identity")
    roots = record.get("roots", ())
    stamps = record.get("root_identities", ())
    if (not isinstance(root, str) or not Path(root).is_absolute()
            or not isinstance(identity, (list, tuple)) or len(identity) != 2
            or any(type(value) is not int or value < 0 for value in identity)
            or not isinstance(roots, (list, tuple))
            or any(not isinstance(value, str) or not Path(value).is_absolute() for value in roots)
            or not isinstance(stamps, (tuple, list)) or (stamps and len(stamps) != len(roots))
            or any(not isinstance(stamp, (list, tuple)) or len(stamp) != 2
                   or any(type(value) is not int or value < 0 for value in stamp) for stamp in stamps)):
        return None
    path = Path(root)
    stamped = identity[0], identity[1]
    token = _workspace_id(path, stamped)
    if record.get("workspace_ref") != token:
        return None
    return WorkspaceRef(token, path, stamped, tuple(map(Path, roots)), tuple((stamp[0], stamp[1]) for stamp in stamps))


def workspace_ref_from_boundary(boundary: object) -> WorkspaceRef | None:
    return read_workspace_ref(boundary.get("workspace_ref")) if isinstance(boundary, dict) else None


def open_workspace_delivery_file(root: Path, parts: tuple[str, ...]) -> int:
    """以原受管文件规则打开普通文件，调用者负责关闭描述符。"""
    if _supports_dir_fd():
        return open_readonly_file_beneath(root, parts)
    _validate_relative_parts(parts)
    target = _portable_path(root, parts, create=False)
    before = target.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise NoFollowPathError("managed file is not regular")
    descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    try:
        after = os.fstat(descriptor)
        if not _same_identity(before, after) or not stat.S_ISREG(after.st_mode) or after.st_nlink != 1:
            raise NoFollowPathError("managed file identity changed")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise

# LLM: 会话运行时 keeps the user/project cwd separate from rollout/task state.  A
# hidden task_root is bookkeeping and must never replace the cwd merely because
# a write boundary exists.  Only an explicit host-authored execution_cwd may
# override the registry root; model payloads and goal prose never participate.
# 模块用途: 为一次工具调用选择用户可见的真实工作目录，避免内部任务台账目录冒充项目目录。


# LLM: Keep this resolver independent from task_root/allowed_write_roots.  Those
# fields constrain persistence and writes; they do not define relative-path
# semantics.  Callers that really need another cwd must provide execution_cwd.
# 函数用途: 返回当前工具调用的工作目录；默认沿用启动 Agent 时确定的项目目录。
def effective_registry_cwd(
    workspace_root: Path,
    write_boundary: dict[str, object] | None,
) -> Path:
    """Return the one trusted cwd for this registry invocation."""

    frozen = workspace_ref_from_boundary(write_boundary)
    if frozen is not None:
        return frozen.root
    root = Path(workspace_root).expanduser().resolve(strict=False)
    boundary = write_boundary if isinstance(write_boundary, dict) else {}
    raw_execution_cwd = str(boundary.get("execution_cwd") or "").strip()
    if not raw_execution_cwd:
        return root
    try:
        return Path(raw_execution_cwd).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return root
