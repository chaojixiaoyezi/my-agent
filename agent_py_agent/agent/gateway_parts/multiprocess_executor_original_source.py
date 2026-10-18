# LLM: 只持原C的不可替换引用；所有写入/实际动作在原controller短门内再次核原来源，字段和ACK无授权作用。
# 模块用途: 为内部执行scope提供不可变的原C P1提交来源，不提供W/native传输或用户命令权限。
import os

from ..user_space.owner_resolver import OwnerIdentity
from .multiprocess_executor_ledger import ExecutorLedger
from .multiprocess_route import owner_digest
from .multiprocess_worker_commit import WorkerCommitIssuer, WorkerCommitRejected


# LLM: 不由文件重建；observer仅可信设施注入，不修改授权决定，原issuer仍负责唯一P1来源登记。
# 类用途: 保存同一个原issuer/controller/lease及其规范定位，防可变facade方法改去另一C。
class OriginalCExecutorSource:
    __slots__ = ('_origin', '_creator_pid', '_sources', '_root', '_partition', '_observer', '_transition')

    # LLM: 一次构造只捕获真实原对象；可选原T来自可信C组合根，不由wire字段恢复写许可。
    # 函数用途: 固定原C、原W、owner及原请求提交边界。
    def __init__(self, issuer, owner, observer=None, *, transition=None):
        if hasattr(self, '_origin') or type(issuer) is not WorkerCommitIssuer or type(owner) is not OwnerIdentity:
            raise WorkerCommitRejected('original_executor_source_required')
        issuer._require_current()
        issuer._require_worker(issuer.process.pid)
        if observer is not None and not callable(observer):
            raise WorkerCommitRejected('executor_source_observer_invalid')
        if transition is not None:
            from .request_binding import GatewayActiveTurnTransition

            if (type(transition) is not GatewayActiveTurnTransition
                    or transition.request_path.parent.parent.parent != issuer.control.paths.root
                    or transition.request_id != transition.request_path.stem or not transition.execution_attempt_id):
                raise WorkerCommitRejected('original_executor_request_required')
        sources = (issuer, owner, issuer.control, issuer._controller, issuer._lease,
                   issuer.launch, issuer.process)
        for key, value in (('_origin', self), ('_creator_pid', os.getpid()), ('_sources', sources),
                           ('_root', issuer.control.paths.root), ('_partition', issuer.launch.partition),
                           ('_observer', observer), ('_transition', transition)):
            object.__setattr__(self, key, value)

    # LLM: 普通赋值不能改变持有的活来源；反射修改宿主私有对象不属于wire权限模型。
    # 函数用途: 拒绝事后重绑来源或观察器。
    def __setattr__(self, name, value):
        raise AttributeError('original_executor_source_is_immutable')

    # LLM: 只读定位不授信。
    # 函数用途: 返回原规范根。
    @property
    def root(self):
        return self._root

    # LLM: 分区来自原launch，不跟随热facade字段。
    # 函数用途: 返回冻结原分区。
    @property
    def partition(self):
        return self._partition

    # LLM: 原对象引用只用于内部来源比较，不允许替换。
    # 函数用途: 返回原issuer。
    @property
    def issuer(self):
        return self._sources[0]

    # LLM: owner来源固定，不由后续元数据重选。
    # 函数用途: 返回原owner。
    @property
    def owner(self):
        return self._sources[1]

    # LLM: 原对象/创建进程检查先于继承锁；仅核原引用和规范定位，不把closing或W死亡当成只读事实失效。
    # 函数用途: 拒copy/fork、重绑issuer内部来源或替换原控制上下文，保留撤权后的原P1读回。
    def _require_source_identity(self):
        if self._origin is not self or self._creator_pid != os.getpid():
            raise WorkerCommitRejected('original_executor_source_required')
        issuer, _owner, control, controller, lease, launch, process = self._sources
        if (issuer._issuer_origin is not issuer or issuer._creator_pid != self._creator_pid
                or issuer._source_origin[0] is not launch or issuer._source_origin[1] is not process
                or issuer.control is not control or issuer._controller is not controller or issuer._lease is not lease
                or issuer.launch is not launch or issuer.process is not process
                or control.paths.root != self.root or launch.gateway_root != self.root
                or launch.partition != self.partition):
            raise WorkerCommitRejected('original_executor_source_changed')

    # LLM: 写入和物理动作必须在来源同一之后再核活C/W；实际短门内也调用，不能用只读校验授予提交。
    # 函数用途: 保留原撤权和存活门，只读入口不调用此活提交检查。
    def _require_original(self):
        self._require_source_identity()
        issuer, _owner, _control, _controller, _lease, _launch, process = self._sources
        issuer._require_current()
        issuer._require_worker(process.pid)

    # LLM: 可选原T先核精确活回合，再进入原C门；不持T等待垫片、上下文运输或用户命令。
    # 函数用途: 把每个同步提交段与原请求停止串行，来源检查仍先于继承锁。
    def _commit(self, callback, phase=None, source_guard=None):
        self._require_original()
        if self._transition is not None:
            return self._transition(phase or 'executor_commit',
                                    lambda: self._commit_current(callback, phase, source_guard))
        return self._commit_current(callback, phase, source_guard)

    # LLM: callback在原controller/lease实际门中执行；原T如已绑定则由外层持有，最终来源检查不变。
    # 函数用途: 在同一原短门核来源、调用壳并物理执行一次，不使用ACK授权。
    def _commit_current(self, callback, phase, source_guard):
        self._require_original()
        completed = []

        # LLM: 原T持续持有；实际C门内核原来源和精确P3，不以全局owner健康或ACK替代本请求准入。
        # 函数用途: 拒绝来源与合法route漂移，再执行同步副作用。
        def effect():
            self._require_original()
            if self._transition is not None:
                from .multiprocess_native_short_operation import require_original_native_admission
                require_original_native_admission(self.issuer, self.owner, self._transition)
            if source_guard is not None:
                source_guard()
            if phase is not None and self._observer is not None:
                self._observer(phase)
            completed.append(callback())

        decision = self._sources[3].launch_owner_writer(owner_digest(self.owner), effect)
        if decision.get('status') != 'started' or not completed:
            raise WorkerCommitRejected('original_executor_source_unavailable')
        return completed[0]

    # LLM: owner与分区仍由原ledger纯校验；绑定了原T时记录不能改去同owner的另一请求或attempt。
    # 函数用途: 拒错绑定字段，不把定位校验替代原T/P3活提交门。
    def _record(self, record):
        source = ExecutorLedger(self.root, self.partition)._bound_record(record)
        if source['owner_sha'] != owner_digest(self.owner):
            raise WorkerCommitRejected('original_executor_owner_required')
        if self._transition is not None and (
                source['request_id'] != self._transition.request_id
                or source['execution_attempt_id'] != self._transition.execution_attempt_id):
            raise WorkerCommitRejected('original_executor_request_required')
        return source

    # LLM: 原issuer继续发布实际P1摘要；不由facade方法动态寻找issuer。
    # 函数用途: 在原短门内调用原P1预留。
    def reserve(self, record, *, source_guard=None):
        source = self._record(record)
        return self._commit(lambda: self.issuer.reserve_executor(source), source_guard=source_guard)

    # LLM: 原P1比较/FSM/fsync仍在原issuer实现，来源不变。
    # 函数用途: 在原短门内推进原登记。
    def advance(self, record, state, registration=None, *, source_guard=None):
        source = self._record(record)
        return self._commit(lambda: self.issuer.advance_executor(source, state, registration),
                            source_guard=source_guard)

    # LLM: 只读冻结规范路径；closing仍允许读已存在事实，读回不授权写入。
    # 函数用途: 从原唯一P1回读记录。
    def read(self, identifier):
        self._require_source_identity()
        return ExecutorLedger(self.root, self.partition).read(identifier)

    # LLM: 废弃不能绕开原来源或撤权门；失败保留未知，不自动修盘。
    # 函数用途: 在原短门内尝试原废弃。
    def abandon(self, record, *, source_guard=None):
        return self.advance(record, 'abandoned', source_guard=source_guard)

    # LLM: 只允许现有两物理边界；callback由受信启动器提供，不解析wire回调或发W许可。
    # 函数用途: 在原C实际门内spawn或发送release字节。
    def perform_short_effect(self, record, phase, callback, *, source_guard=None):
        self._record(record)
        if phase not in {'executor_spawn', 'executor_release'} or not callable(callback):
            raise WorkerCommitRejected('original_executor_effect_required')
        return self._commit(callback, phase, source_guard)
