# LLM: 只在真实C原T/租约末门收口无P3旧请求，保留原route/绑定/指纹；Count0只认实际生命周期锁，不授模型、工具或新执行权。
# 模块用途: 终结拓扑切换遗留请求并修复原响应、输入回执和会话占用。
from __future__ import annotations

import copy
import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from ..runtime_errors import DataCorruptionError
from .io import read_json_file_report, validated_gateway_request_fingerprint
from .response_renderer import read_gateway_terminal_envelope_report


# LLM: 只读且仅供正MP；任何存在或不可读P3都保留原恢复权威，不凭坏文件推断预留缺失。
# 函数用途: 判断请求是否已经失去当前拓扑的合法路由。
def unroutable_reason(paths, payload, *, gate) -> str:
    from .multiprocess_admission import PersistentAdmission, _read_reservation, reservation_file

    if type(gate) is not PersistentAdmission or gate.count <= 0 or gate.paths != paths:
        return ""
    rid = payload.get("id")
    if not isinstance(rid, str) or not rid or rid in {".", ".."} or any(c in rid for c in ("/", "\\", "\x00")):
        return ""
    if _read_reservation(reservation_file(paths, rid)) is not None:
        return ""
    if (payload.get("schema_version") == "gateway_terminal_request.v1"
            and (payload.get("terminal_response") or {}).get("error_code") == "GATEWAY_REQUEST_ROUTE_RETIRED"):
        canonical = _committed_closeout(paths, paths.inbox / (rid + ".json"), payload)
        reason = canonical["terminal_response"].get("route_retirement_reason") if canonical == payload else None
        # canonical会补齐旧指纹；原原因是已提交事实，不能再用failed状态重做pending专用推断。
        if reason in {"legacy_count0_request", "stale_generation_route", "legacy_unstamped_request"}:
            return reason
        raise DataCorruptionError("unroutable_terminal_conflict")
    metadata = payload.get("metadata")
    abv = metadata.get("gateway_admin_binding_version") if isinstance(metadata, dict) else None
    route = payload.get("route")
    if route is None and not abv:
        return "legacy_count0_request"
    if isinstance(route, dict) and route.get("c_run_id") != gate.c_run_id:
        return "stale_generation_route"
    if not payload.get("request_fingerprint") or not abv:
        if payload.get("status", "pending") == "pending":
            return "legacy_unstamped_request"
    return ""


def resume_unroutable_closeout(paths, path, *, admission_gate=None):
    """只分派结构化退休终态；完整身份及T→M→L校验仍由原投影器完成。"""
    from .multiprocess_admission import PersistentAdmission

    if type(admission_gate) is not PersistentAdmission or admission_gate.count <= 0:
        return None
    if not (paths.terminal / path.name).is_file():
        return None
    try:
        report = read_gateway_terminal_envelope_report(paths.terminal / path.name, request_id=path.stem)
        if report.load_error is not None:
            return "blocked"
        if not report.payload or report.payload["terminal_response"].get("error_code") != "GATEWAY_REQUEST_ROUTE_RETIRED":
            return None
        return "expired" if project_unroutable_closeout(paths, path.stem, admission_gate=admission_gate) else "blocked"
    except (OSError, ValueError, RuntimeError, DataCorruptionError):
        return "blocked"


# LLM: 仅查看原guard登记和require_held，不修改_HELD、不造锁来源；fork/FD关闭/路径替换均失败关闭。
# 函数用途: 确认本进程确实持有同一Gateway根的单进程生命周期锁。
def standalone_lifecycle_held_for(root) -> bool:
    from .standalone_lifecycle import _HELD, StandaloneLifecycle, StandaloneLifecycleUnavailable

    expected = Path(os.path.abspath(root))
    for lifecycle in tuple(_HELD):
        if type(lifecycle) is not StandaloneLifecycle or lifecycle.path.parent != expected:
            continue
        try:
            lifecycle.require_held()
        except (OSError, ValueError, StandaloneLifecycleUnavailable):
            continue
        return True
    return False


# LLM: 只生成冻结合同的失败答复，不补身份、路由、指纹或重试业务。
# 函数用途: 告诉客户端旧请求没有在当前拓扑执行，需要重新发送。
def unroutable_closeout_response(request, reason):
    return {"id": request["id"], "ok": False, "status": "failed", "response": "",
            "error_code": "GATEWAY_REQUEST_ROUTE_RETIRED", "route_retirement_reason": reason,
            "ended_at": time.time()}


# LLM: 判死仍按原进程身份及stale合同，活着永不收口；未知且不stale只诊断一次，不把超时单独当明确死亡。
# 函数用途: 为processing收口取得原执行者不再活动的证据。
def unroutable_executor_dead(paths, path, payload, context):
    from .daemon_metadata import process_identity_is_live
    from .recovery import _processing_request_stale

    live = process_identity_is_live(payload.get("lease_process_identity"))
    if live is False or (live is not True and _processing_request_stale(payload, path, context)):
        return True
    gate = context.admission_gate
    warned = getattr(gate, "_unroutable_executor_unproven", None)
    if warned is None:
        warned = gate._unroutable_executor_unproven = set()
    if path.stem not in warned:
        warned.add(path.stem)
        logging.getLogger(__name__).warning("processing_recovery.executor_unproven request_id=%s", path.stem)
    return False


# LLM: 只运输当次真实C/gate/lease/请求/原因；无P2/P3/P5/模型/工具来源，只能在原metadata短窗收尾。
# 类用途: 让无路由请求的会话清理使用现有公共短事务端口。
class UnroutableCloseoutSource:
    # LLM: 构造只捕获原引用及冻结owner纯路径，scope安装前不能消费；不初始化Agent或owner目录。
    # 函数用途: 保存本次收口的精确来源与不可变请求。
    def __init__(self, gate, controller, lease, payload, reason):
        from ..user_space.owner_resolver import resolve_owner_home
        from .multiprocess_processing_recovery import _bytes, _recovery_base_home
        from .request_owner_binding import frozen_request_owner

        self.gate, self.controller, self.lease = gate, controller, lease
        self.paths, self.request_id, self.reason = gate.paths, payload["id"], reason
        self.payload = copy.deepcopy(payload)
        self._captured = (gate, controller, lease, self.paths, self.request_id, reason, _bytes(payload))
        self._origin, self._pid, self._thread, self._active = self, os.getpid(), threading.get_ident(), True
        owner = frozen_request_owner(payload)
        if owner is None:
            raise self.rejection("owner")
        self.home = resolve_owner_home(_recovery_base_home(gate.agent).root, owner)
        self._home_origin = self.home
        self._window = self._store = self._terminal_projection = self._failure_projection = None

    # LLM: 拒绝只报告收口短窗未进入，不伪造执行身份或签重放许可。
    # 函数用途: 给失效来源返回可重试的安全拒绝。
    def rejection(self, phase):
        return BlockingIOError("unroutable_closeout_source_unavailable:" + phase)

    # LLM: 同current/origin/PID/thread和真实C末门逐次复核，controller/lease替换、P3出现或原因变化立即拒绝。
    # 函数用途: 禁止构造、复制、迟到或跨线程来源借权收口。
    def require_original(self, phase):
        from . import multiprocess_processing_recovery as pr
        from .queued_steer_expiry import _expiry_coordinator_source

        gate, controller, lease, paths, rid, reason, raw = self._captured
        if (type(self) is not UnroutableCloseoutSource or self._origin is not self or not self._active
                or pr.current_processing_recovery() is not self or self._pid != os.getpid()
                or self._thread != threading.get_ident() or self.gate is not gate
                or self.controller is not controller or self.lease is not lease or self.paths is not paths
                or self.request_id != rid or self.reason != reason or pr._bytes(self.payload) != raw
                or self.home is not self._home_origin or gate.paths != paths
                or _expiry_coordinator_source(gate) != (controller, lease) or not controller.active()
                or unroutable_reason(paths, self.payload, gate=gate) != reason):
            raise self.rejection(phase)

    # LLM: 真实原窗口才可nested；外层严格T→controller→lease，不持M等T，不重入flock。
    # 函数用途: 给收尾动作打开与过期插话相同的短提交门。
    @contextmanager
    def short_scope(self, phase):
        from . import multiprocess_processing_recovery as pr
        from .queued_steer_expiry import _projection_scope

        self.require_original(phase)
        if self._holds_window():
            self.require_writer(phase)
            yield
            return
        with _projection_scope(self.paths, self.request_id, self.gate, self.controller, self.lease):
            self.require_original(phase)
            with pr._held_recovery_window(self, self.paths, self.request_id):
                yield

    # LLM: 只同步消费本次短窗，返回之后不保留closure或权限。
    # 函数用途: 适配公共元数据短操作端口。
    def short_operation(self, phase, operation):
        with self.short_scope(phase):
            return operation()

    # LLM: 只认原恢复window的实际锁来源，不以source.active替代T持锁事实。
    # 函数用途: 判断是否正处于同线程的原短窗。
    def _holds_window(self):
        from .multiprocess_processing_recovery import OriginalProcessingRecoverySource

        return OriginalProcessingRecoverySource._holds_window(self)

    # LLM: 内部业务锁后不反向取T；关联输入仍消费原receipt身份复核。
    # 函数用途: 确认持久写叶位于本次合法短窗。
    def require_writer(self, phase):
        from .multiprocess_processing_recovery import _require_window_receipt

        self.require_original(phase)
        if not self._holds_window():
            raise self.rejection(phase)
        _require_window_receipt(self, phase)

    # LLM: 已有canonical只登记待补交描述，不重建答复或发执行许可。
    # 函数用途: 兼容原终态恢复的T外投影后段。
    def defer_terminal_projection(self, turn_id, store, settlement_ok):
        from .multiprocess_processing_recovery import OriginalProcessingRecoverySource

        OriginalProcessingRecoverySource.defer_terminal_projection(self, turn_id, store, settlement_ok)

    # LLM: 冷C复用原冻结owner/config路径和initialize=False，不物化业务Agent，不把None当有claim的空账。
    # 函数用途: 取得原请求所有者的既有会话存储。
    @property
    def conversation_store(self):
        from .multiprocess_processing_recovery import _recovery_store, _require_store_thread

        self.require_original("store")
        if self.payload.get("conversation_claim") is None:
            return None
        if self._store is None:
            self._store = _recovery_store(self)
        _require_store_thread(self, self._store)
        return self._store


# LLM: 原C只在同步scope中发布元数据source，共用pr当前窗口便于原recovery叶消费，退出共同失效，不授执行权。
# 函数用途: 为一次无路由收口安装并撤销公共写端口。
@contextmanager
def _closeout_source(gate, payload, reason):
    from ..common.request_execution_ports import (
        bind_request_context_rejection,
        request_execution_ports_scope,
    )
    from . import multiprocess_processing_recovery as pr
    from .queued_steer_expiry import _expiry_coordinator_source
    from .request_transactions import (
        missing_request_context_rejection,
        require_original_request_write_source,
    )

    controller, lease = _expiry_coordinator_source(gate)
    source = UnroutableCloseoutSource(gate, controller, lease, payload, reason)
    bind_request_context_rejection(missing_request_context_rejection, source_validator=require_original_request_write_source)
    token = pr._CURRENT.set(source)
    try:
        with request_execution_ports_scope(source):
            yield source
    finally:
        source._active = False
        pr._CURRENT.reset(token)


# LLM: 原指纹之外额外比冻结owner、route和owner_id，坏canonical不复用；仅复用相同退休错误码，不覆盖其他终态。
# 函数用途: 读回并验证本请求已经提交的无路由终态。
def _committed_closeout(paths, path, current):
    from .recovery import _same_gateway_request_identity
    from .request_owner_binding import frozen_request_owner

    report = read_gateway_terminal_envelope_report(paths.terminal / path.name, request_id=path.stem)
    if report.load_error is not None:
        raise DataCorruptionError("unroutable_terminal_unreadable")
    canonical = report.payload
    if not canonical:
        return None
    response = canonical["terminal_response"]
    if (response.get("error_code") != "GATEWAY_REQUEST_ROUTE_RETIRED"
            or canonical.get("turn_phase") != "closed" or canonical.get("status") != "failed"
            or response.get("status") != "failed" or response.get("ok") is not False
            or not _same_gateway_request_identity(canonical, current, request_id=path.stem)
            or frozen_request_owner(current) is None
            or frozen_request_owner(canonical) != frozen_request_owner(current)
            or canonical.get("owner_id") != current.get("owner_id")
            or canonical.get("route") != current.get("route")
            or any(canonical.get(key) != current.get(key) for key in (
                "conversation_claim", "lease_epoch", "active_turn_recovery", "queue_transport_source", "runtime_authority"))):
        raise DataCorruptionError("unroutable_terminal_conflict")
    return canonical


# LLM: 调用方已持原T；只进入真实C末门，不再取T，不建P3；保留inbox直到投影成功，已封终态只读复用。
# 函数用途: 在准入锁内终结失去路由的pending请求。
def close_out_unroutable_pending_locked(gate, request_path, current, owner, reason):
    from .input_delivery_service import require_gateway_prepared_identity
    from .multiprocess_admission import _blocked, _runtime_commit_scope
    from .queued_steer_expiry import _expiry_coordinator_source
    from .recovery import _commit_gateway_terminal_request, _terminal_gateway_request_payload
    from .request_owner_binding import frozen_request_owner

    try:
        _expiry_coordinator_source(gate)
        with _runtime_commit_scope(gate, owner) as rejection:
            if rejection:
                return rejection
            report = read_json_file_report(request_path)
            if report.load_error is not None:
                raise DataCorruptionError("unroutable_pending_unreadable")
            if report.payload != current:
                return {"status": "raced"}
            if (request_path.parent != gate.paths.inbox or current.get("id") != request_path.stem
                    or (gate.paths.processing / request_path.name).exists()
                    or frozen_request_owner(current) != owner
                    or unroutable_reason(gate.paths, current, gate=gate) != reason):
                raise DataCorruptionError("unroutable_pending_conflict")
            from .multiprocess_processing_recovery import _held_recovery_window

            with _closeout_source(gate, current, reason) as source, _held_recovery_window(source, gate.paths, request_path.stem):
                _require_pending_metadata(source)
                require_gateway_prepared_identity(gate.paths, current)
                canonical = _committed_closeout(gate.paths, request_path, current)
                if canonical is None:
                    response = unroutable_closeout_response(current, reason)
                    canonical = _terminal_gateway_request_payload(request_path.stem, current, response)
                _commit_gateway_terminal_request(gate.paths, gate.paths.terminal / request_path.name,
                                                 gate.paths.failed, request_path.stem, canonical)
            return {"status": "expired", "response": canonical["terminal_response"], "closeout": "unroutable"}
    except (OSError, ValueError, RuntimeError, DataCorruptionError, KeyError, TypeError):
        return _blocked("unroutable_closeout_pending")


# LLM: 原T/末门内只结算本请求queued输入；关联回执仍交原恢复结算器，不把非空输入当空账。
# 函数用途: 将从未执行的输入回执标成terminal_unknown并报告是否结清。
def _settle_queued_input(paths, request_id):
    from . import input_delivery_service as inputs

    summary = {"errors": 0}
    related = inputs._turn_input_request_ids(paths, request_id, summary)
    receipt = inputs.read_gateway_input_receipt(paths, request_id)
    settled = not related and not summary["errors"]
    if receipt is not None:
        if receipt.state == "queued" and not receipt.target_turn_id:
            inputs._write_terminal_unknown(paths, receipt, None)
        elif receipt.state not in {"consumed", "terminal_unknown"} or receipt.reconcile_error:
            settled = False
    return settled


# LLM: admit原T退出后再取T→C→lease，必须已提交退休终态且P3确实不存在；仅全部补交成功才移除inbox。
# 函数用途: 修复无路由pending的响应、历史、输入回执及投影标记。
def project_unroutable_closeout(paths, request_id, *, admission_gate=None):
    from .multiprocess_processing_recovery import requeued_closeout_scope
    from .queued_steer_expiry import _expiry_coordinator_source

    _expiry_coordinator_source(admission_gate)
    path = paths.inbox / (request_id + ".json")
    candidate = path if path.exists() else paths.terminal / path.name
    with requeued_closeout_scope(admission_gate, candidate) as rejection:
        if rejection is not None:
            raise BlockingIOError("unroutable_requeued_source_unavailable") from rejection
        return _project_unroutable_closeout(paths, request_id, admission_gate=admission_gate)


def _project_unroutable_closeout(paths, request_id, *, admission_gate=None):
    """复用原canonical补交链；所有长观察已在外层，关联input仍在T外结算。"""
    from .input_delivery_service import require_gateway_prepared_identity
    from .multiprocess_admission import _read_reservation, reservation_file
    from .recovery import _commit_gateway_terminal_request, _settle_recovered_guidance

    path = paths.inbox / (request_id + ".json")
    terminal = read_gateway_terminal_envelope_report(paths.terminal / path.name, request_id=request_id)
    if terminal.load_error is not None or not terminal.payload:
        raise DataCorruptionError("unroutable_terminal_unavailable")
    reason = unroutable_reason(paths, terminal.payload, gate=admission_gate)
    with _closeout_source(admission_gate, terminal.payload, reason) as source:
        with source.short_scope("unroutable.pending_projection"):
            current = read_json_file_report(path)
            if current.load_error is not None:
                raise DataCorruptionError("unroutable_pending_unreadable")
            payload = current.payload or terminal.payload
            canonical = _committed_closeout(paths, path, payload)
            if (not reason or canonical != source.payload or (paths.processing / path.name).exists()
                    or _read_reservation(reservation_file(paths, request_id)) is not None):
                raise DataCorruptionError("unroutable_projection_conflict")
            _require_pending_metadata(source)
            require_gateway_prepared_identity(paths, payload)
            require_gateway_prepared_identity(paths, canonical)
            _commit_gateway_terminal_request(paths, paths.terminal / path.name, paths.failed, request_id, canonical)
            store = source.conversation_store
            _unchanged, settled = _settle_recovered_guidance(request_id, canonical, canonical["terminal_response"], store, True)
            source.defer_terminal_projection(request_id, store, settled)
        settled = _finish_closeout_projection(paths, source)
        with source.short_scope("unroutable.pending_ack"):
            fresh = read_json_file_report(path)
            if fresh.load_error is not None or (fresh.payload and fresh.payload != current.payload):
                raise DataCorruptionError("unroutable_pending_conflict")
            if _committed_closeout(paths, path, fresh.payload or canonical) != canonical:
                raise DataCorruptionError("unroutable_terminal_conflict")
            if settled and fresh.payload:
                path.unlink()
        return settled


def _require_pending_metadata(source):
    """恢复排队另核原运输/停止证据；从未执行分支仍保留原严格门。"""
    from .multiprocess_processing_recovery import require_stopped_requeued_source
    from .queued_steer_expiry import _has_execution_responsibility, _require_never_started

    source.require_writer("unroutable.pending_metadata")
    if _has_execution_responsibility(source.payload):
        require_stopped_requeued_source(source)
        if source.conversation_store is None:
            raise source.rejection("unroutable.requeued_store")
    else:
        _require_never_started(source.payload)


# LLM: claim与任务只按原request绑定精确收尾；不扫别的任务、不动已提升业务任务，坏账保留canonical供重试。
# 函数用途: 结束原执行租约并清掉同一请求的活动任务索引。
def _finish_closeout_claim(payload, request_id, store):
    from .recovery import _finish_gateway_conversation_claim

    _finish_gateway_conversation_claim(payload, request_id, store)
    binding = payload.get("conversation_claim")
    if binding is None:
        return
    link = store.tasks.load(binding["task_id"])
    if link is None:
        return
    if link.thread_id != binding["thread_id"]:
        raise DataCorruptionError("unroutable_task_thread_conflict")
    status = {"done": "completed", "failed": "failed", "cancelled": "cancelled",
              "stopped": "cancelled", "interrupted": "cancelled"}.get(payload.get("status"))
    if link.status == "active" and status is not None:
        store.tasks.update_status({"task_id": binding["task_id"], "status": status, "expected_status": "active"})
    elif link.status in {"completed", "failed", "cancelled"}:
        store.tasks.update_status({"task_id": binding["task_id"], "status": link.status})


# LLM: 只用原终态恢复后段，关联输入在原T外结算，投影写仍回原source短门；失败保留canonical供下一轮修复。
# 函数用途: 为已封存的processing完成输入和展示补交。
def _finish_closeout_projection(paths, source):
    from .recovery import (
        _complete_gateway_terminal_projection,
        _project_recovered_terminal_response,
    )

    if source._terminal_projection is None:
        return
    turn_id, store, settled = source._terminal_projection
    with source.short_scope("unroutable.queued_input"):
        canonical = read_json_file_report(paths.terminal / (turn_id + ".json"))
        if canonical.load_error is not None or not canonical.payload:
            raise DataCorruptionError("unroutable_terminal_unavailable")
        _finish_closeout_claim(canonical.payload, turn_id, store)
        queued_settled = _settle_queued_input(paths, turn_id)
    settled = _complete_gateway_terminal_projection(paths, turn_id, store, settled)
    # 有关联输入时由原结算器作实际判断；本请求queued输入已在上方封口。
    with source.short_scope("unroutable.projection"):
        from .input_delivery_service import read_gateway_input_receipt

        receipt = read_gateway_input_receipt(paths, turn_id)
        if receipt is not None and (receipt.state not in {"consumed", "terminal_unknown"} or receipt.reconcile_error):
            settled = False
        # 关联输入交原complete结算器；queued-only无Store分支必须确实没有关联责任。
        settled = settled and (queued_settled if store is None else True)
        _project_recovered_terminal_response(paths, turn_id, settled)
    return settled


# LLM: 已有终态原operation优先，绝不重新失败或覆盖canonical；source只供这次元数据补交，退出立即失效。
# 函数用途: 在无P3的旧请求来源内执行已有终态恢复。
def settle_unroutable_terminal(paths, request_path, payload, context, reason, operation):
    with _closeout_source(context.admission_gate, payload, reason) as source:
        result = operation(paths, request_path, payload, context)
        _finish_closeout_projection(paths, source)
        return result


# LLM: 收口前在实际T/末门全文CAS、P3缺失和判死复核；先提交canonical保留hot，claim清理成功才退休hot，绝不restamp/requeue。
# 函数用途: 终结已失路由且执行者不活动的processing请求。
def close_out_unroutable_processing(paths, request_path, payload, context, reason):
    from .recovery import (
        _commit_gateway_terminal_request,
        _recovery_conversation_store,
        _settle_recovered_guidance,
        _terminal_gateway_request_payload,
    )

    with _closeout_source(context.admission_gate, payload, reason) as source:
        with source.short_scope("unroutable.processing"):
            report = read_json_file_report(request_path)
            if report.load_error is not None:
                raise DataCorruptionError("unroutable_processing_unreadable")
            if report.payload != payload:
                return ""
            if request_path.parent != paths.processing or request_path.stem != payload.get("id"):
                raise DataCorruptionError("unroutable_processing_identity")
            validated_gateway_request_fingerprint(payload, request_path.stem)
            if not unroutable_executor_dead(paths, request_path, payload, context):
                raise source.rejection("executor_unproven")
            # 已提交终态必须回原settle路径，不覆盖它，即使另一调用刚刚提交。
            if (paths.terminal / request_path.name).exists():
                from .recovery import _settle_committed_terminal

                result = _settle_committed_terminal(paths, request_path, payload, context)
            else:
                response = unroutable_closeout_response(payload, reason)
                canonical = _terminal_gateway_request_payload(request_path.stem, payload, response)
                store = _recovery_conversation_store(context.agent, payload)
                canonical, settled = _settle_recovered_guidance(request_path.stem, canonical, response, store, True)
                _commit_gateway_terminal_request(paths, paths.terminal / request_path.name,
                                                 paths.failed, request_path.stem, canonical)
                _finish_closeout_claim(canonical, request_path.stem, store)
                _commit_gateway_terminal_request(paths, request_path, paths.failed, request_path.stem, canonical)
                source.defer_terminal_projection(request_path.stem, store, settled)
                result = "failed"
        _finish_closeout_projection(paths, source)
        return result
