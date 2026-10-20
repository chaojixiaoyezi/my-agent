# LLM: 只保存原C注册的callback/请求回合引用；定位key不能授信，原T与原C生命周期门必须包住实际C副作用。
# 模块用途: 一次性C侧短提交，并显式运输本次dispatch拒绝归属；不在W执行回调，不创建第二P1/P3或名额。
import os
import threading
import uuid

from ..user_space.owner_resolver import OwnerIdentity
from . import multiprocess_admission as ma
from .io import read_json_file_report
from .multiprocess_records import record_digest
from .multiprocess_route import owner_digest, validate_worker_route
from .multiprocess_worker_commit import WorkerCommitNotStarted, WorkerCommitRejected
from .request_binding import GatewayActiveTurnTransition

_PHASES = frozenset({'owner_materialize', 'executor_reserve', 'executor_register', 'request_commit'})


# LLM: 调用方已持原T；只读原C记住的occupied P3及canonical route，不重建准入、修改账本或授予W许可。
# 函数用途: 共用精确原请求准入检查，防合法route漂移被全局owner健康检查漏掉。
def require_original_native_admission(issuer, owner, transition):
    issuer._require_current()
    row = ma._read_reservation(ma.reservation_file(issuer.control.paths, transition.request_id))
    payload = read_json_file_report(transition.request_path, context='native.short_operation').payload
    route = validate_worker_route(payload, issuer._source_origin[0].partition)
    if (row is None or row['state'] != 'occupied' or row['request_id'] != transition.request_id
            or row['owner_sha'] != owner_digest(owner) or not route['ok']
            or route['owner'] != [owner.provider, owner.owner_kind, owner.owner_id]
            or row['route'] != payload.get('route')
            or issuer._controller._accepted.get(transition.request_id) != record_digest(row)):
        raise WorkerCommitRejected('original_native_admission_required')
    return row


# LLM: 原callback是可信C组合根交入的同步短提交，不允许用它包远端模型/长tool；正文没有注册或替换callback的入口。
# 类用途: 冻结一个原请求/attempt/phase和owner作用域，仅供原C物理执行一次。
class NativeShortOperation:
    # LLM: 原T对象及owner须来自C已领取请求；保存首次完整P3摘要供终态回收，不保留另一份耐久记录。
    # 函数用途: 捕获原lease/issuer、原回合、原callback及轻量原准入指纹。
    def __init__(self, issuer, owner, transition, phase, operation):
        issuer._require_current()
        if (type(owner) is not OwnerIdentity or type(transition) is not GatewayActiveTurnTransition
                or transition._worker_execution is not None or phase not in _PHASES or not callable(operation)
                or transition.request_path.parent.parent.parent != issuer.control.paths.root
                or transition.request_id != transition.request_path.stem or not transition.execution_attempt_id):
            raise WorkerCommitRejected('original_native_operation_required')
        self._origin, self._creator_pid = self, os.getpid()
        self._sources = (issuer, owner, transition, phase, operation)
        self._lock = threading.Lock()
        self._used = False
        self._p3_digest = None
        self._claim_origin_bytes = None
        self._first_materialize_origin = False
        self.identifier = uuid.uuid4().hex
        transition(phase, self._check_source)

    # LLM: 原T内保首次完整claim bytes与P3摘要；后续只复核原准入、不覆盖首像，消费finally清正文引用。
    # 函数用途: 冻结原请求首像和准入回收指纹，不从普通JSON复活权限或改变预留。
    def _check_source(self):
        issuer, owner, transition, phase, _operation = self._sources
        strict = None
        if phase == 'owner_materialize':
            from .multiprocess_native_reclaim_io import read_reservation
            from .multiprocess_native_refusal_source import claim_bytes, read_claim_payload

            paths = issuer.control.paths
            strict = read_reservation(paths.root, ma.reservation_file(paths, transition.request_id))
            payload = read_claim_payload(paths, transition.request_path)
        row = require_original_native_admission(issuer, owner, transition)
        if phase == 'owner_materialize' and row != strict:
            raise WorkerCommitRejected('first_native_admission_changed')
        if self._p3_digest is None:
            # 原T首个有效读回只捕一次；消费finally清claim正文，回收仍只留原P3摘要。
            self._p3_digest = record_digest(row)
            if phase == 'owner_materialize':
                self._claim_origin_bytes = claim_bytes(payload)

    # LLM: creator/self在继承锁之前；消费标记只防原C回调重放，不是第二业务FSM或授权记录。
    # 函数用途: 把原对象标为只尝试一次；异常、迟到ACK都不能自动重做副作用。
    def _consume(self):
        if self._creator_pid != os.getpid() or self._origin is not self:
            raise WorkerCommitRejected('original_native_operation_required')
        with self._lock:
            if self._used:
                raise WorkerCommitRejected('native_operation_consumed')
            self._used = True

    # LLM: 原T/C门包实际callback；只在本次begin的first/fullclaim明确首拒走原FAILED/P3；T外固定展示保输入pending，on_pin仍先于I/O保债。
    # 函数用途: 为原kernel消息执行一次C短提交；本次归属token不授予W权限，也不回滚_used或pin。
    def apply(self, peer_pid, on_pin, *, rejection_origin=None):
        self._consume()
        issuer, owner, transition, phase, operation = self._sources
        refusal_outcome = []

        # LLM: 原GatewayActiveTurnTransition同步调用；读回、原pin登记、实际短门和物理副作用都在同一T。
        # 函数用途: C仍有效且源未漂移才运行原callback，不用预检结果代替写入门。
        def commit():
            self._check_source()
            try:
                pin = issuer.begin(owner, peer_pid=peer_pid, rejection_origin=rejection_origin)
            except WorkerCommitNotStarted as error:
                from .multiprocess_native_refusal_terminal import terminalize_first_refusal

                outcome = terminalize_first_refusal(self, peer_pid, rejection_origin, error)
                if outcome is not None:
                    refusal_outcome.append(outcome)
                raise
            on_pin(pin)
            completed = []

            # LLM: 此callback由原controller.launch_owner_writer在生命周期门里直接调用，不跨进程发“写许可”。
            # 函数用途: 在实际I/O观察点再次核原C和原W，再做这一次受信同步提交。
            def effect():
                issuer._require_current()
                issuer._require_worker(peer_pid)
                completed.append(operation())

            decision = issuer._controller.launch_owner_writer(owner_digest(owner), effect)
            if decision.get('status') != 'started' or not completed:
                raise WorkerCommitRejected('native_operation_unavailable')
            return completed[0]

        try:
            return transition(phase, commit)
        except WorkerCommitNotStarted:
            if refusal_outcome:
                from .multiprocess_native_refusal_terminal import project_first_refusal

                project_first_refusal(refusal_outcome[0])
            raise
        finally:
            # 已消费对象只保留防重放标记；原pin在session/issuer内，不能因GC自动结清。
            with self._lock:
                self._sources = None
                self._claim_origin_bytes = None
