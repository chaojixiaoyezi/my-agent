
from __future__ import annotations

from pathlib import Path

from .models import SubAgentTask


def task_product_write_roots(task: SubAgentTask, report_roots: list[str]) -> list[str]:
    task_dir = _resolved_path_text(task.task_dir)
    report_root_set = {_resolved_path_text(item) for item in report_roots}
    roots: list[str] = []
    for raw in task.allowed_write_roots:
        text = str(raw or "").strip()
        if not text:
            continue
        resolved = _resolved_path_text(text)
        if resolved == task_dir or resolved in report_root_set:
            continue
        if text not in roots:
            roots.append(text)
    return roots


def task_product_write_policy(task: SubAgentTask, product_roots: list[str]) -> str:
    policy = str((getattr(task, "attributes", None) or {}).get("product_write_policy") or "").strip().lower()
    return policy if policy in {"direct", "delegate"} else "direct"


def _resolved_path_text(path: str | Path) -> str:
    return str(Path(str(path)).expanduser().resolve(strict=False))
