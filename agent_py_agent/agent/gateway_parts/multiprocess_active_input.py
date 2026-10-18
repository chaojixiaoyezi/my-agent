# LLM: 前台适配器只消费原invocation/source/Agent/claim；v3运输仅定位，不造执行权；probe纯观察、drain原T/P3短门追加，确认仍归原reserve→submit→ACK。
# 模块用途: 真实C/W安全点接收原文字；同一deadline贯穿每ref和原guidance/queue末写，过期不假idle或普通failed。
import time
from dataclasses import replace

from ..common.json_io import check_io_deadline
from ..common.request_execution_ports import (
    current_request_execution_source,
    gateway_short_transaction,
    gateway_transaction_writer,
)
from ..conversation.guidance_injection import guidance_not_yet_injected
from ..runtime_errors import DataCorruptionError
from . import input_delivery_service as inputs
from .execution_target import _claim_is_current
from .input_guidance_payload import input_guidance_payload, input_scope, original_input_guidance
from .input_target_observation import target_record
from .io import (
    read_json_file_report,
    try_gateway_turn_transition,
    update_json_file_atomic,
    write_json_file_atomic,
)
from .multiprocess_coordinator_execution import CoordinatorExecution
from .multiprocess_execution import (
    WorkerExecution,
    check_worker_transition,
    current_worker_execution,
)
from .multiprocess_input_reconcile import _identity_bytes
from .paths import GatewayPaths, gateway_paths_from_root
from .request_owner_binding import frozen_request_owner
from .request_transactions import OriginalRequestWriteSource


# LLM: 普通Count0只读ContextVar即None，零文件IO；仅原foreground invocation能绑定，不继承BG/child/host source，也不由params字符串创建身份。
# 函数用途: 在原Gateway RunParams构造处捕获当前真实来源。
def pending_turn_input_source(agent, request, thread_id):
    execution = current_worker_execution()
    if execution is None:
        from ..common.executor_registration import managed_request_execution_required

        if managed_request_execution_required():
            current_request_execution_source('input.transport.capture')
        return None
    if not thread_id:
        return None
    source = current_request_execution_source('input.transport.capture')
    if (type(execution) not in (WorkerExecution, CoordinatorExecution) or execution.agent is not agent
            or type(source) is not OriginalRequestWriteSource or source.background is not None
            or type(execution) is CoordinatorExecution and execution.child_binding is not None):
        return None
    source.require_original('input.transport.capture')
    claim = execution.claim if type(execution) is CoordinatorExecution else execution.binding.claim
    if request.get('id') != claim.request_id or not thread_id:
        raise source.rejection('input.transport.capture')
    return OriginalPendingTurnInput(execution, source, agent, claim, thread_id)


# LLM: 引用仅属原execution寿命，不能序列化/按字段恢复；所有消费与固定closing收口重核原source/current指针，scope退出或换Agent不得续权，不释放P3。
# 类用途: 给真实安全点及正常收口运输原输入；同一预算贯穿每ref与末写，严格未追加项才耐久queue。
class OriginalPendingTurnInput:
    # LLM: 构造只捕获原对象，不读receipt/新配置或申请P3；身份元组直接来自原GatewayClaim。
    # 函数用途: 固定这一真实前台回合，不允许之后按当前活动目标改绑。
    def __init__(self, execution, source, agent, claim, thread_id):
        self.execution, self.source, self.agent, self.claim, self.thread_id = execution, source, agent, claim, thread_id
        self._refs = (execution, source, agent, claim, thread_id)
        self._origin = self
        self.paths = gateway_paths_from_root(claim.path.parent.parent.parent)
        self._paths = vars(self.paths).copy()
        self.require_original(agent)

    # LLM: 先与捕获的原source/引用比较再解引用当前字段；缺失或替换仍由原source报typed，随后原cell完整验，不借C元数据或同名claim写库。
    # 函数用途: 在probe/drain和每个实际writer前核实原执行来源。
    def require_original(self, agent):
        original_source = self._refs[1]
        if (type(self) is not OriginalPendingTurnInput or self._origin is not self
                or any(a is not b for a, b in zip(
                    (self.execution, self.source, self.agent, self.claim, self.thread_id), self._refs))
                or type(self.execution) not in (WorkerExecution, CoordinatorExecution)
                or agent is not self.agent or current_worker_execution() is not self.execution
                or self.execution.agent is not self.agent or self.source.execution is not self.execution
                or self.source.background is not None or type(self.paths) is not GatewayPaths):
            raise original_source.rejection('input.transport.require')
        original_claim = self.execution.claim if type(self.execution) is CoordinatorExecution else self.execution.binding.claim
        canonical_paths = gateway_paths_from_root(original_claim.path.parent.parent.parent)
        if (self.claim is not original_claim or vars(self.paths) != vars(canonical_paths)
                or vars(self.paths) != self._paths
                or current_request_execution_source('input.transport.require') is not original_source):
            raise original_source.rejection('input.transport.require')
        original_source.require_original('input.transport.require')

    # LLM: 先保实际canonical多继承异常同对象，再核捕获source；仅普通BlockingIOError可由runtime在门外等待，不吞别类错误。
    # 函数用途: 确认这次资源忙仍属于原执行，而非来源失效或退出未知。
    def require_retryable_busy(self, agent, error):
        inputs._raise_input_transport_uncertain(error)
        self.require_original(agent)

    # LLM: 原source只拒绝下一retry的admission，不否定此前append事实，也不编造Bounded/物理退出；原tuple仍保P3与processing。
    # 函数用途: 为用尽等待预算生成原执行的保守未结事件。
    def retry_exhausted(self, agent):
        self.require_original(agent)
        return self.source.rejection('input.transport.retry_admission')

    # LLM: 先核真实原source，再核纯deadline；到期仅原来源拒绝下一步骤，不签权、等待或回滚已提交事实。
    # 函数用途: 为本次全部ref和末writer守同一个时间上界。
    def _require_budget(self, agent, deadline):
        self.require_original(agent)
        if deadline is not None and time.monotonic() >= deadline:
            raise self.retry_exhausted(agent)

    # LLM: 原processing/fence/owner/route和同会话claim共同核；probe/append只open，固定finish才可重进自身closing；缺/过期仍unknown，原T/P3不放宽。
    # 函数用途: 只读确认这仍是捕获的同一次前台执行。
    def _current(self, *, finishing=False):
        payload = target_record(self.claim.path)
        check_worker_transition(self.execution, payload)
        expected = {'schema_version': 'gateway_conversation_claim.v1', 'request_id': self.claim.request_id,
                    'thread_id': self.thread_id, 'task_id': 'gateway:' + self.claim.request_id}
        phases = {'open', 'closing'} if finishing else {'open'}
        if (payload.get('conversation_claim') != expected or payload.get('turn_phase') not in phases
                or payload.get('cancel_requested') and not finishing):
            raise self.source.rejection('input.transport.target')
        claim = target_record(self.agent.conversation_store.storage.background_claim_path(self.thread_id))
        if (not _claim_is_current(claim, self.thread_id) or claim.get('reason') != 'gateway_foreground_turn'
                or claim.get('task_id') != 'gateway:' + self.claim.request_id):
            raise self.source.rejection('input.transport.claim')
        return payload, claim

    # LLM: by_turn只定位本turn，逐条回读canonical/full fence；新C在target T内先ref后transport，未提交ref不授权，旧漏ref留C独立扫描补。
    # 函数用途: 只观察自己已发布的输入引用，不扫描共享历史或别的owner回执。
    # LLM: 本次deadline显式贯穿读后与末writer；source先核，资源busy退出全部门再等待，None低层调用不刷新预算。
    # 函数用途: 保持原固定动作及部分事实，不在已过期时开始下一副作用。
    def _receipts(self, agent, *, finishing=False, deadline=None):
        self._require_budget(agent, deadline)
        payload, claim = self._current(finishing=finishing)
        self._require_budget(agent, deadline)
        fence = {key: getattr(self.claim, key) for key in ('request_id', 'execution_attempt_id', 'lease_epoch', 'lease_owner')}
        for ref in inputs.transport_turn_input_references(self.paths, self.claim.request_id, deadline=deadline):
            self._require_budget(agent, deadline)
            receipt = inputs.read_gateway_input_receipt(self.paths, ref['request_id'], blocking=False)
            self._require_budget(agent, deadline)
            if receipt is None or receipt.request_id != ref['request_id'] or receipt.client_message_id != ref['client_message_id']:
                raise DataCorruptionError('input reference canonical identity disappeared or changed')
            if receipt.state in {'queued', 'consumed'}:
                continue
            if receipt.transport is None:
                if finishing:
                    continue
                # LLM: pre-ref读取到一半须先等原target T释放；锁内canonical仍未提交transport才可视为无选中运输，引用本身不签权。
                # 函数用途: 区分正在发布和已放弃的预定位，不因暂态漏掉已接收输入。
                with try_gateway_turn_transition(self.paths, self.claim.request_id) as acquired:
                    if not acquired:
                        raise BlockingIOError('input locator publication is busy')
                    receipt = inputs.read_gateway_input_receipt(self.paths, ref['request_id'], blocking=False)
                    self._require_budget(agent, deadline)
                    if receipt is None:
                        raise DataCorruptionError('input locator canonical receipt disappeared')
                    if receipt.transport is None or receipt.state in {'queued', 'consumed'}:
                        continue
            transport = receipt.transport
            if _identity_bytes(transport['gateway_claim']) != _identity_bytes(fence):
                continue
            from .control_service import _request_matches_scope

            if (transport['target']['thread_id'] != self.thread_id or transport['target']['claim_id'] != claim['claim_id']
                    or frozen_request_owner(receipt.prepared_request) != frozen_request_owner(payload)
                    or _identity_bytes(transport['route']) != _identity_bytes(payload.get('route'))
                    or not _request_matches_scope(payload, input_scope(receipt))):
                raise DataCorruptionError('input transport no longer matches original execution')
            self._require_budget(agent, deadline)
            yield receipt
        self._require_budget(agent, deadline)

    # LLM: params只验证本回合上界，不从字段建立执行身份；child/task-local即便继承opaque引用也不能领取父邮箱。
    # 函数用途: 拒绝参数链把原source带给另一个请求或递归子任务。
    def _require_params(self, params):
        if getattr(params, 'request_id', None) != self.claim.request_id or getattr(params, 'context_scope', '') == 'task_local':
            raise self.source.rejection('input.transport.params')

    # LLM: 已注入/已提交/已消费不触发额外模型轮；pending canonical但投影丢失仍可唤醒原幂等append补投影，未知不返回False。
    # 函数用途: 在结束/工具边界观察是否有本回合尚未注入的真实文字。
    # LLM: 本次deadline显式贯穿读后与末writer；source先核，资源busy退出全部门再等待，None低层调用不刷新预算。
    # 函数用途: 保持原固定动作及部分事实，不在已过期时开始下一副作用。
    def probe(self, agent, params, *, deadline=None):
        self._require_budget(agent, deadline)
        self._require_params(params)
        pending = False
        for receipt in self._receipts(agent, deadline=deadline):
            row = original_input_guidance(agent.conversation_store, receipt)
            if row is None or row.status == 'pending' and guidance_not_yet_injected(params, [row.entry]):
                pending = True
        self._require_budget(agent, deadline)
        return pending
    # LLM: append仅在原source短门，input T只try不倒取等待；全receipt CAS在追加前后均核，原guidance幂等与最终ACK负责去重，不重做模型请求。
    # 函数用途: 在原模型安全点把运输文字追加到原guidance，并补原receipt/by_turn投影。
    # LLM: 本次deadline显式贯穿读后与末writer；source先核，资源busy退出全部门再等待，None低层调用不刷新预算。
    # 函数用途: 保持原固定动作及部分事实，不在已过期时开始下一副作用。
    def drain(self, agent, params, *, deadline=None):
        self._require_budget(agent, deadline)
        self._require_params(params)
        if getattr(params, 'consume_pending_turn_input', True) is False:
            return
        for receipt in self._receipts(agent, deadline=deadline):
            with gateway_short_transaction('input.transport.append'):
                with try_gateway_turn_transition(self.paths, receipt.request_id) as acquired:
                    if not acquired:
                        raise BlockingIOError('gateway input transition is busy')
                    self._require_budget(agent, deadline)
                    self._append(receipt, agent, deadline=deadline)

        self._require_budget(agent, deadline)
    # LLM: 原release短窗/T/P3里先封closing，再按本turn fullreceipt处理严格未append输入；排queue不执行、不释P3/claim，模型/ACK从不重放。
    # 函数用途: 在正常请求仍持原来源时关闭活动收件窗口，把尚未追加的文字完整留给下一轮。
    # LLM: 本次deadline显式贯穿读后与末writer；source先核，资源busy退出全部门再等待，None低层调用不刷新预算。
    # 函数用途: 保持原固定动作及部分事实，不在已过期时开始下一副作用。
    def finish(self, agent, *, deadline=None):
        self._require_budget(agent, deadline)
        with gateway_short_transaction('release'):
            rows = tuple(self._receipts(agent, finishing=True, deadline=deadline))
            # LLM: 原JSON updater保心跳等并发字段；目标T已持，只有同fence/同claim可置closing，不能关闭别的attempt。
            # 函数用途: 与C pre-locator/transport提交共用原target T，使封口后的新输入只能排队。
            def close(current):
                self._require_budget(agent, deadline)
                check_worker_transition(self.execution, current)
                if current.get('turn_phase') not in {'open', 'closing'}:
                    raise self.source.rejection('input.transport.finish')
                return {**current, 'turn_phase': 'closing'}

            with gateway_transaction_writer('input.transport.close'):
                update_json_file_atomic(self.claim.path, close, require_existing=True, blocking=False, deadline=deadline)
            for receipt in rows:
                self._require_budget(agent, deadline)
                self._queue_unseen(receipt, agent, deadline=deadline)

        self._require_budget(agent, deadline)
    # LLM: 只在原finish/T/P3窗口内、canonical仍pending且无binding且v4严格缺席时排完整prepared；任何已append/submit/ACK事实都不重排。
    # 函数用途: 给封口时来不及进入模型的同一条输入确定耐久下一轮去向。
    # LLM: 本次deadline显式贯穿读后与末writer；source先核，资源busy退出全部门再等待，None低层调用不刷新预算。
    # 函数用途: 保持原固定动作及部分事实，不在已过期时开始下一副作用。
    def _queue_unseen(self, receipt, agent, *, deadline=None):
        if receipt.state != 'pending' or receipt.target_turn_id:
            return
        with try_gateway_turn_transition(self.paths, receipt.request_id) as acquired:
            if not acquired:
                raise BlockingIOError('gateway input transition is busy')
            self._require_budget(agent, deadline)
            self._require_receipt(receipt, deadline=deadline)
            if original_input_guidance(agent.conversation_store, receipt) is not None:
                return
            expected = _identity_bytes(receipt.prepared_request)

            # LLM: 固定原路径/载荷末核，不接受外部任意writer；source/receipt已改变或busy都不假queued，Count0不走此叶子。
            # 函数用途: 在原inbox原子写前复核同一完整输入。
            def writer(path, payload):
                self._require_budget(agent, deadline)
                self._require_receipt(receipt, deadline=deadline)
                if path != self.paths.inbox / (receipt.request_id + '.json') or _identity_bytes(payload) != expected:
                    raise self.source.rejection('input.transport.finish_queue')
                self._require_budget(agent, deadline)
                write_json_file_atomic(path, payload, blocking=False, deadline=deadline)

            with gateway_transaction_writer('input.transport.finish_queue'):
                inputs._materialize_prepared_request_locked(self.paths, receipt, write_request=writer, read_blocking=False)
                self._require_receipt(receipt, deadline=deadline)
                updated = replace(receipt, state='queued', updated_at=time.time(), reconcile_error={})
                write_json_file_atomic(inputs.gateway_input_receipt_path(self.paths, receipt.request_id), updated.to_dict(), blocking=False, deadline=deadline)

        self._require_budget(agent, deadline)
    # LLM: v3由原producer规范写入；比较完整raw JSON类型/所有字段，不经from_dict归一化抹掉bool/未知字段或旧transport。
    # 函数用途: 在真实append/receipt写前进行完整原输入CAS。
    # LLM: 本次deadline显式贯穿读后与末writer；source先核，资源busy退出全部门再等待，None低层调用不刷新预算。
    # 函数用途: 保持原固定动作及部分事实，不在已过期时开始下一副作用。
    def _require_receipt(self, receipt, *, deadline=None):
        self._require_budget(self.agent, deadline)
        report = read_json_file_report(inputs.gateway_input_receipt_path(self.paths, receipt.request_id),
                                      context='gateway.input_receipt.read', blocking=False, deadline=deadline)
        inputs._raise_for_input_read_report(report, 'gateway input receipt is unreadable')
        if _identity_bytes(report.payload) != _identity_bytes(receipt.to_dict()):
            raise DataCorruptionError('input receipt changed during forwarding')

        self._require_budget(self.agent, deadline)
    # LLM: 同一个原T/ports窗口已持有；只允许完整等价文字/原精确目标，append后busy/失败不丢canonical回执，重放复用原guidance ID。
    # 函数用途: 完成一条输入的原幂等追加和可修复绑定，不把追加等同模型已接受。
    # LLM: 本次deadline显式贯穿读后与末writer；source先核，资源busy退出全部门再等待，None低层调用不刷新预算。
    # 函数用途: 保持原固定动作及部分事实，不在已过期时开始下一副作用。
    def _append(self, receipt, agent, *, deadline=None):
        self._require_budget(agent, deadline)
        self._current()
        self._require_receipt(receipt, deadline=deadline)
        row = original_input_guidance(agent.conversation_store, receipt)
        self._require_budget(agent, deadline)
        if row is None or row.status == 'pending':
            with gateway_transaction_writer('input.transport.guidance'):
                agent.conversation_store.guidance.append_once(input_guidance_payload(receipt),
                    dedupe_key=receipt.guidance_dedupe_key, blocking=False, deadline=deadline)
        row = original_input_guidance(agent.conversation_store, receipt)
        self._require_budget(agent, deadline)
        if row is None:
            raise DataCorruptionError('input guidance write is unconfirmed')
        with gateway_transaction_writer('input.transport.receipt'):
            self._require_receipt(receipt, deadline=deadline)
            updated = replace(receipt, state='active_pending', target_turn_id=self.claim.request_id, updated_at=time.time())
            write_json_file_atomic(inputs.gateway_input_receipt_path(self.paths, receipt.request_id),
                                   updated.to_dict(), blocking=False, deadline=deadline)
            inputs._write_active_turn_ref(self.paths, updated, blocking=False, deadline=deadline)


# LLM: 仅正常live foreground收口调用；Count0 None零新增IO，原背景/子代理不借此关前台；新typed在旧best-effort catch之外传播。
# 函数用途: 用原请求仍有效的来源关闭同一次活动输入，unknown不落普通failed或清P3。
def finish_pending_turn_inputs(agent, request_id):
    execution = current_worker_execution()
    if execution is None:
        return
    source = current_request_execution_source('input.transport.finish.capture')
    if (type(execution) not in (WorkerExecution, CoordinatorExecution) or execution.agent is not agent
            or type(source) is not OriginalRequestWriteSource or source.background is not None
            or type(execution) is CoordinatorExecution and execution.child_binding is not None):
        return
    source.require_original('input.transport.finish.capture')
    claim = execution.claim if type(execution) is CoordinatorExecution else execution.binding.claim
    if claim.request_id != request_id:
        raise source.rejection('input.transport.finish.capture')
    from ..concurrency.interrupt import wait_interruptibly
    from ..conversation.input_resource_retry import (
        input_resource_deadline,
        run_input_safe_point,
    )

    deadline = input_resource_deadline()
    while True:
        if time.monotonic() >= deadline:
            raise source.rejection('input.transport.retry_admission')
        try:
            payload = target_record(claim.path)
            source.require_original('input.transport.finish.capture')
            check_io_deadline(deadline)
            break
        except BlockingIOError as error:
            inputs._raise_input_transport_uncertain(error)
            source.require_original('input.transport.finish.capture')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise source.rejection('input.transport.retry_admission') from error
            wait_interruptibly(min(0.05, remaining))
    binding = payload.get('conversation_claim')
    if not isinstance(binding, dict) or not binding.get('thread_id'):
        return
    adapter = OriginalPendingTurnInput(execution, source, agent, claim, binding['thread_id'])
    run_input_safe_point(adapter, agent, 'finish', deadline=deadline)
