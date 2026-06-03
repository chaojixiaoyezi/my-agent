
from __future__ import annotations

from pathlib import Path


def runtime_owner_root(agent: object) -> Path:
    home_paths = getattr(agent, "home_paths", None)
    owner_home = getattr(home_paths, "owner_home_dir", None)
    if isinstance(owner_home, str | Path) and str(owner_home).strip():
        return Path(owner_home)
    return Path(agent.root)


def runtime_archive_roots(agent: object) -> tuple[Path, ...]:
    owner = runtime_owner_root(agent)
    if owner:
        return (owner,)
    return (Path(agent.root),)


__all__ = ["runtime_archive_roots", "runtime_owner_root"]
