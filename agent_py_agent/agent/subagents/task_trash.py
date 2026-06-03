
from __future__ import annotations

"""Task-local trash manager for subagent workspaces."""

import json
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


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
    trash.mkdir(parents=True, exist_ok=True)
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


def _append_manifest(manifest: Path, result: TaskTrashMoveResult, actor_run_id: str) -> None:
    record = asdict(result)
    record["actor_run_id"] = actor_run_id
    record["created_at"] = time.time()
    with manifest.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
