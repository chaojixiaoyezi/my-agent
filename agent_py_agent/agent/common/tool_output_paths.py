# LLM: 本模块是工具输出归档物理布局的跨域小权威；Memory Store、Memory Archive
# 和工具调用方只能从这里解析 owner/task 索引位置，不能互相反向导入或复制 glob 规则；
# 原生路径排序完成后释放比较缓存，再做规范路径去重，保持同一读取范围与顺序。
# 模块用途: 统一计算工具大输出根目录、历史任务目录和只读索引路径。

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path


# LLM: Canonical writes always use the exact root's blobs directory; lookup expansion belongs
# to the separate read helpers below and must not change the write target.
# 函数用途: 返回一个 owner 或任务根下唯一的工具输出写入目录。
def tool_output_root(root: str | Path) -> Path:
    return Path(root) / "blobs" / "tool_outputs"


# LLM: 查找覆盖新 runs 与原处保留的旧 tasks 索引，保持两层有界遍历；不改变归档写入身份。
#   先沿原 Path 比较规则排序，再重建同值 Path，避免比较用的组件缓存与规范化结果同时驻留；
#   不缓存目录或权限，每次重新枚举；改动需复核 tool_ref 测量、scope 与路径顺序回归。
# 函数用途: 只读枚举 owner 的工具输出目录，保留原引用范围和顺序，并及时释放路径排序的临时缓存。
def tool_output_roots_for_lookup(root: str | Path) -> tuple[Path, ...]:
    base = Path(root)
    roots = [tool_output_root(base)]
    for namespace in ("runs", "tasks"):
        archive_root = base / namespace
        if archive_root.is_dir():
            roots.extend(Path(str(item)) for item in sorted(archive_root.glob("*/*/work/blobs/tool_outputs")))
    return _unique_paths(roots)


# LLM: Callers scan metadata-only JSONL indexes, never artifact bodies, when verifying refs.
# 函数用途: 显式来源只规范化去重；未提供来源的历史查找仍覆盖 owner 的全部原索引。
def tool_output_index_paths_for_lookup(root: str | Path, *, index_paths: Sequence[Path] | None = None) -> tuple[Path, ...]:
    if index_paths is not None:
        return tuple(dict.fromkeys(Path(path).expanduser().resolve(strict=False) for path in index_paths))
    return tuple(item / "index.jsonl" for item in tool_output_roots_for_lookup(root))


# LLM: 回合开始只量明确来源（默认 owner 主索引），不枚举 runs/tasks；明确来源尚未创建时记零身份而不丢路径。
#   收尾仅在 inode 未换、长度未缩且标记前一字节为 LF 时从标记读起（tool_index_stream 核验），否则仍整读。
# 函数用途: 记录 owner 工具输出主索引的回合起点 [dev, ino, size]，缺失或不可读时返回空。
def tool_output_index_marks(root: str | Path, *, index_paths: Sequence[Path] | None = None) -> dict[str, list[int]]:
    paths = (tool_output_root(root) / "index.jsonl",) if index_paths is None else index_paths
    marks: dict[str, list[int]] = {}
    for index in tool_output_index_paths_for_lookup(root, index_paths=paths):
        try:
            info = os.stat(index)
        except FileNotFoundError:
            if index_paths is not None:
                marks[str(index)] = [0, 0, 0]
            continue
        except OSError:
            if index_paths is not None:
                raise
            continue
        marks[str(index)] = [info.st_dev, info.st_ino, info.st_size]
    return marks


# LLM: Path identity is lexical-resolved and fail-soft; an invalid path cannot widen lookup.
# 函数用途: 保持目录原顺序去重，并忽略无法安全解析的条目。
def _unique_paths(paths: list[Path]) -> tuple[Path, ...]:
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        try:
            resolved = path.expanduser().resolve(strict=False)
        except OSError:
            continue
        key = str(resolved)
        if key not in seen:
            seen.add(key)
            unique.append(resolved)
    return tuple(unique)


__all__ = [
    "tool_output_index_paths_for_lookup",
    "tool_output_root",
    "tool_output_roots_for_lookup",
]
