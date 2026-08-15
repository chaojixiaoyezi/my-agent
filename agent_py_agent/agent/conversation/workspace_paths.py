from __future__ import annotations

"""Owner-scoped workspace paths for durable conversation work."""

import re
from pathlib import Path

_STABLE_TASK_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


def durable_work_root(owner_home: str | Path, work_kind: str) -> Path:
    owner = Path(owner_home).expanduser().resolve(strict=False)
    return owner / ("audits" if str(work_kind or "").strip().lower() == "audit" else "tasks")


def audit_workspace_path(owner_home: str | Path, audit_id: str) -> Path:
    stable_id = str(audit_id or "").strip()
    if not stable_id or _STABLE_TASK_ID.fullmatch(stable_id) is None:
        raise ValueError("audit id is not a stable path segment")
    return durable_work_root(owner_home, "audit") / stable_id


def validated_durable_work_path(
    owner_home: str | Path,
    task_path: str | Path,
    work_kind: str,
    *,
    require_directory: bool = False,
) -> Path:
    # A named root link carries work_kind, while descendant run links only
    # carry their concrete task_path.  Keep both cases owner-confined: an
    # untyped descendant may resolve below either canonical durable-work root,
    # but never elsewhere under the owner home.
    normalized_kind = str(work_kind or "").strip().lower()
    roots = (
        (durable_work_root(owner_home, normalized_kind),)
        if normalized_kind
        else (
            durable_work_root(owner_home, "task"),
            durable_work_root(owner_home, "audit"),
        )
    )
    path = Path(task_path).expanduser().resolve(strict=False)
    root = next((candidate for candidate in roots if path.is_relative_to(candidate)), None)
    if root is None:
        raise ValueError("durable work path is outside its owner task and audit roots")
    if path == root:
        raise ValueError("durable work path must be below its owner root")
    if require_directory and not path.is_dir():
        raise ValueError("durable work path is not a directory")
    return path


__all__ = [
    "audit_workspace_path",
    "durable_work_root",
    "validated_durable_work_path",
]
