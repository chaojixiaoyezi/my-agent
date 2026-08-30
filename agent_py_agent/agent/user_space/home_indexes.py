
# LLM: Global index files are disposable lookup projections over canonical owner
# homes. Runtime writes are change-only and rebuild replaces them with compact
# snapshots; no caller may treat duplicate JSONL history as machine authority.
# 模块用途: 维护跨用户任务和子代理的轻量索引，避免相同状态反复追加导致磁盘与内存膨胀。

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..common.json_io import (
    locked_json_path,
    read_jsonl_objects_report,
    write_text_file_atomic_unlocked,
)
from ..io import append_jsonl
from .home_layout import MyAgentHomePaths
from .owner_resolver import OwnerHomeResult

_INDEX_WRITE_CACHE_CAPACITY = 50_000
_INDEX_WRITE_CACHE: OrderedDict[tuple[str, ...], str] = OrderedDict()
_INDEX_WRITE_LOCK = threading.Lock()


@dataclass(frozen=True)
class TaskIndexRef:
    owner_id: str
    task_id: str
    task_path: str | Path
    status: str
    title: str = ""


@dataclass(frozen=True)
class RunIndexRef:
    owner_id: str
    run_id: str
    task_id: str
    run_path: str | Path
    status: str


@dataclass(frozen=True)
class AgentIndexRef:
    owner_id: str
    agent_id: str
    task_id: str
    run_path: str | Path
    status: str


@dataclass(frozen=True)
class _IndexSpec:
    kind: str
    path: Path
    path_field: str
    key_fields: tuple[str, ...]


@dataclass(frozen=True)
class _ScopeFilter:
    owner_id: str = ""
    task_id: str = ""


@dataclass(frozen=True)
class _LatestRefSource:
    path: Path
    key_fields: tuple[str, ...]
    context: str


@dataclass(frozen=True)
class IndexRefsReport:
    records: list[dict[str, Any]]
    load_errors: list[dict[str, object]]


# LLM: Owner registration is a latest-state projection. Reconstructing the same
# scoped Agent in one process must not append another identical owner row.
# 函数用途: 登记用户目录位置；内容没变化时不重复写索引。
def register_owner_ref(home: MyAgentHomePaths, owner: OwnerHomeResult) -> dict[str, Any]:
    payload = _owner_payload(owner)
    _append_changed_index_record(
        home.global_index_owners_jsonl,
        payload,
        key_fields=("owner_id",),
    )
    return payload


# LLM: Task projection writes only semantic changes; updated_at alone cannot
# grow the global index. Canonical task state remains under the owner home.
# 函数用途: 登记任务最新位置和状态，重复心跳不会制造重复行。
def register_task_ref(home: MyAgentHomePaths, ref: TaskIndexRef) -> dict[str, Any]:
    payload = _task_payload(ref)
    _append_changed_index_record(
        home.global_index_active_tasks_jsonl,
        payload,
        key_fields=("owner_id", "task_id"),
    )
    return payload


# LLM: Run projection is change-only and never substitutes for the canonical
# run state. A status/path change still appends one new latest-state record.
# 函数用途: 登记一次运行的最新引用，状态不变时不重复落盘。
def register_run_ref(home: MyAgentHomePaths, ref: RunIndexRef) -> dict[str, Any]:
    payload = _run_payload(ref)
    _append_changed_index_record(
        home.global_index_active_runs_jsonl,
        payload,
        key_fields=("owner_id", "run_id"),
    )
    return payload


# LLM: Agent projection follows the same change-only rule as task/run indexes;
# identity and status come from structured refs, never display text.
# 函数用途: 登记子代理最新引用，避免每次状态刷新都写一模一样的记录。
def register_agent_ref(home: MyAgentHomePaths, ref: AgentIndexRef) -> dict[str, Any]:
    payload = _agent_payload(ref)
    _append_changed_index_record(
        home.global_index_active_agents_jsonl,
        payload,
        key_fields=("owner_id", "agent_id"),
    )
    return payload


# LLM: Payload builders normalize structured refs only; timestamps are display
# metadata and are excluded later from semantic change detection.
# 函数用途: 把用户目录引用转成可写入全局索引的统一记录。
def _owner_payload(owner: OwnerHomeResult) -> dict[str, Any]:
    return {
        "schema_version": "global-owner-index.v1",
        "owner_id": owner.owner_id,
        "provider": owner.identity.provider,
        "owner_kind": owner.identity.owner_kind,
        "owner_home": str(owner.home_dir),
        "updated_at": _now_iso(),
    }


# LLM: Task payloads preserve canonical owner/task identity and latest projected
# status; they never infer lifecycle from titles or paths.
# 函数用途: 把任务引用整理成稳定的索引记录。
def _task_payload(ref: TaskIndexRef) -> dict[str, Any]:
    return {
        "schema_version": "global-task-index.v1",
        "owner_id": str(ref.owner_id or ""),
        "task_id": str(ref.task_id or ""),
        "task_path": str(ref.task_path),
        "status": str(ref.status or ""),
        "title": str(ref.title or ""),
        "updated_at": _now_iso(),
    }


# LLM: Run payloads keep explicit run/task/owner fields as the only identity
# authority and add no compatibility aliases.
# 函数用途: 把运行引用整理成稳定的索引记录。
def _run_payload(ref: RunIndexRef) -> dict[str, Any]:
    return {
        "schema_version": "global-run-index.v1",
        "owner_id": str(ref.owner_id or ""),
        "run_id": str(ref.run_id or ""),
        "task_id": str(ref.task_id or ""),
        "run_path": str(ref.run_path),
        "status": str(ref.status or ""),
        "updated_at": _now_iso(),
    }


# LLM: Agent payloads use explicit owner/agent/task identity and structured
# lifecycle state; display descriptions do not participate in routing.
# 函数用途: 把子代理引用整理成稳定的索引记录。
def _agent_payload(ref: AgentIndexRef) -> dict[str, Any]:
    return {
        "schema_version": "global-agent-index.v1",
        "owner_id": str(ref.owner_id or ""),
        "agent_id": str(ref.agent_id or ""),
        "task_id": str(ref.task_id or ""),
        "run_path": str(ref.run_path),
        "status": str(ref.status or ""),
        "updated_at": _now_iso(),
    }


# LLM: This process-local fingerprint cache is bounded and excludes timestamps.
# The file append happens while holding the cache lock so concurrent identical
# updates cannot race into duplicate rows; failures never poison the cache.
# 函数用途: 只有索引里的实际字段发生变化时才追加一行，并限制去重缓存自身的大小。
def _append_changed_index_record(
    path: Path,
    payload: dict[str, Any],
    *,
    key_fields: tuple[str, ...],
) -> bool:
    cache_key = _index_cache_key(path, payload, key_fields)
    fingerprint = _index_fingerprint(payload)
    with _INDEX_WRITE_LOCK:
        if _INDEX_WRITE_CACHE.get(cache_key) == fingerprint:
            _INDEX_WRITE_CACHE.move_to_end(cache_key)
            return False
        append_jsonl(path, payload, sort_keys=True)
        _remember_index_fingerprint(cache_key, fingerprint)
    return True


# LLM: A rebuild is a projection compaction, not an event append. Each file is
# replaced atomically under the same sidecar lock used by appenders, then the
# in-process change cache is reseeded from the new snapshot.
# 函数用途: 用磁盘上的权威用户目录重新生成四份紧凑索引，清掉历史重复行和损坏旧行。
def replace_home_index_snapshots(
    home: MyAgentHomePaths,
    *,
    owners: tuple[OwnerHomeResult, ...],
    task_refs: tuple[TaskIndexRef, ...],
    run_refs: tuple[RunIndexRef, ...],
    agent_refs: tuple[AgentIndexRef, ...],
) -> None:
    snapshots = (
        (home.global_index_owners_jsonl, [_owner_payload(item) for item in owners], ("owner_id",)),
        (
            home.global_index_active_tasks_jsonl,
            [_task_payload(item) for item in task_refs],
            ("owner_id", "task_id"),
        ),
        (
            home.global_index_active_runs_jsonl,
            [_run_payload(item) for item in run_refs],
            ("owner_id", "run_id"),
        ),
        (
            home.global_index_active_agents_jsonl,
            [_agent_payload(item) for item in agent_refs],
            ("owner_id", "agent_id"),
        ),
    )
    with _INDEX_WRITE_LOCK:
        for path, records, key_fields in snapshots:
            content = "".join(
                json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                for record in records
            )
            with locked_json_path(path):
                write_text_file_atomic_unlocked(path, content)
            _forget_index_path(path)
            for record in records:
                _remember_index_fingerprint(
                    _index_cache_key(path, record, key_fields),
                    _index_fingerprint(record),
                )


# LLM: Cache identity includes the resolved projection path and the schema's
# structured key fields so unrelated homes or entity kinds can never collide.
# 函数用途: 为一条索引实体生成进程内去重键。
def _index_cache_key(
    path: Path,
    payload: dict[str, Any],
    key_fields: tuple[str, ...],
) -> tuple[str, ...]:
    return (str(path.resolve()), *(str(payload.get(field) or "") for field in key_fields))


# LLM: The semantic fingerprint intentionally ignores updated_at; otherwise an
# unchanged heartbeat would still be treated as a new projection state.
# 函数用途: 生成不受写入时间影响的索引内容指纹。
def _index_fingerprint(payload: dict[str, Any]) -> str:
    stable = {key: value for key, value in payload.items() if key != "updated_at"}
    return json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# LLM: Fingerprint retention is bounded LRU state only; evicting an entry may
# cause one harmless extra append, never loss of canonical task data.
# 函数用途: 记住最近写入指纹，并限制去重缓存的内存占用。
def _remember_index_fingerprint(cache_key: tuple[str, ...], fingerprint: str) -> None:
    _INDEX_WRITE_CACHE[cache_key] = fingerprint
    _INDEX_WRITE_CACHE.move_to_end(cache_key)
    while len(_INDEX_WRITE_CACHE) > _INDEX_WRITE_CACHE_CAPACITY:
        _INDEX_WRITE_CACHE.popitem(last=False)


# LLM: Snapshot replacement invalidates all cached fingerprints for that exact
# projection before reseeding them from the compacted records.
# 函数用途: 清掉某份索引文件对应的旧去重记录。
def _forget_index_path(path: Path) -> None:
    prefix = str(path.resolve())
    for cache_key in tuple(_INDEX_WRITE_CACHE):
        if cache_key and cache_key[0] == prefix:
            _INDEX_WRITE_CACHE.pop(cache_key, None)


def latest_owner_refs(home: MyAgentHomePaths, *, limit: int = 20) -> list[dict[str, Any]]:
    return latest_owner_refs_report(home, limit=limit).records


def latest_owner_refs_report(home: MyAgentHomePaths, *, limit: int = 20) -> IndexRefsReport:
    return _latest_unique_refs_report(
        home.global_index_owners_jsonl,
        key_fields=("owner_id",),
        limit=limit,
        context="home_indexes.owners",
    )


def latest_task_refs(home: MyAgentHomePaths, *, owner_id: str = "", status: str = "", limit: int = 20) -> list[dict[str, Any]]:
    return latest_task_refs_report(home, owner_id=owner_id, status=status, limit=limit).records


def latest_task_refs_report(
    home: MyAgentHomePaths,
    *,
    owner_id: str = "",
    status: str = "",
    limit: int = 20,
) -> IndexRefsReport:
    owner = str(owner_id or "")
    wanted_status = str(status or "")
    rows = []
    report = _latest_unique_refs_report(
        home.global_index_active_tasks_jsonl,
        key_fields=("owner_id", "task_id"),
        limit=0,
        context="home_indexes.active_tasks",
    )
    for record in report.records:
        if owner and str(record.get("owner_id") or "") != owner:
            continue
        if wanted_status and str(record.get("status") or "") != wanted_status:
            continue
        rows.append(record)
        if limit > 0 and len(rows) >= limit:
            break
    return IndexRefsReport(rows, report.load_errors)


def latest_run_refs(home: MyAgentHomePaths, *, owner_id: str = "", task_id: str = "", limit: int = 20) -> list[dict[str, Any]]:
    return latest_run_refs_report(home, owner_id=owner_id, task_id=task_id, limit=limit).records


def latest_run_refs_report(home: MyAgentHomePaths, *, owner_id: str = "", task_id: str = "", limit: int = 20) -> IndexRefsReport:
    return _latest_scoped_refs_report(
        _LatestRefSource(
            home.global_index_active_runs_jsonl,
            ("owner_id", "run_id"),
            "home_indexes.active_runs",
        ),
        filters=_ScopeFilter(owner_id=owner_id, task_id=task_id),
        limit=limit,
    )


def latest_agent_refs(home: MyAgentHomePaths, *, owner_id: str = "", task_id: str = "", limit: int = 20) -> list[dict[str, Any]]:
    return latest_agent_refs_report(home, owner_id=owner_id, task_id=task_id, limit=limit).records


def latest_agent_refs_report(home: MyAgentHomePaths, *, owner_id: str = "", task_id: str = "", limit: int = 20) -> IndexRefsReport:
    return _latest_scoped_refs_report(
        _LatestRefSource(
            home.global_index_active_agents_jsonl,
            ("owner_id", "agent_id"),
            "home_indexes.active_agents",
        ),
        filters=_ScopeFilter(owner_id=owner_id, task_id=task_id),
        limit=limit,
    )


def dangling_index_refs(home: MyAgentHomePaths, *, limit: int = 100) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    specs = (
        _IndexSpec("task", home.global_index_active_tasks_jsonl, "task_path", ("owner_id", "task_id")),
        _IndexSpec("run", home.global_index_active_runs_jsonl, "run_path", ("owner_id", "run_id")),
        _IndexSpec("agent", home.global_index_active_agents_jsonl, "run_path", ("owner_id", "agent_id")),
    )
    for spec in specs:
        findings.extend(_dangling_refs_for(spec, remaining=limit - len(findings)))
        if len(findings) >= limit:
            return findings[:limit]
    return findings


def _dangling_refs_for(
    spec: _IndexSpec,
    *,
    remaining: int,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if remaining <= 0:
        return findings
    for record in _latest_unique_refs(spec.path, key_fields=spec.key_fields, limit=0):
        target = Path(str(record.get(spec.path_field) or ""))
        if target.exists():
            continue
        findings.append({"kind": spec.kind, "missing_path": str(target), "record": record})
        if len(findings) >= remaining:
            break
    return findings


def _latest_scoped_refs_report(
    source: _LatestRefSource,
    *,
    filters: _ScopeFilter,
    limit: int,
) -> IndexRefsReport:
    owner = str(filters.owner_id or "")
    task = str(filters.task_id or "")
    rows = []
    report = _latest_unique_refs_report(source.path, key_fields=source.key_fields, limit=0, context=source.context)
    for record in report.records:
        if owner and str(record.get("owner_id") or "") != owner:
            continue
        if task and str(record.get("task_id") or "") != task:
            continue
        rows.append(record)
        if limit > 0 and len(rows) >= limit:
            break
    return IndexRefsReport(rows, report.load_errors)


def _latest_unique_refs(path: Path, *, key_fields: tuple[str, ...], limit: int) -> list[dict[str, Any]]:
    return _latest_unique_refs_report(path, key_fields=key_fields, limit=limit, context="home_indexes.refs").records


def _latest_unique_refs_report(path: Path, *, key_fields: tuple[str, ...], limit: int, context: str) -> IndexRefsReport:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    report = _latest_jsonl_records_report(path, limit=0, context=context)
    for record in report.records:
        key = tuple(str(record.get(field) or "") for field in key_fields)
        if key in seen:
            continue
        seen.add(key)
        rows.append(record)
        if limit > 0 and len(rows) >= limit:
            break
    return IndexRefsReport(rows, report.load_errors)


def _latest_jsonl_records(path: Path, *, limit: int) -> list[dict[str, Any]]:
    return _latest_jsonl_records_report(path, limit=limit, context="home_indexes.records").records


def _latest_jsonl_records_report(path: Path, *, limit: int, context: str) -> IndexRefsReport:
    if not path.exists():
        return IndexRefsReport([], [])
    report = read_jsonl_objects_report(path, context=context)
    rows = list(reversed(report.records))
    if limit > 0:
        rows = rows[:limit]
    return IndexRefsReport(rows, report.load_errors)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "AgentIndexRef",
    "IndexRefsReport",
    "RunIndexRef",
    "TaskIndexRef",
    "dangling_index_refs",
    "latest_agent_refs",
    "latest_agent_refs_report",
    "latest_owner_refs",
    "latest_owner_refs_report",
    "latest_run_refs",
    "latest_run_refs_report",
    "latest_task_refs",
    "latest_task_refs_report",
    "register_agent_ref",
    "register_owner_ref",
    "register_run_ref",
    "register_task_ref",
    "replace_home_index_snapshots",
]
