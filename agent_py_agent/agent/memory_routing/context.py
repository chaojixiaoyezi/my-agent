from __future__ import annotations

"""LLM contract: build runtime prompt context from deterministic memory rule routes.

这里是长期规则路由的“读取服务层”：主循环以后可以调用它拿到匹配证据、待读路径、
读取小票和可注入 prompt 的短正文，但它不修改主循环、不写规则文件、不调用模型。
"""

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .loader import load_memory_routes
from .matcher import match_routes
from .models import MemoryRouteMatch
from .validator import validate_routes

VALID_CONTEXT_MODES = {"soft", "strict"}


@dataclass
class RoutedMemoryContext:
    """LLM contract: returned runtime bundle for routed memory rule context.

    大白话：这是主循环可以直接消费的一包结果：哪些 route 命中了、哪些文件候选、
    哪些正文已经安全读入、每次读取有没有成功，以及有什么诊断信息。
    """

    enabled: bool
    index_path: str
    routes_count: int = 0
    matches: list[dict[str, Any]] = field(default_factory=list)
    required_read_paths: list[str] = field(default_factory=list)
    candidate_paths: list[str] = field(default_factory=list)
    injected_sections: list[str] = field(default_factory=list)
    receipts: list[dict[str, Any]] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)


@dataclass
class _ReadTarget:
    """LLM contract: internal representation of one safe authority file read target.

    大白话：外部只需要字符串路径；内部读取时还要保留解析后的绝对路径、
    route_id 和命中理由，方便生成 receipt。
    """

    route_id: str
    path: str
    absolute_path: Path
    reasons: list[str] = field(default_factory=list)


def build_routed_memory_context(
    root: str | Path,
    query: str,
    *,
    enabled: bool = True,
    index_path: str = "memory/routing/INDEX.md",
    mode: str = "soft",
    auto_read_limit: int = 3,
    limit: int = 5,
    max_chars_per_file: int = 4000,
) -> RoutedMemoryContext:
    """LLM contract: routes a query to memory authority files and safely reads context.

    大白话：给它项目根目录和用户输入，它先找 index，再做确定性匹配；soft 模式读前几个候选，
    strict 模式把前几个候选标成 required 并读取。所有路径都会卡在 root 里面。
    """

    context = RoutedMemoryContext(enabled=enabled, index_path=str(index_path))
    if not enabled:
        return context

    resolved_root, root_error = _resolve_root(root)
    if root_error:
        context.findings.append(root_error)
        return context

    normalized_mode = mode.strip().lower()
    if normalized_mode not in VALID_CONTEXT_MODES:
        context.findings.append(
            f"memory route mode must be one of {sorted(VALID_CONTEXT_MODES)}, got {mode!r}"
        )
        return context

    index_file, normalized_index, index_error = _resolve_relative_path(
        resolved_root,
        str(index_path),
        label="index_path",
    )
    context.index_path = normalized_index or str(index_path)
    if index_error:
        context.findings.append(index_error)
        return context
    if index_file is None:
        context.findings.append("index_path cannot be resolved")
        return context
    if not index_file.exists():
        context.findings.append(f"memory route index does not exist: {context.index_path}")
        return context
    if not index_file.is_file():
        context.findings.append(f"memory route index is not a file: {context.index_path}")
        return context

    try:
        routes = load_memory_routes(index_file)
    except ValueError as exc:
        context.findings.append(str(exc))
        return context

    context.routes_count = len(routes)
    for finding in validate_routes(routes, resolved_root):
        _append_finding(context.findings, finding)

    matches = match_routes(query, routes, limit=limit)
    context.matches = [_match_to_dict(match) for match in matches]

    targets = _read_targets_from_matches(matches, resolved_root, context.findings)
    context.candidate_paths = [target.path for target in targets]
    if normalized_mode == "strict":
        read_targets = targets[: max(auto_read_limit, 0)]
        context.required_read_paths = [target.path for target in read_targets]
    else:
        read_targets = targets[: max(auto_read_limit, 0)]

    if auto_read_limit <= 0:
        return context

    for target in read_targets:
        section, receipt = _read_authority_file(target, max_chars_per_file=max_chars_per_file)
        context.receipts.append(receipt)
        if section:
            context.injected_sections.append(section)
    return context


def _resolve_root(root: str | Path) -> tuple[Path | None, str]:
    """LLM contract: resolves the configured project root before any index or rule reads.

    大白话：所有后续路径都必须以这个 root 为边界；root 自己解析失败时，
    服务直接返回 finding，避免在未知目录下继续读文件。
    """

    root_path = Path(root)
    try:
        resolved = root_path.resolve()
    except OSError as exc:
        return None, f"root path cannot be resolved: {root_path} ({exc})"
    if not resolved.exists():
        return None, f"root path does not exist: {root_path}"
    if not resolved.is_dir():
        return None, f"root path is not a directory: {root_path}"
    return resolved, ""


def _resolve_relative_path(root: Path, raw_path: str, *, label: str) -> tuple[Path | None, str, str]:
    """LLM contract: resolves a caller-provided relative path without escaping root.

    大白话：index_path 和 authority_path 都走这里。绝对路径、`..` 越界、
    符号链接越界都会被挡住，所以读取层不会碰到项目根目录外的文件。
    """

    cleaned = raw_path.strip()
    if not cleaned:
        return None, "", f"{label} is empty"
    candidate = Path(cleaned)
    if candidate.is_absolute():
        return None, "", f"{label} must be relative, got {cleaned}"
    try:
        resolved = (root / candidate).resolve()
    except OSError as exc:
        return None, "", f"{label} cannot be resolved: {cleaned} ({exc})"
    try:
        relative = resolved.relative_to(root)
    except ValueError:
        return None, "", f"{label} escapes root: {cleaned}"
    return resolved, relative.as_posix(), ""


def _read_targets_from_matches(
    matches: list[MemoryRouteMatch],
    root: Path,
    findings: list[str],
) -> list[_ReadTarget]:
    """LLM contract: converts matched routes into unique safe read targets.

    大白话：多个 route 可能指向同一个 authority file，这里只保留第一次命中的路径；
    不安全路径只进入 findings，不会进入 candidate_paths 或读取队列。
    """

    targets: list[_ReadTarget] = []
    seen_paths: set[str] = set()
    for match in matches:
        raw_path = match.route.authority_path.strip()
        if not raw_path:
            continue
        absolute_path, normalized_path, error = _resolve_relative_path(
            root,
            raw_path,
            label="authority_path",
        )
        if error:
            _append_finding(findings, _authority_path_finding(match, error, raw_path))
            continue
        if absolute_path is None or normalized_path in seen_paths:
            continue
        seen_paths.add(normalized_path)
        targets.append(
            _ReadTarget(
                route_id=match.route.route_id,
                path=normalized_path,
                absolute_path=absolute_path,
                reasons=list(match.reasons),
            )
        )
    return targets


def _read_authority_file(
    target: _ReadTarget,
    *,
    max_chars_per_file: int,
) -> tuple[str, dict[str, Any]]:
    """LLM contract: reads one safe authority file and returns a prompt section plus receipt.

    大白话：真正打开文件只发生在这里。成功时正文会按字符数截断后进入 injected_sections；
    失败时只留下 receipt，调用方能审计原因但不会拿到坏正文。
    """

    started = time.perf_counter()
    receipt = _new_receipt(target, status="planned")
    try:
        if not target.absolute_path.exists():
            receipt["status"] = "missing"
            receipt["error"] = f"authority file does not exist: {target.path}"
            return "", _finish_receipt(receipt, started)
        if not target.absolute_path.is_file():
            receipt["status"] = "not_file"
            receipt["error"] = f"authority path is not a file: {target.path}"
            return "", _finish_receipt(receipt, started)
        content = target.absolute_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        receipt["status"] = "error"
        receipt["error"] = str(exc)
        return "", _finish_receipt(receipt, started)

    receipt["status"] = "read"
    receipt["content_hash"] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    section = _build_injected_section(target.path, content, max_chars_per_file=max_chars_per_file)
    return section, _finish_receipt(receipt, started)


def _build_injected_section(path: str, content: str, *, max_chars_per_file: int) -> str:
    """LLM contract: formats bounded authority text for prompt injection.

    大白话：注入文本必须带来源路径，正文最多放 `max_chars_per_file` 个字符；
    超出部分只写省略提示，避免一条规则文件把 prompt 撑爆。
    """

    limit = max(max_chars_per_file, 0)
    truncated = len(content) > limit
    body = content[:limit]
    suffix = ""
    if truncated:
        suffix = f"\n\n[truncated: {len(content) - limit} chars omitted]"
    return f"### Routed memory authority: {path}\n\n{body}{suffix}".strip()


def _match_to_dict(match: MemoryRouteMatch) -> dict[str, Any]:
    """LLM contract: serializes one route match into plain JSON-friendly data.

    大白话：运行时、日志和测试不需要 dataclass 对象；这里把关键证据摊平成 dict，
    保留 route_id、路径、分数和命中理由。
    """

    route = match.route
    return {
        "route_id": route.route_id,
        "topic": route.topic,
        "authority_path": route.authority_path,
        "scope": route.scope,
        "priority": route.priority,
        "score": match.score,
        "matched_terms": list(match.matched_terms),
        "reasons": list(match.reasons),
    }


def _new_receipt(target: _ReadTarget, *, status: str) -> dict[str, Any]:
    """LLM contract: creates the stable receipt shape required by runtime callers.

    大白话：无论读取成功还是失败，小票字段都一致，后续日志和断言不用到处判空。
    """

    return {
        "route_id": target.route_id,
        "path": target.path,
        "status": status,
        "content_hash": "",
        "elapsed_ms": 0.0,
        "error": "",
        "reasons": list(target.reasons),
    }


def _finish_receipt(receipt: dict[str, Any], started: float) -> dict[str, Any]:
    """LLM contract: records elapsed time for a read attempt.

    大白话：耗时由读取函数统一收口，成功、缺文件和异常都会留下同一格式的毫秒数。
    """

    receipt["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return receipt


def _authority_path_finding(match: MemoryRouteMatch, error: str, raw_path: str) -> str:
    """LLM contract: formats authority path safety findings like the route validator.

    大白话：context 层和 doctor 层的报错尽量长得一样，人看日志时不用学习两套说法。
    """

    label = match.route.route_id or "<empty route_id>"
    if "must be relative" in error:
        return f"route '{label}': authority_path must be relative, got {raw_path}"
    if "escapes root" in error:
        return f"route '{label}': authority_path escapes root: {raw_path}"
    if "cannot be resolved" in error:
        return f"route '{label}': authority_path cannot be resolved ({error})"
    return f"route '{label}': {error}"


def _append_finding(findings: list[str], finding: str) -> None:
    """LLM contract: appends a diagnostic once while preserving first-seen order.

    大白话：validator 和 runtime safety check 可能发现同一个问题；去重后输出更安静，
    但仍然保留第一次出现的位置。
    """

    if finding and finding not in findings:
        findings.append(finding)
