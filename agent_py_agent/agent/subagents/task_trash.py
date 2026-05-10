# LLM: Task trash manager replaces direct child-agent deletion with auditable workspace-local moves.
# 模块用途: 给每个任务目录提供 trash/，让子代理通过受控移动代替 rm，并记录 manifest。

from __future__ import annotations

"""Task-local trash manager for subagent workspaces."""

import json
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


# LLM: TaskTrashMoveRequest is the bundle for moving one path into task-local trash.
# 类用途: 保存一次 trash move 请求，调用方必须提供任务目录、源路径和可选授权根目录。
@dataclass(frozen=True)
class TaskTrashMoveRequest:
    task_dir: str | Path
    source_path: str | Path
    allowed_roots: list[str | Path] = field(default_factory=list)
    reason: str = ""
    actor_run_id: str = ""
    trash_dir: str | Path = ""


# LLM: TaskTrashMoveResult is the auditable outcome returned to parent agents and tests.
# 类用途: 返回 trash move 是否成功、源/目标路径、manifest 引用和阻断原因。
@dataclass
class TaskTrashMoveResult:
    moved: bool
    source: str
    destination: str = ""
    manifest_ref: str = ""
    reason: str = ""
    blockers: list[str] = field(default_factory=list)


# LLM: ensure_task_trash recreates the task-local trash folder if users or cleanup hooks removed it.
# 函数用途: 确保任务目录下 trash/ 和 manifest 文件存在，供长期任务重复使用。
def ensure_task_trash(task_dir: str | Path, trash_dir: str | Path = "") -> Path:
    task = Path(task_dir).expanduser().resolve()
    trash = _resolve_trash_dir(task, trash_dir)
    trash.mkdir(parents=True, exist_ok=True)
    manifest = trash / "manifest.jsonl"
    manifest.touch(exist_ok=True)
    return trash


# LLM: move_to_task_trash is the only deletion-like operation child agents should receive.
# 函数用途: 把授权范围内的文件或目录移动到 task-local trash，并追加 manifest 审计记录。
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


# LLM: _move_blockers keeps trash moves scoped to task or explicit allowed roots.
# 函数用途: 汇总源路径不存在、越界、移动 trash 自身等阻断原因。
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


# LLM: _resolve_source treats relative paths as task-local and absolute paths literally.
# 函数用途: 将源路径解析成绝对路径，供边界检查和移动使用。
def _resolve_source(task: Path, source_path: str | Path) -> Path:
    source = Path(source_path).expanduser()
    return source.resolve() if source.is_absolute() else (task / source).resolve()


# LLM: _resolve_trash_dir prevents custom trash paths from escaping the task directory.
# 函数用途: 解析 trash 目录；越界配置回退到 task_dir/trash。
def _resolve_trash_dir(task: Path, trash_dir: str | Path) -> Path:
    if not str(trash_dir or "").strip():
        return task / "trash"
    candidate = Path(trash_dir).expanduser()
    path = candidate.resolve() if candidate.is_absolute() else (task / candidate).resolve()
    return path if _is_relative_to(path, task) else task / "trash"


# LLM: _allowed_roots normalizes optional extra roots but never allows paths outside the task by default.
# 函数用途: 归一化允许被移入 trash 的源路径根目录，默认只允许 task_dir。
def _allowed_roots(task: Path, roots: list[str | Path]) -> list[Path]:
    resolved = []
    for raw in roots:
        candidate = Path(raw).expanduser()
        path = candidate.resolve() if candidate.is_absolute() else (task / candidate).resolve()
        if _is_relative_to(path, task):
            resolved.append(path)
    return resolved or [task]


# LLM: _unique_destination avoids overwriting prior trash entries with the same source basename.
# 函数用途: 生成不冲突的 trash 目标路径，保留源文件名方便人工查看。
def _unique_destination(trash: Path, source: Path) -> Path:
    stem = f"{int(time.time() * 1000)}_{source.name}"
    destination = trash / stem
    counter = 1
    while destination.exists():
        destination = trash / f"{stem}_{counter}"
        counter += 1
    return destination


# LLM: _append_manifest records trash moves without embedding file content.
# 函数用途: 追加 manifest JSONL，记录移动来源、目标、原因和执行者。
def _append_manifest(manifest: Path, result: TaskTrashMoveResult, actor_run_id: str) -> None:
    record = asdict(result)
    record["actor_run_id"] = actor_run_id
    record["created_at"] = time.time()
    with manifest.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


# LLM: _is_relative_to keeps path scope checks readable across Python versions.
# 函数用途: 判断 path 是否在 root 下，供源路径和 trash 目录边界复用。
def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
