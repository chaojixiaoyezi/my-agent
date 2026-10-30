# LLM: owner 配额继续使用原文件和锁序；显式非阻塞准入只用于有期限管理，默认工具写入保持等待语义。
# LLM: C可选准确FD出生门，scan仍原算法门外；只透传真实native对象，不按码名分类，默认False无新global guard。
# 模块用途: 复用唯一owner配额和锁序，C只收紧实际出生来源。
from __future__ import annotations

import json
import math
import os
import stat
import subprocess
import sys
import time
import uuid
from collections import OrderedDict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from ..common.global_write_commit import GlobalWriteCommitUnavailable, execute_global_write
from ..common.global_write_guard import GlobalWriteInWorkerError
from ..common.json_io import locked_json_path, read_json_object_report
from ..common.nofollow_fs import (
    open_directory_beneath,
    read_bytes_beneath,
    write_bytes_atomic_beneath,
)
from ..common.owner_write_errors import (
    LocalControlCoordinatorUnavailable,
    OwnerManagementIngressUnavailable,
)
from ..contracts.optimization_contracts import require_schema_generation

_QUOTA_LOCK_BASENAME = ".owner-quota"
# 原生用量扫描最多 15 秒：扫描挂起不应拖住配额判定，超时按不可用处理。
_NATIVE_USAGE_SCAN_TIMEOUT_SECONDS = 15.0
# 仅用量展示可短期复用碎片目录统计；权威写入准入绝不读取这个缓存。
_DISPLAY_USAGE_TTL_SECONDS = 5.0
# 仅用量展示缓存最多保留的目录统计条数，不参与写入配额准入。
_DISPLAY_USAGE_CACHE_MAX_ENTRIES = 256
_DISPLAY_USAGE_CACHE: OrderedDict[Path, tuple[tuple[int, ...], float, int]] = OrderedDict()
_DISPLAY_USAGE_LOCK = RLock()
_QUOTA_LEDGER = '.owner-quota-ledger.json'
_QUOTA_SCANS: OrderedDict = OrderedDict()
_QUOTA_SCANS_LOCK = RLock()
_QUOTA_SCAN_CACHE_SIZE = 16
_QUOTA_SCAN_CURSOR_TTL = 3600
_QUOTA_SORT_BATCH = 128


# LLM: 原配额写者在owner锁内解释append字节，不预先读取大小，保持批量写原子准入语义。
# 类用途: 描述一个owner文件的删除、替换或追加目标。
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


# LLM: 此值只表示同次锁内准入计算，不可当跨事务或后台扫描后的长期授权。
# 类用途: 返回当前用量、预期用量及owner上限。
@dataclass(frozen=True)
class OwnerQuotaProjection:
    used_bytes: int
    projected_bytes: int
    max_bytes: int


# LLM: 权威用量或策略读取不可靠时关闭式拒绝增长，不能降级成无限配额。
# 类用途: 区分无法安全判定配额与已经确定超额。
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


# LLM: 仅携同次锁内投影，不记录用户正文；调用方应保留非增长修复入口。
# 类用途: 告知当前写入将超过磁盘配额。
class OwnerQuotaExceeded(RuntimeError):
    # LLM: 保存准确投影供结构化处理，异常文本只包含字节统计，不改变账本。
    # 函数用途: 构造带用量详情的超额异常。
    def __init__(self, projection: OwnerQuotaProjection):
        self.projection = projection
        super().__init__(
            "owner 磁盘配额不足: "
            f"used_bytes={projection.used_bytes} "
            f"projected_bytes={projection.projected_bytes} "
            f"max_bytes={projection.max_bytes}"
        )


# LLM: 准入只做128项/10ms有界鲜活核对，不把展示缓存或后台旧账当成封闭写域证明。
# 类用途: 在原owner锁内预留增长，退出时按实际字节结算，包括失败后的部分写入。
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

    # LLM: 生命周期严格局限一次原owner锁，预留ID不跨线程共享。
    # 函数用途: 创建本次准入的前像和预留容器。
    def __init__(self, enforcer: OwnerQuotaEnforcer, *, enabled: bool) -> None:
        self._enforcer = enforcer
        self.enabled = bool(enabled)
        self._before = {}
        self._before_inodes = {}
        self._reservation = uuid.uuid4().hex

    # LLM: 完整小批核对可准入，未读完只允许非增长；绝不触发旧全树walk或find。
    # 函数用途: 计算本批增长并在写入前持久预留。
    def check(self, changes: Iterable[OwnerQuotaChange]) -> OwnerQuotaProjection | None:
        if not self.enabled:
            return None
        relevant = _normalized_owner_changes(self._enforcer.owner_root, changes)
        if not relevant:
            return None
        try:
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
        for path, change in relevant.items():
            try:
                info = path.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            desired = current_sizes[path] + (change.final_size or 0) if change.append else (change.final_size or 0)
            if info.st_nlink > 1 and desired > current_sizes[path]:
                raise OwnerQuotaUnavailable('hardlinked target growth requires inode replacement')
        root = self._enforcer.owner_root
        ledger = _read_quota_ledger(root)
        total, count, complete = _bounded_quota_snapshot(root, max_entries=128, max_ms=10)
        if not complete and final > replaced:
            ledger['dirty_since'] = ledger.get('dirty_since') or time.time()
            _write_quota_ledger(root, ledger)
            raise OwnerQuotaUnavailable('owner usage reconciliation pending; growth refused')
        used = total if complete else max(ledger['used_bytes'], self._enforcer.max_bytes)
        projection = OwnerQuotaProjection(
            used_bytes=used,
            projected_bytes=max(0, used - replaced + final),
            max_bytes=self._enforcer.max_bytes,
        )
        if projection.projected_bytes > projection.max_bytes and projection.projected_bytes > used:
            raise OwnerQuotaExceeded(projection)
        if complete:
            ledger.update(used_bytes=total, inode_count=count, dirty_since=None)
            # 所有正规写者都持同一owner锁；拿到锁时旧预留只可能是退出未确认的遗留。
            ledger['reservations'] = {}
            self._before = {path: _logical_file_size(path) for path in self._before}
            self._before_inodes = {path: int(path.is_file()) for path in self._before}
        for path, size in current_sizes.items():
            self._before.setdefault(path, size)
            self._before_inodes.setdefault(path, int(path.is_file()))
        delta = max(0, final - replaced)
        ledger['reservations'][self._reservation] = {'bytes': delta, 'expires_at': time.time() + 300,
            'paths': [str(path.relative_to(root)) for path in self._before]}
        ledger['reserved_bytes'] = sum(row['bytes'] for row in ledger['reservations'].values())
        ledger['revision'] += 1
        ledger['dirty_since'] = ledger.get('dirty_since') or time.time()
        _write_quota_ledger(root, ledger)
        return projection

    # LLM: 即使业务抛异常也核实际大小；读回失败保留预留和dirty，不能伪造abort成功。
    # 函数用途: 以真实文件差值结算本次预留，不把预计final_size当实际写入。
    def commit_delta(self):
        if not self.enabled or not self._before:
            return
        root = self._enforcer.owner_root
        ledger = _read_quota_ledger(root)
        delta = sum(_logical_file_size(path) - before for path, before in self._before.items())
        ledger['used_bytes'] = max(0, ledger['used_bytes'] + delta)
        ledger['inode_count'] = max(0, ledger['inode_count'] + sum(
            int(path.is_file()) - self._before_inodes[path] for path in self._before))
        ledger['reservations'].pop(self._reservation, None)
        ledger['reserved_bytes'] = sum(row['bytes'] for row in ledger['reservations'].values())
        ledger['revision'] += 1
        _write_quota_ledger(root, ledger)

        self._before.clear()
        self._before_inodes.clear()

    # LLM: abort仍须读回，异常不是零副作用证明。
    # 函数用途: 撤销未使用预留并结算可能已写部分。
    def abort_reservation(self):
        self.commit_delta()


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
            admission = OwnerQuotaAdmission(self, enabled=True)
            try:
                yield admission
            finally:
                admission.commit_delta()

    # LLM: 共用原admission锁和退出结算；业务异常也必须核实际落盘差额。
    # 函数用途: 为已知变更批次预留配额并在离开时结算。
    @contextmanager
    def reserve(self, changes: Iterable[OwnerQuotaChange]) -> Iterator[OwnerQuotaProjection | None]:
        with self.admission() as admission:
            projection = admission.check(changes)
            yield projection


# LLM: 无Agent的正式写入口仍读同一quota.json，坏策略关闭式拒绝；缺显式限额沿原默认无限。
# 函数用途: 为确认回调等非Agent入口创建规范配额门。
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
            while len(_DISPLAY_USAGE_CACHE) > _DISPLAY_USAGE_CACHE_MAX_ENTRIES:
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


# LLM: 仅Linux调用系统find并限制15秒；非支持平台返回None，真实扫描故障拒绝，不使用展示缓存。
# 函数用途: 为需要精确扫描的原调用方统计普通文件字节，不跟随符号链接。
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


# LLM: 只处理当前owner规范路径；控制账及锁不得作为免费payload目标，追加合并不改变原批次计费语义。
# 函数用途: 去重并校验同一批配额变更，拒绝控制文件覆盖与混合写入。
def _normalized_owner_changes(
    owner_root: Path,
    changes: Iterable[OwnerQuotaChange],
) -> dict[Path, OwnerQuotaChange]:
    normalized: dict[Path, OwnerQuotaChange] = {}
    for change in changes:
        path = Path(change.path).expanduser().resolve(strict=False)
        if path in (owner_root / _QUOTA_LEDGER, owner_root / (_QUOTA_LOCK_BASENAME + '.lock')):
            raise OwnerQuotaUnavailable('quota control metadata is not a payload target')
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


# LLM: 只stat叶元数据且不跟随链接；并发消失按零，其他IO错误不得伪装成未写入。
# 函数用途: 在准入与结算时读取单个普通文件的逻辑字节数。
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


# LLM: 控制账与原配额锁为固定大小系统元数据，不计用户payload；除此之外trash/归档/硬链接都计费。
# 函数用途: 读取唯一增量账；坏账/高代际不允许重置为零。
def _read_quota_ledger(root):
    raw = read_bytes_beneath(root, (_QUOTA_LEDGER,), max_bytes=1_048_576, require_dir_fd=True)
    if raw is None:
        return {'schema_generation': 1, 'used_bytes': 0, 'reserved_bytes': 0, 'inode_count': 0,
                'revision': 0, 'reservations': {}, 'dirty_since': time.time(), 'reconcile_cursor': {}}
    try:
        row = json.loads(raw)
        require_schema_generation(row)
        for key in ('used_bytes', 'reserved_bytes', 'inode_count', 'revision'):
            if type(row[key]) is not int or row[key] < 0:
                raise ValueError('invalid quota count')
        if not isinstance(row['reservations'], dict) or len(row['reservations']) > 128:
            raise ValueError('invalid quota reservations')
        for reservation in row['reservations'].values():
            if (type(reservation.get('bytes')) is not int or reservation['bytes'] < 0
                    or type(reservation.get('expires_at')) not in (int, float)
                    or not math.isfinite(reservation['expires_at'])):
                raise ValueError('invalid quota reservation')
        if sum(item['bytes'] for item in row['reservations'].values()) != row['reserved_bytes']:
            raise ValueError('quota reservation total mismatch')
        return row
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise OwnerQuotaUnavailable('quota ledger corrupt') from exc


# LLM: 原owner锁已持有，原子私有写与R1校验，不增加第二配额策略。
# 函数用途: 持久保存预留、计数与可重启扫描的观察游标。
def _write_quota_ledger(root, ledger):
    require_schema_generation(ledger)
    encoded = json.dumps(ledger, sort_keys=True, allow_nan=False).encode()
    if len(encoded) > 1_048_576:
        raise OwnerQuotaUnavailable('quota ledger too large')
    write_bytes_atomic_beneath(root, (_QUOTA_LEDGER,), encoded)


# LLM: FD逐层nofollow，不以路径别名扫描外部；每条目录项都消耗预算，深度超过64关闭式拒绝。
# 函数用途: 惰性枚举owner的普通文件字节和inode，不生成全目录列表。
def _quota_entries(root, parts=()):
    if len(parts) > 64:
        raise OwnerQuotaUnavailable('quota traversal depth exceeded')
    descriptor = open_directory_beneath(root, parts, require_dir_fd=True)
    try:
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if not parts and entry.name in (_QUOTA_LEDGER, _QUOTA_LOCK_BASENAME + '.lock'):
                    yield (0, 0)
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    yield (0, 0)
                    continue
                yield (info.st_size, 1) if stat.S_ISREG(info.st_mode) else (0, 0)
                if stat.S_ISDIR(info.st_mode):
                    try:
                        yield from _quota_entries(root, (*parts, entry.name))
                    except FileNotFoundError:
                        continue
    finally:
        os.close(descriptor)


# LLM: 一次全新有限核对只用于小owner准入；超预算关闭迭代器，不把未读完的总量当权威。
# 函数用途: 至多读max_entries个目录项并返回是否准确读到末尾。
def _bounded_quota_snapshot(root, *, max_entries, max_ms):
    iterator = _quota_entries(root)
    total, count = 0, 0
    deadline = time.monotonic() + max_ms / 1000
    try:
        for _ in range(max_entries):
            if time.monotonic() >= deadline:
                break
            try:
                size, files = next(iterator)
            except StopIteration:
                return total, count, True
            total += size
            count += files
        return total, count, False
    finally:
        iterator.close()


# LLM: 后台账仍是观察而非准入证明；最多16个LRU扫描，每个暂停扫描至多持一个目录流及绑定FD，重启沿路径合计恢复。
#   原owner写revision变化、root身份变化或一小时过期即重扫，异常不得发布complete或清孤儿预留。
# 函数用途: 按目录项/时间预算续扫owner，完成后回收孤儿预留并记录准确观察用量。
def reconcile_owner_quota_slice(owner_root, *, max_entries=128, max_ms=20, blocking=True):
    if (type(max_entries) is not int or not 0 < max_entries <= 100000
            or type(max_ms) not in (int, float) or not math.isfinite(max_ms) or max_ms <= 0):
        raise ValueError('invalid quota reconciliation budget')
    root = Path(owner_root).expanduser().resolve(strict=False)
    with _locked_quota_scan_cache(blocking=blocking), _locked_owner_quota(root / _QUOTA_LOCK_BASENAME, blocking=blocking):
        ledger = _read_quota_ledger(root)
        key = (os.getpid(), str(root))
        scan = _QUOTA_SCANS.get(key)
        recovered = _resumable_quota_cursor(root, ledger)
        if scan is not None and (scan['revision'] != ledger['revision'] or recovered is None
                                 or scan['epoch'] != recovered['epoch']):
            scan['iterator'].close()
            del _QUOTA_SCANS[key]
            scan = None
        replay_skip = 0
        if scan is None:
            cursor = recovered or {}
            prep = cursor.get('prep') if isinstance(cursor.get('prep'), dict) else None
            replay_skip = int(prep.get('block_skip', 0)) if prep is not None else 0
            state = {'prep': dict(prep) if prep is not None else None}
            scan = {'iterator': _sorted_quota_entries(root, after=cursor.get('last_relpath', ''), state=state),
                    'revision': ledger['revision'], 'last_relpath': cursor.get('last_relpath', ''),
                    'used': cursor.get('partial_used_bytes', 0), 'inodes': cursor.get('partial_inode_count', 0),
                    'scanned': cursor.get('scanned', 0), 'epoch': cursor.get('epoch', uuid.uuid4().hex),
                    'started_at': cursor.get('started_at', time.time()), 'state': state}
            _QUOTA_SCANS[key] = scan
        _QUOTA_SCANS.move_to_end(key)
        while len(_QUOTA_SCANS) > _QUOTA_SCAN_CACHE_SIZE:
            _, expired = _QUOTA_SCANS.popitem(last=False)
            expired['iterator'].close()
        complete, scanned, enumerated = False, 0, 0
        enumeration_budget = max(1024, max_entries * 4)
        # 重放也计入枚举；旧前缀吃满普通预算时，额外只给该持久前缀一次重放额度。
        replay_allowance = replay_skip if replay_skip >= enumeration_budget else 0
        enumeration_budget += replay_allowance
        deadline = time.monotonic() + max_ms / 1000
        try:
            while scanned < max_entries and enumerated < enumeration_budget and time.monotonic() < deadline:
                try:
                    entry = next(scan['iterator'])
                except StopIteration:
                    complete = True
                    break
                if entry is None:
                    enumerated += 1
                    continue
                relpath, size, count = entry
                scan['used'] += size
                scan['inodes'] += count
                scan['scanned'] += 1
                scan['last_relpath'] = relpath
                scanned += 1
        except BaseException:
            scan['iterator'].close()
            _QUOTA_SCANS.pop(key, None)
            raise
        identity = root.stat(follow_symlinks=False)
        cursor_payload = {
            'epoch': scan['epoch'], 'scanned': scan['scanned'], 'complete': complete,
            'last_relpath': scan['last_relpath'], 'partial_used_bytes': scan['used'],
            'partial_inode_count': scan['inodes'], 'started_at': scan['started_at'],
            'ledger_revision': ledger['revision'], 'root_dev': identity.st_dev, 'root_ino': identity.st_ino}
        prep_state = scan.get('state', {}).get('prep') if isinstance(scan.get('state'), dict) else None
        if not complete and isinstance(prep_state, dict):
            cursor_payload['prep'] = prep_state
        ledger['reconcile_cursor'] = cursor_payload
        if complete:
            ledger.update(used_bytes=scan['used'], inode_count=scan['inodes'], dirty_since=None,
                          reservations={}, reserved_bytes=0)
            scan['iterator'].close()
            _QUOTA_SCANS.pop(key, None)
        else:
            ledger['dirty_since'] = ledger.get('dirty_since') or time.time()
        _write_quota_ledger(root, ledger)
        return {'complete': complete, 'scanned_entries': scanned, 'enumerated_entries': enumerated,
                'replay_allowance': replay_allowance,
                'used_bytes': ledger['used_bytes'],
                'inode_count': ledger['inode_count'], 'cursor': ledger['reconcile_cursor']}


# LLM: 验证排序准备阶段持久状态的结构、深度、候选上限与单调顺序；损坏即拒绝使用。
# 函数用途: 校验未完成目录枚举批次的持久恢复字段。
def _valid_quota_prep_cursor(prep):
    if not isinstance(prep, dict):
        return False
    parts, after, enumerated = prep.get('parts'), prep.get('after'), prep.get('enumerated')
    names, dir_id = prep.get('names'), prep.get('dir_id')
    dir_offset = prep.get('dir_offset', 0)
    block_skip = prep.get('block_skip', 0)
    if (not isinstance(parts, list) or len(parts) > 64
            or any(not isinstance(p, str) or not p or '/' in p or p in ('.', '..') for p in parts)
            or not isinstance(after, str) or type(enumerated) is not int or enumerated <= 0
            or type(dir_offset) is not int or dir_offset < 0
            or type(block_skip) is not int or not 0 <= block_skip <= enumerated
            or not isinstance(names, list) or len(names) > _QUOTA_SORT_BATCH
            or any(not isinstance(n, str) or not n or n <= after for n in names)
            or names != sorted(set(names))
            or not isinstance(dir_id, list) or len(dir_id) != 2
            or any(type(v) is not int or v < 0 for v in dir_id)):
        return False
    return True


# LLM: 安全读取或设置目录FD的内核偏移，不支持目录seek时回退到0且不抛异常。
# 函数用途: 为大目录跨缓存淘汰恢复提供有界内核目录块定位。
def _safe_dir_lseek(fd, pos, whence):
    try:
        return max(0, int(os.lseek(fd, pos, whence)))
    except OSError:
        return 0


# LLM: 只恢复未过期、同root身份及同写revision的完整游标；旧格式缺部分统计须重扫，不猜累计值。
# 函数用途: 验证持久续点可用性，防止LRU淘汰或重启丢失可信扫描进度。
def _resumable_quota_cursor(root, ledger):
    cursor = ledger.get('reconcile_cursor', {})
    if not isinstance(cursor, dict) or cursor.get('complete') is not False:
        return None
    counts = ('scanned', 'partial_used_bytes', 'partial_inode_count', 'ledger_revision', 'root_dev', 'root_ino')
    if any(type(cursor.get(key)) is not int or cursor[key] < 0 for key in counts):
        return None
    last, started, epoch = cursor.get('last_relpath'), cursor.get('started_at'), cursor.get('epoch')
    if (not isinstance(last, str) or last.startswith('/') or any(p in ('.', '..') for p in last.split('/'))
            or not isinstance(epoch, str) or not epoch or len(epoch) > 128
            or type(started) not in (float, int) or not math.isfinite(started)
            or not 0 <= time.time() - started <= _QUOTA_SCAN_CURSOR_TTL
            or cursor['ledger_revision'] != ledger['revision']):
        return None
    if 'prep' in cursor and cursor['prep'] is not None and not _valid_quota_prep_cursor(cursor['prep']):
        return None
    info = root.stat(follow_symlinks=False)
    return cursor if (info.st_dev, info.st_ino) == (cursor['root_dev'], cursor['root_ino']) else None


# LLM: 每层只保留最小128个名字；每个真实枚举（含恢复片重读项）均交回预算门，完整枚举完才发表排序批次。
#   暂停记录当前getdirentries块起点偏移与候选集，恢复时校验目录身份并从块起点继续有界枚举。
# 函数用途: 可暂停且可跨缓存淘汰有界恢复地准备排序候选，避免大目录在切片重启后原地踏步或超预算。
def _quota_sorted_names(root, parts, after, state=None):
    import bisect

    descriptor = open_directory_beneath(root, parts, require_dir_fd=True)
    try:
        dir_stat = os.fstat(descriptor)
        dir_id = [dir_stat.st_dev, dir_stat.st_ino]
        prep = state.get('prep') if isinstance(state, dict) else None
        if (isinstance(prep, dict) and tuple(prep.get('parts', ())) == parts
                and prep.get('after') == after and prep.get('dir_id') == dir_id):
            names = list(prep.get('names', ()))
            raw_count = int(prep.get('enumerated', 0))
            resume_offset = int(prep.get('dir_offset', 0))
            replay_remaining = int(prep.get('block_skip', 0))
            if resume_offset > 0:
                _safe_dir_lseek(descriptor, resume_offset, os.SEEK_SET)
        else:
            names = []
            raw_count = 0
            replay_remaining = 0
        last_pos = _safe_dir_lseek(descriptor, 0, os.SEEK_CUR)
        block_pos = last_pos
        block_skip = 0
        with os.scandir(descriptor) as entries:
            for entry in entries:
                cur_pos = _safe_dir_lseek(descriptor, 0, os.SEEK_CUR)
                if cur_pos != last_pos:
                    block_pos = last_pos
                    last_pos = cur_pos
                    block_skip = 0
                block_skip += 1
                if replay_remaining:
                    replay_remaining -= 1
                    yield None
                    continue
                name = entry.name + ('/' if entry.is_dir(follow_symlinks=False) else '')
                if name > after and (len(names) < _QUOTA_SORT_BATCH or name < names[-1]):
                    idx = bisect.bisect_left(names, name)
                    if idx == len(names) or names[idx] != name:
                        names.insert(idx, name)
                        if len(names) > _QUOTA_SORT_BATCH:
                            names.pop()
                raw_count += 1
                if isinstance(state, dict):
                    state['prep'] = {
                        'parts': list(parts), 'after': after, 'enumerated': raw_count,
                        'dir_offset': block_pos, 'block_skip': block_skip, 'names': list(names), 'dir_id': dir_id,
                    }
                yield None
            if isinstance(state, dict):
                state['prep'] = None
            return names
    finally:
        os.close(descriptor)


# LLM: 每个叶stat以原root逐层nofollow重开，返回前关闭FD；并发消失不计，权限/链接错误继续上抛。
# 函数用途: 取得一个排序候选的真实普通文件元数据，不跟随符号链接。
def _quota_sorted_stat(root, parts, name):
    descriptor = open_directory_beneath(root, parts, require_dir_fd=True)
    try:
        return os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    finally:
        os.close(descriptor)


# LLM: 持久last_relpath为完整字典序键，恢复只进入可能包含后继的子树；深度64和每层128名字有界。
#   排序准备逐项yield供时间/枚举预算中断；重启跳过旧键不重复计费，时间预算仍不抢占单个系统调用。
# 函数用途: 为后台配额生成确定性续扫项，同时保持前台原有小目录扫描算法不变。
def _sorted_quota_entries(root, parts=(), *, after='', state=None):
    if len(parts) > 64:
        raise OwnerQuotaUnavailable('quota traversal depth exceeded')
    prefix = '/'.join(parts) + '/' if parts else ''
    remaining = after[len(prefix):] if after.startswith(prefix) else ''
    sub_name, sep, _ = remaining.partition('/')
    if sep == '/' and sub_name:
        try:
            info = _quota_sorted_stat(root, parts, sub_name)
        except FileNotFoundError:
            info = None
        if info is not None and stat.S_ISDIR(info.st_mode):
            try:
                yield from _sorted_quota_entries(root, (*parts, sub_name), after=after, state=state)
            except FileNotFoundError:
                pass
        last_name = sub_name + '/'
    else:
        last_name = remaining
    while True:
        names = yield from _quota_sorted_names(root, parts, last_name, state=state)
        if not names:
            return
        for name in names:
            relpath = '/'.join((*parts, name))
            if relpath <= after and not (name.endswith('/') and after.startswith(relpath)):
                continue
            try:
                info = _quota_sorted_stat(root, parts, name.rstrip('/'))
            except FileNotFoundError:
                if relpath > after:
                    yield relpath, 0, 0
                continue
            if relpath > after:
                control = not parts and name in (_QUOTA_LEDGER, _QUOTA_LOCK_BASENAME + '.lock')
                regular = stat.S_ISREG(info.st_mode) and not control
                yield relpath, max(0, info.st_size) if regular else 0, int(regular)
            if stat.S_ISDIR(info.st_mode):
                try:
                    yield from _sorted_quota_entries(root, (*parts, name.rstrip('/')), after=after, state=state)
                except FileNotFoundError:
                    continue
        if len(names) < _QUOTA_SORT_BATCH:
            return
        last_name = names[-1]


# LLM: 必须先于各owner锁，所有后台扫描共享同一短片；非阻塞获取失败不接触扫描器和持久账。
# 函数用途: 防不同owner线程同时推进或关闭同一个缓存生成器。
@contextmanager
def _locked_quota_scan_cache(*, blocking):
    if not _QUOTA_SCANS_LOCK.acquire(blocking=blocking):
        raise OwnerQuotaLockContention('quota scan cache is busy')
    try:
        yield
    finally:
        _QUOTA_SCANS_LOCK.release()
