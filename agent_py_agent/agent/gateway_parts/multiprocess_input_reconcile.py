# LLM: 修原queued/cursor及完整v3 locator投影；locator不是消费/授权，旧漏ref晚于封口仍defer；C短门内JSON只尝试原锁，busy同对象停止本行；原C/Root/lease与receipt末门不放宽，不授P3/W/Store权。
# 模块用途: 让冷或健康协调者补原收件队列；未支持的绑定输入明确延期，不创建foreign Agent或第二账本。
from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager

from ..backends.bounded_call import BoundedCallExitUnconfirmedError
from ..common.global_write_guard import GlobalWriteInWorkerError, require_global_writer
from ..common.request_execution_ports import (
    RequestExecutionContextUnavailable,
    RequestExecutionOutcomeUncertainError,
)
from ..runtime_errors import DataCorruptionError, RecoverableRuntimeError
from . import input_delivery_service as inputs
from .control_plane import GatewayControlPlane
from .io import (
    gateway_turn_transition,
    locked_file_transition,
    read_json_file_report,
    write_json_file_atomic,
)
from .multiprocess_admission import PersistentAdmission
from .multiprocess_coordinator import CoordinatorUnavailable
from .multiprocess_drain_runtime import GatewayDrainRuntime
from .paths import gateway_paths_from_root

_UNCERTAIN = (RequestExecutionOutcomeUncertainError, RequestExecutionContextUnavailable,
              CoordinatorUnavailable, BoundedCallExitUnconfirmedError, GlobalWriteInWorkerError)
_STOP_SCAN = (*_UNCERTAIN, BlockingIOError)


# LLM: 字节仅比较同原receipt/完整prepared，不生成摘要或权限；任何正文、actor或媒体变化都不沿旧快照提交。
# 函数用途: 固定已经读取的原JSON事实，避免可变dict在末写前换内容。
def _identity_bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


# LLM: 没有claim只拒入口，不能伪造attempt/entered或借旧P3清账；错误不携正文、文件路径或来源对象。
# 函数用途: 报告当前C队列元数据来源已不可核实。
def _missing_source():
    return RequestExecutionContextUnavailable('original_coordinator_queued_input_source_required')


# LLM: 延期不等于损坏/结清；summary仅诊断且可JSON化，不授恢复、执行或释放权，普通错误不能冒充typed未知。
# 类用途: 让全延期扫描沿原独立段退避，避免空转记成功。
class CoordinatorInputDeferredError(RecoverableRuntimeError):
    category = 'state'

    # LLM: 只保存扫描标量/状态计数，不保存Context/异常栈/Store或正文。
    # 函数用途: 如实报告未接绑定输入来源，不把延期数加成已消费数。
    def __init__(self, summary):
        self.summary = dict(summary)
        super().__init__('原绑定输入的执行来源尚未接入，队列修复已保留原回执并延期对账')


# LLM: 此对象只持原C设施与同步receipt窗口；queue/cursor及既有v3定位补漏均核完整canonical，JSON只尝试原锁；复制/跨线程/关闭/替换不能延长写权，不登记新身份。
# 类用途: 把原收件元数据写和查找投影补漏绑定到同一次协调者扫描，补hint不代表结清。
class _QueuedInputSource:
    # LLM: 构造不读owner、库或P3，只核原Root已经保留的同一个gate；冷None与健康actual Agent均不作为授权来源。
    # 函数用途: 捕获本次扫描的原对象、规范队列路径和进程线程身份。
    def __init__(self, context, paths, gate):
        if (type(getattr(context, 'control_plane', None)) is not GatewayControlPlane
                or type(getattr(context, 'drain_runtime', None)) is not GatewayDrainRuntime):
            raise _missing_source()
        self.context, self.paths, self.gate = context, paths, gate
        self.control, self.runtime = context.control_plane, context.drain_runtime
        self.controller, self.lease = self.runtime.controller, self.runtime._lease_origin
        self._refs = (context, paths, gate, self.control, self.runtime, self.controller, self.lease)
        self._agent = context.agent
        self._canonical = vars(gateway_paths_from_root(self.lease.root)).copy()
        self._origin, self._pid, self._thread = self, os.getpid(), threading.get_ident()
        self._active, self._window = True, None
        self.require_original()

    # LLM: 原角色拒门不授C权；实际control/Root/controller/lease和retained gate末核，不拿owner quarantine当收件拒绝。
    # 函数用途: 在锁前和实际写前拒绝失源，Count0完全不经过此入口。
    def require_original(self):
        require_global_writer('gateway_queued_input_reconcile')
        try:
            same = all(current is original for current, original in zip(
                (self.context, self.paths, self.gate, self.control, self.runtime, self.controller, self.lease), self._refs))
            if (type(self) is not _QueuedInputSource or self._origin is not self or not self._active or not same
                    or self._pid != os.getpid() or self._thread != threading.get_ident()
                    or type(self.control) is not GatewayControlPlane or type(self.runtime) is not GatewayDrainRuntime
                    or type(self.gate) is not PersistentAdmission or self.runtime.control is not self.control
                    or self.context.control_plane is not self.control or self.context.drain_runtime is not self.runtime
                    or self.context.paths is not self.paths or self.control.paths is not self.paths
                    or self.context.agent is not self._agent or self.runtime._closing
                    or self.control.controller() is not self.controller or self.controller._lease is not self.lease
                    or self.runtime._lease_origin is not self.lease
                    or type(self.context.coordinator_worker_count) is not int
                    or self.context.coordinator_worker_count != self.control.worker_count
                    or self.gate.agent is not (self._agent if self._agent is not None else self.control)
                    or self.gate.paths is not self.paths or self.gate._runtime_takeover is not self.controller
                    or (self.gate.count, self.gate.c_run_id) != (self.control.worker_count, self.control.c_run_id)
                    or any(getattr(self.paths, key) != value for key, value in self._canonical.items())
                    or not any(row[0] is self.gate and row[1] is self.paths and row[2] is self.gate.agent
                               and row[3:] == (self.gate.count, self.gate.c_run_id)
                               for row in self.runtime._admission_origins)):
                raise _missing_source()
            self.runtime.require_original()
            if not self.controller.active():
                raise _missing_source()
        except (AttributeError, TypeError, ValueError):
            raise _missing_source() from None

    # LLM: 先取得原input/cursor事务锁，后controller→lease→Root末门；门内只同步队列IO，不取Store/M/task/P3或等待执行。
    # 函数用途: 使关闭producer与本次精确元数据写互斥。
    @contextmanager
    def _commit_scope(self, transition):
        self.require_original()
        with transition:
            with self.controller._mutex, self.lease._lifecycle_gate, self.runtime._gate:
                self.require_original()
                yield

    # LLM: 原input T已持；末写非阻塞重读同完整receipt，锁忙立即原样退出，不借fresh值重签，窗口替换或退出一律拒绝。
    # 函数用途: 在原inbox原子写之前核实当前来源与原收件快照仍一致。
    def _require_queue_writer(self, window):
        self.require_original()
        if self._window is not window:
            raise _missing_source()
        current = inputs.read_gateway_input_receipt(self.paths, window[0], blocking=False)
        if current is None or _identity_bytes(current.to_dict()) != window[1]:
            raise _missing_source()
        self.require_original()

    # LLM: queued只修原prepared；receipt/候选只尝试原读锁，末writer只尝试原写锁，保全原owner/actor/media/摘要/canonical校验，不造claim/P3。
    # 函数用途: 在独立input事务中补回一条准确的原队列请求。
    def repair_queued(self, receipt):
        if receipt.state != 'queued':
            raise _missing_source()
        with self._commit_scope(gateway_turn_transition(self.paths, receipt.request_id)):
            current = inputs.read_gateway_input_receipt(self.paths, receipt.request_id, blocking=False)
            expected = _identity_bytes(receipt.to_dict())
            if current is None or _identity_bytes(current.to_dict()) != expected:
                raise _missing_source()
            window = (receipt.request_id, expected, _identity_bytes(receipt.prepared_request))
            self._window = window

            # LLM: 只由原materialize同步调用；实际写前再核来源，只尝试原common写锁，busy不假写成功，不返回跨窗口许可。
            # 函数用途: 在原队列原子写的最后位置复核这条收件。
            def write_request(path, payload):
                self._require_queue_writer(window)
                if path != self.paths.inbox / (window[0] + '.json') or _identity_bytes(payload) != window[2]:
                    raise _missing_source()
                self.require_original()
                write_json_file_atomic(path, payload, blocking=False)

            try:
                inputs._materialize_prepared_request_locked(
                    self.paths, receipt, write_request=write_request, read_blocking=False)
                self._require_queue_writer(window)
            finally:
                self._window = None

    # LLM: 只补已有canonical v3的同一个by_turn查找投影；原C/Root/gate及完整raw CAS都须当前，不据引用重签目标/ACK或迁移旧无transport。
    # 函数用途: 在原独立扫描中修复已提交运输的漏定位，旧目标晚于封口仍保持原待恢复责任。
    def repair_transport_locator(self, receipt):
        if receipt.transport is None or receipt.state in {'queued', 'consumed'}:
            return
        with self._commit_scope(gateway_turn_transition(self.paths, receipt.request_id)):
            path = inputs.gateway_input_receipt_path(self.paths, receipt.request_id)
            report = read_json_file_report(path, context='gateway.input_locator.repair', blocking=False)
            inputs._raise_for_input_read_report(report, 'gateway input receipt is unreadable')
            expected = _identity_bytes(receipt.to_dict())
            if _identity_bytes(report.payload) != expected:
                raise _missing_source()
            self.require_original()
            inputs._write_active_turn_ref(self.paths, receipt, blocking=False,
                target_turn_id=receipt.transport['target']['turn_id'])
            current = read_json_file_report(path, context='gateway.input_locator.repair', blocking=False)
            inputs._raise_for_input_read_report(current, 'gateway input receipt is unreadable')
            if _identity_bytes(current.payload) != expected:
                raise _missing_source()
            self.require_original()

    # LLM: cursor是独立轮转投影；原JSON写锁只尝试不等，busy不假推进，只记examined不代表绑定输入已结或可执行。
    # 函数用途: 在独立cursor事务中沿原格式提交扫描位置。
    def commit_cursor(self, processed_path):
        if processed_path.parent != self.paths.root / 'input_receipts' or processed_path.suffix != '.json':
            raise _missing_source()
        cursor = self.paths.root / 'input_receipts/state/reconcile_cursor.json'
        transition = locked_file_transition(cursor.with_name('.reconcile_cursor.transition'))
        with self._commit_scope(transition):

            # LLM: 原cursor helper是唯一格式权威；末核C/本行名，只尝试原common写锁，busy原样退出，不增持久调度账。
            # 函数用途: 在cursor实际原子替换前阻止迟到、关闭及路径漂移。
            def write_cursor(path, payload):
                self.require_original()
                if path != cursor or payload.get('last_name') != processed_path.name:
                    raise _missing_source()
                write_json_file_atomic(path, payload, blocking=False)

            inputs._commit_reconcile_cursor(self.paths, processed_path, write_cursor=write_cursor)


# LLM: 只消费原扫描页/receipt/queue/cursor；未支持行只延期轮转，实际typed或资源busy同对象停止且不越行，普通坏行保原转动。
# 函数用途: 为冷和健康C修复queued输入，不解析活guidance或构造业务Agent。
def reconcile_coordinator_queued_inputs(context, paths, gate, *, limit=64):
    source = _QueuedInputSource(context, paths, gate)
    summary = dict(checked=0, queued=0, consumed=0, deferred=0, errors=0, unsupported_states={})
    try:
        for path in inputs._reconcile_receipt_paths(paths, limit=max(1, int(limit or 1))):
            _reconcile_input_row(source, paths, path, summary)
        return summary
    finally:
        source._active = False


# LLM: 单行分类与原cursor finally同闭包；typed或BlockingIOError原对象停止，ordinary坏行仍轮转；已有v3只补lookup且仍deferred，reader非阻塞，不假缺失或结清。
# 函数用途: 处理扫描页的一条原收件，避免状态与异常分支嵌套遮蔽游标提交顺序。
def _reconcile_input_row(source, paths, path, summary):
    advance = True
    try:
        source.require_original()
        receipt = inputs.read_gateway_input_receipt(paths, path.stem, blocking=False)
        if receipt is None:
            return
        if receipt.request_id != path.stem:
            raise DataCorruptionError('gateway input receipt filename conflicts')
        summary['checked'] += 1
        if receipt.state == 'queued':
            source.repair_queued(receipt)
            summary['queued'] += 1
        elif receipt.state == 'consumed':
            summary['consumed'] += 1
        else:
            if receipt.transport is not None:
                source.repair_transport_locator(receipt)
            summary['deferred'] += 1
            states = summary['unsupported_states']
            states[receipt.state] = states.get(receipt.state, 0) + 1
    except _STOP_SCAN:
        advance = False
        raise
    except Exception:
        summary['errors'] += 1
    finally:
        if advance:
            try:
                source.commit_cursor(path)
            except _STOP_SCAN:
                raise
            except Exception:
                summary['errors'] += 1


# LLM: 沿原普通坏行错误与本片明确延期让独立段退避；不重建typed异常、不把deferred并入已结计数或更改receipt。
# 函数用途: 禁止整页延期被循环记成成功，保留原派发及其它修复段的机会。
def require_coordinator_input_progress(summary):
    inputs.raise_if_input_reconcile_unsettled(summary)
    if summary['deferred']:
        raise CoordinatorInputDeferredError(summary)
