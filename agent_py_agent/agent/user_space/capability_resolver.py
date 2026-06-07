from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..common.path_segments import safe_path_segment
from .capability_index import (
    CapabilityIndexRecordsReport,
    dedupe_capability_index_load_errors,
    read_capability_index_records,
)
from .home_layout import MyAgentHomePaths
from .owner_policy import EffectiveOwnerPolicy, resolve_effective_owner_policy


@dataclass(frozen=True)
class CapabilityResolveResult:
    name: str
    kind: str
    status: str
    resolved_id: str = ""
    source: str = ""
    path: str = ""
    candidates: tuple[dict[str, object], ...] = ()
    cache_path: Path | None = None
    index_load_errors: tuple[dict[str, object], ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "resolved_id": self.resolved_id,
            "source": self.source,
            "path": self.path,
            "candidates": list(self.candidates),
            "cache_path": str(self.cache_path) if self.cache_path else "",
            "index_load_errors": list(self.index_load_errors),
        }

@dataclass(frozen=True)
class CapabilityResolveOptions:
    kind: str = "skill"
    run_id: str = ""
    policy: EffectiveOwnerPolicy | None = None


@dataclass(frozen=True)
class _RevokedCheckReport:
    result: CapabilityResolveResult | None
    load_errors: list[dict[str, object]]


@dataclass(frozen=True)
class _CandidateRecordsReport:
    records: list[dict[str, object]]
    load_errors: list[dict[str, object]]


def resolve_owner_capability(
    home: MyAgentHomePaths,
    name: str,
    options: CapabilityResolveOptions | None = None,
) -> CapabilityResolveResult:
    opts = options or CapabilityResolveOptions()
    capability_name = str(name or "").strip()
    capability_kind = str(opts.kind or "skill").strip() or "skill"
    if not capability_name:
        return CapabilityResolveResult(name="", kind=capability_kind, status="missing_name")
    revoked = _revoked_result_report(home, capability_name, kind=capability_kind)
    if revoked.result is not None:
        return replace(revoked.result, index_load_errors=tuple(revoked.load_errors))
    cache_path = _cache_path(home, run_id=opts.run_id, name=capability_name, kind=capability_kind)
    cached = _read_cached_result(cache_path)
    if cached is not None:
        return replace(cached, index_load_errors=tuple(revoked.load_errors))
    effective = opts.policy or resolve_effective_owner_policy(home)
    candidates = _candidate_records_report(home, capability_name, kind=capability_kind, policy=effective)
    load_errors = dedupe_capability_index_load_errors([*revoked.load_errors, *candidates.load_errors])
    result = _result_from_candidates(capability_name, capability_kind, candidates.records, cache_path)
    result = replace(result, index_load_errors=tuple(load_errors))
    _write_cache(cache_path, result)
    return result


def _candidate_records_report(
    home: MyAgentHomePaths,
    name: str,
    *,
    kind: str,
    policy: EffectiveOwnerPolicy,
) -> _CandidateRecordsReport:
    records: list[dict[str, object]] = []
    load_errors: list[dict[str, object]] = []
    if _source_enabled(policy, "owner"):
        owner = _owner_records_report(home, name, kind=kind)
        records.extend(owner.records)
        load_errors.extend(owner.load_errors)
    if _source_enabled(policy, "shared"):
        shared = read_capability_index_records(_shared_index_path(home, kind), name, source="shared")
        records.extend(shared.records)
        load_errors.extend(shared.load_errors)
    if _source_enabled(policy, "builtin"):
        records.extend(_builtin_record(name, kind=kind))
    return _CandidateRecordsReport(_dedupe_candidates(records), load_errors)


def _owner_records_report(home: MyAgentHomePaths, name: str, *, kind: str) -> CapabilityIndexRecordsReport:
    root = Path(home.owner_home_dir) / _kind_dir(kind)
    records: list[dict[str, object]] = []
    for path in [root / name, root / ".drafts" / name]:
        if path.exists():
            records.append(_record(name, kind=kind, source="owner", path=path))
    draft_index = root / ".drafts" / f"{kind}_candidates.jsonl"
    draft = read_capability_index_records(draft_index, name, source="owner_draft")
    records.extend(draft.records)
    return CapabilityIndexRecordsReport(records, draft.load_errors)


def _revoked_result_report(home: MyAgentHomePaths, name: str, *, kind: str) -> _RevokedCheckReport:
    shared = read_capability_index_records(_shared_index_path(home, kind), name, source="shared")
    draft = read_capability_index_records(
        Path(home.owner_home_dir) / _kind_dir(kind) / ".drafts" / f"{kind}_candidates.jsonl",
        name,
        source="owner_draft",
    )
    records = [*shared.records, *draft.records]
    load_errors = [*shared.load_errors, *draft.load_errors]
    for row in records:
        status = str(row.get("status") or "").strip()
        if status in {"revoked", "disabled", "revoked_for_security"}:
            return _RevokedCheckReport(
                CapabilityResolveResult(
                    name=name,
                    kind=kind,
                    status="revoked",
                    resolved_id=str(row.get("id") or row.get("name") or name),
                    source=str(row.get("source") or ""),
                    path=str(row.get("path") or ""),
                    candidates=tuple(records),
                ),
                load_errors,
            )
    return _RevokedCheckReport(None, load_errors)


def _builtin_record(name: str, *, kind: str) -> list[dict[str, object]]:
    return [_record(name, kind=kind, source="builtin", path="")]


def _result_from_candidates(
    name: str,
    kind: str,
    candidates: list[dict[str, object]],
    cache_path: Path | None,
) -> CapabilityResolveResult:
    if not candidates:
        return CapabilityResolveResult(name=name, kind=kind, status="not_found", cache_path=cache_path)
    preferred = _highest_priority_candidates(candidates)
    if len(preferred) == 1:
        chosen = preferred[0]
        return CapabilityResolveResult(
            name=name,
            kind=kind,
            status="resolved",
            resolved_id=str(chosen.get("id") or chosen.get("name") or name),
            source=str(chosen.get("source") or ""),
            path=str(chosen.get("path") or ""),
            candidates=tuple(candidates),
            cache_path=cache_path,
        )
    if len(candidates) > 1:
        return CapabilityResolveResult(
            name=name,
            kind=kind,
            status="ambiguous",
            candidates=tuple(candidates),
            cache_path=cache_path,
        )
    chosen = candidates[0]
    return CapabilityResolveResult(
        name=name,
        kind=kind,
        status="resolved",
        resolved_id=str(chosen.get("id") or chosen.get("name") or name),
        source=str(chosen.get("source") or ""),
        path=str(chosen.get("path") or ""),
        candidates=tuple(candidates),
        cache_path=cache_path,
    )


def _highest_priority_candidates(candidates: list[dict[str, object]]) -> list[dict[str, object]]:
    priority = {"owner": 0, "owner_draft": 1, "shared": 2, "builtin": 3}
    ranked = [(priority.get(str(row.get("source") or ""), 99), row) for row in candidates]
    best = min(score for score, _ in ranked)
    return [row for score, row in ranked if score == best]


def _source_enabled(policy: EffectiveOwnerPolicy, source: str) -> bool:
    sources = set(policy.enabled_skill_sources) | set(policy.enabled_tool_sources)
    return not sources or source in sources


def _shared_index_path(home: MyAgentHomePaths, kind: str) -> Path:
    if kind == "tool":
        return home.shared_indexes_tools_jsonl
    if kind == "workflow":
        return home.shared_indexes_workflows_jsonl
    if kind == "role_template":
        return home.shared_indexes_role_templates_jsonl
    return home.shared_indexes_skills_jsonl


def _kind_dir(kind: str) -> str:
    return {"tool": "tools", "workflow": "workflows", "role_template": "role_templates"}.get(kind, "skills")


def _record(name: str, *, kind: str, source: str, path: Path | str) -> dict[str, object]:
    return {"id": name, "name": name, "kind": kind, "source": source, "path": str(path)}


def _dedupe_candidates(records: list[dict[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in records:
        key = (str(row.get("source") or ""), str(row.get("id") or row.get("name") or ""), str(row.get("path") or ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def _cache_path(home: MyAgentHomePaths, *, run_id: str, name: str, kind: str) -> Path | None:
    if not run_id:
        return None
    safe_run = safe_path_segment(run_id, default="item", replacement="_")
    safe_name = safe_path_segment(name, default="item", replacement="_")
    return home.owner_memory_runtime_refs_dir / "capability_resolver" / safe_run / f"{kind}-{safe_name}.json"


def _read_cached_result(path: Path | None) -> CapabilityResolveResult | None:
    if path is None or not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return CapabilityResolveResult(
        name=str(payload.get("name") or ""),
        kind=str(payload.get("kind") or "skill"),
        status=str(payload.get("status") or ""),
        resolved_id=str(payload.get("resolved_id") or ""),
        source=str(payload.get("source") or ""),
        path=str(payload.get("path") or ""),
        candidates=tuple(item for item in payload.get("candidates", []) if isinstance(item, dict)),
        cache_path=path,
    )


def _write_cache(path: Path | None, result: CapabilityResolveResult) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = result.to_dict()
    payload["index_load_errors"] = []
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


__all__ = ["CapabilityResolveOptions", "CapabilityResolveResult", "resolve_owner_capability"]
