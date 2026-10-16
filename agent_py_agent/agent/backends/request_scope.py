# LLM: 这里只保存进程内请求资源事实；不保存用户正文、凭据或任务状态，不代替调度/权限账。
# 模块用途: 统一端点前台占用和物理请求名额；排队只存不透明 owner 和资源事实，不参与授权或工具执行。
from __future__ import annotations

import contextvars
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit

_LOCK = threading.Lock()
_FOREGROUND: dict[tuple[str, str, int | None], int] = {}
# 与前台登记共用端点身份和锁；这里仅登记物理请求，绝不把整轮前台占用当作名额。
_INFLIGHT: dict[tuple[str, str, int | None], _EndpointQueue] = {}
# GC 可在锁内分配时触发；只原子追加票据，下一次申请/归还在安全的临界区入口排空，不重入业务状态。
_DEFERRED_RELEASES: deque[tuple[tuple[str, str, int | None], _QueueTicket]] = deque()
# 两分钟给正常拥塞排空机会，但不允许线程无限等；调用方绝对期限更短时不能滚动延长。
PROVIDER_QUEUE_TIMEOUT_SECONDS = 120.0
# 两秒以内不刷屏，之后每次排队只报告一次；收到名额或退出时报告解除。
PROVIDER_QUEUE_NOTICE_SECONDS = 2.0
# 全进程最多 1024 个等待者，约为默认 500 执行槽的两倍，防止多端点队列无界增长。
PROVIDER_QUEUE_MAX_WAITER_COUNT = 1024
# 在途端点数量也有界；空端点立即删除，不累计历史端点。
PROVIDER_QUEUE_MAX_ENDPOINT_COUNT = 128
_OWNER: contextvars.ContextVar[str] = contextvars.ContextVar("provider_owner", default="")


# LLM: 身份必须由宿主结构化 home_paths 绑定，空身份共用未知桶；此标签不是访问授权。
# 函数用途: 为主子或辅助调用设置公平排队身份，异常退出也恢复上一层。
@contextmanager
def provider_owner_scope(owner: str) -> Iterator[None]:
    token = _OWNER.set(str(owner or ""))
    try:
        yield
    finally:
        _OWNER.reset(token)


# LLM: 票据只保存资源调度字段；不得增加输入正文、凭据、backend 或 Agent 引用。
# 类用途: 记录一个 owner 内先来先到的请求及其唤醒通知。
@dataclass(eq=False)
class _QueueTicket:
    owner: str
    wake: threading.Event = field(default_factory=threading.Event)
    granted: bool = False
    released: bool = False


# LLM: 状态只在 request_scope._LOCK 下改变；额度冲突取较小值直到端点排空，避免跨 owner 配置抬高上限。
# 类用途: 保存同一端点在途数量和各用户的先来先到队列。
@dataclass
class _EndpointQueue:
    limit: int
    active: int = 0
    owners: dict[str, deque[_QueueTicket]] = field(default_factory=dict)
    last_owner: str = ""


# LLM: lease 只代表一次物理发送，调用方负责在响应关闭后 release；GC 仅为丢弃响应的最后防线。
# 类用途: 管理一次进程内物理请求名额，关闭、取消和异常出口共用幂等归还接口。
class ProviderLease:
    # LLM: key/ticket 均由同一权威申请入口生成，不持有请求载荷或凭据。
    # 函数用途: 保存已授予的本次物理请求名额。
    def __init__(self, key, ticket: _QueueTicket):
        self.key, self.ticket = key, ticket

    # LLM: 所有成功、异常、取消、close/GC 出口复用此入口；锁内标记使重复归还不减两次。
    # 函数用途: 归还本请求名额并立即唤醒下一个公平等待者。
    def release(self) -> None:
        _drop_ticket(self.key, self.ticket)

    # LLM: GC 可能在本线程持非重入锁时调用；绝不等待锁，延迟票据不能引用 lease 或形成自引用环。
    # 函数用途: 非阻塞归还丢弃名额，竞争时留给下次安全锁入口处理。
    def __del__(self):
        if not self.ticket.released:
            _finalize_ticket(self.key, self.ticket)


# LLM: 默认零在任何锁/身份/等待操作前返回；申请失败只撤本票据，授予与取消交错也不能丢名额。
# 函数用途: 为一次物理请求申请可停止的公平名额，排队到期抛暂时性错误。
def acquire_provider_request(request: object, notify=None) -> ProviderLease | None:
    limit = max(0, int(getattr(request, "provider_max_concurrent_requests", 0)))
    if limit == 0:
        return None
    key = _endpoint_key(request)
    if key is None:
        return None
    ticket = _QueueTicket(_OWNER.get())
    _enqueue_ticket(key, ticket, limit)
    try:
        deadline = time.monotonic() + PROVIDER_QUEUE_TIMEOUT_SECONDS
        deadlines = (deadline, _DEADLINE.get(), getattr(request, "deadline", None))
        _wait_for_ticket(ticket, notify, min(value for value in deadlines if value is not None))
        return ProviderLease(key, ticket)
    except BaseException:
        _drop_ticket(key, ticket)
        raise


# LLM: 只给诊断/测试返回数量，不导出其它 owner 的身份；非授权或调度依据。
# 函数用途: 读取指定端点的在途与排队数量，空端点返回零。
def provider_endpoint_counts(request: object) -> tuple[int, int]:
    key = _endpoint_key(request)
    with _LOCK:
        state = _INFLIGHT.get(key)
        return (state.active, _waiter_count(state)) if state else (0, 0)


# LLM: 计数只在本模块锁保护下使用；所有 owner 队列都是有界的票据引用。
# 函数用途: 合计当前端点尚未授予的请求数。
def _waiter_count(state: _EndpointQueue) -> int:
    return sum(len(queue) for queue in state.owners.values())


# LLM: 只抛 transient 子类，不误归额度用尽或配置错误，不含其它用户信息。
# 函数用途: 用统一结构化错误报告排队到期或队列容量耗尽。
def _queue_timeout() -> BaseException:
    from .errors import ProviderQueueTimeoutError

    return ProviderQueueTimeoutError("模型接口排队等待超过上限或队列已满，请稍后继续；此前工具不会重放。")


# LLM: 锁内同时登记/授予；资源上限在首次建立前检查，拒绝不残留空状态。
# 函数用途: 将本请求加入对应用户队列，有空名额则立即公平授予。
def _enqueue_ticket(key, ticket: _QueueTicket, limit: int) -> None:
    with _LOCK:
        _drain_deferred_releases()
        state = _INFLIGHT.get(key)
        if state is None and len(_INFLIGHT) >= PROVIDER_QUEUE_MAX_ENDPOINT_COUNT:
            raise _queue_timeout()
        state = state or _EndpointQueue(limit)
        state.limit = min(state.limit, limit)
        waiting = sum(_waiter_count(row) for row in _INFLIGHT.values())
        if state.active >= state.limit and waiting >= PROVIDER_QUEUE_MAX_WAITER_COUNT:
            raise _queue_timeout()
        _INFLIGHT[key] = state
        state.owners.setdefault(ticket.owner, deque()).append(ticket)
        _grant_waiters(state)


# LLM: 调用方必须持唯一锁；同 owner 内 pop 左端，上一 owner 仍有积压时优先让另一 owner 接棒。
# 函数用途: 按用户轮转填满可用名额，并唤醒获准的请求线程。
def _grant_waiters(state: _EndpointQueue) -> None:
    while state.owners and state.active < state.limit:
        owner = next((owner for owner in state.owners if owner != state.last_owner), next(iter(state.owners)))
        queue = state.owners.pop(owner)
        ticket = queue.popleft()
        if queue:
            state.owners[owner] = queue
        state.last_owner = owner
        state.active += 1
        ticket.granted = True
        ticket.wake.set()


# LLM: granted 与 released 在同一锁下裁决；先排空 GC 延迟归还，再撤销本票据，不触碰其它 owner 权限。
# 函数用途: 撤销等待或归还占用，端点无活动无排队时删除整个状态。
def _drop_ticket(key, ticket: _QueueTicket) -> None:
    with _LOCK:
        _drain_deferred_releases()
        _drop_ticket_locked(key, ticket)


# LLM: 调用方已持唯一锁，正常归还与 GC 共用此幂等实现；released 防止重复延迟票据多减。
# 函数用途: 在安全临界区中归还或撤回一张票据并接棒下一用户。
def _drop_ticket_locked(key, ticket: _QueueTicket) -> None:
    if ticket.released:
        return
    ticket.released = True
    state = _INFLIGHT[key]
    if ticket.granted:
        state.active -= 1
    else:
        _remove_waiter(state, ticket)
    _grant_waiters(state)
    if not state.active and not state.owners:
        _INFLIGHT.pop(key)


# LLM: 只在临界区入口调用；CPython deque 追加/弹出原子，竞争的析构最多留待下次，不重入未完成修改。
# 函数用途: 排空此前因锁竞争延期归还的资源票据。
def _drain_deferred_releases() -> None:
    while _DEFERRED_RELEASES:
        _drop_ticket_locked(*_DEFERRED_RELEASES.popleft())


# LLM: 析构绝不阻塞或改成 RLock；锁不可得时只原子追加，仍由同一锁内出口幂等裁决。
# 函数用途: 尽快归还 GC 丢弃票据，或把归还延期到下个安全锁入口。
def _finalize_ticket(key, ticket: _QueueTicket) -> None:
    if not _LOCK.acquire(blocking=False):
        _DEFERRED_RELEASES.append((key, ticket))
        return
    try:
        _drain_deferred_releases()
        _drop_ticket_locked(key, ticket)
    finally:
        _LOCK.release()


# LLM: 仅在本模块锁内调用；按票据 identity 删除，不能按 owner 标签删除同用户的其它请求。
# 函数用途: 从对应用户先来先到队列撤回一条已取消或到期的等待者。
def _remove_waiter(state: _EndpointQueue, ticket: _QueueTicket) -> None:
    queue = state.owners[ticket.owner]
    queue.remove(ticket)
    if not queue:
        state.owners.pop(ticket.owner)


# LLM: 复用线程中断的即时 Event 唤醒，不等待慢连接关闭回调；显示回调在锁外且失败不反噬请求。
# 函数用途: 等待公平名额，超两秒通知排队；停止、超时和授予都发出明确解除状态。
def _wait_for_ticket(ticket: _QueueTicket, notify, deadline: float) -> None:
    from ..concurrency.interrupt import register_interrupt_wakeup

    with register_interrupt_wakeup(ticket.wake):
        _poll_ticket(ticket, notify, deadline)


# LLM: 停止和授予均直接置位 Event；只在一次提示或绝对期限到达时额外唤醒，不作高频轮询。
# 函数用途: 以事件等待已登记票据并只发布一次排队提示，退出不丢失解除状态。
def _poll_ticket(ticket: _QueueTicket, notify, deadline: float) -> None:
    started = time.monotonic()
    announced = False
    phase = "cancelled"
    try:
        while not _ticket_ready(ticket, deadline):
            elapsed = time.monotonic() - started
            announced = _announce_wait(notify, elapsed, announced)
            next_notice = deadline if announced else started + PROVIDER_QUEUE_NOTICE_SECONDS
            ticket.wake.wait(max(0.001, min(deadline, next_notice) - time.monotonic()))
        phase = "admitted"
    finally:
        if announced:
            _queue_notice(notify, phase, time.monotonic() - started)


# LLM: 排队展示不是资源裁决；只能在锁外调用，一张票据只公开一次 waiting。
# 函数用途: 达到两秒门槛后发布提示并保留已通知事实，避免等待循环深嵌套。
def _announce_wait(notify, elapsed: float, announced: bool) -> bool:
    if announced or elapsed < PROVIDER_QUEUE_NOTICE_SECONDS:
        return announced
    _queue_notice(notify, "waiting", elapsed)
    return True


# LLM: 停止优先于同刻授予；名额事实在锁内读取，清 Event 后再检查停止封住即时唤醒交错。
# 函数用途: 检查本等待者的停止、不可延长的绝对期限和授予；迟到授予也必须撤回。
def _ticket_ready(ticket: _QueueTicket, deadline: float) -> bool:
    from ..concurrency.interrupt import is_interrupted

    if is_interrupted():
        raise InterruptedError("模型接口排队已被用户停止")
    with _LOCK:
        if time.monotonic() >= deadline:
            raise _queue_timeout()
        if ticket.granted:
            return True
        ticket.wake.clear()
    if is_interrupted():
        raise InterruptedError("模型接口排队已被用户停止")
    return False


# LLM: 白名单事件只含本次等待事实，复用 admission_wait_reason 名称；不泄露其它 owner、地址或正文。
# 函数用途: 在锁外发布可供 TUI/IM 状态显示的排队通知，忽略观测异常。
def _queue_notice(notify, phase: str, elapsed: float) -> None:
    if not callable(notify):
        return
    try:
        notify({"phase": phase, "admission_wait_reason": "provider_concurrency", "wait_seconds": round(elapsed, 3)})
    except Exception:
        pass


# LLM: 响应只接管本次物理 lease；无 lease（默认关闭、旧替身）不加锁、不创建新资源。
# 函数用途: 响应关闭时幂等归还其名额，供 HTTP 与 WebSocket 共用。
def release_provider_response(response: object) -> None:
    lease = getattr(response, "_provider_request_lease", None)
    if isinstance(lease, ProviderLease):
        lease.release()
_DEADLINE: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "provider_request_deadline", default=None,
)


# LLM: 资源身份取真实后端的 HTTP origin；不按模型名、用户或 localhost 字样猜测服务器。
# 函数用途: 为同 Gateway 内共用模型端点的请求生成无凭据资源键；代理别名不自动合并。
def _endpoint_key(backend: object) -> tuple[str, str, int | None] | None:
    try:
        parts = urlsplit(str(getattr(backend, "api_base", "") or ""))
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return None
        return parts.scheme, parts.hostname.lower(), parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return None


# LLM: 占用覆盖完整 agent.run（包括工具间隙/Compact），finally 清理；不串行化任何前台任务。
# 函数用途: 登记正在使用某模型端点的主/子代理工作片，供低优先级维护延后启动。
@contextmanager
def foreground_model_scope(backend: object) -> Iterator[None]:
    key = _endpoint_key(backend)
    if key is not None:
        with _LOCK:
            _FOREGROUND[key] = _FOREGROUND.get(key, 0) + 1
    try:
        yield
    finally:
        if key is not None:
            with _LOCK:
                remaining = _FOREGROUND[key] - 1
                if remaining:
                    _FOREGROUND[key] = remaining
                else:
                    _FOREGROUND.pop(key, None)


# LLM: 只读取进程内活动事实，不扫描 owner 历史；已开始的后台请求不在此处强杀。
# 函数用途: 判断此后端的服务器是否正被前台工作片使用。
def foreground_model_active(backend: object) -> bool:
    key = _endpoint_key(backend)
    with _LOCK:
        return key is not None and _FOREGROUND.get(key, 0) > 0


# LLM: 预算是 request-local 的绝对 deadline；嵌套预算只能缩短，不修改共享 backend 配置。
# 函数用途: 为一次有外层预算的后台调用设置 HTTP 可用时长，退出后恢复原预算。
@contextmanager
def provider_request_budget(seconds: float) -> Iterator[None]:
    deadline = time.monotonic() + max(0.0, float(seconds))
    previous = _DEADLINE.get()
    token = _DEADLINE.set(min(previous, deadline) if previous is not None else deadline)
    try:
        yield
    finally:
        _DEADLINE.reset(token)


# LLM: 无预算时原样保留默认值；有预算时使用剩余墙钟时长，避免固定 240s 抢先截断 720s 调用。
# 函数用途: 把调用方授权的剩余等待时长传入单次 HTTP 信封。
def provider_request_timeout(default: float) -> float:
    deadline = _DEADLINE.get()
    if deadline is None:
        return default
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("模型调用预算已耗尽")
    return remaining
