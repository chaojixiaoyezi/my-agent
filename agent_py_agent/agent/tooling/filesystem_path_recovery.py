# LLM: Missing-path recovery is a bounded convenience layer, never an implicit full-workspace
# search. Keep ordinary file-tool failures fast; exhaustive discovery belongs to explicit tools.
# 模块用途: 给路径不存在错误补充少量安全候选，同时保证大型工作区里也能迅速返回。
from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import ToolHandlerOutcome

_DISCOVERY_IGNORES = frozenset({
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "venv",
})

_MAX_VISITED_PER_REQUEST = 1_024
_MAX_DIRECTORIES_PER_REQUEST = 128
_MAX_ENTRIES_PER_DIRECTORY = 256
_MAX_DISCOVERY_DEPTH = 8


# LLM: One budget is shared by parent probing and every workspace root so multiple roots cannot
# multiply recovery work. This state is request-local and must never be persisted or reused.
# 类用途: 记录一次缺失路径候选搜索还允许查看多少目录项和目录。
@dataclass
class CandidateScanBudget:
    remaining_entries: int = _MAX_VISITED_PER_REQUEST
    remaining_directories: int = _MAX_DIRECTORIES_PER_REQUEST


@dataclass(frozen=True)
class MissingPathRequest:
    tool_name: str
    raw_path: str
    target: Path
    workspace_roots: list[Path]
    display_path: str
    expected_kind: str = "any"
    retry_tool: str = ""


@dataclass(frozen=True)
class CandidateQuery:
    raw_name: str
    target: Path
    expected_kind: str


@dataclass(frozen=True)
class MissingPathRecovery:
    requested_path: str
    resolved_path: str
    display_path: str
    candidate_paths: tuple[str, ...]
    expected_kind: str

    def envelope(self) -> dict[str, Any]:
        return {
            "path_not_found": True,
            "requested_path": self.requested_path,
            "resolved_path": self.resolved_path,
            "display_path": self.display_path,
            "expected_kind": self.expected_kind,
            "candidate_paths": list(self.candidate_paths),
            "next_actions": [
                "如果候选路径符合目标，请用对应读取工具重新读取确认。",
                "如果候选都不对，请先用 list_files 或 search_text 缩小范围。",
            ],
        }


def missing_path_result(request: MissingPathRequest) -> ToolHandlerOutcome:
    candidates = suggest_missing_path_candidates(
        raw_path=request.raw_path,
        target=request.target,
        workspace_roots=request.workspace_roots,
        expected_kind=request.expected_kind,
    )
    recovery = MissingPathRecovery(
        requested_path=str(request.raw_path),
        resolved_path=str(request.target),
        display_path=request.display_path,
        candidate_paths=tuple(str(candidate) for candidate in candidates),
        expected_kind=request.expected_kind,
    )
    return ToolHandlerOutcome(
        request.tool_name,
        False,
        _render_missing_path(recovery, retry_tool=request.retry_tool or request.tool_name),
        result_envelope=recovery.envelope(),
        error_code="PATH_NOT_FOUND",
    )


# LLM: Candidate discovery is intentionally best-effort and bounded. A miss without candidates
# is valid; callers direct the model to explicit list/search tools instead of blocking the turn.
# 函数用途: 在固定小预算内查找少量相似路径，找不到时立即返回空列表。
def suggest_missing_path_candidates(
    *,
    raw_path: str,
    target: Path,
    workspace_roots: list[Path],
    expected_kind: str = "any",
    limit: int = 5,
) -> list[Path]:
    roots = _normalized_roots(workspace_roots)
    raw_name = _raw_name(raw_path, target)
    if not raw_name:
        return []
    query = CandidateQuery(raw_name=raw_name, target=target, expected_kind=expected_kind)
    scored: dict[Path, int] = {}
    budget = CandidateScanBudget()
    _score_parent_siblings(scored, target.parent, query, roots, budget, limit=limit)
    for root in roots:
        if budget.remaining_entries <= 0 or budget.remaining_directories <= 0:
            break
        _score_tree_candidates(scored, root, query, budget, limit=limit)
        if _enough_exact_candidates(scored, limit) or budget.remaining_entries <= 0:
            break
    ranked = sorted(scored.items(), key=lambda item: (-item[1], len(item[0].parts), item[0].as_posix()))
    return [path for path, _score in ranked[:limit]]


def _render_missing_path(recovery: MissingPathRecovery, *, retry_tool: str) -> str:
    missing_label = "文件不存在" if recovery.expected_kind == "file" else "路径不存在"
    lines = [
        f"{missing_label}: {recovery.display_path}",
        "path_not_found=true",
        f"requested_path={recovery.requested_path}",
        f"expected_kind={recovery.expected_kind}",
    ]
    if recovery.candidate_paths:
        lines.append("candidate_paths:")
        lines.extend(f"- {path}" for path in recovery.candidate_paths)
        lines.append(f"如果候选符合目标，请用 {retry_tool} 重新读取确认；系统不会自动读取候选。")
    else:
        lines.append("candidate_paths=[]")
        lines.append("没有找到安全候选；请先用 list_files 或 search_text 在工作区内定位目标。")
    return "\n".join(lines)


# LLM: Parent probing uses the same request budget as tree discovery. Never materialize all
# children of a directory because a single generated or dependency directory may be enormous.
# 函数用途: 有上级目录时优先查看附近项目，但只读固定数量的目录项。
def _score_parent_siblings(
    scored: dict[Path, int],
    parent: Path,
    query: CandidateQuery,
    roots: list[Path],
    budget: CandidateScanBudget,
    *,
    limit: int,
) -> None:
    if (
        budget.remaining_entries <= 0
        or budget.remaining_directories <= 0
        or not parent.exists()
        or not parent.is_dir()
        or not _is_under_any_root(parent, roots)
    ):
        return
    budget.remaining_directories -= 1
    try:
        iterator = os.scandir(parent)
    except OSError:
        return
    with iterator:
        for index, entry in enumerate(iterator):
            if index >= _MAX_ENTRIES_PER_DIRECTORY or budget.remaining_entries <= 0:
                break
            budget.remaining_entries -= 1
            if entry.name in _DISCOVERY_IGNORES:
                continue
            _score_candidate(scored, Path(entry.path), query, bonus=10)
            if _enough_exact_candidates(scored, limit):
                break


# LLM: A breadth-first traversal gives nearby task artifacts a chance before entering a large
# repository. It shares a global budget and exits as soon as enough exact candidates exist.
# 函数用途: 在工作区浅层按广度优先寻找候选，避免深陷第一个大型仓库。
def _score_tree_candidates(
    scored: dict[Path, int],
    root: Path,
    query: CandidateQuery,
    budget: CandidateScanBudget,
    *,
    limit: int,
) -> None:
    for item in _walk_candidate_items(root, query.expected_kind, budget):
        _score_candidate(scored, item, query, bonus=_part_overlap(query.target, item))
        if _enough_exact_candidates(scored, limit):
            return


# LLM: Traversal must not follow directory symlinks and must cap total entries, directories,
# per-directory fanout and depth. These are hard latency bounds, not search-quality promises.
# 函数用途: 以小预算遍历可能的候选项，工作区再大也不会无限扫描。
def _walk_candidate_items(
    root: Path,
    expected_kind: str,
    budget: CandidateScanBudget,
):
    include_dirs = expected_kind in {"directory", "any"}
    include_files = expected_kind in {"file", "any"}
    pending: deque[tuple[Path, int]] = deque([(root, 0)])
    while pending and budget.remaining_entries > 0 and budget.remaining_directories > 0:
        current, depth = pending.popleft()
        budget.remaining_directories -= 1
        try:
            iterator = os.scandir(current)
        except OSError:
            continue
        with iterator:
            for index, entry in enumerate(iterator):
                if index >= _MAX_ENTRIES_PER_DIRECTORY or budget.remaining_entries <= 0:
                    break
                budget.remaining_entries -= 1
                if entry.name in _DISCOVERY_IGNORES:
                    continue
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    is_file = entry.is_file(follow_symlinks=False)
                except OSError:
                    continue
                item = Path(entry.path)
                if is_dir and depth < _MAX_DISCOVERY_DEPTH:
                    pending.append((item, depth + 1))
                if (include_dirs and is_dir) or (include_files and is_file):
                    yield item


def _score_candidate(
    scored: dict[Path, int],
    item: Path,
    query: CandidateQuery,
    *,
    bonus: int = 0,
) -> None:
    if not _kind_matches(item, query.expected_kind):
        return
    score = _name_score(item.name, query.raw_name)
    if score <= 0:
        return
    resolved = item.resolve(strict=False)
    scored[resolved] = max(scored.get(resolved, 0), score + bonus)


def _name_score(candidate_name: str, raw_name: str) -> int:
    candidate = candidate_name.lower()
    raw = raw_name.lower()
    if candidate == raw:
        return 100
    candidate_stem = Path(candidate_name).stem.lower()
    raw_stem = Path(raw_name).stem.lower()
    if candidate_stem and candidate_stem == raw_stem:
        return 90
    if candidate.startswith(raw) or raw.startswith(candidate):
        return 75
    if raw in candidate or candidate in raw:
        return 65
    return 0


def _part_overlap(target: Path, candidate: Path) -> int:
    ignored = {"", ".", "/", "data", "tasks", "agents", "outputs", "workspace"}
    left = {part.lower() for part in target.parts if part.lower() not in ignored}
    right = {part.lower() for part in candidate.parts if part.lower() not in ignored}
    return min(20, len(left & right) * 4)


def _enough_exact_candidates(scored: dict[Path, int], limit: int) -> bool:
    return sum(1 for score in scored.values() if score >= 100) >= limit


def _kind_matches(item: Path, expected_kind: str) -> bool:
    if expected_kind == "file":
        return item.is_file()
    if expected_kind == "directory":
        return item.is_dir()
    return item.exists()


def _raw_name(raw_path: str, target: Path) -> str:
    raw = str(raw_path or "").replace("\\", "/").rstrip("/")
    if raw:
        name = Path(raw).name
        if name:
            return name
    return target.name


def _normalized_roots(workspace_roots: list[Path]) -> list[Path]:
    roots: list[Path] = []
    seen: set[str] = set()
    for root in workspace_roots:
        resolved = Path(root).resolve(strict=False)
        key = str(resolved)
        if key in seen or not resolved.exists() or not resolved.is_dir():
            continue
        seen.add(key)
        roots.append(resolved)
    return roots


def _is_under_any_root(path: Path, roots: list[Path]) -> bool:
    resolved = path.resolve(strict=False)
    for root in roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False
