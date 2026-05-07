# LLM: Memory archive module; keep task/run workspace files and long-term memory records stable.
# 模块用途: 维护任务工作区、运行记录、compact 链和长期记忆归档。

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 state_payload 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 state payload 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def state_payload(task_id: str, run_id: str, task: Any, now: float) -> dict[str, object]:
    return {
        "version": 1,
        "task_id": task_id,
        "primary_run_id": run_id,
        "status": str(getattr(task, "status", "")),
        "verification_status": str(getattr(task, "verification_status", "")),
        "progress": float(getattr(task, "progress", 0.0) or 0.0),
        "current_step": str(getattr(task, "current_step", "")),
        "latest_summary": str(getattr(task, "latest_summary", "")),
        "blockers": list(getattr(task, "blockers", []) or []),
        "artifact_refs": list(getattr(task, "artifact_refs", []) or []),
        "evidence_refs": list(getattr(task, "evidence_refs", []) or []),
        "child_run_ids": list(getattr(task, "child_ids", []) or []),
        "updated_at": now,
        "legacy": {
            "task_dir": str(getattr(task, "task_dir", "")),
            "task_json": str(Path(str(getattr(task, "task_dir", ""))) / "task.json")
            if getattr(task, "task_dir", "")
            else "",
        },
    }


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 timeline_event 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 timeline event 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def timeline_event(task: Any, now: float, previous_state: dict[str, object]) -> dict[str, object]:
    task_id = str(getattr(task, "root_id", "") or getattr(task, "id", "task"))
    run_id = str(getattr(task, "id", "") or task_id)
    return {
        "ts": now,
        "event": "task_workspace_synced",
        "task_id": task_id,
        "run_id": run_id,
        "status": str(getattr(task, "status", "")),
        "previous_status": str(previous_state.get("status") or ""),
        "summary": str(getattr(task, "latest_summary", "")),
        "refs": {
            "legacy_task_dir": str(getattr(task, "task_dir", "")),
        },
    }


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 read_json_object 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 读取 read json object 需要的文件、记录或配置，并整理成调用方可直接使用的结果。
def read_json_object(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 write_json 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 write json 相关记录，集中处理目标路径、格式化和状态更新。
def write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 append_timeline 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 append timeline 相关记录，集中处理目标路径、格式化和状态更新。
def append_timeline(path: Path, payload: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


__all__ = [
    "append_timeline",
    "read_json_object",
    "state_payload",
    "timeline_event",
    "write_json",
]
