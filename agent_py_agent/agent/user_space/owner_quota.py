# LLM: owner 配额继续使用原文件和锁序；显式非阻塞准入只用于有期限管理，默认工具写入保持等待语义。
# LLM: C可选准确FD出生门，scan仍原算法门外；只透传真实native对象，不按码名分类，默认False无新global guard。
# 模块用途: 复用唯一owner配额和锁序，C只收紧实际出生来源。
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from collections import OrderedDict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from ..common.global_write_commit import GlobalWriteCommitUnavailable, execute_global_write
from ..common.global_write_guard import GlobalWriteInWorkerError
from ..common.json_io import locked_json_path, read_json_object_report
from ..common.nofollow_fs import read_bytes_beneath, write_bytes_atomic_beneath
from ..common.owner_write_errors import (
    LocalControlCoordinatorUnavailable,
    OwnerManagementIngressUnavailable,
)

_QUOTA_LOCK_BASENAME = ".owner-quota"
# 原生用量扫描最多 15 秒：扫描挂起不应拖住配额判定，超时按不可用处理。
_NATIVE_USAGE_SCAN_TIMEOUT_SECONDS = 15.0
# 仅用量展示可短期复用碎片目录统计；权威写入准入绝不读取这个缓存。
_DISPLAY_USAGE_TTL_SECONDS = 5.0
_DISPLAY_USAGE_CACHE_LIMIT = 256
_DISPLAY_USAGE_CACHE: OrderedDict[Path, tuple[tuple[int, ...], float, int]] = OrderedDict()
_DISPLAY_USAGE_LOCK = RLock()


@dataclass(frozen=True)
class OwnerQuotaChange:
    """One owner-scoped file size change evaluated under the quota lock.

    ``append`` makes ``final_size`` an added byte count.  This is deliberately
    resolved while the owner lock is held, so two concurrent append operations
    cannot both project from the same stale pre-lock file size.
    """

    path: Path
    final_size: int | None
    append: bool = False


@dataclass(frozen=True)
class OwnerQuotaProjection:
    used_bytes: int
    projected_bytes: int
    max_bytes: int


class OwnerQuotaUnavailable(RuntimeError):
    """The authoritative owner quota or current usage could not be read safely."""


# LLM: 普通本地用户使用显式有限配额；零子代理/深度有意义，bool和遗漏字段不得被强转。
# 类用途: 表达注册账号的四项配额，不改变旧owner的默认解析行为。
@dataclass(frozen=True)
class LocalUserQuotaLimits:
    max_active_agents: int = 2
    max_subagents: int = 2
    max_depth: int = 1
    max_disk_mb: int = 1024

    # LLM: 所有构造路径都校验上下界，防止内部adapter绕过JSON校验。
    # 函数用途: 拒绝无限、负数和类型混淆配额。
    def __post_init__(self):
        bounds = ((self.max_active_agents, 1, 1000), (self.max_subagents, 0, 50),
                  (self.max_depth, 0, 4), (self.max_disk_mb, 1, 1048576))
        if any(type(value) is not int or not low <= value <= high for value, low, high in bounds):
            raise ValueError("invalid local user quota")

    # LLM: 返回新字典供序列化，不能修改冻结配额本身。
    # 函数用途: 投影与现有quota.json相同的四个字段。
    def to_dict(self):
        return {"max_active_agents": self.max_active_agents, "max_subagents": self.max_subagents,
                "max_depth": self.max_depth, "max_disk_mb": self.max_disk_mb}

    # LLM: 显式输入要求字段齐全且无扩展，不隐式补值或缩小超上限请求。
    # 函数用途: 严格读取调用方指定的用户配额。
    @classmethod
    def parse(cls, value):
        if value is None:
            return cls()
        if type(value) is not dict or set(value) != set(cls().to_dict()):
            raise ValueError("invalid local user quota")
        return cls(value["max_active_agents"], value["max_subagents"],
                   value["max_depth"], value["max_disk_mb"])


# LLM: 新账号quota仍只有一个权威文件；严格有界无链接读取，不回退为无限配额。
# 函数用途: 返回普通用户当前配额和修订。
def read_local_user_quota(home_root: Path, owner_root: Path):
    parts = (owner_root / "quota.json").relative_to(home_root).parts
    try:
        raw = read_bytes_beneath(home_root, parts, max_bytes=16384, require_dir_fd=True)
        payload = json.loads(raw) if raw is not None else None
        if type(payload) is not dict or payload.get("schema_version") != "quota.v2":
            raise ValueError("missing quota")
        revision = payload["revision"]
        if type(revision) is not int or revision < 1:
            raise ValueError("invalid revision")
        limits = LocalUserQuotaLimits.parse({key: payload[key] for key in LocalUserQuotaLimits().to_dict()})
        return limits, revision
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise OwnerQuotaUnavailable("local user quota unavailable") from exc


# LLM: 调用方已持用户短锁；本函数沿原owner quota→JSON锁序，仅首次种有限配额，已有值只核验不覆盖。
# 函数用途: 在账号发布前种下并读回唯一quota文件。
def seed_local_user_quota(home_root: Path, owner_root: Path, limits: LocalUserQuotaLimits):
    parts = (owner_root / "quota.json").relative_to(home_root).parts
    payload = {"schema_version": "quota.v2", "revision": 1, **limits.to_dict()}
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    enforcer = OwnerQuotaEnforcer(owner_root, max_bytes=limits.max_disk_mb * 1024 * 1024)
    with enforcer.admission() as admission:
        with locked_json_path(owner_root / "quota.json"):
            existing = read_bytes_beneath(home_root, parts, max_bytes=16384, require_dir_fd=True)
            if existing is None:
                admission.check((OwnerQuotaChange(owner_root / "quota.json", len(encoded)),))
                write_bytes_atomic_beneath(home_root, parts, encoded)
            observed, revision = read_local_user_quota(home_root, owner_root)
            if observed != limits or revision != 1:
                raise OwnerQuotaUnavailable("local user seed differs")
    return revision


# LLM: 锁竞争是暂态事实，与策略/存储不可用分开表达；调用方可安全地按 busy/重试处理，
#   而不能把策略或存储故障也吞成 busy（MC4-2501）。
# 类用途: 表示 owner 配额锁被其它进程/线程暂时持有（由底层 BlockingIOError 产生）。
class OwnerQuotaLockContention(OwnerQuotaUnavailable):
    """The owner quota lock is temporarily held elsewhere; a retry may succeed."""


class OwnerQuotaExceeded(RuntimeError):
    def __init__(self, projection: OwnerQuotaProjection):
        self.projection = projection
        super().__init__(
            "owner 磁盘配额不足: "
            f"used_bytes={projection.used_bytes} "
            f"projected_bytes={projection.projected_bytes} "
            f"max_bytes={projection.max_bytes}"
        )


class OwnerQuotaAdmission:
    """One owner-quota critical section shared by a whole durable mutation.

    Repository writers often have to lock and read their authority file before
    the exact final byte count is known.  Keeping the owner lock outside that
    repository lock establishes one lock order for every writer:

    ``owner quota -> repository/file lock -> mutation``.

    The admission object deliberately rescans usage at ``check`` time.  This
    lets a caller validate the complete multi-file mutation immediately before
    it writes, without estimating from stale pre-lock state.
    """

    def __init__(self, enforcer: OwnerQuotaEnforcer, *, enabled: bool) -> None:
        self._enforcer = enforcer
        self.enabled = bool(enabled)

    def check(self, changes: Iterable[OwnerQuotaChange]) -> OwnerQuotaProjection | None:
        if not self.enabled:
            return None
        relevant = _normalized_owner_changes(self._enforcer.owner_root, changes)
        if not relevant:
            return None
        try:
            used = _owner_usage_bytes_for_admission(self._enforcer.owner_root)
            current_sizes = {path: _logical_file_size(path) for path in relevant}
        except (OSError, RuntimeError, ValueError) as exc:
            raise OwnerQuotaUnavailable("owner disk usage is unavailable") from exc
        replaced = sum(current_sizes.values())
        final = sum(
            0
            if change.final_size is None
            else (
                current_sizes[path] + change.final_size
                if change.append
                else change.final_size
            )
            for path, change in relevant.items()
        )
        projection = OwnerQuotaProjection(
            used_bytes=used,
            projected_bytes=max(0, used - replaced + final),
            max_bytes=self._enforcer.max_bytes,
        )
        if projection.projected_bytes > projection.max_bytes and projection.projected_bytes > used:
            raise OwnerQuotaExceeded(projection)
        return projection


# LLM: 唯一 owner 配额锁始终先于领域文件锁；非阻塞模式不授予绕锁权限，等待选择不改变持久策略。
# 类用途: 为用户目录内的结构化写入提供跨线程、跨进程的统一空间准入。
class OwnerQuotaEnforcer:
    """Cross-process admission gate for structured writes inside one owner home.

    All file tools share the same owner-local lock.  The gate scans the
    canonical owner home while holding that lock, computes the logical final
    size of the whole batch, and only then lets the mutation proceed.  Deletes
    and non-growing repairs remain possible when an owner is already over its
    limit.
    """

    # LLM: coordinator_io仅更严格IO策略，不缓存source；原quota公式/策略/扫描不改，C出生消费原scope。
    # 函数用途: 绑定原owner配额与锁锚点，按实例选择准确FD出生门。
    def __init__(self, owner_root: str | Path, *, max_bytes: int, policy_available: bool = True, coordinator_io: bool = False):
        self.owner_root = Path(owner_root).expanduser().resolve(strict=False)
        self.max_bytes = max(0, int(max_bytes))
        self.policy_available = bool(policy_available)
        self.coordinator_io = coordinator_io
        self._lock_anchor = self.owner_root / _QUOTA_LOCK_BASENAME

    # LLM: 默认阻塞保持既有调用合同；限时准备可选择立即拒绝竞争，但仍须在同一锁内检查和执行完整写入。
    # LLM: C只透传锁出生策略，原quota锁仍跨领域/同步embedding；max_bytes<=0不添scan或新限制。
    # 函数用途: 持原owner配额锁供领域同一准入共享，等待在Root外。
    @contextmanager
    def admission(self, *, blocking: bool = True) -> Iterator[OwnerQuotaAdmission]:
        """Hold the owner gate while a repository computes and applies a mutation."""

        if not self.policy_available:
            raise OwnerQuotaUnavailable("owner quota policy is unavailable")
        if self.max_bytes <= 0:
            yield OwnerQuotaAdmission(self, enabled=False)
            return
        options = {} if not self.coordinator_io else {"coordinator_io": True}
        with _locked_owner_quota(self._lock_anchor, blocking=blocking, **options):
            yield OwnerQuotaAdmission(self, enabled=True)

    @contextmanager
    def reserve(self, changes: Iterable[OwnerQuotaChange]) -> Iterator[OwnerQuotaProjection | None]:
        with self.admission() as admission:
            projection = admission.check(changes)
            yield projection


def owner_quota_enforcer_from_policy(
    owner_root: str | Path,
    *,
    quota_path: str | Path | None = None,
) -> OwnerQuotaEnforcer:
    """Build the canonical gate for non-Agent write entrances.

    Feishu confirmation callbacks do not own a ``SimpleAgent`` instance, but
    they still mutate the same owner Persona authority.  Reading that owner's
    structured quota file here keeps those callbacks on the same gate.  A
    malformed policy is fail-closed; a missing limit uses the seeded unlimited
    default, matching ``resolve_effective_owner_policy``.
    """

    root = Path(owner_root).expanduser().resolve(strict=False)
    path = Path(quota_path) if quota_path is not None else root / "quota.json"
    report = read_json_object_report(path, context="owner_policy.quota")
    raw = report.payload.get("max_disk_mb")
    try:
        max_disk_mb = int(raw)
    except (TypeError, ValueError):
        max_disk_mb = 0
    return OwnerQuotaEnforcer(
        root,
        max_bytes=max(0, max_disk_mb) * 1024 * 1024,
        policy_available=report.load_error is None,
    )


# LLM: 默认展示扫描只缓存display_archives各目录直接文件字节，5秒/256项有界；exact供权威准入禁用缓存。
#   trash与冷归档始终计费，硬链接逐路径计数；目录mtime不证明文件没原地扩写，不能用于授权。
# 函数用途: 统计owner普通文件逻辑用量，减少展示档案重复逐文件stat，不跟随符号链接。
def owner_logical_usage_bytes(root: str | Path, *, exact: bool = False) -> int:
    """Count regular-file bytes below an owner root without following symlinks.

    Hard links are intentionally counted per visible path.  A logical quota
    must not be bypassable by creating many links to the same inode.
    """

    owner_root = Path(root).expanduser().resolve(strict=False)
    try:
        root_info = owner_root.stat(follow_symlinks=False)
    except FileNotFoundError:
        return 0
    except OSError as exc:
        raise OwnerQuotaUnavailable("owner disk usage root is unavailable") from exc
    if not stat.S_ISDIR(root_info.st_mode):
        raise OwnerQuotaUnavailable("owner disk usage root is not a directory")
    total = 0

    # LLM: 同时删除目录不计费，其余错误继续关闭式拒绝统计，不返回不完整用量。
    # 函数用途: 处理用量遍历期间的目录消失或读取故障。
    def handle_walk_error(exc: OSError) -> None:
        # A concurrently removed runtime directory no longer consumes quota.
        # Other traversal failures (for example EACCES) remain fail-closed.
        if isinstance(exc, FileNotFoundError):
            return
        raise exc

    try:
        for directory, dirnames, filenames in os.walk(
            owner_root,
            followlinks=False,
            onerror=handle_walk_error,
        ):
            base = Path(directory)
            dirnames[:] = [name for name in dirnames if not (base / name).is_symlink()]
            cached = not exact and "display_archives" in base.relative_to(owner_root).parts
            total += _directory_usage_bytes(owner_root, base, filenames, cached=cached)
    except OSError as exc:
        raise OwnerQuotaUnavailable("owner disk usage scan failed") from exc
    return total


# LLM: 配额准入保持精确；Linux原native扫描保留，portable路径显式exact，不借展示TTL放宽写入权限。
# 函数用途: 为持锁配额准入返回准确用量，包括回收站和冷归档。
def _owner_usage_bytes_for_admission(root: str | Path) -> int:
    """Return exact regular-file usage without monopolising the Python runtime.

    Large owner workspaces can contain hundreds of thousands of files.  Running
    the authoritative walk in Python while the per-owner quota lock is held can
    starve an otherwise healthy Gateway under concurrent background work.  On
    Linux, delegate only the mechanical directory traversal to the system
    ``find`` implementation; the same regular-file, no-symlink-following and
    per-visible-hardlink semantics are preserved.  Unsupported platforms keep
    the portable Python implementation.  Runtime scan failures remain
    fail-closed instead of silently bypassing quota enforcement.
    """

    owner_root = Path(root).expanduser().resolve(strict=False)
    native = _native_regular_file_usage_bytes(owner_root)
    if native is not None:
        return native
    return owner_logical_usage_bytes(owner_root, exact=True)


# LLM: 只缓存单目录直接文件总数；目录身份/mtime/ctime或TTL失效即重算，权限错误不回旧值。
# 函数用途: 为展示档案目录复用短期统计；exact调用方不使用缓存。
def _directory_usage_bytes(owner_root: Path, base: Path, names: list[str], *, cached: bool) -> int:
    if not cached:
        return _directory_regular_bytes(owner_root, base, names)
    info = base.stat(follow_symlinks=False)
    key = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns)
    now = time.monotonic()
    with _DISPLAY_USAGE_LOCK:
        entry = _DISPLAY_USAGE_CACHE.get(base)
        if entry is not None and entry[0] == key and now - entry[1] < _DISPLAY_USAGE_TTL_SECONDS:
            _DISPLAY_USAGE_CACHE.move_to_end(base)
            return entry[2]
    total = _directory_regular_bytes(owner_root, base, names)
    after = base.stat(follow_symlinks=False)
    if key == (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns):
        with _DISPLAY_USAGE_LOCK:
            _DISPLAY_USAGE_CACHE[base] = (key, now, total)
            _DISPLAY_USAGE_CACHE.move_to_end(base)
            while len(_DISPLAY_USAGE_CACHE) > _DISPLAY_USAGE_CACHE_LIMIT:
                _DISPLAY_USAGE_CACHE.popitem(last=False)
    return total


# LLM: 硬链接逐路径计数，不跟随符号链接；只容忍并发消失，其他stat错误必须上抛。
# 函数用途: 统计一个目录的直接普通文件字节，不排除trash或冷归档。
def _directory_regular_bytes(owner_root: Path, base: Path, names: list[str]) -> int:
    total = 0
    for name in names:
        path = base / name
        if path == owner_root / f"{_QUOTA_LOCK_BASENAME}.lock":
            continue
        try:
            info = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode):
            total += max(0, int(info.st_size))
    return total


def _native_regular_file_usage_bytes(owner_root: Path) -> int | None:
    if not sys.platform.startswith("linux"):
        return None
    try:
        root_info = owner_root.stat(follow_symlinks=False)
    except FileNotFoundError:
        return 0
    except OSError as exc:
        raise OwnerQuotaUnavailable("owner disk usage root is unavailable") from exc
    if not stat.S_ISDIR(root_info.st_mode):
        raise OwnerQuotaUnavailable("owner disk usage root is not a directory")
    find_path = next(
        (
            candidate
            for candidate in ("/usr/bin/find", "/bin/find")
            if os.access(candidate, os.X_OK)
        ),
        "",
    )
    if not find_path:
        return None
    lock_path = owner_root / f"{_QUOTA_LOCK_BASENAME}.lock"
    try:
        completed = subprocess.run(
            [
                find_path,
                os.fspath(owner_root),
                "-type",
                "f",
                "!",
                "-path",
                os.fspath(lock_path),
                "-printf",
                "%s\n",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=_NATIVE_USAGE_SCAN_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired as exc:
        raise OwnerQuotaUnavailable("owner disk usage scan timed out") from exc
    if completed.returncode != 0:
        detail = str(completed.stderr or "").lower()
        if any(token in detail for token in ("unknown predicate", "unrecognized", "illegal option")):
            return None
        raise OwnerQuotaUnavailable("owner disk usage scan failed")
    try:
        return sum(int(line) for line in completed.stdout.splitlines() if line)
    except ValueError as exc:
        raise OwnerQuotaUnavailable("owner disk usage scan returned invalid data") from exc


def _normalized_owner_changes(
    owner_root: Path,
    changes: Iterable[OwnerQuotaChange],
) -> dict[Path, OwnerQuotaChange]:
    normalized: dict[Path, OwnerQuotaChange] = {}
    for change in changes:
        path = Path(change.path).expanduser().resolve(strict=False)
        try:
            path.relative_to(owner_root)
        except ValueError:
            continue
        size = change.final_size
        if size is not None and int(size) < 0:
            raise ValueError("quota final_size must be non-negative")
        if change.append and size is None:
            raise ValueError("quota append change requires a byte count")
        candidate = OwnerQuotaChange(
            path=path,
            final_size=None if size is None else int(size),
            append=bool(change.append),
        )
        previous = normalized.get(path)
        if previous is not None and previous.append and candidate.append:
            candidate = OwnerQuotaChange(
                path=path,
                final_size=int(previous.final_size or 0) + int(candidate.final_size or 0),
                append=True,
            )
        elif previous is not None and previous.append != candidate.append:
            raise ValueError("quota batch cannot mix append and replacement for one path")
        normalized[path] = candidate
    return normalized


def _logical_file_size(path: Path) -> int:
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return 0
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return 0
    return max(0, int(info.st_size))


# LLM: 沿用原 JSON 双层锁；锁竞争（BlockingIOError）与策略/存储故障分开转领域错误，
#   不能因为立即准入失败而无锁继续（MC4-2501）。
# LLM: 只有新birth原scope的实际native类同对象透传，普通OSError/RuntimeError仍原领域包装；不改usage scan catch。
# 函数用途: 保原配额锁竞争/坏账语义，C出生拒绝不可洗成普通配额故障。
@contextmanager
def _locked_owner_quota(path: Path, *, blocking: bool = True, coordinator_io: bool = False) -> Iterator[None]:
    options = {} if not coordinator_io else {"birth_commit": _coordinator_quota_birth}
    manager = locked_json_path(path, blocking=blocking, **options)
    try:
        manager.__enter__()
    except BlockingIOError as exc:
        raise OwnerQuotaLockContention("owner quota lock is busy") from exc
    except (OSError, RuntimeError, ValueError) as exc:
        if coordinator_io and isinstance(exc, (GlobalWriteCommitUnavailable, GlobalWriteInWorkerError,
                                                 LocalControlCoordinatorUnavailable, OwnerManagementIngressUnavailable)):
            raise
        raise OwnerQuotaUnavailable("owner quota lock is unavailable") from exc
    try:
        yield
    finally:
        manager.__exit__(None, None, None)


__all__ = [
    "OwnerQuotaAdmission",
    "OwnerQuotaChange",
    "OwnerQuotaEnforcer",
    "OwnerQuotaExceeded",
    "OwnerQuotaLockContention",
    "OwnerQuotaProjection",
    "OwnerQuotaUnavailable",
    "owner_quota_enforcer_from_policy",
    "owner_logical_usage_bytes",
]


# LLM: 仅原lock FD出生，原scope每次重新核来源；无source缓存、无新上下文。
# 函数用途: 为C配额锁打开/创建/收紧FD，锁等待不入Root。
def _coordinator_quota_birth(operation):
    return execute_global_write(operation, require_scope=True)
