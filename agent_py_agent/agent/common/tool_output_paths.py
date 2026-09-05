# LLM: 本模块是工具输出归档物理布局的跨域小权威；Memory Store、Memory Archive
# 和工具调用方只能从这里解析 owner/task 索引位置，不能互相反向导入或复制 glob 规则。
# 模块用途: 统一计算工具大输出根目录、历史任务目录和只读索引路径。

from __future__ import annotations

from pathlib import Path


# LLM: Canonical writes always use the exact root's blobs directory; lookup expansion belongs
# to the separate read helpers below and must not change the write target.
# 函数用途: 返回一个 owner 或任务根下唯一的工具输出写入目录。
def tool_output_root(root: str | Path) -> Path:
    return Path(root) / "blobs" / "tool_outputs"


# LLM: 查找覆盖新 runs 与原处保留的旧 tasks 索引，保持两层有界遍历；不改变归档写入身份。
# 函数用途: 枚举 owner 内可查找的工具输出目录，不读正文；切换运行布局不能丢失旧引用。
def tool_output_roots_for_lookup(root: str | Path) -> tuple[Path, ...]:
    base = Path(root)
    roots = [tool_output_root(base)]
    for namespace in ("runs", "tasks"):
        archive_root = base / namespace
        if archive_root.is_dir():
            roots.extend(sorted(archive_root.glob("*/*/work/blobs/tool_outputs")))
    return _unique_paths(roots)


# LLM: Callers scan metadata-only JSONL indexes, never artifact bodies, when verifying refs.
# 函数用途: 将 owner 范围内的工具输出目录投影成规范 index.jsonl 路径。
def tool_output_index_paths_for_lookup(root: str | Path) -> tuple[Path, ...]:
    return tuple(item / "index.jsonl" for item in tool_output_roots_for_lookup(root))


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
