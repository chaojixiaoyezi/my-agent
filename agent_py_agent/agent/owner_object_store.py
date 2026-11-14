"""规模 owner 文件事实源：PostgreSQL/RLS 清单 + S3 对象，Pod 磁盘只作临时缓存。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import stat
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from agent_py_agent.agent.common.json_io import write_private_json_file_atomic_unlocked
from agent_py_agent.agent.common.nofollow_fs import NoFollowPathError, _snapshot_stat_fields_match
from agent_py_agent.agent.owner_change_journal import OwnerChangeJournal
from agent_py_agent.agent.owner_snapshot_cache import OwnerSnapshotIndex
from agent_py_agent.agent.owner_snapshot_files import (
    DownloadedObjects,
    SnapshotParent,
    downloaded_object,
    file_signature,
    lexical_path,
    tree_snapshot,
    validate_relative,
)
from agent_py_agent.agent.owner_snapshot_index import (
    OWNER_SNAPSHOT_RACY_MTIME_SECONDS,
    OwnerStagedFile,
    OwnerStatIndex,
    descriptor_digest,
    stage_and_hash,
    stage_open_file,
)
from agent_py_agent.agent.pg_rls import tenant_session
from agent_py_agent.agent.scale_runtime import ScaleRuntimeConfig
from agent_py_agent.agent.storage_backend import StorageBackend

try:
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
except ImportError:  # pragma: no cover - scale extra 在构造 backend 时已给清晰错误
    text = None
    DBAPIError = ConnectionError


@dataclass(frozen=True)
class OwnerFile:
    relative_path: str
    object_key: str
    content_sha256: str
    size_bytes: int
    file_mode: int


@dataclass(frozen=True)
class OwnerManifestDelta:
    complete: bool
    entries: tuple[tuple[str, OwnerFile | None], ...]
    reason: str = ''


@dataclass(frozen=True)
class OwnerObjectConfig:
    bucket: str
    prefix: str = ""
    sse: str = "AES256"
    kms_key: str = ""


# LLM: 租约 CAS 未命中时禁止写清单；调用方保留 DIRTY 并交队列重试。
# 类用途: 表示快照提交权已经失效。
class OwnerSnapshotLeaseLost(RuntimeError):
    """数据库拒绝了当前租约代次。"""


# LLM: COMMIT 连接失败不能推断提交或回滚；下轮从权威清单恢复。
# 类用途: 表示数据库提交结果无法确认。
class OwnerSnapshotCommitUnknown(RuntimeError):
    """快照事务的提交结果未知。"""




# 快照协议的封闭代码路径集合，不是容量或重试调优参数。
OWNER_SNAPSHOT_MODES = ("full", "delta_compat", "delta")


# LLM: 所有租约语句都在 tenant_session 短事务内运行；只有数据库时钟和 incarnation 决定提交权。
# 类用途: 管理快照租约头，并在同一事务中围栏和更新文件清单。
class _OwnerSnapshotHead:
    # LLM: 保存后端引用，不获取连接或改变数据库。
    # 函数用途: 建立快照租约 SQL 助手。
    def __init__(self, backend: StorageBackend) -> None:
        self._backend = backend

    # LLM: 仅空闲或已过期租约可被取得；无返回行是正常竞争，不抛队列异常。
    # 函数用途: 用数据库时间尝试获取 owner 的租约。
    def acquire(self, tenant: str, kind: str, owner: str, *, holder: str,
                incarnation: str, ttl_seconds: float) -> int | None:
        with tenant_session(self._backend, tenant) as conn:
            version = conn.execute(text(
                "INSERT INTO owner_snapshot_head (tenant, owner_kind, owner_id, version, holder, holder_incarnation, lease_expires_at, updated_at) VALUES (:tenant, :kind, :owner, 0, :holder, :inc, now() + make_interval(secs => :ttl), :now_ms) ON CONFLICT (tenant, owner_kind, owner_id) DO UPDATE SET holder = EXCLUDED.holder, holder_incarnation = EXCLUDED.holder_incarnation, lease_expires_at = EXCLUDED.lease_expires_at, updated_at = EXCLUDED.updated_at WHERE owner_snapshot_head.lease_expires_at IS NULL OR owner_snapshot_head.lease_expires_at <= now() RETURNING version"
            ), {"tenant": tenant, "kind": kind, "owner": owner, "holder": holder,
                "inc": incarnation, "ttl": ttl_seconds, "now_ms": int(time.time() * 1000)}).scalar_one_or_none()
        return None if version is None else int(version)

    # LLM: 等待剩余时间由数据库计算，本机时间不参与租约有效性判定。
    # 函数用途: 读取当前租约距离到期的秒数。
    def seconds_until_expiry(self, tenant: str, kind: str, owner: str) -> float:
        with tenant_session(self._backend, tenant) as conn:
            seconds = conn.execute(text(
                "SELECT GREATEST(EXTRACT(EPOCH FROM (lease_expires_at - now())), 0) AS wait_seconds FROM owner_snapshot_head WHERE tenant = :tenant AND owner_kind = :kind AND owner_id = :owner"
            ), {"tenant": tenant, "kind": kind, "owner": owner}).scalar_one_or_none()
        return 0.0 if seconds is None else float(seconds)

    # LLM: 过期租约即使代次相同也不能续活；返回值只作本地观察，提交仍须 CAS。
    # 函数用途: 续期仍有效的当前租约。
    def renew(self, tenant: str, kind: str, owner: str, *, incarnation: str, ttl_seconds: float) -> bool:
        with tenant_session(self._backend, tenant) as conn:
            result = conn.execute(text(
                "UPDATE owner_snapshot_head SET lease_expires_at = now() + make_interval(secs => :ttl) WHERE tenant = :tenant AND owner_kind = :kind AND owner_id = :owner AND holder_incarnation = :inc AND lease_expires_at > now()"
            ), {"tenant": tenant, "kind": kind, "owner": owner, "inc": incarnation, "ttl": ttl_seconds})
            return result.rowcount == 1

    # LLM: CAS 必须先于任何清单变更，且所有语句共享租户事务；仅退出提交阶段的连接故障归为结果未知。
    # 函数用途: 原子提交租约版本和文件清单增量。
    def commit(self, tenant: str, kind: str, owner: str, *, incarnation: str,
               removed: list[str], changed: list[OwnerFile], expected_version: int | None = None) -> int:
        exiting = False
        now_ms = int(time.time() * 1000)
        try:
            with tenant_session(self._backend, tenant) as conn:
                version = conn.execute(text(
                    "UPDATE owner_snapshot_head SET version = version + 1, holder = NULL, holder_incarnation = NULL, lease_expires_at = NULL, updated_at = :now_ms WHERE tenant = :tenant AND owner_kind = :kind AND owner_id = :owner AND (CAST(:expected AS BIGINT) IS NULL OR version=CAST(:expected AS BIGINT)) AND holder_incarnation = :inc AND lease_expires_at > now() RETURNING version"
                ), {"tenant": tenant, "kind": kind, "owner": owner, "inc": incarnation,
                    "now_ms": now_ms, "expected": expected_version}).scalar_one_or_none()
                if version is None:
                    raise OwnerSnapshotLeaseLost("owner 快照租约已失效")
                if removed:
                    conn.execute(text(
                        "DELETE FROM owner_file_manifest WHERE owner_kind=:kind AND owner_id=:owner AND relative_path = :path"
                    ), [{"kind": kind, "owner": owner, "path": path} for path in removed])
                if changed:
                    conn.execute(text(
                        "INSERT INTO owner_file_manifest "
                        "(tenant, owner_kind, owner_id, relative_path, object_key, content_sha256, size_bytes, file_mode, updated_at) "
                        "VALUES (:tenant, :kind, :owner, :path, :object_key, :sha, :size, :mode, :now_ms) "
                        "ON CONFLICT (tenant, owner_kind, owner_id, relative_path) DO UPDATE SET "
                        "object_key=EXCLUDED.object_key, content_sha256=EXCLUDED.content_sha256, "
                        "size_bytes=EXCLUDED.size_bytes, file_mode=EXCLUDED.file_mode, updated_at=EXCLUDED.updated_at"
                    ), [{"tenant": tenant, "kind": kind, "owner": owner, "path": row.relative_path,
                         "object_key": row.object_key, "sha": row.content_sha256, "size": row.size_bytes,
                         "mode": row.file_mode, "now_ms": now_ms} for row in changed])
                version = conn.execute(text('SELECT version FROM owner_snapshot_head WHERE tenant=:tenant '
                                            'AND owner_kind=:kind AND owner_id=:owner'),
                                       {'tenant': tenant, 'kind': kind, 'owner': owner}).scalar_one_or_none()
                if version is None:
                    raise OwnerSnapshotLeaseLost('owner 快照提交头消失')
                version = int(version)
                exiting = True
        except (ConnectionError, OSError, DBAPIError) as exc:
            if exiting:
                raise OwnerSnapshotCommitUnknown("owner 快照事务提交结果未知") from exc
            raise
        return version

    # LLM: incarnation 防止旧 holder 清除新租约；释放失败由调用方容许 TTL 收敛。
    # 函数用途: 释放仍属于当前代次的租约。
    def release(self, tenant: str, kind: str, owner: str, *, incarnation: str) -> None:
        with tenant_session(self._backend, tenant) as conn:
            conn.execute(text(
                "UPDATE owner_snapshot_head SET holder = NULL, holder_incarnation = NULL, lease_expires_at = NULL WHERE tenant = :tenant AND owner_kind = :kind AND owner_id = :owner AND holder_incarnation = :inc"
            ), {"tenant": tenant, "kind": kind, "owner": owner, "inc": incarnation})


class OwnerSnapshotStore:
    """以租约、原生变更证明和清单代次提交 owner 文件增量。"""

    _EPHEMERAL_ROOTS = frozenset({"cache", "tmp", "trash"})
    _head: _OwnerSnapshotHead | None = None

    # LLM: 构造只绑定租约助手；观察器在 slot 首次使用时出生，显式恢复仍保留完整校验。
    # 函数用途: 初始化对象存储、租约与变更投影依赖。
    def __init__(
        self,
        backend: StorageBackend,
        object_client: Any,
        config: OwnerObjectConfig,
    ) -> None:
        if not backend.is_postgres:
            raise ValueError("OwnerSnapshotStore 只允许正式 PostgreSQL/RLS 路径")
        if not config.bucket:
            raise ValueError("owner object bucket 必填")
        self._backend = backend
        self._client = object_client
        self._bucket = config.bucket
        self._prefix = config.prefix.strip("/")
        self._sse = config.sse
        self._kms_key = config.kms_key
        self._head = _OwnerSnapshotHead(backend)

    # LLM: advisory 在租约之前获取；DIRTY 先于家目录写入，只有 CAS 提交和索引保存成功才发布 CLEAN。
    # 函数用途: 在可续期租约内校对温缓存、执行回合并提交增量。
    @contextmanager
    def checkout_delta(
        self, tenant: str, owner_kind: str, owner_id: str, owner_home: Path, *,
        warm_dir: Path, lease_ttl_seconds: float, hold_advisory_lock: bool,
    ) -> Iterator[Path]:
        head = self._head
        if head is None:
            raise RuntimeError("owner 快照缺少租约 SQL 助手")
        if not lease_ttl_seconds > 0:
            raise ValueError("owner 快照租约 TTL 必须大于零")
        warm_dir = lexical_path(warm_dir)
        home_root = warm_dir / "home"
        owner_home = lexical_path(owner_home)
        if not owner_home.is_relative_to(home_root):
            raise ValueError("owner_home 必须位于 warm_dir/home 内")
        incarnation = uuid.uuid4().hex
        holder = f"{socket.gethostname()}:{os.getpid()}"
        renew_interval = lease_ttl_seconds / 3
        with ExitStack() as stack:
            warm_guard = stack.enter_context(SnapshotParent(warm_dir, create=True))
            home_guard = stack.enter_context(SnapshotParent(warm_dir, ('home',), create=True,
                                                           expected_root=warm_guard.root_identity))
            owner_parts = owner_home.relative_to(home_root).parts
            try:
                with SnapshotParent(home_root, owner_parts, expected_root=home_guard.identity) as before_owner:
                    original_owner_identity = before_owner.identity
            except FileNotFoundError:
                original_owner_identity = None
            owner_guard = stack.enter_context(SnapshotParent(home_root, owner_parts, create=True,
                                                            expected_root=home_guard.identity))
            if original_owner_identity is not None and owner_guard.identity != original_owner_identity:
                raise NoFollowPathError('owner snapshot lexical owner binding changed')
            owner_identity, home_identity = owner_guard.identity, home_guard.identity
            if hold_advisory_lock:
                stack.enter_context(self._owner_lock(tenant, owner_kind, owner_id))
            version = None
            while version is None:
                version = head.acquire(tenant, owner_kind, owner_id, holder=holder,
                                       incarnation=incarnation, ttl_seconds=lease_ttl_seconds)
                if version is None:
                    time.sleep(min(renew_interval, head.seconds_until_expiry(tenant, owner_kind, owner_id)))

            # LLM: 释放失败不覆盖已知提交结果；连接故障只让租约按 TTL 到期。
            # 函数用途: 尽力释放当前租约代次。
            def release() -> None:
                try:
                    head.release(tenant, owner_kind, owner_id, incarnation=incarnation)
                except (ConnectionError, OSError, DBAPIError):
                    pass

            stack.callback(release)
            stop, lost = threading.Event(), threading.Event()

            # LLM: 续租观察不替代提交 CAS；暂时连接失败不推断租约仍然有效。
            # 函数用途: 在独立短事务中周期性续租。
            def renew() -> None:
                while not stop.wait(renew_interval):
                    try:
                        if not head.renew(tenant, owner_kind, owner_id, incarnation=incarnation,
                                          ttl_seconds=lease_ttl_seconds):
                            lost.set()
                    except Exception:
                        continue

            thread = threading.Thread(target=renew, name="owner-snapshot-lease-renew", daemon=True)
            thread.start()
            stack.callback(thread.join, timeout=renew_interval)
            stack.callback(stop.set)
            state_path = warm_dir / "state.json"
            try:
                previous = json.loads(state_path.read_text(encoding="utf-8"))
                clean = isinstance(previous, dict) and previous.get("state") == "CLEAN"
            except (OSError, ValueError, UnicodeError, RecursionError):
                clean = False
            write_private_json_file_atomic_unlocked(state_path, {"state": "DIRTY"})
            projection = warm_dir / 'snapshot-index.sqlite3'
            recovered_index = False
            try:
                index = stack.enter_context(OwnerSnapshotIndex.open(projection))
            except sqlite3.Error:
                # 这里只退休可重建投影；原 owner 文件仍须由远端清单和对象 hash 完整恢复后才交付。
                OwnerSnapshotIndex.retire_projection(projection)
                clean = False
                recovered_index = True
                index = stack.enter_context(OwnerSnapshotIndex.open(projection))
            identity = [tenant, owner_kind, owner_id, str(owner_home)]
            metadata = index.metadata()
            journal = self._journal()
            home_root.mkdir(parents=True, exist_ok=True)

            def observe(clock):
                warm_guard.verify()
                home_guard.verify()
                owner_guard.verify()
                result = journal.query(home_root, clock, control_root=warm_dir)
                warm_guard.verify()
                home_guard.verify()
                owner_guard.verify()
                return result

            batch = observe(str(metadata.get('clock') or ''))
            valid = (clean and type(metadata.get('version')) is int and 0 <= metadata['version'] <= version
                     and metadata.get('identity') == identity and original_owner_identity is not None and batch.complete)
            reason = ''
            remote_paths = set()
            if valid and metadata['version'] != version:
                delta = self._read_manifest_delta(tenant, owner_kind, owner_id, after=metadata['version'], through=version)
                valid = delta.complete
                reason = delta.reason
                if valid:
                    for relative, row in delta.entries:
                        remote_paths.add(relative)
                        if row is None:
                            index.forget(relative)
                        else:
                            index.set_manifest(row)
            if not valid:
                if not clean:
                    reason = 'cache_projection_unreadable' if recovered_index else 'cache_dirty_or_cold'
                elif metadata.get('identity') != identity:
                    reason = 'cache_identity_changed'
                elif type(metadata.get('version')) is not int or metadata['version'] != version:
                    reason = reason or 'remote_version_changed'
                elif original_owner_identity is None:
                    reason = 'owner_home_missing'
                else:
                    reason = batch.reason or 'observer_coverage_unconfirmed'
                rows = self._read_manifest(tenant, owner_kind, owner_id)
                self._prune_warm_home(home_root, owner_home, expected_root=home_identity)
                restored = (not clean or original_owner_identity is None
                            or any(path.is_symlink() for path in self._warm_paths(owner_home)))
                if restored:
                    if owner_home.exists() and not owner_home.is_dir():
                        raise NoFollowPathError('owner snapshot root is not a directory; original entry preserved')
                    self.restore(tenant, owner_kind, owner_id, owner_home, expected_root=owner_identity)
                    index.reset(rows)
                    for row in rows:
                        index.record(row.relative_path, (owner_home / _safe_relative(row.relative_path)).lstat(),
                                     row.content_sha256, time.time())
                else:
                    wanted = {row.relative_path for row in rows}
                    for stale in index.paths() - wanted:
                        index.forget(stale)
                    for row in rows:
                        index.set_manifest(row)
                # 完整校对是显式恢复边界；先装watch再扫描，期间变化必须在cookie之后再次校对。
                batch = observe(batch.clock)
                self._reconcile_warm(owner_home, rows, index, expected_root=owner_identity)
                for _ in range(3):
                    after = observe(batch.clock)
                    if not after.complete:
                        batch = after
                        break
                    pending, foreign = self._observed_paths(home_root, owner_home, after.paths, index, expected_root=owner_identity)
                    if not pending and not foreign:
                        batch = after
                        break
                    self._reconcile_observed(home_root, owner_home, pending, foreign, index,
                                             expected_root=owner_identity, expected_home=home_identity)
                    batch = after
                else:
                    raise RuntimeError('owner 缓存校对期间持续变动，保留 DIRTY 等待重试')
            else:
                for _ in range(3):
                    pending, foreign = self._observed_paths(home_root, owner_home, batch.paths, index, expected_root=owner_identity)
                    pending.update(remote_paths)
                    remote_paths.clear()
                    self._reconcile_observed(home_root, owner_home, pending, foreign, index,
                                             expected_root=owner_identity, expected_home=home_identity)
                    after = observe(batch.clock)
                    if not after.complete:
                        raise RuntimeError('owner 观察器在温缓存校对中丢失覆盖，保留 DIRTY')
                    batch = after
                    pending, foreign = self._observed_paths(home_root, owner_home, batch.paths, index, expected_root=owner_identity)
                    if not pending and not foreign:
                        break
                else:
                    raise RuntimeError('owner 温缓存持续变动，保留 DIRTY 等待重试')
            start_clock = batch.clock
            yield owner_home
            changed_batch = observe(start_clock)
            if changed_batch.complete:
                pending, foreign = self._observed_paths(home_root, owner_home, changed_batch.paths, index, expected_root=owner_identity)
                if foreign:
                    self._prune_warm_home(home_root, owner_home, expected_root=home_identity)
                for _ in range(3):
                    removed, changed = self._snapshot_observed(tenant, owner_home, warm_dir, pending, index,
                                                              expected_root=owner_identity)
                    after = observe(changed_batch.clock)
                    if not after.complete:
                        changed_batch = after
                        break
                    more, foreign = self._observed_paths(home_root, owner_home, after.paths, index, expected_root=owner_identity)
                    changed_batch = after
                    if foreign:
                        self._prune_warm_home(home_root, owner_home, expected_root=home_identity)
                    if not more:
                        break
                    pending.update(more)
                else:
                    raise RuntimeError('owner 快照期间持续变动，保留 DIRTY 等待重试')
            if not changed_batch.complete:
                reason = reason or changed_batch.reason or 'observer_coverage_lost'
                rows = [OwnerFile(**row) for row in index.manifest().values()]
                removed, changed, verified = self._snapshot_delta(tenant, owner_home, warm_dir, rows, index,
                                                                  expected_root=owner_identity)
                changed_batch = observe(changed_batch.clock)
                _validate_verified_files(verified)
            warm_guard.verify()
            home_guard.verify()
            owner_guard.verify()
            committed = head.commit(tenant, owner_kind, owner_id, incarnation=incarnation, removed=removed, changed=changed,
                                    expected_version=version)
            warm_guard.verify()
            home_guard.verify()
            owner_guard.verify()
            for relative in removed:
                index.forget(relative)
            for row in changed:
                index.set_manifest(row)
            index.set_metadata({'identity': identity, 'version': committed, 'clock': changed_batch.clock,
                                'mode': 'full_reconcile' if reason else 'incremental', 'reason': reason,
                                'recovered_index': recovered_index})
            index.conn.commit()
            write_private_json_file_atomic_unlocked(state_path, {"state": "CLEAN"})
            with SnapshotParent(warm_dir) as control:
                for name in ('stat-index.json', 'staging'):
                    try:
                        info = control.info(name)
                    except FileNotFoundError:
                        continue
                    control.remove(name, info)

    def _journal(self):
        journal = getattr(self, '_changes', None)
        if journal is None:
            self._changes = journal = OwnerChangeJournal()
        return journal

    def start(self):
        self._journal().start()

    def release_warm(self, warm_dir):
        journal = getattr(self, '_changes', None)
        if journal is not None:
            journal.release(Path(warm_dir) / 'home')

    def close(self):
        journal = getattr(self, '_changes', None)
        if journal is not None:
            journal.close()

    def _observed_paths(self, home_root, owner_home, events, index, *, expected_root=None):
        with SnapshotParent(owner_home, expected_root=expected_root) as owner:
            root_identity = owner.root_identity
        prefix = owner_home.relative_to(home_root).as_posix()
        paths, subtrees, foreign, whole_root = set(), set(), False, False
        for event in events:
            name = event['name']
            if name == prefix:
                whole_root = whole_root or not event['exists'] or event.get('new')
                continue
            if not name.startswith(prefix + '/'):
                if prefix.startswith(name + '/') and event.get('type') == 'd' and event['exists']:
                    continue
                foreign = True
                continue
            relative = name[len(prefix) + 1:]
            _safe_relative(relative)
            if event.get('type') == 'd' and event['exists'] and not event.get('new'):
                continue
            paths.add(relative)
            if event.get('type') == 'd' or not event['exists'] and event.get('type') not in {'f', 'l'}:
                subtrees.add(relative)
        if whole_root:
            if owner_home.is_symlink():
                raise RuntimeError('owner 快照拒绝符号链接根')
            paths.update(index.paths())
            if owner_home.is_dir():
                paths.update(path.relative_to(owner_home).as_posix() for path in self._warm_paths(owner_home))
            return paths, foreign
        roots = set()
        # 同一批嵌套目录事件只展开最外层，避免 rename/new-subtree 引出重复子树扫描。
        for relative in sorted(subtrees, key=lambda value: (value.count('/'), value)):
            if not any(parent.as_posix() in roots for parent in _safe_relative(relative).parents):
                roots.add(relative)
        for relative in roots:
            paths.update(index.subtree(relative))
            target = owner_home / _safe_relative(relative)
            if target.is_dir() and not target.is_symlink():
                with SnapshotParent(owner_home, _safe_relative(relative).parts,
                                    expected_root=root_identity) as selected:
                    paths.update(path.relative_to(owner_home).as_posix() for path in
                        self._warm_paths(target, expected_root=selected.identity))
        return paths, foreign

    def _reconcile_observed(self, home_root, owner_home, paths, foreign, index, *, expected_root=None, expected_home=None):
        if foreign:
            self._prune_warm_home(home_root, owner_home, expected_root=expected_home)
        previous = index.manifest(paths)
        with SnapshotParent(owner_home, expected_root=expected_root) as owner:
            root_identity = owner.root_identity
        for relative in sorted(paths, key=lambda value: (value.count('/'), value), reverse=True):
            selected = _safe_relative(relative)
            if relative not in previous:
                try:
                    with SnapshotParent(owner_home, selected.parts[:-1], expected_root=root_identity) as parent:
                        try:
                            info = parent.info(selected.name)
                        except FileNotFoundError:
                            info = None
                        if info is not None:
                            try:
                                parent.remove(selected.name, info, recursive=False)
                            except OSError as exc:
                                # 仍有被索引后代的目录可保留；权限、换链等其它错误不能吞成已删。
                                import errno

                                if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                                    raise
                except FileNotFoundError:
                    pass
                index.forget(relative)
        for relative, value in previous.items():
            row = OwnerFile(**value)
            selected = _safe_relative(relative)
            target = owner_home / selected
            valid = False
            try:
                with SnapshotParent(owner_home, selected.parts[:-1], expected_root=root_identity) as parent:
                    try:
                        info = parent.info(selected.name)
                    except FileNotFoundError:
                        info = None
                    valid = (info is not None and stat.S_ISREG(info.st_mode) and info.st_size == row.size_bytes
                             and stat.S_IMODE(info.st_mode) == row.file_mode
                             and _stable_file_digest(target, expected_stat=info, owner_root=owner_home,
                                expected_root=root_identity)[0] == row.content_sha256)
                    parent.verify()
            except FileNotFoundError:
                pass
            if not valid:
                self._download_row(owner_home, row, expected_root=root_identity)
            with SnapshotParent(owner_home, selected.parts[:-1], expected_root=root_identity) as parent:
                index.record(relative, parent.info(selected.name), row.content_sha256, time.time())

    def _snapshot_observed(self, tenant, owner_home, warm_dir, paths, index, *, expected_root=None):
        with SnapshotParent(owner_home, expected_root=expected_root) as owner:
            root_identity = owner.root_identity
        previous = index.manifest(paths)
        removed, changed = [], []
        for relative in sorted(paths):
            target = owner_home / _safe_relative(relative)
            try:
                info = target.lstat()
            except FileNotFoundError:
                if relative in previous:
                    removed.append(relative)
                continue
            if stat.S_ISLNK(info.st_mode):
                raise RuntimeError(f'owner 快照拒绝符号链接: {relative}')
            if not stat.S_ISREG(info.st_mode):
                if relative in previous:
                    removed.append(relative)
                continue
            if Path(relative).parts[0] in self._EPHEMERAL_ROOTS:
                continue
            staged, digest, size = stage_and_hash(target, warm_dir / 'staging',
                owner_root=owner_home, expected_stat=info, expected_root=root_identity)
            try:
                after = target.lstat()
                fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_mode', 'st_nlink')
                if any(getattr(info, field) != getattr(after, field) for field in fields):
                    raise RuntimeError(f'owner 文件在快照时仍被写入: {target}')
                key = self._object_key(tenant, digest)
                old = previous.get(relative)
                row = OwnerFile(relative, key, digest, size, stat.S_IMODE(info.st_mode))
                if old is None or row != OwnerFile(**old):
                    self._upload(staged, key)
                    changed.append(row)
            finally:
                staged.unlink(missing_ok=True)
            index.record(relative, target.lstat(), digest, time.time())
        return removed, changed

    # LLM: 显式扫描不吞权限等枚举错误，不能将漏读路径解释为删除；符号链接只列出而不跟随。
    # 函数用途: 完整列举温缓存子树，任何不可读目录都使本轮失败。
    def _warm_paths(self, owner_home: Path, *, expected_root=None) -> list[Path]:
        with SnapshotParent(owner_home, expected_root=expected_root) as parent:
            return sorted(owner_home.joinpath(*parts) for parts in tree_snapshot(parent))

    # LLM: 只遍历 owner 祖先链的兄弟节点；不跟随兄弟符号链接，不扫描其它子树。
    # 函数用途: 清除温缓存家根中不属于当前 owner 的路径。
    def _prune_warm_home(self, home_root: Path, owner_home: Path, *, expected_root=None) -> None:
        try:
            with SnapshotParent(home_root, expected_root=expected_root) as root:
                root_identity = root.root_identity
        except FileNotFoundError:
            return
        parts = owner_home.relative_to(home_root).parts
        for position, selected in enumerate(parts):
            with SnapshotParent(home_root, parts[:position], expected_root=root_identity) as parent:
                try:
                    kept = parent.info(selected)
                except FileNotFoundError:
                    kept = None
                if kept is not None and (not stat.S_ISDIR(kept.st_mode) or stat.S_ISLNK(kept.st_mode)
                        or getattr(kept, 'st_file_attributes', 0) & 0x400):
                    raise NoFollowPathError('owner snapshot selected ancestor changed type')
                for name in parent.names():
                    if name != selected:
                        parent.remove(name, parent.info(name))
                if kept is None:
                    return
                if _identity_pair(parent.info(selected)) != _identity_pair(kept):
                    raise NoFollowPathError('owner snapshot selected ancestor changed')

    # LLM: 与 full restore 的单行下载和摘要检查一致；调用方先消除路径类型冲突。
    # 函数用途: 下载并校验一行清单对应的文件。
    def _download_row(self, owner_home: Path, row: OwnerFile, *, expected_root=None) -> None:
        relative = _safe_relative(row.relative_path)
        with SnapshotParent(owner_home, relative.parts[:-1], create=True, expected_root=expected_root) as original:
            root_identity, parent_identity = original.root_identity, original.identity
            try:
                leaf_signature = original.entry_signature(relative.name)
            except FileNotFoundError:
                leaf_signature = None
        # 网络下载期间不持owner目录门；私有下载区不在可变workspace中，发表再绑定同一原父目录。
        with downloaded_object(self._client, self._bucket, row) as source:
            with SnapshotParent(owner_home, relative.parts[:-1], expected_root=root_identity) as parent:
                if parent.identity != parent_identity:
                    raise NoFollowPathError('owner snapshot download parent changed')
                try:
                    current = parent.entry_signature(relative.name)
                except FileNotFoundError:
                    current = None
                if current != leaf_signature:
                    raise NoFollowPathError('owner snapshot destination changed while downloading')
                if current is not None and stat.S_ISDIR(parent.info(relative.name).st_mode):
                    parent.remove(relative.name, parent.info(relative.name))
                parent.publish(relative.name, source, row)

    # LLM: 每行对照权威摘要和完整文件状态，不信任版本号；先删除阻挡新目录的旧文件及所有额外路径。
    # 函数用途: 将 CLEAN 缓存逐行校对成与完整恢复相同的文件树。
    def _reconcile_warm(self, owner_home: Path, rows: list[OwnerFile], index: OwnerStatIndex, *, expected_root=None) -> None:
        files = {_safe_relative(row.relative_path) for row in rows}
        directories = {parent for path in files for parent in path.parents if parent != Path(".")}
        with SnapshotParent(owner_home, expected_root=expected_root) as owner:
            root_identity = owner.root_identity
            paths = self._warm_paths(owner_home, expected_root=root_identity)
            owner.verify()
        for path in reversed(paths):
            rel = path.relative_to(owner_home)
            try:
                with SnapshotParent(owner_home, rel.parts[:-1], expected_root=root_identity) as parent:
                    info = parent.info(rel.name)
                    if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                        if rel not in directories:
                            parent.remove(rel.name, info)
                    elif rel not in files:
                        parent.remove(rel.name, info)
            except FileNotFoundError:
                pass
        for rel in index.paths() - {row.relative_path for row in rows}:
            index.forget(rel)
        verified = []
        for row in rows:
            relative = _safe_relative(row.relative_path)
            target = owner_home / relative
            valid = False
            try:
                with SnapshotParent(owner_home, relative.parts[:-1], expected_root=root_identity) as parent:
                    try:
                        info = parent.info(relative.name)
                    except FileNotFoundError:
                        info = None
                    if (info is not None and stat.S_ISREG(info.st_mode) and info.st_size == row.size_bytes
                            and stat.S_IMODE(info.st_mode) == row.file_mode):
                        digest = _cached_file_digest(index, row.relative_path, info)
                        refreshed = digest is None
                        signature = _owner_file_signature(info)
                        if refreshed:
                            # racy只拒绝复用旧摘要；同一可信父锚点和FD重算，不等同必须重下对象。
                            digest, _size, signature = _stable_file_digest(target, expected_stat=info,
                                owner_root=owner_home, expected_root=root_identity)
                        if digest == row.content_sha256:
                            verified.append((target, signature, owner_home, root_identity, digest))
                            if refreshed:
                                index.record(row.relative_path, info, digest, time.time())
                            valid = True
                    parent.verify()
            except FileNotFoundError:
                pass
            if not valid:
                self._download_row(owner_home, row, expected_root=root_identity)
                with SnapshotParent(owner_home, relative.parts[:-1], expected_root=root_identity) as parent:
                    index.record(row.relative_path, parent.info(relative.name), row.content_sha256, time.time())
        _validate_verified_files(verified)

    # LLM: 只上传独立暂存副本，先上传后围栏提交；symlink 与临时根过滤保持 full 的遍历语义。
    # 函数用途: 计算本轮移除路径和变化清单行并更新本地签名。
    def _snapshot_delta(self, tenant: str, owner_home: Path, warm_dir: Path, rows: list[OwnerFile],
                        index: OwnerStatIndex, *, expected_root=None):
        with SnapshotParent(owner_home, expected_root=expected_root) as owner:
            root_identity = owner.root_identity
        previous = {row.relative_path: row for row in rows}
        seen: set[str] = set()
        changed: list[OwnerFile] = []
        verified = []
        for path in self._warm_paths(owner_home, expected_root=root_identity):
            st = path.lstat()
            if stat.S_ISLNK(st.st_mode):
                raise RuntimeError(f"owner 快照拒绝符号链接: {path.relative_to(owner_home)}")
            if not stat.S_ISREG(st.st_mode):
                continue
            relative = path.relative_to(owner_home)
            if relative.parts and relative.parts[0] in self._EPHEMERAL_ROOTS:
                continue
            rel = relative.as_posix()
            seen.add(rel)
            old = previous.get(rel)
            if old is not None and st.st_size == old.size_bytes and stat.S_IMODE(st.st_mode) == old.file_mode:
                digest = _cached_file_digest(index, rel, st)
                refreshed = digest is None
                signature = _owner_file_signature(st)
                if digest is None:
                    digest, _size, signature = _stable_file_digest(path, expected_stat=st,
                        owner_root=owner_home, expected_root=root_identity)
                if digest == old.content_sha256:
                    # 未变文件不暂存或远端HEAD；近期mtime仍为racy，下轮必须重新核验正文。
                    verified.append((path, signature, owner_home, root_identity, digest))
                    if refreshed:
                        index.record(rel, st, digest, time.time())
                    continue
            staged, digest, size = stage_and_hash(path, warm_dir / "staging",
                owner_root=owner_home, expected_stat=st, expected_root=root_identity)
            try:
                key = self._object_key(tenant, digest)
                row = OwnerFile(rel, key, digest, size, stat.S_IMODE(st.st_mode))
                if row != old:
                    if old is None or old.object_key != key:
                        self._upload(staged, key)
                    changed.append(row)
            finally:
                staged.unlink(missing_ok=True)
            index.record(rel, st, digest, time.time())
        _validate_verified_files(verified)
        for rel in index.paths() - seen:
            index.forget(rel)
        return sorted(previous.keys() - seen), changed, verified

    def require_ready(self) -> None:
        """启动硬门：bucket 可达且已开版本控制，灾难恢复不能靠覆盖后的单一对象。"""
        OwnerChangeJournal.require_available()
        self._client.head_bucket(Bucket=self._bucket)
        versioning = self._client.get_bucket_versioning(Bucket=self._bucket)
        if str(versioning.get("Status") or "") != "Enabled":
            raise RuntimeError("owner object bucket 必须启用 versioning")
        key = "/".join(part for part in (self._prefix, "_health", "owner-store-ready-v1") if part)
        payload = b"my-agent-owner-store-ready-v1"
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=payload,
                IfNoneMatch="*",
                **_encryption_args(self._sse, self._kms_key),
            )
        except Exception as exc:
            response = getattr(exc, "response", {})
            code = str((response.get("Error") or {}).get("Code") or "")
            if code not in {"412", "PreconditionFailed"}:
                raise
        body = self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        if body != payload:
            raise RuntimeError("owner object bucket 健康对象校验失败")
        missing = "/".join(part for part in (self._prefix, "_health", "owner-store-missing-v1") if part)
        try:
            self._client.head_object(Bucket=self._bucket, Key=missing)
        except Exception as exc:
            response = getattr(exc, "response", {})
            code = str((response.get("Error") or {}).get("Code") or "")
            if code in {"404", "NoSuchKey", "NotFound"}:
                return
            raise
        raise RuntimeError("owner object bucket 保留的 missing probe key 不应存在")

    def restore(self, tenant: str, owner_kind: str, owner_id: str, owner_home: Path, *, expected_root=None) -> int:
        rows = self._read_manifest(tenant, owner_kind, owner_id)
        for row in rows:
            _safe_relative(row.relative_path)
        with SnapshotParent(owner_home, create=True, expected_root=expected_root) as original:
            root_identity = original.root_identity
            before = tree_snapshot(original)
        with DownloadedObjects(self._client, self._bucket) as batch:
            ready = [(row, batch.fetch(row)) for row in rows]
            # 所有远端hash通过后才清理；下载期间目录/文件变动则保留原树和替换者，交DIRTY恢复。
            with SnapshotParent(owner_home, expected_root=root_identity) as parent:
                if tree_snapshot(parent) != before:
                    raise NoFollowPathError('owner snapshot restore source tree changed')
                for name in parent.names():
                    parent.remove(name, parent.info(name))
            for row, name in ready:
                relative = _safe_relative(row.relative_path)
                with batch.open(name, row) as source:
                    with SnapshotParent(owner_home, relative.parts[:-1], create=True,
                                        expected_root=root_identity) as parent:
                        parent.publish(relative.name, source, row)
        return len(rows)

    def commit(self, tenant: str, owner_kind: str, owner_id: str, owner_home: Path) -> int:
        rows: list[OwnerFile] = []
        verified = []
        with SnapshotParent(owner_home) as parent:
            owner_identity = parent.root_identity
        # stage不在可写owner树内；一次只保留一个文件，上传不会重新读取已换名的业务路径。
        with tempfile.TemporaryDirectory(prefix='my-agent-owner-upload-') as temporary:
            stage_root = Path(temporary).resolve()
            if stage_root.is_relative_to(owner_home.resolve()):
                raise RuntimeError('owner 上传暂存目录位于可写owner内；清单尚未提交，请将宿主临时目录配置到owner外')
            for path in sorted(owner_home.rglob("*")):
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode):
                    raise RuntimeError(f"owner 快照拒绝符号链接: {path.relative_to(owner_home)}")
                if not stat.S_ISREG(info.st_mode):
                    continue
                relative = path.relative_to(owner_home)
                if relative.parts and relative.parts[0] in self._EPHEMERAL_ROOTS:
                    continue
                staged, digest, size, signature = _stage_full_owner_file(owner_home, relative.parts,
                    stage_root, expected_stat=info, expected_root=owner_identity)
                try:
                    key = self._object_key(tenant, digest)
                    self._upload(staged, key)
                finally:
                    staged.unlink(missing_ok=True)
                verified.append((path, signature, digest, size))
                rows.append(OwnerFile(relative.as_posix(), key, digest, size, stat.S_IMODE(info.st_mode)))
            for source, signature, digest, size in verified:
                relative = source.relative_to(owner_home).parts
                with SnapshotParent(owner_home, relative[:-1], expected_root=owner_identity) as parent:
                    descriptor = parent.open_read(relative[-1])
                    try:
                        if file_signature(descriptor) != signature:
                            raise RuntimeError(f'owner 文件在快照时仍被写入: {source}')
                        current_digest, current_size = descriptor_digest(descriptor, size)
                        if (current_digest != digest or current_size != size
                                or file_signature(descriptor) != signature):
                            raise RuntimeError(f'owner 文件在快照时仍被写入: {source}')
                        parent.verify()
                    finally:
                        os.close(descriptor)
            self._replace_manifest(tenant, owner_kind, owner_id, rows)
        return len(rows)

    @contextmanager
    def _owner_lock(self, tenant: str, owner_kind: str, owner_id: str) -> Iterator[None]:
        owner_ref = f"{tenant}\x1f{owner_kind}\x1f{owner_id}"
        with self._backend.connect() as conn:
            conn.execute(
                text("SELECT pg_advisory_lock(hashtextextended(:owner_ref, 0))"),
                {"owner_ref": owner_ref},
            )
            try:
                yield
            finally:
                conn.execute(
                    text("SELECT pg_advisory_unlock(hashtextextended(:owner_ref, 0))"),
                    {"owner_ref": owner_ref},
                )

    def _read_manifest_delta(self, tenant, owner_kind, owner_id, *, after, through):
        with tenant_session(self._backend, tenant) as conn:
            current = conn.execute(text('SELECT version,change_floor FROM owner_snapshot_head WHERE tenant=:tenant '
                                        'AND owner_kind=:kind AND owner_id=:owner'),
                                   {'tenant': tenant, 'kind': owner_kind, 'owner': owner_id}).one()
            if current.version != through:
                raise OwnerSnapshotLeaseLost('owner 清单在取得租约后发生变化')
            if after < current.change_floor:
                return OwnerManifestDelta(False, (), 'remote_change_cursor_expired')
            rows = conn.execute(text('''SELECT c.relative_path,c.removed,m.object_key,m.content_sha256,m.size_bytes,m.file_mode
                FROM owner_file_changes c LEFT JOIN owner_file_manifest m ON m.tenant=c.tenant
                AND m.owner_kind=c.owner_kind AND m.owner_id=c.owner_id AND m.relative_path=c.relative_path
                WHERE c.tenant=:tenant AND c.owner_kind=:kind AND c.owner_id=:owner
                AND c.version>:after AND c.version<=:through ORDER BY c.version'''),
                                {'tenant': tenant, 'kind': owner_kind, 'owner': owner_id,
                                 'after': after, 'through': through}).all()
        entries = []
        for row in rows:
            _safe_relative(row.relative_path)
            if row.removed:
                if row.object_key is not None:
                    raise RuntimeError('owner 删除投影与权威清单冲突')
                entries.append((row.relative_path, None))
            else:
                if row.object_key is None:
                    raise RuntimeError('owner 变更投影缺少权威清单行')
                entries.append((row.relative_path, OwnerFile(row.relative_path, row.object_key, row.content_sha256,
                                                            int(row.size_bytes), int(row.file_mode))))
        return OwnerManifestDelta(True, tuple(entries))

    def _read_manifest(self, tenant: str, owner_kind: str, owner_id: str) -> list[OwnerFile]:
        with tenant_session(self._backend, tenant) as conn:
            result = conn.execute(
                text(
                    "SELECT relative_path, object_key, content_sha256, size_bytes, file_mode "
                    "FROM owner_file_manifest WHERE owner_kind=:kind AND owner_id=:owner "
                    "ORDER BY relative_path"
                ),
                {"kind": owner_kind, "owner": owner_id},
            ).all()
        return [OwnerFile(str(r.relative_path), str(r.object_key), str(r.content_sha256), int(r.size_bytes), int(r.file_mode)) for r in result]

    def _replace_manifest(
        self,
        tenant: str,
        owner_kind: str,
        owner_id: str,
        rows: list[OwnerFile],
    ) -> None:
        now = int(time.time() * 1000)
        with tenant_session(self._backend, tenant) as conn:
            conn.execute(
                text("DELETE FROM owner_file_manifest WHERE owner_kind=:kind AND owner_id=:owner"),
                {"kind": owner_kind, "owner": owner_id},
            )
            if rows:
                conn.execute(
                    text(
                        "INSERT INTO owner_file_manifest "
                        "(tenant, owner_kind, owner_id, relative_path, object_key, content_sha256, "
                        "size_bytes, file_mode, updated_at) VALUES "
                        "(:tenant, :kind, :owner, :path, :object_key, :sha, :size, :mode, :updated)"
                    ),
                    [
                        {
                            "tenant": tenant,
                            "kind": owner_kind,
                            "owner": owner_id,
                            "path": row.relative_path,
                            "object_key": row.object_key,
                            "sha": row.content_sha256,
                            "size": row.size_bytes,
                            "mode": row.file_mode,
                            "updated": now,
                        }
                        for row in rows
                    ],
                )

    def _object_key(self, tenant: str, digest: str) -> str:
        tenant_ref = hashlib.sha256(tenant.encode()).hexdigest()[:20]
        parts = [part for part in (self._prefix, "owners", tenant_ref, digest[:2], digest) if part]
        return "/".join(parts)

    def _upload(self, path: OwnerStagedFile, key: str) -> None:
        if not isinstance(path, OwnerStagedFile):
            raise RuntimeError('OWNER_SNAPSHOT_STAGE_REQUIRED: 上传只接受原受管stage FD；未上传，请经stage_and_hash重建')
        expected_digest = key.rsplit('/', 1)[-1]
        expected_size = path.stat().st_size
        try:
            response = self._client.head_object(Bucket=self._bucket, Key=key)
        except Exception as exc:  # boto ClientError是可选依赖；只有原不存在码允许上传。
            response = getattr(exc, "response", {})
            code = str((response.get("Error") or {}).get("Code") or "")
            if code not in {"404", "NoSuchKey", "NotFound"}:
                raise
        else:
            # 只信服务端完整对象校验和，用户metadata与multipart ETag均不作正文证明。
            checksum = response.get('ChecksumSHA256') if isinstance(response, dict) else None
            if checksum and response.get('ChecksumType') == 'FULL_OBJECT':
                if (checksum != base64.b64encode(bytes.fromhex(expected_digest)).decode('ascii')
                        or response.get('ContentLength') != expected_size):
                    raise RuntimeError('owner 已存在对象摘要或大小不符；清单尚未提交，须核实对象存储完整性')
                return
            body = self._client.get_object(Bucket=self._bucket, Key=key)['Body']
            digest, size = hashlib.sha256(), 0
            primary = None
            try:
                while True:
                    chunk = body.read(min(65536, expected_size - size + 1))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > expected_size:
                        raise RuntimeError('owner 已存在对象大小不符；清单尚未提交，须核实对象存储完整性')
                    digest.update(chunk)
                if size != expected_size or digest.hexdigest() != expected_digest:
                    raise RuntimeError('owner 已存在对象摘要或大小不符；清单尚未提交，须核实对象存储完整性')
            except BaseException as error:
                primary = error
                raise
            finally:
                try:
                    body.close()
                except BaseException as error:
                    if primary is None:
                        raise
                    primary.add_note(f'owner object stream close failed: {type(error).__name__}')
            return
        extra = _encryption_args(self._sse, self._kms_key)
        with path.open_reader() as reader:
            self._client.upload_fileobj(reader, self._bucket, key, ExtraArgs=extra)


def owner_store_from_runtime(runtime: ScaleRuntimeConfig, backend: StorageBackend) -> OwnerSnapshotStore:
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover - scale extra 应包含
        raise RuntimeError("S3 owner store 需要 boto3；请安装 my-agent[scale]") from exc
    client = boto3.client(
        "s3",
        endpoint_url=runtime.owner_s3_endpoint or None,
        region_name=runtime.owner_s3_region,
        config=Config(
            connect_timeout=5,
            read_timeout=30,
            retries={"max_attempts": 3, "mode": "standard"},
            tcp_keepalive=True,
        ),
    )
    return OwnerSnapshotStore(
        backend,
        client,
        OwnerObjectConfig(
            bucket=runtime.owner_s3_bucket,
            prefix=runtime.owner_s3_prefix,
            sse=runtime.owner_s3_sse,
            kms_key=runtime.owner_s3_kms_key,
        ),
    )


def require_owner_store_ready(runtime: ScaleRuntimeConfig, backend: StorageBackend) -> None:
    owner_store_from_runtime(runtime, backend).require_ready()


def _safe_relative(value: str) -> Path:
    try:
        return Path(*validate_relative(value))
    except NoFollowPathError as exc:
        raise RuntimeError(f"owner manifest 路径越界: {value!r}") from exc


def _identity_pair(info):
    return info.st_dev, info.st_ino


def _encryption_args(sse: str, kms_key: str) -> dict[str, str]:
    extra: dict[str, str] = {}
    if sse:
        extra["ServerSideEncryption"] = sse
    if sse == "aws:kms":
        extra["SSEKMSKeyId"] = kms_key
    return extra


def _file_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _owner_file_signature(info):
    return tuple(getattr(info, name) for name in
        ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_mode', 'st_nlink'))


def _validate_verified_files(verified):
    for path, signature, owner_root, expected_root, expected_digest in verified:
        digest, _size, current = _stable_file_digest(path, owner_root=owner_root, expected_root=expected_root)
        if current != signature or digest != expected_digest:
            raise RuntimeError(f"owner 文件在快照时仍被写入: {path}")


def _cached_file_digest(index, relative, info):
    # 回填旧mtime不能把同kernel tick的ctime碰撞变成缓存命中；未来时间同样不授予稳定性。
    now = time.time()
    if (os.name == 'nt' or now - info.st_mtime < OWNER_SNAPSHOT_RACY_MTIME_SECONDS
            or now - info.st_ctime < OWNER_SNAPSHOT_RACY_MTIME_SECONDS):
        return None
    return index.lookup(relative, info)


def _stage_full_owner_file(owner_home, parts, staging, *, expected_stat, expected_root=None):
    """源与下载目标共用跨平台父锚点，FD复制同批字节并计算上传内容键。"""
    staged = None
    try:
        with SnapshotParent(owner_home, tuple(parts[:-1]), expected_root=expected_root) as parent:
            descriptor = parent.open_read(parts[-1])
            with os.fdopen(descriptor, 'rb') as source:
                opened = os.fstat(source.fileno())
                if not _snapshot_stat_fields_match(expected_stat, opened, cross_windows=os.name == 'nt'):
                    raise RuntimeError('owner 文件在快照时仍被写入；上传尚未开始')
                signature = file_signature(source.fileno())
                staged, digest, size = stage_open_file(source.fileno(), staging)
                parent.verify()
                current = parent.open_read(parts[-1])
                try:
                    if file_signature(current) != signature:
                        raise RuntimeError('owner 文件在快照时仍被写入；上传尚未开始')
                finally:
                    os.close(current)
        return staged, digest, size, signature
    except BaseException as error:
        if staged is not None:
            try:
                staged.unlink(missing_ok=True)
            except BaseException as cleanup:
                error.add_note(f'owner stage cleanup failed: {type(cleanup).__name__}')
        raise


def _stable_file_digest(path: Path, *, expected_stat=None, owner_root=None,
                        expected_root=None) -> tuple[str, int, tuple[int, ...]]:
    before = expected_stat if expected_stat is not None else path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise RuntimeError(f"owner 快照来源不是常规文件: {path}")
    owner_root = path.parent if owner_root is None else owner_root
    relative = path.relative_to(owner_root)
    with SnapshotParent(owner_root, relative.parts[:-1], expected_root=expected_root) as parent:
        descriptor = parent.open_read(relative.name)
        with os.fdopen(descriptor, 'rb') as source:
            opened = os.fstat(source.fileno())
            if not _snapshot_stat_fields_match(before, opened, cross_windows=os.name == 'nt'):
                raise RuntimeError(f"owner 文件在快照时仍被写入: {path}")
            signature = file_signature(source.fileno())
            digest, size = descriptor_digest(source.fileno(), opened.st_size)
            repeated, repeated_size = descriptor_digest(source.fileno(), opened.st_size)
            if (size != opened.st_size or repeated_size != opened.st_size
                    or digest != repeated or file_signature(source.fileno()) != signature
                    or parent.entry_signature(path.name) != signature):
                raise RuntimeError(f"owner 文件在快照时仍被写入: {path}")
            parent.verify()
    return digest, opened.st_size, signature


__all__ = [
    "OwnerFile",
    "OwnerObjectConfig",
    "OwnerSnapshotStore",
    "OwnerSnapshotLeaseLost",
    "OwnerSnapshotCommitUnknown",
    "owner_store_from_runtime",
    "require_owner_store_ready",
]
