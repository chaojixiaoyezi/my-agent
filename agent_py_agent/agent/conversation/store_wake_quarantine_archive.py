# LLM: 唤醒毒丸结案留档的保留期归档（毒丸第 4 步）：只移不删，保持相对位置。顶层结案记录按 quarantine.quarantined_at
#   判断（读不出或缺字段的按文件时间），超过保留期移到 quarantine/archive/<id>.json，它的坏账留档 ledger/<id>.json 一并移到
#   archive/ledger/；读不出的信封留档 unreadable/<id>.json 与没有顶层记录对应的孤儿坏账按 max(mtime, ctime) 判断
#   （os.replace 不改 mtime、会改 ctime）。replayed/ 不动，它是重放次数的唯一权威。
#   每条顶层记录在它自己的结案锁（wake_quarantine_path）里移动，与 WakeAttemptStore.quarantine/replay 串行。
#   归档只换物理位置、不改发布语义：archive/<id>.json 仍是发布层的只读安装位置（store_wake_publication._installed_signal），
#   同键再发布返回原结案信号；重放对已归档的记录拒绝（WAKE_REPLAY_ARCHIVED）。改动时联测 test_wake_quarantine_archive.py。
#   运维不能单独删 archive/<id>.json：同键回执还在时，再发布和查回执会抛 DataCorruptionError；要删必须连同 wake_queue/dedupe
#   里同键的回执一起删（见 docs/design/WAKE_POISON_PILL.md 第 8 节）。
# 模块用途: 在 6 小时一次的账本整理里，把超过 14 天的唤醒结案记录和留档移进归档目录，列表和计数只看近期的；清理归档须连同去重回执一起删。
from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

from ..gateway_parts.io import locked_file_transition
from .store_io import read_json_object_report
from .store_layout import ConversationStorage

# 结案留档移入归档前的保留期（设计第 8 节：超过 14 天的记录移入归档）；安全兜底用内部常量，不进配置。
WAKE_QUARANTINE_ARCHIVE_AFTER_SECONDS = 14 * 24 * 3600


# LLM: 只在账本整理周期（ConversationStore.gc_stale_ledger_records）调用；逐条独立，一条失败或撞名不影响其它条。
#   返回实际移动的文件数（记录、坏账留档、读不出的信封各算一个）。不读 replayed/，不删除任何文件。
# 函数用途: 把超过保留期的唤醒结案记录与两类留档移进 quarantine/archive/；会移动文件。
def archive_stale_wake_quarantine(
    storage: ConversationStorage, *, current: float, retention_seconds: float = WAKE_QUARANTINE_ARCHIVE_AFTER_SECONDS,
) -> int:
    archive = storage.wake_quarantine_archive_dir
    moved = sum(_archive_record(storage, path, current, retention_seconds)
                for path in _json_files(storage.wake_quarantine_dir))
    moved += sum(_archive_loose(path, archive / "unreadable" / path.name, current, retention_seconds)
                 for path in _json_files(storage.wake_quarantine_unreadable_dir))
    moved += sum(_archive_loose(path, archive / "ledger" / path.name, current, retention_seconds)
                 for path in _json_files(storage.wake_quarantine_ledger_dir)
                 if not (storage.wake_quarantine_dir / path.name).exists())
    return moved


# LLM: 在这条唤醒的结案锁里重读记录再判断，避免与并发的结案或重放交错；文件名不是合法唤醒 ID 的杂项文件不碰。
#   记录移走后再移它的坏账留档（有才移），两者都在同一把锁里。
# 函数用途: 判断一条顶层结案记录是否超过保留期，超过就连同坏账留档一起移进归档；返回移动的文件数。
def _archive_record(storage: ConversationStorage, path: Path, current: float, retention_seconds: float) -> int:
    try:
        lock_path = storage.wake_quarantine_path(path.stem)
    except ValueError:
        return 0
    with locked_file_transition(lock_path):
        if not path.exists():
            return 0
        payload, error = read_json_object_report(path, context="conversation.wake_quarantine.archive")
        settled_at = (_record_settled_at(payload) if error is None else None) or _file_settled_at(path)
        if current - settled_at <= retention_seconds:
            return 0
        moved = _archive_move(path, storage.wake_quarantine_archive_path(path.stem))
        if moved:
            moved += _archive_move(storage.wake_quarantine_ledger_path(path.stem),
                                   storage.wake_quarantine_archive_dir / "ledger" / path.name)
    return moved


# LLM: 没有结案记录可读的留档（读不出的信封、孤儿坏账）只能看文件时间；取 max(mtime, ctime)，
#   因为结案时是 os.replace 移过来的，mtime 还是原文件最后写入的时间，会比结案早很多。
# 函数用途: 判断一份无记录的留档是否超过保留期，超过就移进归档；返回移动的文件数。
def _archive_loose(path: Path, target: Path, current: float, retention_seconds: float) -> int:
    try:
        settled_at = _file_settled_at(path)
    except FileNotFoundError:
        return 0
    if current - settled_at <= retention_seconds:
        return 0
    return _archive_move(path, target)


# LLM: 归档位置已有同名文件时不覆盖（同一唤醒只会结案归档一次，撞名说明有外部改动，原文件留在原位给运维）；
#   源文件已不在（被并发重放或清理）按没移动处理。同一文件系统上 os.replace 原子改名，字节不变。
# 函数用途: 把一个留档文件原样移到归档位置，返回 1 表示移动了、0 表示没移动。
def _archive_move(source: Path, target: Path) -> int:
    if not source.exists() or target.exists():
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, target)
    except FileNotFoundError:
        return 0
    return 1


# LLM: 只认结案记录顶层 quarantine 键里的正有限数 quarantined_at；缺失、布尔或坏值返回 None，由调用方改看文件时间。
# 函数用途: 读出一条结案记录的结案时间。
def _record_settled_at(payload: dict[str, Any]) -> float | None:
    facts = payload.get("quarantine") if isinstance(payload.get("quarantine"), dict) else {}
    value = facts.get("quarantined_at")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


# LLM: 只读文件元数据，不读内容；文件已不在时 FileNotFoundError 照常上抛，由调用方按没移动处理。
# 函数用途: 取一份留档文件的落地时间：max(mtime, ctime)。
def _file_settled_at(path: Path) -> float:
    stat = path.stat()
    return max(stat.st_mtime, stat.st_ctime)


# LLM: 只列顶层普通文件，不递归：archive/、replayed/、unreadable/、ledger/ 这些子目录由调用方分别处理。
# 函数用途: 列出一个目录顶层的 JSON 文件（目录不存在时为空），不含子目录。
def _json_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.glob("*.json") if path.is_file()) if directory.is_dir() else []


__all__ = [
    "WAKE_QUARANTINE_ARCHIVE_AFTER_SECONDS",
    "archive_stale_wake_quarantine",
]
