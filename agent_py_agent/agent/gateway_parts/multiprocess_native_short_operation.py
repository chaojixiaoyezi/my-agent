# LLM: 只保存原C注册的callback/请求回合引用；定位key不能授信，原T与原C生命周期门必须包住实际C副作用。
# 模块用途: 为原生责任桥提供一次性C侧短提交；不在W执行回调，不创建第二P1/P3或名额。
import os
import threading
import uuid

from ..user_space.owner_resolver import OwnerIdentity
from . import multiprocess_admission as ma
from .io import read_json_file_report
from .multiprocess_records import record_digest
from .multiprocess_route import owner_digest, validate_worker_route
from .multiprocess_worker_commit import WorkerCommitRejected
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
        self.identifier = uuid.uuid4().hex
        transition(phase, self._check_source)

    # LLM: 读取调用必须在原T里；仅原C已记住的occupied P3可进入回调，首次完整摘要不会被后续读回覆盖。
    # 函数用途: 复核原准入并留轻量回收指纹，不从普通JSON复活权限或改变预留。
    def _check_source(self):
        issuer, owner, transition, _phase, _operation = self._sources
        row = require_original_native_admission(issuer, owner, transition)
        if self._p3_digest is None:
            # 只在原T的首次有效读回捕获；消费后仍留摘要，不留原重引用。
            self._p3_digest = record_digest(row)

    # LLM: creator/self在继承锁之前；消费标记只防原C回调重放，不是第二业务FSM或授权记录。
    # 函数用途: 把原对象标为只尝试一次；异常、迟到ACK都不能自动重做副作用。
    def _consume(self):
        if self._creator_pid != os.getpid() or self._origin is not self:
            raise WorkerCommitRejected('original_native_operation_required')
        with self._lock:
            if self._used:
                raise WorkerCommitRejected('native_operation_consumed')
            self._used = True

    # LLM: 原T先取得，原C门再包实际callback；begin/finish只是责任，on_pin在实际I/O前保留原pin供失败收尾。
    # 函数用途: 为一份真实kernel消息在C内执行原短提交；从不授予W写权限。
    def apply(self, peer_pid, on_pin):
        self._consume()
        issuer, owner, transition, phase, operation = self._sources

        # LLM: 原GatewayActiveTurnTransition同步调用；读回、原pin登记、实际短门和物理副作用都在同一T。
        # 函数用途: C仍有效且源未漂移才运行原callback，不用预检结果代替写入门。
        def commit():
            self._check_source()
            pin = issuer.begin(owner, peer_pid=peer_pid)
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
        finally:
            # 已消费对象只保留防重放标记；原pin在session/issuer内，不能因GC自动结清。
            with self._lock:
                self._sources = None
