# LLM: Subagent workspace root helpers keep path contracts shared by runner gating and artifact checks.
# 模块用途: 从子代理运行目录推导真实项目工作区根目录，避免不同模块各自猜路径。

from __future__ import annotations

from pathlib import Path

_SUBAGENT_WORKSPACE_MARKERS = (
    ("data", "subagents"),
    (".my_agent", "subagents"),
    (".my-agent", "subagents"),
)


# LLM: derived_workspace_roots_from_subagent_path turns run-local paths into bounded project roots.
# 函数用途: 从 `/workspace/.my_agent/subagents/<run>` 或 `/workspace/data/subagents/<run>` 推导 `/workspace`；只返回路径候选，不扫描文件系统。
def derived_workspace_roots_from_subagent_path(path: Path) -> list[Path]:
    parts = Path(path).parts
    roots: list[Path] = []
    for index in _subagent_workspace_marker_indexes(parts):
        candidate = Path(*parts[:index])
        if _usable_workspace_root(candidate, path, roots):
            roots.append(candidate)
    return roots


# LLM: _subagent_workspace_marker_indexes recognizes stable internal subagent directory markers only.
# 函数用途: 定位 data/subagents、.my_agent/subagents、.my-agent/subagents 这几种受控目录边界。
def _subagent_workspace_marker_indexes(parts: tuple[str, ...]) -> list[int]:
    return [
        index
        for index in range(1, len(parts) - 2)
        if any(_matches_marker(parts, index, marker) for marker in _SUBAGENT_WORKSPACE_MARKERS)
    ]


# LLM: _matches_marker keeps marker matching exact and avoids broad "subagents" directory guesses.
# 函数用途: 判断 parts 从 index 开始是否精确匹配一个受控 marker。
def _matches_marker(parts: tuple[str, ...], index: int, marker: tuple[str, ...]) -> bool:
    return tuple(parts[index:index + len(marker)]) == marker


# LLM: _usable_workspace_root rejects filesystem roots, duplicates, and the original run path.
# 函数用途: 确认推导出的候选不是 `/`、`.`、原路径或已加入的重复项。
def _usable_workspace_root(candidate: Path, original: Path, roots: list[Path]) -> bool:
    if candidate == candidate.parent:
        return False
    return str(candidate) not in {"", "."} and candidate != original and candidate not in roots
