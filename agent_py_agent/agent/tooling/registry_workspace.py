"""Authoritative per-invocation workspace selection for registry tools."""

from __future__ import annotations

from pathlib import Path

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

    root = Path(workspace_root).expanduser().resolve(strict=False)
    boundary = write_boundary if isinstance(write_boundary, dict) else {}
    raw_execution_cwd = str(boundary.get("execution_cwd") or "").strip()
    if not raw_execution_cwd:
        return root
    try:
        return Path(raw_execution_cwd).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return root
