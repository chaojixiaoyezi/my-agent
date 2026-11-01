
# LLM: Global index files are disposable lookup projections over canonical owner
# homes. Runtime writes are change-only and rebuild replaces them with compact
# snapshots; no caller may treat duplicate JSONL history as machine authority.
# 共享整体替换先核原模式拒门；owner受控追加与纯读取不受该门扩大，仍各守原调用权限。
# 模块用途: 维护跨用户任务和子代理的轻量索引，避免相同状态反复追加导致磁盘与内存膨胀。

from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..common.json_io import (
    append_private_jsonl_records,
    locked_json_path,
    read_jsonl_objects_report,
    write_private_text_file_atomic_unlocked,
)
from ..common.nofollow_fs import (
    create_private_file_beneath,
    ensure_private_dir,
    open_directory_beneath,
)
from .home_layout import MyAgentHomePaths, home_paths
from .owner_resolver import OwnerHomeResult

# 写缓存最多缓存 5 万条索引键（个数）：批量写入前先攒内存，满了强制刷盘防失控。
_INDEX_WRITE_CACHE_CAPACITY_COUNT = 50_000
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


# LLM: 四份索引各自的**权威键定义**。写入侧（register_*_ref）与维护侧的按 key 压缩
#   （home_index_compact.key_fields_for）都从这里取，避免同一概念在多处各抄一份表——
#   抄两份的后果是 spec 改了以后压缩会把不同实体的行当成同一个 key 合并掉。
#   键用文件名索引，因为压缩只拿到路径；文件名与 home_layout 的字段一一对应。
# 函数用途: 按索引文件名取该文件记录的唯一键字段。
INDEX_KEY_FIELDS_BY_FILE: dict[str, tuple[str, ...]] = {
    "owners.jsonl": ("owner_id",),
    "active_tasks.jsonl": ("owner_id", "task_id"),
    "active_runs.jsonl": ("owner_id", "run_id"),
    "active_agents.jsonl": ("owner_id", "agent_id"),
}


def key_fields_for_index_file(path: str | Path) -> tuple[str, ...]:
    """按索引文件名返回权威键定义；未知文件名退化成 owners 的键（最保守的单一键）。"""
    return INDEX_KEY_FIELDS_BY_FILE.get(Path(path).name, ("owner_id",))


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


@dataclass(frozen=True)
class TaskRefLookupScope:
    """定位缓存只属于可信 owner 根；里面没有任务业务状态。"""

    owner_root: Path
    owner_id: str
    source: Path
    projection: Path


def task_ref_lookup_scope(owner_home: object) -> TaskRefLookupScope:
    if isinstance(owner_home, MyAgentHomePaths):
        owner_root = owner_home.owner_home_dir
        base = owner_home.root
        owner_id = owner_home.owner_id or "local/main"
        source = owner_home.global_index_active_tasks_jsonl
    elif isinstance(owner_home, OwnerHomeResult):
        owner_root, owner_id = owner_home.home_dir, owner_home.owner_id
        base = owner_home.root
        source = home_paths(owner_home.root).global_index_active_tasks_jsonl
    else:
        owner_root = Path(owner_home).expanduser().absolute()
        # 只按宿主既有 owner 布局解释可信路径，不用索引正文构造 owner 根。
        if owner_root.parent.name == "local" and owner_root.parent.parent.name == "owners":
            base = owner_root.parents[2]
            owner_id = "local/" + owner_root.name
        elif (
            len(owner_root.parents) >= 5
            and owner_root.parent.name in {"users", "groups"}
            and owner_root.parents[2].name == "providers"
            and owner_root.parents[3].name == "owners"
        ):
            base = owner_root.parents[4]
            owner_id = owner_root.relative_to(base / "owners").as_posix()
        else:
            # 老版单 owner 布局；根本身就是调用方已绑定的 local/main 空间。
            base, owner_id = owner_root, "local/main"
        source = home_paths(base).global_index_active_tasks_jsonl
    raw_root = Path(owner_root).expanduser().absolute()
    if raw_root.is_symlink() or ".." in raw_root.parts:
        raise OSError("owner root cannot be rebound through a symlink")
    relative = raw_root.relative_to(Path(base).expanduser().absolute())
    anchor = Path(base).resolve(strict=True)
    descriptor = open_directory_beneath(anchor, relative.parts)
    os.close(descriptor)
    root = anchor.joinpath(*relative.parts)
    return TaskRefLookupScope(root, owner_id, source, root / "cache" / "task_locator.sqlite3")


def task_ref_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM locator_meta WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row is not None else default


def set_task_ref_meta(conn: sqlite3.Connection, key: str, value: object) -> None:
    text = str(value)
    if task_ref_meta(conn, key) != text:
        conn.execute("INSERT OR REPLACE INTO locator_meta(key,value) VALUES (?,?)", (key, text))


@contextmanager
def task_ref_projection(owner_home: object):
    """每次精确查询使用有界 SQLite 页缓存；游标与位置投影同事务提交，可删除重建。"""
    scope = task_ref_lookup_scope(owner_home)
    ensure_private_dir(scope.projection.parent, canonical_root=scope.owner_root)
    try:
        descriptor = create_private_file_beneath(scope.owner_root, ("cache", scope.projection.name))
    except FileExistsError:
        pass
    else:
        os.close(descriptor)
    leaf = scope.projection.lstat()
    if not stat.S_ISREG(leaf.st_mode) or leaf.st_nlink != 1:
        raise OSError("unsafe task locator projection")
    conn = sqlite3.connect(scope.projection, timeout=0.05)
    try:
        conn.execute("PRAGMA cache_size=-256")
        conn.execute("CREATE TABLE IF NOT EXISTS locator_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        conn.execute("""CREATE TABLE IF NOT EXISTS task_refs(
            owner_id TEXT NOT NULL, task_id TEXT NOT NULL, path TEXT NOT NULL, origin TEXT NOT NULL,
            PRIMARY KEY(owner_id,task_id,origin,path))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS legacy_dirs(
            path TEXT PRIMARY KEY, depth INTEGER NOT NULL, position INTEGER NOT NULL DEFAULT 0,
            signature TEXT NOT NULL DEFAULT '', done INTEGER NOT NULL DEFAULT 0)""")
        conn.execute("BEGIN IMMEDIATE")
        identity = scope.owner_root.stat()
        binding = f"{scope.owner_root}:{identity.st_dev}:{identity.st_ino}:{scope.owner_id}:{scope.source}"
        if task_ref_meta(conn, "binding") != binding:
            conn.execute("DELETE FROM locator_meta")
            conn.execute("DELETE FROM task_refs")
            conn.execute("DELETE FROM legacy_dirs")
            set_task_ref_meta(conn, "binding", binding)
        with conn:
            yield scope, conn
    finally:
        conn.close()


def _task_ref_source_stamp(info: os.stat_result) -> str:
    return ":".join(str(v) for v in (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))


def _sync_task_ref_source(scope: TaskRefLookupScope, conn: sqlite3.Connection, max_records: int) -> dict:
    """沿现成 active_tasks.jsonl 的准确字节位置增量消费，不全读/反序列化历史。"""
    previous = task_ref_meta(conn, "index.stamp")
    offset = int(task_ref_meta(conn, "index.offset", "0"))
    try:
        info = scope.source.stat(follow_symlinks=False)
    except FileNotFoundError:
        if previous != "missing":
            conn.execute("DELETE FROM task_refs WHERE origin='publisher'")
            set_task_ref_meta(conn, "index.stamp", "missing")
            set_task_ref_meta(conn, "index.offset", 0)
            set_task_ref_meta(conn, "index.errors", 0)
        return {"index_complete": True, "index_records_read": 0, "index_errors": 0,
                "index_errors_total": 0, "index_offset": 0}
    if not stat.S_ISREG(info.st_mode):
        raise OSError("unsafe task reference source")
    stamp = _task_ref_source_stamp(info)
    reset = previous in {"", "missing"}
    if not reset:
        old = tuple(int(v) for v in previous.split(":"))
        reset = old[:2] != (info.st_dev, info.st_ino) or info.st_size < old[2]
        reset |= info.st_size == old[2] and stamp != previous
    scanned = errors = 0
    with scope.source.open("rb") as stream:
        if _task_ref_source_stamp(os.fstat(stream.fileno())) != stamp:
            raise OSError("task reference source changed before open")
        if not reset and offset:
            stream.seek(max(0, offset - 128))
            reset = stream.read(min(offset, 128)).hex() != task_ref_meta(conn, "index.anchor")
        if reset:
            conn.execute("DELETE FROM task_refs WHERE origin='publisher'")
            offset = 0
            set_task_ref_meta(conn, "index.errors", 0)
        discarding = False if reset else task_ref_meta(conn, "index.discarding") == "1"
        stream.seek(offset)
        # 正常未完成行不签收。超长坏行按有界字节片丢弃到换行，保存丢弃状态，
        # 不能为限制单行内存就永久堵住其后的正常记录。
        steps = 0
        while steps < max_records and stream.tell() < info.st_size:
            start = stream.tell()
            raw = stream.readline(65537)
            steps += 1
            if discarding:
                discarding = not raw.endswith(b"\n")
                continue
            if len(raw) > 65536:
                scanned += 1
                errors += 1
                discarding = not raw.endswith(b"\n")
                continue
            if not raw.endswith(b"\n"):
                stream.seek(start)
                break
            scanned += 1
            try:
                row = json.loads(raw)
            except (ValueError, UnicodeError):
                errors += 1
                continue
            if not isinstance(row, dict):
                errors += 1
                continue
            if not isinstance(row.get("owner_id"), str) or not row["owner_id"]:
                errors += 1
                continue
            if row.get("owner_id") != scope.owner_id:
                continue
            task_id, path = row.get("task_id"), row.get("task_path")
            if not isinstance(task_id, str) or not task_id or not isinstance(path, str) or not path:
                errors += 1
                continue
            conn.execute("DELETE FROM task_refs WHERE owner_id=? AND task_id=? AND origin='publisher'",
                         (scope.owner_id, task_id))
            conn.execute("INSERT INTO task_refs VALUES (?,?,?,'publisher')", (scope.owner_id, task_id, path))
        offset = stream.tell()
        stream.seek(max(0, offset - 128))
        anchor = stream.read(min(offset, 128)).hex()
        after = os.fstat(stream.fileno())
    current = scope.source.stat(follow_symlinks=False)
    if _task_ref_source_stamp(current) != _task_ref_source_stamp(after) or _task_ref_source_stamp(after) != stamp:
        raise OSError("task reference source changed during slice")
    set_task_ref_meta(conn, "index.stamp", stamp)
    set_task_ref_meta(conn, "index.offset", offset)
    set_task_ref_meta(conn, "index.anchor", anchor)
    set_task_ref_meta(conn, "index.discarding", int(discarding))
    total_errors = int(task_ref_meta(conn, "index.errors", "0")) + errors
    set_task_ref_meta(conn, "index.errors", total_errors)
    return {"index_complete": offset == info.st_size and not discarding, "index_records_read": scanned,
            "index_errors": errors, "index_errors_total": total_errors, "index_offset": offset}


def lookup_task_ref(owner_home: object, task_id: str, *, max_records: int = 128) -> dict:
    """返回位置候选而非任务状态；调用方必须重新读取 canonical state 核身份。"""
    with task_ref_projection(owner_home) as (scope, conn):
        coverage = _sync_task_ref_source(scope, conn, max(1, min(max_records, 128)))
        paths = []
        if coverage["index_complete"]:
            rows = conn.execute(
                "SELECT path,origin FROM task_refs WHERE owner_id=? AND task_id=? ORDER BY origin DESC LIMIT 17",
                (scope.owner_id, task_id),
            ).fetchall()
            published = [path for path, origin in rows if origin == "publisher"]
            paths = published or [path for path, _ in rows]
        coverage.update(legacy_files_scanned=0, legacy_total_scanned=int(task_ref_meta(conn, "legacy.total", "0")),
                        legacy_complete=task_ref_meta(conn, "legacy.done") == "1",
                        legacy_errors=int(task_ref_meta(conn, "legacy.errors", "0")))
        return {"status": "partial" if not coverage["index_complete"] else "found" if paths else "missing",
                "paths": paths, "coverage": coverage}


# LLM: Owner registration is a latest-state projection. Reconstructing the same
# scoped Agent in one process must not append another identical owner row.
# 函数用途: 登记用户目录位置；内容没变化时不重复写索引。
def register_owner_ref(home: MyAgentHomePaths, owner: OwnerHomeResult) -> dict[str, Any]:
    payload = _owner_payload(owner)
    _append_changed_index_record(
        home.global_index_owners_jsonl,
        payload,
        key_fields=key_fields_for_index_file(home.global_index_owners_jsonl),
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
        key_fields=key_fields_for_index_file(home.global_index_active_tasks_jsonl),
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
        key_fields=key_fields_for_index_file(home.global_index_active_runs_jsonl),
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
        key_fields=key_fields_for_index_file(home.global_index_active_agents_jsonl),
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
#   索引文件本身带任务/代理/运行标识与标题，必须与锁同口径私有：走 append_private_jsonl_records
#   （文件 0600、目录 0700、已有 0644 下次追加即收紧），不再用跟随 umask 的 append_jsonl。
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
        append_private_jsonl_records(path, [payload], sort_keys=True)
        _remember_index_fingerprint(cache_key, fingerprint)
    return True


# LLM: A rebuild is a projection compaction, not an event append. Each file is
# replaced atomically under the same sidecar lock used by appenders, then the
# in-process change cache is reseeded from the new snapshot.
#   原模式拒门先于锁、临时文件和写缓存；不将worker可追加自己owner引用扩成整体重建权限。
# 函数用途: 默认模式按原四索引顺序替换紧凑投影，受限worker/host在共享写副作用前拒绝。
def replace_home_index_snapshots(
    home: MyAgentHomePaths,
    *,
    owners: tuple[OwnerHomeResult, ...],
    task_refs: tuple[TaskIndexRef, ...],
    run_refs: tuple[RunIndexRef, ...],
    agent_refs: tuple[AgentIndexRef, ...],
) -> None:
    from ..common.global_write_guard import require_global_writer

    require_global_writer("global_index")
    snapshots = (
        (
            home.global_index_owners_jsonl,
            [_owner_payload(item) for item in owners],
            key_fields_for_index_file(home.global_index_owners_jsonl),
        ),
        (
            home.global_index_active_tasks_jsonl,
            [_task_payload(item) for item in task_refs],
            key_fields_for_index_file(home.global_index_active_tasks_jsonl),
        ),
        (
            home.global_index_active_runs_jsonl,
            [_run_payload(item) for item in run_refs],
            key_fields_for_index_file(home.global_index_active_runs_jsonl),
        ),
        (
            home.global_index_active_agents_jsonl,
            [_agent_payload(item) for item in agent_refs],
            key_fields_for_index_file(home.global_index_active_agents_jsonl),
        ),
    )
    with _INDEX_WRITE_LOCK:
        for path, records, key_fields in snapshots:
            content = "".join(
                json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                for record in records
            )
            with locked_json_path(path):
                write_private_text_file_atomic_unlocked(path, content)
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
    while len(_INDEX_WRITE_CACHE) > _INDEX_WRITE_CACHE_CAPACITY_COUNT:
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
        key_fields=key_fields_for_index_file(home.global_index_owners_jsonl),
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
        key_fields=key_fields_for_index_file(home.global_index_active_tasks_jsonl),
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
            key_fields_for_index_file(home.global_index_active_runs_jsonl),
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
            key_fields_for_index_file(home.global_index_active_agents_jsonl),
            "home_indexes.active_agents",
        ),
        filters=_ScopeFilter(owner_id=owner_id, task_id=task_id),
        limit=limit,
    )


def dangling_index_refs(home: MyAgentHomePaths, *, limit: int = 100) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for spec in _dangling_index_specs(home):
        findings.extend(_dangling_refs_for(spec, remaining=limit - len(findings)))
        if len(findings) >= limit:
            return findings[:limit]
    return findings


# LLM: 悬空引用检查要读的三份索引。键一律从 INDEX_KEY_FIELDS_BY_FILE 取：
#   读取键一旦和压缩用的键不一致，压缩会把读取侧视为不同的行合并掉，而没有测试会发现
#   （9b 的 MH12 就是这条）。抽成函数是为了让测试能直接核对这三处。
# 函数用途: 返回悬空引用检查用的三份索引 spec。
def _dangling_index_specs(home: MyAgentHomePaths) -> tuple[_IndexSpec, ...]:
    return (
        _IndexSpec(
            "task",
            home.global_index_active_tasks_jsonl,
            "task_path",
            key_fields_for_index_file(home.global_index_active_tasks_jsonl),
        ),
        _IndexSpec(
            "run",
            home.global_index_active_runs_jsonl,
            "run_path",
            key_fields_for_index_file(home.global_index_active_runs_jsonl),
        ),
        _IndexSpec(
            "agent",
            home.global_index_active_agents_jsonl,
            "run_path",
            key_fields_for_index_file(home.global_index_active_agents_jsonl),
        ),
    )


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
    "lookup_task_ref",
    "register_agent_ref",
    "register_owner_ref",
    "register_run_ref",
    "register_task_ref",
    "replace_home_index_snapshots",
]
