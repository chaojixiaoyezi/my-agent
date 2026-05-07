# LLM: Memory archive module; keep task/run workspace files and long-term memory records stable.
# 模块用途: 维护任务工作区、运行记录、compact 链和长期记忆归档。

from __future__ import annotations

"""JSONL storage primitives for memory hook snapshots and raw archive events.

新手说明:
这个文件只管把快照和归档事件写到固定目录。
它不决定什么时候压缩、不调用模型，也不读取配置；以后接入真实流程时，外层负责判断时机，这里负责可靠落盘。
"""

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ..io import append_jsonl
from ._storage_dates import (
    _coerce_retention_days,
    _coerce_today,
    _date_from_filename,
    _date_key,
)
from ._storage_verify import (
    MemoryArchiveError,
    _verify_json_file_payload,
    _verify_record_exists,
)
from .models import CompressionSnapshot, RawMemoryEvent


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 snapshot_path_for 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 snapshot path for 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def snapshot_path_for(root: str | Path, created_at: str | int | float | None = None) -> Path:

    return Path(root) / "memory" / "hooks" / f"{_date_key(created_at)}.jsonl"


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 compression_snapshot_dir 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 compression snapshot dir 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def compression_snapshot_dir(root: str | Path) -> Path:

    return Path(root) / "memory_archive" / "snapshots"


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 compression_snapshot_file_for 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 compression snapshot file for 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def compression_snapshot_file_for(root: str | Path, snapshot: CompressionSnapshot) -> Path:

    safe_id = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in snapshot.snapshot_id)
    return compression_snapshot_dir(root) / f"{_date_key(snapshot.created_at)}--{safe_id}.json"


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 raw_event_path_for 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 raw event path for 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def raw_event_path_for(root: str | Path, created_at: str | int | float | None = None) -> Path:

    return Path(root) / "memory" / "raw" / f"{_date_key(created_at)}.jsonl"


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 filter_snapshot_for_level 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 判断 filter snapshot for level 是否满足规则、查询或上下文条件，返回确定性的筛选结果。
def filter_snapshot_for_level(payload: dict[str, Any], level: int) -> dict[str, Any]:

    level = max(0, min(3, level))
    if level == 0:
        return dict(payload)
    result = dict(payload)
    if level >= 1:
        _clear_tool_parameter_previews(result)
    if level >= 2:
        result["user_intents"] = result.get("user_intents", [])[:1]
        result["assistant_actions"] = result.get("assistant_actions", [])[:1]
        result["decisions"] = []
        result["open_questions"] = []
    if level >= 3:
        result["user_intents"] = []
        result["assistant_actions"] = []
        result["tool_calls"] = []
        result["content"] = ""
    return result


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _clear_tool_parameter_previews 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 clear tool parameter previews 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _clear_tool_parameter_previews(payload: dict[str, Any]) -> None:
    for tc in payload.get("tool_calls", []):
        if isinstance(tc, dict) and "parameters_preview" in tc:
            tc["parameters_preview"] = ""


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 filter_raw_event_for_level 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 判断 filter raw event for level 是否满足规则、查询或上下文条件，返回确定性的筛选结果。
def filter_raw_event_for_level(payload: dict[str, Any], level: int) -> dict[str, Any]:

    level = max(0, min(3, level))
    if level == 0:
        return dict(payload)
    result = dict(payload)
    if level >= 2:
        result["content_path"] = ""
    if level >= 3:
        result["content_path"] = ""
    return result


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 append_snapshot 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 append snapshot 相关记录，集中处理目标路径、格式化和状态更新。
def append_snapshot(root: str | Path, snapshot: CompressionSnapshot) -> Path:

    path = snapshot_path_for(root, snapshot.created_at)
    payload = snapshot.to_dict()
    append_jsonl(path, payload, sort_keys=True)
    _verify_record_exists(path, key="snapshot_id", value=snapshot.snapshot_id, expected=payload)
    return path


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 write_compression_snapshot_file 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 write compression snapshot file 相关记录，集中处理目标路径、格式化和状态更新。
def write_compression_snapshot_file(root: str | Path, snapshot: CompressionSnapshot) -> Path:

    path = compression_snapshot_file_for(root, snapshot)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = snapshot.to_dict()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    _verify_json_file_payload(path, expected=payload)
    return path


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 append_raw_event 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 append raw event 相关记录，集中处理目标路径、格式化和状态更新。
def append_raw_event(root: str | Path, event: RawMemoryEvent) -> Path:

    path = raw_event_path_for(root, event.created_at)
    payload = filter_raw_event_for_level(event.to_dict(), event.archive_level)
    append_jsonl(path, payload, sort_keys=True)
    _verify_record_exists(path, key="event_id", value=event.event_id, expected=payload)
    return path


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 enforce_retention 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 enforce retention 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def enforce_retention(root: str | Path, retention_days: Any, today: date | str | None = None) -> list[Path]:

    days = _coerce_retention_days(retention_days)
    if days is None or days == 0:
        return []

    hook_dir = Path(root) / "memory" / "hooks"
    if not hook_dir.exists():
        return []

    today_date = _coerce_today(today) or date.today()
    cutoff = today_date - timedelta(days=days - 1)
    deleted: list[Path] = []

    for path in sorted(hook_dir.glob("*.jsonl")):
        file_date = _date_from_filename(path)
        if file_date is None or file_date >= cutoff:
            continue
        path.unlink()
        deleted.append(path)

    return deleted
