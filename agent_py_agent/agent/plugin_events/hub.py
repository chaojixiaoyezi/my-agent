# LLM: 插件事件中心（M 线第一期 B3）：按 owner 分区，把宿主事件合并投给「已启用 + 清单订阅 + 握手声明」的插件。
#   投递走共用通道（B2 的 PluginChannelPool），本模块只做合并、单在途、计数与回收；事件点接线在 B4，
#   本模块只提供 publish 入口与 Gateway 组装。publish 永不阻塞、永不向调用方抛异常；发送在后台线程池里做。
# 模块用途: 实现设计第 7 节的观察投递语义（docs/design/PLUGIN_EVENT_HOOKS.md）。
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from ..plugin_channel import (
    REQUEST_TIMEOUT_SECONDS,
    ChannelCall,
    PluginChannelPool,
    PluginChannelRevoked,
    RetireScope,
)
from .protocol import (
    EVENTS_OBSERVE_METHOD,
    MAX_BATCH_EVENTS,
    EventEnvelope,
    EventFact,
    PluginEventUnavailable,
    build_event_payload,
    normalize_event_fact,
    require_events_capability,
)

logger = logging.getLogger(__name__)


# LLM: 事件中心的可注入依赖；池由调用方注入（Gateway 用 server 上的唯一共用池，测试用假客户端工厂自建）。
#   clock 是墙钟，只用于记录事件时间与最近送达时间；pool_clock 传给池（acquire 刷新 last_used、
#   close_idle 判空闲），必须与池自己的时钟同基准（默认都是 monotonic）——否则共用连接上两个
#   调用方的时间基准不一致，空闲关闭会把刚用过的连接当成过期。
# 类用途: 打包事件中心的外部依赖（安装表读取、墙钟、池时钟、后台执行器、共用池、客户端工厂）。
@dataclass(frozen=True)
class EventHubWiring:
    installations: Callable[[object], tuple | None] | None = None
    clock: Callable[[], float] = time.time
    pool_clock: Callable[[], float] = time.monotonic
    executor: ThreadPoolExecutor | None = None
    pool: PluginChannelPool | None = None
    client_factory: Callable[[object, object], object] | None = None


# LLM: 每个（owner, 插件）一个槽：记录激活代次、每类型「已纳入发送」的发布计数水位、序号。
#   序号按（owner, 插件激活）各自计数、换代从 1 重新开始，插件从序号里看不出没订阅的类型或别的 owner 发生过什么。
# 类用途: 保存一个插件的事件投递水位与序号。
@dataclass
class _Slot:
    activation_id: str = ""
    last_sent: dict = field(default_factory=dict)
    seq: int = 0


# LLM: owner 级只留每类最新一条并累计发布数；dropped_before 用「当前计数 - 上次发送水位 - 1」算，
#   所以即使合并了很多条也不需要保留历史事件对象。
# 类用途: 保存某 owner 某事件类型的最新事实与累计发布数。
@dataclass(frozen=True)
class _Latest:
    fact: EventFact
    count: int
    occurred_at: float


# LLM: 计数按（owner, 插件, 事件类型）聚合；last_error_code 只存错误码，不存错误文字或插件输出。
# 类用途: 保存一个事件类型的投递计数。
@dataclass
class _Counts:
    delivered: int = 0
    coalesced: int = 0
    failed: int = 0
    unavailable: int = 0
    last_error_code: str = ""
    last_delivered_at: float = 0.0


# LLM: 一次发送尝试的结果分类；用独立对象而不是裸字符串，让计数更新只按结构化类别分支。
# 类用途: 描述一批发送的结局（送达 / 失败 / 不可用）与错误码。
@dataclass(frozen=True)
class _Outcome:
    kind: str
    error_code: str = ""


_DELIVERED = _Outcome("delivered")
_UNAVAILABLE = _Outcome("unavailable")


# LLM: 事件投递对主流程是旁路：publish 只加锁更新待发并调度后台任务，任何内部异常都吞掉记日志。
#   按 owner 分区；多用户 Gateway 里 A 的事件永远不会投给 B 的插件（分区键是 owner 的 home_dir）。
# 类用途: 合并、调度并投递一个 Gateway 进程内所有 owner 的插件观察事件。
class PluginEventHub:
    # LLM: 池优先用注入的（Gateway 的唯一共用池）；没注入才按 client_factory 自建，且只有自建的池由本类关闭。
    #   执行器默认 4 个线程、与面板服务同档：每个（owner, 激活）的发送任务各自独立，一个插件挂住只占自己的一个线程。
    # 函数用途: 创建事件中心（不启动任何插件进程、不读安装表）。
    def __init__(self, *, wiring: EventHubWiring | None = None) -> None:
        wiring = wiring or EventHubWiring()
        self._installations = wiring.installations or _enabled_installations
        self._clock = wiring.clock
        self._pool_clock = wiring.pool_clock
        self._owns_pool = wiring.pool is None
        self._pool = wiring.pool if wiring.pool is not None else PluginChannelPool(
            client_factory=wiring.client_factory or _default_client)
        self._executor = wiring.executor or ThreadPoolExecutor(max_workers=4, thread_name_prefix="plugin-events")
        self._lock = threading.Lock()
        self._owners: dict[str, object] = {}
        self._latest: dict[str, dict[str, _Latest]] = {}
        self._slots: dict[str, dict[str, _Slot]] = {}
        self._stats: dict[str, dict[str, dict[str, _Counts]]] = {}
        self._managed: dict[str, set] = {}
        self._version: dict[str, int] = {}
        self._running: set = set()
        self._inflight: set = set()
        self._closed = False

    # LLM: 事件点（B4）只调这一个入口：归一化坏输入、更新待发、调度发送，全部在毫秒级完成；
    #   插件慢/挂/崩都只影响后台投递，不影响调用方；本方法保证不向调用方抛任何异常。
    # 函数用途: 发布一条观察事件（永不阻塞、永不抛异常）。
    def publish(self, owner, event) -> None:
        try:
            fact = normalize_event_fact(event)
            owner_key = _owner_key(owner)
            if fact is None or owner_key is None:
                return
            with self._lock:
                self._accept(owner_key, owner, fact)
        except Exception:  # noqa: BLE001 事件点不能因为投递侧的任何问题失败
            logger.warning("插件事件发布失败，已丢弃", exc_info=True)

    # LLM: 调用方必须已持有 self._lock；关闭后丢弃（停机排空阶段的发布按撤销处理，不重试）。
    # 函数用途: 把一条事件写入待发并调度投递循环（调用方已持锁）。
    def _accept(self, owner_key: str, owner, fact: EventFact) -> None:
        if self._closed:
            return
        self._owners[owner_key] = owner
        latest = self._latest.setdefault(owner_key, {})
        previous = latest.get(fact.type)
        count = (previous.count if previous is not None else 0) + 1
        latest[fact.type] = _Latest(fact=fact, count=count, occurred_at=self._clock())
        self._version[owner_key] = self._version.get(owner_key, 0) + 1
        self._schedule(owner_key)

    # LLM: 只读快照：返回值是全新字典，调用方（B6 的 /plugins info）改动它不会影响内部计数；
    #   计数只在内存里，不写盘（观察不是决定）。
    # 函数用途: 返回某 owner 的投递计数快照（插件 → 事件类型 → 六项计数）。
    def stats(self, owner_key: str) -> dict:
        with self._lock:
            source = self._stats.get(owner_key, {})
            return {plugin_id: {event_type: _counts_payload(counts)
                                for event_type, counts in types.items()}
                    for plugin_id, types in source.items()}

    # LLM: 关闭是终态：先置标记挡住新发布，再清空全部内存表；发送线程池不等待（后台任务会在取池时
    #   撞到撤销或被关闭标记拒绝，自然退出）；只有自建的池由本类关闭，注入的池归 Gateway 管。
    # 函数用途: 停止事件中心（可重复调用）。
    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._owners.clear()
            self._latest.clear()
            self._slots.clear()
            self._stats.clear()
            self._managed.clear()
            self._version.clear()
            self._running.clear()
            self._inflight.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)
        if self._owns_pool:
            self._pool.close()

    # LLM: 只在 hub 锁内调用；调度的是 owner 级的「读表 + 分发」一轮（很快，不做 I/O 发送）；
    #   发送由每（owner, 激活）独立的发送任务做，单在途由 _inflight 标记保证——
    #   同一 owner 同时最多一轮分发，在途期间新事件只更新待发，不重复提交任务。
    # 函数用途: 为某 owner 调度一轮后台分发（已在跑则不重复调度）。
    def _schedule(self, owner_key: str) -> None:
        if owner_key in self._running:
            return
        self._running.add(owner_key)
        try:
            self._executor.submit(self._drain, owner_key)
        except Exception:
            self._running.discard(owner_key)
            raise

    # LLM: 后台线程主体：只跑一轮（读表 → 回收 → 分发）；读表期间的新发布由分发锁内的收集覆盖，
    #   分发之后的新发布由发布者自己的调度覆盖（清 running 与分发在同一锁段）。异常兜底清 running，
    #   否则该 owner 的事件流会永远卡在「有循环在跑」的假象里。
    # 函数用途: 执行一轮 owner 级分发（异常兜底）。
    def _drain(self, owner_key: str) -> None:
        try:
            self._drain_once(owner_key)
        except BaseException:  # noqa: BLE001 后台线程兜底，避免静默死掉把调度标记卡住
            logger.warning("插件事件投递异常 owner=%s", owner_key, exc_info=True)
            self._finish(owner_key)

    # LLM: 一轮 = 取时间界 → 读表 → 回收 → 空闲关闭 → 锁内分发。读表失败（None）不投递也不回收，
    #   等下一次发布重新触发——绝不能把读表失败当成「没有插件」；清 running 与分发在同一锁段，
    #   保证分发出锁后的新发布一定能重新调度（原先锁内恒真的版本比较是死代码，已删）。
    # 函数用途: 执行一轮 owner 级分发（读表、回收、调度发送任务）。
    def _drain_once(self, owner_key: str) -> None:
        if self._stopped():
            self._finish(owner_key)
            return
        # 回收的时间界要在读表之前取：界之后别的调用方新建的连接一律不碰（B2 合同）。
        seq_before = self._pool.current_seq()
        owner = self._owner(owner_key)
        if owner is None:
            self._finish(owner_key)
            return
        rows = self._installations(owner)
        if rows is None:
            self._finish(owner_key)
            return
        _recycle(self, owner_key, rows, seq_before)
        _close_idle(self, owner_key)
        with self._lock:
            try:
                self._dispatch(owner_key, rows)
            finally:
                self._running.discard(owner_key)

    # LLM: 调用方必须已持有 self._lock；槽按插件身份维护（换代重置水位与序号），订阅判定只读清单声明；
    #   每个（owner, 激活）同一时间只提交一个发送任务（在途时新事件只进待发、不收集不消耗水位），
    #   收集即消耗增量（水位前进），所以失败不会无限重试。
    # 函数用途: 为有订阅、未在途、有待发的插件各收集一批并提交发送任务。
    def _dispatch(self, owner_key: str, rows: tuple) -> None:
        slots = self._slots.setdefault(owner_key, {})
        latest = self._latest.get(owner_key, {})
        for row in rows:
            declarations = getattr(row.manifest, "events", ())
            if not row.enabled or row.activation is None or not declarations:
                continue
            slot = self._slot_for(slots, row, latest)
            key = _inflight_key(owner_key, row.activation.activation_id)
            if key in self._inflight:
                continue
            batch = self._collect_slot(owner_key, row, slot, latest)
            if not batch:
                continue
            self._inflight.add(key)
            try:
                self._executor.submit(_send_batch, self, owner_key, row, batch)
            except Exception:
                self._inflight.discard(key)
                raise

    # LLM: 槽的水位基准决定 dropped_before 从哪算起：新槽（中途启用）对齐到「当前最新一条之前」，
    #   插件从当前最新一条开始收、第一条 dropped_before=0，启用之前的合并历史不计入；
    #   换代把水位对齐当前计数并重置序号，丢掉旧代待发——「代次失效就丢掉待发」的落点在这里。
    # 函数用途: 取或建立插件槽，并在新建或换代时对齐水位（换代同时把序号归零）。
    def _slot_for(self, slots: dict, row, latest: dict) -> _Slot:
        slot = slots.get(row.manifest.plugin_id)
        activation_id = row.activation.activation_id
        if slot is None:
            slot = slots[row.manifest.plugin_id] = _Slot(activation_id=activation_id)
            slot.last_sent = {event_type: entry.count - 1 for event_type, entry in latest.items()}
        elif slot.activation_id != activation_id:
            slot.activation_id = activation_id
            slot.last_sent = {event_type: entry.count for event_type, entry in latest.items()}
            slot.seq = 0
        return slot

    # LLM: 调用方必须已持有 self._lock；只收集订阅了的类型，dropped_before 用发布计数差算
    #   （只算槽建立之后被覆盖的条数），序号按（owner, 插件激活）递增、插件视角连续；
    #   正文只在该插件声明 text 时保留。
    # 函数用途: 收集一个插件的待发事件（最多 MAX_BATCH_EVENTS 条）。
    def _collect_slot(self, owner_key: str, row, slot: _Slot, latest: dict) -> list:
        batch = []
        for declaration in row.manifest.events:
            entry = latest.get(declaration.type)
            if entry is None:
                continue
            last = slot.last_sent.get(declaration.type, 0)
            if entry.count <= last:
                continue
            dropped = entry.count - last - 1
            slot.seq += 1
            slot.last_sent[declaration.type] = entry.count
            self._counts_for(owner_key, row.manifest.plugin_id, declaration.type).coalesced += dropped
            batch.append(EventEnvelope(fact=entry.fact, event_id=_event_id(), seq=slot.seq,
                                       dropped_before=dropped, occurred_at=entry.occurred_at,
                                       include_content=declaration.content == "text"))
        return batch[:MAX_BATCH_EVENTS]

    # LLM: 本方法自己拿 hub 锁（是不可重入锁，调用方不要持锁进来）；计数按批内每个事件类型各记一条。
    # 函数用途: 按结局更新一个插件一批事件的计数。
    def _bump(self, owner_key: str, plugin_id: str, batch: list, outcome: _Outcome) -> None:
        with self._lock:
            for item in batch:
                _apply_outcome(self._counts_for(owner_key, plugin_id, item.fact.type), outcome, self._clock())

    # LLM: 调用方必须已持有 self._lock；计数表按需建层，历史不随槽清理而丢。
    # 函数用途: 取得某（owner, 插件, 事件类型）的计数对象。
    def _counts_for(self, owner_key: str, plugin_id: str, event_type: str) -> _Counts:
        by_type = self._stats.setdefault(owner_key, {}).setdefault(plugin_id, {})
        return by_type.setdefault(event_type, _Counts())

    # LLM: owner 引用在发布时登记，后台线程读安装表时取用；close 后清空。
    # 函数用途: 取某 owner 的对象引用。
    def _owner(self, owner_key: str):
        with self._lock:
            return self._owners.get(owner_key)

    # LLM: 停机后不再发送（池也会拒绝新建），后台线程按轮退出。
    # 函数用途: 事件中心是否已关闭。
    def _stopped(self) -> bool:
        with self._lock:
            return self._closed

    # LLM: 只在锁内或后台线程退出路径调用；清掉调度标记，允许下一次发布重新触发。
    # 函数用途: 结束某 owner 的投递循环调度。
    def _finish(self, owner_key: str) -> None:
        with self._lock:
            self._running.discard(owner_key)


# LLM: 只回收 hub acquire 过的激活（managed 集合），有效集合用「本 owner 全部已启用激活」；
#   时间界在读表前取（调用方传入）。面板服务只回收它自己管的，事件中心同理——不碰别人的连接。
# 函数用途: 摘除 hub 管过、但已不在有效集合里的失效连接。
def _recycle(hub: PluginEventHub, owner_key: str, rows: tuple, seq_before: int) -> None:
    with hub._lock:
        managed = frozenset(hub._managed.get(owner_key, ()))
    if not managed:
        return
    valid = {row.activation.activation_id for row in rows if row.enabled and row.activation is not None}
    hub._pool.retire_stale(owner_key, valid, RetireScope(managed=managed, created_before=seq_before))


# LLM: 空闲关闭原先只在有人查面板时才跑，只用飞书、不开 TUI 的 owner 的事件插件进程会一直开着；
#   这里每一轮顺手关 hub 自己管的空闲连接（只关客户端、保留连接条目，下次发送会重建）；
#   在途（含已提交未开跑的发送任务）的连接不关——busy 标记来自 hub._inflight。
# 函数用途: 对 hub 管过的事件连接做一次空闲关闭。
def _close_idle(hub: PluginEventHub, owner_key: str) -> None:
    with hub._lock:
        managed = frozenset(hub._managed.get(owner_key, ()))
        busy = {key for key in hub._inflight if key[0] == owner_key}
    if not managed:
        return
    for key in hub._pool.owner_keys(owner_key):
        if key[1] in managed:
            hub._pool.close_idle(key, hub._pool_clock(), key in busy)


# LLM: 每次发送前先 acquire（用池时钟刷新 last_used，B2 的四步用法），再发一批；发送结果只影响计数：
#   不可用（握手没声明）按 unavailable、撤销不计失败（事件因停用被丢弃）、其它异常按 failed，
#   退避与否由通道按连接级/请求级自己判定，hub 不重试。发送任务按（owner, 激活）独立调度，
#   慢/挂/崩的插件只占自己的线程，不阻塞同 owner 或其它 owner 的插件。
# 函数用途: 把一批事件发给一个插件并记账。
def _send(hub: PluginEventHub, owner_key: str, row, batch: list) -> None:
    owner = hub._owner(owner_key)
    if owner is None:
        return
    call = ChannelCall(owner=owner, method=EVENTS_OBSERVE_METHOD,
                       params={"events": [_payload(item) for item in batch]},
                       timeout=REQUEST_TIMEOUT_SECONDS, before_send=require_events_capability)
    try:
        connection = hub._pool.acquire(owner_key, row, hub._pool_clock())
        _mark_managed(hub, owner_key, row.activation.activation_id)
        hub._pool.request(connection, call)
    except PluginEventUnavailable:
        hub._bump(owner_key, row.manifest.plugin_id, batch, _UNAVAILABLE)
    except PluginChannelRevoked:
        return
    except Exception as exc:  # noqa: BLE001 插件故障只记这次失败，不影响其它插件或主流程
        hub._bump(owner_key, row.manifest.plugin_id, batch, _Outcome("failed", getattr(exc, "code", "")))
    else:
        hub._bump(owner_key, row.manifest.plugin_id, batch, _DELIVERED)


# LLM: 后台发送任务：发送前先取版本快照；发送期间有新发布就在收尾时再触发一轮分发，
#   该插件的新待发不会漏。收尾先清在途标记，之后同一插件才能被再次调度。
# 函数用途: 发送一批事件并在收尾清在途标记（必要时再调度一轮分发）。
def _send_batch(hub: PluginEventHub, owner_key: str, row, batch: list) -> None:
    key = _inflight_key(owner_key, row.activation.activation_id)
    with hub._lock:
        version = hub._version.get(owner_key, 0)
    try:
        _send(hub, owner_key, row, batch)
    finally:
        _finish_send(hub, owner_key, key, version)


# LLM: 调用方不要持锁；清在途标记与「按需再调度」在同一锁段完成——否则标记清掉后、
#   再调度前来的新发布会被发布者的 running 检查吞掉。
# 函数用途: 发送任务的收尾（清在途标记；发送期间有新发布则再触发一轮分发）。
def _finish_send(hub: PluginEventHub, owner_key: str, key: tuple, version: int) -> None:
    with hub._lock:
        hub._inflight.discard(key)
        if hub._version.get(owner_key, 0) != version:
            _reschedule(hub, owner_key)


# LLM: 调用方必须已持有 hub._lock；调度失败只记日志（在途标记已清，下一次发布还会触发），
#   不能把发送任务的收尾变成异常。
# 函数用途: 在锁内安全地再触发一轮分发。
def _reschedule(hub: PluginEventHub, owner_key: str) -> None:
    try:
        hub._schedule(owner_key)
    except Exception:  # noqa: BLE001 调度失败不影响发送结果
        logger.warning("插件事件再调度失败 owner=%s", owner_key, exc_info=True)


# LLM: managed 是「hub 建过连接」的激活集合，只增不减（每 owner 有界）；回收只看它，
#   保证事件中心绝不摘面板服务或其它调用方的连接。
# 函数用途: 记下 hub acquire 过的激活。
def _mark_managed(hub: PluginEventHub, owner_key: str, activation_id: str) -> None:
    with hub._lock:
        hub._managed.setdefault(owner_key, set()).add(activation_id)


# LLM: 默认安装表读取只取启用且有激活代次的记录；表不可读时返回 None（不是空元组），
#   让调用方能区分「读表失败」与「确实没有启用的插件」——失败时绝不能当成空集合去停别人的连接。
# 函数用途: 读取 owner 当前启用的插件；读表失败时返回 None。
def _enabled_installations(owner) -> tuple | None:
    from ..plugin_install_store import PluginInstallStore

    try:
        return tuple(row for row in PluginInstallStore(owner).snapshot()
                     if row.enabled and row.activation is not None)
    except (OSError, ValueError):
        logger.warning("插件安装表读取失败，本轮不投递也不回收")
        return None


# LLM: 默认客户端与面板服务同一个插件客户端类型（延迟导入避免包初始化期的循环依赖）；
#   Gateway 路径一律注入共用池，不会走到这个默认工厂。
# 函数用途: 为一个固定激活创建插件客户端（不启动进程）。
def _default_client(owner, installation):
    from ..plugin_runtime import PluginMCPClient

    return PluginMCPClient(owner, installation)


# LLM: owner 分区键用 owner home 的字符串形式（与插件命令、面板服务的口径一致）；
#   拿不到 home 就丢弃这条事件，不猜其它身份字段。
# 函数用途: 把 owner 对象归一成分区键；无效时返回 None。
def _owner_key(owner) -> str | None:
    home = getattr(owner, "home_dir", None)
    text = str(home) if home is not None else ""
    return text or None


# LLM: 发送在途的粒度是（owner, 激活）：换代后的新激活与旧代的在途任务互不影响，
#   同一 owner 的不同插件也各发各的（一个挂住不拖累另一个）——设计第 7 节「每个插件一个在途」的落点。
# 函数用途: 生成一个发送在途标记键。
def _inflight_key(owner_key: str, activation_id: str) -> tuple:
    return (owner_key, activation_id)


# LLM: 公共字段组装只发生在发送阶段；dropped_before 与 seq 已在收集阶段算好。
# 函数用途: 把一条待发事件转成线上 payload。
def _payload(envelope: EventEnvelope) -> dict:
    return build_event_payload(envelope)


# LLM: 快照字段与设计第 7 节一一对应；新增字段时同步 B6 的展示与合同测试。
# 函数用途: 把计数对象转成只读字典。
def _counts_payload(counts: _Counts) -> dict:
    return {"delivered": counts.delivered, "coalesced": counts.coalesced, "failed": counts.failed,
            "unavailable": counts.unavailable, "last_error_code": counts.last_error_code,
            "last_delivered_at": counts.last_delivered_at}


# LLM: 计数更新按结构化类别分支；delivered 记送达时间，failed 记错误码，unavailable 只计数
#   （原因由计数本身表达，不写错误码）。
# 函数用途: 按一次发送的结局更新单个（插件, 事件类型）的计数。
def _apply_outcome(counts: _Counts, outcome: _Outcome, now: float) -> None:
    if outcome.kind == "delivered":
        counts.delivered += 1
        counts.last_delivered_at = now
        return
    if outcome.kind == "unavailable":
        counts.unavailable += 1
        return
    counts.failed += 1
    if outcome.error_code:
        counts.last_error_code = outcome.error_code


# LLM: 事件 ID 只要求进程内唯一、跨进程不冲突；用随机 hex 而不是计数器，避免多进程重号。
# 函数用途: 生成一个事件编号。
def _event_id() -> str:
    return "ev-" + uuid.uuid4().hex
