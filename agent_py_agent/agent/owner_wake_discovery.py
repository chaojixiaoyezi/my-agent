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
from datetime import datetime, timezone
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
def _owner_fact_kind(owner_home: Path) -> str:
    if _owner_has_hard_facts(owner_home):
        return "hard"
    if _owner_has_soft_facts(owner_home):
        return "soft"
    return "none"


def _owner_has_hard_facts(owner_home: Path) -> bool:
    if any(
        _has_pending_wake_signal(store_root) or _has_enabled_progress_policy(store_root)
        for store_root in _store_roots(owner_home)
    ):
        return True
    return (
        _has_unfinished_subagent_run(owner_home)
        or _has_unfinished_task_ledger(owner_home)
        or _has_incomplete_watch_lane(owner_home)
        or _has_due_scheduler_fact(owner_home)
    )


def _owner_has_soft_facts(owner_home: Path) -> bool:
    return _has_pending_memory_curator_work(owner_home)


# LLM: 发现层只认结构化信号,与 CuratorService.run_if_due 同源;「无法证明没活=有活」已删——
# 真机 184 个 feishu 测试号被 state 缺失/每天必挂/对话即挂三条过宽判定天天挂起,
# 占满 64 容量运行池,真活的 user-a/user-b 被 LRU 逐出饿死(探针实锤)。
# 函数用途: 让 scoped owner 在 Gateway 重启或 LRU 逐出后重新进入既有后台 lane。
def _has_pending_memory_curator_work(owner_home: Path) -> bool:
    state_path = owner_home / "memory" / "curator" / "state.json"
    try:
        from .memory_store.curator_models import MemoryCuratorState

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
    # 失败退避:刚失败的 pending owner 不判活(curator 层 _run_pending_when_due 同窗口 300s)。
    # 否则失败后的 curator 软活每轮分页都被种回登记表占工位,硬事实 owner 反而被挤(真机实锤)。
    last_failure = _curator_success_timestamp(state.last_failure_at)
    if last_failure > 0 and time.time() - last_failure < _CURATOR_FAILURE_BACKOFF_SECONDS:
        return False
    if state.pending_reasons or state.active_lease:
        return True
    if _has_pending_promotable_candidates(owner_home):
        return True
    # 日终归档从「每天必挂」改为「真到期」:当天没归档且已过归档时刻(23 点)才挂。
    # 注意放 last_success 判定之前——无成功记录的 owner 也要能触发日终归档。
    if state.last_daily_finalize_date != datetime.now(timezone.utc).date().isoformat():
        if datetime.now(timezone.utc).hour >= _DAILY_FINALIZE_HOUR:
            return True
    last_success = _curator_success_timestamp(state.last_success_at)
    if last_success <= 0:
        return _curator_input_newer_than(owner_home, 0.0)
    # interval 兜底:距上次成功 >= interval 且期间有新输入才挂(对话即挂收敛为真到期)
    if time.time() - last_success >= _CURATOR_INTERVAL_SECONDS:
        return _curator_input_newer_than(owner_home, last_success)
    return False


# LLM: 候选文件是当前态唯一事实源(candidates.jsonl),发现层只读 status/origin 字段不解析正文。
# 函数用途: 是否有用户确认待晋升(pending_review 且 user_explicit/tool_verified)的候选——
# 有即策展活,与 run_if_due 晋升兜底同源(gateway_loops._has_pending_review_candidates 迁移)。
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
                if str(record.get("status") or "").strip().lower() != "pending_review":
                    continue
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


def _has_unfinished_task_ledger(owner_home: Path) -> bool:
    """owner 名下普通任务账本是否有未完成的(tasks/<date>/<name>/work/state.json 的 status)。

    子代理投影在 ``agents/``,solo 任务账本在 ``tasks/`` 按日分目录。主代理要靠 tick 驱动
    续跑;gateway 重启/逐出后发现层不认 RUNNING 账本 → owner 永不进池 → 任务停摆
    (真机:celery 复刻 RUNNING 6h 无人驱动,主代理会话停在闸门拦截处)。与
    _has_unfinished_subagent_run 同构:非终态即还需被驱动;坏文件跳过;倒序扫新日期
    优先(活跃任务总在最近日期目录),命中即停。"""
    tasks_root = owner_home / "tasks"
    if not tasks_root.is_dir():
        return False
    try:
        task_files = sorted(tasks_root.glob("*/*/work/state.json"), reverse=True)
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
    与收口退休守卫/来源接管同一把尺(audit_state),命中即该 owner 需要被 tick。"""
    try:
        from .ingestion.audit_state import owner_home_has_incomplete_watch

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
    """只认「已启用且已到期」的 policy(看 next_due_at,与 runtime._runnable_due_policies 同尺)。

    LLM: 旧实现只查 enabled 字段,退休/未到期 policy 的 owner 也会被当有活挂起,
    与真到期语义相悖;到期判定移到 _policy_due,发现层只做轻量筛选。"""
    now = time.time()
    for path in (store_root / "progress_policies").glob("*.json"):
        if _policy_due(path, now):
            return True
    return False


def _policy_due(path: Path, now: float) -> bool:
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
        return float(next_due) <= now
    except (TypeError, ValueError):
        return True


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
