# LLM: 队列worker沿原claim/heartbeat/complete权威工作；停止只撤销新领取并按单一总deadline等待，不伪造完成。
# 模块用途: 管理并发消费者、持续在途心跳和可再次等待的结构化排空结果。
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from agent_py_agent.agent.ingress_queue import ClaimedMessage, IngressQueue

Handler = Callable[[dict], None]
_LOGGER = logging.getLogger(__name__)


# LLM: 只投影实际alive线程及同一monotonic截止时刻，不把停止请求视为持久任务完成。
# 类用途: 明确区分已排空与仍有在途线程的停止结果。
@dataclass(frozen=True)
class DrainReport:
    drained: bool
    unfinished: int
    deadline_monotonic: float
    in_flight: tuple[dict, ...] = ()

    # LLM: 返回结构化机器事实，无handler正文或凭据。
    # 函数用途: 供进程入口记录排空状态和非零退出原因。
    def to_dict(self) -> dict:
        return {'status': 'drained' if self.drained else 'unfinished',
                'drained': self.drained, 'unfinished': self.unfinished,
                'deadline_monotonic': self.deadline_monotonic, 'in_flight': list(self.in_flight)}


# LLM: 每条claim维持独立心跳直到handler及队列提交完成；stop事件不能覆盖Thread内部_stop方法。
# 类用途: 防止长任务在排空期间丢租约被重复领取。
class _Heartbeat(threading.Thread):
    # LLM: 构造不发请求，run才沿原队列租约续租。
    # 函数用途: 绑定本条消息的claim token及租约周期。
    def __init__(self, queue: IngressQueue, claim_token: str, lease_seconds: int) -> None:
        super().__init__(daemon=True)
        self._queue = queue
        self._token = claim_token
        self._lease = lease_seconds
        self._stop_event = threading.Event()

    # LLM: 瞬时续租异常仍沿原重试节拍，不宣称租约成功，也不停止handler。
    # 函数用途: 在任务执行期间周期续租。
    def run(self) -> None:
        interval = max(1.0, self._lease / 2)
        while not self._stop_event.wait(interval):
            try:
                self._queue.heartbeat(self._token, lease_seconds=self._lease)
            except Exception:
                pass

    # LLM: 只中断心跳等待，调用方仍可join核验物理退出。
    # 函数用途: 在任务完成或失败提交后结束心跳。
    def stop(self) -> None:
        self._stop_event.set()


# LLM: 原领取、业务执行和持久完成/失败顺序不变，stop不取消在途handler。
# 类用途: 处理一条消息并在退出请求后停止领取新消息。
class QueueWorker:
    # LLM: 只保存依赖，启动仍由run_forever或run_once显式进行。
    # 函数用途: 准备一个队列消费者。
    def __init__(self, queue: IngressQueue, handler: Handler, *, lease_seconds: int = 120, poll_interval: float = 0.5) -> None:
        self._queue = queue
        self._handler = handler
        self._lease = lease_seconds
        self._poll = poll_interval
        self._stop = threading.Event()
        self._gate = threading.Lock()
        self._pending: dict | None = None
        self._executing = False
        self._deadline: float | None = None
        self._heartbeat: _Heartbeat | None = None

    # LLM: 只复制本消费者从原队列取得的消息ID/attempt及阶段，不把payload或claim token放进日志。
    # 函数用途: 让停止报告看到尚未确认落账的责任。
    @property
    def in_flight(self) -> dict | None:
        with self._gate:
            if (self._pending is not None and self._pending['phase'] == 'heartbeat_unfinished'
                    and self._heartbeat is not None and not self._heartbeat.is_alive()):
                self._pending = None
            return dict(self._pending) if self._pending is not None else None

    # LLM: 已确认任务的心跳清理沿当前池等待预算收尾；绝不清除settlement_unknown责任。
    # 函数用途: 后续显式等待能观察上次超时后完成的心跳退出。
    def wait_cleanup(self, deadline: float) -> None:
        with self._gate:
            heartbeat = self._heartbeat if self._pending is not None and self._pending['phase'] == 'heartbeat_unfinished' else None
        if heartbeat is not None and heartbeat.is_alive():
            heartbeat.join(timeout=max(0.0, deadline - time.monotonic()))

    # LLM: 线程死亡不能清除pending；这里只区分当前是否仍在本次执行内，供停止结果去重计数。
    # 函数用途: 返回是否正在领取、处理或确认任务。
    @property
    def executing(self) -> bool:
        with self._gate:
            return self._executing

    # LLM: 成功领取后必须沿_process执行原完成/失败写入，空队列不产生任务状态；未知结果立即脱敏日志，不等stop/wait。
    # 函数用途: 领取并处理最多一条消息。
    def run_once(self) -> bool:
        with self._gate:
            if self._stop.is_set() or self._pending is not None:
                return False
            self._pending = {'message_id': None, 'phase': 'claiming'}
            self._executing = True
        # claim准入在同一短门内先于stop线性化；已准入但尚未返回的DB调用属于在途，不持门做IO。
        try:
            msg = self._queue.claim(lease_seconds=self._lease)
            if msg is None:
                with self._gate:
                    self._pending = None
                return False
            with self._gate:
                self._pending = {'message_id': msg.id, 'attempt': msg.attempts, 'phase': 'processing'}
            self._process(msg)
            if self.in_flight is not None:
                self._log_pending('UnconfirmedAcknowledgment')
            return True
        except Exception as exc:
            with self._gate:
                self._pending = {**(self._pending or {}), 'phase': 'settlement_unknown', 'error_type': type(exc).__name__}
                self._stop.set()
            self._log_pending(type(exc).__name__)
            return True
        finally:
            with self._gate:
                self._executing = False

    # LLM: 只记录类型/阶段/消息ID与attempt；不含payload、claim token、异常正文或traceback，不改变持久确认事实。
    # 函数用途: 在无人等待的后台消费者退出前输出机器可读故障诊断。
    def _log_pending(self, error_type: str) -> None:
        pending = self.in_flight or {}
        _LOGGER.error('queue_worker_settlement_unknown', extra={
            'event': 'queue_worker_settlement_unknown', 'phase': pending.get('phase'),
            'message_id': pending.get('message_id'), 'attempt': pending.get('attempt'),
            'error_type': error_type})

    # LLM: 心跳覆盖handler与complete/fail；finally等待心跳真正退出，池的alive判断包含这段清理。
    # 函数用途: 执行一条消息并提交原队列结果。
    def _process(self, msg: ClaimedMessage) -> None:
        hb = _Heartbeat(self._queue, msg.claim_token, self._lease)
        self._heartbeat = hb
        confirmed = False
        try:
            hb.start()
            try:
                self._handler(msg.payload)
            except Exception as exc:
                confirmed = self._queue.fail(msg.claim_token, error=f'{type(exc).__name__}: {exc}') is True
            else:
                # 完成确认失败是未知结果，绝不将已成功的handler重新归为业务失败。
                confirmed = self._queue.complete(msg.claim_token) is True
        finally:
            hb.stop()
            if hb.ident is not None:
                hb.join(timeout=None if self._deadline is None else max(0.0, self._deadline - time.monotonic()))
            with self._gate:
                if confirmed and not hb.is_alive():
                    self._pending = None
                else:
                    self._pending = {**(self._pending or {}), 'phase': 'heartbeat_unfinished' if confirmed else 'settlement_unknown'}
                    self._stop.set()

    # LLM: stop只在两次领取之间生效；当前handler仍由心跳保护直到持久提交。
    # 函数用途: 持续处理消息，空闲时可立即被停止事件唤醒。
    def run_forever(self) -> None:
        while not self._stop.is_set():
            if not self.run_once():
                self._stop.wait(self._poll)

    # LLM: 不写complete或fail，不丢弃在途claim。
    # 函数用途: 请求消费者停止领取新活。
    def stop(self, *, deadline: float | None = None) -> None:
        with self._gate:
            self._stop.set()
            if deadline is not None:
                self._deadline = deadline


# LLM: 线程引用是本池的在途事实源；一个总deadline覆盖全部join，超时保留引用供后续等待。
# 类用途: 并发消费原队列并如实报告停止是否排空。
class WorkerPool:
    # LLM: 不创建后台线程，所有worker共享同一原队列。
    # 函数用途: 配置消费者数量与租约。
    def __init__(self, queue: IngressQueue, handler: Handler, *, workers: int = 2, lease_seconds: int = 120) -> None:
        self._pool = [QueueWorker(queue, handler, lease_seconds=lease_seconds) for _ in range(max(1, workers))]
        self._threads: list[threading.Thread] = []
        self._worker_threads: dict[QueueWorker, threading.Thread] = {}

    # LLM: 启动后台消费者；已有在途线程时禁止重复启动另一批消费者。
    # 函数用途: 显式启动一次线程池。
    def start(self) -> None:
        if any(thread.is_alive() for thread in self._threads):
            raise RuntimeError('worker pool already started')
        for worker in self._pool:
            thread = threading.Thread(target=worker.run_forever, daemon=True)
            thread.start()
            self._threads.append(thread)
            self._worker_threads[worker] = thread

    # LLM: deadline可由进程层传入以共享总预算；超时不取消handler、不停其心跳、不清空活线程。
    # 函数用途: 请求停止领取，并在一份总预算内等待所有消费者。
    def stop(self, timeout: float = 5.0, *, deadline: float | None = None) -> DrainReport:
        until = time.monotonic() + max(0.0, timeout) if deadline is None else deadline
        for worker in self._pool:
            worker.stop(deadline=until)
        for thread in self._threads:
            thread.join(timeout=max(0.0, until - time.monotonic()))
        for worker in self._pool:
            worker.wait_cleanup(until)
        self._threads = [thread for thread in self._threads if thread.is_alive()]
        pending = [(worker, value) for worker in self._pool if (value := worker.in_flight) is not None]
        unknown_dead = sum(worker not in self._worker_threads or not self._worker_threads[worker].is_alive() for worker, value in pending)
        unfinished = len(self._threads) + unknown_dead
        return DrainReport(unfinished == 0, unfinished, until, tuple(value for worker, value in pending))


# LLM: 周期回收仍沿原recover_stale/purge_old，退出事件不冒充任务终态，也不与Thread内部方法重名。
# 类用途: 回收过期租约与到期终态队列行。
class StaleReaper(threading.Thread):
    # LLM: 不做队列IO，启动后按原周期执行维护。
    # 函数用途: 准备租约回收及保留期参数。
    def __init__(self, queue: IngressQueue, *, interval: float = 30.0, purge_interval: float = 3600.0, retention_days: float = 7.0) -> None:
        super().__init__(daemon=True)
        self._queue = queue
        self._interval = interval
        self._purge_every = max(1, round(purge_interval / max(1.0, interval)))
        self._retention_ms = int(retention_days * 86_400_000)
        self._stop_event = threading.Event()
        self._tick = 0

    # LLM: 维护错误不杀死本线程，下一节拍继续；不隐式更改原队列状态机。
    # 函数用途: 周期运行队列维护直到明确停止。
    def run(self) -> None:
        while not self._stop_event.wait(self._interval):
            self._tick += 1
            self._safe_recover()
            if self._tick % self._purge_every == 0:
                self._safe_purge()

    # LLM: 只调用原恢复方法，瞬时异常留待下一拍，不报告恢复成功。
    # 函数用途: 安全尝试一次过期租约回收。
    def _safe_recover(self) -> None:
        try:
            self._queue.recover_stale()
        except Exception:
            pass

    # LLM: 原保留期限与删除权威不变，失败不伪造已清理数量。
    # 函数用途: 安全尝试一次过期终态清理。
    def _safe_purge(self) -> None:
        try:
            self._queue.purge_old(retention_ms=self._retention_ms)
        except Exception:
            pass

    # LLM: 仅设置线程退出事件，调用方必须join判断是否结束。
    # 函数用途: 停止后续维护节拍。
    def stop(self) -> None:
        self._stop_event.set()
