from __future__ import annotations

"""Authoritative per-invocation workspace selection for registry tools."""

from pathlib import Path


# 会话运行时 carries one cwd in the immutable turn context and 长期助手 resolves one
# task/session cwd before file or process tools run.  Keep the same invariant
# here: a typed task boundary selects one cwd for every relative-path surface;
# model payloads and goal prose never participate in the choice.
def effective_registry_cwd(
    workspace_root: Path,
    write_boundary: dict[str, object] | None,
) -> Path:
    """Return the one trusted cwd for this registry invocation."""

    root = Path(workspace_root).expanduser().resolve(strict=False)
    boundary = write_boundary if isinstance(write_boundary, dict) else {}
    if "allowed_write_roots" not in boundary:
        return root
    raw_task_root = str(boundary.get("task_root") or "").strip()
    if not raw_task_root:
        return root
    try:
        return Path(raw_task_root).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return root
