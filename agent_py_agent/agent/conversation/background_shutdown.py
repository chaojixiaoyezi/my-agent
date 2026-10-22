# LLM: 本模块只运输同一Gateway提交片的原claim/worker引用；中断不取消TaskLink或资源，核收只读原账，未知保持阻塞。
# 模块用途: 把后台执行片的准确停止句柄交回宿主收尾，避免只等调度循环就声称排空。
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar

from ..concurrency.interrupt import _ThreadExitObservation, interrupt_by_name
from ..runtime_db.operations import (
    AGENT_RUN_TERMINAL_STATUSES,
    ATTEMPT_STATUS_UNKNOWN,
    RUN_STATUS_LEGACY_CREATED,
    attempt_status_is_terminal,
)
from ..runtime_db.repository import RuntimeRepository
from .run_claim import conversation_claim_interrupt_name

_CURRENT: ContextVar[BackgroundSlice | None] = ContextVar('background_shutdown_slice', default=None)
# 停止原已提交后台片的有界等待秒数；超时保留未确认责任，不取消长期任务或宣称排空。
SHUTDOWN_WAIT_SECONDS = 2.0


# LLM: 只保原宿主已提交job中的确切claim和出生前worker观察，不能从task/latest投影生成中断对象。
# 类用途: 保存一个后台片的停止事实和原执行引用，不成为任务状态或恢复许可。
class BackgroundSlice:
    # LLM: repo由原scheduler实际agent取得；None只表示本片尚未发布RuntimeDB身份，不建立替代库。
    # 函数用途: 建立一次提交片的进程内观察，尚未领取claim时也记住早到停止。
    def __init__(self, owner, repo):
        self.owner = owner
        self.repo = repo
        self.claims = []
        self.workers = []
        self.finished = False

    # LLM: 调用点必须已登记同claim的interruptible；关闭门后即时中断并拒新片，不触碰后继claim。
    # 函数用途: 保存原领取句柄，让宿主停止只命中这一片。
    def bind_claim(self, claim):
        with self.owner.lock:
            self.claims.append(claim)
            stopping = self.owner.stopping
        if stopping:
            interrupt_by_name(conversation_claim_interrupt_name(claim.claim_id))
            raise InterruptedError('Gateway正在停止本次后台执行片')

    # LLM: 仅在原claim finally之后逐账核收；先前已结片可退出观察，后继领取同路径不抹掉未结责任。
    # 函数用途: 在文件被下一片合法复用之前收回已确认结束的原claim引用。
    def release_claim(self, claim):
        try:
            settled = not self._claim_blocker(claim)
        except Exception:
            settled = False
        if settled:
            with self.owner.lock:
                self.claims = [item for item in self.claims if item is not claim]

    # LLM: 无锁跨中断回调；仅冻结原claim列表，迟到bind会自己消费同一个停止事实。
    # 函数用途: 中断本片已经登记的准确claim，不发任务级取消。
    def interrupt(self):
        with self.owner.lock:
            claims = tuple(self.claims)
        for claim in claims:
            interrupt_by_name(conversation_claim_interrupt_name(claim.claim_id))

    # LLM: 只读原repo零等待连接和原claim，不按TTL、自然文案或Future结果猜执行终态。
    # 函数用途: 返回本片仍未结清的客观原因，读失败和身份变化也保留责任。
    def blockers(self):
        if not self.finished:
            return [{'reason':'background_slice_running', 'ref':'submitted_slice'}]
        with self.owner.lock:
            claims, workers = tuple(self.claims), tuple(self.workers)
        out = [{'reason':'background_model_worker_unsettled', 'ref':'original_worker'}
               for worker in workers if not worker.exited()]
        for claim in claims:
            try:
                reason = self._claim_blocker(claim)
            except Exception:
                reason = 'background_claim_unreadable'
            if reason:
                out.append({'reason':reason, 'ref':claim.claim_id})
        return out

    # LLM: 原claim必须仍是同一领取号且已由原finally结束；已发布authority才按准确attempt核原终态/操作/锁。
    # 函数用途: 核对原片的持久收尾，不创建attempt、不清理任何锁或来源。
    def _claim_blocker(self, claim):
        row = claim.claims.load(claim.thread_id, claim_scope_id=claim.claim_scope_id)
        if (row.get('load_error') or row.get('schema_version') != 'background_run_claim.v1'
                or row.get('claim_id') != claim.claim_id or row.get('thread_id') != claim.thread_id):
            return 'background_claim_identity_unconfirmed'
        if row.get('status') not in {'finished','failed','cancelled'}:
            return 'background_claim_unsettled'
        binding = row.get('runtime_authority')
        if binding is None:
            return ''
        if (not isinstance(binding, dict) or binding.get('claim_id') != claim.claim_id
                or binding.get('schema_version') != 'conversation_claim_execution.v1'
                or not binding.get('attempt_id') or not binding.get('agent_run_id')
                or not isinstance(self.repo, RuntimeRepository)):
            return 'background_attempt_identity_unconfirmed'
        repo = RuntimeRepository(self.repo.db_path, read_only=True, existing_only=True)
        attempt = repo.get_attempt(binding['attempt_id'])
        run = repo.get_agent_run(binding['agent_run_id'])
        task_run = repo.get_task_run(run['task_run_id']) if run is not None else None
        if (attempt is None or run is None or task_run is None
                or task_run['task_id'] != binding.get('task_id')
                or attempt['agent_run_id'] != binding['agent_run_id']
                or run['current_attempt_id'] != binding['attempt_id']
                or run['run_id'] != binding.get('run_id')):
            return 'background_attempt_identity_unconfirmed'
        if (not attempt_status_is_terminal(attempt['status']) or attempt['status'] == ATTEMPT_STATUS_UNKNOWN
                or not attempt['ended_at']
                or run['status'] not in RUN_STATUS_LEGACY_CREATED | AGENT_RUN_TERMINAL_STATUSES
                or task_run['status'] not in RUN_STATUS_LEGACY_CREATED | AGENT_RUN_TERMINAL_STATUSES):
            return 'background_attempt_unsettled'
        if repo.unsettled_attempt_operations(binding['attempt_id']):
            return 'background_operation_unsettled'
        if repo.get_agent_run_exec_lock(binding['agent_run_id']) is not None:
            return 'background_execution_lock_present'
        return ''


# LLM: 一个context只持同一原supervisor引用和在途观察，默认构造无I/O；不可据此授执行、清UNKNOWN或取消业务。
# 类用途: 在调度线程和宿主cleanup之间传递早停事实与有界退出结果。
class BackgroundShutdown:
    # LLM: 不启动线程/计时器；deadline只在原停止入口第一次采样，重复shutdown不能增加预算。
    # 函数用途: 建立一个Gateway代次的进程内停止交接。
    def __init__(self):
        self.lock = threading.RLock()
        self.stopping = False
        self.deadline = 0.0
        self.supervisor = None
        self.slices = set()

    # LLM: 只读进程内活跃后台执行片计数；不访问数据库或磁盘，供安全重启turn_wait统计在途后台回合。
    # 函数用途: 返回当前正在执行、尚未结束的后台会话/定时任务执行片数量。
    def active_slice_count(self) -> int:
        with self.lock:
            return sum(1 for item in self.slices if not item.finished)

    # LLM: 原后台loop只能绑定其自己创建的同一supervisor，不能换成另一个对象抹掉责任。
    # 函数用途: 保存已存在的调度器引用，供cleanup读取其原future集合。
    def bind(self, supervisor):
        with self.lock:
            if self.supervisor is not None and self.supervisor is not supervisor:
                raise RuntimeError('后台停止控制不能更换调度器')
            self.supervisor = supervisor

    # LLM: 先关闭新片准入再在锁外通知准确claim，未绑定claim的job之后仍看到stopping。
    # 函数用途: 开始同一两秒预算的后台收尾，保留所有持久业务来源。
    def stop(self):
        with self.lock:
            if not self.stopping:
                self.stopping = True
                self.deadline = time.monotonic() + SHUTDOWN_WAIT_SECONDS
            slices = tuple(self.slices)
        for item in slices:
            item.interrupt()

    # LLM: ContextVar只跟随原job与其copy_context；finally不改执行账，只移除已核清观察，未知继续保留。
    # 函数用途: 在原后台工作函数外包住本片控制，阻止停止后的新claim开始。
    @contextmanager
    def observe(self, repo):
        with self.lock:
            previous = tuple(self.slices)
        for prior in previous:
            if prior.finished and not prior.blockers():
                with self.lock:
                    self.slices.discard(prior)
        item = BackgroundSlice(self, repo)
        with self.lock:
            if self.stopping:
                raise InterruptedError('Gateway已停止接纳后台执行片')
            self.slices.add(item)
        token = _CURRENT.set(item)
        try:
            yield
        finally:
            _CURRENT.reset(token)
            item.finished = True
            if not item.blockers():
                with self.lock:
                    self.slices.discard(item)

    # LLM: future只是原提交责任之一；原claim/attempt和模型worker也必须核收。轮询不启动新执行或重试业务。
    # 函数用途: 在第一次停止的绝对期限内等待原工作返回，超时保留准确阻塞。
    def wait(self):
        while self.blockers() and time.monotonic() < self.deadline:
            time.sleep(min(.01, max(0.0, self.deadline-time.monotonic())))
        return self.blockers()

    # LLM: 仅消费原supervisor的future和本片原观察；不把未开始future取消当运行中job物理退出证明。
    # 函数用途: 为cleanup生成无正文的后台未结清清单。
    def blockers(self):
        with self.lock:
            supervisor, slices = self.supervisor, tuple(self.slices)
        futures = tuple(getattr(supervisor, '_inflight', {}).values()) + tuple(
            getattr(supervisor, '_curator_inflight', {}).values())
        out = [{'reason':'background_future_unsettled','ref':'original_future'}
               for future in futures if not future.done()]
        for item in slices:
            out.extend(item.blockers())
        return out


# LLM: 只在真实claim已登记中断名后调用；没有宿主观察的Count0其它调用与MP路径保持原样。
# 函数用途: 把当前领取的原句柄交给这一片已存在的宿主停止观察。
def observe_background_claim(claim):
    item = _CURRENT.get()
    if item is not None:
        item.bind_claim(claim)


# LLM: 默认原Thread.start不变；当前后台片只在出生前冻结同一物理worker，未知启动或换引用不得视为退出。
# 函数用途: 沿原启动入口记录本片模型保护线程的真实退出责任。
def start_background_model_worker(worker):
    item = _CURRENT.get()
    if item is None:
        worker.start()
        return
    observation = _ThreadExitObservation(worker)
    with item.owner.lock:
        item.workers.append(observation)
    observation.start()


# LLM: 必须在原_finish_slice之后；失败/未结不移除原观察，不根据任务正文或TTL补终态。
# 函数用途: 通知当前宿主片核收刚完成finally的原claim。
def finish_background_claim(claim):
    item = _CURRENT.get()
    if item is not None:
        item.release_claim(claim)


# LLM: 全进程原模型/有界调用registry只读快照保物理retention；不能以逻辑failed清空worker责任。
# 函数用途: 汇总尚未退出的本进程模型或取消清理，不读取独立采集进程或发停止信号。
def model_shutdown_blockers():
    from ..backends.bounded_call import bounded_call_drain_snapshot
    from ..contracts.model_call_ledger import model_call_drain_snapshot

    rows = [{'reason':'model_call_retained', 'ref':record.call_id}
            for _ledger, record, _retained in model_call_drain_snapshot()]
    rows.extend({'reason':'bounded_call_retained', 'ref':call.call_id}
                for call in bounded_call_drain_snapshot())
    return rows


# LLM: 原job每次重新领取前都消费同一早停事实；不能先写一个新claim再把它取消冒充零接纳。
# 函数用途: 在停止后阻止同tick继续领取后继业务片，未观察调用保持原行为。
def check_background_admission():
    item = _CURRENT.get()
    if item is not None and item.owner.stopping:
        raise InterruptedError('Gateway已停止接纳新的后台claim')
