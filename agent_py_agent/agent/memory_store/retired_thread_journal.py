# LLM: 退休台账只追加已提交的线程退休证明，不执行侧状态删除；消费者仍须复核回收站状态。
# 模块用途: 提供所有者内退休线程记录的追加、字节分页与四态判定合同。
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path

# 退休线程台账单页最多返回的行数。
RETIRED_THREAD_JOURNAL_PAGE_ROWS = 256


# LLM: 页结果不可变，字节续点只跨过已消费的完整物理行。
# 类用途: 返回退休台账的一页记录及下次读取位置。
@dataclass(frozen=True)
class RetiredThreadPage:
    rows: tuple[dict, ...]
    next_offset: int
    complete: bool


# LLM: 只接受所有者内的非空相对路径，未知路径不产生退休证明。
# 函数用途: 校验台账路径并拆为安全相对组件。
def _relative_parts(value: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("invalid_retired_thread_path")
    path = Path(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("invalid_retired_thread_path")
    return path.parts


# LLM: 外部会话根不得写入本所有者的退休台账；不通过 resolve 跟随链接猜归属。
# 函数用途: 将调用方路径转为所有者根下的规范相对路径。
def _owner_relative(home, path: Path) -> str:
    owner = Path(home.owner_home_dir)
    candidate = Path(path)
    relative = candidate.relative_to(owner) if candidate.is_absolute() else candidate
    _relative_parts(str(relative))
    return str(relative)


# LLM: 未知版本、字段类型和时间均不能成为删除依据。
# 函数用途: 检查退休记录的固定字段及路径形状。
def _validate_row(row: dict) -> None:
    if not isinstance(row, dict) or row.get("schema") != "retired_thread.v1":
        raise ValueError("invalid_retired_thread_row")
    thread_id = row.get("thread_id")
    if not isinstance(thread_id, str) or _relative_parts(thread_id) != (thread_id,):
        raise ValueError("invalid_retired_thread_row")
    if not isinstance(row.get("action_id"), str) or not row["action_id"]:
        raise ValueError("invalid_retired_thread_row")
    stamp = row.get("retired_at")
    if type(stamp) not in (int, float) or not 0 <= stamp < math.inf:
        raise ValueError("invalid_retired_thread_row")
    _relative_parts(row.get("conversation_root"))
    _relative_parts(row.get("trash_container"))


# LLM: 仅由领域在退休事务提交之后调用；沿用原 JSON 锁与私有追加叶，不重写台账历史。
# 函数用途: 追加一条所有者相对路径的退休证明。
def append_retired_thread(
    home, *, thread_id: str, conversation_root: Path, action_id: str,
    trash_container: Path, retired_at: float,
) -> None:
    from ..common.json_io import append_private_jsonl_records_unlocked, locked_json_path

    row = {
        "schema": "retired_thread.v1",
        "thread_id": thread_id,
        "conversation_root": _owner_relative(home, conversation_root),
        "action_id": action_id,
        "trash_container": _owner_relative(home, trash_container),
        "retired_at": retired_at,
    }
    _validate_row(row)
    path = Path(home.owner_data_dir) / "retention" / "retired_threads.jsonl"
    _owner_relative(home, path)
    with locked_json_path(path):
        append_private_jsonl_records_unlocked(path, [row], sort_keys=True)


# LLM: 只读no-follow字节分页；坏完整行（含过深JSON）跳过，末尾半行不消费，I/O失败上抛。
# 函数用途: 从字节续点读取至多一页完整台账行。
def iter_retired_threads(
    home, *, offset: int = 0, max_rows: int = RETIRED_THREAD_JOURNAL_PAGE_ROWS,
) -> RetiredThreadPage:
    from ..common.nofollow_fs import open_readonly_file_beneath

    if type(offset) is not int or offset < 0 or type(max_rows) is not int or max_rows <= 0:
        raise ValueError("invalid_retired_thread_page")
    path = Path(home.owner_data_dir) / "retention" / "retired_threads.jsonl"
    parts = _relative_parts(_owner_relative(home, path))
    try:
        descriptor = open_readonly_file_beneath(home.owner_home_dir, parts)
    except FileNotFoundError:
        return RetiredThreadPage((), offset, True)
    rows, next_offset = [], offset
    with os.fdopen(descriptor, "rb") as handle:
        handle.seek(offset)
        for _ in range(max_rows):
            raw = handle.readline()
            if not raw:
                return RetiredThreadPage(tuple(rows), next_offset, True)
            if not raw.endswith(b"\n"):
                return RetiredThreadPage(tuple(rows), next_offset, False)
            next_offset = handle.tell()
            try:
                row = json.loads(raw)
            except (ValueError, UnicodeDecodeError, RecursionError):
                continue
            if isinstance(row, dict):
                rows.append(row)
        complete = not handle.read(1)
    return RetiredThreadPage(tuple(rows), next_offset, complete)


# LLM: 父目录逐级 no-follow，最终叶用 lstat；仅明确缺失可表示不在，其他异常不能伪造成已清除。
# 函数用途: 检查所有者内一个状态路径是否存在。
def _state_path_exists(home, parts: tuple[str, ...]) -> bool:
    from ..common.nofollow_fs import open_directory_beneath

    try:
        parent = open_directory_beneath(home.owner_home_dir, parts[:-1], require_dir_fd=True)
    except FileNotFoundError:
        return False
    try:
        os.lstat(parts[-1], dir_fd=parent)
    except FileNotFoundError:
        return False
    finally:
        os.close(parent)
    return True


# LLM: 只有回收站和线程叶均明确不存在才返回 purged；坏行、链接父级与其他读取错误均保护对象。
# 函数用途: 区分退休线程的等待、清除、恢复与未知状态。
def retired_thread_state(home, row: dict) -> str:
    try:
        _validate_row(row)
        trash = _relative_parts(row["trash_container"])
        conversation = _relative_parts(row["conversation_root"])
        if _state_path_exists(home, trash):
            return "waiting"
        if _state_path_exists(home, (*conversation, "threads", row["thread_id"] + ".json")):
            return "restored"
    except (OSError, ValueError, TypeError):
        return "unknown"
    return "purged"


# LLM: 消费者只接受四态判定的正面清除证明，不自行把不存在或错误升级为删除权。
# 函数用途: 判断退休证明对应的回收站是否已经清除且线程未恢复。
def retired_thread_purged(home, row: dict) -> bool:
    return retired_thread_state(home, row) == "purged"
