# LLM: 策展时间判断复用consumer纯判据；C调用显式可信配置，mtime仍仅候选而非消费许可。
# LLM: owner恢复仍保pending/旧lease与真实输入/日终；待确认候选不单独成为新策展工作，修改须联测owner/curator/确认合同。
# 模块用途: 从原owner结构事实恢复后台活跃登记；内部状态类型不再转导给CLI，不替候选授执行或发布权。
# LLM: 本地普通用户与provider共享发现分页及快照，local/main仍由原base tick；不跟随本地链接或创建目录。
"""磁盘级 owner 唤醒发现:治「盯守转非阻塞后睡死叫不醒」的登记表易失性。

真机实锤(接手文档 §1):scoped owner 的到点唤醒(progress policy)/待处理唤醒信号
(wake signal)都持久化在该 owner 自己的会话存储里,但后台主代理循环只 tick「进程内
最近活跃 owner 登记表」里的 owner——登记表是易失的(网关重启清零、LRU 逐出),且只有
新入站请求才补记。长盯守的非阻塞挂起期恰恰没有新请求:网关一重启,policy 到点后
永远无人消费,盯守中途停摆(u-mix-6 现场:policy enabled、next_due_at 过期 2 万秒,
spool 攒 445 个候选没人读)。

本模块把「哪些 owner 有待消费的调度事实」改为从磁盘直接发现(纯结构化信号:
enabled 的 policy 文件存在 / wake_queue 待处理信号文件存在 / 在册未完成子代理 run /
未盯完的 watch 快照),供后台循环周期性把这些 owner 种回活跃登记表——重启/逐出后
自愈,登记表退化为热路径加速。

后两项是宿主级重启停摆的补口径(真机实锤:重启后重新派发的子代理卡 PENDING 12 分钟
不恢复、数据源游标冻死——PENDING run 不发 wake 信号,盯守 policy 又可能已被退休,
只有这两样的 owner 对旧口径完全隐形,永不入表 → 名下 supervision/续派/唤醒全部不跑)。

分页发现不再每页重扫全部 owner home:``discover_owner_home_page`` 读一份有界目录快照
(一次枚举 + 整体排序,供同一轮分页的相邻页复用),失效口径 = 目录结构签名
(mtime_ns + inode + 条目数)变化,或复用超过 ``_SNAPSHOT_TTL_SECONDS``。快照只是查询
投影,不是第二套权威状态:进程重启后缓存为空、首次调用就是一次全新枚举;游标始终是
排序键(不是下标),owner 增删只会把续页位置对齐到键边界,不会丢项或重复。

事实判定(``_owner_fact_kind``)同样按"结构化签名 + 时间边界"复用:签名覆盖 owner home
下判定读取的路径；小目录和活跃状态每轮现读，大目录稳定终态复用最多60秒，
历史消息复用最多1秒并始终检查最近32条和60秒内修改项；目录增删及链接原子替换立即失效;
时间驱动的转变(policy 与 job 到点、策展 interval/退避/日终、watch 窗口关闭)由判定时算出的
最近边界兜住,另有 ``_FACT_TTL_SECONDS`` 兜底。缓存只对"判定本身足够贵"的 owner 生效
(见 ``_FACT_MIN_CACHED_SECONDS``):空/安静 owner 的判定比签名还便宜,为它们维护签名是负收益。
缓存未命中、签名不可读或判定异常一律回退现读,判定逻辑本身没有第二份实现。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
from bisect import bisect_right
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from .conversation.models import THREAD_TASK_LINK_ACTIVE_STATUS
from .conversation.task_state import conversation_task_link_is_terminal
from .gateway_parts.io import update_json_file_atomic
from .memory_store import curator_models as _curator_models
from .runtime_db.operations import exec_lock_scope
from .runtime_db.repository import (
    _QUIESCENT_ATTEMPT_STATUSES,
    AGENT_RUN_TERMINAL_STATUSES,
    RuntimeRepository,
    _quiescent_nonterminal_agent_runs,
)
from .runtime_db.schema import runtime_db_path
from .runtime_db.user_interrupt import user_interrupt_resume_until
from .user_space.run_workspace import FinishRunWorkspaceRequest, finish_run_workspace

_LOGGER = logging.getLogger(__name__)

# owner home 内会话存储根(conversations 目录)的已知形态。网关按 workspace root 跑时
# 是 workspace/runtime/workspaces/<slug>/conversations;直连/历史部署可能落在
# conversations 或 data/conversations。全部是固定深度的 glob,不做递归扫描。
_STORE_ROOT_PATTERNS = (
    "workspace/runtime/workspaces/*/conversations",
    "conversations",
    "data/conversations",
)
_PROVIDER_BUCKET_KINDS = {"users": "user", "groups": "group"}
OwnerWakeCursor = tuple[str, str, str]

# 分页目录快照:一次「枚举 + 整体排序」的结果投影,供同一轮分页的相邻页复用。
# 两个复用上界都必须成立才算命中——结构签名一致(TTL 内)+ 未超过 TTL。快照不是权威
# 状态:进程重启后缓存为空,首次调用与「无缓存」逐字一致;条目数上界防止路径种类
# (多租户/多测试目录)把缓存撑大。
_SNAPSHOT_TTL_SECONDS = 30.0
# wake 快照最多保留的条目数；快照只用于本轮判定，多了无意义。
_SNAPSHOT_MAX_ENTRY_COUNT = 4
_OWNER_HOME_SNAPSHOTS: OrderedDict[str, _OwnerHomeSnapshot] = OrderedDict()
_SNAPSHOT_LOCK = threading.Lock()

# 事实判定缓存:同一 owner home 的硬/软事实结论,绑定"相关文件的结构化签名"(mtime_ns/尺寸/
# inode/条目名) + 时间边界。真机实锤:一页 19 owner 约 68.8ms,其中绝大部分花在
# _owner_fact_kind 逐盘读 policy/wake/curator/子代理/账本;而相邻 tick 之间这些文件通常
# 一个字节都没变。
#
# 复用要求两个上界同时成立:
#   ① 结构签名一致(owner home 下判定读到的每个文件/目录的 mtime_ns+size+inode+名字集合)
#      → 小目录/活跃文件的新增、删除和原地改写立即失效；大目录历史项由下面的
#        _DirectoryProjection 有界复核，目录变更与链接原子替换不等待兜底时间;
#   ② 未超过"最近一次可能因时间而改变结论的时刻"(policy 到期、job 到期、策展 interval/
#      退避/日终、watch 窗口关闭、执行权锁过期重探),且未超过兜底 TTL。
# 缓存只是投影:进程重启后为空;签名计算或读取异常一律回退现读,且绝不写缓存。
# 兜底 TTL:只兜"签名没覆盖到的变化"(粗粒度时间戳文件系统上的同尺寸原地改写、改钟)。
# 时间驱动的转变不靠它,而由判定时算出的最近边界收紧。取 600s 的理由:默认重扫间隔是
# gateway_loops.BACKGROUND_OWNER_WAKE_RESCAN_SECONDS=120s,TTL 若与之同量级则每两轮就现读一次、收益被抵消;
# 600s 下稳态每 5 轮重扫现读一次,同时把未建模风险的最大陈旧窗口钉死在 10 分钟内。
_FACT_TTL_SECONDS = 600.0
# 条目上界按"一页 owner 数 × 若干页"取:太小时大 owners 目录会 LRU 抖动(实测 500 owner/页
# 时 256 条会让后半页每轮重新现读)。条目本身只有摘要与时刻,1024 条约 200KB。
_FACT_MAX_ENTRY_COUNT = 1024
# 执行锁被占用时重新探测的间隔秒数；避免忙等锁释放。
_EXEC_LOCK_REPROBE_SECONDS = 30.0
# 自适应下限:现读判定比这个还快时不为它维护签名(签名要遍历目录元数据,可能比判定本身贵)。
# 判定的绝对耗时随机器负载等比变化,而签名与判定的开销比不变,所以固定阈值在这里是稳定的。
_FACT_MIN_CACHED_SECONDS = 3e-4
_OWNER_FACT_CACHE: OrderedDict[str, _OwnerFactCacheEntry] = OrderedDict()
_OWNER_FACT_LOCK = threading.Lock()
_OWNER_FACT_STATS: dict[str, int] = {
    "hit": 0,
    "miss": 0,
    "fallback": 0,
    "cheap": 0,
    "racing": 0,
    "unstable": 0,
}


@dataclass(frozen=True)
class OwnerHomeDiscoveryTarget:
    identity: Any
    home_dir: Path


@dataclass(frozen=True)
class OwnerHomeDiscoveryPage:
    targets: tuple[OwnerHomeDiscoveryTarget, ...]
    next_cursor: OwnerWakeCursor | None
    scanned: int


@dataclass(frozen=True)
class OwnerWakeDiscoveryPage:
    owners: tuple[Any, ...]
    next_cursor: OwnerWakeCursor | None
    scanned: int
    # 硬/软事实 owner 分开返回(seed 路硬先软后种入,软事实在池满时可以被等);
    # owners = hard + soft,保持旧「全部待唤醒 owner」语义。
    hard_owners: tuple[Any, ...] = ()
    soft_owners: tuple[Any, ...] = ()


@dataclass(frozen=True)
class OwnerWakeSeedPage:
    seeded: int
    next_cursor: OwnerWakeCursor | None
    scanned: int


@dataclass(frozen=True)
class _OwnerFactCacheEntry:
    """一次 _owner_fact_kind 判定的投影条目。

    LLM: ``signature_digest`` 是「相关文件结构化签名的折叠」——键来自磁盘事实(mtime_ns/
    size/inode/条目名)而不是上次结论;签名按 owner home 全量重算再折叠成定长摘要,只为让
    缓存条目有界(结构化签名本身可能上百项)。``valid_until`` 是 monotonic 时刻,同时受
    "最近一次可能因时间改变结论的时刻"与 _FACT_TTL_SECONDS 约束。命中要求两者都成立。
    类用途: 让同一 owner 在文件没变时不必每轮重读 policy/账本/子代理/策展状态。
    """

    signature_digest: str
    valid_until: float
    kind: str
    files: int


@dataclass(frozen=True)
class _OwnerHomeSnapshot:
    """providers 根目录的一次枚举投影:已排序候选 + 预计算排序键 + 失效签名。

    LLM: ``candidates`` 与 ``keys`` 必须同序同源(同一份 candidates 派生),分页的
    bisect 定位只依赖 keys;快照是不可变值对象,复用期间不得原地修改。
    类用途: 让「取第 N 页」不再重新枚举排序全部 owner home 的只读缓存条目。
    """

    signature: tuple[Any, ...]
    created_at: float
    candidates: tuple[tuple[str, str, Path], ...]
    keys: tuple[OwnerWakeCursor, ...]


def discover_wake_pending_owners(owners_dir: str | Path, *, limit: int = 64) -> list[Any]:
    """扫 owners/providers/<provider>/{users,groups}/<id> 找「有待消费调度事实」的 owner。

    事实=任一会话存储根下:enabled 的 progress policy,或 wake_queue/{urgent,normal}
    里的待处理信号文件。返回 OwnerIdentity 列表(最多 limit 个)。base(local/main)
    不在此列——它恒被后台循环 tick,无需发现。
    """
    return list(discover_wake_pending_owner_page(owners_dir, limit=limit).owners)


def discover_wake_pending_owner_page(
    owners_dir: str | Path,
    *,
    limit: int = 64,
    after_cursor: OwnerWakeCursor | None = None,
) -> OwnerWakeDiscoveryPage:
    """Return one bounded, ordered page without starving owners after the cap."""
    page = discover_owner_home_page(
        owners_dir,
        limit=limit,
        after_cursor=after_cursor,
    )
    kinds = [(_owner_fact_kind(target.home_dir), target.identity) for target in page.targets]
    hard = tuple(identity for kind, identity in kinds if kind == "hard")
    soft = tuple(identity for kind, identity in kinds if kind == "soft")
    return OwnerWakeDiscoveryPage(
        (*hard, *soft),
        page.next_cursor,
        page.scanned,
        hard_owners=hard,
        soft_owners=soft,
    )


# LLM: 本地普通用户与provider共用同一有界可失效目录快照；分页仍按原排序键继续，不创建owner。
# LLM: Pagination reads one bounded, invalidatable directory snapshot instead of
# re-enumerating and re-sorting every owner home per page. The page contract is unchanged:
# the resume position still comes from bisect_right over the same sort key on the same
# sorted candidate order, so adding or removing an owner cannot make a page repeat or skip
# an owner that is still present. Do not add pagination state here — callers own cursors.
# 函数用途: 取一页 owner home(不判事实),网关的唤醒种入/owner 维护/orphan 三条路径各自
# 带游标翻页。改动时要同步 cli/gateway_loops.py 的三个调用点与
# tests/test_owner_wake_discovery.py 的分页/失效用例。
def discover_owner_home_page(
    owners_dir: str | Path,
    *,
    limit: int = 64,
    after_cursor: OwnerWakeCursor | None = None,
) -> OwnerHomeDiscoveryPage:
    """Return one bounded canonical provider-owner page without filtering facts."""
    providers_root = Path(owners_dir) / "providers"
    if not providers_root.is_dir() and not (Path(owners_dir) / "local").is_dir():
        return OwnerHomeDiscoveryPage((), None, 0)
    snapshot = _owner_home_snapshot(providers_root)
    candidates = snapshot.candidates
    start = _candidate_start(snapshot.keys, after_cursor)
    scanned = 0
    targets: list[OwnerHomeDiscoveryTarget] = []
    page_size = max(1, limit)
    end = min(len(candidates), start + page_size)
    for index in range(start, end):
        provider, owner_kind, owner_home = candidates[index]
        scanned += 1
        targets.append(
            OwnerHomeDiscoveryTarget(
                identity=_identity(provider, owner_kind, owner_home.name),
                home_dir=owner_home,
            )
        )
    next_cursor: OwnerWakeCursor | None = None
    if end > start and end < len(candidates):
        provider, owner_kind, owner_home = candidates[end - 1]
        next_cursor = _owner_cursor(provider, owner_kind, owner_home.name)
    return OwnerHomeDiscoveryPage(tuple(targets), next_cursor, scanned)


def _owner_home_snapshot(providers_root: Path) -> _OwnerHomeSnapshot:
    """取 providers 根的有界目录快照:命中即复用,否则重新枚举并整体排序。

    LLM: 失效有两个上界,必须同时成立才复用——① 结构签名(_owner_home_signature)与当前
    磁盘一致;② 距快照生成不超过 _SNAPSHOT_TTL_SECONDS(monotonic 计时,不受系统改钟影响)。
    签名在枚举之前计算,因此「枚举期间目录又变了」只会让下次校验失效,不会存下一个比内容
    更新的签名。多线程安全:目录 I/O 在锁外做,只有缓存读写持锁。
    函数用途: 让相邻页(网关在 owner 数 > 页大小时相邻 tick 连跑)共享同一次全量枚举排序;
    缓存条目按 _SNAPSHOT_MAX_ENTRY_COUNT 淘汰,不随路径种类无限增长。
    """
    cache_key = str(providers_root)
    now = time.monotonic()
    signature = _owner_home_signature(providers_root)
    with _SNAPSHOT_LOCK:
        cached = _OWNER_HOME_SNAPSHOTS.get(cache_key)
        if (
            cached is not None
            and now - cached.created_at <= _SNAPSHOT_TTL_SECONDS
            and cached.signature == signature
        ):
            _OWNER_HOME_SNAPSHOTS.move_to_end(cache_key)
            return cached
    candidates = tuple(
        sorted(
            _candidate_owner_homes(providers_root),
            key=lambda item: _owner_cursor(item[0], item[1], item[2].name),
        )
    )
    snapshot = _OwnerHomeSnapshot(
        signature=signature,
        created_at=now,
        candidates=candidates,
        keys=tuple(_owner_cursor(provider, kind, home.name) for provider, kind, home in candidates),
    )
    with _SNAPSHOT_LOCK:
        _OWNER_HOME_SNAPSHOTS[cache_key] = snapshot
        _OWNER_HOME_SNAPSHOTS.move_to_end(cache_key)
        while len(_OWNER_HOME_SNAPSHOTS) > _SNAPSHOT_MAX_ENTRY_COUNT:
            _OWNER_HOME_SNAPSHOTS.popitem(last=False)
    return snapshot


# LLM: local目录签名也参与原快照失效，新增普通用户不得等TTL；只读元数据不作为执行授权。
# 函数用途: 对provider和本地普通用户目录计算共同的发现签名。
def _owner_home_signature(providers_root: Path) -> tuple[Any, ...]:
    """枚举所读目录的结构签名:provider 集合 + 每个 owner bucket 目录的元数据指纹。

    LLM: 与 _candidate_owner_homes 复用同一个 _provider_buckets 迭代器,保证签名覆盖枚举
    读到的每一个目录(口径漂移会漏失效,改 _candidate_owner_homes 必须同步这里)。只读目录
    元数据,不逐条目 stat,因此成本与 owner 数近似无关。
    函数用途: 判断上一轮枚举结果能否继续复用;新增/删除/替换 owner 目录都会改变签名。
    """
    entries: list[tuple[str, str, tuple[int, int, int]]] = []
    for provider_dir, owner_kind, bucket_dir in _provider_buckets(providers_root):
        entries.append((provider_dir.name, owner_kind, _bucket_signature(bucket_dir)))
    entries.append(("local", "user", _bucket_signature(providers_root.parent / "local")))
    return tuple(entries)


def _bucket_signature(bucket_dir: Path) -> tuple[int, int, int]:
    """单个 owner bucket 目录的 (mtime_ns, inode, 条目数) 指纹。

    LLM: mtime_ns 覆盖「目录内条目增删改名」,inode 覆盖「整目录被删除后重建」,条目数覆盖
    时间戳粒度粗的文件系统上同一时刻内的增删。任一维度不可读用 -1 占位:不可读状态自身也
    是可比签名,恢复可读后指纹必然变化,不会永久误命中。
    函数用途: 给 _owner_home_signature 提供单目录指纹;不做递归,只有 stat + 一次 listdir。
    """
    try:
        status = bucket_dir.stat()
    except OSError:
        return (-1, -1, -1)
    try:
        count = len(os.listdir(bucket_dir))
    except OSError:
        count = -1
    return (status.st_mtime_ns, status.st_ino, count)


# LLM: The cursor is a sort key, never an index: resuming is "first key strictly greater than
# the last emitted key" over the same ordered key space, so owner insert/delete can only shift
# the boundary, never duplicate or skip an owner that is still present. keys and candidates
# must come from the same snapshot.
# 函数用途: 在已排序键上定位续页起点;after_cursor 为 None 表示从头开始。
def _candidate_start(
    keys: tuple[OwnerWakeCursor, ...],
    after_cursor: OwnerWakeCursor | None,
) -> int:
    if after_cursor is None:
        return 0
    return bisect_right(keys, after_cursor)


def _owner_cursor(provider: str, owner_kind: str, owner_id: str) -> OwnerWakeCursor:
    return provider, owner_kind, owner_id


# LLM: 保留provider枚举原顺序，本地用户经过同一分页排序，main和链接不进入新候选。
# 函数用途: 合并两类规范owner目录作为原发现器的候选输入。
def _candidate_owner_homes(providers_root: Path):
    for provider_dir, owner_kind, bucket_dir in _provider_buckets(providers_root):
        for owner_home in _dirs_of(bucket_dir):
            yield provider_dir.name, owner_kind, owner_home
    for home in _iter_owner_user_dirs(providers_root.parent.parent):
        yield "local", "user", home


# LLM: 只枚举已物化的owners/local直接子目录；跳过main、隐藏项及任一链接，不创建账号或执行工作。
# 函数用途: 找出本地普通用户的真实目录供原后台发现链复用。
def _iter_owner_user_dirs(root_home: Path):
    local = root_home / "owners" / "local"
    if local.is_symlink() or not local.is_dir():
        return
    for home in sorted(local.iterdir()):
        if home.name != "main" and not home.name.startswith(".") and not home.is_symlink() and home.is_dir():
            yield home


def _provider_buckets(providers_root: Path):
    for provider_dir in _dirs_of(providers_root):
        for bucket, owner_kind in _PROVIDER_BUCKET_KINDS.items():
            yield provider_dir, owner_kind, provider_dir / bucket


def _dirs_of(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return [path for path in sorted(root.iterdir()) if path.is_dir()]


def seed_registry_from_disk(registry: Any, owners_dir: str | Path, *, limit: int = 64) -> int:
    """把磁盘发现的待唤醒 owner 种进活跃登记表;返回种入数。best-effort,绝不外抛。"""
    return seed_registry_page_from_disk(registry, owners_dir, limit=limit).seeded


def seed_registry_page_from_disk(
    registry: Any,
    owners_dir: str | Path,
    *,
    limit: int = 64,
    after_cursor: OwnerWakeCursor | None = None,
) -> OwnerWakeSeedPage:
    """Seed one bounded page and return its continuation cursor.

    硬事实 owner 先种(registry 满时优先逐软、软 owner 被拒时硬仍然入表),
    软事实(curator)后种——软活可以等,硬活不能饿死。"""
    try:
        page = discover_wake_pending_owner_page(
            owners_dir,
            limit=limit,
            after_cursor=after_cursor,
        )
        seeded = 0
        for owner in page.hard_owners:
            if registry.record(owner, hard=True):
                seeded += 1
        for owner in page.soft_owners:
            if registry.record(owner, hard=False):
                seeded += 1
        return OwnerWakeSeedPage(seeded, page.next_cursor, page.scanned)
    except Exception:
        _LOGGER.warning("owner wake discovery failed (owners_dir=%s)", owners_dir, exc_info=True)
        return OwnerWakeSeedPage(0, after_cursor, 0)


# 事实类别:hard = 必须被驱动(等不起),soft = 可以等(池满时被逐出/拒绝)。
# 真机实锤分层必要性:215 个 feishu 测试号的 curator 软活把 64 容量登记表/池灌满,
# 真活 user-a 的 wake 硬事实按字母序最后被 LRU 逐出 → 唤醒永不消费 → 父任务永不收口。
#
# LLM: This is the only entry point the page path uses. A cached verdict is reused only when the
# freshly recomputed structural signature matches AND the deadline derived at evaluation time has
# not passed. Every failure path (unreadable signature, cache miss, changed signature) falls back
# to the live evaluation below, which stays the single implementation of the fact rules.
#
# Caching is adaptive: the signature itself costs directory metadata walks, so for an owner whose
# evaluation is already cheap (empty/quiet home, or a fact found by the first predicate) paying for
# the signature would cost more than the judgement it replaces. Only judgements slower than
# _FACT_MIN_CACHED_SECONDS are remembered, which removes the regression for light owners while
# keeping the win for content-heavy ones.
# LLM: 只缓存同一快照内稳定判断；分页probe发现hard后，本次二次判定不得覆盖成否定并长时缓存。
# 函数用途: 判定一个 owner home 是否有待驱动的事实;贵的判定在文件没变时不再逐盘重读。
def _owner_fact_kind(owner_home: Path) -> str:
    cache_key = str(owner_home)
    cached = _cached_owner_fact_kind(cache_key)
    if cached is not None and time.monotonic() < cached.valid_until:
        try:
            digest, _files = _owner_fact_signature_digest(owner_home)
        except Exception:  # noqa: BLE001 签名不可读: 不缓存,直接现读
            _bump_fact_stat("fallback")
            return _evaluate_owner_fact_kind(owner_home, [])
        if cached.signature_digest == digest:
            _bump_fact_stat("hit")
            return cached.kind
        # 签名变了: 落到现读(下面的 miss 统计由 _store/_drop 路径统一负责)
    # 未命中分两步,顺序很关键:
    #   ① **先判定、后决定要不要签名**。判定比"为它维护签名"便宜(安静 owner)时就直接返回,
    #      一个字节的签名成本都不付——这正是 _FACT_MIN_CACHED_SECONDS 要保护的人群(实测:先取
    #      签名再判定会让这类 owner 每调用慢 2.5x)。
    #   ② 只有判定确实贵、值得缓存时,才做快照绑定: 判定前取一次签名 → 再判定一次 → 判定后
    #      再取一次签名,两次一致才允许写缓存。旧实现是"判定(旧状态)→ 签名(新状态)"直接写缓存,
    #      会留下从未同时成立的 (kind, digest) 组合,后续按摘要命中它,最长 _FACT_TTL_SECONDS
    #      看不到新 wake。不一致时用新快照**有界**重判一次(最多一次,不是无界重试),仍不一致就
    #      只返回结论、不写缓存(下次调用照旧现读)。
    kind, elapsed = _probe_owner_fact_kind(owner_home)
    if elapsed < _FACT_MIN_CACHED_SECONDS:
        _drop_owner_fact_kind(cache_key)
        _bump_fact_stat("cheap")
        return kind
    for attempt in (0, 1):
        deadlines: list[float] = []
        before = _owner_fact_signature_or_none(owner_home)
        if before is None:
            _bump_fact_stat("fallback")
            return kind
        rechecked = _evaluate_owner_fact_kind(owner_home, deadlines)
        if kind == "hard" and rechecked != "hard":
            _drop_owner_fact_kind(cache_key)
            return kind
        kind = rechecked
        try:
            digest, file_count = _owner_fact_signature_digest(owner_home)
        except Exception:  # noqa: BLE001 签名不可读: 结论照常返回,只是不缓存
            _bump_fact_stat("fallback")
            return kind
        if digest == before:
            # 时间边界用墙钟(time.time,与各谓词同域)表达,再折算成 monotonic 有效期(不受改钟影响)。
            budget = _FACT_TTL_SECONDS
            if deadlines:
                budget = min(budget, max(0.0, min(deadlines) - time.time()))
            _store_owner_fact_kind(
                cache_key,
                _OwnerFactCacheEntry(
                    signature_digest=digest,
                    valid_until=time.monotonic() + budget,
                    kind=kind,
                    files=file_count,
                ),
            )
            _bump_fact_stat("miss")
            return kind
        _drop_owner_fact_kind(cache_key)
        if attempt == 0:
            _bump_fact_stat("racing")
            continue
    # 连续两次判定都撞上改写: 只返回最后一次结论,不缓存(宁可下次现读,也不写错绑定)。
    _bump_fact_stat("unstable")
    return kind


# 函数用途: 现读判定一次并返回 (结论, 判定耗时秒);只用于判断"值不值得为它维护签名"。
def _probe_owner_fact_kind(owner_home: Path) -> tuple[str, float]:
    started = time.perf_counter()
    kind = _evaluate_owner_fact_kind(owner_home, [])
    return kind, time.perf_counter() - started


# 函数用途: 取 owner home 的结构签名摘要;读不到签名时返回 None,由调用方决定是否还能缓存。
def _owner_fact_signature_or_none(owner_home: Path) -> str | None:
    try:
        digest, _files = _owner_fact_signature_digest(owner_home)
    except Exception:  # noqa: BLE001 签名不可读: 调用方按"不能绑定快照"处理
        return None
    return digest


def _evaluate_owner_fact_kind(owner_home: Path, deadlines: list[float]) -> str:
    """现读判定(唯一实现): 硬事实优先,其次软事实,都没有就是 none。

    ``deadlines`` 收集"本次结论可能因时间而改变"的最早墙钟时刻(policy/job 到点、watch 窗口
    关闭、策展 interval/退避/日终、执行权锁重探);调用方用它决定缓存有效期,传空列表即
    "只按文件事实失效"。
    """
    if _owner_has_hard_facts(owner_home, deadline_out=deadlines):
        return "hard"
    if _owner_has_soft_facts(owner_home, deadline_out=deadlines):
        return "soft"
    return "none"


def _cached_owner_fact_kind(cache_key: str) -> _OwnerFactCacheEntry | None:
    """读事实缓存条目(只读快照;命中确认后再 move_to_end)。"""
    with _OWNER_FACT_LOCK:
        return _OWNER_FACT_CACHE.get(cache_key)


def _drop_owner_fact_kind(cache_key: str) -> None:
    """丢弃事实缓存条目(判定变便宜/签名变化/判定异常时的保守处理)。"""
    with _OWNER_FACT_LOCK:
        _OWNER_FACT_CACHE.pop(cache_key, None)


def _store_owner_fact_kind(cache_key: str, entry: _OwnerFactCacheEntry) -> None:
    """写入有界事实缓存(条目数上界,超出按 LRU 淘汰)。"""
    with _OWNER_FACT_LOCK:
        _OWNER_FACT_CACHE[cache_key] = entry
        _OWNER_FACT_CACHE.move_to_end(cache_key)
        while len(_OWNER_FACT_CACHE) > _FACT_MAX_ENTRY_COUNT:
            _OWNER_FACT_CACHE.popitem(last=False)


def _bump_fact_stat(name: str) -> None:
    """事实缓存结构化计数(命中/未命中/回退现读);缺键容忍,计数不影响主链路。"""
    with _OWNER_FACT_LOCK:
        _OWNER_FACT_STATS[name] = int(_OWNER_FACT_STATS.get(name, 0)) + 1


# LLM: agents签名采用独立有界页；大目录页变即放弃缓存，不能用无界glob换取缓存命中。
# LLM: The signature must cover exactly the paths the fact predicates read; a path that is read
# but not signed can change without invalidating the cached verdict. When a predicate starts
# reading a new path, add it here in the same change (tests assert add/remove/edit invalidation).
# Only metadata is read (no JSON parsing, no SQLite), so a signature pass stays far cheaper than
# the evaluation it guards.
# 函数用途: 计算 owner home 事实相关文件的结构化签名,供判定缓存失效比较。
def _owner_fact_signature(owner_home: Path) -> tuple[tuple[str, tuple[Any, ...]], ...]:
    from .tooling.process_session_store import process_session_store_root

    parts: list[tuple[str, tuple[Any, ...]]] = []
    parts.append(("process_completions", _dir_entries_signature(process_session_store_root(owner_home, owner_home), "bg-*.json")))
    store_roots = tuple(_store_roots(owner_home))
    parts.append(("store_roots", tuple(str(root) for root in store_roots)))
    for root in store_roots:
        parts.append((f"wake_urgent:{root}", _dir_entries_signature(root / "wake_queue" / "urgent", "*.json")))
        parts.append((f"wake_normal:{root}", _dir_entries_signature(root / "wake_queue" / "normal", "*.json")))
        parts.append((f"policies:{root}", _dir_entries_signature(root / "progress_policies", "*.json")))
        parts.append((f"messages:{root}", _dir_entries_signature(root / "messages", "*.jsonl")))
    parts.append(("agents", _subagent_page_signature(owner_home)))
    parts.append(
        (
            "task_links",
            _dir_entries_signature(
                owner_home / "workspace" / "runtime" / "workspaces",
                "*/conversations/tasks/*.json",
            ),
        )
    )
    for root_name in ("runs", "tasks"):
        parts.append(
            (f"ledger:{root_name}", _dir_entries_signature(owner_home / root_name, "*/*/work/state.json"))
        )
    parts.append(("audit", _dir_entries_signature(owner_home / "audit", "*.jsonl")))
    parts.append(("watch_state", _dir_entries_signature(owner_home / "watch_state", "*.json")))
    for relative in (
        "memory_policy.json",
        "memory/curator/state.json",
        "memory/candidates.jsonl",
        "data/scheduler/store.json",
    ):
        parts.append((relative, _file_signature(owner_home / relative)))
    # runtime.db 走 WAL:只 stat 主库会漏掉"提交只写了 -wal"的变化,三个文件一起签。
    db_path = runtime_db_path(owner_home)
    for suffix in ("", "-wal", "-shm"):
        parts.append((f"runtime_db{suffix}", _file_signature(Path(f"{db_path}{suffix}"))))
    return tuple(parts)


def _owner_fact_signature_digest(owner_home: Path) -> tuple[str, int]:
    """把结构化签名折叠成定长摘要 + 文件计数(缓存条目因此有界,键仍来自磁盘事实)。"""
    parts = _owner_fact_signature(owner_home)
    files = sum(len(payload) for _name, payload in parts)
    blob = repr(parts).encode("utf-8", "surrogatepass")
    return hashlib.blake2b(blob, digest_size=16).hexdigest(), files


def _file_signature(path: Path) -> tuple[Any, ...]:
    """单文件签名: (mtime_ns, size, inode);不存在/不可读用显式占位(本身也是可比状态)。"""
    try:
        status = path.stat()
    except OSError:
        return ("missing",)
    return (status.st_mtime_ns, status.st_size, status.st_ino)


# LLM: 小目录逐项现读；大目录签名复用有界投影，活跃项即刻检测，历史项定期复核。
# 函数用途: 为事实缓存提供目录签名，不在每轮重新遍历历史状态。
def _dir_entries_signature(root: Path, pattern: str) -> tuple[Any, ...]:
    try:
        if pattern == "*/*/work/state.json":
            return tuple(scan.signature for scan in _ledger_directory_scans(root)), _directory_fp(root)
        if pattern == "*/conversations/tasks/*.json":
            folders = _conversation_task_link_dirs(root)
            if folders is None:
                return (("scan_error",),)
            return tuple(_directory_scan(folder, "link").signature for folder in folders), _directory_fp(root)
        if "/" not in pattern:
            return _directory_scan(root, "flat", pattern).signature
        paths = sorted(root.glob(pattern))
    except OSError:
        return (("glob_error",),)
    entries = tuple((str(path), *_file_signature(path)) for path in paths)
    return (entries, _dir_own_signature(root))


def _dir_own_signature(root: Path) -> tuple[int, int]:
    """目录自身指纹 (mtime_ns, inode);不可读用 (-1, -1) 占位。"""
    try:
        status = root.stat()
    except OSError:
        return (-1, -1)
    return (status.st_mtime_ns, status.st_ino)


def _owner_has_hard_facts(owner_home: Path, *, deadline_out: list[float] | None = None) -> bool:
    from .conversation.process_events import owner_has_pending_process_completions

    if any(
        _has_pending_wake_signal(store_root)
        or _has_enabled_progress_policy(store_root, deadline_out=deadline_out)
        for store_root in _store_roots(owner_home)
    ):
        return True
    return (
        owner_has_pending_process_completions(owner_home)
        or _has_unfinished_subagent_run(owner_home)
        or _has_unfinished_task_ledger(owner_home, deadline_out=deadline_out)
        or _has_incomplete_watch_lane(owner_home, deadline_out=deadline_out)
        or _has_due_scheduler_fact(owner_home, deadline_out=deadline_out)
    )


def _owner_has_soft_facts(owner_home: Path, *, deadline_out: list[float] | None = None) -> bool:
    return _has_pending_memory_curator_work(owner_home, deadline_out=deadline_out)


# LLM: 发现层只认结构化信号,与 CuratorService.run_if_due 同源;「无法证明没活=有活」已删——
# 真机 184 个 feishu 测试号被 state 缺失/每天必挂/对话即挂三条过宽判定天天挂起,
# 占满 64 容量运行池,真活的 user-a/user-b 被 LRU 逐出饿死(探针实锤)。
# 函数用途: 让 scoped owner 在 Gateway 重启或 LRU 逐出后重新进入既有后台 lane。
def _owner_memory_enabled(owner_home: Path) -> bool:
    """owner memory_policy 总闸(唯一 authority:判定走 canonical effective_memory_enabled,
    读取走 canonical read_json_object_report,与 resolve_effective_owner_policy 同源)。

    文件缺失视为开启(老 owner 兼容)、坏 JSON 视为开启(发现层不因解析失败误杀,
    与「无法证明没活=有活」已删后的宽容语义一致)——只有显式 enabled=false 才短路。"""
    from .common.json_io import read_json_object_report
    from .user_space.owner_policy import effective_memory_enabled

    report = read_json_object_report(
        owner_home / "memory_policy.json", context="owner_wake.memory_policy"
    )
    return effective_memory_enabled(report.payload)


# LLM: 内部解析沿canonical模型，不公开转导类型；原退避、pending/旧lease和时间边界不变。
# 函数用途: 判断owner是否仍有原策展运行/输入事实，区别于候选来源资格与用户正式确认。
def _has_pending_memory_curator_work(
    owner_home: Path,
    *,
    deadline_out: list[float] | None = None,
    curator_config=None,
    timezone_name: str = "",
) -> bool:
    """策展软事实判定。

    LLM: ``deadline_out`` 可选收集"结论可能因时间改变"的最早墙钟时刻(退避到期、interval 到期、
    实际本地日终窗口),供缓存决定有效期；C显式传可信配置，默认使用原配置默认值和服务器本地时区。
    """
    if (curator_config is not None and not curator_config.enabled) or not _owner_memory_enabled(owner_home):
        return False  # 总闸关闭:curator 软活不判活(不占登记表工位,对称 skill 空快照)
    state_path = owner_home / "memory" / "curator" / "state.json"
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return False
        state = _curator_models.MemoryCuratorState.from_dict(payload)
    except FileNotFoundError:
        # 从未初始化:有新输入就该初始化(一次性低频,不构成占位源)
        return _curator_input_newer_than(owner_home, 0.0)
    except (OSError, UnicodeError, ValueError, TypeError):
        # 坏 state 不能证明有活;CuratorService 运行时会以结构化错误暴露,发现层不据此占工位
        return False
    return curator_work_is_due(owner_home,state,curator_config=curator_config,
        timezone_name=timezone_name,deadline_out=deadline_out)


# LLM: state须由原canonical reader提供，配置来自宿主；mtime仍只是候选粗筛，原service保batch/排除/turn_threshold，结果不授P3或执行。
# 函数用途: 用consumer同源clock和实际配置判断已读state的时间资格。
def curator_work_is_due(owner_home, state, *, curator_config=None, timezone_name="", deadline_out=None):
    from .memory_store.curator_due import (
        daily_finalize_due,
        interval_elapsed,
        localized_now,
        next_local_boundary,
        parse_time,
    )
    from .memory_store.curator_models import MemoryCuratorConfig, curator_failure_retry_seconds

    config = MemoryCuratorConfig() if curator_config is None else curator_config
    if not config.enabled or not _owner_memory_enabled(owner_home):
        return False
    now = time.time()
    current = localized_now(datetime.fromtimestamp(now,timezone.utc),timezone_name)
    failure = parse_time(state.last_failure_at)
    backoff = curator_failure_retry_seconds(state.last_failure_code,max(30,min(300,config.interval_seconds)))
    if state.last_failure_code and failure is not None and now-failure.timestamp() < backoff:
        _add_deadline(deadline_out,failure.timestamp()+backoff)
        return False
    if state.pending_reasons or state.active_lease:
        return True
    if daily_finalize_due(state,current,config.daily_finalize_hour):
        _add_deadline(deadline_out,next_local_boundary(current,timezone_name,0,next_day=True))
        return True
    if state.last_daily_finalize_date != current.date().isoformat():
        _add_deadline(deadline_out,next_local_boundary(current,timezone_name,config.daily_finalize_hour))
    success = parse_time(state.last_success_at)
    if success is None:
        return _curator_input_newer_than(owner_home,0.0)
    if interval_elapsed(state,current,config.interval_seconds):
        return _curator_input_newer_than(owner_home,success.timestamp())
    _add_deadline(deadline_out,success.timestamp()+config.interval_seconds)
    return False


def _add_deadline(deadline_out: list[float] | None, moment: float) -> None:
    """记录一个"结论可能因时间改变"的墙钟时刻(可选出口;None 表示调用方不需要)。"""
    if deadline_out is not None:
        deadline_out.append(float(moment))


# LLM: mtime 只用于发现“需要 tick”的 owner；真正 cursor/due 判定仍由 CuratorService 的结构化 state 完成。
# 函数用途: 检查 ConversationStore 消息或 owner audit 是否晚于最近成功策展。
def _curator_input_newer_than(owner_home: Path, timestamp: float) -> bool:
    roots = [*(root / "messages" for root in _store_roots(owner_home)), owner_home / "audit"]
    return any(_curator_directory_newer_than(root, timestamp) for root in roots)


# LLM: 只读目录；每个匹配文件只stat一次，读取异常保守判为有输入，不吞掉后续唤醒。
# 函数用途: 检查单个消息或audit目录是否存在晚于策展时间的非空输入。
def _curator_directory_newer_than(root: Path, timestamp: float) -> bool:
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if not entry.name.endswith(".jsonl"):
                    continue
                if _curator_entry_newer_than(entry, timestamp):
                    return True
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return False


# LLM: 单条消失只跳过该条，不提前结束整个目录；DirEntry缓存保证is_file不额外stat。
# 函数用途: 读取一条消息元数据并处理文件在枚举后消失的竞态。
def _curator_entry_newer_than(entry: os.DirEntry, timestamp: float) -> bool:
    try:
        st = entry.stat()
        return entry.is_file() and st.st_size > 0 and st.st_mtime > timestamp
    except FileNotFoundError:
        return False
    except OSError:
        return True


# LLM: deadline_out 收集最近一个可触发 job/rule 的有效到期时间（含 backoff_until 并跳过已有 waiting/活动 run 的规则），
#   到点时结论从 none 变成 hard；不传参时保持布尔语义。
# 函数用途: 从 owner 调度账本发现真正到期或待恢复的运行，避免 waiting 运行或退避期规则每秒空唤醒。
def _has_due_scheduler_fact(
    owner_home: Path,
    *,
    deadline_out: list[float] | None = None,
) -> bool:
    """Discover due jobs and recoverable nonterminal runs from the owner ledger."""

    path = owner_home / "data" / "scheduler" / "store.json"
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        # Keep a broken scheduler ledger visible to the owner worker so its
        # runtime health can report the failure instead of silently sleeping.
        return True
    if not isinstance(payload, dict):
        return True
    runs = payload.get("runs")
    current = time.time()
    if isinstance(runs, dict):
        for run in runs.values():
            if not isinstance(run, dict):
                continue
            status = str(run.get("status") or "")
            if status in {"queued", "claimed", "running"}:
                return True
            if status == "waiting" and _waiting_scheduler_run_needs_wake(
                owner_home, run, now=current, deadline_out=deadline_out
            ):
                return True
    from .scheduler.due_index import _earliest_due_at

    earliest = _earliest_due_at(payload, now=current)
    if 0 < earliest <= current:
        return True
    if earliest > current:
        _add_deadline(deadline_out, earliest)
    return False


# LLM: waiting运行若关联TaskLink已进入终态则立即唤醒收口；若仍为active则只投影低频复核deadline，避免每秒空转。
# 函数用途: 为冷重启或漏通知的终态waiting调度保留发现入口，同时不恢复正常等待期间的每秒唤醒。
def _waiting_scheduler_run_needs_wake(
    owner_home: Path,
    run: dict[str, object],
    *,
    now: float,
    deadline_out: list[float] | None,
) -> bool:
    run_id = str(run.get("run_id") or "").strip()
    if not run_id:
        return False
    status = _find_waiting_run_task_status(owner_home, run_id)
    if not status:
        return False
    if conversation_task_link_is_terminal(status):
        return True
    if status == "active":
        updated = _scheduler_timestamp(run.get("updated_at")) or now
        _add_deadline(deadline_out, max(updated + 60.0, now + 60.0))
    return False


# LLM: 按已知会话根顺序读取waiting关联TaskLink状态（复用_read_cached_task_status_fields缓存），不递归全盘。
# 函数用途: 兼容工作区与直连会话布局下的定时任务关联状态查询。
def _find_waiting_run_task_status(owner_home: Path, run_id: str) -> str:
    name = f"{run_id}.json"
    for rel in ("conversations/tasks", "data/conversations/tasks"):
        fields = _read_cached_task_status_fields(owner_home / rel / name)
        if fields is not None and fields[1]:
            return str(fields[1]).strip().lower()
    for folder in _conversation_task_link_dirs(owner_home / "workspace" / "runtime" / "workspaces") or ():
        fields = _read_cached_task_status_fields(folder / name)
        if fields is not None and fields[1]:
            return str(fields[1]).strip().lower()
    return ""


def _scheduler_timestamp(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


# 未完成 = 还需要被驱动:待派(PLANNING/PENDING)、在跑(RUNNING,重启后需判活回收)、
# 待解阻(BLOCKED,能力批复/续派通道)。终态与人为暂停(PAUSED)不算。
_UNFINISHED_RUN_STATUSES = frozenset({"PLANNING", "PENDING", "RUNNING", "BLOCKED"})
# PAUSED不在未完成集合中，因此暂停时不唤醒；也不是沉淀终态，必须留在live_names，
# 才能在>32条运行目录中原地恢复为RUNNING后立即发现，而不等待60秒历史复核。
_SETTLED_LEDGER_STATUSES = frozenset(
    {"DONE", "FAILED", "CANCELLED", "ABANDONED", "TIMEOUT", "CHANNEL_ERROR", "COMPLETED"}
)
# TaskRun原生终态与历史关闭别名；closed_at仍是独立关闭事实，不能据父AgentRun终态直接推定。
_TERMINAL_TASK_RUN_STATUSES = AGENT_RUN_TERMINAL_STATUSES | {"completed", "interrupted", "closed"}

# R1-03 发现层 repo 缓存：唤醒轮每 tick 调 unfinished_task_ids，每 tick 重建
# RuntimeRepository（schema 初始化开销）不可接受。按 db 文件 mtime_ns 失效——
# 外部写（settle/reclaim）后 mtime 变化，下次 tick 自动重建，读旧状态窗口 ≤ 1 tick。
_RUNTIME_REPO_CACHE: dict[str, tuple[int, RuntimeRepository]] = {}


def _runtime_repo_for_owner(owner_home: Path) -> RuntimeRepository | None:
    """owner home 对应的权威库实例（模块级缓存，按 mtime_ns 失效）。

    无 db 文件 → None（发现层 fail-open：无权威库时保持旧行为，不误杀）。
    """
    db_path = runtime_db_path(owner_home)
    try:
        mtime_ns = db_path.stat().st_mtime_ns
    except OSError:
        return None
    cached = _RUNTIME_REPO_CACHE.get(str(db_path))
    if cached is not None and cached[0] == mtime_ns:
        return cached[1]
    repo = RuntimeRepository(db_path)
    _RUNTIME_REPO_CACHE[str(db_path)] = (mtime_ns, repo)
    return repo


def _main_run_row_for_task(repo: RuntimeRepository, task_id: str):
    """task 的 role='main' root AgentRun（终态/锁过滤的权威锚点）。

    SQL 与主链接线复用同一把尺（repository.main_agent_run_for_task，
    R1-03 补漏：续跑登记与发现层过滤必须看到同一棵 run 树）。
    取最新一条；无权威记录 → None（发现层不据此裁决，照旧驱动）。
    """
    return repo.main_agent_run_for_task(task_id)


# LLM: 现行根目录权威优先，兼容data布局；不经写repo。冷WAL库用immutable禁建sidecar，DELETE库保持读锁。
# 函数用途: 为静止探针提供短时只读连接，读取期间权威变化或缺失时放弃静止证明。
@contextmanager
def _quiescent_probe_connection(owner_home: Path) -> Iterator[sqlite3.Connection | None]:
    primary = db_path = runtime_db_path(owner_home)
    if not db_path.exists() and not db_path.is_symlink():
        db_path = owner_home / "data" / "runtime.db"
    if owner_home.is_symlink() or db_path.is_symlink() or db_path.parent.is_symlink() or not db_path.is_file():
        yield None
        return
    wal, shm = Path(f"{db_path}-wal"), Path(f"{db_path}-shm")
    has_wal = wal.exists()
    if wal.is_symlink() or shm.is_symlink() or (has_wal and (not wal.is_file() or not shm.is_file())):
        yield None
        return
    paths = (primary, db_path, wal)
    before = tuple(_file_signature(path) for path in paths)
    if db_path != primary and before[0] != ("missing",):
        yield None
        return
    with db_path.open("rb") as source:
        immutable = not has_wal and source.read(20)[18:20] == b"\x02\x02"
    address = db_path.absolute().as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
    with closing(sqlite3.connect(address, uri=True, timeout=0.05)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        yield conn
        if before != tuple(_file_signature(path) for path in paths):
            raise sqlite3.DatabaseError("runtime authority changed during quiescent probe")


# LLM: 仅复用Runtime原静止判据；身份解析和全树查询在同一只读事务，不初始化schema或借用可写repo缓存。
# 函数用途: 确认BLOCKED子代理已静止且父任务已关闭或整树可结算；缺失、冲突或读错都保留唤醒。
def _blocked_subagent_is_quiescent(owner_home: Path, run_id: str, payload: dict[str, object]) -> bool:
    try:
        selected = str(payload.get("run_id") or run_id)
        with _quiescent_probe_connection(owner_home) as conn:
            if conn is None:
                return False
            conn.execute("BEGIN")
            run = conn.execute(
                "SELECT ar.*, aa.status AS attempt_status, aa.agent_run_id AS attempt_run_id FROM agent_runs ar "
                "LEFT JOIN agent_attempts aa ON aa.attempt_id = ar.current_attempt_id "
                "WHERE ar.run_id = ? OR ar.agent_run_id = ? ORDER BY (ar.run_id = ?) DESC LIMIT 1",
                (selected, selected, selected),
            ).fetchone()
            if run is None or not run["parent_agent_run_id"] or run["status"] not in {"created", *AGENT_RUN_TERMINAL_STATUSES}:
                return False
            if run["current_attempt_id"] and run["attempt_run_id"] != run["agent_run_id"]:
                return False
            if run["status"] not in AGENT_RUN_TERMINAL_STATUSES and run["attempt_status"] not in _QUIESCENT_ATTEMPT_STATUSES:
                return False
            attempts = conn.execute("SELECT status FROM agent_attempts WHERE agent_run_id = ?", (run["agent_run_id"],))
            if any(row["status"] not in _QUIESCENT_ATTEMPT_STATUSES for row in attempts):
                return False
            if conn.execute("SELECT 1 FROM resource_locks WHERE canonical_scope = ?", (exec_lock_scope(run["agent_run_id"]),)).fetchone():
                return False
            task = conn.execute("SELECT * FROM task_runs WHERE task_run_id = ?", (run["task_run_id"],)).fetchone()
            if task is None:
                return False
            task_ref = str(payload.get("root_task_id") or payload.get("task_id") or task["task_id"])
            # owner投影可用根run_id作task_id；仅接受同一TaskRun的根身份，不跨树猜父任务。
            if task_ref != task["task_id"] and conn.execute(
                "SELECT 1 FROM agent_runs WHERE task_run_id = ? AND parent_agent_run_id = '' AND run_id = ?",
                (run["task_run_id"], task_ref),
            ).fetchone() is None:
                return False
            if float(task["closed_at"] or 0) > 0 or task["status"] in _TERMINAL_TASK_RUN_STATUSES:
                return True
            agents = conn.execute(
                "SELECT ar.agent_run_id, ar.parent_agent_run_id, ar.status, aa.status AS attempt_status "
                "FROM agent_runs ar LEFT JOIN agent_attempts aa ON aa.attempt_id = ar.current_attempt_id "
                "WHERE ar.task_run_id = ?", (run["task_run_id"],),
            ).fetchall()
            return (sum(not row["parent_agent_run_id"] for row in agents) == 1
                    and _quiescent_nonterminal_agent_runs(conn, agents) is not None)
    except Exception:
        return False


# 每次发现最多检查5000个目录项；跨轮内存迭代器避免大目录反复从头扫描。
_SUBAGENT_SCAN_MAX_DIRS = 5000
_SUBAGENT_SWEEP_LOCK = threading.RLock()
_SUBAGENT_SWEEP_POSITIVES: OrderedDict[str, tuple[tuple[int, int], bool]] = OrderedDict()


# LLM: 签名与wake扫描使用不同游标，不消耗彼此页面；每轮只stat预算内状态，跨页变动使缓存保守失效。
# 函数用途: 为owner事实缓存生成有界agents页签名，包含目录物理身份。
def _subagent_page_signature(owner_home: Path) -> tuple[Any, ...]:
    from .subagents.services.persistence.locator_hygiene import agent_projection_page

    runs, complete = agent_projection_page(owner_home, _SUBAGENT_SCAN_MAX_DIRS, purpose="signature")
    return (_dir_own_signature(owner_home / "agents"), complete,
            tuple((run.name, *_file_signature(run / "state.json")) for run in runs))


# LLM: 仅明示缺失工作区且无runtime活跃行才可跳过；读错、未知和closeout均保原唤醒，不改任何状态。
# 函数用途: 识别已删除测试工作区留下的悬空投影，不将它们误作需要驱动的子代理。
def _subagent_projection_is_dangling_orphan(owner_home: Path, run_dir: Path, payload: dict[str, object]) -> bool:
    from .subagents.services.persistence.locator_hygiene import (
        projection_runtime_facts,
        projection_workspace_missing,
    )
    try:
        if payload.get("runtime_closeout_pending") or payload.get("run_id", run_dir.name) != run_dir.name:
            return False
        if not projection_workspace_missing(run_dir, payload):
            return False
        status, pending = projection_runtime_facts(owner_home, run_dir.name, payload)
        return not pending and (not status or status in AGENT_RUN_TERMINAL_STATUSES)
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return False


# LLM: 收口重试优先，缺失locator和runtime终态过滤仅作用于陈旧投影；公平有界分页不新建数据库。
# 函数用途: 从owner级投影发现需驱动的子代理，跳过测试孤儿但不改变可恢复状态。
def _has_unfinished_subagent_run(owner_home: Path) -> bool:
    with _SUBAGENT_SWEEP_LOCK:
        root = owner_home / "agents"
        if root.is_symlink() or not root.is_dir():
            _SUBAGENT_SWEEP_POSITIVES.pop(str(root), None)
            return False
        info = root.stat()
        identity = info.st_dev, info.st_ino
        previous = _SUBAGENT_SWEEP_POSITIVES.pop(str(root), None)
        positive, exhausted = _probe_subagent_page(owner_home)
        positive = positive or bool(previous and previous[0] == identity and previous[1])
        _SUBAGENT_SWEEP_POSITIVES[str(root)] = (identity, False if exhausted else positive)
        while len(_SUBAGENT_SWEEP_POSITIVES) > 32:
            _SUBAGENT_SWEEP_POSITIVES.popitem(last=False)
        return positive or not exhausted


# LLM: 单页正事实与是否穷尽分开返回，由调用方跨页聚合；读取错误不伪造完整目录扫描。
# 函数用途: 按原runtime和closeout判据检查一页子代理投影。
def _probe_subagent_page(owner_home: Path) -> tuple[bool, bool]:
    """owner 名下在册子代理 run 是否有未完成的(agents/<run-id>/state.json 的 status)。

    ``owner_home/agents`` 是 owner 级全局投影，不是 SubAgentManager 的工作区。
    权威任务记录会分布在 workspace runtime 下，而这个投影专门用来让
    owner 级扫描不用猜每个 workspace slug。

    除"未完成状态"外，``runtime_closeout_pending`` 同样是硬事实：runner 终态收口写库失败、
    或收口后父级通知前中断时，run/task 都已是终态、且还没有 wake 信号，如果这里不认它，
    Gateway 的 reconcile 车道就不会来推进这条待重试事实，恢复链永远跑不到。
    每轮最多检查固定预算目录项，超大目录跨轮续扫，坏文件跳过。"""
    from .subagents.services.persistence.locator_hygiene import (
        agent_projection_page,
        projection_runtime_facts,
    )

    agents_dir = owner_home / "agents"
    if agents_dir.is_symlink() or not agents_dir.is_dir():
        return False, True
    try:
        runs, exhausted = agent_projection_page(owner_home, _SUBAGENT_SCAN_MAX_DIRS, purpose="wake")
        task_files = [run / "state.json" for run in runs]
    except OSError:
        return False, False
    for path in task_files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if bool(payload.get("runtime_closeout_pending")):
            return True, exhausted
        status = str(payload.get("status") or "").strip().upper()
        if status in _UNFINISHED_RUN_STATUSES:
            if _subagent_projection_is_dangling_orphan(owner_home, path.parent, payload):
                continue
            if status in {"PLANNING", "PENDING", "RUNNING"}:
                try:
                    runtime_status, pending = projection_runtime_facts(owner_home, path.parent.name, payload)
                    if not pending and runtime_status in AGENT_RUN_TERMINAL_STATUSES:
                        continue
                except (OSError, ValueError, TypeError, sqlite3.Error):
                    pass
            if status == "BLOCKED" and _blocked_subagent_is_quiescent(owner_home, path.parent.name, payload):
                continue
            return True, exhausted
    # 尚未检查到目录尾只能说未知，不能让预算外的活跃/closeout被当作不存在。
    return False, exhausted


_TASK_STATUS_CACHE_MAX_ENTRIES_COUNT = 131072
_TASK_STATUS_CACHE_LOCK = threading.Lock()
_TASK_STATUS_CACHE: OrderedDict[str, tuple[tuple[int, int, int, int], str, str]] = OrderedDict()

# 历史状态只复用60秒，任意手动原地改写不会永久隐形；原子链接替换由目录fp立即失效。
_HISTORY_REVERIFY_SECONDS = 60.0
# 平铺目录唤醒扫描在高频轮询下复核目录状态的最小间隔。
_FLAT_REVERIFY_SECONDS = 1.0
# 唤醒目录扫描缓存允许保留的最大目录条目数。
_DIRECTORY_CACHE_MAX_ENTRIES = 4096
_DIRECTORY_SCAN_LOCK = threading.RLock()
_LEDGER_DATE_DIR_CACHE: OrderedDict[str, _DirectoryProjection] = OrderedDict()
_FACT_DIRECTORY_CACHE: OrderedDict[str, _DirectoryProjection] = OrderedDict()


@dataclass(frozen=True)
class _DirectoryProjection:
    """LLM: 锁内发布的只读目录投影；records/statuses 不得在发布后原地修改。
    类用途: 同时缓存运行名、历史元数据、活跃集合和摘要，热路径不再遍历所有历史条目。
    """

    dir_fp: tuple[int, ...]
    run_names: tuple[str, ...]
    records: dict[str, tuple[Any, ...]]
    live_names: tuple[str, ...]
    active_rows: tuple[tuple[str, str, Path], ...]
    statuses: dict[str, frozenset[str]]
    signature: tuple[Any, ...]
    verified_at: float


# LLM: ctime补足恢复mtime的原子替换；目录不可读仍由扫描层分别处理缺失和错误。
# 函数用途: 读取目录的完整结构指纹，不读取目录中每个文件。
def _directory_fp(root: Path) -> tuple[int, ...]:
    try:
        st = root.stat()
    except FileNotFoundError:
        return ()
    return st.st_ino, st.st_mtime_ns, st.st_ctime_ns, st.st_size


# LLM: DirEntry类型信息来自目录枚举；仅在目录指纹变化时重枚举运行名或匹配文件名。
# 函数用途: 按模式取排序后的条目名，lock等非匹配文件不构造Path也不stat。
def _scan_names(root: Path, mode: str, pattern: str) -> tuple[str, ...]:
    try:
        with os.scandir(root) as entries:
            names = tuple(sorted(
                entry.name for entry in entries
                if (entry.is_dir() if mode == "ledger" else fnmatchcase(entry.name, pattern))
            ))
        # 小关联目录保留既有glob错误边界，避免将列不全误认作无关联；大目录完全绕过glob。
        if mode == "link" and len(names) <= 32:
            return tuple(sorted(path.name for path in root.glob(pattern)))
        return names
    except FileNotFoundError:
        return ()


# LLM: 账本状态位于固定叶子，链接与平层签名条目直接位于目录内。
# 函数用途: 将缓存的条目名转换成扫描对象，不做路径resolve。
def _scan_entry_path(root: Path, name: str, mode: str) -> Path:
    return root / name / "work" / "state.json" if mode == "ledger" else root / name


# LLM: 状态解析与现有_TASK_STATUS_CACHE同源；读期间变更的状态不允许成为稳定终态。
# 函数用途: 一次stat读取状态与物理指纹，缺失或竞态返回需下轮重查的投影。
def _directory_record(path: Path, mode: str) -> tuple[Any, ...]:
    try:
        st = path.stat()
    except OSError:
        return None, None
    fp = (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    if mode == "flat":
        return fp, None
    fields = _read_cached_task_status_fields(path, stat_result=st)
    with _TASK_STATUS_CACHE_LOCK:
        cached = _TASK_STATUS_CACHE.get(str(path))
        if cached is None or cached[0] != fp or cached[1:] != fields:
            return None, fields
    return fp, fields


# LLM: 仅显式合法终态可复用，未知/损坏/活跃状态必须保留实时复核；终态mtime距现在超过5秒才可稳定复用。
# 函数用途: 判定单条账本或任务链接是否仍需每轮现读，防止未知状态被误归入已沉淀终态。
def _status_record_live(record: tuple[Any, ...], mode: str, now_ns: int) -> bool:
    fp, fields = record
    if fp is None or fields is None or not fields[1]:
        return True
    terminal = (fields[1].upper() in _SETTLED_LEDGER_STATUSES if mode == "ledger"
                else conversation_task_link_is_terminal(fields[1].lower()))
    return not terminal or now_ns - fp[2] <= 5_000_000_000


# LLM: 只在冷读或内容变更时构造全量索引/摘要；正常轮询只访问live_names与active_rows。
# 函数用途: 从同一次记录快照生成签名和用于发现/补关的状态索引。
def _build_directory_projection(root, mode, fp, names, records, verified_at):
    now_ns = time.time_ns()
    if mode == "flat":
        recent = set(sorted(names, key=lambda name: (records[name][0] or (0, 0, 0, 0))[2], reverse=True)[:32])
        live = tuple(name for name in names if name in recent
                     or records[name][0] is None or now_ns - records[name][0][2] <= 60_000_000_000)
    else:
        live = tuple(name for name in names if _status_record_live(records[name], mode, now_ns))
    active, statuses = [], {}
    for name, (_file_fp, fields) in records.items():
        if fields is not None and fields[0]:
            task_id, status = fields
            statuses.setdefault(task_id, set()).add(status.lower())
            if (status.upper() in _UNFINISHED_RUN_STATUSES if mode == "ledger"
                    else status.lower() == THREAD_TASK_LINK_ACTIVE_STATUS):
                active.append((task_id, status, _scan_entry_path(root, name, mode)))
    blob = repr(tuple((name, records[name][0]) for name in names)).encode("utf-8", "surrogatepass")
    signature = (str(root), fp, len(names), hashlib.blake2b(blob, digest_size=16).hexdigest())
    return _DirectoryProjection(fp, names, records, live, tuple(active),
                                {key: frozenset(value) for key, value in statuses.items()}, signature, verified_at)


# LLM: 投影读改写共用锁，防止旧投影覆盖新记录；目录上限4096，解析LRU独立限制131072。
# 函数用途: 返回共享目录投影，扫描异常由调用方保守处理。
def _directory_scan(root: Path, mode: str, pattern: str = "*.json") -> _DirectoryProjection:
    with _DIRECTORY_SCAN_LOCK:
        cache = _LEDGER_DATE_DIR_CACHE if mode == "ledger" else _FACT_DIRECTORY_CACHE
        key = str(root) if mode == "ledger" else f"{root}:{mode}:{pattern}"
        cached = cache.get(key)
        projection = _refresh_directory_projection(root, mode, pattern, cached)
        cache[key] = projection
        cache.move_to_end(key)
        while len(cache) > _DIRECTORY_CACHE_MAX_ENTRIES:
            cache.popitem(last=False)
        return projection


# LLM: 目录变更即全量复核；小目录始终stat；大目录只检查活跃/新近记录，TTL兜手动历史改写。
# 函数用途: 刷新共享目录快照，未变时直接返回旧摘要，避免每轮hash十万条记录。
def _refresh_directory_projection(root, mode, pattern, cached):
    fp, now = _directory_fp(root), time.monotonic()
    same_dir = cached is not None and cached.dir_fp == fp
    names = cached.run_names if same_dir else _scan_names(root, mode, pattern)
    limit = 64 if mode == "flat" else 32
    ttl = _FLAT_REVERIFY_SECONDS if mode == "flat" else _HISTORY_REVERIFY_SECONDS
    full = not same_dir or len(names) <= limit or now - cached.verified_at >= ttl
    check_names = names if full else cached.live_names
    updates = {}
    for name in check_names:
        record = _directory_record(_scan_entry_path(root, name, mode), mode)
        if cached is None or cached.records.get(name) != record:
            updates[name] = record
    if same_dir and not updates and not full:
        if mode == "flat":
            return cached
        live = tuple(name for name in cached.live_names if _status_record_live(cached.records[name], mode, time.time_ns()))
        return cached if live == cached.live_names else replace(cached, live_names=live)
    records = {name: updates.get(name, cached.records.get(name)) for name in names} if cached else updates
    projection = _build_directory_projection(root, mode, fp, names, records, now if full else cached.verified_at)
    # 大消息目录冷扫描可能比1秒TTL还慢，平层复核完成后才开始计算复用窗口。
    return replace(projection, verified_at=time.monotonic()) if full and mode == "flat" else projection


# LLM: 根目录只枚举日期层；每个日期下运行名和状态摘要由有界目录缓存复用。
# 函数用途: 同源提供runs与tasks的账本扫描，不下钻glob每个work目录。
def _ledger_directory_scans(root: Path) -> tuple[_DirectoryProjection, ...]:
    return tuple(_directory_scan(root / name, "ledger") for name in _scan_names(root, "ledger", ""))


# LLM: 仅缓存物理指纹未变文件的字符串投影；扫描器可传已读stat，失败不缓存。
# 函数用途: 读取任务链接或运行状态文件的 task_id 与 status，未变化文件复用内存指纹快照。
def _read_cached_task_status_fields(path: Path, *, stat_result=None) -> tuple[str, str] | None:
    try:
        before_st = path.stat() if stat_result is None else stat_result
    except OSError:
        return None
    before_fp = (before_st.st_ino, before_st.st_size, before_st.st_mtime_ns, before_st.st_ctime_ns)
    key = str(path)
    with _TASK_STATUS_CACHE_LOCK:
        cached = _TASK_STATUS_CACHE.get(key)
        if cached is not None and cached[0] == before_fp:
            _TASK_STATUS_CACHE.move_to_end(key)
            return cached[1], cached[2]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    task_id = str(payload.get("task_id") or "").strip()
    status = str(payload.get("status") or "").strip()
    try:
        after_st = path.stat()
    except OSError:
        after_st = None
    if after_st is not None:
        after_fp = (after_st.st_ino, after_st.st_size, after_st.st_mtime_ns, after_st.st_ctime_ns)
        if after_fp == before_fp:
            with _TASK_STATUS_CACHE_LOCK:
                _TASK_STATUS_CACHE[key] = (after_fp, task_id, status)
                _TASK_STATUS_CACHE.move_to_end(key)
                while len(_TASK_STATUS_CACHE) > _TASK_STATUS_CACHE_MAX_ENTRIES_COUNT:
                    _TASK_STATUS_CACHE.popitem(last=False)
    return task_id, status


# LLM: 链接生命周期、账本交集与runtime权威不变；扫描投影只复用历史项，活跃项每轮检查。
# 函数用途: 从默认/配置关联及运行账本中找仍须驱动的任务，并保留崩溃补关路径。
def unfinished_task_ids(
    owner_home: Path,
    *,
    deadline_out: list[float] | None = None,
    conversation_tasks_dir: Path | None = None,
) -> list[str]:
    """owner 名下未完成任务 id 列表——link active(生命周期权威)且账本非终态(交叉验证)。

    link(workspace 下 ``conversations/tasks/<task_id>.json``)由 bind_task 同步写,
    status=active 是生命周期在册;但 link 单独不够:线程 goal/协作/audit 任务 bind 后
    link 也是 active,它们各有自己的驱动通道(thread_goal_continue/协作事件/audit 通道),
    不该落 task_ledger_resume(测试实锤 9 例回归)。账本(tasks/<date>/<name>/work/
    state.json)由任务运行时写,只有真正开始跑的任务才有——两者取交集就是「在册且
    在跑,需要兜底驱动」的任务。

    反向排除:子代理 DONE 后账本残留 RUNNING(真机:7 月旧任务 932h 无终态)但 link
    已终态;影子账本(task-path: 指纹寻址,无 link)。发现层用它判硬事实,后台 tick
    用它把任务落成续跑 wake——两处同一把尺。坏文件跳过。

    LLM: ``deadline_out`` 只用于收集"执行权锁过期"这一时间边界(锁过期后同一任务会重新进入
    候选,none → hard);不传参时行为与旧实现逐字一致。``conversation_tasks_dir`` 是调用方 agent 实际
    在用的会话存储关联目录(``conversation_store.storage.tasks_dir``,由 runtime_paths 同一解析入口算出);
    配置了 conversation_workspace 指向默认布局之外时,它补进扫描与补关,不传或就在默认布局里时行为不变。"""
    links_root = owner_home / "workspace" / "runtime" / "workspaces"
    active_ids: list[str] = []
    links_by_task: dict[str, Path] = {}
    link_scans, link_dirs = _conversation_task_link_scan(links_root, conversation_tasks_dir)
    active_rows = sorted((row for scan in link_scans for row in scan.active_rows), key=lambda row: str(row[2]), reverse=True)
    for task_id, _status, path in active_rows:
        if task_id not in links_by_task:
            active_ids.append(task_id)
            links_by_task[task_id] = path
    repo = _runtime_repo_for_owner(owner_home)
    if repo is not None:
        _reconcile_terminal_conversation_task_runs(repo, {}, link_dirs, link_scans=link_scans)
    if not active_ids:
        return []
    # 新记录在 runs，存量在 tasks；仅扫描标准宿主 state 叶子，不从业务文件名推断活跃运行。
    try:
        ledger_scans = tuple(scan for root in ("runs", "tasks")
                             for scan in _ledger_directory_scans(owner_home / root))
    except OSError:
        return []
    ledger_ids = {row[0] for scan in ledger_scans for row in scan.active_rows}
    candidates = [task_id for task_id in active_ids if task_id in ledger_ids]
    return _filter_by_runtime_authority(
        owner_home,
        candidates,
        links_by_task,
        deadline_out=deadline_out,
    )


# LLM: Discovery replays the same TaskRun closeout CAS after process crashes, with the same two rules as the runtime edge
#   (conversation/task_run_closeout): one unambiguous terminal link status, or — C12c, 2026-10-01 — a link file that truly
#   does not exist (the request never promoted a conversation task, so its own agent tree governs; reason
#   no_conversation_task). "Truly absent" is a filesystem fact checked right before the CAS: any `<task_id>.json` under the
#   canonical `*/conversations/tasks` dirs or the caller runtime's configured conversation tasks dir (link_dirs from
#   _conversation_task_link_scan), readable or not, keeps the TaskRun open, and so does an incomplete directory scan
#   (link_dirs is None). The absent branch also requires the root main run to be terminal before trying the CAS. Active, conflicting,
#   unreadable or unknown link states, and unknown/active trees, stay open. Side effect: may write task_run.closed.
#   Change together with test_task_run_close_without_conversation_task.py and test_owner_wake_discovery_task_run_no_link.py.
# 函数用途: 网关启动或周期发现时补齐“会话链接已结束（或根本没有会话任务）、代理树也已结束，但执行总账未关”的崩溃窗口。
def _reconcile_terminal_conversation_task_runs(
    repo: RuntimeRepository,
    link_statuses: dict[str, set[str]],
    link_dirs: tuple[Path, ...] | None,
    *,
    link_scans: tuple[_DirectoryProjection, ...] = (),
) -> None:
    try:
        open_runs = repo.open_task_runs()
    except Exception:  # noqa: BLE001 恢复投影失败不能阻断 owner 发现
        return
    for task_run in open_runs:
        task_id = str(task_run["task_id"] or "").strip()
        try:
            statuses = set(link_statuses.get(task_id, set()))
            for scan in link_scans:
                statuses.update(scan.statuses.get(task_id, ()))
            reason = _task_run_reconcile_reason(repo, task_id, statuses, link_dirs)
        except Exception:  # noqa: BLE001 单条坏账不影响其它 owner 任务发现
            continue
        if not reason:
            continue
        try:
            repo.settle_task_run_if_agent_tree_terminal(
                task_run_id=str(task_run["task_run_id"] or ""),
                task_id=task_id,
                operator="wake-discovery-task-run-reconcile",
                reason=reason,
            )
        except Exception:  # noqa: BLE001 单条坏账不影响其它 owner 任务发现
            continue


# LLM: 发现与补关共用关联状态投影；配置目录不可见时保留默认关联，只禁止按无关联补关。
# 函数用途: 返回本owner默认/额外关联目录的扫描投影和完整性标记。
def _conversation_task_link_scan(
    links_root: Path, runtime_tasks_dir: Path | None,
) -> tuple[tuple[_DirectoryProjection, ...], tuple[Path, ...] | None]:
    link_dirs = _conversation_task_link_dirs(links_root)
    extra = _configured_task_links_dir(links_root, runtime_tasks_dir)
    folders = list(link_dirs or ())
    complete = link_dirs is not None
    if extra is not None:
        if not extra.is_dir():
            complete = False
        else:
            folders.append(extra)
    scans = []
    try:
        for folder in folders:
            scans.append(_directory_scan(folder, "link"))
    except OSError:
        return tuple(scans), None
    return tuple(scans), tuple(folders) if complete else None


# LLM: 默认布局的关联目录都匹配 `<links_root>/*/conversations/tasks`，已在通配里，返回 None 免得同一目录扫两遍；
#   其余（配置的 conversation_workspace 在别处）原样返回。按 resolve(strict=False) 比较，不创建目录。
# 函数用途: 判断运行时实际会话存储的关联目录是否需要额外扫描。
def _configured_task_links_dir(links_root: Path, runtime_tasks_dir: Path | None) -> Path | None:
    if runtime_tasks_dir is None:
        return None
    folder = Path(runtime_tasks_dir)
    resolved = folder.resolve(strict=False)
    in_default_layout = (
        resolved.name == "tasks"
        and resolved.parent.name == "conversations"
        and resolved.parent.parent.parent == links_root.resolve(strict=False)
    )
    return None if in_default_layout else folder


# LLM: 会话任务关联的规范位置是 `<owner>/workspace/runtime/workspaces/<工作区>/conversations/tasks/<task_id>.json`
#   （store_layout.task_path）。根目录不存在说明这个 owner 从没建过会话存储，返回空元组（所有 TaskRun 都没有关联）；
#   列目录出错返回 None，调用方就不按"没有关联"补关任何一条。只做目录元数据读取。
# 函数用途: 列出本 owner 全部会话任务关联目录；列不全时明确返回 None。
def _conversation_task_link_dirs(links_root: Path) -> tuple[Path, ...] | None:
    if not links_root.is_dir():
        return ()
    try:
        workspaces = _scan_names(links_root, "ledger", "")
        # 稀疏工作区保留原glob的错误语义；大量工作区只检查固定两层目录。
        if len(workspaces) <= 32:
            return tuple(links_root.glob("*/conversations/tasks"))
        return tuple(folder for name in workspaces
                     if (folder := links_root / name / "conversations" / "tasks").is_dir())
    except OSError:
        return None


# LLM: 返回补关原因，空串表示保持打开。唯一可读的终态关联 → conversation_task_<状态>；没有读出状态时，只有
#   link_dirs 列全、任何目录里都没有这个任务的关联文件（读坏的文件也算"有"）、且根主代理执行轮已终态，才返回
#   no_conversation_task——树里其余执行是否终态/静止仍由仓库的树终态 CAS 判。只读：stat 与一次运行库查询。
# 函数用途: 决定一条未关的执行总账能不能由发现层补关，以及用什么原因。
def _task_run_reconcile_reason(
    repo: RuntimeRepository, task_id: str, statuses: set[str], link_dirs: tuple[Path, ...] | None,
) -> str:
    if len(statuses) == 1:
        status = next(iter(statuses))
        return f"conversation_task_{status}" if conversation_task_link_is_terminal(status) else ""
    if statuses or not task_id or link_dirs is None:
        return ""
    if any((folder / f"{task_id}.json").exists() for folder in link_dirs):
        return ""
    root = _main_run_row_for_task(repo, task_id)
    if root is None or str(root["status"] or "") not in AGENT_RUN_TERMINAL_STATUSES:
        return ""
    return "no_conversation_task"


def _filter_by_runtime_authority(
    owner_home: Path,
    candidates: list[str],
    links_by_task: dict[str, Path] | None = None,
    *,
    deadline_out: list[float] | None = None,
) -> list[str]:
    """R1-03：runtime.db 是终态单一权威——state.json 残留 RUNNING 不再驱动。

    对每个候选 task 查 role='main' AgentRun：
      - 权威终态（done/failed/cancelled）→ 先把任务级账本（link/state.json/
        task_run）投影终态（自愈闭环，R1-03 补漏 2）；投影成功不写诊断
        （残留是「待自愈」而非冲突）；投影失败才写 status_conflict 诊断
        （可观测）。无论投影结果如何都排除——权威终态绝不参与驱动
        （自喂循环真机根因：settle 置终态后仍被每 cooldown 挂新 attempt）。
      - 权威 cancelled 但当前代次是用户 Esc / /interrupt 中断、仍在续接窗口内
        （runtime_db.user_interrupt）→ 不投影、不诊断、也不驱动，用户再发消息接着原任务；过窗口照上一条收口。
      - 非终态但有活跃执行权锁（worker 在跑）→ 排除不催（驱动链 lease 感知）。
      - 其余（含无权威记录）→ 照旧驱动。

    LLM: 执行权锁会过期——锁一没,同一任务立刻回到候选(结论从 none 变 hard)。锁的到期时刻不在
    这里重新查库(那是额外 SQLite 查询),而是记一个短的保守重探边界 _EXEC_LOCK_REPROBE_SECONDS,
    让判定缓存最多 30s 后重新现读;不传 deadline_out 时行为与旧实现逐字一致。
    """
    if not candidates:
        return []
    repo = _runtime_repo_for_owner(owner_home)
    if repo is None:
        return candidates  # 无权威库：保持旧行为（fail-open 不误杀）
    filtered: list[str] = []
    for task_id in candidates:
        try:
            row = _main_run_row_for_task(repo, task_id)
        except Exception:  # noqa: BLE001 审计是附加保证，查询失败不反噬驱动
            filtered.append(task_id)
            continue
        if row is None:
            filtered.append(task_id)
            continue
        status = str(row["status"] or "")
        if status in AGENT_RUN_TERMINAL_STATUSES:
            if status == "cancelled" and _user_interrupt_still_resumable(repo, row, deadline_out):
                continue  # 用户中断后的续接窗口内：任务保持 active，不收成 cancelled、不写冲突诊断，也不驱动
            # 投影只对「任务终止语义」的终态执行：cancelled（孤儿回收/用户
            # 停止/会话控制）意味着任务生命周期终止，账本残留该自愈。done/
            # failed 是轮间/可重试形态——link active 是持续任务（audit/监控
            # 一轮轮跑）的正常生命周期，link 终态化会误杀它（wake stale
            # 检查把 link 终态当作「任务已死」作废 pending wake，真机教训）。
            projected = False
            if status == "cancelled":
                link_path = (links_by_task or {}).get(task_id)
                projected = _project_task_ledger_terminal(
                    owner_home, repo, task_id, row, link_path,
                )
            # 诊断只覆盖「cancelled 投影失败」：done/failed 有意不投影（轮间形态，
            # link active 是持续任务正常生命周期），不是冲突——写诊断会把
            # 正常形态每 tick 记成 ledger_stale_after_terminal 无限洪泛。
            if status == "cancelled" and not projected:
                try:
                    repo.append_event(
                        event_type="status_conflict",
                        attempt_id="",
                        agent_run_id=str(row["agent_run_id"]),
                        payload={"status": status, "source": "unfinished_task_ids",
                                 "reason": "ledger_stale_after_terminal",
                                 "task_id": task_id},
                    )
                except Exception:  # noqa: BLE001 事件写失败不反噬
                    pass
            continue  # 权威终态 → 排除（自愈成功不驱动；失败保留诊断）
        if repo.has_active_exec_lock(str(row["agent_run_id"])):
            _add_deadline(deadline_out, time.time() + _EXEC_LOCK_REPROBE_SECONDS)
            continue  # worker 在跑 → 不催
        filtered.append(task_id)
    return filtered


# LLM: 用户 Esc / /interrupt 中断的执行代次（runtime_db.user_interrupt 事件，绑定 current_attempt_id）在续接窗口内
#   不算“任务终止”，账本自愈跳过它；窗口截止时刻进 deadline_out，判定缓存到点重算，过期后照原规则收口。
#   读不到事件按旧规则处理（返回 False）。只读 runtime_events。
# 函数用途: 判断一个已 cancelled 的主执行轮是否是用户中断、仍在可续接窗口内。
def _user_interrupt_still_resumable(repo: RuntimeRepository, row, deadline_out: list[float] | None) -> bool:
    try:
        resume_until = user_interrupt_resume_until(repo, row)
    except Exception:  # noqa: BLE001 读不到中断事实：按旧规则收口，不猜
        return False
    if resume_until <= time.time():
        return False
    _add_deadline(deadline_out, resume_until)
    return True


# LLM: 自愈收口原因只取结构化事实：带用户中断事件的（此时已过续接窗口）记 user_interrupt_expired，其余照旧
#   terminal_run_ledger_stale。读事件失败按旧原因。只读 runtime_events。
# 函数用途: 给账本自愈关 TaskRun 选择审计原因，区分“用户中断后没回来”和普通陈旧账本。
def _ledger_heal_reason(repo: RuntimeRepository, run_row) -> str:
    try:
        expired = user_interrupt_resume_until(repo, run_row) > 0
    except Exception:  # noqa: BLE001
        expired = False
    return "user_interrupt_expired" if expired else "terminal_run_ledger_stale"


# LLM: 扫描快照不是写权限；与执行器换代共用 task transition 锁，重新核对当前 attempt 与 Goal。
# 函数用途: 防止旧取消记录盖掉已续接的执行，或把有持续目标的一轮中断升级成整项任务取消。
def _project_task_ledger_terminal(
    owner_home: Path,
    repo: RuntimeRepository,
    task_id: str,
    run_row,
    link_path: Path | None,
) -> bool:
    if link_path is None or not link_path.is_file():
        return False
    from .conversation.store import ConversationStore

    store = ConversationStore(link_path.parent.parent, initialize=False)
    with store.tasks.transition_guard(task_id):
        current = repo.main_agent_run_for_task(task_id)
        if current is None:
            return False
        fields = ("agent_run_id", "current_attempt_id", "current_attempt_generation", "status")
        if any(current[key] != run_row[key] for key in fields):
            return True  # 旧快照已失效；新执行负责后续投影。
        link = store.tasks.load(task_id)
        if link is None:
            return False
        if link.status == "interrupted":
            return True  # 用户可恢复中断不是取消任务。
        try:
            goal = (
                store.goals.load(link.thread_id, task_id=task_id)
                if store.storage.goal_path(link.thread_id).is_file() else None
            )
        except (KeyError, ValueError, OSError):
            return False  # Goal 记录存在但不可核对，不能把它猜成普通取消任务。
        if goal is not None and goal.task_id == task_id and goal.status == "active":
            return True  # 是否续跑只由原 Goal wake 决定，此处不新增唤醒。
        return _project_task_ledger_terminal_locked(owner_home, repo, task_id, run_row, link_path)


# LLM: 调用方已持任务锁并核对当前代次；这里只投影真实任务取消，不决定是否恢复。
# 函数用途: 同步任务链接、归档和任务运行账本，保持原子换代之外的幂等收尾。
def _project_task_ledger_terminal_locked(
    owner_home: Path,
    repo: RuntimeRepository,
    task_id: str,
    run_row,
    link_path: Path | None,
) -> bool:
    """R1-03 补漏 2：run 权威终态后把任务级账本投影终态（发现层自愈）。

    真机风暴根因：孤儿回收 tick settle run 终态（cancelled）后 state.json
    残留 RUNNING、link 残留 active、task_run 未 closeout → 发现层每 tick 写
    一条 status_conflict 诊断事件（每秒 2 条无限增长）。本函数一步闭环：
      ① link status → 'cancelled'（生命周期权威终态，保留其余字段）
      ② state.json → finish_run_workspace（权威终态写；身份从文件读，
         过 run_workspace.json 与 state.json 双重 identity 校验）
      ③ task_run → settle_task_run_terminal（closed_at CAS 幂等）
    三步全部成功 → True（不写诊断事件，风暴自然停）；任一步失败 → False
    （保留诊断事件可观测）。各步独立幂等：部分成功后下次 tick 继续收敛，
    且 link 已终态后该 task 不再进候选（诊断一次性/低频）。
    """
    if link_path is None or not link_path.is_file():
        return False
    try:
        link_payload = json.loads(link_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return False
    if not isinstance(link_payload, dict):
        return False
    # ① link status → 'cancelled'（生命周期权威终态，保留其余字段）
    if str(link_payload.get("status") or "").strip().lower() == THREAD_TASK_LINK_ACTIVE_STATUS:
        def updater(data: dict[str, Any]) -> dict[str, Any]:
            if str(data.get("status") or "").strip().lower() != THREAD_TASK_LINK_ACTIVE_STATUS:
                return data
            updated = dict(data)
            updated["status"] = "cancelled"
            return updated

        try:
            update_json_file_atomic(link_path, updater, require_existing=True)
        except (OSError, FileNotFoundError, TypeError):
            return False
    # ② state.json → finish_run_workspace（权威终态写；身份从文件读）
    task_root = _task_root_for_link(owner_home, link_payload, task_id)
    if task_root is None:
        return False
    try:
        identity = _read_json_object_at(task_root / "work" / "run_workspace.json")
        state = _read_json_object_at(task_root / "work" / "state.json")
        request = FinishRunWorkspaceRequest(
            root=task_root,
            request_id=str(identity.get("request_id") or "").strip(),
            run_id=str(
                identity.get("run_id") or state.get("primary_run_id") or ""
            ).strip(),
            task_id=str(
                identity.get("task_id") or state.get("task_id") or task_id
            ).strip(),
            status="CANCELLED",
            verification_status="UNVERIFIED",
            runtime_status="cancelled",
        )
        finished = finish_run_workspace(request)
    except Exception:  # noqa: BLE001 投影失败不反噬发现层
        return False
    if finished is None:
        return False
    projected = _read_json_object_at(task_root / "work" / "state.json")
    if str(projected.get("status") or "").strip().upper() != "CANCELLED":
        return False
    # ③ task_run 终态化（closed_at CAS 幂等；与 agent_run.completed 对称可审计）
    try:
        result = repo.settle_task_run_terminal(
            task_run_id=str(run_row["task_run_id"] or ""),
            task_id=task_id,
            status="cancelled",
            operator="wake-discovery-ledger-heal",
            reason=_ledger_heal_reason(repo, run_row),
        )
    except Exception:  # noqa: BLE001
        return False
    return bool(result.get("settled"))


def _task_root_for_link(owner_home: Path, link_payload: dict, task_id: str) -> Path | None:
    """定位 task 工作区根目录：优先 link.task_path（相对 owner_home 解析），
    失败按 state.json 的 task_id 反查（旧 link 无 task_path 的兜底）。"""
    task_path = str(link_payload.get("task_path") or "").strip()
    if task_path:
        candidate = Path(task_path)
        if not candidate.is_absolute():
            candidate = Path(owner_home) / candidate
        if (candidate / "work" / "state.json").is_file():
            return candidate
    tasks_root = Path(owner_home) / "tasks"
    try:
        for state_path in sorted(tasks_root.glob("*/*/work/state.json"), reverse=True):
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            if (
                isinstance(payload, dict)
                and str(payload.get("task_id") or "").strip() == task_id
            ):
                return state_path.parent.parent
    except (OSError, UnicodeError, ValueError):
        return None
    return None


def _read_json_object_at(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _has_unfinished_task_ledger(
    owner_home: Path,
    *,
    deadline_out: list[float] | None = None,
) -> bool:
    """owner 名下普通任务账本是否有未完成的(见 unfinished_task_ids)。

    主代理要靠 tick 驱动续跑;gateway 重启/逐出后发现层不认 RUNNING 账本 → owner
    永不进池 → 任务停摆(真机:celery 复刻 RUNNING 6h 无人驱动,主代理会话停在闸门
    拦截处)。"""
    return bool(unfinished_task_ids(owner_home, deadline_out=deadline_out))


def _has_incomplete_watch_lane(
    owner_home: Path,
    *,
    deadline_out: list[float] | None = None,
) -> bool:
    """owner 名下是否有未盯完的 watch 路(未 close 且窗口未满或 spool 有未判完候选)——
    与收口退休守卫/来源接管同一把尺(audit_state),命中即该 owner 需要被 tick。

    LLM: 判定本身仍调 audit_state 的唯一权威口径;``deadline_out`` 只额外收集"窗口关闭"这一
    时间边界(窗口内的 watch 会因为窗口走完而不再是硬事实),失败/不可读时按旧语义返回 False
    且不写任何边界。"""
    try:
        from .ingestion.audit_state import owner_home_has_incomplete_watch

        incomplete = owner_home_has_incomplete_watch(owner_home)
    except Exception:
        return False
    if incomplete and deadline_out is not None:
        # 只在"确实判活"时才算窗口关闭时刻:判定为假时不可能有"未来才开启的窗口"
        # (窗口内一律判真),所以这里不为绝大多数 quiet owner 多读一次 watch 快照。
        _add_deadline(deadline_out, _watch_window_deadline(owner_home))
    return incomplete


def _watch_window_deadline(owner_home: Path) -> float:
    """最早的"watch 窗口关闭"墙钟时刻;无窗口/不可读/已关窗返回正无穷(不设边界)。

    LLM: 只看窗口维度——spool 积压(backlog)是文件事实,由结构化签名覆盖,不在这里算时间边界。
    """
    if not (owner_home / "watch_state").is_dir():
        return float("inf")  # 无 watch 快照目录:连一次 glob 都不做(绝大多数 owner 的形态)
    try:
        from .ingestion.watch_state import list_states

        now = time.time()
        deadlines = [
            opened_at + window
            for row in list_states(owner_home)
            if not bool(row.get("closed"))
            for window, opened_at in [
                (
                    int(row.get("watch_window_seconds") or 0),
                    float(row.get("opened_at") or 0.0),
                )
            ]
            if window > 0 and opened_at > 0 and now < opened_at + window
        ]
    except Exception:  # noqa: BLE001 额外边界不可读不影响判定本身
        return float("inf")
    return min(deadlines) if deadlines else float("inf")


def _store_roots(owner_home: Path):
    for pattern in _STORE_ROOT_PATTERNS:
        yield from owner_home.glob(pattern)


# LLM: Passive clients and wake discovery must share the same finite set of existing conversation
# layouts. This helper never creates paths, follows no recursive glob, and receives an already
# authenticated exact owner home from its caller.
# 函数用途: 列出某个 owner 家目录中已存在的会话库，供重启恢复和冷通知重放共用。
def existing_conversation_store_roots(owner_home: str | Path) -> tuple[Path, ...]:
    resolved_home = Path(owner_home)
    unique: dict[str, Path] = {}
    for store_root in _store_roots(resolved_home):
        if not store_root.is_dir():
            continue
        unique.setdefault(str(store_root.resolve(strict=False)), store_root)
    return tuple(unique[key] for key in sorted(unique))


def _has_pending_wake_signal(store_root: Path) -> bool:
    # wake_queue/{urgent,normal} 里的文件即待处理(处理过的会被挪进 handled/)。
    for kind in ("urgent", "normal"):
        directory = store_root / "wake_queue" / kind
        try:
            with os.scandir(directory) as entries:
                if any(entry.name.endswith(".json") for entry in entries):
                    return True
        except OSError:
            continue
    return False


def _has_enabled_progress_policy(
    store_root: Path,
    *,
    deadline_out: list[float] | None = None,
) -> bool:
    """只认「已启用且已到期」的 policy(看 next_due_at,与 runtime._runnable_due_policies 同尺)。

    LLM: 旧实现只查 enabled 字段,退休/未到期 policy 的 owner 也会被当有活挂起,
    与真到期语义相悖;到期判定移到 _policy_due,发现层只做轻量筛选。
    ``deadline_out`` 收集未到期 policy 的 next_due_at(到点这一刻结论会翻转);不传参时行为与
    旧实现逐字一致。"""
    now = time.time()
    upcoming: list[float] = []
    for path in (store_root / "progress_policies").glob("*.json"):
        if _policy_due(path, now, deadline_out=upcoming):
            return True
    if upcoming and deadline_out is not None:
        deadline_out.append(min(upcoming))
    return False


def _policy_due(path: Path, now: float, *, deadline_out: list[float] | None = None) -> bool:
    """单条 policy 是否到期。

    LLM: ``deadline_out`` 只在"已启用但尚未到期"时追加 next_due_at(该时刻结论会翻转);
    坏 JSON / enabled=false / 无 next_due_at 都不产生时间边界(它们的翻转只由文件变化驱动)。
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return False
    if not isinstance(payload, dict) or not bool(payload.get("enabled", True)):
        return False
    next_due = payload.get("next_due_at")
    if next_due is None:
        # 无到期时间的旧格式:视为到期交 runtime 裁决,不静默丢弃
        return True
    try:
        due = float(next_due) <= now
    except (TypeError, ValueError):
        return True
    if not due:
        _add_deadline(deadline_out, float(next_due))
    return due


def _identity(provider: str, owner_kind: str, owner_id: str) -> Any:
    from .user_space.owner_resolver import OwnerIdentity

    if owner_kind == "group":
        return OwnerIdentity.provider_group(provider, owner_id)
    return OwnerIdentity.provider_user(provider, owner_id)


__all__ = [
    "OwnerHomeDiscoveryPage",
    "OwnerHomeDiscoveryTarget",
    "OwnerWakeCursor",
    "OwnerWakeDiscoveryPage",
    "OwnerWakeSeedPage",
    "discover_owner_home_page",
    "discover_wake_pending_owner_page",
    "discover_wake_pending_owners",
    "existing_conversation_store_roots",
    "seed_registry_from_disk",
    "seed_registry_page_from_disk",
]
