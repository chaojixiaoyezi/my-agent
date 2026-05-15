# LLM: Subagent write boundary policy separates inherited product authority from direct product writes.
# 模块用途: 给 runner 写入边界生成 product roots 和 product write policy，避免 manager mixin 继续膨胀。

from __future__ import annotations

from pathlib import Path

from .models import SubAgentTask


# LLM: task_product_write_roots exposes inherited product authority separately from task-local report roots.
# 函数用途: 取出当前任务可覆盖的产物目录；角色不再因为身份被默认剥夺写入能力。
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


# LLM: task_product_write_policy keeps subagents capable by default while honoring explicit strict overrides.
# 函数用途: 返回写入边界的业务产物策略；默认 direct，只有任务显式声明 delegate 时才限制直接写业务产物。
def task_product_write_policy(task: SubAgentTask, product_roots: list[str]) -> str:
    policy = str((getattr(task, "attributes", None) or {}).get("product_write_policy") or "").strip().lower()
    return policy if policy in {"direct", "delegate"} else "direct"


# LLM: _resolved_path_text normalizes comparison only, never changes persisted user paths.
# 函数用途: 把路径转成可比较字符串；路径不存在时也不触盘。
def _resolved_path_text(path: str | Path) -> str:
    return str(Path(str(path)).expanduser().resolve(strict=False))
