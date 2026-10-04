
from __future__ import annotations

"""Task-local trash manager for subagent workspaces.

回收站清单落盘走 append_private_text（pw2）：0600 文件、0700 目录。
"""

import json
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..common.json_io import append_private_text
from ..common.nofollow_fs import ensure_private_dir


@dataclass(frozen=True)
class TaskTrashMoveRequest:
    task_dir: str | Path
    source_path: str | Path
    allowed_roots: list[str | Path] = field(default_factory=list)
    reason: str = ""
    actor_run_id: str = ""
    trash_dir: str | Path = ""


@dataclass
class TaskTrashMoveResult:
    moved: bool
    source: str
    destination: str = ""
    manifest_ref: str = ""
    reason: str = ""
    blockers: list[str] = field(default_factory=list)


def ensure_task_trash(task_dir: str | Path, trash_dir: str | Path = "") -> Path:
    task = Path(task_dir).expanduser().resolve()
    trash = _resolve_trash_dir(task, trash_dir)
    # 目录缺失时逐级按 0700 新建（pbfix 2026-10-04：统一走 nofollow_fs.ensure_private_dir；已存在的目录一律不动）。
    ensure_private_dir(trash)
    manifest = trash / "manifest.jsonl"
    manifest.touch(exist_ok=True)
    return trash


def move_to_task_trash(request: TaskTrashMoveRequest) -> TaskTrashMoveResult:
    task = Path(request.task_dir).expanduser().resolve()
    trash = ensure_task_trash(task, request.trash_dir)
    source = _resolve_source(task, request.source_path)
    blockers = _move_blockers(task, trash, source, request.allowed_roots)
    if blockers:
        return TaskTrashMoveResult(False, str(source), reason=blockers[0], blockers=blockers)
    destination = _unique_destination(trash, source)
    shutil.move(str(source), str(destination))
    result = TaskTrashMoveResult(
        moved=True,
        source=str(source),
        destination=str(destination),
        manifest_ref=str(trash / "manifest.jsonl"),
        reason=request.reason or "moved_to_task_trash",
    )
    _append_manifest(trash / "manifest.jsonl", result, request.actor_run_id)
    return result


def _move_blockers(task: Path, trash: Path, source: Path, allowed_roots: list[str | Path]) -> list[str]:
    if not source.exists():
        return ["source_missing"]
    if source == task:
        return ["cannot_trash_task_dir"]
    if source == trash or _is_relative_to(source, trash):
        return ["source_already_in_trash"]
    roots = _allowed_roots(task, allowed_roots)
    if not any(_is_relative_to(source, root) for root in roots):
        return ["source_outside_allowed_roots"]
    return []


def _resolve_source(task: Path, source_path: str | Path) -> Path:
    source = Path(source_path).expanduser()
    return source.resolve() if source.is_absolute() else (task / source).resolve()


def _resolve_trash_dir(task: Path, trash_dir: str | Path) -> Path:
    if not str(trash_dir or "").strip():
        return task / "trash"
    candidate = Path(trash_dir).expanduser()
    path = candidate.resolve() if candidate.is_absolute() else (task / candidate).resolve()
    return path if _is_relative_to(path, task) else task / "trash"


def _allowed_roots(task: Path, roots: list[str | Path]) -> list[Path]:
    resolved = []
    for raw in roots:
        candidate = Path(raw).expanduser()
        path = candidate.resolve() if candidate.is_absolute() else (task / candidate).resolve()
        resolved.append(path)
    return resolved or [task]


def _unique_destination(trash: Path, source: Path) -> Path:
    stem = f"{int(time.time() * 1000)}_{source.name}"
    destination = trash / stem
    counter = 1
    while destination.exists():
        destination = trash / f"{stem}_{counter}"
        counter += 1
    return destination


# LLM: 回收站清单是宿主运行数据：私有追加（0600/0700）；记录里补 actor_run_id 与 created_at，格式不变。
# 函数用途: 把一次移入回收站的结果追加进 manifest.jsonl。
def _append_manifest(manifest: Path, result: TaskTrashMoveResult, actor_run_id: str) -> None:
    record = asdict(result)
    record["actor_run_id"] = actor_run_id
    record["created_at"] = time.time()
    append_private_text(manifest, json.dumps(record, ensure_ascii=False) + "\n")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
