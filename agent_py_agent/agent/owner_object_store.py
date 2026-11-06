"""规模 owner 文件事实源：PostgreSQL/RLS 清单 + S3 对象，Pod 磁盘只作临时缓存。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import stat
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from agent_py_agent.agent.common.json_io import write_private_json_file_atomic_unlocked
from agent_py_agent.agent.owner_snapshot_index import OwnerStatIndex, stage_and_hash
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


# LLM: 忙租约只在模块内等待，不传播给有重试次数上限的队列。
# 类用途: 标识内部租约竞争，不作为外部失败接口。
class OwnerSnapshotLeaseBusy(RuntimeError):
    """内部租约忙信号；acquire 以 None 表达。"""


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
               removed: list[str], changed: list[OwnerFile]) -> int:
        exiting = False
        now_ms = int(time.time() * 1000)
        try:
            with tenant_session(self._backend, tenant) as conn:
                version = conn.execute(text(
                    "UPDATE owner_snapshot_head SET version = version + 1, holder = NULL, holder_incarnation = NULL, lease_expires_at = NULL, updated_at = :now_ms WHERE tenant = :tenant AND owner_kind = :kind AND owner_id = :owner AND holder_incarnation = :inc AND lease_expires_at > now() RETURNING version"
                ), {"tenant": tenant, "kind": kind, "owner": owner, "inc": incarnation,
                    "now_ms": now_ms}).scalar_one_or_none()
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
    """每次 owner 执行都恢复并原子替换清单；对象先上传，清单后提交。"""

    _EPHEMERAL_ROOTS = frozenset({"cache", "tmp", "trash"})
    _head: _OwnerSnapshotHead | None = None

    # LLM: 构造时只绑定租约助手；full 的恢复、提交和连接锁实现保持原样。
    # 函数用途: 初始化对象存储和可选增量快照依赖。
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
        warm_dir = warm_dir.resolve()
        home_root = warm_dir / "home"
        if not owner_home.resolve().is_relative_to(home_root.resolve()):
            raise ValueError("owner_home 必须位于 warm_dir/home 内")
        owner_home = owner_home.resolve()
        incarnation = uuid.uuid4().hex
        holder = f"{socket.gethostname()}:{os.getpid()}"
        renew_interval = lease_ttl_seconds / 3
        with ExitStack() as stack:
            if hold_advisory_lock:
                stack.enter_context(self._owner_lock(tenant, owner_kind, owner_id))
            while head.acquire(tenant, owner_kind, owner_id, holder=holder,
                               incarnation=incarnation, ttl_seconds=lease_ttl_seconds) is None:
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
            rows = self._read_manifest(tenant, owner_kind, owner_id)
            self._prune_warm_home(home_root, owner_home)
            if (not clean or not owner_home.is_dir()
                    or any(stat.S_ISLNK(path.lstat().st_mode) for path in self._warm_paths(owner_home))):
                if owner_home.exists() and not owner_home.is_dir():
                    owner_home.unlink()
                self.restore(tenant, owner_kind, owner_id, owner_home)
                index = OwnerStatIndex()
                for row in rows:
                    index.record(row.relative_path, (owner_home / _safe_relative(row.relative_path)).lstat(),
                                 row.content_sha256, time.time())
            else:
                index = OwnerStatIndex.load(warm_dir / "stat-index.json")
                self._reconcile_warm(owner_home, rows, index)
            yield owner_home
            removed, changed = self._snapshot_delta(tenant, owner_home, warm_dir, rows, index)
            head.commit(tenant, owner_kind, owner_id, incarnation=incarnation, removed=removed, changed=changed)
            index.save(warm_dir / "stat-index.json")
            write_private_json_file_atomic_unlocked(state_path, {"state": "CLEAN"})
            shutil.rmtree(warm_dir / "staging", ignore_errors=True)

    # LLM: 显式扫描不吞权限等枚举错误，不能将漏读路径解释为删除；符号链接只列出而不跟随。
    # 函数用途: 完整列举温缓存子树，任何不可读目录都使本轮失败。
    def _warm_paths(self, owner_home: Path) -> list[Path]:
        pending = [owner_home]
        paths: list[Path] = []
        while pending:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    paths.append(path)
                    if stat.S_ISDIR(entry.stat(follow_symlinks=False).st_mode):
                        pending.append(path)
        return sorted(paths)

    # LLM: 只遍历 owner 祖先链的兄弟节点；不跟随兄弟符号链接，不扫描其它子树。
    # 函数用途: 清除温缓存家根中不属于当前 owner 的路径。
    def _prune_warm_home(self, home_root: Path, owner_home: Path) -> None:
        current = home_root
        for part in owner_home.relative_to(home_root).parts:
            if not current.exists():
                break
            for sibling in current.iterdir():
                if sibling.name != part:
                    if sibling.is_symlink() or not sibling.is_dir():
                        sibling.unlink()
                    else:
                        shutil.rmtree(sibling)
            current = current / part

    # LLM: 与 full restore 的单行下载和摘要检查一致；调用方先消除路径类型冲突。
    # 函数用途: 下载并校验一行清单对应的文件。
    def _download_row(self, owner_home: Path, row: OwnerFile) -> None:
        target = owner_home / _safe_relative(row.relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.restore")
        self._client.download_file(self._bucket, row.object_key, str(temporary))
        digest, size = _file_digest(temporary)
        if digest != row.content_sha256 or size != row.size_bytes:
            temporary.unlink(missing_ok=True)
            raise RuntimeError(f"owner object 校验失败: {row.relative_path}")
        os.chmod(temporary, row.file_mode & 0o777)
        temporary.replace(target)

    # LLM: 每行对照权威摘要和完整文件状态，不信任版本号；先删除阻挡新目录的旧文件及所有额外路径。
    # 函数用途: 将 CLEAN 缓存逐行校对成与完整恢复相同的文件树。
    def _reconcile_warm(self, owner_home: Path, rows: list[OwnerFile], index: OwnerStatIndex) -> None:
        files = {_safe_relative(row.relative_path) for row in rows}
        directories = {parent for path in files for parent in path.parents if parent != Path(".")}
        for path in reversed(self._warm_paths(owner_home)):
            rel = path.relative_to(owner_home)
            if path.is_dir() and not path.is_symlink():
                if rel not in directories:
                    shutil.rmtree(path)
            elif rel not in files:
                path.unlink()
        for rel in index.paths() - {row.relative_path for row in rows}:
            index.forget(rel)
        for row in rows:
            target = owner_home / _safe_relative(row.relative_path)
            st = target.lstat() if target.exists() else None
            if (st is not None and stat.S_ISREG(st.st_mode) and st.st_size == row.size_bytes
                    and stat.S_IMODE(st.st_mode) == row.file_mode
                    and index.lookup(row.relative_path, st) == row.content_sha256):
                continue
            self._download_row(owner_home, row)
            index.record(row.relative_path, target.lstat(), row.content_sha256, time.time())

    # LLM: 只上传独立暂存副本，先上传后围栏提交；symlink 与临时根过滤保持 full 的遍历语义。
    # 函数用途: 计算本轮移除路径和变化清单行并更新本地签名。
    def _snapshot_delta(self, tenant: str, owner_home: Path, warm_dir: Path, rows: list[OwnerFile],
                        index: OwnerStatIndex) -> tuple[list[str], list[OwnerFile]]:
        previous = {row.relative_path: row for row in rows}
        seen: set[str] = set()
        changed: list[OwnerFile] = []
        for path in self._warm_paths(owner_home):
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
            if (old is not None and index.lookup(rel, st) == old.content_sha256
                    and st.st_size == old.size_bytes and stat.S_IMODE(st.st_mode) == old.file_mode):
                continue
            staged, digest, size = stage_and_hash(path, warm_dir / "staging")
            key = self._object_key(tenant, digest)
            try:
                self._upload(staged, key)
            finally:
                staged.unlink(missing_ok=True)
            row = OwnerFile(rel, key, digest, size, stat.S_IMODE(st.st_mode))
            if row != old:
                changed.append(row)
            index.record(rel, st, digest, time.time())
        for rel in index.paths() - seen:
            index.forget(rel)
        return sorted(previous.keys() - seen), changed

    def require_ready(self) -> None:
        """启动硬门：bucket 可达且已开版本控制，灾难恢复不能靠覆盖后的单一对象。"""
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

    @contextmanager
    def checkout(
        self,
        tenant: str,
        owner_kind: str,
        owner_id: str,
        owner_home: Path,
    ) -> Iterator[Path]:
        """跨 Pod 串行同一 owner；异常执行不覆盖上一份已提交快照。"""
        with self._owner_lock(tenant, owner_kind, owner_id):
            self.restore(tenant, owner_kind, owner_id, owner_home)
            try:
                yield owner_home
            except BaseException:
                raise
            else:
                self.commit(tenant, owner_kind, owner_id, owner_home)

    def restore(self, tenant: str, owner_kind: str, owner_id: str, owner_home: Path) -> int:
        rows = self._read_manifest(tenant, owner_kind, owner_id)
        shutil.rmtree(owner_home, ignore_errors=True)
        owner_home.mkdir(parents=True, exist_ok=True)
        for row in rows:
            target = owner_home / _safe_relative(row.relative_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.restore")
            self._client.download_file(self._bucket, row.object_key, str(temporary))
            digest, size = _file_digest(temporary)
            if digest != row.content_sha256 or size != row.size_bytes:
                temporary.unlink(missing_ok=True)
                raise RuntimeError(f"owner object 校验失败: {row.relative_path}")
            os.chmod(temporary, row.file_mode & 0o777)
            temporary.replace(target)
        return len(rows)

    def commit(self, tenant: str, owner_kind: str, owner_id: str, owner_home: Path) -> int:
        rows: list[OwnerFile] = []
        for path in sorted(owner_home.rglob("*")):
            if path.is_symlink():
                raise RuntimeError(f"owner 快照拒绝符号链接: {path.relative_to(owner_home)}")
            if not path.is_file():
                continue
            relative = path.relative_to(owner_home)
            if relative.parts and relative.parts[0] in self._EPHEMERAL_ROOTS:
                continue
            digest, size = _stable_file_digest(path)
            key = self._object_key(tenant, digest)
            self._upload(path, key)
            rows.append(
                OwnerFile(
                    relative.as_posix(),
                    key,
                    digest,
                    size,
                    stat.S_IMODE(path.stat().st_mode),
                )
            )
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

    def _upload(self, path: Path, key: str) -> None:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return
        except Exception as exc:  # boto 的 ClientError 在 scale extra 内，模块本身保持可选导入
            response = getattr(exc, "response", {})
            code = str((response.get("Error") or {}).get("Code") or "")
            if code not in {"404", "NoSuchKey", "NotFound"}:
                raise
        extra = _encryption_args(self._sse, self._kms_key)
        self._client.upload_file(str(path), self._bucket, key, ExtraArgs=extra)


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
    pure = PurePosixPath(value)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise RuntimeError(f"owner manifest 路径越界: {value!r}")
    return Path(*pure.parts)


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


def _stable_file_digest(path: Path) -> tuple[str, int]:
    before = path.stat()
    digest, size = _file_digest(path)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError(f"owner 文件在快照时仍被写入: {path}")
    return digest, size


__all__ = [
    "OwnerFile",
    "OwnerObjectConfig",
    "OwnerSnapshotStore",
    "OwnerSnapshotLeaseLost",
    "OwnerSnapshotCommitUnknown",
    "owner_store_from_runtime",
    "require_owner_store_ready",
]
