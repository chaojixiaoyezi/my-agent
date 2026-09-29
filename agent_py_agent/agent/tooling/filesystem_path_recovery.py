# LLM: Missing-path recovery is a bounded convenience layer, never an implicit full-workspace
# search. Keep ordinary file-tool failures fast; exhaustive discovery belongs to explicit tools.
# 模块用途: 给路径不存在错误补充少量安全候选，同时保证大型工作区里也能迅速返回。
from __future__ import annotations

import itertools
import os
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
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

# LLM: Near-name suggestions are a separate, narrower feature from tree discovery: they only run
# when the parent directory exists, they never leave that one directory, and they never rewrite
# the requested path. The caps are internal constants on purpose -- exposing them as user config
# would invite values that silently disable the feature or make every miss scan a huge directory.
# 常量用途: 同一目录内"写错文件名"提示的距离上限、建议条数与目录条目上限。
_NEAR_NAME_DISTANCE_LIMIT = 2
_NEAR_NAME_MAX_SUGGESTIONS = 2
# 按单次查找耗时实测取值（10000 条约 37ms，512 条约 2ms）；真实目录极少超过 133 条。
_NEAR_NAME_MAX_DIRECTORY_ENTRIES = 512
# LLM: Edit distance is O(len^2), so a very long name dominates the cost even inside a small
# directory. Skipping near-name matching for long names bounds the worst case without changing the
# algorithm (2026-09-28 review, N5). 64 is well above any realistic filename.
# 常量用途: 超过这个长度的名字不做近名匹配，把最坏耗时卡住（距离计算是 O(长度^2)）。
_NEAR_NAME_MAX_NAME_LENGTH = 64


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
    # 工具的路径裁决（通常是 tool.check_path_access）。返回 PathAccessDecision，AccessGate 读其
    # .allowed —— 那个 dataclass 没有 __bool__，不能用 bool() 判断（2026-09-28 复审）。
    decide_access: Callable[[Path], Any] | None = None


@dataclass(frozen=True)
class CandidateQuery:
    raw_name: str
    target: Path
    expected_kind: str


@dataclass(frozen=True)
class AccessGate:
    """把工具自己的路径裁决带进候选生成，让候选用和 resolve_path 完全一样的规则过审。

    `decide` 通常是 `check_path_access`：接收一个「已经解析过」的路径，返回
    `PathAccessDecision`。**必须读它的 `.allowed`**——那个 dataclass 没有定义 `__bool__`，
    用 `bool(decision)` 会恒为真、闸门形同虚设（2026-09-28 复审实测确认）。

    为 None 时不额外拦截（没有裁决能力的调用方仍受 workspace_roots 约束）。
    """

    decide: Callable[[Path], Any] | None = None

    def allows(self, path: Path) -> bool:
        if self.decide is None:
            return True
        try:
            decision = self.decide(path)
        except Exception:
            # 裁决本身出错时按"不允许"处理：宁可少给建议，也不能因为异常泄露路径。
            return False
        # 兼容两种返回：结构化裁决（读 .allowed）与直接返回布尔。
        allowed = getattr(decision, "allowed", None)
        return bool(allowed) if allowed is not None else bool(decision)


# LLM: Groups the "where may I look, and how many may I return" inputs of near-name lookup so the
# lookup itself takes few arguments. Frozen and request-local: never persisted or reused.
# 类用途: 把近名查找的授权根、返回条数与期望类型打包成一个只读输入。
@dataclass(frozen=True)
class NearNameScope:
    workspace_roots: list[Path]
    expected_kind: str = "any"
    limit: int = _NEAR_NAME_MAX_SUGGESTIONS
    # 工具自己的路径裁决；每个候选都过一遍，跟 resolve_path 用同一条规则。
    access: AccessGate = field(default_factory=AccessGate)

    def admits(self, entry: os.DirEntry) -> bool:
        """目录条目能否作为候选：类型相符 + 解析后的目标通过路径裁决。

        类型判断**跟随链接**：裁决看的是 resolve 之后的路径，所以指向工作区内部的合法链接
        可以放行，指向墙外的链接会被策略拒掉（不论目标是否存在）——不会重新变成探测口
        （2026-09-28 复审 B4）。
        """
        item = Path(entry.path)
        if not _kind_matches(item, self.expected_kind, follow_symlinks=True):
            return False
        return self.access.allows(item.resolve(strict=False))


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
        access=AccessGate(request.decide_access),
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
    access: AccessGate | None = None,
) -> list[Path]:
    roots = _normalized_roots(workspace_roots)
    raw_name = _raw_name(raw_path, target)
    if not raw_name:
        return []
    gate = access or AccessGate(None)
    query = CandidateQuery(raw_name=raw_name, target=target, expected_kind=expected_kind)
    scored: dict[Path, int] = {}
    budget = CandidateScanBudget()
    _score_parent_siblings(scored, target.parent, query, roots, budget, limit=limit, access=gate)
    for root in roots:
        if budget.remaining_entries <= 0 or budget.remaining_directories <= 0:
            break
        _score_tree_candidates(scored, root, query, budget, limit=limit, access=gate)
        if _enough_exact_candidates(scored, limit) or budget.remaining_entries <= 0:
            break
    ranked = sorted(scored.items(), key=lambda item: (-item[1], len(item[0].parts), item[0].as_posix()))
    candidates = [path for path, _score in ranked[:limit]]
    # 同目录"写错文件名"的提示排在最前：它比跨目录的相似路径更可能就是用户想读的那个。
    # 已经出现在候选里的路径不重复列出。
    near = suggest_near_name_paths(
        raw_name,
        target.parent,
        NearNameScope(workspace_roots=roots, expected_kind=expected_kind, access=gate),
    )
    seen = set(candidates)
    merged = [path for path in near if path not in seen]
    return (merged + candidates)[:limit]


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


# LLM: Contract: only called when the requested path does not exist. Returns near-name siblings
# from that one parent directory; never walks subdirectories and never returns a path outside the
# granted roots. Side effect: reads directory entries (no file contents) within a fixed cap.
# Callers: suggest_missing_path_candidates, which folds these into candidate_paths.
# 函数用途: 用户把文件名写错（少字/多字/串位）时，在同目录里找出最像的两个名字作为提示。
def suggest_near_name_paths(raw_name: str, parent: Path, scope: NearNameScope) -> list[Path]:
    if not raw_name or not parent.exists() or not parent.is_dir():
        return []
    # 超长名字直接不做近名匹配：编辑距离是 O(长度^2)，长名字会把最坏耗时拉起来，
    # 而真实文件名远短于这个上限（2026-09-28 复审 N5 的便宜解法）。
    if len(raw_name) > _NEAR_NAME_MAX_NAME_LENGTH:
        return []
    roots = _normalized_roots(scope.workspace_roots)
    # 越权目录直接不扫 —— 不能靠"相近文件名"泄露墙外有哪些文件。
    if not _is_under_any_root(parent, roots):
        return []
    entries = _scan_entries_within_cap(parent, _NEAR_NAME_MAX_DIRECTORY_ENTRIES)
    if entries is None:
        return []
    scored = _score_near_name_entries(entries, raw_name, parent, scope)
    scored.sort(key=lambda item: (item[0], item[1]))
    return [path for _distance, _name, path in scored[: scope.limit]]


# LLM: Streams at most ``cap + 1`` entries and gives up past the cap, so a huge directory is never
# fully materialized (same rule as the traversal helper in this module). Returns None to mean
# "stop, do not suggest" rather than an empty list, which would be indistinguishable from "nothing
# close enough".
# 函数用途: 最多读上限 +1 条目录项；超过上限就返回 None，表示"不扫了"。
def _scan_entries_within_cap(parent: Path, cap: int) -> list[os.DirEntry] | None:
    try:
        with os.scandir(parent) as iterator:
            entries = list(itertools.islice(iterator, cap + 1))
    except OSError:
        return None
    # 目录太大就完全不扫：宁可不给建议，也不让一次读错路径拖慢整轮。
    return entries if len(entries) <= cap else None


# LLM: One pass over the directory entries, keeping only names close enough and of the right kind.
#   Security: an entry is reported by its OWN path inside the granted directory, never by the path
#   its symlink resolves to -- resolving first would leak the existence and absolute location of
#   files in other owners' homes (2026-09-28 review, N1). Types are read with follow_symlinks=False
#   so a link to a missing target cannot be distinguished from a link to a private file.
# 函数用途: 挑出与写错名字足够接近的同目录条目，附带距离用于排序。
def _score_near_name_entries(
    entries: list[os.DirEntry],
    raw_name: str,
    parent: Path,
    scope: NearNameScope,
) -> list[tuple[int, str, Path]]:
    requested = Path(parent, raw_name).resolve(strict=False)
    scored: list[tuple[int, str, Path]] = []
    for entry in entries:
        if entry.name == raw_name or not scope.admits(entry):
            continue
        distance = _edit_distance_within(raw_name.lower(), entry.name.lower(), _NEAR_NAME_DISTANCE_LIMIT)
        if distance is None:
            continue
        # 报告条目自身路径（仍在授权目录内）；不 resolve，避免暴露链接目标。
        candidate = Path(entry.path)
        if candidate.resolve(strict=False) == requested:
            continue
        scored.append((distance, entry.name.lower(), candidate))
    return scored


# LLM: Reads an entry's type without following symlinks, so a link can never be classified by what
# it points at (which may live outside every granted root).
# 函数用途: 用 follow_symlinks=False 判断条目类型，不因链接目标而分类。
def _entry_kind_matches(entry: os.DirEntry, expected_kind: str) -> bool:
    try:
        if expected_kind == "file":
            return entry.is_file(follow_symlinks=False)
        if expected_kind == "directory":
            return entry.is_dir(follow_symlinks=False)
        return entry.is_file(follow_symlinks=False) or entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


# LLM: Returns None when the distance exceeds ``limit`` so callers can skip cheaply; the early
# exit matters because this runs once per directory entry. Two-row rolling array: the per-row
# "did anything stay within limit" check happens in a helper, so nesting stays at two levels.
#   Note for future edits: a banded (O(len * limit)) rewrite was attempted and rejected -- it ran
#   fast but disagreed with the naive DP on 1306/20000 random pairs. Speed here is bounded by the
#   per-directory entry cap instead, which is both simpler and verified.
# 函数用途: 算两个名字的编辑距离，超过上限就返回 None（省掉完整计算）。
def _edit_distance_within(left: str, right: str, limit: int) -> int | None:
    if left == right:
        return 0
    if abs(len(left) - len(right)) > limit:
        return None
    previous = list(range(len(right) + 1))
    for row_index, left_char in enumerate(left, 1):
        current = _next_distance_row(previous, left_char, right, row_index)
        if min(current) > limit:
            return None
        previous = current
    return previous[-1] if previous[-1] <= limit else None


# LLM: Pure helper for one row of the edit-distance matrix; extracting it keeps the caller at two
# nesting levels without changing the arithmetic.
# 函数用途: 由上一行算出编辑距离的下一行。
def _next_distance_row(previous: list[int], left_char: str, right: str, row_index: int) -> list[int]:
    current = [row_index]
    for column_index, right_char in enumerate(right, 1):
        substitute_cost = previous[column_index - 1] + (left_char != right_char)
        current.append(min(previous[column_index] + 1, current[column_index - 1] + 1, substitute_cost))
    return current


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
    access: AccessGate,
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
            _score_candidate(scored, Path(entry.path), query, bonus=10, access=access)
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
    access: AccessGate,
) -> None:
    for item in _walk_candidate_items(root, query.expected_kind, budget):
        _score_candidate(scored, item, query, bonus=_part_overlap(query.target, item), access=access)
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
    access: AccessGate,
) -> None:
    # 类型判断跟随链接（裁决看的是 resolve 后的路径，B4），但**报告时用条目自身路径**：
    # 把 resolve 结果写进候选会把别人家的绝对路径暴露出来。
    if not _kind_matches(item, query.expected_kind, follow_symlinks=True):
        return
    score = _name_score(item.name, query.raw_name)
    if score <= 0:
        return
    # 每个候选都过工具自己的权限裁决；不过审的直接丢弃（含指向墙外的链接）。
    if not access.allows(item.resolve(strict=False)):
        return
    scored[item] = max(scored.get(item, 0), score + bonus)


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


# LLM: Type checks default to NOT following symlinks. Following them would let a link inside a
# granted workspace report the type (and therefore the existence) of a file in another owner's
# home, because stat runs in the Gateway process rather than inside the tool sandbox.
# 函数用途: 判断路径类型；默认不跟随符号链接，避免用链接探测墙外文件。
def _kind_matches(item: Path, expected_kind: str, *, follow_symlinks: bool = True) -> bool:
    if expected_kind == "file":
        return item.is_file() if follow_symlinks else (item.is_file() and not item.is_symlink())
    if expected_kind == "directory":
        return item.is_dir() if follow_symlinks else (item.is_dir() and not item.is_symlink())
    if follow_symlinks:
        return item.exists()
    return item.exists() and not item.is_symlink()


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
