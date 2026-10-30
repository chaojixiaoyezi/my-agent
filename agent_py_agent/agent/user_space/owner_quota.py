# LLM: owner 配额继续使用原文件和锁序；显式非阻塞准入只用于有期限管理，默认工具写入保持等待语义。
# LLM: C可选准确FD出生门，scan仍原算法门外；只透传真实native对象，不按码名分类，默认False无新global guard。
# 模块用途: 复用唯一owner配额和锁序，C只收紧实际出生来源。
from __future__ import annotations

import base64
import ctypes
import json
import math
import os
import stat
import subprocess
import sys
import time
import uuid
import zlib
from collections import OrderedDict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
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
_DISPLAY_USAGE_CACHE: OrderedDict[
    Path, tuple[tuple[int, int, int, int, tuple[str, ...]], float, int]
] = OrderedDict()
_DISPLAY_USAGE_LOCK = RLock()
_QUOTA_LEDGER = '.owner-quota-ledger.json'
_QUOTA_CONTROL_NAMES = (_QUOTA_LEDGER, _QUOTA_LOCK_BASENAME + '.lock')
# 宿主运行时子树：Gateway/adapter服务队列、日志、心跳与工具遥测由宿主在准入外按各自生命周期写，模型工具只读，不计owner载荷。
_QUOTA_RUNTIME_DIRS = frozenset({('workspace', 'runtime', 'services'), ('data', 'telemetry')})
# 宿主可丢弃读索引（local_storage.owner_read_index的cache库）及SQLite伴随文件，同样在准入外写入。
_QUOTA_RUNTIME_FILES = frozenset(('cache', 'owner_read_index.sqlite3' + suffix) for suffix in ('', '-wal', '-shm', '-journal'))
_QUOTA_SCANS: OrderedDict = OrderedDict()
_QUOTA_SCANS_LOCK = RLock()
# 进程内配额续扫缓存最多保留的活跃 owner 扫描器数量。
_QUOTA_SCAN_CACHE_SIZE = 16
# 持久配额续扫游标允许恢复的最长存活秒数；与默认每日维护周期一致，大owner对账跨小时仍可续扫。
_QUOTA_SCAN_CURSOR_TTL_SECONDS = 86400
# 配额字典序扫描每批最多保留的排序候选目录项数。
_QUOTA_SORT_BATCH_ENTRIES = 128
# 进程内单目录排序候选窗口上限；热缓存一遍枚举即可覆盖，更大目录才回退到多遍有界选择。
_QUOTA_MAX_CACHED_DIR_ENTRIES = 65536
# 进程内单目录排序候选窗口的名称总字节上限，与条数上限一起约束每个缓存扫描器的内存。
_QUOTA_MAX_CACHED_NAME_BYTES = 4194304
# 持久目录批次的名称总上限，避免续点账本无限增长。
_QUOTA_MAX_PERSISTED_DIR_ENTRIES = 4096
# 持久目录批次与排序准备的名称总字节上限。
_QUOTA_MAX_PERSISTED_NAME_BYTES = 196608
# 逐目录摘要表的估算持久字节上限；超过即不再逐目录核验，改由受控写代次与后台对账维持（账本总上限1MiB）。
_QUOTA_MAX_METADATA_BYTES = 655360
# 准入与结算只逐项核验不超过这么多目录项的目录；轮转窗口每次准入也只花这么多额度，更大目录交后台对账。
_QUOTA_VERIFY_BUDGET_ENTRIES = 256
# 核验一个目录时每层nofollow打开按这么多次lstat计入额度；打开比单项lstat贵一个数量级。
_QUOTA_VERIFY_OPEN_COST_ENTRIES = 16
# 进程内最多记住这么多owner在本进程写账本后的root目录戳；淘汰只失去结算时免扫root的优化。
_QUOTA_ROOT_STAMP_CACHE_SIZE = 256
_QUOTA_ROOT_STAMPS: OrderedDict = OrderedDict()
_QUOTA_ROOT_STAMPS_LOCK = RLock()
_QUOTA_HASH_MASK = (1 << 64) - 1
# 原生目录读取缓冲固定32KiB；未消费记录随prep持久化，不在重启时重新枚举。
_QUOTA_DIR_BUFFER_BYTES = 32768
# 仅传递同次前台生成器的观察容器，不携带权限或跨调用缓存。
_QUOTA_SNAPSHOT_STATE = ContextVar('quota_snapshot_state', default=None)


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
        self._before_children = {}
        self._reservation = uuid.uuid4().hex
        self._authoritative_baseline = False
        self._ledger = None

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
        # 干净基线已显示超过128项时有界快照必然读不完，直接走基线核验，不再白做一次全新核对。
        complete = False
        if not _quota_baseline_exceeds(ledger, 128):
            snapshot = {}
            token = _QUOTA_SNAPSHOT_STATE.set(snapshot)
            try:
                total, count, complete = _bounded_quota_snapshot(root, max_entries=128, max_ms=10)
            finally:
                _QUOTA_SNAPSHOT_STATE.reset(token)
        # 同一准入分阶段调用时先结算上阶段；其它遗留预留仍不得当成干净基线。
        if not complete and self._before:
            self.commit_delta()
            ledger = _read_quota_ledger(root)
        touched = {'/'.join(path.relative_to(root).parts[:depth])
                   for path in relevant for depth in range(len(path.relative_to(root).parts))}
        authoritative = not complete and _verified_quota_baseline(root, ledger, touched)
        if not complete and not authoritative and final > replaced:
            ledger['dirty_since'] = ledger.get('dirty_since') or time.time()
            _write_quota_ledger(root, ledger)
            raise OwnerQuotaUnavailable('owner usage reconciliation pending; growth refused')
        used = total if complete else ledger['used_bytes'] if authoritative else max(ledger['used_bytes'], self._enforcer.max_bytes)
        projection = OwnerQuotaProjection(
            used_bytes=used,
            projected_bytes=max(0, used - replaced + final),
            max_bytes=self._enforcer.max_bytes,
        )
        if projection.projected_bytes > projection.max_bytes and projection.projected_bytes > used:
            raise OwnerQuotaExceeded(projection)
        if complete:
            self._before_children.clear()
            ledger.update(used_bytes=total, inode_count=count, dirty_since=None)
            # 所有正规写者都持同一owner锁；拿到锁时旧预留只可能是退出未确认的遗留。
            ledger['reservations'] = {}
            self._before = {path: _logical_file_size(path) for path in self._before}
            self._before_inodes = {path: int(path.is_file()) for path in self._before}
            ledger['reconcile_cursor'] = _quota_baseline_cursor(root, ledger, snapshot)
        elif not authoritative:
            ledger['dirty_since'] = ledger.get('dirty_since') or time.time()
        self._authoritative_baseline = complete or authoritative
        for path in relevant:
            parts = path.relative_to(root).parts
            for depth in range(1, len(parts) + 1):
                child_parts = parts[:depth]
                key = ('/'.join(child_parts[:-1]), child_parts[-1])
                if key not in self._before_children:
                    try:
                        info = _quota_sorted_stat(root, child_parts[:-1], child_parts[-1])
                    except FileNotFoundError:
                        before = None
                    else:
                        before = _quota_child_row(child_parts[:-1], child_parts[-1], info)
                    self._before_children[key] = (before, depth < len(parts))
        for path, size in current_sizes.items():
            self._before.setdefault(path, size)
            self._before_inodes.setdefault(path, int(path.is_file()))
        delta = max(0, final - replaced)
        ledger['reservations'][self._reservation] = {'bytes': delta, 'expires_at': time.time() + 300,
            'paths': [str(path.relative_to(root)) for path in self._before]}
        ledger['reserved_bytes'] = sum(row['bytes'] for row in ledger['reservations'].values())
        ledger['revision'] += 1
        # 完整基线与进行中的对账都随受控写代次前进；只有原本同步的游标才跟进，脏账不会因此变可信。
        cursor = ledger.get('reconcile_cursor')
        if isinstance(cursor, dict) and cursor.get('ledger_revision') == ledger['revision'] - 1:
            cursor['ledger_revision'] = ledger['revision']
        self._ledger = (ledger, _write_quota_ledger(root, ledger))
        return projection

    # LLM: 即使业务抛异常也核实际大小；读回失败保留预留和dirty，不能伪造abort成功。
    # 函数用途: 以真实文件差值结算本次预留，不把预计final_size当实际写入。
    def commit_delta(self):
        if not self.enabled or not self._before:
            return
        root = self._enforcer.owner_root
        # 整个准入持同一owner锁；账本文件身份未变即是本准入刚写的那份，免去一次大账本读取与解析。
        cached, self._ledger = self._ledger, None
        if cached is not None and cached[1] is not None and _quota_ledger_identity(root) == cached[1]:
            ledger = cached[0]
        else:
            ledger = _read_quota_ledger(root)
        after = {path: _logical_file_size(path) for path in self._before}
        after_inodes = {path: int(path.is_file()) for path in self._before}
        delta = sum(after[path] - before for path, before in self._before.items())
        ledger['used_bytes'] = max(0, ledger['used_bytes'] + delta)
        ledger['inode_count'] = max(0, ledger['inode_count'] + sum(
            after_inodes[path] - self._before_inodes[path] for path in self._before))
        ledger['reservations'].pop(self._reservation, None)
        ledger['reserved_bytes'] = sum(row['bytes'] for row in ledger['reservations'].values())
        root_vouched = _quota_root_vouched(root, ledger['revision'])
        ledger['revision'] += 1
        cursor = ledger.get('reconcile_cursor')
        synced = isinstance(cursor, dict) and cursor.get('ledger_revision') == ledger['revision'] - 1
        if synced and cursor.get('complete') is True:
            if ledger.get('dirty_since') is None and not ledger['reservations']:
                if _refresh_quota_baseline(root, cursor, self._before_children, root_vouched=root_vouched):
                    cursor['ledger_revision'] = ledger['revision']
                else:
                    ledger['dirty_since'] = time.time()
        elif synced and cursor.get('complete') is False:
            # 进行中的对账不因受控写重来：已扫过的路径补差额，变过目录项的窗口作废重枚举。
            _apply_quota_pass_changes(root, cursor, {
                path: (self._before[path], after[path], self._before_inodes[path], after_inodes[path])
                for path in self._before}, self._before_children)
            cursor['ledger_revision'] = ledger['revision']
        _write_quota_ledger(root, ledger)

        self._before.clear()
        self._before_inodes.clear()
        self._before_children.clear()

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
            parts = base.relative_to(owner_root).parts
            dirnames[:] = [name for name in dirnames
                           if not (base / name).is_symlink() and (*parts, name) not in _QUOTA_RUNTIME_DIRS]
            cached = not exact and "display_archives" in parts
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


# LLM: 只缓存单目录直接文件总数；目录身份/时间、已遍历文件名或TTL失效即重算，权限错误不回旧值。
# 函数用途: 为展示档案目录复用短期统计；exact调用方不使用缓存。
def _directory_usage_bytes(owner_root: Path, base: Path, names: list[str], *, cached: bool) -> int:
    if not cached:
        return _directory_regular_bytes(owner_root, base, names)
    info = base.stat(follow_symlinks=False)
    names_key = tuple(sorted(names))
    key = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, names_key)
    now = time.monotonic()
    with _DISPLAY_USAGE_LOCK:
        entry = _DISPLAY_USAGE_CACHE.get(base)
        if entry is not None and entry[0] == key and now - entry[1] < _DISPLAY_USAGE_TTL_SECONDS:
            _DISPLAY_USAGE_CACHE.move_to_end(base)
            return entry[2]
    total = _directory_regular_bytes(owner_root, base, names)
    after = base.stat(follow_symlinks=False)
    if key == (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns, names_key):
        with _DISPLAY_USAGE_LOCK:
            _DISPLAY_USAGE_CACHE[base] = (key, now, total)
            _DISPLAY_USAGE_CACHE.move_to_end(base)
            while len(_DISPLAY_USAGE_CACHE) > _DISPLAY_USAGE_CACHE_MAX_ENTRIES:
                _DISPLAY_USAGE_CACHE.popitem(last=False)
    return total


# LLM: 硬链接逐路径计数，不跟随符号链接；只容忍并发消失，其他stat错误必须上抛。
#   与权威扫描同一排除口径：配额控制账、宿主运行时文件和空锁侧车不计，trash与冷归档照常计费。
# 函数用途: 统计一个目录的直接普通文件字节。
def _directory_regular_bytes(owner_root: Path, base: Path, names: list[str]) -> int:
    total = 0
    parts = base.relative_to(owner_root).parts
    for name in names:
        if not parts and name in _QUOTA_CONTROL_NAMES:
            continue
        try:
            info = (base / name).stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode) and not _quota_excluded(parts, name, info):
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
    # 空锁侧车本就是0字节；其余排除项与权威扫描相同，运行时子树按路径前缀排除。
    excluded = [os.fspath(owner_root / name) for name in _QUOTA_CONTROL_NAMES]
    excluded += [os.fspath(owner_root.joinpath(*parts)) for parts in sorted(_QUOTA_RUNTIME_FILES)]
    excluded += [os.fspath(owner_root.joinpath(*parts)) + "/*" for parts in sorted(_QUOTA_RUNTIME_DIRS)]
    command = [find_path, os.fspath(owner_root), "-type", "f"]
    for path in excluded:
        command += ["!", "-path", path]
    try:
        completed = subprocess.run(
            [*command, "-printf", "%s\n"],
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
            relative = path.relative_to(owner_root).parts
        except ValueError:
            continue
        # 宿主运行时路径不计入owner配额，与扫描同口径；声明了也不计费，否则下一次对账会把它抹掉。
        if relative in _QUOTA_RUNTIME_FILES or any(relative[:len(prefix)] == prefix for prefix in _QUOTA_RUNTIME_DIRS):
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
#   逐目录表只是核验辅助而非计数权威；极端长名把账本撑过1MiB时放弃逐目录核验，不让对账或准入永久写不进去。
#   写后记下本进程看到的root目录戳：账本原子替换必然改root时间，结算据此判断root目录项是否只被账本写动过。
# 函数用途: 持久保存预留、计数与可重启扫描的观察游标。
def _write_quota_ledger(root, ledger):
    require_schema_generation(ledger)
    encoded = json.dumps(ledger, sort_keys=True, allow_nan=False).encode()
    cursor = ledger.get('reconcile_cursor')
    if len(encoded) > 1_048_576 and isinstance(cursor, dict) and 'dir_stamps' in cursor:
        cursor.pop('dir_stamps')
        cursor['metadata_complete'] = False
        encoded = json.dumps(ledger, sort_keys=True, allow_nan=False).encode()
    if len(encoded) > 1_048_576:
        raise OwnerQuotaUnavailable('quota ledger too large')
    write_bytes_atomic_beneath(root, (_QUOTA_LEDGER,), encoded)
    try:
        info = Path(root).stat(follow_symlinks=False)
    except OSError:
        info = None
    with _QUOTA_ROOT_STAMPS_LOCK:
        if info is None:
            _QUOTA_ROOT_STAMPS.pop(str(root), None)
        else:
            _QUOTA_ROOT_STAMPS[str(root)] = (
                ledger['revision'], [info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns])
            _QUOTA_ROOT_STAMPS.move_to_end(str(root))
            while len(_QUOTA_ROOT_STAMPS) > _QUOTA_ROOT_STAMP_CACHE_SIZE:
                _QUOTA_ROOT_STAMPS.popitem(last=False)
    return _quota_ledger_identity(root)


# LLM: 原子替换每次都换inode；身份含inode、大小与纳秒时间，任何别处写入都会使其不同。
# 函数用途: 返回账本文件当前身份，失败返回None。
def _quota_ledger_identity(root):
    try:
        info = (Path(root) / _QUOTA_LEDGER).stat(follow_symlinks=False)
    except OSError:
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


# LLM: 只有干净、同步的游标才能说明owner已知很大；脏账、未同步或缺表时返回False，前台照常做全新有界核对。
# 函数用途: 判断最近可信基线中的计费目录项是否超过给定数量，超过即停止累加。
def _quota_baseline_exceeds(ledger, entries):
    cursor = ledger.get('reconcile_cursor')
    if (ledger.get('dirty_since') is not None or not isinstance(cursor, dict)
            or cursor.get('ledger_revision') != ledger['revision']):
        return False
    directories = cursor.get('dir_stamps')
    if cursor.get('metadata_complete') is not True or not isinstance(directories, dict):
        return cursor.get('complete') in (True, False) and ledger['inode_count'] > entries
    total = 0
    for row in directories.values():
        if _valid_quota_row(row):
            total += row[4]
            if total > entries:
                return True
    return False


# LLM: 只证明“本进程上次写账本之后root目录项没有再变”；别的进程写过、缓存淘汰或任何外部增删都返回False。
# 函数用途: 判断root当前目录戳是否仍是本进程在该账本修订写入后观察到的值。
def _quota_root_vouched(root, revision):
    try:
        info = Path(root).stat(follow_symlinks=False)
    except OSError:
        return False
    with _QUOTA_ROOT_STAMPS_LOCK:
        return _QUOTA_ROOT_STAMPS.get(str(root)) == (
            revision, [info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns])


# LLM: 摘要使用稳定整数混合，不依赖进程随机hash；目录时间在自身基线核验。
# 函数用途: 将每种子项的名称、类型和身份合成可加减的64位摘要。
def _quota_child_digest(name: str, info: os.stat_result) -> tuple[int, int]:
    name_hash = zlib.crc32(os.fsencode(name)) * 0x9E3779B97F4A7C15
    reg_bytes = 0
    if stat.S_ISREG(info.st_mode):
        reg_bytes = max(0, int(info.st_size))
        value = (name_hash ^ (stat.S_IFREG * 0xBF58476D1CE4E5B9)
                 ^ (int(info.st_dev) * 0x94D049BB133111EB)
                 ^ (int(info.st_ino) * 0xD6E8FEB86659FD93)
                 ^ (reg_bytes * 0xA0761D6478BD642F)
                 ^ (int(info.st_mtime_ns) * 0xE7037ED1A0B428DB) ^ int(info.st_ctime_ns))
    elif stat.S_ISDIR(info.st_mode):
        value = (name_hash ^ (stat.S_IFDIR * 0xBF58476D1CE4E5B9)
                 ^ (int(info.st_dev) * 0x94D049BB133111EB) ^ int(info.st_ino))
    else:
        value = (name_hash ^ (stat.S_IFMT(info.st_mode) * 0xBF58476D1CE4E5B9)
                 ^ (int(info.st_dev) * 0x94D049BB133111EB)
                 ^ (int(info.st_ino) * 0xD6E8FEB86659FD93)
                 ^ (int(info.st_mtime_ns) * 0xE7037ED1A0B428DB) ^ int(info.st_ctime_ns))
    value &= _QUOTA_HASH_MASK
    return reg_bytes, value if value else 1


# LLM: 配额控制账、宿主运行时路径与空锁侧车对计数和摘要都不可见；扫描、核验、结算和展示用量同一口径。
#   运行时目录只在确是目录、运行时文件只在确是普通文件时排除；*.lock只在0字节时排除，有内容即照常计费。
# 函数用途: 判断一个目录项是否属于配额结构性排除。
def _quota_excluded(parts, name, info):
    if not parts and name in _QUOTA_CONTROL_NAMES:
        return True
    path = (*parts, name)
    if path in _QUOTA_RUNTIME_DIRS:
        return stat.S_ISDIR(info.st_mode)
    if path in _QUOTA_RUNTIME_FILES:
        return stat.S_ISREG(info.st_mode)
    return name.endswith('.lock') and stat.S_ISREG(info.st_mode) and info.st_size == 0


# LLM: 排除项贡献为零；其余子项计1项、普通文件字节与64位摘要。
# 函数用途: 返回一个子项对所在目录摘要的加法贡献。
def _quota_child_row(parts, name, info):
    if _quota_excluded(parts, name, info):
        return (0, 0, 0)
    reg_bytes, digest = _quota_child_digest(name, info)
    return (1, reg_bytes, digest)


# LLM: 键按JSON转义计长，七个整数加分隔符按典型104字节估；只用于给目录表封顶，极端大数撑过1MiB时由写账本兜底放弃整表。
# 函数用途: 估算一行目录摘要的持久字节。
def _quota_row_bytes(rel):
    return len(json.dumps(rel)) + 104


# LLM: 每个计费子项仅积累一次；根目录时间允许控制账替换，目录换身份仍使基线失效。
#   目录表按估算持久字节封顶：超过即整表作废，后续靠受控写代次与对账，不让账本或内存随目录数无界增长。
# 函数用途: 收集每目录七整数摘要，不保留逐文件名称和指纹。
def _capture_quota_metadata(state, parts, info, name=None):
    if state is None or state.get('metadata_complete', True) is False:
        return
    state['metadata_complete'] = True
    directories = state.setdefault('dir_stamps', {})
    rel = '/'.join(parts)
    if name is None:
        stamp = [int(info.st_dev), int(info.st_ino), int(info.st_mtime_ns), int(info.st_ctime_ns)]
        if rel in directories:
            if directories[rel][:2] != stamp[:2] or rel and directories[rel][:4] != stamp:
                state['metadata_complete'] = False
                return
            directories[rel][:4] = stamp
        else:
            size = state.get('metadata_bytes')
            if size is None:
                size = sum(_quota_row_bytes(key) for key in directories)
            size += _quota_row_bytes(rel)
            if size > _QUOTA_MAX_METADATA_BYTES:
                state['metadata_complete'] = False
                state.pop('dir_stamps', None)
                state.pop('metadata_bytes', None)
                return
            state['metadata_bytes'] = size
            directories[rel] = [*stamp, 0, 0, 0]
        return
    if info is None:
        return
    count, reg_bytes, digest = _quota_child_row(parts, name, info)
    if not count:
        return
    row = directories.get(rel)
    if row is None:
        state['metadata_complete'] = False
        return
    row[4] += 1
    row[5] += reg_bytes
    row[6] = (row[6] + digest) & _QUOTA_HASH_MASK


# LLM: 持久化只复制完整的有界观察；缺字段和旧游标都不能成为授权证明。
# 函数用途: 提取可重启的基线元数据。
def _quota_metadata_payload(state):
    if state.get('metadata_complete') is not True:
        return {'metadata_complete': False}
    return {'metadata_complete': True, 'dir_stamps': deepcopy(state.get('dir_stamps', {}))}


# LLM: 小owner完整扫描也发布同样的基线，便于受控写入跨过128项门槛。
# 函数用途: 为前台全量小快照生成权威游标。
def _quota_baseline_cursor(root, ledger, state):
    identity = root.stat(follow_symlinks=False)
    return {'complete': True, 'ledger_revision': ledger['revision'],
            'root_dev': identity.st_dev, 'root_ino': identity.st_ino,
            **_quota_metadata_payload(state)}


# LLM: 所有持久路径必须是owner内的真实相对组件；不能让坏游标越界或跟随链接。
# 函数用途: 校验元数据键和目录候选名称。
def _valid_quota_parts(parts):
    return (isinstance(parts, (tuple, list)) and len(parts) <= 64
            and all(isinstance(p, str) and p and '/' not in p and '\x00' not in p and p not in ('.', '..') for p in parts))


# LLM: 坏行一律视为核验失败而不是跳过。
# 函数用途: 校验一行持久目录摘要是七个非负整数。
def _valid_quota_row(row):
    return isinstance(row, list) and len(row) == 7 and all(type(value) is int and value >= 0 for value in row)


# LLM: 单目录nofollow打开并逐项lstat。身份必须一致；目录时间可以不同（锁侧车、原子替换都会改），但排除项之外的
#   项数、普通文件字节与摘要必须与期望完全相等，出现的子目录必须已有摘要行。通过后采纳当前目录戳供结算比较。
# 函数用途: 核验一个目录的直接子项与期望摘要一致。
def _verify_quota_dir(root, rel, row, dirs, expected=None):
    parts = rel.split('/') if rel else []
    if not _valid_quota_parts(parts) or not _valid_quota_row(row):
        return False
    target = list(row[4:7] if expected is None else expected)
    descriptor = open_directory_beneath(root, tuple(parts), require_dir_fd=True)
    try:
        info = os.fstat(descriptor)
        if [info.st_dev, info.st_ino] != row[:2]:
            return False
        count, reg_total, acc = 0, 0, 0
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if not parts and entry.name in _QUOTA_CONTROL_NAMES:
                    continue
                child = entry.stat(follow_symlinks=False)
                added, reg_bytes, digest = _quota_child_row(parts, entry.name, child)
                if not added:
                    continue
                count += 1
                if count > target[0]:
                    return False
                if stat.S_ISDIR(child.st_mode) and (f'{rel}/{entry.name}' if rel else entry.name) not in dirs:
                    return False
                reg_total += reg_bytes
                acc = (acc + digest) & _QUOTA_HASH_MASK
        if [count, reg_total, acc] != target:
            return False
        row[:4] = [info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns]
        return True
    finally:
        os.close(descriptor)


# 函数用途: 按“逐层打开目录 + 每个子项一次lstat”估算核验一个目录花掉的额度。
def _quota_dir_cost(rel, row):
    return row[4] + _QUOTA_VERIFY_OPEN_COST_ENTRIES * (rel.count('/') + 2 if rel else 1)


# LLM: budget=None逐目录全量核验，只供测试与诊断。准入传budget：先逐项核本次写入路径上不超过单目录额度的目录，
#   余下额度按持久位置轮转核其它小目录；超过单目录额度的大目录只交后台对账，前台工作不随整树规模增长。
# 函数用途: 有界核验持久逐目录摘要并推进轮转位置。
def _verify_quota_metadata(root, cursor, *, touched=(), budget=None):
    try:
        dirs = cursor.get('dir_stamps')
        if cursor.get('metadata_complete') is not True or not isinstance(dirs, dict) or '' not in dirs:
            return False
        if budget is None:
            return all(isinstance(rel, str) and _verify_quota_dir(root, rel, row, dirs)
                       for rel, row in list(dirs.items()))
        spent, checked = 0, set()
        for rel in sorted(touched):
            row = dirs.get(rel)
            if row is None:
                continue
            if not _valid_quota_row(row):
                return False
            checked.add(rel)
            if row[4] <= _QUOTA_VERIFY_BUDGET_ENTRIES:
                if not _verify_quota_dir(root, rel, row, dirs):
                    return False
                spent += _quota_dir_cost(rel, row)
        names = list(dirs)
        position = cursor.get('verify_pos')
        position = position % len(names) if type(position) is int and position >= 0 else 0
        visited = 0
        while visited < len(names) and spent < budget:
            rel = names[(position + visited) % len(names)]
            row = dirs[rel]
            visited += 1
            if rel in checked:
                continue
            if not isinstance(rel, str) or not _valid_quota_row(row):
                return False
            if row[4] > _QUOTA_VERIFY_BUDGET_ENTRIES:
                spent += 1
                continue
            if not _verify_quota_dir(root, rel, row, dirs):
                return False
            spent += _quota_dir_cost(rel, row)
        cursor['verify_pos'] = (position + visited) % len(names)
        return True
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        return False


# LLM: 孤儿预留、dirty、修订或root不符均拒绝。完整逐目录表做有界核验；后台对账进行中或目录表超容时以受控写代次为
#   新鲜性证明：干净账本的used_bytes来自一次完整测量且此后只经声明增量变化，准入外写入由下一轮对账测出并纠正。
# 函数用途: 判断旧账是否仍可安全地用于当前增长准入。
def _verified_quota_baseline(root, ledger, touched=()):
    cursor = ledger.get('reconcile_cursor')
    if (ledger.get('dirty_since') is not None or ledger.get('reservations')
            or not isinstance(cursor, dict) or cursor.get('complete') not in (True, False)
            or cursor.get('ledger_revision') != ledger['revision']):
        return False
    try:
        info = root.stat(follow_symlinks=False)
        if (info.st_dev, info.st_ino) != (cursor.get('root_dev'), cursor.get('root_ino')):
            return False
    except OSError:
        return False
    if cursor.get('complete') is True and cursor.get('metadata_complete') is True:
        return _verify_quota_metadata(root, cursor, touched=touched, budget=_QUOTA_VERIFY_BUDGET_ENTRIES)
    return True


# LLM: 按声明路径逐层重开父目录，取当前目录戳和子项后像，把声明子项的前后贡献相减相加；last给定（对账进行中）时
#   只调整对账已经走过的子项（字典序<=last）。声明路径上的新目录在需要时补一行。返回每个受影响目录的新行、
#   目录戳是否与旧行一致、目录项是否有增删；目录换身份或计数为负即抛错，由调用方关闭式处理。
# 函数用途: 计算一次声明变更对逐目录摘要的增量。
def _quota_declared_rows(root, dirs, before_children, *, last=None):
    grouped = {}
    for (parent_rel, name), (before, is_dir) in before_children.items():
        grouped.setdefault(parent_rel, []).append((name, before, is_dir))
    changed = {}
    for rel, children in grouped.items():
        parts = tuple(rel.split('/')) if rel else ()
        row = list(dirs[rel]) if rel in dirs else None
        if row is None and last is not None and rel + '/' > last:
            continue
        descriptor = open_directory_beneath(root, parts, require_dir_fd=True)
        try:
            info = os.fstat(descriptor)
            stamp = [info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns]
            if row is None:
                row, vouched = [*stamp, 0, 0, 0], False
            elif not _valid_quota_row(row) or row[:2] != stamp[:2]:
                raise ValueError('quota directory identity changed')
            else:
                vouched = row[:4] == stamp
            dentries = False
            for name, before, is_dir in children:
                try:
                    after = _quota_child_row(parts, name, os.stat(name, dir_fd=descriptor, follow_symlinks=False))
                except FileNotFoundError:
                    after = None
                dentries = dentries or (before is None) != (after is None)
                if last is not None and '/'.join((*parts, name)) + ('/' if is_dir else '') > last:
                    continue
                for sign, contribution in ((-1, before), (1, after)):
                    if contribution is not None:
                        row[4] += sign * contribution[0]
                        row[5] += sign * contribution[1]
                        row[6] = (row[6] + sign * contribution[2]) & _QUOTA_HASH_MASK
                if row[4] < 0 or row[5] < 0:
                    raise ValueError('quota directory summary underflow')
            row[:4] = stamp
            changed[rel] = (row, vouched, dentries)
        finally:
            os.close(descriptor)
    return changed


# LLM: 新行超出目录表字节预算时整表作废，回到受控写代次；不截断成半张表冒充完整。
# 函数用途: 合并声明变更产生的目录行并维持目录表容量上限。
def _merge_quota_rows(cursor, dirs, changed):
    added = [rel for rel in changed if rel not in dirs]
    dirs.update({rel: row for rel, (row, _vouched, _dentries) in changed.items()})
    if added and sum(_quota_row_bytes(rel) for rel in dirs) > _QUOTA_MAX_METADATA_BYTES:
        cursor.pop('dir_stamps', None)
        cursor['metadata_complete'] = False


# LLM: 准入已逐项核过路径上的小目录；结算只把声明子项的前后贡献相减相加。目录项在事务中有增删（新建、原子替换、
#   侧车或未声明写）且目录不大时，按期望摘要重扫一次，事务内未声明的增删当场发现；root以本进程账本写后戳判断。
#   大目录只采纳期望值，交后台对账。无逐目录表（超容）时只结算字节，由受控写代次维持可信。
# 函数用途: 以事务前像和真实落盘后像增量维护可信目录摘要。
def _refresh_quota_baseline(root, cursor, before_children, *, root_vouched):
    if cursor.get('metadata_complete') is not True:
        return True
    dirs = cursor.get('dir_stamps')
    if not isinstance(dirs, dict):
        return False
    try:
        changed = _quota_declared_rows(root, dirs, before_children)
        merged = {**dirs, **{rel: row for rel, (row, _vouched, _dentries) in changed.items()}}
        for rel, (row, vouched, _dentries) in changed.items():
            if (root_vouched if rel == '' else vouched) or row[4] > _QUOTA_VERIFY_BUDGET_ENTRIES:
                continue
            if not _verify_quota_dir(root, rel, list(row), merged, expected=row[4:7]):
                return False
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        return False
    _merge_quota_rows(cursor, dirs, changed)
    return True


# LLM: 对账进行中时受控写不让本轮重来（大owner否则永远扫不完）：字典序已走过的路径直接补字节与inode差额；
#   增删过目录项的目录作废其名称窗口与排序准备，下一片从当前位置重新枚举；逐目录行同样补差，算不出则放弃逐目录表。
# 函数用途: 把一次已结算的声明变更并入进行中的对账游标。
def _apply_quota_pass_changes(root, cursor, paths, before_children):
    last = cursor.get('last_relpath')
    if not isinstance(last, str):
        return
    for path, (before, after, before_inode, after_inode) in paths.items():
        if '/'.join(path.relative_to(root).parts) <= last:
            cursor['partial_used_bytes'] = max(0, int(cursor.get('partial_used_bytes', 0)) + after - before)
            cursor['partial_inode_count'] = max(0, int(cursor.get('partial_inode_count', 0)) + after_inode - before_inode)
    dirs = cursor.get('dir_stamps') if cursor.get('metadata_complete') is True else None
    try:
        changed = _quota_declared_rows(root, dirs if isinstance(dirs, dict) else {}, before_children, last=last)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        cursor.update(metadata_complete=False, dir_batch={}, prep=None)
        cursor.pop('dir_stamps', None)
        return
    batches = cursor.get('dir_batch') if isinstance(cursor.get('dir_batch'), dict) else {}
    prep = cursor.get('prep')
    for rel, (_row, _vouched, dentries) in changed.items():
        if dentries:
            batches.pop(rel, None)
            if isinstance(prep, dict) and '/'.join(prep.get('parts') or []) == rel:
                cursor['prep'] = None
    if isinstance(dirs, dict):
        _merge_quota_rows(cursor, dirs, changed)


# LLM: FD逐层nofollow，不以路径别名扫描外部；每条目录项都消耗预算，深度超过64关闭式拒绝；排除项不计也不进入。
# 函数用途: 惰性枚举owner的普通文件字节和inode，不生成全目录列表。
def _quota_entries(root, parts=()):
    if len(parts) > 64:
        raise OwnerQuotaUnavailable('quota traversal depth exceeded')
    descriptor = open_directory_beneath(root, parts, require_dir_fd=True)
    try:
        state = _QUOTA_SNAPSHOT_STATE.get()
        _capture_quota_metadata(state, parts, os.fstat(descriptor))
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if not parts and entry.name in _QUOTA_CONTROL_NAMES:
                    yield (0, 0)
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    if state is not None:
                        state['metadata_complete'] = False
                    yield (0, 0)
                    continue
                if _quota_excluded(parts, entry.name, info):
                    yield (0, 0)
                    continue
                _capture_quota_metadata(state, parts, info, entry.name)
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
    iterator = None
    total, count = 0, 0
    deadline = time.monotonic() + max_ms / 1000
    try:
        iterator = _quota_entries(root)
        for _ in range(max_entries):
            if time.monotonic() >= deadline:
                break
            try:
                size, files = next(iterator)
            except StopIteration:
                return total, count, True
            total += size
            count += files
        if time.monotonic() < deadline:
            try:
                next(iterator)
            except StopIteration:
                return total, count, True
        return total, count, False
    finally:
        if iterator is not None:
            iterator.close()


# LLM: 后台账仍是观察而非准入证明；最多16个LRU扫描，每个暂停扫描至多持一个目录流及绑定FD，重启沿路径合计恢复。
#   受控写由结算并入持久游标，进程内扫描器见到新revision即改从持久游标续扫；root身份变化、游标过期或本轮开始后
#   出现未结算的新预留（准入后崩溃的孤儿）即从头重扫，异常不得发布complete或清孤儿预留。
# max_ms 约束 replay_skip 前缀重放完成后的本片新增扫描工作，不是整次调用的墙钟上限。
# 重放仍受 enumeration_budget（含已持久前缀的一次额外额度）约束，并至少推进一个新位置后检查时间，避免恢复饥饿。
# 函数用途: 有界续扫 owner；完成后回收孤儿预留并记录准确观察用量。
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
            state = {'prep': deepcopy(prep), **_quota_metadata_payload(cursor)}
            if not cursor:
                state.pop('metadata_complete', None)
            state['dir_batches'] = deepcopy(cursor.get('dir_batch', {}))
            scan = {'iterator': _sorted_quota_entries(root, after=cursor.get('last_relpath', ''), state=state),
                    'revision': ledger['revision'], 'last_relpath': cursor.get('last_relpath', ''),
                    'orphans': cursor.get('orphans', sorted(ledger['reservations'])),
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
            while scanned < max_entries and enumerated < enumeration_budget:
                # 已计费前缀重放完并推进至少一个新位置后才检查时间；总枚举额度不变。
                if (scanned or enumerated > replay_skip) and time.monotonic() >= deadline:
                    break
                try:
                    entry = next(scan['iterator'])
                except StopIteration:
                    complete = True
                    break
                if entry is None:
                    enumerated += 1
                    if replay_skip > 0 and enumerated == replay_skip:
                        # 旧前缀不消耗新一片的时间额度；只在这次重放完成时刷新一次。
                        deadline = time.monotonic() + max_ms / 1000
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
            'partial_inode_count': scan['inodes'], 'started_at': scan['started_at'], 'orphans': scan['orphans'],
            'ledger_revision': ledger['revision'], 'root_dev': identity.st_dev, 'root_ino': identity.st_ino}
        cursor_payload.update(_quota_metadata_payload(scan['state']))
        prep_state = scan.get('state', {}).get('prep') if isinstance(scan.get('state'), dict) else None
        if not complete and isinstance(prep_state, dict):
            checkpoint = scan['state'].get('_checkpoint_fn')
            if callable(checkpoint):
                point = checkpoint()
                prep_state.update(dir_offset=point['offset'], block_skip=0, native_pending=point['pending'])
            cursor_payload['prep'] = _persisted_quota_prep(prep_state)
        batches = scan['state'].get('dir_batches', {})
        for rel, batch in list(batches.items()):
            prefix = rel + '/' if rel else ''
            batch['names'] = [name for name in batch['names'] if prefix + name > scan['last_relpath']]
            batch['control_names'] = [name for name in batch.get('control_names', [])
                                      if prefix + name > scan['last_relpath']]
            if not batch['names'] or rel and not scan['last_relpath'].startswith(prefix):
                del batches[rel]
        if not complete:
            persisted, names_count, names_bytes = {}, 0, 0
            for rel, batch in batches.items():
                names = []
                for name in batch['names']:
                    size = len(os.fsencode(name))
                    if (names_count >= _QUOTA_MAX_PERSISTED_DIR_ENTRIES
                            or names_bytes + size > _QUOTA_MAX_PERSISTED_NAME_BYTES):
                        break
                    names.append(name)
                    names_count += 1
                    names_bytes += size
                if names:
                    persisted[rel] = {**batch, 'names': names,
                                      'overflow': bool(batch.get('overflow')) or len(names) < len(batch['names'])}
            cursor_payload['dir_batch'] = persisted
        ledger['reconcile_cursor'] = cursor_payload
        if complete:
            ledger.update(used_bytes=scan['used'], inode_count=scan['inodes'], dirty_since=None,
                          reservations={}, reserved_bytes=0)
            scan['iterator'].close()
            _QUOTA_SCANS.pop(key, None)
        # 未完成的一轮不改变账本可信度：干净账继续按受控写代次准入，脏账保持脏直到本轮完成。
        _write_quota_ledger(root, ledger)
        return {'complete': complete, 'scanned_entries': scanned, 'enumerated_entries': enumerated,
                'replay_allowance': replay_allowance,
                'used_bytes': ledger['used_bytes'],
                'inode_count': ledger['inode_count'], 'cursor': ledger['reconcile_cursor']}


# LLM: 进程内窗口可到65536项；持久化只保留最小的前缀并记下上界，冷恢复继续枚举时不再收上界以外的名字，
#   窗口耗尽后从末名之后再枚举，裁掉的名字不漏也不重复计费。
# 函数用途: 把排序准备状态裁剪到持久名称条数与字节上限。
def _persisted_quota_prep(prep):
    names, size = [], 0
    for name in prep['names']:
        size += len(os.fsencode(name))
        if len(names) >= _QUOTA_MAX_PERSISTED_DIR_ENTRIES or size > _QUOTA_MAX_PERSISTED_NAME_BYTES:
            return {**prep, 'names': names, 'overflow': True, 'bound': names[-1]}
        names.append(name)
    return {**prep, 'names': names}


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
            or not isinstance(names, list) or len(names) > _QUOTA_MAX_PERSISTED_DIR_ENTRIES
            or any(not _valid_quota_parts((n.rstrip('/'),)) or n <= after for n in names)
            or sum(len(os.fsencode(n)) for n in names) > _QUOTA_MAX_PERSISTED_NAME_BYTES
            or names != sorted(set(names))
            or not isinstance(dir_id, list) or len(dir_id) != 4
            or any(type(v) is not int or v < 0 for v in dir_id)):
        return False
    bound = prep.get('bound')
    if bound is not None and (not isinstance(bound, str) or any(name > bound for name in names)):
        return False
    pending = prep.get('native_pending')
    controls = prep.get('control_names', [])
    if (not isinstance(controls, list) or controls != sorted(set(controls))
            or any(parts or name not in (_QUOTA_LEDGER, _QUOTA_LOCK_BASENAME + '.lock') for name in controls)):
        return False
    if pending is not None:
        if not isinstance(pending, str) or len(pending) > (_QUOTA_DIR_BUFFER_BYTES + 2) // 3 * 4:
            return False
        try:
            if len(base64.b64decode(pending, validate=True)) > _QUOTA_DIR_BUFFER_BYTES or block_skip:
                return False
        except ValueError:
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
#   本轮开始后新出现的预留只能是准入后崩溃的孤儿，其落盘差额未并入游标，必须从头重扫。
# 函数用途: 验证持久续点可用性，防止LRU淘汰或重启丢失可信扫描进度。
def _resumable_quota_cursor(root, ledger):
    cursor = ledger.get('reconcile_cursor', {})
    if not isinstance(cursor, dict) or cursor.get('complete') is not False:
        return None
    if cursor.get('metadata_complete') is True:
        directories = cursor.get('dir_stamps')
        # 旧四整数指纹不能接着累加七整数摘要；从零重扫，不能沿用旧的部分用量。
        if (not isinstance(directories, dict) or '' not in directories
                or sum(_quota_row_bytes(rel) for rel in directories) > _QUOTA_MAX_METADATA_BYTES
                or any(not isinstance(rel, str) or not _valid_quota_parts(rel.split('/') if rel else [])
                       or not isinstance(stamp, list) or len(stamp) != 7
                       or any(type(value) is not int or value < 0 for value in stamp)
                       for rel, stamp in directories.items())):
            return None
    counts = ('scanned', 'partial_used_bytes', 'partial_inode_count', 'ledger_revision', 'root_dev', 'root_ino')
    if any(type(cursor.get(key)) is not int or cursor[key] < 0 for key in counts):
        return None
    last, started, epoch = cursor.get('last_relpath'), cursor.get('started_at'), cursor.get('epoch')
    if (not isinstance(last, str) or last.startswith('/') or any(p in ('.', '..') for p in last.split('/'))
            or not isinstance(epoch, str) or not epoch or len(epoch) > 128
            or type(started) not in (float, int) or not math.isfinite(started)
            or not 0 <= time.time() - started <= _QUOTA_SCAN_CURSOR_TTL_SECONDS
            or cursor['ledger_revision'] != ledger['revision']):
        return None
    if 'prep' in cursor and cursor['prep'] is not None and not _valid_quota_prep_cursor(cursor['prep']):
        return None
    orphans = cursor.get('orphans')
    if (not isinstance(orphans, list) or any(not isinstance(item, str) for item in orphans)
            or not set(ledger['reservations']) <= set(orphans)):
        return None
    batches = cursor.get('dir_batch', {})
    if not isinstance(batches, dict) or len(batches) > 65:
        return None
    total_names, names_bytes = 0, 0
    for rel, batch in batches.items():
        if not isinstance(batch, dict) or not _valid_quota_parts(batch.get('parts')) or rel != '/'.join(batch['parts']):
            return None
        names, identity = batch.get('names'), batch.get('dir_id')
        if (not isinstance(names, list) or names != sorted(set(names))
                or any(not _valid_quota_parts((n.rstrip('/'),)) for n in names)
                or not isinstance(identity, list) or len(identity) != 4
                or any(type(v) is not int or v < 0 for v in identity)):
            return None
        prefix = rel + '/' if rel else ''
        controls = batch.get('control_names', [])
        if (not isinstance(controls, list) or controls != sorted(set(controls))
                or any(rel or name not in (_QUOTA_LEDGER, _QUOTA_LOCK_BASENAME + '.lock') or name <= last
                       for name in controls)):
            return None
        if any(prefix + name <= last for name in names):
            return None
        total_names += len(names)
        names_bytes += sum(len(os.fsencode(name)) for name in names)
    if (total_names > _QUOTA_MAX_PERSISTED_DIR_ENTRIES
            or names_bytes > _QUOTA_MAX_PERSISTED_NAME_BYTES):
        return None
    info = root.stat(follow_symlinks=False)
    return cursor if (info.st_dev, info.st_ino) == (cursor['root_dev'], cursor['root_ino']) else None


# LLM: 只读POSIX目录流的公开dirent64布局，不访问CPython对象内存；未消费缓冲可持久化，避免冷恢复重放。
# 类用途: 在64位Linux/macOS上提供等同scandir的惰性名称枚举及准确逐项偏移。
class _QuotaDirectoryStream:
    def __init__(self, descriptor):
        self.descriptor = descriptor
        self.pending = b''
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.operation = getattr(self.libc, '__getdirentries64' if sys.platform == 'darwin' else 'getdents64')
        argument_types = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
        if sys.platform == 'darwin':
            argument_types.append(ctypes.POINTER(ctypes.c_longlong))
        self.operation.argtypes = argument_types
        self.operation.restype = ctypes.c_ssize_t
        self.buffer = ctypes.create_string_buffer(_QUOTA_DIR_BUFFER_BYTES)

    def __enter__(self):
        return self

    def __exit__(self, exc_type=None, exc=None, traceback=None):
        return False

    def __iter__(self):
        return self

    def checkpoint(self):
        return {'pending': base64.b64encode(self.pending).decode('ascii'),
                'offset': _safe_dir_lseek(self.descriptor, 0, os.SEEK_CUR)}

    def restore(self, pending):
        self.pending = base64.b64decode(pending, validate=True)

    def __next__(self):
        while True:
            if not self.pending:
                args = [self.descriptor, self.buffer, len(self.buffer)]
                if sys.platform == 'darwin':
                    base = ctypes.c_longlong()
                    args.append(ctypes.byref(base))
                count = self.operation(*args)
                if count < 0:
                    error = ctypes.get_errno()
                    raise OSError(error, os.strerror(error))
                if not count:
                    raise StopIteration
                self.pending = self.buffer.raw[:count]
            # 仅解析已复制且有界的字节串，不解引用dirent或CPython对象内存。
            name_offset = 21 if sys.platform == 'darwin' else 19
            type_offset = 20 if sys.platform == 'darwin' else 18
            record_length = int.from_bytes(self.pending[16:18], sys.byteorder)
            if not name_offset < record_length <= len(self.pending):
                raise OwnerQuotaUnavailable('invalid quota directory record')
            record, self.pending = self.pending[:record_length], self.pending[record_length:]
            raw = record[name_offset:]
            if b'\x00' not in raw:
                raise OwnerQuotaUnavailable('unterminated quota directory name')
            name = os.fsdecode(raw.split(b'\x00', 1)[0])
            if name in ('.', '..'):
                continue
            return _QuotaDirectoryEntry(name, record[type_offset], self.descriptor)


# LLM: d_type未知时才做nofollow stat；不持有新FD，不跟随符号链接。
# 类用途: 为排序准备暴露目录项名称和目录判定。
@dataclass
class _QuotaDirectoryEntry:
    name: str
    kind: int
    descriptor: int

    def is_dir(self, *, follow_symlinks=False):
        if follow_symlinks or self.kind == 0:
            return stat.S_ISDIR(os.stat(self.name, dir_fd=self.descriptor, follow_symlinks=follow_symlinks).st_mode)
        return self.kind == 4


# LLM: 仅已知64位dirent64平台使用逐项内核续点；其它平台保留scandir的有界块重放协议。
# 函数用途: 打开真实惰性目录枚举器，不在预算门外做列表化。
def _quota_scandir(descriptor):
    if ctypes.sizeof(ctypes.c_void_p) == 8 and sys.platform in ('darwin', 'linux'):
        try:
            return _QuotaDirectoryStream(descriptor)
        except AttributeError:
            pass
    return os.scandir(descriptor)


# LLM: 单遍在进程内保留最多65536项/4MiB的最小名称窗口，热续扫一遍枚举即覆盖大平铺目录；溢出也缓存，耗尽窗口后再枚举。
#   持久化时窗口裁到4096项/192KiB并标记溢出，冷恢复有界。
#   暂停记录当前getdirentries块起点偏移与候选集，恢复时校验目录身份并从块起点继续有界枚举。
# 函数用途: 可暂停且可跨缓存淘汰有界恢复地准备排序候选，避免大目录在切片重启后原地踏步或超预算。
def _quota_sorted_names(root, parts, after, state=None):
    import bisect

    descriptor = open_directory_beneath(root, parts, require_dir_fd=True)
    try:
        dir_stat = os.fstat(descriptor)
        dir_id = [dir_stat.st_dev, dir_stat.st_ino, dir_stat.st_mtime_ns, dir_stat.st_ctime_ns]
        _capture_quota_metadata(state, parts, dir_stat)
        batches = state.setdefault('dir_batches', {}) if isinstance(state, dict) else {}
        key = '/'.join(parts)
        cached = batches.get(key)
        # root的时间戳被每片账本原子替换扰动；缓存只作枚举加速，准入另核root名称全集和文件指纹。
        def same_directory(saved):
            return saved == dir_id or not parts and isinstance(saved, list) and saved[:2] == dir_id[:2]

        if cached is not None and same_directory(cached.get('dir_id')):
            remaining = cached['names']
            del remaining[:bisect.bisect_right(remaining, after)]
            controls = [name for name in cached.get('control_names', []) if name > after]
            cached['control_names'] = controls
            if remaining or controls or not cached.get('overflow'):
                return sorted([*remaining, *controls])[:_QUOTA_SORT_BATCH_ENTRIES]
            batches.pop(key, None)
        prep = state.get('prep') if isinstance(state, dict) else None
        pending = None
        if (isinstance(prep, dict) and tuple(prep.get('parts', ())) == parts
                and prep.get('after') == after and same_directory(prep.get('dir_id'))):
            names = list(prep.get('names', ()))
            controls = list(prep.get('control_names', ()))
            overflow = prep.get('overflow', False)
            raw_count = int(prep.get('enumerated', 0))
            bound = prep.get('bound')
            resume_offset = int(prep.get('dir_offset', 0))
            replay_remaining = int(prep.get('block_skip', 0))
            pending = prep.get('native_pending')
            if resume_offset > 0:
                _safe_dir_lseek(descriptor, resume_offset, os.SEEK_SET)
        else:
            names = []
            controls = []
            overflow = False
            raw_count = 0
            bound = None
            replay_remaining = 0
        names_bytes = sum(len(os.fsencode(name)) for name in names)
        last_pos = _safe_dir_lseek(descriptor, 0, os.SEEK_CUR)
        block_pos = last_pos
        block_skip = 0
        with _quota_scandir(descriptor) as entries:
            if pending is not None:
                restore = getattr(entries, 'restore', None)
                if not callable(restore):
                    raise OwnerQuotaUnavailable('quota directory cursor backend unavailable')
                restore(pending)
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
                # 两个固定控制文件另存，避免4096个payload恰好被控制项挤出窗口而整目录重扫。
                control = not parts and name in (_QUOTA_LEDGER, _QUOTA_LOCK_BASENAME + '.lock')
                if name > after and control:
                    if name not in controls:
                        controls.append(name)
                        controls.sort()
                elif name > after and bound is not None and name > bound:
                    overflow = True
                elif name > after:
                    name_size = len(os.fsencode(name))
                    full = (len(names) >= _QUOTA_MAX_CACHED_DIR_ENTRIES
                            or names_bytes + name_size > _QUOTA_MAX_CACHED_NAME_BYTES)
                    if full:
                        overflow = True
                    if not full or names and name < names[-1]:
                        idx = bisect.bisect_left(names, name)
                        if idx == len(names) or names[idx] != name:
                            names.insert(idx, name)
                            names_bytes += name_size
                            while (len(names) > _QUOTA_MAX_CACHED_DIR_ENTRIES
                                   or len(names) > 1 and names_bytes > _QUOTA_MAX_CACHED_NAME_BYTES):
                                names_bytes -= len(os.fsencode(names.pop()))
                                overflow = True
                raw_count += 1
                if isinstance(state, dict):
                    state['prep'] = {
                        'parts': list(parts), 'after': after, 'enumerated': raw_count,
                        'dir_offset': block_pos, 'block_skip': block_skip,
                        'names': names, 'dir_id': dir_id,
                        'control_names': controls,
                        'overflow': overflow, 'bound': bound,
                    }
                    state['_checkpoint_fn'] = getattr(entries, 'checkpoint', None)
                yield None
            if isinstance(state, dict):
                state['prep'] = None
            batches[key] = {'parts': list(parts), 'dir_id': dir_id, 'names': names,
                            'control_names': controls, 'overflow': overflow}
            return sorted([*names, *controls])[:_QUOTA_SORT_BATCH_ENTRIES]
    finally:
        if isinstance(state, dict):
            state.pop('_checkpoint_fn', None)
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
        if info is not None and stat.S_ISDIR(info.st_mode) and not _quota_excluded(parts, sub_name, info):
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
        batch = state.get('dir_batches', {}).get('/'.join(parts), {}) if isinstance(state, dict) else {}
        window_overflow = batch.get('overflow', False)
        for name in names:
            relpath = '/'.join((*parts, name))
            if relpath <= after and not (name.endswith('/') and after.startswith(relpath)):
                continue
            try:
                info = _quota_sorted_stat(root, parts, name.rstrip('/'))
            except FileNotFoundError:
                if isinstance(state, dict):
                    state['metadata_complete'] = False
                if relpath > after:
                    yield relpath, 0, 0
                continue
            excluded = _quota_excluded(parts, name.rstrip('/'), info)
            if relpath > after:
                _capture_quota_metadata(state, parts, info, name.rstrip('/'))
                regular = stat.S_ISREG(info.st_mode) and not excluded
                yield relpath, max(0, info.st_size) if regular else 0, int(regular)
            if stat.S_ISDIR(info.st_mode) and not excluded:
                try:
                    yield from _sorted_quota_entries(root, (*parts, name.rstrip('/')), after=after, state=state)
                except FileNotFoundError:
                    continue
        # 字节上限或持久化裁剪可产生不足128项的窗口；短窗口不等于整个目录已读完。
        if len(names) < _QUOTA_SORT_BATCH_ENTRIES and not window_overflow:
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
