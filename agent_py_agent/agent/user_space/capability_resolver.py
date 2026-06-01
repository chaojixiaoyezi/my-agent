from __future__ import annotations

# LLM: Owner capability resolver maps short capability names to explicit owner/shared refs.
# 模块用途: 解析 owner/workspace/shared/builtin/optional 能力来源；同一 run 内缓存结果，避免反复确认。
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .home_layout import MyAgentHomePaths
from .owner_policy import EffectiveOwnerPolicy, resolve_effective_owner_policy


# LLM: CapabilityResolveResult is the refs-only answer from owner capability lookup.
# 类用途: 描述能力短名解析状态、来源、候选和 run 内缓存位置。
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

    # LLM: to_dict serializes resolver results for run-scoped cache files.
    # 函数用途: 转成 JSON 友好的字典，不读取能力正文。
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
        }

# LLM: CapabilityResolveOptions keeps optional lookup knobs out of the public function signature.
# 类用途: 保存能力类型、run id 和已解析 owner policy。
@dataclass(frozen=True)
class CapabilityResolveOptions:
    kind: str = "skill"
    run_id: str = ""
    policy: EffectiveOwnerPolicy | None = None


# LLM: resolve_owner_capability resolves an owner-visible capability without installing or enabling it.
# 函数用途: 按 owner/shared/builtin 顺序解析能力短名，并写入同 run 缓存。
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
    revoked = _revoked_result(home, capability_name, kind=capability_kind)
    if revoked is not None:
        return revoked
    cache_path = _cache_path(home, run_id=opts.run_id, name=capability_name, kind=capability_kind)
    cached = _read_cached_result(cache_path)
    if cached is not None:
        return cached
    effective = opts.policy or resolve_effective_owner_policy(home)
    candidates = _candidate_records(home, capability_name, kind=capability_kind, policy=effective)
    result = _result_from_candidates(capability_name, capability_kind, candidates, cache_path)
    _write_cache(cache_path, result)
    return result


# LLM: _candidate_records collects candidates from enabled sources only.
# 函数用途: 根据 owner policy 汇总 owner、shared 和 builtin 候选。
def _candidate_records(
    home: MyAgentHomePaths,
    name: str,
    *,
    kind: str,
    policy: EffectiveOwnerPolicy,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    if _source_enabled(policy, "owner"):
        records.extend(_owner_records(home, name, kind=kind))
    if _source_enabled(policy, "shared"):
        records.extend(_index_records(_shared_index_path(home, kind), name, source="shared"))
    if _source_enabled(policy, "builtin"):
        records.extend(_builtin_record(name, kind=kind))
    return _dedupe_candidates(records)


# LLM: _owner_records searches owner-private capability folders and draft indexes.
# 函数用途: 读取 owner home 内同名能力目录和草稿候选行。
def _owner_records(home: MyAgentHomePaths, name: str, *, kind: str) -> list[dict[str, object]]:
    root = Path(home.owner_home_dir) / _kind_dir(kind)
    records: list[dict[str, object]] = []
    for path in [root / name, root / ".drafts" / name]:
        if path.exists():
            records.append(_record(name, kind=kind, source="owner", path=path))
    draft_index = root / ".drafts" / f"{kind}_candidates.jsonl"
    records.extend(_index_records(draft_index, name, source="owner_draft"))
    return records


# LLM: _index_records tolerates bad JSONL rows while scanning a capability index.
# 函数用途: 从 shared/owner draft 索引中取同名能力候选。
def _index_records(path: Path, name: str, *, source: str) -> list[dict[str, object]]:
    if not path.exists():
        return []
    rows: list[dict[str, object]] = []
    wanted = name.lower()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        label = str(payload.get("name") or payload.get("title") or payload.get("id") or "").strip()
        if label.lower() != wanted:
            continue
        row = dict(payload)
        row.setdefault("source", source)
        rows.append(row)
    return rows


# LLM: _revoked_result lets security revocation override stale run caches.
# 函数用途: 检查 owner/shared 索引中是否有 revoked/disabled 状态。
def _revoked_result(home: MyAgentHomePaths, name: str, *, kind: str) -> CapabilityResolveResult | None:
    records = [
        *_index_records(_shared_index_path(home, kind), name, source="shared"),
        *_index_records(Path(home.owner_home_dir) / _kind_dir(kind) / ".drafts" / f"{kind}_candidates.jsonl", name, source="owner_draft"),
    ]
    for row in records:
        status = str(row.get("status") or "").strip().lower()
        if status in {"revoked", "disabled", "revoked_for_security"}:
            return CapabilityResolveResult(
                name=name,
                kind=kind,
                status="revoked",
                resolved_id=str(row.get("id") or row.get("name") or name),
                source=str(row.get("source") or ""),
                path=str(row.get("path") or ""),
                candidates=tuple(records),
            )
    return None


# LLM: _builtin_record is the open-world fallback for known builtin names.
# 函数用途: 为 builtin 来源生成统一候选记录。
def _builtin_record(name: str, *, kind: str) -> list[dict[str, object]]:
    return [_record(name, kind=kind, source="builtin", path="")]


# LLM: _result_from_candidates converts candidates into resolved/ambiguous/not_found states.
# 函数用途: 根据候选数量和来源优先级生成最终解析结果。
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


# LLM: _highest_priority_candidates keeps owner-private capabilities ahead of shared defaults.
# 函数用途: 按 owner、owner_draft、shared、builtin 选择最高优先级候选。
def _highest_priority_candidates(candidates: list[dict[str, object]]) -> list[dict[str, object]]:
    priority = {"owner": 0, "owner_draft": 1, "shared": 2, "builtin": 3}
    ranked = [(priority.get(str(row.get("source") or ""), 99), row) for row in candidates]
    best = min(score for score, _ in ranked)
    return [row for score, row in ranked if score == best]


# LLM: _source_enabled applies owner policy without rejecting open-world capability names.
# 函数用途: 判断某个能力来源是否在 owner policy 中启用。
def _source_enabled(policy: EffectiveOwnerPolicy, source: str) -> bool:
    sources = set(policy.enabled_skill_sources) | set(policy.enabled_tool_sources)
    return not sources or source in sources


# LLM: _shared_index_path chooses the shared index for the requested capability family.
# 函数用途: 返回 skill/tool/workflow/role_template 对应的公共索引路径。
def _shared_index_path(home: MyAgentHomePaths, kind: str) -> Path:
    if kind == "tool":
        return home.shared_indexes_tools_jsonl
    if kind == "workflow":
        return home.shared_indexes_workflows_jsonl
    if kind == "role_template":
        return home.shared_indexes_role_templates_jsonl
    return home.shared_indexes_skills_jsonl


# LLM: _kind_dir maps capability families to owner-home directories with a skills fallback.
# 函数用途: 计算 owner 私有能力目录名。
def _kind_dir(kind: str) -> str:
    return {"tool": "tools", "workflow": "workflows", "role_template": "role_templates"}.get(kind, "skills")


# LLM: _record normalizes a filesystem capability hit into an index-like row.
# 函数用途: 生成统一候选记录。
def _record(name: str, *, kind: str, source: str, path: Path | str) -> dict[str, object]:
    return {"id": name, "name": name, "kind": kind, "source": source, "path": str(path)}


# LLM: _dedupe_candidates removes duplicate capability rows while preserving priority order.
# 函数用途: 按 source/id/path 去重候选。
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


# LLM: _cache_path keeps capability resolution cache scoped to one owner and run.
# 函数用途: 生成 run 内能力解析缓存路径；没有 run_id 时不缓存。
def _cache_path(home: MyAgentHomePaths, *, run_id: str, name: str, kind: str) -> Path | None:
    if not run_id:
        return None
    safe_run = _safe_segment(run_id)
    safe_name = _safe_segment(name)
    return home.owner_memory_runtime_refs_dir / "capability_resolver" / safe_run / f"{kind}-{safe_name}.json"


# LLM: _read_cached_result restores a previous run-scoped capability decision.
# 函数用途: 容错读取能力解析缓存。
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


# LLM: _write_cache persists a resolver result only when a run-scoped cache exists.
# 函数用途: 写入能力解析缓存，避免同 run 反复确认。
def _write_cache(path: Path | None, result: CapabilityResolveResult) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# LLM: _safe_segment turns open-world ids into safe cache filename pieces.
# 函数用途: 清理 run/capability 名称中的路径控制字符。
def _safe_segment(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in value).strip("_") or "item"


__all__ = ["CapabilityResolveOptions", "CapabilityResolveResult", "resolve_owner_capability"]
