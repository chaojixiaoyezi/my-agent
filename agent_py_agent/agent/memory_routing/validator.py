from __future__ import annotations

"""LLM contract: validation helpers for memory routing indexes and authority paths.

这里像一个小 doctor：它不改文件，只把索引里明显危险或不可用的问题列出来，
比如 route_id 重复、没有触发词、正式规则文件不存在。
"""

from pathlib import Path

from .models import MemoryRoute

VALID_INJECT_MODES = {"always", "on_hit", "never"}


def validate_routes(routes: list[MemoryRoute], root: str | Path) -> list[str]:
    """LLM contract: returns human-readable findings for invalid route indexes.

    大白话：给它一组 route 和项目根目录，它会逐条检查。返回的是人能看懂的中文/英文混合诊断，
    后续 CLI 可以直接打印；没有问题就返回空列表。
    """

    findings: list[str] = []
    seen_route_ids: dict[str, int] = {}
    keyword_owners: dict[str, str] = {}
    root_path = Path(root)
    try:
        resolved_root = root_path.resolve()
    except OSError as exc:
        return [f"root path cannot be resolved: {root_path} ({exc})"]

    for idx, route in enumerate(routes):
        label = route.route_id or f"<empty route_id at #{idx}>"
        route_id = route.route_id.strip()
        if not route_id:
            findings.append(f"route #{idx}: route_id is empty")
        elif route_id in seen_route_ids:
            findings.append(
                f"route '{route_id}': duplicate route_id, first seen at #{seen_route_ids[route_id]}"
            )
        else:
            seen_route_ids[route_id] = idx

        if not route.topic.strip():
            findings.append(f"route '{label}': topic is empty")
        if not route.trigger_keywords and not route.aliases:
            findings.append(f"route '{label}': trigger_keywords and aliases are both empty")
        if route.inject_mode not in VALID_INJECT_MODES:
            findings.append(f"route '{label}': inject_mode must be one of {sorted(VALID_INJECT_MODES)}")
        seen_terms: set[str] = set()
        for term in [*route.trigger_keywords, *route.aliases]:
            normalized = term.strip().lower()
            if not normalized:
                continue
            if normalized in seen_terms:
                findings.append(f"route '{label}': duplicate keyword '{term}'")
                continue
            seen_terms.add(normalized)
            owner = keyword_owners.get(normalized)
            if owner and owner != label:
                findings.append(f"route keyword conflict '{term}': routes '{owner}' and '{label}'")
            else:
                keyword_owners[normalized] = label
        if not route.authority_file():
            findings.append(f"route '{label}': authority_path/source_file is empty")
            continue
        _validate_authority_path(route, resolved_root, findings)
    return findings


def _validate_authority_path(
    route: MemoryRoute,
    resolved_root: Path,
    findings: list[str],
) -> None:
    """LLM contract: validates one route authority path against the project root.

    大白话：正式规则文件必须在项目根目录里面，并且文件要真的存在。这样能防止索引写错路径，
    也避免未来自动读取规则时读到项目外面的奇怪文件。
    """

    raw_path = route.authority_file()
    route_label = route.route_id or "<empty route_id>"
    candidate = Path(raw_path)
    if candidate.is_absolute():
        findings.append(f"route '{route_label}': source_file must be relative, got {raw_path}")
        return
    try:
        resolved_candidate = (resolved_root / candidate).resolve()
    except OSError as exc:
        findings.append(f"route '{route_label}': source_file cannot be resolved ({exc})")
        return
    if not resolved_candidate.is_relative_to(resolved_root):
        findings.append(f"route '{route_label}': source_file escapes root: {raw_path}")
        return
    if not resolved_candidate.exists():
        findings.append(f"route '{route_label}': authority_path does not exist: {raw_path}")
    elif not resolved_candidate.is_file():
        findings.append(f"route '{route_label}': authority_path is not a file: {raw_path}")
