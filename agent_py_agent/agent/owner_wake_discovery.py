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
"""

from __future__ import annotations

import json
import logging
import time
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


@dataclass(frozen=True)
class OwnerWakeSeedPage:
    seeded: int
    next_cursor: OwnerWakeCursor | None
    scanned: int


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
    found = tuple(
        target.identity
        for target in page.targets
        if _owner_has_wake_pending_facts(target.home_dir)
    )
    return OwnerWakeDiscoveryPage(found, page.next_cursor, page.scanned)


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
    candidates = sorted(
        _candidate_owner_homes(providers_root),
        key=lambda item: _owner_cursor(item[0], item[1], item[2].name),
    )
    start = _candidate_start(candidates, after_cursor)
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


def _candidate_start(
    candidates: list[tuple[str, str, Path]],
    after_cursor: OwnerWakeCursor | None,
) -> int:
    if after_cursor is None:
        return 0
    keys = [_owner_cursor(provider, owner_kind, owner_home.name) for provider, owner_kind, owner_home in candidates]
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
    """Seed one bounded page and return its continuation cursor."""
    try:
        page = discover_wake_pending_owner_page(
            owners_dir,
            limit=limit,
            after_cursor=after_cursor,
        )
        for owner in page.owners:
            registry.record(owner)
        return OwnerWakeSeedPage(len(page.owners), page.next_cursor, page.scanned)
    except Exception:
        _LOGGER.warning("owner wake discovery failed (owners_dir=%s)", owners_dir, exc_info=True)
        return OwnerWakeSeedPage(0, after_cursor, 0)


def _owner_has_wake_pending_facts(owner_home: Path) -> bool:
    if any(
        _has_pending_wake_signal(store_root) or _has_enabled_progress_policy(store_root)
        for store_root in _store_roots(owner_home)
    ):
        return True
    return (
        _has_unfinished_subagent_run(owner_home)
        or _has_incomplete_watch_lane(owner_home)
        or _has_due_scheduler_fact(owner_home)
    )


def _has_due_scheduler_fact(owner_home: Path) -> bool:
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
    if isinstance(runs, dict) and any(
        isinstance(run, dict)
        and str(run.get("status") or "") in {"queued", "claimed", "running"}
        for run in runs.values()
    ):
        return True
    jobs = payload.get("jobs")
    current = time.time()
    return isinstance(jobs, dict) and any(
        isinstance(job, dict)
        and str(job.get("status") or "") == "active"
        and 0 < _scheduler_timestamp(job.get("next_run_at")) <= current
        for job in jobs.values()
    )


def _scheduler_timestamp(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


# 未完成 = 还需要被驱动:待派(PLANNING/PENDING)、在跑(RUNNING,重启后需判活回收)、
# 待解阻(BLOCKED,能力批复/续派通道)。终态与人为暂停(PAUSED)不算。
_UNFINISHED_RUN_STATUSES = frozenset({"PLANNING", "PENDING", "RUNNING", "BLOCKED"})


def _has_unfinished_subagent_run(owner_home: Path) -> bool:
    """owner 名下在册子代理 run 是否有未完成的(agents/<run-id>/state.json 的 status)。

    ``owner_home/agents`` 是 owner 级全局投影，不是 SubAgentManager 的工作区。
    权威任务记录会分布在 workspace runtime 下，而这个投影专门用来让
    owner 级扫描不用猜每个 workspace slug。

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
        if isinstance(payload, dict) and str(payload.get("status") or "").strip().upper() in _UNFINISHED_RUN_STATUSES:
            return True
    return False


def _has_incomplete_watch_lane(owner_home: Path) -> bool:
    """owner 名下是否有未盯完的 watch 路(未 close 且窗口未满或 spool 有未判完候选)——
    与收口退休守卫/排期自愈同一把尺(wake_backstop),命中即该 owner 需要被 tick。"""
    try:
        from .ingestion.wake_backstop import owner_home_has_incomplete_watch

        return owner_home_has_incomplete_watch(owner_home)
    except Exception:
        return False


def _store_roots(owner_home: Path):
    for pattern in _STORE_ROOT_PATTERNS:
        yield from owner_home.glob(pattern)


def _has_pending_wake_signal(store_root: Path) -> bool:
    # wake_queue/{urgent,normal} 里的文件即待处理(处理过的会被挪进 handled/)。
    for kind in ("urgent", "normal"):
        if any((store_root / "wake_queue" / kind).glob("*.json")):
            return True
    return False


def _has_enabled_progress_policy(store_root: Path) -> bool:
    for path in (store_root / "progress_policies").glob("*.json"):
        if _policy_enabled(path):
            return True
    return False


def _policy_enabled(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return False
    return isinstance(payload, dict) and bool(payload.get("enabled", True))


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
    "seed_registry_from_disk",
    "seed_registry_page_from_disk",
]
