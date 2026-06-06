"""Shared path access policy for main agents and subagents."""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

PATH_ACCESS_MODE_NORMAL = "normal"
PATH_ACCESS_MODE_FULL = "full"
DEFAULT_PATH_ACCESS_MODE = PATH_ACCESS_MODE_NORMAL
DEFAULT_DANGEROUS_PATH_ROOTS = (
    "/etc",
    "/private/etc",
    "/System",
    "/bin",
    "/sbin",
    "/usr/bin",
    "/usr/sbin",
    "/var/db",
    "/var/root",
    "/root",
    "~/.ssh",
    "~/.gnupg",
    "~/.aws",
    "~/.kube",
    "~/.docker",
)
_VALID_MODES = {PATH_ACCESS_MODE_NORMAL, PATH_ACCESS_MODE_FULL}


@dataclass(frozen=True)
class PathAccessDecision:
    allowed: bool
    code: str = ""
    message: str = ""
    dangerous_root: str = ""


@dataclass(frozen=True)
class PathAccessPolicy:
    mode: str = DEFAULT_PATH_ACCESS_MODE
    dangerous_roots: tuple[Path, ...] = ()

    @classmethod
    def from_config(cls, config: object | None) -> PathAccessPolicy:
        return cls.from_values(
            mode=getattr(config, "path_access_mode", DEFAULT_PATH_ACCESS_MODE),
            dangerous_roots=getattr(config, "path_dangerous_roots", DEFAULT_DANGEROUS_PATH_ROOTS),
        )

    @classmethod
    def from_values(
        cls,
        *,
        mode: object = DEFAULT_PATH_ACCESS_MODE,
        dangerous_roots: Iterable[object] | None = None,
    ) -> PathAccessPolicy:
        normalized_mode = normalize_path_access_mode(mode)
        roots = tuple(_normalized_root(item) for item in (dangerous_roots or DEFAULT_DANGEROUS_PATH_ROOTS))
        return cls(mode=normalized_mode, dangerous_roots=tuple(root for root in roots if root is not None))

    def check(self, path: str | Path) -> PathAccessDecision:
        if self.mode == PATH_ACCESS_MODE_FULL:
            return PathAccessDecision(True)
        try:
            resolved = Path(path).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            return PathAccessDecision(False, "PATH_RESOLUTION_FAILED", "路径解析失败，请检查路径是否有效。")
        for root in self.dangerous_roots:
            if _is_relative_to(resolved, root):
                return PathAccessDecision(
                    False,
                    "PATH_DANGEROUS_ROOT_BLOCKED",
                    f"路径位于危险目录，当前 path_access_mode=normal 不允许访问: target={resolved} dangerous_root={root}",
                    str(root),
                )
        return PathAccessDecision(True)


def normalize_path_access_mode(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in _VALID_MODES else PATH_ACCESS_MODE_NORMAL


def _normalized_root(value: object) -> Path | None:
    text = os.path.expandvars(str(value or "").strip())
    if not text:
        return None
    try:
        return Path(text).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


__all__ = [
    "DEFAULT_DANGEROUS_PATH_ROOTS",
    "DEFAULT_PATH_ACCESS_MODE",
    "PATH_ACCESS_MODE_FULL",
    "PATH_ACCESS_MODE_NORMAL",
    "PathAccessDecision",
    "PathAccessPolicy",
    "normalize_path_access_mode",
]
