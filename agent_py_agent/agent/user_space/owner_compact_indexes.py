# LLM: Owner compact indexes expose task/run/agent compact pointers without copying compact bodies.
# 模块用途: 在 owner_home/compact/by_* 下写轻量恢复指针，方便父代理和 doctor 快速定位 rollup。

from __future__ import annotations

import json
from pathlib import Path


# LLM: sync_owner_compact_indexes writes small owner-level pointers to the latest task rollup.
# 函数用途: 根据 task rollup 写 by_task/by_run/by_agent 指针；不复制子运行正文。
def sync_owner_compact_indexes(task_root: Path, rollup: dict[str, object]) -> None:
    owner_home = _owner_home_for_task(task_root)
    if owner_home is None:
        return
    task_id = _safe_key(rollup.get("task_id") or task_root.name)
    payload = _owner_compact_payload(rollup)
    _write_json(owner_home / "compact" / "by_task" / f"{task_id}.json", payload)
    for row in rollup.get("child_runs", []):
        if not isinstance(row, dict):
            continue
        run_id = _safe_key(row.get("run_id"))
        if not run_id:
            continue
        child_payload = {**payload, "run_id": str(row.get("run_id") or ""), "status": str(row.get("status") or "")}
        _write_json(owner_home / "compact" / "by_run" / f"{run_id}.json", child_payload)
        _write_json(owner_home / "compact" / "by_agent" / f"{run_id}.json", child_payload)


# LLM: _owner_compact_payload keeps owner compact indexes small and refs-only.
# 函数用途: 生成 owner_home/compact/by_* 的统一轻量指针内容。
def _owner_compact_payload(rollup: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "owner-compact-index.v1",
        "task_id": str(rollup.get("task_id") or ""),
        "task_workspace": str(rollup.get("task_workspace") or ""),
        "rollup_json": str(rollup.get("rollup_json") or ""),
        "compact_package": str(rollup.get("compact_package") or ""),
        "status": str(rollup.get("status") or ""),
        "child_count": int(rollup.get("child_count") or 0),
        "updated_at": str(rollup.get("updated_at") or ""),
    }


# LLM: _owner_home_for_task infers the owner home from a V2 task workspace path.
# 函数用途: 从 owner_home/tasks/<date>/<task> 路径回推 owner_home；非 task 路径返回 None。
def _owner_home_for_task(task_root: Path) -> Path | None:
    parts = task_root.parts
    if "tasks" not in parts:
        return None
    index = len(parts) - 1 - list(reversed(parts)).index("tasks")
    if index <= 0:
        return None
    return Path(*parts[:index])


# LLM: _safe_key turns open-world ids into file names without rejecting unknown ids.
# 函数用途: 把 task/run/agent id 转成安全文件名，未知字符用下划线兜底。
def _safe_key(value: object) -> str:
    text = str(value or "").strip()
    return "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in text)


# LLM: _write_json writes deterministic refs-only owner compact index files.
# 函数用途: 创建父目录并写入带排序键的 UTF-8 JSON。
def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


__all__ = ["sync_owner_compact_indexes"]
