# LLM: Runtime archive roots mirror new owner storage without breaking legacy workspace readers.
# 模块用途: 给 raw archive、runtime_fact、token ledger 等写入点提供“旧工作区 + 新 owner home”的兼容根目录。

from __future__ import annotations

from pathlib import Path


def runtime_owner_root(agent: object) -> Path:
    home_paths = getattr(agent, "home_paths", None)
    owner_home = getattr(home_paths, "owner_home_dir", None)
    if owner_home:
        return Path(owner_home)
    return Path(agent.root)


def runtime_archive_roots(agent: object) -> tuple[Path, ...]:
    roots: list[Path] = [Path(agent.root)]
    owner = runtime_owner_root(agent)
    if owner not in roots:
        roots.append(owner)
    return tuple(roots)


__all__ = ["runtime_archive_roots", "runtime_owner_root"]
