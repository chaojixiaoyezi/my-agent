# LLM: 原 CoordinatorExecution/T/P3/P5 是本地主体工具的唯一权限来源；P1 仍落原按 owner 哈希分区的账本，不能借 W 身份或从 JSON 重建 C 权。
# 模块用途: 把本地 main 的真实 C 认领接到执行者预留、短物理动作和原租约责任，撤权后只允许读回和精确结清。
import os

from ..user_space.owner_resolver import OwnerIdentity
from .multiprocess_coordinator import (
    _is_registered_coordinator_lease,
    pin_coordinator_commit,
    retire_coordinator_commit,
)
from .multiprocess_coordinator_execution import (
    CoordinatorExecution,
    require_coordinator_execution_agent,
)
from .multiprocess_dispatch import WorkerDispatchError
from .multiprocess_executor_ledger import ExecutorLedger
from .multiprocess_records import record_digest
from .multiprocess_route import WorkerPartition, owner_digest
from .multiprocess_worker_commit import WorkerCommitRejected


# LLM: 实例只由已安装的原 C scope 构造，捕获原 gate/claim/controller/lease/Agent；来源摘要只证明自产 P1，不是第二权威账。
# 类用途: 保存一个本地请求的实际协调者来源，每次写入和 spawn/R 都重新核原请求与原撤权门。
class CoordinatorExecutorSource:
    __slots__ = ('_origin', '_creator_pid', '_sources', '_root', '_partition', '_transition',
                 '_executor_records', '_pins', '_domains', '_epoch')

    # LLM: 构造不造claim/worker；只认原main或自产actualchild绑定，原T/P3/P5最终核；缺controller拒，gate.agent不改。
    # 函数用途: 固定当前 C 本地认领及其物理域收尾引用，不从热配置重新选 owner。
    def __init__(self, execution):
        from .request_binding import GatewayActiveTurnTransition

        if type(execution) is not CoordinatorExecution or execution.creator_pid != os.getpid():
            raise WorkerCommitRejected('original_coordinator_executor_source_required')
        gate = execution.gate
        controller = getattr(gate, '_runtime_takeover', None)
        if controller is None or not controller.active():
            raise WorkerCommitRejected('original_coordinator_executor_source_required')
        _require_source_agent(execution, 'original_coordinator_executor_source_required')
        owner = OwnerIdentity.local_main()
        partition = WorkerPartition(int(owner_digest(owner), 16) % gate.count, gate.count, gate.c_run_id)
        for name, value in (('_origin', self), ('_creator_pid', os.getpid()),
                            ('_sources', (execution, gate, execution.claim, execution.agent, controller, controller._lease)),
                            ('_root', gate.paths.root), ('_partition', partition), ('_epoch', execution.claim.lease_epoch),
                            ('_executor_records', {}), ('_pins', set()), ('_domains', []),
                            ('_transition', GatewayActiveTurnTransition(execution.claim.path, execution.claim.request_id,
                                                                       execution.claim.execution_attempt_id))):
            object.__setattr__(self, name, value)
        self._require_source_identity()
        self._commit(lambda: controller._current_coordinator_sources.append((self, self._sources)))

    # LLM: 公开赋值不能换活来源；私有引用的反射攻击不属于 wire 协议模型。
    # 函数用途: 拒绝构造后的来源重绑。
    def __setattr__(self, name, value):
        raise AttributeError('coordinator_executor_source_is_immutable')

    # LLM: 规范根只是定位，不授予写权限。
    # 函数用途: 返回捕获的唯一 Gateway 根。
    @property
    def root(self):
        return self._root

    # LLM: C 角色不来自桶号，桶只是原 P1 路径；实际角色由原 C scope 决定。
    # 函数用途: 返回 owner 对应的固定 P1 物理分区。
    @property
    def partition(self):
        return self._partition

    # LLM: owner 由原 C 路由固定；请求正文不能改成远端用户。
    # 函数用途: 返回本地主体身份。
    @property
    def owner(self):
        return OwnerIdentity.local_main()

    # LLM: 静态核原对象/自产main或child绑定/登记lease；read/finish不要求scope活，已登记P1责任不能因terminal消失，末写仍原T门。
    # 函数用途: 在继承锁和系统调用前拒绝 copy/fork、来源替换和定位漂移。
    def _require_source_identity(self):
        if self._origin is not self or self._creator_pid != os.getpid():
            raise WorkerCommitRejected('original_coordinator_executor_source_required')
        execution, gate, claim, agent, controller, lease = self._sources
        _require_source_agent(execution, 'original_coordinator_executor_source_changed')
        if (execution.gate is not gate or execution.claim is not claim or execution.agent is not agent
                or execution.creator_pid != self._creator_pid
                or getattr(gate, '_runtime_takeover', None) is not controller or controller._lease is not lease
                or lease.controller is not controller or not _is_registered_coordinator_lease(lease)
                or gate.paths.root != self.root or controller.paths.root != self.root
                or gate.count != self.partition.count or gate.c_run_id != self.partition.c_run_id
                or controller.c_run_id != self.partition.c_run_id or claim.lease_epoch != self._epoch
                or claim.path != gate.paths.processing / (claim.request_id + '.json')
                or self._transition._worker_execution is not execution):
            raise WorkerCommitRejected('original_coordinator_executor_source_changed')

    # LLM: 原 T/P3/P5 重读发生在每个实际短动作，不能以构造预检、ACK 或来源摘要代替当次权限。
    # 函数用途: 在原请求事务与原 C 撤权门内执行一次同步效果，不持门等待命令退出。
    def _commit(self, callback, phase=None, source_guard=None):
        self._require_source_identity()

        # LLM: GatewayActiveTurnTransition 已持原 T 和 C 生命周期门，最终再核原引用后才消费受信 callback。
        # 函数用途: 拒绝争门期间的来源漂移并执行一次原短动作。
        def effect():
            self._require_source_identity()
            if source_guard is not None:
                source_guard()
            completed = []
            decision = self._sources[4].launch_owner_writer(owner_digest(self.owner), lambda: completed.append(callback()))
            if decision.get('status') != 'started' or not completed:
                raise WorkerCommitRejected('original_coordinator_executor_source_unavailable')
            return completed[0]

        return self._transition(phase or 'executor_commit', effect)

    # LLM: 纯结构与 canonical 桶校验不授信；请求、attempt、epoch、owner 不能借同桶另一记录。
    # 函数用途: 核对本请求的不可重绑 P1 字段。
    def _bound_record(self, record):
        self._require_source_identity()
        row = ExecutorLedger(self.root, self.partition)._bound_record(record)
        claim = self._sources[2]
        if (row['owner_sha'] != owner_digest(self.owner) or row['request_id'] != claim.request_id
                or row['execution_attempt_id'] != claim.execution_attempt_id or row['lease_epoch'] != self._epoch):
            raise WorkerCommitRejected('original_coordinator_executor_binding_required')
        return row

    # LLM: 原唯一 P1 写入与自产摘要登记在同一原短门；文件存在不能补造来源。
    # 函数用途: 持久预留一个全新执行者，成功之后才能创建垫片。
    def reserve(self, record, *, source_guard=None):
        row = self._bound_record(record)
        return self._commit(lambda: self._write(row), source_guard=source_guard)

    # LLM: 原完整摘要和原 FSM/CAS 缺一不可，推进不授予另一个请求或旧 C 写权。
    # 函数用途: 推进本来源真正提交过的执行者记录。
    def advance(self, record, state, registration=None, *, source_guard=None):
        row = self._bound_record(record)
        return self._commit(lambda: self._write(row, state, registration), source_guard=source_guard)

    # LLM: 调用方已持原 T/C 门；P1 fsync 成功后才更新进程内摘要，失败保留原账和未知责任。
    # 函数用途: 同步写入 canonical 记录并发布准确的自产来源摘要。
    def _write(self, row, state=None, registration=None):
        identifier = row['reservation_id']
        if state is not None and self._executor_records.get(identifier) != record_digest(row):
            raise WorkerCommitRejected('original_coordinator_executor_submission_required')
        ledger = ExecutorLedger(self.root, self.partition)
        result = ledger.reserve(row) if state is None else ledger.advance(row, state, registration)
        self._executor_records[identifier] = record_digest(result)
        return result

    # LLM: closing 可读原账，不复活执行权；只读入口不核尚在 processing 的业务状态。
    # 函数用途: 从唯一规范 P1 回读本次登记。
    def read(self, identifier):
        self._require_source_identity()
        return ExecutorLedger(self.root, self.partition).read(identifier)

    # LLM: 清理登记仍不能绕过原权限门，失败不吞成已废弃。
    # 函数用途: 尝试标记本来源的预留为 abandoned。
    def abandon(self, record, *, source_guard=None):
        return self.advance(record, 'abandoned', source_guard=source_guard)

    # LLM: 仅原受信启动器可提交 spawn/R，callback 不是 wire 字段；最终源复核与物理动作同门。
    # 函数用途: 在原 C 短门创建垫片或发送放行字节。
    def perform_short_effect(self, record, phase, callback, *, source_guard=None):
        self._bound_record(record)
        if phase not in {'executor_spawn', 'executor_release'} or not callable(callback):
            raise WorkerCommitRejected('original_coordinator_executor_effect_required')
        return self._commit(callback, phase, source_guard)

    # LLM: pin只表责任，原T/P3/P5与lease门决定新增；source_guard/on_pin仅受信C宿主设施本地回调，同门先核再创建/发布，wire不能提供；同步C pin/host tests。
    # 函数用途: 在原租约账保留本地执行域寿命，并允许宿主设施在同一短门记住该精确责任。
    def begin_execution_domain(self, *, source_guard=None, on_pin=None):
        if ((source_guard is not None and not callable(source_guard))
                or (on_pin is not None and not callable(on_pin))):
            raise WorkerCommitRejected('original_coordinator_executor_callback_required')
        # LLM: 调用方已通过原T/C门，增加的只有本来源在原lease中的精确责任。
        # 函数用途: 保存本次物理域pin，不以标签授予执行权。
        def remember():
            claim = self._sources[2]
            pin = pin_coordinator_commit(self._sources[5], (self.partition.c_run_id, 'C', owner_digest(self.owner),
                                                           claim.request_id, claim.execution_attempt_id))
            self._pins.add(pin)
            if on_pin is not None:
                on_pin(pin)
            return pin
        return self._commit(remember, source_guard=source_guard)

    # LLM: 仅原 domain 在确认实际内核退出屏障后调用；closing 允许精确结清，不从 EOF/超时推断结束。
    # 函数用途: 移除本来源真正持有的原租约责任，拒绝跨来源和重放。
    def finish_execution_domain(self, pin):
        self._require_source_identity()
        lease = self._sources[5]
        with lease._lifecycle_gate:
            if pin not in self._pins:
                raise WorkerCommitRejected('original_coordinator_executor_pin_required')
            retire_coordinator_commit(lease, pin)
            self._pins.remove(pin)

    # LLM: 原引用表不是第二生命周期权威；只由真实 domain 判定 barrier，不修改 P1/P3，不重建丢失对象。
    # 函数用途: 对实际已退出的本地执行域结清物理责任，活或未知域原样保留。
    def collect_completed_domains(self):
        self._require_source_identity()
        for domain in self._domains:
            domain.observe()
            domain.retire_lifetime()


# LLM: main/child共同身份门仅核静态自产来源，失败保原native source typed拒绝；物理read/finish不要求当前scope活。
# 函数用途: 将准确Agent绑定错误保持为原执行者来源合同错误。
def _require_source_agent(execution, reason):
    try:
        require_coordinator_execution_agent(execution)
    except WorkerDispatchError as error:
        raise WorkerCommitRejected(reason) from error
