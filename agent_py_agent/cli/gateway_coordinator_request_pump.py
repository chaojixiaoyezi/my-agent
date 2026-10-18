# LLM: cold C沿同一control/Root/lease/gate做元数据准入、恢复和queued/cursor修复；不构造Agent/签业务权，bound input另守原来源。
# 模块用途: 给允许的W请求盖章并修历史/原收件队列；不执行C或W业务，未结绑定输入明确延期。
from __future__ import annotations

import os
import time

from ..agent.gateway_parts.multiprocess_admission import (
    PersistentAdmission,
    coordinator_admission_gate,
)
from ..agent.gateway_parts.request_worker import (
    AdmissionLimits,
    GatewayInboxScanGate,
    dispatch_pending_requests,
)
from ..agent.gateway_parts.restart_service import RESTART_DRAIN_HOLD_REASON, restart_draining
from .gateway_loop_backoff import LoopErrorBackoff


# LLM: 此对象只运输原C引用；恢复/投影调用原pipeline与同一gate，不发布新准备回执、不制造Agent，不建第二队列/准入账。
# 类用途: 让cold C沿原公平扫描、恢复、终态与queued修复收口元数据，始终不消费新业务claim。
class CoordinatorMetadataRequestPump:
    # LLM: 来源核验先于原gate/Root登记；节流复用main纯内存helper，初始化不读owner或补种子；helper只在mainloop完成定义后lazy导入。
    # 函数用途: 捕获原cold上下文与唯一准入门，保存恢复/投影/queued独立退避与原报告观察，不把报告当权威。
    def __init__(self, context, paths):
        from .gateway_loops import _RecoverThrottle, _require_coordinator_loop_source

        _require_coordinator_loop_source(context)
        if context.agent is not None or paths is not context.paths:
            raise ValueError('cold_original_context_required')
        self.context = self._context_origin = context
        self.paths = self._paths_origin = paths
        self.control = self._control_origin = context.control_plane
        self.runtime = self._runtime_origin = context.drain_runtime
        self._controller_origin = self.control.controller()
        self._lease_origin = self.runtime._lease_origin
        self._origin, self._creator_pid = self, os.getpid()
        self._admission_gate = self._gate_origin = coordinator_admission_gate(self.control, paths, context)
        self.runtime.retain_admission(self._admission_gate)
        self.limits = AdmissionLimits.from_config(self.control.config)
        self._scan_gate = GatewayInboxScanGate()
        self.backoff = LoopErrorBackoff()
        self.tick_failed = False
        self._recover_throttle = _RecoverThrottle(self.control)
        self._recovery_backoff = LoopErrorBackoff()
        self._projection_backoff = LoopErrorBackoff()
        self._reconcile_backoff = LoopErrorBackoff()
        self._next_terminal_projection_at = 0.0
        self._next_input_reconcile_at = 0.0
        self._terminal_projection_startup = True
        self._last_recovery_report = self._last_projection_report = None
        self._last_input_report = None
        self.require_original()

    # LLM: 每拍只认原对象/PID/control/controller/lease/gate，复制、关闭或重绑定均只拒绝；最终写入仍由原T/controller/lease末门复核。
    # 函数用途: 在任何扫描或释放前阻止迟到、复制及失源pump继续写队列。
    def require_original(self):
        from .gateway_loops import _require_coordinator_loop_source

        if (type(self) is not CoordinatorMetadataRequestPump or self._origin is not self
                or self._creator_pid != os.getpid() or self.context is not self._context_origin
                or self.paths is not self._paths_origin or self.paths is not self.context.paths
                or self.control is not self._control_origin or self.control is not self.context.control_plane
                or self.runtime is not self._runtime_origin or self.runtime is not self.context.drain_runtime
                or self.control.controller() is not self._controller_origin
                or self.runtime._lease_origin is not self._lease_origin
                or self._admission_gate is not self._gate_origin
                or type(self._admission_gate) is not PersistentAdmission
                or self._admission_gate.agent is not self.control or self._admission_gate.paths is not self.paths
                or self._admission_gate._runtime_takeover is not self._controller_origin
                or self._admission_gate.count != self.control.worker_count
                or self._admission_gate.c_run_id != self.control.c_run_id):
            raise ValueError('cold_original_context_required')
        _require_coordinator_loop_source(self.context)

    # LLM: 原release/dispatch先行，W留pending且C gate禁止新claim；随后独立修复，queued/cursor末核同原源，bound延期不取Store。
    # 函数用途: 推进一拍cold准入和历史修复，不把route数或修复报告当业务完成或释放许可。
    def tick(self):
        self.require_original()
        self._admission_gate.release_settled()
        dispatched = dispatch_pending_requests(self.paths, self.limits, self._reject_business_submit,
            scan_gate=self._scan_gate, admission_gate=self._admission_gate,
            hold_reason=RESTART_DRAIN_HOLD_REASON if restart_draining() else '')
        self._repair_due()
        return dispatched

    # LLM: 不持T等待；恢复沿原timeout/3，投影及queued沿原0.75秒；失败/绑定输入延期仅推迟所属段，不阻塞dispatch。
    # 函数用途: 在同原来源仍活时运行到期修复，不创建后台线程或第二扫描游标。
    def _repair_due(self):
        from .gateway_loops import _PROJECTION_SITE, _RECONCILE_SITE, _RECOVERY_SITE

        self.require_original()
        if self._recover_throttle.due():
            delay = self._run_repair_segment(_RECOVERY_SITE, self._recovery_backoff, self._recover_once)
            self._recover_throttle.defer(delay)
        now = time.monotonic()
        if now >= self._next_terminal_projection_at:
            delay = self._run_repair_segment(_PROJECTION_SITE, self._projection_backoff, self._project_terminals_once)
            self._next_terminal_projection_at = now + max(0.75, delay)
        if now >= self._next_input_reconcile_at:
            delay = self._run_repair_segment(_RECONCILE_SITE, self._reconcile_backoff, self._reconcile_inputs_once)
            self._next_input_reconcile_at = now + max(0.75, delay)

    # LLM: 只读errors保原报告，不能从OK计数/文本重建异常、claim或entered；逃逸异常交原观察器保同对象，报告失败只增加本段退避。
    # 函数用途: 隔离一次修复的失败节奏，保留正常派发与另一修复段的机会。
    def _run_repair_segment(self, site, backoff, operation):
        from .gateway_loops import _note_loop_tick_error

        try:
            errors = operation()
        except Exception as error:
            _note_loop_tick_error(site, error, backoff)
            return backoff.delay()
        if errors:
            backoff.record_failure(site.context + ':report')
            return backoff.delay()
        backoff.record_success()
        return 0.0

    # LLM: 只执行healthy同义的周期startup=False；原startup/planned marker尚未运输，不猜首次tick。原Context.agent与gate.agent均是实际ControlPlane。
    # 函数用途: 用同一原P3/P2门调用既有processing恢复，原报告仅作本拍诊断，不建立新恢复实现。
    def _recover_once(self):
        from ..agent.gateway_parts.recovery import (
            _non_negative_int,
            _RecoveryContext,
            recover_gateway_processing_requests_report,
        )

        self.require_original()
        config = self.control.config
        context = _RecoveryContext(
            time.time(), _non_negative_int(config.gateway_request_max_attempts, default=1),
            max(1, int(config.gateway_processing_timeout_seconds or 1)), False, self.control, None,
            admission_gate=self._admission_gate,
        )
        self._last_recovery_report = None
        self._last_recovery_report = recover_gateway_processing_requests_report(self.paths, params=context)
        return self._last_recovery_report.load_errors

    # LLM: 复用原terminal有限页/完整pass/finite游标和恢复source末门；报告不授释放权，canonical未完成或Runtime未知仍保原责任。
    # 函数用途: 用同一原gate补terminal派生投影与原post欠账，不重写canonical答复或运行模型。
    def _project_terminals_once(self):
        from ..agent.gateway_parts.recovery import (
            _RecoveryContext,
            repair_gateway_terminal_projections,
        )

        self.require_original()
        startup, self._terminal_projection_startup = self._terminal_projection_startup, False
        context = _RecoveryContext(
            time.time(), 1, 1, False, self.control, None, admission_gate=self._admission_gate,
        )
        self._last_projection_report = None
        self._last_projection_report = repair_gateway_terminal_projections(
            self.paths, agent=self.control, limit=16, full_scan=startup, recovery_context=context,
        )
        return self._last_projection_report[1]

    # LLM: 只把同context/Root原gate交queued消费者，报告只诊断；延期参与独立退避，不进入旧foreign Agent/Store对账。
    # 函数用途: 修复原收件队列与扫描位置，保留尚未接入的绑定输入原文和责任。
    def _reconcile_inputs_once(self):
        from ..agent.gateway_parts.multiprocess_input_reconcile import (
            reconcile_coordinator_queued_inputs,
            require_coordinator_input_progress,
        )

        self.require_original()
        self._last_input_report = None
        self._last_input_report = reconcile_coordinator_queued_inputs(self.context, self.paths, self._admission_gate)
        require_coordinator_input_progress(self._last_input_report)
        return ()

    # LLM: 原metadata-only gate应先于P3拒C路由，本回调只防内部错误流入执行；不执行、重排或释放已经发生的claim/P3责任。
    # 函数用途: 明确拒绝把cold控制面当成业务执行者，不提供假提交成功。
    def _reject_business_submit(self, claim, user_key, conversation_key):
        raise RuntimeError('cold_coordinator_business_submit_forbidden')

    # LLM: 没有私有池或线程可关闭；原gate由同Root继续持有，关闭不删除P3/claim或以空资源表推断已排空。
    # 函数用途: 适配原请求循环收尾，真正停止及资源观察仍归原Root。
    def shutdown(self):
        return None
