
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULT_ROUTE_INDEX = Path("memory") / "routing" / "INDEX.md"


@dataclass(frozen=True)
class RouteIndexTarget:
    path: Path
    authority_root: Path


def resolve_route_index_target(root: Path, raw_index: str | None, *, home_paths: object | None = None) -> RouteIndexTarget:
    candidate = Path(raw_index).expanduser() if raw_index else DEFAULT_ROUTE_INDEX
    if candidate.is_absolute():
        return RouteIndexTarget(path=candidate.resolve(), authority_root=root)
    workspace_index = (root / candidate).resolve()
    if raw_index or workspace_index.exists() or home_paths is None:
        return RouteIndexTarget(path=workspace_index, authority_root=root)
    if _is_local_main_owner(home_paths):
        home_index = getattr(home_paths, "memory_routing_index_md", None)
        home_root = getattr(home_paths, "root", None)
    else:
        home_index = getattr(home_paths, "owner_memory_routing_index_md", None)
        home_root = getattr(home_paths, "owner_home_dir", None)
    if home_index is not None and home_root is not None:
        return RouteIndexTarget(path=Path(home_index).resolve(), authority_root=Path(home_root).resolve())
    return RouteIndexTarget(path=workspace_index, authority_root=root)


def runtime_route_root_and_index(agent) -> tuple[Path, str]:
    target = resolve_route_index_target(agent.root, None, home_paths=getattr(agent, "home_paths", None))
    if target.authority_root == agent.root:
        return target.authority_root, DEFAULT_ROUTE_INDEX.as_posix()
    return target.authority_root, DEFAULT_ROUTE_INDEX.as_posix()


def _is_local_main_owner(home_paths: object) -> bool:
    return (
        str(getattr(home_paths, "owner_provider", "") or "local") == "local"
        and str(getattr(home_paths, "owner_kind", "") or "main") == "main"
        and str(getattr(home_paths, "owner_id", "") or "local/main") == "local/main"
    )


__all__ = ["DEFAULT_ROUTE_INDEX", "RouteIndexTarget", "resolve_route_index_target", "runtime_route_root_and_index"]
