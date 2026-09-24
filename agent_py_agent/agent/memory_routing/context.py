
from __future__ import annotations

"""LLM contract: build runtime prompt context from deterministic memory rule routes.

新手说明:
这里是长期规则路由的'读取服务层'：主循环以后可以调用它拿到匹配证据、待读路径、
读取小票和可注入 prompt 的短正文，但它不修改主循环、不写规则文件、不调用模型。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._context_paths import (
    _append_finding,
    _read_targets_from_matches,
    _ReadTarget,
    _resolve_relative_path,
    _resolve_root,
)
from ._context_reading import (
    _build_injected_section,
    _finish_receipt,
    _new_receipt,
    _read_authority_file,
)
from .loader import load_memory_routes
from .matcher import match_routes
from .models import MemoryRouteMatch
from .validator import validate_routes

VALID_CONTEXT_MODES = {"soft", "strict"}


# LLM: 本轮记忆路由与正式召回的过程事实；findings 只放结构化码，supplement_entry_ids 只记召回前补充查询真正追加的
#   正式记录编号，供上下文包落观察证据，不参与召回、权限或预算判定。
# 类用途: 汇总一轮的记忆路由命中、注入段落、回执、发现码与补充召回追加的记录编号。
@dataclass
class RoutedMemoryContext:

    enabled: bool
    index_path: str
    routes_count: int = 0
    matches: list[dict[str, Any]] = field(default_factory=list)
    required_read_paths: list[str] = field(default_factory=list)
    candidate_paths: list[str] = field(default_factory=list)
    injected_sections: list[str] = field(default_factory=list)
    receipts: list[dict[str, Any]] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    supplement_entry_ids: list[str] = field(default_factory=list)


@dataclass
class RouteContextOptions:
    """Bundle for build_routed_memory_context keyword parameters."""

    enabled: bool = True
    index_path: str = "memory/routing/INDEX.md"
    mode: str = "soft"
    auto_read_limit: int = 3
    limit: int = 5
    max_chars_per_file: int = 4000


def build_routed_memory_context(
    root: str | Path,
    query: str,
    *,
    options: RouteContextOptions | None = None,
) -> RoutedMemoryContext:
    """Route a query to safe memory authority reads and prompt sections."""

    _opts = options or RouteContextOptions()
    context = RoutedMemoryContext(enabled=_opts.enabled, index_path=str(_opts.index_path))
    if not _opts.enabled:
        return context

    resolved_root = _prepare_root(context, root)
    normalized_mode = _normalize_context_mode(context, _opts.mode)
    if resolved_root is None or not normalized_mode:
        return context

    index_file = _resolve_index_file(context, resolved_root, str(_opts.index_path))
    if index_file is None:
        return context
    routes = _load_routes(context, index_file)
    if routes is None:
        return context

    context.routes_count = len(routes)
    for finding in validate_routes(routes, resolved_root):
        _append_finding(context.findings, finding)

    matches = match_routes(query, routes, limit=_opts.limit)
    context.matches = [_match_to_dict(match) for match in matches]

    targets = _read_targets_from_matches(matches, resolved_root, context.findings)
    context.candidate_paths = [target.path for target in targets]
    if normalized_mode == "strict":
        read_targets = targets[: max(_opts.auto_read_limit, 0)]
        context.required_read_paths = [target.path for target in read_targets]
    else:
        read_targets = targets[: max(_opts.auto_read_limit, 0)]

    if _opts.auto_read_limit <= 0:
        return context

    _read_target_sections(context, read_targets, max_chars_per_file=_opts.max_chars_per_file)
    return context


def _read_target_sections(
    context: RoutedMemoryContext,
    read_targets: list[_ReadTarget],
    *,
    max_chars_per_file: int,
) -> None:
    for target in read_targets:
        section, receipt = _read_authority_file(target, max_chars_per_file=max_chars_per_file)
        context.receipts.append(receipt)
        if section:
            context.injected_sections.append(section)


def _prepare_root(context: RoutedMemoryContext, root: str | Path) -> Path | None:
    """Resolve root and attach a finding instead of raising."""
    resolved_root, root_error = _resolve_root(root)
    if root_error:
        context.findings.append(root_error)
        return None
    return resolved_root


def _normalize_context_mode(context: RoutedMemoryContext, mode: str) -> str:
    """Normalize route context mode and record invalid values."""
    normalized_mode = mode.strip().lower()
    if normalized_mode in VALID_CONTEXT_MODES:
        return normalized_mode
    context.findings.append(
        f"memory route mode must be one of {sorted(VALID_CONTEXT_MODES)}, got {mode!r}"
    )
    return ""


def _resolve_index_file(context: RoutedMemoryContext, root: Path, index_path: str) -> Path | None:
    """Resolve and validate the configured route index file."""
    index_file, normalized_index, index_error = _resolve_relative_path(root, index_path, label="index_path")
    context.index_path = normalized_index or index_path
    if index_error:
        context.findings.append(index_error)
        return None
    if index_file is None:
        context.findings.append("index_path cannot be resolved")
        return None
    if not index_file.exists():
        context.findings.append(f"memory route index does not exist: {context.index_path}")
        return None
    if not index_file.is_file():
        context.findings.append(f"memory route index is not a file: {context.index_path}")
        return None
    return index_file


def _load_routes(context: RoutedMemoryContext, index_file: Path):
    """Load routes and convert parser errors into context findings."""
    try:
        return load_memory_routes(index_file)
    except ValueError as exc:
        context.findings.append(str(exc))
        return None


def _match_to_dict(match: MemoryRouteMatch) -> dict[str, Any]:

    route = match.route
    return {
        "route_id": route.route_id,
        "topic": route.topic,
        "authority_path": route.authority_file(),
        "inject_mode": route.inject_mode,
        "scope": route.scope,
        "priority": route.priority,
        "score": match.score,
        "matched_terms": list(match.matched_terms),
        "reasons": list(match.reasons),
    }
