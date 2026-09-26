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
下判定读到的每个文件(mtime_ns/size/inode/条目名),因此新增、删除或原地改写 wake/policy/
子代理 run/任务账本/link/调度账本/策展 state/候选/watch 快照/runtime.db 都会立刻失效;
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
import threading
import time
from bisect import bisect_right
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .conversation.models import THREAD_TASK_LINK_ACTIVE_STATUS
from .conversation.task_state import conversation_task_link_is_terminal
from .gateway_parts.io import update_json_file_atomic
from .memory_store.candidate_models import host_promotion_mode
from .runtime_db.repository import (
    AGENT_RUN_TERMINAL_STATUSES,
    RuntimeRepository,
)
from .runtime_db.schema import runtime_db_path
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
# 策展发现常量:与 config.CuratorConfig(interval_seconds=10_800)/daily_finalize_hour=23 对齐,
# 发现层不引配置避免循环依赖,只保证「到期」语义一致(真到期,非每天必挂/对话即挂)。
_CURATOR_INTERVAL_SECONDS = 10_800
_DAILY_FINALIZE_HOUR = 23
# 策展失败退避窗口:与 curator._run_pending_when_due 的 retry_after 上限(300s)对齐——
# 失败后的 pending owner 在窗口内不被发现层判活,避免 215 个失败 curator owner 每轮
# 分页种回登记表占工位(真机:占满 64 容量池,真活 user-a 被逐出饿死)。
_CURATOR_FAILURE_BACKOFF_SECONDS = 300
_PROVIDER_BUCKET_KINDS = {"users": "user", "groups": "group"}
OwnerWakeCursor = tuple[str, str, str]

# 分页目录快照:一次「枚举 + 整体排序」的结果投影,供同一轮分页的相邻页复用。
# 两个复用上界都必须成立才算命中——结构签名一致(TTL 内)+ 未超过 TTL。快照不是权威
# 状态:进程重启后缓存为空,首次调用与「无缓存」逐字一致;条目数上界防止路径种类
# (多租户/多测试目录)把缓存撑大。
_SNAPSHOT_TTL_SECONDS = 30.0
_SNAPSHOT_MAX_ENTRIES = 4
_OWNER_HOME_SNAPSHOTS: OrderedDict[str, _OwnerHomeSnapshot] = OrderedDict()
_SNAPSHOT_LOCK = threading.Lock()

# 事实判定缓存:同一 owner home 的硬/软事实结论,绑定"相关文件的结构化签名"(mtime_ns/尺寸/
# inode/条目名) + 时间边界。真机实锤:一页 19 owner 约 68.8ms,其中绝大部分花在
# _owner_fact_kind 逐盘读 policy/wake/curator/子代理/账本;而相邻 tick 之间这些文件通常
# 一个字节都没变。
#
# 复用要求两个上界同时成立:
#   ① 结构签名一致(owner home 下判定读到的每个文件/目录的 mtime_ns+size+inode+名字集合)
#      → 新增/删除/原地改写 wake 文件、policy、子代理 state.json、任务账本、link、调度账本、
#        策展 state、候选 jsonl、watch 快照、runtime.db(-wal/-shm) 都会立刻改变签名;
#   ② 未超过"最近一次可能因时间而改变结论的时刻"(policy 到期、job 到期、策展 interval/
#      退避/日终、watch 窗口关闭、执行权锁过期重探),且未超过兜底 TTL。
# 缓存只是投影:进程重启后为空;签名计算或读取异常一律回退现读,且绝不写缓存。
# 兜底 TTL:只兜"签名没覆盖到的变化"(粗粒度时间戳文件系统上的同尺寸原地改写、改钟)。
# 时间驱动的转变不靠它,而由判定时算出的最近边界收紧。取 600s 的理由:默认重扫间隔是
# background_owner_wake_rescan_seconds=120s,TTL 若与之同量级则每两轮就现读一次、收益被抵消;
# 600s 下稳态每 5 轮重扫现读一次,同时把未建模风险的最大陈旧窗口钉死在 10 分钟内。
_FACT_TTL_SECONDS = 600.0
# 条目上界按"一页 owner 数 × 若干页"取:太小时大 owners 目录会 LRU 抖动(实测 500 owner/页
# 时 256 条会让后半页每轮重新现读)。条目本身只有摘要与时刻,1024 条约 200KB。
_FACT_MAX_ENTRIES = 1024
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
    if not providers_root.is_dir():
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
    缓存条目按 _SNAPSHOT_MAX_ENTRIES 淘汰,不随路径种类无限增长。
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
        while len(_OWNER_HOME_SNAPSHOTS) > _SNAPSHOT_MAX_ENTRIES:
            _OWNER_HOME_SNAPSHOTS.popitem(last=False)
    return snapshot


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


def _candidate_owner_homes(providers_root: Path):
    for provider_dir, owner_kind, bucket_dir in _provider_buckets(providers_root):
        for owner_home in _dirs_of(bucket_dir):
            yield provider_dir.name, owner_kind, owner_home


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
        kind = _evaluate_owner_fact_kind(owner_home, deadlines)
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
        while len(_OWNER_FACT_CACHE) > _FACT_MAX_ENTRIES:
            _OWNER_FACT_CACHE.popitem(last=False)


def _bump_fact_stat(name: str) -> None:
    """事实缓存结构化计数(命中/未命中/回退现读);缺键容忍,计数不影响主链路。"""
    with _OWNER_FACT_LOCK:
        _OWNER_FACT_STATS[name] = int(_OWNER_FACT_STATS.get(name, 0)) + 1


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
    parts.append(("agents", _dir_entries_signature(owner_home / "agents", "*/state.json")))
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


def _dir_entries_signature(root: Path, pattern: str) -> tuple[Any, ...]:
    """目录内匹配条目的结构化签名: 每个条目的名字 + (mtime_ns, size, inode),再加目录自身指纹。

    LLM: 逐条目 stat 是必需的——只看目录 mtime 会漏掉"文件被原地改写"(目录 mtime 不变),
    而 enabled/next_due_at/status 这类内容正是判定要读的东西。目录自身指纹(mtime_ns+inode)
    覆盖"匹配集合没变但目录被动过"的形态;任何一项不可读都写显式占位,恢复可读后签名必变。
    函数用途: 让"新增/删除/改写"三类变化都能让事实判定缓存立刻失效,同时只做元数据读取。
    """
    try:
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


def _has_pending_memory_curator_work(
    owner_home: Path,
    *,
    deadline_out: list[float] | None = None,
) -> bool:
    """策展软事实判定。

    LLM: ``deadline_out`` 可选收集"结论可能因时间改变"的最早墙钟时刻(退避到期、interval 到期、
    下一个 23:00 日终窗口),供判定缓存决定有效期;不传参时行为与旧实现逐字一致。
    """
    if not _owner_memory_enabled(owner_home):
        return False  # 总闸关闭:curator 软活不判活(不占登记表工位,对称 skill 空快照)
    state_path = owner_home / "memory" / "curator" / "state.json"
    try:
        from .memory_store.curator_models import MemoryCuratorState, curator_failure_retry_seconds

        payload = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return False
        state = MemoryCuratorState.from_dict(payload)
    except FileNotFoundError:
        # 从未初始化:有新输入就该初始化(一次性低频,不构成占位源)
        return _curator_input_newer_than(owner_home, 0.0)
    except (OSError, UnicodeError, ValueError, TypeError):
        # 坏 state 不能证明有活;CuratorService 运行时会以结构化错误暴露,发现层不据此占工位
        return False
    now = time.time()
    # 失败退避:刚失败的 pending owner 不判活(curator 层 _run_pending_when_due 同窗口 300s)。
    # 否则失败后的 curator 软活每轮分页都被种回登记表占工位,硬事实 owner 反而被挤(真机实锤)。
    last_failure = _curator_success_timestamp(state.last_failure_at)
    # 没配模型的失败用更长的同源退避(curator_failure_retry_seconds),不按维护周期反复重建 owner 实例。
    backoff = curator_failure_retry_seconds(state.last_failure_code, _CURATOR_FAILURE_BACKOFF_SECONDS)
    if last_failure > 0 and now - last_failure < backoff:
        # 退避到期这一刻结论可能翻转(还需新输入,新输入由消息/审计 mtime 签名覆盖)。
        _add_deadline(deadline_out, last_failure + backoff)
        return False
    if state.pending_reasons or state.active_lease:
        # 非空 lease/pending 是文件事实:lease 过期与否都仍判活(旧语义),无时间边界。
        return True
    if _has_pending_promotable_candidates(owner_home):
        return True
    # 日终归档从「每天必挂」改为「真到期」:当天没归档且已过归档时刻(23 点)才挂。
    # 注意放 last_success 判定之前——无成功记录的 owner 也要能触发日终归档。
    if state.last_daily_finalize_date != datetime.now(timezone.utc).date().isoformat():
        if datetime.now(timezone.utc).hour >= _DAILY_FINALIZE_HOUR:
            # 日终窗口内的"挂起"跨过 UTC 零点就不再成立(日期滚动),所以边界是下一个零点。
            _add_deadline(deadline_out, _next_utc_midnight_timestamp())
            return True
        _add_deadline(deadline_out, _next_daily_finalize_timestamp())
    last_success = _curator_success_timestamp(state.last_success_at)
    if last_success <= 0:
        return _curator_input_newer_than(owner_home, 0.0)
    # interval 兜底:距上次成功 >= interval 且期间有新输入才挂(对话即挂收敛为真到期)
    if now - last_success >= _CURATOR_INTERVAL_SECONDS:
        return _curator_input_newer_than(owner_home, last_success)
    _add_deadline(deadline_out, last_success + _CURATOR_INTERVAL_SECONDS)
    return False


def _add_deadline(deadline_out: list[float] | None, moment: float) -> None:
    """记录一个"结论可能因时间改变"的墙钟时刻(可选出口;None 表示调用方不需要)。"""
    if deadline_out is not None:
        deadline_out.append(float(moment))


def _next_utc_midnight_timestamp() -> float:
    """下一个 UTC 零点(日期滚动会让"当天未归档"的判定换一天)。"""
    current = datetime.now(timezone.utc)
    moment = current.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return moment.timestamp()


def _next_daily_finalize_timestamp() -> float:
    """下一个 UTC 日终归档时刻(当天 23:00;已过则次日 23:00)。

    LLM: 走模块级 ``datetime`` 的 now() 而不是 fromtimestamp——发现层的日终判定本来就以
    ``datetime.now(timezone.utc)`` 为唯一时钟,测试用冻结时钟替换该名字时必须同时生效。
    """
    current = datetime.now(timezone.utc)
    moment = current.replace(hour=_DAILY_FINALIZE_HOUR, minute=0, second=0, microsecond=0)
    if moment <= current:
        moment = moment + timedelta(days=1)
    return moment.timestamp()


# LLM: Candidate discovery reads only typed status/promotion fields. New records use the host-owned
# promotion_mode; the narrow legacy fallback never inspects content and cannot grant formal authority.
# 函数用途: 判断 owner 是否有待自主晋升或门槛重试的候选，以便 Gateway 重启后仍能唤醒策展器。
def _has_pending_promotable_candidates(owner_home: Path) -> bool:
    path = owner_home / "memory" / "candidates.jsonl"
    if not path.is_file():
        return False
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(record, dict):
                    continue
                if str(record.get("status") or "").strip().lower() not in {
                    "pending_review",
                    "approved",
                }:
                    continue
                mode = str(record.get("promotion_mode") or "").strip().lower()
                current_mode = host_promotion_mode(record)
                if current_mode == "auto_eligible":
                    return True
                if mode == "manual_required":
                    continue
                # 升级前记录可能没有 promotion_mode。仅保留旧的明确事实来源唤醒语义；
                # 真正权限会在 CandidateService/PromotionService 重新计算和核验。
                origin = str(record.get("origin") or "").strip().lower()
                if origin in {"user_explicit", "tool_verified"}:
                    return True
    except OSError:
        return False
    return False


# LLM: mtime 只用于发现“需要 tick”的 owner；真正 cursor/due 判定仍由 CuratorService 的结构化 state 完成。
# 函数用途: 检查 ConversationStore 消息或 owner audit 是否晚于最近成功策展。
def _curator_input_newer_than(owner_home: Path, timestamp: float) -> bool:
    candidates = [
        *(path for root in _store_roots(owner_home) for path in (root / "messages").glob("*.jsonl")),
        *(owner_home / "audit").glob("*.jsonl"),
    ]
    for path in candidates:
        try:
            if path.is_file() and path.stat().st_size > 0 and path.stat().st_mtime > timestamp:
                return True
        except OSError:
            return True
    return False


# LLM: 坏 last_success_at 不能让 owner 永久沉睡；返回零使 discovery 保守唤醒后由严格 state parser 报错。
# 函数用途: 将 Curator UTC ISO 时间转换成发现层比较时间。
def _curator_success_timestamp(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        return 0.0
    return parsed.astimezone(timezone.utc).timestamp()


def _has_due_scheduler_fact(
    owner_home: Path,
    *,
    deadline_out: list[float] | None = None,
) -> bool:
    """Discover due jobs and recoverable nonterminal runs from the owner ledger.

    LLM: ``deadline_out`` 收集"最近一个 active job 的 next_run_at"——job 到点这一刻结论会从
    none 变成 hard,所以缓存有效期不能越过它。不传参时行为与旧实现逐字一致。
    """

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
    if isinstance(runs, dict) and any(
        isinstance(run, dict)
        and str(run.get("status") or "") in {"queued", "claimed", "running"}
        for run in runs.values()
    ):
        return True
    jobs = payload.get("jobs")
    current = time.time()
    due = False
    upcoming: list[float] = []
    if isinstance(jobs, dict):
        for job in jobs.values():
            if not isinstance(job, dict) or str(job.get("status") or "") != "active":
                continue
            next_run_at = _scheduler_timestamp(job.get("next_run_at"))
            if 0 < next_run_at <= current:
                due = True
                break
            if next_run_at > current:
                upcoming.append(next_run_at)
    if due:
        return True
    if upcoming:
        _add_deadline(deadline_out, min(upcoming))
    return False


def _scheduler_timestamp(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


# 未完成 = 还需要被驱动:待派(PLANNING/PENDING)、在跑(RUNNING,重启后需判活回收)、
# 待解阻(BLOCKED,能力批复/续派通道)。终态与人为暂停(PAUSED)不算。
_UNFINISHED_RUN_STATUSES = frozenset({"PLANNING", "PENDING", "RUNNING", "BLOCKED"})

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


def _has_unfinished_subagent_run(owner_home: Path) -> bool:
    """owner 名下在册子代理 run 是否有未完成的(agents/<run-id>/state.json 的 status)。

    ``owner_home/agents`` 是 owner 级全局投影，不是 SubAgentManager 的工作区。
    权威任务记录会分布在 workspace runtime 下，而这个投影专门用来让
    owner 级扫描不用猜每个 workspace slug。

    除"未完成状态"外，``runtime_closeout_pending`` 同样是硬事实：runner 终态收口写库失败、
    或收口后父级通知前中断时，run/task 都已是终态、且还没有 wake 信号，如果这里不认它，
    Gateway 的 reconcile 车道就不会来推进这条待重试事实，恢复链永远跑不到。
    倒序扫(run 目录名带时间戳,新的更可能未完成),命中即停;坏文件跳过。"""
    agents_dir = owner_home / "agents"
    if not agents_dir.is_dir():
        return False
    try:
        task_files = sorted(agents_dir.glob("*/state.json"), reverse=True)
    except OSError:
        return False
    for path in task_files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if bool(payload.get("runtime_closeout_pending")):
            return True
        if str(payload.get("status") or "").strip().upper() in _UNFINISHED_RUN_STATUSES:
            return True
    return False


def unfinished_task_ids(
    owner_home: Path,
    *,
    deadline_out: list[float] | None = None,
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
    候选,none → hard);不传参时行为与旧实现逐字一致。"""
    links_root = owner_home / "workspace" / "runtime" / "workspaces"
    active_ids: list[str] = []
    links_by_task: dict[str, Path] = {}
    link_statuses: dict[str, set[str]] = {}
    if links_root.is_dir():
        try:
            link_files = sorted(links_root.glob("*/conversations/tasks/*.json"), reverse=True)
        except OSError:
            link_files = []
        for path in link_files:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            task_id = str(payload.get("task_id") or "").strip()
            status = str(payload.get("status") or "").strip().lower()
            if task_id:
                link_statuses.setdefault(task_id, set()).add(status)
            if task_id and status == THREAD_TASK_LINK_ACTIVE_STATUS and task_id not in active_ids:
                active_ids.append(task_id)
                links_by_task[task_id] = path
    repo = _runtime_repo_for_owner(owner_home)
    if repo is not None:
        _reconcile_terminal_conversation_task_runs(repo, link_statuses)
    if not active_ids:
        return []
    # 新记录在 runs，存量在 tasks；仅扫描标准宿主 state 叶子，不从业务文件名推断活跃运行。
    try:
        task_files = sorted(
            (path for root in ("runs", "tasks")
             for path in (owner_home / root).glob("*/*/work/state.json")),
            reverse=True,
        )
    except OSError:
        return []
    ledger_ids: set[str] = set()
    for path in task_files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        if isinstance(payload, dict) and str(payload.get("status") or "").strip().upper() in _UNFINISHED_RUN_STATUSES:
            task_id = str(payload.get("task_id") or "").strip()
            if task_id:
                ledger_ids.add(task_id)
    candidates = [task_id for task_id in active_ids if task_id in ledger_ids]
    return _filter_by_runtime_authority(
        owner_home,
        candidates,
        links_by_task,
        deadline_out=deadline_out,
    )


# LLM: Discovery replays the same TaskRun closeout CAS after process crashes. It may
# trust only one unambiguous canonical link status per task and a fully terminal RuntimeDB
# tree; active, conflicting, unreadable, or unknown link states remain open.
# 函数用途: 网关启动或周期发现时补齐“会话链接已结束、代理树也已结束但执行总账未关”的崩溃窗口。
def _reconcile_terminal_conversation_task_runs(
    repo: RuntimeRepository,
    link_statuses: dict[str, set[str]],
) -> None:
    try:
        open_runs = repo.open_task_runs()
    except Exception:  # noqa: BLE001 恢复投影失败不能阻断 owner 发现
        return
    for task_run in open_runs:
        task_id = str(task_run["task_id"] or "").strip()
        statuses = link_statuses.get(task_id, set())
        if len(statuses) != 1:
            continue
        link_status = next(iter(statuses))
        if not conversation_task_link_is_terminal(link_status):
            continue
        try:
            repo.settle_task_run_if_agent_tree_terminal(
                task_run_id=str(task_run["task_run_id"] or ""),
                task_id=task_id,
                operator="wake-discovery-task-run-reconcile",
                reason=f"conversation_task_{link_status}",
            )
        except Exception:  # noqa: BLE001 单条坏账不影响其它 owner 任务发现
            continue


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
            reason="terminal_run_ledger_stale",
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
        if any((store_root / "wake_queue" / kind).glob("*.json")):
            return True
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
