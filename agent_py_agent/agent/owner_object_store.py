"""规模 owner 文件事实源：PostgreSQL/RLS 清单 + S3 对象，Pod 磁盘只作临时缓存。"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from agent_py_agent.agent.pg_rls import tenant_session
from agent_py_agent.agent.scale_runtime import ScaleRuntimeConfig
from agent_py_agent.agent.storage_backend import StorageBackend

try:
    from sqlalchemy import text
except ImportError:  # pragma: no cover - scale extra 在构造 backend 时已给清晰错误
    text = None


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


class OwnerSnapshotStore:
    """每次 owner 执行都恢复并原子替换清单；对象先上传，清单后提交。"""

    _EPHEMERAL_ROOTS = frozenset({"cache", "tmp", "trash"})

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
    "owner_store_from_runtime",
    "require_owner_store_ready",
]
