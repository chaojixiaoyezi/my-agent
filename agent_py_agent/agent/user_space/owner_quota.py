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
# 宿主在准入外维护的owner状态文件（运行库及SQLite伴随文件、审计日志、维护状态）：照常计费，但不进目录摘要，
#   宿主自己的写入不被前台核验当成准入外篡改；准入外的字节变化由后台对账计入。
_QUOTA_HOST_FILES = frozenset({*(('runtime.db' + suffix,) for suffix in ('', '-wal', '-shm', '-journal')),
                               ('audit_log.jsonl',), ('data', 'maintenance.json')})
# 宿主在准入外维护的整棵owner子树（对话压缩归档）：同样照常计费、不进目录摘要。
_QUOTA_HOST_TREES = frozenset({'compact'})
_QUOTA_SCANS: OrderedDict = OrderedDict()
_QUOTA_SCANS_LOCK = RLock()
# 进程内配额续扫缓存最多保留的活跃 owner 扫描器数量。
_QUOTA_SCAN_CACHE_SIZE = 16
# 持久配额续扫游标允许恢复的最长闲置秒数；放宽至两倍日维护周期，避免稍迟的日调度拍次误判未完成游标过期。
_QUOTA_SCAN_CURSOR_TTL_SECONDS = 172800
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
# 前台按名字核对大目录时至多枚举的目录项数；行记录已超过它的目录不核、留给后台对账，行内而实际超过即判不通过。
_QUOTA_LARGE_DIR_MAX_ENTRIES = 65536
# 逐目录摘要行格式代次：3为十整数行（含物理目录项、计费普通文件数与需lstat定性的计费项数），摘要不含宿主状态；
#   其它代次的表不用于核验，进行中的一轮从头重扫。
_QUOTA_ROW_SCHEMA_VERSION = 3
# 进程内最多记住这么多owner在本进程写账本后的root目录戳；淘汰只失去结算时免扫root的优化。
_QUOTA_ROOT_STAMP_CACHE_SIZE = 256
_QUOTA_ROOT_STAMPS: OrderedDict = OrderedDict()
_QUOTA_FINITE_ROOTS: OrderedDict = OrderedDict()
_QUOTA_ROOT_STAMPS_LOCK = RLock()
_QUOTA_HASH_MASK = (1 << 64) - 1
# 原生目录读取缓冲固定32KiB；未消费记录随prep持久化，不在重启时重新枚举。
_QUOTA_DIR_BUFFER_BYTES = 32768
# 仅传递同次前台生成器的观察容器，不携带权限或跨调用缓存。
_QUOTA_SNAPSHOT_STATE = ContextVar('quota_snapshot_state', default=None)
# 进程内只绑定一次的libc目录读取函数，不含任何owner状态。
_QUOTA_DIRENT_READER: list = []


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
        self._invalidated = False
        self._pending = None
        self._snapshot = None
        # 中间结算只把本准入预留降为0字节标记、不移除：业务仍可能回滚，最后一次退出结算才清除。
        self._held = False

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
        if self._snapshot is not None:
            self._resolve_snapshot(ledger)
        # 干净基线已显示超过128项时有界快照必然读不完，直接走基线核验，不再白做一次全新核对。
        complete = False
        snapshot_invalidated = False
        if not _quota_baseline_exceeds(ledger, 128):
            snapshot = {}
            token = _QUOTA_SNAPSHOT_STATE.set(snapshot)
            try:
                total, count, complete = _bounded_quota_snapshot(root, max_entries=128, max_ms=10)
            finally:
                _QUOTA_SNAPSHOT_STATE.reset(token)
                if snapshot.get('metadata_invalidated'):
                    complete = False
                    snapshot_invalidated = True
                    self._invalidated = True
                    self._ledger = None
                    ledger['dirty_since'] = ledger.get('dirty_since') or time.time()
                    if isinstance(ledger.get('reconcile_cursor'), dict):
                        _drop_quota_metadata(ledger['reconcile_cursor'], invalidated=True)
                    else:
                        ledger['reconcile_cursor'] = {'complete': False, 'metadata_complete': False, 'metadata_invalidated': True}
                    try:
                        _write_quota_ledger(root, ledger)
                    except BaseException:
                        _write_quota_ledger(root, ledger)
                        if not self._before:
                            self._invalidated = False
                        raise
                    if not self._before:
                        self._invalidated = False
        # 已先持久化观察失效，同一准入分阶段调用时结算上阶段及后续前像读取失败均保留该事实。
        if not complete and self._before:
            self.commit_delta(final=False)
            ledger = _read_quota_ledger(root)
        touched = {'/'.join(path.relative_to(root).parts[:depth])
                   for path in relevant for depth in range(len(path.relative_to(root).parts))}
        # 同一准入中间结算留下的0字节标记只拦冷进程，本准入后续阶段照常核验基线。
        authoritative = not complete and not snapshot_invalidated and _verified_quota_baseline(
            root, ledger, touched, reservation=self._reservation)
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
        rebased = None
        if complete:
            ledger.update(used_bytes=total, inode_count=count, dirty_since=None)
            # 所有正规写者都持同一owner锁；拿到锁时旧预留只可能是退出未确认的遗留。
            ledger['reservations'] = {}
            # 全新快照已计入全部落盘结果，但上阶段前像、待确认结算与失效只在快照确已发布后才随之以当前大小重定基准：
            #   写入报错时按读回修订判定，替换前失败仍按旧前像结算，替换后才报错不再叠加同一差额。
            rebased = ({path: _logical_file_size(path) for path in self._before},
                       {path: _billed_file_inode(root, path) for path in self._before}, {})
            ledger['reconcile_cursor'] = _quota_baseline_cursor(root, ledger, snapshot)
        elif not authoritative:
            ledger['dirty_since'] = ledger.get('dirty_since') or time.time()
        self._authoritative_baseline = complete or authoritative
        children = self._before_children if rebased is None else rebased[2]
        for path in relevant:
            parts = path.relative_to(root).parts
            for depth in range(1, len(parts) + 1):
                child_parts = parts[:depth]
                key = ('/'.join(child_parts[:-1]), child_parts[-1])
                if key not in children:
                    try:
                        info = _quota_sorted_stat(root, child_parts[:-1], child_parts[-1])
                    except FileNotFoundError:
                        before = None
                    else:
                        before = _quota_child_row(child_parts[:-1], child_parts[-1], info)
                    children[key] = (before, depth < len(parts))
        if rebased is not None:
            # 快照未发布时仍按旧前像结算：新路径的目录项前像照常补入，已有的保持本准入开始时的值。
            for key, row in children.items():
                self._before_children.setdefault(key, row)
        for path, size in current_sizes.items():
            self._before.setdefault(path, size)
            self._before_inodes.setdefault(path, _billed_file_inode(root, path))
        delta = max(0, final - replaced)
        ledger['reservations'][self._reservation] = {'bytes': delta, 'expires_at': time.time() + 300,
            'paths': [str(path.relative_to(root)) for path in self._before]}
        ledger['reserved_bytes'] = sum(row['bytes'] for row in ledger['reservations'].values())
        ledger['revision'] += 1
        # 完整基线与进行中的对账都随受控写代次前进；只有原本同步的游标才跟进，脏账不会因此变可信。
        cursor = ledger.get('reconcile_cursor')
        if isinstance(cursor, dict) and cursor.get('ledger_revision') == ledger['revision'] - 1:
            cursor['ledger_revision'] = ledger['revision']
        if rebased is not None:
            self._snapshot = (ledger['revision'], rebased)
        self._ledger = (ledger, _write_quota_ledger(root, ledger))
        if rebased is not None:
            self._resolve_snapshot(ledger)
        return projection

    # LLM: 锁内只有本准入写账本，快照写入报错后修订只会由这次写入推进：读回修订等于快照修订即已发布，前像、待确认结算与
    #   失效随快照重定基准；仍是快照前修订即未发布，保留旧前像照常结算；其余无法归属，按观察失效置脏交后台对账。
    # 函数用途: 按锁内读回的账本判定未确认的完整快照是否已发布，并据此决定是否消费上阶段前像。
    def _resolve_snapshot(self, ledger):
        revision, (sizes, inodes, children) = self._snapshot
        self._snapshot = None
        if ledger['revision'] == revision:
            self._before.update(sizes)
            self._before_inodes.update(inodes)
            self._before_children = children
            self._pending = None
            self._invalidated = False
        elif ledger['revision'] != revision - 1:
            self._invalidated = True

    # LLM: 即使业务抛异常也核实际大小；读回失败保留预留和dirty，不能伪造abort成功。
    #   结算写入报错不证明未替换：保留待确认修订与后像，下次结算锁内读回判定，已发布只补写确认、不再叠加同一差额。
    #   final为False是check()内的中间结算：业务仍可能回滚，盘上保留本准入0字节预留标记让冷进程不信任该账；
    #   只有准入退出的最后一次结算（final）才移除本准入预留。
    # 函数用途: 以真实文件差值结算本次预留，不把预计final_size当实际写入。
    def commit_delta(self, *, final=True):
        if not self.enabled or (not self._before and not self._invalidated and not (final and self._held)):
            return
        root = self._enforcer.owner_root
        # 整个准入持同一owner锁；账本文件身份未变即是本准入刚写的那份，免去一次大账本读取与解析。
        cached, self._ledger = self._ledger, None
        published = False
        if self._snapshot is not None or self._pending is not None:
            try:
                ledger = _read_quota_ledger(root)
            except BaseException:
                # 读回失败无法判定是否已发布：待确认路径之后又有变化时，盘上可能是已失准的干净后像，以本次结算后像置脏与失效落盘，
                #   不再结算差额；路径未变时盘上要么是准确后像、要么仍带本准入预留，快照未确认时同理，都不会放宽增长。
                if self._pending is not None:
                    _, after, after_inodes, fallback = self._pending
                    # 大小或inode对照本身报错同样无法确认，按已变化处理：dirty/失效照常落盘，再传播原读回异常。
                    try:
                        unchanged = ({**self._before, **after}
                                     == {path: _logical_file_size(path) for path in self._before}
                                     and {**self._before_inodes, **after_inodes}
                                     == {path: _billed_file_inode(root, path) for path in self._before})
                    except Exception:
                        unchanged = False
                    if not unchanged:
                        if final:
                            fallback['reservations'].pop(self._reservation, None)
                            fallback['reserved_bytes'] = sum(row['bytes'] for row in fallback['reservations'].values())
                        fallback['dirty_since'] = fallback.get('dirty_since') or time.time()
                        if isinstance(fallback.get('reconcile_cursor'), dict):
                            _drop_quota_metadata(fallback['reconcile_cursor'], invalidated=True)
                        else:
                            fallback['reconcile_cursor'] = {'complete': False, 'metadata_complete': False,
                                                            'metadata_invalidated': True}
                        try:
                            _write_quota_ledger(root, fallback)
                        except BaseException:
                            _write_quota_ledger(root, fallback)
                raise
            # 先判定未确认的完整快照：已发布则上阶段待确认结算已被快照吸收，不再按同一修订误判。
            if self._snapshot is not None:
                self._resolve_snapshot(ledger)
            if self._pending is not None:
                # 锁内只有本准入写账本，待确认期间修订只会由这次结算推进：读回仍是结算前修订即未发布，照常重算；否则已发布或无法归属，不再叠加。
                revision, after, after_inodes, _ = self._pending
                published = ledger['revision'] != revision - 1
                # 修订对不上，或前像路径在待确认后又有变化（待确认路径对照其后像，之后才加入的路径对照其前像），
                #   差额无法精确归属：按观察失效置脏，交后台对账重算。对照时大小或inode读取报错同样无法确认，一并置脏落盘。
                if published:
                    try:
                        settled = (ledger['revision'] == revision
                                   and {**self._before, **after}
                                   == {path: _logical_file_size(path) for path in self._before}
                                   and {**self._before_inodes, **after_inodes}
                                   == {path: _billed_file_inode(root, path) for path in self._before})
                    except Exception:
                        settled = False
                    if not settled:
                        self._invalidated = True
        elif cached is not None and cached[1] is not None and _quota_ledger_identity(root) == cached[1]:
            ledger = cached[0]
        else:
            ledger = _read_quota_ledger(root)
        if self._invalidated:
            ledger['dirty_since'] = ledger.get('dirty_since') or time.time()
            if isinstance(ledger.get('reconcile_cursor'), dict):
                _drop_quota_metadata(ledger['reconcile_cursor'], invalidated=True)
            else:
                ledger['reconcile_cursor'] = {'complete': False, 'metadata_complete': False, 'metadata_invalidated': True}
        if not self._before or published:
            # 已发布的后像补写一次即确认；首次补写报错（含替换前失败）就在本次退出重发同一份确认，dirty/失效照带、不再叠加差额，
            #   最后一次退出不依赖以后的结算；重发再报错仍保留待确认状态与前像，由外层退出重做同一判定。
            #   最后一次结算的确认才移除本准入预留：两次确认都在替换前失败时，盘上仍留预留标记，冷进程不会信任偏低的账。
            if final:
                ledger['reservations'].pop(self._reservation, None)
                ledger['reserved_bytes'] = sum(row['bytes'] for row in ledger['reservations'].values())
            confirmed = False
            try:
                _write_quota_ledger(root, ledger)
                confirmed = True
            finally:
                if not confirmed:
                    _write_quota_ledger(root, ledger)
                self._pending = None
                self._invalidated = False
                self._before.clear()
                self._before_inodes.clear()
                self._before_children.clear()
                self._held = not final
            return
        self._pending = None
        invalidated_ledger = deepcopy(ledger) if self._invalidated else None
        try:
            after = {path: _logical_file_size(path) for path in self._before}
            after_inodes = {path: _billed_file_inode(root, path) for path in self._before}
            delta = sum(after[path] - before for path, before in self._before.items())
            ledger['used_bytes'] = max(0, ledger['used_bytes'] + delta)
            ledger['inode_count'] = max(0, ledger['inode_count'] + sum(
                after_inodes[path] - self._before_inodes[path] for path in self._before))
            ledger['reservations'].pop(self._reservation, None)
            if not final:
                # 差额已计入used_bytes，标记不再占字节；业务回滚后外层结算在替换前失败时，冷进程见预留即拒绝增长。
                ledger['reservations'][self._reservation] = {'bytes': 0, 'expires_at': time.time() + 300,
                    'paths': [str(path.relative_to(root)) for path in self._before]}
            ledger['reserved_bytes'] = sum(row['bytes'] for row in ledger['reservations'].values())
            root_vouched = _quota_root_vouched(root, ledger['revision'])
            ledger['revision'] += 1
            cursor = ledger.get('reconcile_cursor')
            synced = isinstance(cursor, dict) and cursor.get('ledger_revision') == ledger['revision'] - 1
            if synced and cursor.get('complete') is True:
                # 本准入自己的标记不阻止目录表随受控写前进；其它预留仍按遗留处理。
                if ledger.get('dirty_since') is None and not ledger['reservations'].keys() - {self._reservation}:
                    if _refresh_quota_baseline(root, cursor, self._before_children, root_vouched=root_vouched):
                        cursor['ledger_revision'] = ledger['revision']
                    else:
                        ledger['dirty_since'] = time.time()
            elif synced and cursor.get('complete') is False:
                # 进行中的对账不因受控写重来：已扫过的路径补差额，变过目录项的窗口作废重枚举；目录核对不通过即置脏。
                if not _apply_quota_pass_changes(root, cursor, {
                        path: (self._before[path], after[path], self._before_inodes[path], after_inodes[path])
                        for path in self._before}, self._before_children, root_vouched=root_vouched):
                    ledger['dirty_since'] = ledger.get('dirty_since') or time.time()
                cursor['ledger_revision'] = ledger['revision']
            self._pending = (ledger['revision'], after, after_inodes, ledger)
            _write_quota_ledger(root, ledger)
            self._pending = None
            self._invalidated = False
            self._before.clear()
            self._before_inodes.clear()
            self._before_children.clear()
            self._held = not final
        finally:
            if self._pending is not None:
                _write_quota_ledger(root, ledger)
                self._pending = None
                self._invalidated = False
                self._before.clear()
                self._before_inodes.clear()
                self._before_children.clear()
                self._held = not final
            elif self._invalidated and invalidated_ledger is not None:
                _write_quota_ledger(root, invalidated_ledger)
                self._invalidated = False

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
        if self.max_bytes > 0:
            _remember_quota_finite_root(self.owner_root)

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


# 函数用途: 按与准入一致的整数口径解析quota.json的max_disk_mb（拒绝布尔值与溢出）。
def _parse_quota_disk_mb(raw) -> int:
    if isinstance(raw, bool):
        return 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError, OverflowError):
        return 0


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
    max_disk_mb = _parse_quota_disk_mb(report.payload.get("max_disk_mb"))
    return OwnerQuotaEnforcer(
        root,
        max_bytes=max_disk_mb * 1024 * 1024,
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
    if len(encoded) > 1_048_576 and isinstance(cursor, dict) and cursor.get('metadata_complete') is True:
        _drop_quota_metadata(cursor)
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
#   按物理目录项累加：快照对排除项与控制文件同样消耗条目额度。
# 函数用途: 判断最近可信基线中的目录项是否超过给定数量，超过即停止累加。
def _quota_baseline_exceeds(ledger, entries):
    cursor = ledger.get('reconcile_cursor')
    if (ledger.get('dirty_since') is not None or not isinstance(cursor, dict)
            or cursor.get('ledger_revision') != ledger['revision']):
        return False
    table = _quota_cursor_table(cursor)
    if table is None:
        return cursor.get('complete') in (True, False) and ledger['inode_count'] > entries
    total = 0
    for row in table.values():
        if _valid_quota_row(row):
            total += _quota_row_entries(row)
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


# LLM: 只看路径、不stat：宿主固定状态文件、宿主只读子目录内的原子写临时名（如data/.maintenance.json.<随机>.tmp），
#   以及宿主子树（含子树根本身）内的一切目录项；根目录等模型可写位置的临时名不豁免，保持失败关闭。
# 函数用途: 判断一个目录项是否为宿主在准入外维护、不进目录摘要的状态。
def _quota_host_managed(parts, name):
    if (parts[0] if parts else name) in _QUOTA_HOST_TREES or (*parts, name) in _QUOTA_HOST_FILES:
        return True
    if not (parts and name.startswith('.') and name.endswith('.tmp')):
        return False
    return (*parts, name[1:].rpartition('.')[0].rpartition('.')[0]) in _QUOTA_HOST_FILES


# LLM: 与_quota_excluded同口径：这些名字是否排除取决于类型或大小，其余名字（控制账、宿主状态之外）一律计费。
# 函数用途: 判断一个目录项是否必须lstat才能确定计不计费。
def _quota_name_needs_stat(parts, name):
    return name.endswith('.lock') or (*parts, name) in _QUOTA_RUNTIME_DIRS or (*parts, name) in _QUOTA_RUNTIME_FILES


# LLM: 与扫描同一口径：只有不跟随链接的普通文件且不属结构性排除（如0字节*.lock）才计1个inode。
# 函数用途: 返回受控写入目标在结算口径下的计费inode数。
def _billed_file_inode(root, path):
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return 0
    parts = path.relative_to(root).parts
    return int(stat.S_ISREG(info.st_mode) and not _quota_excluded(parts[:-1], parts[-1], info))


# LLM: 排除项与宿主状态贡献为零；其余子项计1项、普通文件字节、64位摘要、是否计费普通文件以及是否需lstat才能定性。
# 函数用途: 返回一个子项对所在目录摘要的加法贡献。
def _quota_child_row(parts, name, info):
    if _quota_excluded(parts, name, info) or _quota_host_managed(parts, name):
        return (0, 0, 0, 0, 0)
    reg_bytes, digest = _quota_child_digest(name, info)
    return (1, reg_bytes, digest, int(stat.S_ISREG(info.st_mode)), int(_quota_name_needs_stat(parts, name)))


# LLM: 键按JSON转义计长，十个整数加分隔符按典型112字节估；只用于给目录表封顶，极端大数撑过1MiB时由写账本兜底放弃整表。
# 函数用途: 估算一行目录摘要的持久字节。
def _quota_row_bytes(rel):
    return len(json.dumps(rel)) + 112


# LLM: 同一目录在旧行、新行与扫描行之间只计一次：扫描行走完即替换旧行，路径上至多64个目录短暂并存，不让临近上限的表因此整表作废。
# 函数用途: 返回游标或扫描状态中目录表的估算持久字节，首次按需计算。
def _quota_metadata_bytes(state):
    size = state.get('metadata_bytes')
    if size is None:
        size = sum(_quota_row_bytes(rel) for rel in {
            rel for key in ('dir_stamps', 'base_rows', 'scan_rows') for rel in state.get(key) or {}})
        state['metadata_bytes'] = size
    return size


# 函数用途: 放弃逐目录表；并发删除或目录换身份导致的观察失效额外标记metadata_invalidated。
def _drop_quota_metadata(state, *, invalidated=False):
    state['metadata_complete'] = False
    if invalidated:
        state['metadata_invalidated'] = True
    for key in ('dir_stamps', 'base_rows', 'scan_rows', 'metadata_bytes'):
        state.pop(key, None)


# LLM: 每目录一行十整数：身份与目录时间、计费项数、普通文件字节、可加摘要、物理目录项数、计费普通文件数、需lstat定性的计费项数。
#   扫描中的目录先记在scan_rows，整目录走完才并入dir_stamps；再次打开同一目录只核身份，目录时间保留首次打开值，
#   让发布完整对账前的漂移清点发现期间的增删。三张表合计按估算持久字节封顶，超过即整表作废，回到受控写代次。
#   宿主子树内的目录不建行：其内容照常计费，但随宿主保留策略增删，不参与前台核验。
# 函数用途: 收集每目录摘要，不保留逐文件名称和指纹。
def _capture_quota_metadata(state, parts, info, name=None):
    if state is None or state.get('metadata_complete', True) is False or parts and parts[0] in _QUOTA_HOST_TREES:
        return
    state['metadata_complete'] = True
    scanning = state.setdefault('scan_rows', {})
    rel = '/'.join(parts)
    if name is None:
        stamp = [int(info.st_dev), int(info.st_ino), int(info.st_mtime_ns), int(info.st_ctime_ns)]
        row = scanning.get(rel)
        if row is not None:
            if row[:2] != stamp[:2]:
                _drop_quota_metadata(state, invalidated=True)
            return
        if rel not in (state.get('base_rows') or {}) and rel not in (state.get('dir_stamps') or {}):
            size = _quota_metadata_bytes(state) + _quota_row_bytes(rel)
            if size > _QUOTA_MAX_METADATA_BYTES:
                _drop_quota_metadata(state)
                return
            state['metadata_bytes'] = size
        scanning[rel] = [*stamp, 0, 0, 0, 0, 0, 0]
        return
    row = scanning.get(rel)
    if row is None:
        _drop_quota_metadata(state, invalidated=True)
        return
    row[7] += 1
    if info is None:
        return
    count, reg_bytes, digest, files, needs_stat = _quota_child_row(parts, name, info)
    if count:
        row[4] += 1
        row[5] += reg_bytes
        row[6] = (row[6] + digest) & _QUOTA_HASH_MASK
        row[8] += files
        row[9] += needs_stat


# LLM: 目录全部子项走完后，本轮新行替换上一轮旧行，旧行同时退出滚动基线。
# 函数用途: 把一个已走完目录的扫描行并入完整目录表。
def _finish_quota_dir(state, parts):
    if not isinstance(state, dict) or state.get('metadata_complete') is not True:
        return
    rel = '/'.join(parts)
    row = state.get('scan_rows', {}).pop(rel, None)
    if row is None:
        return
    state.setdefault('dir_stamps', {})[rel] = row
    (state.get('base_rows') or {}).pop(rel, None)


# LLM: 持久化只复制完整的有界观察；缺字段和旧游标都不能成为授权证明。逐行浅拷贝，避免每片深拷贝整张表。
# 函数用途: 提取可重启的基线元数据。
def _quota_metadata_payload(state):
    if state.get('metadata_complete') is not True:
        payload = {'metadata_complete': False}
        if state.get('metadata_invalidated') is True:
            payload['metadata_invalidated'] = True
        return payload
    payload = {'metadata_complete': True, 'row_format': _QUOTA_ROW_SCHEMA_VERSION,
               'dir_stamps': {rel: list(row) for rel, row in (state.get('dir_stamps') or {}).items()}}
    for key in ('base_rows', 'scan_rows'):
        if state.get(key):
            payload[key] = {rel: list(row) for rel, row in state[key].items()}
    return payload


# LLM: 小owner完整扫描也发布同样的基线，便于受控写入跨过128项门槛。
# 函数用途: 为前台全量小快照生成权威游标。
def _quota_baseline_cursor(root, ledger, state):
    identity = root.stat(follow_symlinks=False)
    return {'complete': True, 'ledger_revision': ledger['revision'],
            'root_dev': identity.st_dev, 'root_ino': identity.st_ino,
            **_quota_metadata_payload(state)}


# LLM: 只有本行格式的完整表才能用于核验；旧格式表、超容、首轮扫描尚无root行时返回None，按受控写代次维持。
#   对账进行中时，已走完目录用本轮新行，其余目录用上一轮仍随受控写维护的旧行；结构损坏返回空表让核验失败。
# 函数用途: 取得游标中可用于核验的逐目录表视图（行对象与游标共享）。
def _quota_cursor_table(cursor):
    if (not isinstance(cursor, dict) or cursor.get('metadata_complete') is not True
            or cursor.get('row_format') != _QUOTA_ROW_SCHEMA_VERSION):
        return None
    fresh, base = cursor.get('dir_stamps'), cursor.get('base_rows', {})
    if not isinstance(fresh, dict) or not isinstance(base, dict):
        return {}
    if cursor.get('complete') is True:
        return fresh
    table = {**base, **fresh}
    return table if '' in table else None


# LLM: 所有持久路径必须是owner内的真实相对组件；不能让坏游标越界或跟随链接。
# 函数用途: 校验元数据键和目录候选名称。
def _valid_quota_parts(parts):
    return (isinstance(parts, (tuple, list)) and len(parts) <= 64
            and all(isinstance(p, str) and p and '/' not in p and '\x00' not in p and p not in ('.', '..') for p in parts))


# LLM: 坏行一律视为核验失败而不是跳过。
# 函数用途: 校验一行持久目录摘要是十个非负整数。
def _valid_quota_row(row):
    return isinstance(row, list) and len(row) == 10 and all(type(value) is int and value >= 0 for value in row)


# 函数用途: 校验一张持久目录表的键与行。
def _valid_quota_table(table):
    return isinstance(table, dict) and all(
        isinstance(rel, str) and _valid_quota_parts(rel.split('/') if rel else []) and _valid_quota_row(row)
        for rel, row in table.items())


# LLM: 计费项数与物理项数取大：上千个空锁名不计费但同样要逐项lstat，不能把这种目录当小目录前台全扫。
# 函数用途: 返回核验一个目录需要逐项处理的目录项数。
def _quota_row_entries(row):
    return max(row[4], row[7])


# LLM: 单目录nofollow打开并逐项lstat（宿主状态按名字跳过，不stat）。身份必须一致；目录时间可以不同（锁侧车、原子替换、
#   宿主状态写入都会改），但排除项与宿主状态之外的项数、普通文件字节与摘要必须与期望完全相等。出现的子目录必须已有摘要行，
#   唯一例外是准入外新建、确实为空的子目录：它不改变计费字节与inode，期望不含它时连同空行一起采纳，此后像其它目录一样核验。
#   前台传cap：物理目录项超过cap立即停下返回None交大目录核对，空锁名再多也不放大前台lstat。
#   通过后采纳当前目录戳、项数与摘要、物理项数、普通文件数与需lstat定性项数。
# 函数用途: 核验一个目录的直接子项与期望摘要一致。
def _verify_quota_dir(root, rel, row, table, expected=None, cap=None):
    parts = rel.split('/') if rel else []
    if not _valid_quota_parts(parts) or not _valid_quota_row(row):
        return False
    target = list(row[4:7] if expected is None else expected)
    descriptor = open_directory_beneath(root, tuple(parts), require_dir_fd=True)
    try:
        info = os.fstat(descriptor)
        if [info.st_dev, info.st_ino] != row[:2]:
            return False
        count = reg_total = acc = files = named = physical = 0
        adopted, adopted_digest = {}, 0
        with os.scandir(descriptor) as entries:
            for entry in entries:
                physical += 1
                if cap is not None and physical > cap:
                    row[7] = max(row[7], physical)
                    return None
                if not parts and entry.name in _QUOTA_CONTROL_NAMES or _quota_host_managed(parts, entry.name):
                    continue
                child = entry.stat(follow_symlinks=False)
                added, reg_bytes, digest, is_file, needs_stat = _quota_child_row(parts, entry.name, child)
                if not added:
                    continue
                count += 1
                child_rel = f'{rel}/{entry.name}' if rel else entry.name
                if stat.S_ISDIR(child.st_mode) and child_rel not in table:
                    stamp = _quota_empty_dir_stamp(root, (*parts, entry.name), child)
                    if stamp is None:
                        return False
                    adopted[child_rel] = stamp
                    adopted_digest = (adopted_digest + digest) & _QUOTA_HASH_MASK
                if count - len(adopted) > target[0]:
                    return False
                reg_total += reg_bytes
                acc = (acc + digest) & _QUOTA_HASH_MASK
                files += is_file
                named += needs_stat
        # 新空目录可能已由上次核验并入期望（其空行未能持久）；两种口径之一相等即通过。
        if [count, reg_total, acc] != target and (
                not adopted or [count - len(adopted), reg_total, (acc - adopted_digest) & _QUOTA_HASH_MASK] != target):
            return False
        row[:4] = [info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns]
        row[4:10] = [count, reg_total, acc, physical, files, named]
        for child_rel, stamp in adopted.items():
            table[child_rel] = [*stamp, 0, 0, 0, 0, 0, 0]
        return True
    finally:
        os.close(descriptor)


# LLM: 准入外新建的空目录不改变计费字节与inode：nofollow打开核对仍是同一目录且没有任何目录项，返回打开时的目录戳
#   供父目录采纳空行（其后新增内容会改变这个戳并被核出）；打不开、身份不符或非空返回None，按准入外增删处理。
# 函数用途: 判断一个尚无摘要行的子目录是否为可采纳的空目录。
def _quota_empty_dir_stamp(root, parts, info):
    try:
        descriptor = open_directory_beneath(root, parts, require_dir_fd=True)
    except OSError:
        return None
    try:
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            return None
        with os.scandir(descriptor) as entries:
            if next(entries, None) is not None:
                return None
        return [current.st_dev, current.st_ino, current.st_mtime_ns, current.st_ctime_ns]
    finally:
        os.close(descriptor)


# LLM: 大目录不逐项lstat：只读名字，持锁时间不随单目录规模增长。行记录已超过_QUOTA_LARGE_DIR_MAX_ENTRIES项的目录
#   前台无法在上限内核对，返回None不核也不采纳目录戳，留给后台对账（与原位追加同属披露残留）；行在上限内而实际枚举超过上限
#   即不通过。root控制账与宿主状态不计；名字即可定性的计费项数须与行中(计费项数-需lstat定性项数)相等。需lstat定性的候选
#   不超过额度时全部lstat，计费数须与行相等；超过额度时只有行记录其计费数为0才抽查额度个且须全不计费，否则计费数无法在
#   额度内证明，一律不通过。通过才采纳新目录戳与物理项数；同数替换、原位追加与抽查之外候选的新内容仍交后台对账。
# 函数用途: 以名字计数核对一个大目录的计费目录项数，无法在上限内核对时返回None。
def _quota_large_dir_matches(root, rel, row):
    if _quota_row_entries(row) > _QUOTA_LARGE_DIR_MAX_ENTRIES:
        return None
    parts = tuple(rel.split('/')) if rel else ()
    descriptor = open_directory_beneath(root, parts, require_dir_fd=True)
    try:
        info = os.fstat(descriptor)
        if [info.st_dev, info.st_ino] != row[:2]:
            return False
        physical, named, candidates = 0, 0, []
        with os.scandir(descriptor) as entries:
            for entry in entries:
                physical += 1
                if physical > _QUOTA_LARGE_DIR_MAX_ENTRIES:
                    return False
                name = entry.name
                if not parts and name in _QUOTA_CONTROL_NAMES or _quota_host_managed(parts, name):
                    continue
                if _quota_name_needs_stat(parts, name):
                    candidates.append(name)
                else:
                    named += 1
        if named != row[4] - row[9] or len(candidates) > _QUOTA_VERIFY_BUDGET_ENTRIES and row[9]:
            return False
        billed = 0
        for name in candidates[:_QUOTA_VERIFY_BUDGET_ENTRIES]:
            try:
                child = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                continue
            billed += not _quota_excluded(parts, name, child)
        if billed != row[9]:
            return False
        row[:4] = [info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns]
        row[7] = physical
        return True
    finally:
        os.close(descriptor)


# 函数用途: 以一次不跟随末级链接的stat比较目录身份与目录时间。
def _quota_stamp_unchanged(root_fd, rel, row):
    try:
        info = os.stat(rel, dir_fd=root_fd, follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError):
        return False
    return [info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns] == row[:4]


# 函数用途: 按“逐层打开目录 + 每个子项一次lstat”估算核验一个目录花掉的额度。
def _quota_dir_cost(rel, row):
    return _quota_row_entries(row) + _QUOTA_VERIFY_OPEN_COST_ENTRIES * (rel.count('/') + 2 if rel else 1)


# LLM: 不大的目录逐项核（超过额度即停转大目录）；大目录先O(1)比目录戳，变了再按名字计数核计费项数。
#   root目录戳因账本原子替换必变，以本进程写账本后的root戳为准。返回花掉的额度，核对不通过返回None。
# 函数用途: 前台核验逐目录表中的一行。
def _verify_quota_entry(root, root_fd, rel, row, table, root_vouched):
    if not isinstance(rel, str) or not _valid_quota_row(row):
        return None
    if _quota_row_entries(row) <= _QUOTA_VERIFY_BUDGET_ENTRIES:
        result = _verify_quota_dir(root, rel, row, table, cap=_QUOTA_VERIFY_BUDGET_ENTRIES)
        if result is not None:
            return _quota_dir_cost(rel, row) if result else None
    unchanged = root_vouched if not rel else _quota_stamp_unchanged(root_fd, rel, row)
    if unchanged:
        return 1
    matched = _quota_large_dir_matches(root, rel, row)
    if matched is None:
        return 1
    return _QUOTA_VERIFY_BUDGET_ENTRIES if matched else None


# LLM: budget=None逐目录全量核验，只供测试与诊断。准入传budget：先核本次写入路径上的目录，余下额度按持久位置轮转核
#   其它目录；物理项超过额度的大目录只比目录戳（变了按名字计数），前台工作不随整树规模和空锁名数量增长。
# 函数用途: 有界核验持久逐目录摘要并推进轮转位置。
def _verify_quota_metadata(root, cursor, *, touched=(), budget=None, root_vouched=False):
    try:
        table = _quota_cursor_table(cursor)
        if not table or '' not in table:
            return False
        if budget is None:
            return all(isinstance(rel, str) and _verify_quota_dir(root, rel, row, table)
                       for rel, row in list(table.items()))
        root_fd = open_directory_beneath(root, (), require_dir_fd=True)
        try:
            spent, checked = 0, set()
            for rel in sorted(touched):
                row = table.get(rel)
                if row is None:
                    continue
                checked.add(rel)
                cost = _verify_quota_entry(root, root_fd, rel, row, table, root_vouched)
                if cost is None:
                    return False
                spent += cost
            names = list(table)
            position = cursor.get('verify_pos')
            position = position % len(names) if type(position) is int and position >= 0 else 0
            visited = 0
            while visited < len(names) and spent < budget:
                rel = names[(position + visited) % len(names)]
                visited += 1
                if rel in checked:
                    continue
                cost = _verify_quota_entry(root, root_fd, rel, table[rel], table, root_vouched)
                if cost is None:
                    return False
                spent += cost
            cursor['verify_pos'] = (position + visited) % len(names)
            return True
        finally:
            os.close(root_fd)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        return False


# LLM: 孤儿预留、dirty、修订或root不符均拒绝。有逐目录表就做有界核验，对账进行中也一样（已走完目录用新行、其余用旧行）；
#   无表（超容、旧格式或首轮扫描）时以受控写代次为新鲜性证明，准入外写入由下一轮对账测出并纠正。
#   reservation只供持同一owner锁的本准入排除自己中间结算留下的0字节标记；冷进程不传，见任何预留都拒绝。
# 函数用途: 判断旧账是否仍可安全地用于当前增长准入。
def _verified_quota_baseline(root, ledger, touched=(), reservation=None):
    cursor = ledger.get('reconcile_cursor')
    if (ledger.get('dirty_since') is not None or (ledger.get('reservations') or {}).keys() - {reservation}
            or not isinstance(cursor, dict) or cursor.get('complete') not in (True, False)
            or cursor.get('metadata_invalidated') is True
            or cursor.get('ledger_revision') != ledger['revision']):
        return False
    try:
        info = root.stat(follow_symlinks=False)
        if (info.st_dev, info.st_ino) != (cursor.get('root_dev'), cursor.get('root_ino')):
            return False
    except OSError:
        return False
    if _quota_cursor_table(cursor) is None:
        return True
    return _verify_quota_metadata(root, cursor, touched=touched, budget=_QUOTA_VERIFY_BUDGET_ENTRIES,
                                  root_vouched=_quota_root_vouched(root, ledger['revision']))


# LLM: 按声明路径逐层重开父目录，取当前目录戳和子项后像，把声明子项的前后贡献相减相加（含物理项与普通文件数）。
#   limit给定（对账中的扫描行）时只调整字典序<=limit的子项且保留原目录戳；create为False时不为无行目录补行。
#   返回每个受影响目录的新行、目录戳是否与旧行一致、目录项是否有增删；目录换身份或计数为负即抛错，由调用方关闭式处理。
# 函数用途: 计算一次声明变更对逐目录摘要的增量。
def _quota_declared_rows(root, dirs, before_children, *, limit=None, create=True):
    grouped = {}
    for (parent_rel, name), (before, is_dir) in before_children.items():
        grouped.setdefault(parent_rel, []).append((name, before, is_dir))
    changed = {}
    for rel, children in grouped.items():
        parts = tuple(rel.split('/')) if rel else ()
        row = list(dirs[rel]) if rel in dirs else None
        if row is None and not create or parts and parts[0] in _QUOTA_HOST_TREES:
            continue
        descriptor = open_directory_beneath(root, parts, require_dir_fd=True)
        try:
            info = os.fstat(descriptor)
            stamp = [info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns]
            if row is None:
                row, vouched = [*stamp, 0, 0, 0, 0, 0, 0], False
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
                if limit is not None and '/'.join((*parts, name)) + ('/' if is_dir else '') > limit:
                    continue
                row[7] = max(0, row[7] + (after is not None) - (before is not None))
                for sign, contribution in ((-1, before), (1, after)):
                    if contribution is not None:
                        row[4] += sign * contribution[0]
                        row[5] += sign * contribution[1]
                        row[6] = (row[6] + sign * contribution[2]) & _QUOTA_HASH_MASK
                        row[8] += sign * contribution[3]
                        row[9] += sign * contribution[4]
                if min(row[4], row[5], row[8], row[9]) < 0:
                    raise ValueError('quota directory summary underflow')
            if limit is None:
                row[:4] = stamp
            changed[rel] = (row, vouched, dentries)
        finally:
            os.close(descriptor)
    return changed


# LLM: 结算时逐个受影响目录：目录项未变（目录戳一致，root以本进程写账后戳为准）直接采纳；变了且不大按期望摘要重扫
#   （物理项超过额度即停，转大目录核对）；大目录按名字计数核计费项数，不逐项lstat。
# 函数用途: 返回声明变更后核对未通过的目录集合。
def _quota_mismatched_rows(root, changed, table, root_vouched):
    merged = {**table, **{rel: row for rel, (row, _vouched, _dentries) in changed.items()}}
    failed = set()
    for rel, (row, vouched, _dentries) in changed.items():
        if root_vouched if not rel else vouched:
            continue
        if _quota_row_entries(row) <= _QUOTA_VERIFY_BUDGET_ENTRIES:
            result = _verify_quota_dir(root, rel, row, merged, expected=row[4:7], cap=_QUOTA_VERIFY_BUDGET_ENTRIES)
            if result is not None:
                if not result:
                    failed.add(rel)
                continue
        if _quota_large_dir_matches(root, rel, row) is False:
            failed.add(rel)
    return failed


# LLM: 新行超出目录表字节预算时整表作废，回到受控写代次；不截断成半张表冒充完整。
# 函数用途: 合并声明变更产生的目录行并维持目录表容量上限。
def _merge_quota_rows(cursor, dirs, changed):
    added = [rel for rel in changed if rel not in dirs]
    dirs.update({rel: row for rel, (row, _vouched, _dentries) in changed.items()})
    if added and _quota_metadata_bytes(cursor) + sum(_quota_row_bytes(rel) for rel in added) > _QUOTA_MAX_METADATA_BYTES:
        _drop_quota_metadata(cursor)
    cursor.pop('metadata_bytes', None)


# LLM: 准入已核过路径上的目录；结算只把声明子项的前后贡献相减相加，目录项在事务中有增删时再按期望核对一次，
#   事务内未声明的增删当场发现。无逐目录表（超容、旧格式）时只结算字节，由受控写代次维持可信。
# 函数用途: 以事务前像和真实落盘后像增量维护可信目录摘要。
def _refresh_quota_baseline(root, cursor, before_children, *, root_vouched):
    table = _quota_cursor_table(cursor)
    if table is None:
        return True
    try:
        changed = _quota_declared_rows(root, table, before_children)
        if _quota_mismatched_rows(root, changed, table, root_vouched):
            return False
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        return False
    _merge_quota_rows(cursor, cursor['dir_stamps'], changed)
    return True


# LLM: 对账进行中时受控写不让本轮重来：字典序已走过的路径直接补字节与inode差额，增删过目录项的目录作废名称窗口与排序准备。
#   完整行（本轮已走完的新行与尚未走到的旧行）按声明前后贡献全额补差并像完整基线一样核对；核对不通过的目录把目录时间清零，
#   让发布前的漂移清点必然重扫补正，并返回False由调用方置脏。扫描中的目录行只补已走过的子项，保留首次打开时的目录戳。
#   没有旧基线时只给已走过的新建目录补行；正在扫描的目录不另建完整行，由扫描行走完后接替。
#   枚举已走完、正在清点漂移时所有路径都按已走过处理，不再有后续枚举替它计入。
# 函数用途: 把一次已结算的声明变更并入进行中的对账游标。
def _apply_quota_pass_changes(root, cursor, paths, before_children, *, root_vouched):
    last = cursor.get('last_relpath')
    if not isinstance(last, str):
        return True
    settling = cursor.get('settling') is True
    for path, (before, after, before_inode, after_inode) in paths.items():
        if settling or '/'.join(path.relative_to(root).parts) <= last:
            cursor['partial_used_bytes'] = max(0, int(cursor.get('partial_used_bytes', 0)) + after - before)
            cursor['partial_inode_count'] = max(0, int(cursor.get('partial_inode_count', 0)) + after_inode - before_inode)
    tracked = (cursor.get('metadata_complete') is True and cursor.get('row_format') == _QUOTA_ROW_SCHEMA_VERSION
               and all(isinstance(cursor.get(key, {}), dict) for key in ('dir_stamps', 'base_rows', 'scan_rows')))
    fresh = cursor.setdefault('dir_stamps', {}) if tracked else {}
    base = cursor.setdefault('base_rows', {}) if tracked else {}
    scanning = cursor.setdefault('scan_rows', {}) if tracked else {}
    table = {**base, **fresh}
    try:
        changed = _quota_declared_rows(root, table, before_children)
        partial = _quota_declared_rows(root, scanning, before_children, limit=last, create=False)
        rows = {rel: value for rel, value in changed.items()
                if tracked and (rel in table or rel not in scanning
                                and ('' in table or rel and (settling or rel + '/' <= last)))}
        failed = _quota_mismatched_rows(root, rows, table, root_vouched)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        _drop_quota_metadata(cursor, invalidated=True)
        cursor.update(dir_batch={}, prep=None)
        return False
    batches = cursor.get('dir_batch') if isinstance(cursor.get('dir_batch'), dict) else {}
    prep = cursor.get('prep')
    for rel, (_row, _vouched, dentries) in changed.items():
        if dentries:
            batches.pop(rel, None)
            if isinstance(prep, dict) and '/'.join(prep.get('parts') or []) == rel:
                cursor['prep'] = None
    if tracked:
        for rel, (row, _vouched, _dentries) in rows.items():
            if rel in failed:
                row[2] = row[3] = 0
            if rel in fresh or rel not in base and rel and (settling or rel + '/' <= last):
                fresh[rel] = row
            else:
                base[rel] = row
        scanning.update({rel: row for rel, (row, _vouched, _dentries) in partial.items()})
        cursor.pop('metadata_bytes', None)
        if _quota_metadata_bytes(cursor) > _QUOTA_MAX_METADATA_BYTES:
            _drop_quota_metadata(cursor)
        cursor.pop('metadata_bytes', None)
    return not failed and not cursor.get('metadata_invalidated')


# LLM: FD逐层nofollow，不以路径别名扫描外部；每条目录项都消耗预算，深度超过64关闭式拒绝；排除项不计也不进入。
#   宿主状态随宿主生命周期增删，扫描中途消失只少计，不作废目录表。
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
                    _capture_quota_metadata(state, parts, None, entry.name)
                    yield (0, 0)
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    if state is not None and not _quota_host_managed(parts, entry.name):
                        _drop_quota_metadata(state, invalidated=True)
                    yield (0, 0)
                    continue
                _capture_quota_metadata(state, parts, info, entry.name)
                if _quota_excluded(parts, entry.name, info):
                    yield (0, 0)
                    continue
                yield (info.st_size, 1) if stat.S_ISREG(info.st_mode) else (0, 0)
                if stat.S_ISDIR(info.st_mode):
                    try:
                        yield from _quota_entries(root, (*parts, entry.name))
                    except FileNotFoundError:
                        if state is not None and not _quota_host_managed(parts, entry.name):
                            _drop_quota_metadata(state, invalidated=True)
                        continue
        _finish_quota_dir(state, parts)
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


# LLM: 一轮枚举走完时，本轮已计入的目录若在走过之后又有增删（目录戳与首次打开时不同），按当前内容重扫其直接子项并补正
#   总量与目录行：消失或换身份的子目录减去整棵子树行，新出现的子目录补扫。root目录戳因账本替换必变，只在本轮连续推进中
#   不能证明它只被本进程写账本改动时才重扫。上一轮留下而本轮未走到的旧行（目录已不存在）直接丢弃。
#   每轮只建一次清点生成器：首步逐行比目录戳（行数受目录表字节上限约束）；之后重扫的每个目录项、补扫的每个排序项都交还
#   一次对账片，受本片条目与时间额度约束，额度用尽即暂停到下一片续作，单片工作不随漂移规模增长。已重扫完的目录换上新戳，
#   冷恢复后不再重扫；重扫到一半遇冷恢复的目录不再从头重扫，本轮照常发布但置脏，由下一轮正常扫描补正，避免反复冷恢复时
#   永远走不完。任何意外错误放弃整张目录表，回到受控写代次，不发布半张表。
# 函数用途: 在发布完整对账前分片补正走过之后发生的准入外增删。
def _settle_quota_pass_drift(root, scan):
    state = scan['state']
    if state.get('metadata_complete') is not True:
        if state.get('metadata_invalidated'):
            scan['dirty'] = True
        return
    if state.get('scan_rows'):
        scan['dirty'] = True
        _drop_quota_metadata(state, invalidated=True)
        return
    rows = state.setdefault('dir_stamps', {})
    state.pop('base_rows', None)
    state.pop('metadata_bytes', None)
    try:
        root_fd = open_directory_beneath(root, (), require_dir_fd=True)
        try:
            changed = [(rel, row) for rel, row in sorted(rows.items())
                       if (not _quota_scan_root_clean(root, scan) if not rel
                           else not _quota_stamp_unchanged(root_fd, rel, row))]
        finally:
            os.close(root_fd)
        children = {}
        for rel in rows:
            if rel:
                children.setdefault(rel.rpartition('/')[0], []).append(rel)
        for rel, expected_row in changed:
            if rows.get(rel) is not expected_row:
                continue
            if rel == scan.get('retry_rel'):
                # 已置脏发布，下一轮从头补正，其余目录不必再重扫。
                scan['dirty'] = True
                break
            scan['settling_rel'] = rel
            yield from _rescan_quota_dir(root, rel, rows, children, scan)
            scan['settling_rel'] = None
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        scan['dirty'] = True
        _drop_quota_metadata(state, invalidated=True)
        return
    # 补扫的新子树行同样受目录表字节上限约束，超过即整表作废。
    if _quota_metadata_bytes(state) > _QUOTA_MAX_METADATA_BYTES:
        _drop_quota_metadata(state)
    state.pop('metadata_bytes', None)


# LLM: root目录戳因每片账本原子替换必变。本进程连续推进同一轮时记下每次写账本后的root戳，片首、清点与写账本前都核对
#   root只被本进程写账本改动过（新一轮首片以扫描首次打开root时的行戳为准）；任一次不符、冷恢复或续扫都按有准入外增删处理，
#   清点照常重扫root。核对与账本改名之间的窗口内的增删会并入账本写入，与准入的root戳链同一口径。
# 函数用途: 判断本轮扫描期间root目录项是否只被本进程写账本改动。
def _quota_scan_root_clean(root, scan):
    if scan.get('root_clean'):
        expected = scan.get('root_stamp')
        if expected is None:
            state = scan['state']
            row = (state.get('scan_rows') or {}).get('') or (state.get('dir_stamps') or {}).get('')
            expected = row[:4] if row else None
        try:
            info = root.stat(follow_symlinks=False)
        except OSError:
            info = None
        scan['root_clean'] = info is not None and expected == [info.st_dev, info.st_ino, info.st_mtime_ns,
                                                               info.st_ctime_ns]
    return scan.get('root_clean', False)


# LLM: 目录已不存在或换了身份时由父目录重扫处理；若轮到自身仍未由父目录消解或中途消失/换身份/子树变化，
#   则标记本轮dirty，不允许把未完成的补扫发布成干净账本。所有变化目录直接走逐项交还切片额度的重扫循环，
#   不在后台同步执行未计入切片预算的大目录名字快核；全部成功才一次性替换行并按差额补正总量。
# 函数用途: 分片重扫一个走过后有增删的目录并维护其子目录行。
def _rescan_quota_dir(root, rel, rows, children, scan):
    row = rows[rel]
    parts = tuple(rel.split('/')) if rel else ()
    try:
        descriptor = open_directory_beneath(root, parts, require_dir_fd=True)
    except (FileNotFoundError, NotADirectoryError):
        scan['dirty'] = True
        return
    try:
        info = os.fstat(descriptor)
        if [info.st_dev, info.st_ino] != row[:2]:
            scan['dirty'] = True
            return
        fresh = [info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, 0, 0, 0, 0, 0, 0]
        present = {}
        with os.scandir(descriptor) as entries:
            for entry in entries:
                yield None
                fresh[7] += 1
                if not parts and entry.name in _QUOTA_CONTROL_NAMES or _quota_host_managed(parts, entry.name):
                    continue
                child = entry.stat(follow_symlinks=False)
                count, reg_bytes, digest, files, needs_stat = _quota_child_row(parts, entry.name, child)
                if not count:
                    continue
                fresh[4] += 1
                fresh[5] += reg_bytes
                fresh[6] = (fresh[6] + digest) & _QUOTA_HASH_MASK
                fresh[8] += files
                fresh[9] += needs_stat
                if stat.S_ISDIR(child.st_mode):
                    present[f'{rel}/{entry.name}' if rel else entry.name] = [child.st_dev, child.st_ino]
    finally:
        os.close(descriptor)
    added = []
    for child, identity in present.items():
        if child not in rows or rows[child][:2] != identity:
            subtree = yield from _scan_new_quota_subtree(root, child)
            if subtree is None:
                scan['dirty'] = True
                return
            added.append(subtree)
    for child in children.get(rel, ()):
        if child in rows and present.get(child) != rows[child][:2]:
            for gone in [key for key in rows if key == child or key.startswith(child + '/')]:
                old = rows.pop(gone)
                scan['used'] -= old[5]
                scan['inodes'] -= old[8]
    for used, inodes, subtree in added:
        scan['used'] += used
        scan['inodes'] += inodes
        rows.update(subtree)
    scan['used'] = max(0, scan['used'] + fresh[5] - row[5])
    scan['inodes'] = max(0, scan['inodes'] + fresh[8] - row[8])
    rows[rel] = fresh


# LLM: 走过之后新出现的子目录沿后台同一排序枚举补扫并收集其目录行，逐项交还对账片（显式传观察容器，可跨片暂停）；
#   子树中途消失或变化返回None。
# 函数用途: 分片计算一棵对账走过后才出现的子树的字节、inode与目录行。
def _scan_new_quota_subtree(root, rel):
    state, used, inodes = {}, 0, 0
    iterator = _sorted_quota_entries(root, tuple(rel.split('/')), state=state)
    try:
        for entry in iterator:
            yield None
            if entry is not None:
                used += entry[1]
                inodes += entry[2]
    except (FileNotFoundError, NotADirectoryError):
        return None
    finally:
        iterator.close()
    if state.get('metadata_complete') is not True or state.get('scan_rows'):
        return None
    return used, inodes, state.get('dir_stamps') or {}


# LLM: 后台账仍是观察而非准入证明；最多16个LRU扫描，每个暂停扫描至多持一个目录流及绑定FD，重启沿路径合计恢复。
#   受控写由结算并入持久游标，进程内扫描器见到新revision即改从持久游标续扫；root身份变化、游标闲置超期或本轮开始后
#   出现未结算的新预留（准入后崩溃的孤儿）即从头重扫，异常不得发布complete或清孤儿预留。
#   同revision的热续扫只核epoch、root身份与闲置期限，不再逐行校验本进程上一片刚写下的目录表。
#   新一轮以上一份可信目录表为滚动基线，走完前未走到的目录仍可被准入核验；枚举走完时先分片清点走过之后的漂移再发布。
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
        # 本片对账本只整键替换顶层字段、不原地改嵌套值：浅拷贝即锁内可靠前像（原预留、计数与游标），
        #   供最终checkpoint失败且恢复读也失败时保守落盘，不必每片深拷贝整张目录表。
        preimage = dict(ledger)
        key = (os.getpid(), str(root))
        scan = _QUOTA_SCANS.get(key)
        previous = ledger.get('reconcile_cursor')
        if scan is not None and not (
                scan['revision'] == ledger['revision'] and isinstance(previous, dict)
                and previous.get('complete') is False and previous.get('epoch') == scan['epoch']
                and _quota_cursor_active(previous) and _quota_root_matches(root, previous)):
            scan['iterator'].close()
            del _QUOTA_SCANS[key]
            scan = None
        replay_skip = 0
        if scan is None:
            cursor = _resumable_quota_cursor(root, ledger) or {}
            prep = cursor.get('prep') if isinstance(cursor.get('prep'), dict) else None
            replay_skip = int(prep.get('block_skip', 0)) if prep is not None else 0
            state = {'prep': deepcopy(prep), **(_quota_metadata_payload(cursor) if cursor
                                                else _quota_pass_baseline(ledger))}
            state['dir_batches'] = deepcopy(cursor.get('dir_batch', {}))
            scan = {'revision': ledger['revision'], 'last_relpath': cursor.get('last_relpath', ''),
                    'orphans': cursor.get('orphans', sorted(ledger['reservations'])),
                    'used': cursor.get('partial_used_bytes', 0), 'inodes': cursor.get('partial_inode_count', 0),
                    'scanned': cursor.get('scanned', 0), 'epoch': cursor.get('epoch', uuid.uuid4().hex),
                    'started_at': cursor.get('started_at', time.time()), 'state': state,
                    'settling': cursor.get('settling') is True, 'root_clean': not cursor, 'root_stamp': None,
                    'retry_rel': cursor.get('settling_rel'), 'dirty': cursor.get('metadata_invalidated') is True}
            # 枚举已走完、正在清点漂移的一轮直接续清点，不再重放已走完的枚举。
            scan['iterator'] = (_settle_quota_pass_drift(root, scan) if scan['settling'] else
                                _sorted_quota_entries(root, after=scan['last_relpath'], state=state))
            _QUOTA_SCANS[key] = scan
        else:
            if isinstance(previous, dict) and previous.get('metadata_complete') is False:
                _drop_quota_metadata(scan['state'], invalidated=bool(previous.get('metadata_invalidated')))
                if previous.get('metadata_invalidated'):
                    scan['dirty'] = True
            _quota_scan_root_clean(root, scan)
        _QUOTA_SCANS.move_to_end(key)
        while len(_QUOTA_SCANS) > _QUOTA_SCAN_CACHE_SIZE:
            _, expired = _QUOTA_SCANS.popitem(last=False)
            expired['iterator'].close()
        complete, scanned, enumerated, settled = False, 0, 0, 0
        enumeration_budget = max(1024, max_entries * 4)
        # 重放也计入枚举；旧前缀吃满普通预算时，额外只给该持久前缀一次重放额度。
        replay_allowance = replay_skip if replay_skip >= enumeration_budget else 0
        enumeration_budget += replay_allowance
        deadline = time.monotonic() + max_ms / 1000
        try:
            while scanned < max_entries and enumerated + settled < enumeration_budget:
                # 已计费前缀重放完并推进至少一个新位置后才检查时间；总枚举额度不变。
                if (scanned or settled or enumerated > replay_skip) and time.monotonic() >= deadline:
                    break
                try:
                    entry = next(scan['iterator'])
                except StopIteration:
                    if scan['settling']:
                        complete = True
                        break
                    # 枚举走完后每轮只建一次漂移清点，清点逐项交还本片额度，余下的下一片续作。
                    scan['settling'] = True
                    scan['iterator'] = _settle_quota_pass_drift(root, scan)
                    continue
                if entry is None and scan['settling']:
                    settled += 1
                    continue
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
            identity = root.stat(follow_symlinks=False)
            cursor_payload = {
                'epoch': scan['epoch'], 'scanned': scan['scanned'], 'complete': complete,
                'last_relpath': scan['last_relpath'], 'partial_used_bytes': scan['used'],
                'partial_inode_count': scan['inodes'], 'started_at': scan['started_at'], 'updated_at': time.time(),
                'orphans': scan['orphans'],
                'ledger_revision': ledger['revision'], 'root_dev': identity.st_dev, 'root_ino': identity.st_ino}
            if scan['settling'] and not complete:
                cursor_payload.update(settling=True, settling_rel=scan.get('settling_rel'))
            cursor_payload.update(_quota_metadata_payload(scan['state']))
            # 准入核验的轮转位置跨片保留，免得每片都从表头重核同一批目录。
            if isinstance(previous, dict) and type(previous.get('verify_pos')) is int:
                cursor_payload['verify_pos'] = previous['verify_pos']
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
            if scan['state'].get('metadata_invalidated'):
                scan['dirty'] = True
            if complete:
                # 清点放弃过的目录未补正或枚举观察失效，总量照常发布但保持脏，由下一轮补正后才恢复增长。
                ledger.update(used_bytes=scan['used'], inode_count=scan['inodes'],
                              dirty_since=time.time() if scan.get('dirty') else None, reservations={}, reserved_bytes=0)
                scan['iterator'].close()
                _QUOTA_SCANS.pop(key, None)
            else:
                if scan.get('dirty'):
                    ledger['dirty_since'] = ledger.get('dirty_since') or time.time()
                # 账本替换会掩盖本片内root的其它改动，必须在写账本前核对。
                _quota_scan_root_clean(root, scan)
            ledger['reconcile_cursor'] = cursor_payload
        except BaseException:
            scan['iterator'].close()
            _QUOTA_SCANS.pop(key, None)
            if scan['state'].get('metadata_invalidated') or scan.get('dirty'):
                ledger['dirty_since'] = ledger.get('dirty_since') or time.time()
                if isinstance(ledger.get('reconcile_cursor'), dict):
                    _drop_quota_metadata(ledger['reconcile_cursor'], invalidated=True)
                else:
                    ledger['reconcile_cursor'] = {'complete': False, 'metadata_complete': False, 'metadata_invalidated': True}
                try:
                    _write_quota_ledger(root, ledger)
                except BaseException:
                    _write_quota_ledger(root, ledger)
            raise
        ledger_written = False
        try:
            # 未完成的一轮不改变账本可信度：干净账继续按受控写代次准入，脏账保持脏直到本轮完成。
            _write_quota_ledger(root, ledger)
            ledger_written = True
            if not complete and scan['root_clean']:
                info = root.stat(follow_symlinks=False)
                scan['root_stamp'] = [info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns]
        except BaseException:
            scan['iterator'].close()
            _QUOTA_SCANS.pop(key, None)
            if not ledger_written and (scan['state'].get('metadata_invalidated') or scan.get('dirty')):
                # checkpoint报错不证明未替换：先读回盘上实际账本标脏；读回也失败时回退锁内前像（保留原预留），失效不随异常丢失。
                try:
                    fallback = _read_quota_ledger(root)
                except BaseException:
                    fallback = preimage
                fallback['dirty_since'] = fallback.get('dirty_since') or time.time()
                if isinstance(fallback.get('reconcile_cursor'), dict):
                    _drop_quota_metadata(fallback['reconcile_cursor'], invalidated=True)
                else:
                    fallback['reconcile_cursor'] = {'complete': False, 'metadata_complete': False, 'metadata_invalidated': True}
                try:
                    _write_quota_ledger(root, fallback)
                except BaseException:
                    _write_quota_ledger(root, fallback)
            raise
        return {'complete': complete, 'scanned_entries': scanned, 'enumerated_entries': enumerated,
                'settled_entries': settled, 'replay_allowance': replay_allowance,
                'used_bytes': ledger['used_bytes'],
                'inode_count': ledger['inode_count'], 'cursor': ledger['reconcile_cursor']}


# LLM: 存活期按最后一次推进计：持续推进的长轮不会因总耗时超过期限被重置，只有闲置超期的游标才作废；旧游标无updated_at时按开始时间。
# 函数用途: 判断进行中的对账游标距上次推进是否仍在存活期内。
def _quota_cursor_active(cursor):
    updated = cursor.get('updated_at', cursor.get('started_at'))
    return (type(updated) in (float, int) and math.isfinite(updated)
            and 0 <= time.time() - updated <= _QUOTA_SCAN_CURSOR_TTL_SECONDS)


# 函数用途: 判断游标记录的root身份与当前root一致。
def _quota_root_matches(root, cursor):
    try:
        info = root.stat(follow_symlinks=False)
    except OSError:
        return False
    return (info.st_dev, info.st_ino) == (cursor.get('root_dev'), cursor.get('root_ino'))


# LLM: 只有干净、同步、本行格式且含root行的上一份目录表才能作为新一轮的滚动基线；否则本轮不带基线，走完前按受控写代次维持。
# 函数用途: 为新一轮对账复制上一份可信目录表作为旧行基线。
def _quota_pass_baseline(ledger):
    cursor = ledger.get('reconcile_cursor')
    if ledger.get('dirty_since') is not None or not isinstance(cursor, dict) \
            or cursor.get('ledger_revision') != ledger['revision']:
        return {}
    table = _quota_cursor_table(cursor)
    if not table or '' not in table or not _valid_quota_table(table):
        return {}
    state = {'metadata_complete': True, 'base_rows': {rel: list(row) for rel, row in table.items()}}
    return state if _quota_metadata_bytes(state) <= _QUOTA_MAX_METADATA_BYTES else {}


# LLM: 登记本进程曾以正限额构造过配额准入器的owner根身份，供无quota.json或测试夹具识别有限配额。
# 函数用途: 记住当前owner根目录启用过有限配额准入。
def _remember_quota_finite_root(root: Path) -> None:
    try:
        info = root.stat(follow_symlinks=False)
    except OSError:
        return
    with _QUOTA_ROOT_STAMPS_LOCK:
        _QUOTA_FINITE_ROOTS[root] = (info.st_dev, info.st_ino)
        _QUOTA_FINITE_ROOTS.move_to_end(root)
        while len(_QUOTA_FINITE_ROOTS) > _QUOTA_ROOT_STAMP_CACHE_SIZE:
            _QUOTA_FINITE_ROOTS.popitem(last=False)


# LLM: quota.json的max_disk_mb>0或本进程对该root身份建过有限配额准入器时视为配额开启；关闭时不触发每拍追赶。
# 函数用途: 判断owner是否启用了有限磁盘配额。
def owner_quota_enabled(owner_root) -> bool:
    root = Path(owner_root).expanduser().resolve(strict=False)
    raw = read_json_object_report(root / "quota.json", context="owner_policy.quota").payload.get("max_disk_mb")
    if _parse_quota_disk_mb(raw) > 0:
        return True
    try:
        info = root.stat(follow_symlinks=False)
    except OSError:
        return False
    with _QUOTA_ROOT_STAMPS_LOCK:
        return _QUOTA_FINITE_ROOTS.get(root) == (info.st_dev, info.st_ino)


# LLM: 只读账本，不取锁；缺账、坏账、脏账或对账一轮尚未走完都算待对账，供维护在追赶期放大单片额度。
#   require_ledger时只认可已开启有限配额且可读的已有账：未开启配额、缺账与坏账都不触发每拍维护续作。
# 函数用途: 判断owner配额账是否仍需追赶对账。
def owner_quota_reconcile_pending(owner_root, *, require_ledger=False):
    root = Path(owner_root).expanduser().resolve(strict=False)
    if require_ledger and (not os.path.lexists(root / _QUOTA_LEDGER) or not owner_quota_enabled(root)):
        return False
    try:
        ledger = _read_quota_ledger(root)
    except (OSError, RuntimeError, ValueError):
        return not require_ledger
    cursor = ledger.get('reconcile_cursor')
    return ledger.get('dirty_since') is not None or not isinstance(cursor, dict) or cursor.get('complete') is not True


# LLM: 宿主在准入外写完状态文件后调用。只在非阻塞配额锁下、对干净同步的完整目录表做一次有界核验（先核给定目录，
#   余下额度照常轮转），全部通过才写回采纳的新目录戳，让后续准入在大目录上仍走O(1)目录戳比较。无账时不建锁文件；
#   锁忙、脏账、无表或核验不通过都不改账，留给准入核验与对账。
# 函数用途: 宿主写完状态文件后刷新所在目录的配额目录行，返回是否写回。
def refresh_owner_quota_host_rows(owner_root, rels):
    root = Path(owner_root).expanduser().resolve(strict=False)
    if not os.path.lexists(root / _QUOTA_LEDGER):
        return False
    try:
        with _locked_owner_quota(root / _QUOTA_LOCK_BASENAME, blocking=False):
            ledger = _read_quota_ledger(root)
            cursor = ledger.get('reconcile_cursor')
            if (ledger.get('dirty_since') is not None or ledger.get('reservations')
                    or not isinstance(cursor, dict) or cursor.get('complete') is not True
                    or cursor.get('ledger_revision') != ledger['revision'] or not _quota_cursor_table(cursor)):
                return False
            if not _verify_quota_metadata(root, cursor, touched=tuple(rels), budget=_QUOTA_VERIFY_BUDGET_ENTRIES,
                                          root_vouched=_quota_root_vouched(root, ledger['revision'])):
                return False
            _write_quota_ledger(root, ledger)
            return True
    except (OwnerQuotaUnavailable, OSError):
        return False


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


# LLM: 只恢复闲置未超期、同root身份及同写revision的完整游标；旧格式缺部分统计须重扫，不猜累计值。
#   本轮开始后新出现的预留只能是准入后崩溃的孤儿，其落盘差额未并入游标，必须从头重扫。
#   目录表必须是本行格式：旧格式行不能接着累加本格式摘要；正在扫描的目录行须恰好覆盖续点所在路径上的各级目录，
#   枚举已走完、正在清点漂移的游标则不得留有扫描中的目录行。
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
            or not _quota_cursor_active(cursor)
            or cursor['ledger_revision'] != ledger['revision']):
        return None
    if cursor.get('metadata_complete') not in (True, False):
        return None
    if 'metadata_invalidated' in cursor and (
            not isinstance(cursor['metadata_invalidated'], bool)
            or cursor['metadata_invalidated'] and cursor.get('metadata_complete') is not False):
        return None
    if cursor.get('metadata_complete') is True:
        tables = [cursor.get(name, {}) for name in ('dir_stamps', 'base_rows', 'scan_rows')]
        if (cursor.get('row_format') != _QUOTA_ROW_SCHEMA_VERSION
                or not all(_valid_quota_table(table) for table in tables)
                or _quota_metadata_bytes({'dir_stamps': tables[0], 'base_rows': tables[1], 'scan_rows': tables[2]})
                > _QUOTA_MAX_METADATA_BYTES):
            return None
        components = last.rstrip('/').split('/') if last else []
        # 宿主子树内的目录不建扫描行，续点落在其中时只要求root扫描行。
        depth = 1 if components and components[0] in _QUOTA_HOST_TREES else len(components)
        open_dirs = {'/'.join(components[:index]) for index in range(depth)}
        if cursor.get('settling') is True:
            pending = cursor.get('settling_rel')
            if tables[2] or pending is not None and not (
                    isinstance(pending, str) and _valid_quota_parts(pending.split('/') if pending else [])):
                return None
        elif not open_dirs <= set(tables[2]) or any(rel and last != rel and not last.startswith(rel + '/')
                                                    for rel in tables[2]):
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
    return cursor if _quota_root_matches(root, cursor) else None


# LLM: 只读POSIX目录流的公开dirent64布局，不访问CPython对象内存；未消费缓冲可持久化，避免冷恢复重放。
# 类用途: 在64位Linux/macOS上提供等同scandir的惰性名称枚举及准确逐项偏移。
class _QuotaDirectoryStream:
    def __init__(self, descriptor):
        self.descriptor = descriptor
        self.pending = b''
        self.operation = _quota_dirent_reader()
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
                self.pending = ctypes.string_at(self.buffer, count)
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


# LLM: 每次新建CDLL都会重建函数指针类，成本与读一个小目录相当；进程内只绑定一次libc目录读取入口。
# 函数用途: 返回已设好参数类型的getdirentries/getdents64函数。
def _quota_dirent_reader():
    if not _QUOTA_DIRENT_READER:
        operation = getattr(ctypes.CDLL(None, use_errno=True),
                            '__getdirentries64' if sys.platform == 'darwin' else 'getdents64')
        argument_types = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
        if sys.platform == 'darwin':
            argument_types.append(ctypes.POINTER(ctypes.c_longlong))
        operation.argtypes = argument_types
        operation.restype = ctypes.c_ssize_t
        _QUOTA_DIRENT_READER.append(operation)
    return _QUOTA_DIRENT_READER[0]


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
#   调用方可借入刚从父目录FD打开、尚未读过的本目录FD，省去从root逐层重开；借入的FD由调用方关闭。
# 函数用途: 可暂停且可跨缓存淘汰有界恢复地准备排序候选，避免大目录在切片重启后原地踏步或超预算。
def _quota_sorted_names(root, parts, after, state=None, descriptor=None):
    import bisect

    owned = descriptor is None
    if owned:
        descriptor = open_directory_beneath(root, parts, require_dir_fd=True)
    else:
        _safe_dir_lseek(descriptor, 0, os.SEEK_SET)
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
        if owned:
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
#   同一窗口只打开目录一次再逐名lstat；进入子目录前关闭本层FD，子目录打开一次即供其排序准备与首个窗口共用，暂停时至多持一个目录FD。
#   目录走完才把扫描行并入完整表。宿主状态中途消失只少计，不作废目录表。
# 函数用途: 为后台配额生成确定性续扫项，同时保持前台原有小目录扫描算法不变。
def _sorted_quota_entries(root, parts=(), *, after='', state=None, descriptor=None):
    try:
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
            descended = False
            if info is not None and stat.S_ISDIR(info.st_mode) and not _quota_excluded(parts, sub_name, info):
                try:
                    yield from _sorted_quota_entries(root, (*parts, sub_name), after=after, state=state)
                    descended = True
                except FileNotFoundError:
                    pass
            # 续点所在目录已消失或被替换时其扫描行不再完整，整表作废；宿主子树不建行，随宿主增删不影响目录表。
            if not descended and isinstance(state, dict) and not _quota_host_managed(parts, sub_name):
                _drop_quota_metadata(state, invalidated=True)
            last_name = sub_name + '/'
        else:
            last_name = remaining
        while True:
            names = yield from _quota_sorted_names(root, parts, last_name, state=state, descriptor=descriptor)
            if not names:
                break
            batch = state.get('dir_batches', {}).get('/'.join(parts), {}) if isinstance(state, dict) else {}
            window_overflow = batch.get('overflow', False)
            for name in names:
                relpath = '/'.join((*parts, name))
                if relpath <= after and not (name.endswith('/') and after.startswith(relpath)):
                    continue
                if descriptor is None:
                    descriptor = open_directory_beneath(root, parts, require_dir_fd=True)
                    _capture_quota_metadata(state, parts, os.fstat(descriptor))
                try:
                    info = os.stat(name.rstrip('/'), dir_fd=descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    if isinstance(state, dict) and not _quota_host_managed(parts, name.rstrip('/')):
                        _drop_quota_metadata(state, invalidated=True)
                    if relpath > after:
                        yield relpath, 0, 0
                    continue
                excluded = _quota_excluded(parts, name.rstrip('/'), info)
                if relpath > after:
                    _capture_quota_metadata(state, parts, info, name.rstrip('/'))
                    regular = stat.S_ISREG(info.st_mode) and not excluded
                    yield relpath, max(0, info.st_size) if regular else 0, int(regular)
                if stat.S_ISDIR(info.st_mode) and not excluded:
                    os.close(descriptor)
                    descriptor = None
                    child_parts = (*parts, name.rstrip('/'))
                    try:
                        yield from _sorted_quota_entries(
                            root, child_parts, after=after, state=state,
                            descriptor=open_directory_beneath(root, child_parts, require_dir_fd=True))
                    except FileNotFoundError:
                        if isinstance(state, dict) and not _quota_host_managed(parts, child_parts[-1]):
                            _drop_quota_metadata(state, invalidated=True)
                        continue
            # 字节上限或持久化裁剪可产生不足128项的窗口；短窗口不等于整个目录已读完。
            if len(names) < _QUOTA_SORT_BATCH_ENTRIES and not window_overflow:
                break
            last_name = names[-1]
        _finish_quota_dir(state, parts)
    finally:
        if descriptor is not None:
            os.close(descriptor)


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
