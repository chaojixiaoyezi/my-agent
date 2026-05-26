# LLM: Context bundle internal-root helpers keep user product roots separate from run-private files.
# 模块用途: 归一 task 内部目录，用于过滤 allowed_write_roots 里的非交付目录。

from __future__ import annotations

from pathlib import Path


def internal_root_texts(task: object) -> set[str]:
    fields = (
        "task_dir",
        "data_dir",
        "output_dir",
        "tests_dir",
        "reports_dir",
        "logs_dir",
        "scratch_dir",
        "task_workspace_dir",
        "agent_run_workspace_dir",
        "agent_run_artifacts_dir",
    )
    return {_resolved_path_text(getattr(task, field, "")) for field in fields if path_text(getattr(task, field, ""))}


def path_is_internal(path: str, internal_roots: set[str]) -> bool:
    resolved = _resolved_path_text(path)
    return any(resolved == root or resolved.startswith(f"{root}/") for root in internal_roots if root)


def path_text(value: object) -> str:
    if isinstance(value, Path):
        return str(value)
    return value if isinstance(value, str) else ""


def _resolved_path_text(value: object) -> str:
    text = path_text(value)
    return str(Path(text).expanduser().resolve(strict=False)) if text else ""
