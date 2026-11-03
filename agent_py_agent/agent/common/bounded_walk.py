"""有界目录遍历与结构化覆盖度。"""
from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BoundedWalkResult:
    paths: tuple[Path, ...]
    truncated: bool
    coverage: dict[str, object]


# LLM: 调用方提供包含规则，返回路径与覆盖事实；默认不跟随符号链接。
# 函数用途: 在预算内枚举目录，明确区分完整结果与部分观察。
def bounded_walk(
    root: Path, *, include: Callable[[os.DirEntry[str]], bool],
    exclude_dir_names: frozenset[str] = frozenset({".git", "__pycache__"}),
    max_depth: int = 8, max_entries: int = 20000, max_dirs: int = 4000,
    deadline_seconds: float = 2.0, follow_symlinks: bool = False,
    exclude_relative_paths: frozenset[str] = frozenset(),
) -> BoundedWalkResult:
    base = Path(root)
    started = time.monotonic()
    paths: list[Path] = []
    coverage: dict[str, object] = {
        "visited_entries": 0, "visited_dirs": 0, "pruned_dirs": 0,
        "elapsed_ms": 0.0, "stop_reason": "complete", "error_count": 0,
    }
    entries = dirs = pruned = errors = 0
    depth_limited = False
    stop_reason = "complete"
    pending = [(base, 0)] if base.is_dir() and (follow_symlinks or not base.is_symlink()) else []
    seen: set[tuple[int, int]] = set()
    while pending:
        directory, depth = pending.pop()
        if time.monotonic() - started >= max(0.0, deadline_seconds):
            stop_reason = "deadline"
            break
        if dirs >= max(0, max_dirs):
            stop_reason = "max_dirs"
            break
        if entries >= max(0, max_entries):
            stop_reason = "max_entries"
            break
        try:
            if follow_symlinks:
                stat = directory.stat()
                identity = (stat.st_dev, stat.st_ino)
                if identity in seen:
                    pruned += 1
                    continue
                seen.add(identity)
            # 不先 list(scandir)：高扇出单目录也受 entry/deadline 预算约束。
            with os.scandir(directory) as iterator:
                dirs += 1
                children: list[tuple[Path, int]] = []
                while True:
                    if time.monotonic() - started >= max(0.0, deadline_seconds):
                        stop_reason = "deadline"
                        break
                    if entries >= max(0, max_entries):
                        stop_reason = "max_entries"
                        break
                    entry = next(iterator, None)
                    if entry is None:
                        break
                    entries += 1
                    if not follow_symlinks and entry.is_symlink():
                        continue
                    is_dir = entry.is_dir(follow_symlinks=follow_symlinks)
                    path = Path(entry.path)
                    if is_dir and (entry.name in exclude_dir_names or
                                   path.relative_to(base).as_posix() in exclude_relative_paths):
                        pruned += 1
                        continue
                    if include(entry):
                        paths.append(path)
                    if is_dir:
                        if depth >= max(0, max_depth):
                            depth_limited = True
                            pruned += 1
                        else:
                            children.append((path, depth + 1))
                pending.extend(reversed(children))
        except OSError:
            # 目录自然消失或不可读只提供部分观察，不能把缺口说成完整扫描。
            errors += 1
        if stop_reason != "complete":
            break
    if stop_reason == "complete" and depth_limited:
        stop_reason = "max_depth"
    coverage.update(visited_entries=entries, visited_dirs=dirs, pruned_dirs=pruned,
                    elapsed_ms=(time.monotonic() - started) * 1000,
                    stop_reason=stop_reason, error_count=errors)
    return BoundedWalkResult(tuple(paths), stop_reason != "complete" or bool(errors), coverage)
