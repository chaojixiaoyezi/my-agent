# LLM: 仅实际BG binder/current claim安装本栈opaque来源；v2投影只定位，所有追加和最终claim结算仍过原Gateway T/P3/P5，Count0不查询来源。
# 模块用途: 为主后台会话提供纯文字安全点与仍活claim的最终收件结算，不关闭整个Gateway或新建恢复队列。
from dataclasses import replace

from ..common.json_io import serialize_private_json_no_newline
from ..common.request_execution_ports import (
    current_request_execution_source,
    gateway_short_transaction,
    gateway_transaction_writer,
)
from ..conversation.claim_execution_binding import current_claim_execution
from ..runtime_errors import DataCorruptionError
from . import input_delivery_service as inputs
from .input_guidance_payload import input_guidance_payload, input_scope, original_input_guidance
from .input_target_observation import target_record
from .input_transport import validated_input_transport
from .io import (
    locked_file_transition,
    read_json_file_report,
    try_gateway_turn_transition,
    write_json_file_atomic,
)
from .multiprocess_coordinator_execution import CoordinatorExecution
from .multiprocess_execution import check_worker_transition, current_worker_execution
from .multiprocess_input_reconcile import _identity_bytes
from .paths import gateway_paths_from_root
from .request_owner_binding import frozen_request_owner
from .request_transactions import OriginalRequestWriteSource


# LLM: 无原claim/主BG时不查询Store或生成新身份；只从当前精确typed来源捕获，child/task-local不接父邮箱。
# 函数用途: 在实际RunParams组合点安装本次模型的BG输入句柄。
def bind_background_input(agent, params, binding):
    claim = current_claim_execution()
    if claim is None or params.context_scope == 'task_local' or claim.claim_scope_id != claim.thread_id:
        return None
    source = current_request_execution_source('input.background.capture')
    if type(source) is not OriginalRequestWriteSource or source.background is not binding.scope:
        raise source.rejection('input.background.capture')
    pending = BackgroundPendingInput(agent, params.request_id, binding, claim, source)
    previous = claim.gateway_input_source
    if previous is not None:
        previous.require_original(agent)
        if previous.invocation_id != params.request_id or previous.background is not binding.scope:
            raise source.rejection('input.background.capture')
    object.__setattr__(claim, 'gateway_input_source', pending)
    return pending


# LLM: exact类/原对象/当前指针共同核源；JSON永不构造本对象，旧params不继承后续core attempt的追加权。
# 类用途: 保存当前主BG模型实际身份，执行固定probe/append/final叶子。
class BackgroundPendingInput:
    # LLM: 构造只捕获已存在对象；无磁盘/模型/权限副作用，原claim槽仅本词法scope有效。
    # 函数用途: 保存本次invocation和原Gateway/source引用。
    def __init__(self, agent, invocation_id, binding, claim_binding, source):
        self.agent, self.invocation_id, self.binding, self.claim_binding, self.source = agent, invocation_id, binding, claim_binding, source
        self.background, self.execution = binding.scope, binding.scope.execution
        self.gateway_claim = self.execution.claim if type(self.execution) is CoordinatorExecution else self.execution.binding.claim
        self.paths = gateway_paths_from_root(self.gateway_claim.path.parent.parent.parent)
        self._refs = (agent, invocation_id, binding, claim_binding, source, self.background, self.execution, self.gateway_claim)
        self._paths_origin, self._paths = self.paths, vars(self.paths).copy()
        self._origin, self._authority, self._terminal = self, None, None

    # LLM: 仅当前publisher完成同claim CAS后调用；绑定来自原core callback，不能从投影重新签发模型身份。
    # 函数用途: 保存本次actual发布的core身份，供参数与Compact代次比较。
    def runtime_published(self, binding, authority, row):
        self.require_original(self.agent)
        if binding is not self.binding or authority.get('invocation_run_id') != self.invocation_id:
            raise self.source.rejection('input.background.publish')
        self._authority = dict(authority)
        self._transport(row)

    # LLM: current BG/source/claim槽及所有捕获引用须同对象；不放宽FG validator，不认结束后的LocalRunControl。
    # 函数用途: 在每个安全点及实际writer前确认原执行仍活。
    def require_original(self, agent):
        from .multiprocess_background_execution import (
            BackgroundRuntimeBinding,
            current_background_request,
        )
        if (type(self) is not BackgroundPendingInput or self._origin is not self or agent is not self.agent
                or self.invocation_id != self._refs[1]
                or any(a is not b for a, b in zip(
                    (self.agent, self.binding, self.claim_binding, self.source, self.background, self.execution, self.gateway_claim),
                    (self._refs[0], *self._refs[2:])))
                or self.paths is not self._paths_origin or vars(self.paths) != self._paths
                or type(self.binding) is not BackgroundRuntimeBinding
                or current_background_request() is not self.background or self.binding.scope is not self.background
                or current_claim_execution() is not self.claim_binding or self.claim_binding.gateway_input_source is not self
                or current_worker_execution() is not self.execution or self.execution.agent is not self.agent
                or self.source.background is not self.background or self.source.execution is not self.execution
                or current_request_execution_source('input.background.require') is not self.source):
            raise self.source.rejection('input.background.require')
        self.source.require_original('input.background.require')

    # LLM: typed优先沿原分类同对象上抛；普通忙只在所有门外用原helper等待。
    # 函数用途: 验证资源争用仍可在本次原scope内重试。
    def require_retryable_busy(self, agent, error):
        inputs._raise_input_transport_uncertain(error)
        self.require_original(agent)

    # LLM: 预算只限制本次IO，不推定物理结束或释放任何预留。
    # 函数用途: 返回原Gateway tuple的未结异常。
    def retry_exhausted(self, agent):
        self.require_original(agent)
        return self.source.rejection('input.background.retry_admission')

    # LLM: deadline原入口只取一次，读后/末写均核；不刷新provider余额。
    # 函数用途: 守住同一次全部refs的资源预算。
    def _budget(self, deadline):
        import time
        self.require_original(self.agent)
        if deadline is not None and time.monotonic() >= deadline:
            raise self.retry_exhausted(self.agent)

    # LLM: 只使用原publisher同claim/core/reference完整字段；Gateway与业务两组attempt分别验证。
    # 函数用途: 从原已发布claim投影得到当前精确v2快照。
    def _transport(self, row):
        from .execution_target import GatewayExecutionTarget
        authority = row.get('runtime_authority', {})
        if (self._authority is None or any(authority.get(k) != self._authority.get(k) for k in
                ('task_id', 'run_id', 'agent_run_id', 'attempt_id', 'invocation_run_id'))):
            raise self.source.rejection('input.background.authority')
        target = GatewayExecutionTarget(self.claim_binding.thread_id, 'task', self.invocation_id,
            self.claim_binding.claim_id, authority['attempt_id'], authority, authority['task_id'])
        payload = target_record(self.gateway_claim.path)
        check_worker_transition(self.execution, payload)
        ref = row.get('gateway_transport_reference')
        fence = {'request_id': self.gateway_claim.request_id, 'execution_attempt_id': self.gateway_claim.execution_attempt_id,
                 'lease_epoch': self.gateway_claim.lease_epoch, 'lease_owner': self.gateway_claim.lease_owner}
        transport = validated_input_transport({'schema_version': 'gateway_input_transport.v2', 'target': target.to_dict(),
            'gateway_claim': fence, 'route': payload['route'], 'claim_transport_reference': ref})
        expected = {'schema_version': 'gateway_runtime_authority.v1', 'request_id': fence['request_id'],
                    'gateway_execution_attempt_id': fence['execution_attempt_id'],
                    **{k: authority[k] for k in ('task_id', 'run_id', 'agent_run_id', 'attempt_id')}}
        if payload.get('runtime_authority') != expected or payload.get('turn_phase') != 'open':
            raise self.source.rejection('input.background.authority')
        return transport, payload

    # LLM: running/本栈actual terminal两种模式分开；finished JSON不能在scope外恢复写源。
    # 函数用途: 读取同一次原claim，允许已成功finish后的剩余固定结算。
    def _current(self, *, finishing=False):
        self.require_original(self.agent)
        path = self.claim_binding.claims.storage.background_claim_path(self.claim_binding.claim_scope_id)
        row = target_record(path)
        if self._terminal is not None:
            if not finishing or _identity_bytes(row) != self._terminal:
                raise self.source.rejection('input.background.terminal')
        elif (row.get('status') != 'running' or row.get('thread_id') != self.claim_binding.thread_id
                or row.get('claim_scope_id') != self.claim_binding.claim_scope_id or row.get('claim_id') != self.claim_binding.claim_id):
            raise self.source.rejection('input.background.claim')
        transport, payload = self._transport(row)
        return row, transport, payload

    # LLM: 业务by_turn只查本invocation，canonical/full Gateway fence与同claim逐条核；pre-ref无transport不授权。
    # 函数用途: 列出本claim实际可观察的输入，不扫描共享历史。
    def _receipts(self, *, finishing=False, deadline=None):
        self._budget(deadline)
        _row, transport, payload = self._current(finishing=finishing)
        for ref in inputs.transport_turn_input_references(self.paths, self.invocation_id, deadline=deadline):
            self._budget(deadline)
            receipt = inputs.read_gateway_input_receipt(self.paths, ref['request_id'], blocking=False)
            if receipt is None or receipt.request_id != ref['request_id'] or receipt.client_message_id != ref['client_message_id']:
                raise DataCorruptionError('background input locator canonical changed')
            if receipt.state in {'queued', 'consumed'} or receipt.transport is None:
                continue
            value = validated_input_transport(receipt.transport)
            if value.get('schema_version') != 'gateway_input_transport.v2':
                continue
            from .input_target_observation import background_input_peer_matches
            actual = value['claim_transport_reference']
            expected = transport['claim_transport_reference']
            if (_identity_bytes(value['gateway_claim']) != _identity_bytes(transport['gateway_claim'])
                    or _identity_bytes(value['route']) != _identity_bytes(transport['route'])
                    or any(type(actual[k]) is not type(v) or actual[k] != v for k, v in expected.items() if k != 'core_attempt_id')
                    or frozen_request_owner(receipt.prepared_request) != frozen_request_owner(payload)
                    or not background_input_peer_matches(self.agent.conversation_store, self.claim_binding.thread_id, input_scope(receipt))):
                raise self.source.rejection('input.background.receipt')
            yield receipt, value == transport
        self._budget(deadline)

    # LLM: 旧params不能因新core发布而追加旧attempt；递归本地scope不接父邮箱。
    # 函数用途: 核实际安全点仍属于本次模型参数。
    def _params(self, params):
        if (self._authority is None or params.request_id != self.invocation_id or params.context_scope == 'task_local'
                or params.attempt_id != self._authority['attempt_id'] or params.run_id != self._authority['run_id']):
            raise self.source.rejection('input.background.params')

    # LLM: 共享原纯注入ID reader，不复制集合；仅旧attempt未append不产生额外模型轮。
    # 函数用途: 观察本次真实输入是否需要原模型安全点消费。
    def probe(self, agent, params, *, deadline=None):
        from ..conversation.guidance_injection import guidance_not_yet_injected
        self.require_original(agent)
        self._params(params)
        pending = False
        for receipt, exact in self._receipts(deadline=deadline):
            row = original_input_guidance(agent.conversation_store, receipt, deadline=deadline)
            if (row is None and exact or row is not None and row.status == 'pending'
                    and guidance_not_yet_injected(params, [row.entry])):
                pending = True
        return pending

    # LLM: 完整input CAS只读原raw，任何漂移不由DTO归一化覆盖；同deadline到末writer。
    # 函数用途: 在固定append/queue前核同一输入原文。
    def _receipt_current(self, receipt, deadline):
        self._budget(deadline)
        report = read_json_file_report(inputs.gateway_input_receipt_path(self.paths, receipt.request_id),
            context='gateway.background.input.cas', blocking=False, deadline=deadline)
        inputs._raise_for_input_read_report(report, 'background input unreadable')
        if _identity_bytes(report.payload) != _identity_bytes(receipt.to_dict()):
            raise self.source.rejection('input.background.cas')

    # LLM: Gateway T→try input T，原幂等append_once后只补绑定；旧attempt无canonical不补新身份。
    # 函数用途: 将等价纯文字追加到原task邮箱，消费仍由原reserve/submit/ACK决定。
    def drain(self, agent, params, *, deadline=None):
        self.require_original(agent)
        self._params(params)
        if getattr(params, 'consume_pending_turn_input', True) is False:
            return
        for receipt, exact in self._receipts(deadline=deadline):
            with gateway_short_transaction('input.background.append'):
                with try_gateway_turn_transition(self.paths, receipt.request_id) as acquired:
                    if not acquired:
                        raise BlockingIOError('background input transition busy')
                    from .input_target_observation import input_background_task_guard
                    with input_background_task_guard(agent.conversation_store, self._authority['task_id']):
                        self._append(receipt, exact, deadline)

    # LLM: 已持原Gateway/input/task try门；末核真实claim和TaskLink，再按原append_once提交，不从外部接writer。
    # 函数用途: 完成一条本次纯文字的原幂等追加及receipt绑定。
    def _append(self, receipt, exact, deadline):
        agent = self.agent
        from ..conversation.models import ThreadTaskLink
        row_task = target_record(agent.conversation_store.storage.task_path(self._authority['task_id']))
        link = ThreadTaskLink.from_dict(row_task) if row_task else None
        if (link is None or link.thread_id != self.claim_binding.thread_id or link.status != 'active'):
            raise self.source.rejection('input.background.task')
        _claim_row, _transport, payload = self._current()
        from .input_target_observation import active_text_options_match
        if not active_text_options_match(receipt.prepared_request, payload):
            raise self.source.rejection('input.background.text_options')
        self._receipt_current(receipt, deadline)
        row = original_input_guidance(agent.conversation_store, receipt, deadline=deadline)
        if row is None and not exact:
            return
        if row is None:
            with gateway_transaction_writer('input.background.append'):
                agent.conversation_store.guidance.append_once(input_guidance_payload(receipt),
                    dedupe_key=receipt.guidance_dedupe_key, blocking=False, deadline=deadline)
        self._receipt_current(receipt, deadline)
        updated = replace(receipt, state='active_pending', target_turn_id=self.invocation_id)
        with gateway_transaction_writer('input.background.binding'):
            write_json_file_atomic(inputs.gateway_input_receipt_path(self.paths, receipt.request_id),
                updated.to_dict(), blocking=False, deadline=deadline)
            inputs._write_active_turn_ref(self.paths, updated, blocking=False, deadline=deadline)

    # LLM: 实际finish结果是同claim的输入线性化点；不设Gateway closing，不使用已finish的model LocalRunControl。
    # 函数用途: 在原仍活source中结算claim，并仅把严格未append输入排完整原队列。
    def finish_claim(self, request, *, deadline):
        with gateway_short_transaction('release'):
            self._budget(deadline)
            row, _transport, _payload = self._current(finishing=True)
            rows = tuple(self._receipts(finishing=True, deadline=deadline))
            if self._terminal is None:
                result = self.claim_binding.claims.finish(request, expected_raw=serialize_private_json_no_newline(row), blocking=False, deadline=deadline)
                if result is None:
                    raise self.source.rejection('input.background.finish_cas')
                self._terminal = _identity_bytes(result)
            for receipt, _exact in rows:
                self._settle_input(receipt, deadline)
            self._budget(deadline)

    # LLM: 同actual terminal只投影原consumed事实；完整v2/canonical缺席才可排，持原M到writer，reserved/submitted/未知不重排。
    # 函数用途: 补原ACK回执投影，并把尚未追加的完整输入留到原inbox，保旧P3责任。
    def _settle_input(self, receipt, deadline):
        with try_gateway_turn_transition(self.paths, receipt.request_id) as acquired:
            if not acquired:
                raise BlockingIOError('background input transition busy')
            path = self.agent.conversation_store.storage.guidance_dedupe_path(receipt.guidance_dedupe_key)
            with locked_file_transition(path.with_name('.' + path.name + '.transition'), blocking=False):
                self._receipt_current(receipt, deadline)
                row = original_input_guidance(self.agent.conversation_store, receipt, deadline=deadline)
                if row is not None:
                    if row.status == 'consumed':
                        self._project_consumed(receipt, deadline)
                    return
                if receipt.state != 'pending' or receipt.target_turn_id:
                    return
                expected = _identity_bytes(receipt.prepared_request)
                # LLM: 原materializer固定同步writer，末核完整payload/路径/source；不返回任意写许可。
                # 函数用途: 在原inbox原子替换前确认同一完整输入。
                def writer(path, payload):
                    self._current(finishing=True)
                    self._receipt_current(receipt, deadline)
                    if path != self.paths.inbox / (receipt.request_id + '.json') or _identity_bytes(payload) != expected:
                        raise self.source.rejection('input.background.queue')
                    write_json_file_atomic(path, payload, blocking=False, deadline=deadline)
                with gateway_transaction_writer('input.background.queue'):
                    inputs._materialize_prepared_request_locked(self.paths, receipt, write_request=writer, read_blocking=False)
                    self._receipt_current(receipt, deadline)
                    queued = replace(receipt, state='queued', reconcile_error={})
                    write_json_file_atomic(inputs.gateway_input_receipt_path(self.paths, receipt.request_id),
                        queued.to_dict(), blocking=False, deadline=deadline)

    # LLM: 仅上层已核原consumed及同input/M/T后调用；末writer再核全receipt/source，不生成ACK或重放业务。
    # 函数用途: 将原guidance消费事实补到唯一input回执，减少收口分支嵌套。
    def _project_consumed(self, receipt, deadline):
        with gateway_transaction_writer('input.background.consumed_projection'):
            self._receipt_current(receipt, deadline)
            consumed = replace(receipt, state='consumed', reconcile_error={})
            write_json_file_atomic(inputs.gateway_input_receipt_path(self.paths, receipt.request_id),
                consumed.to_dict(), blocking=False, deadline=deadline)


# LLM: 无当前实际BG handle时原finish同参数直调；有则仅本栈重试IO，不重跑业务/finish，不延长claim来源。
# 函数用途: 在原_finish_slice调用点结算同claim和输入，四typed及部分提交事实原样传播。
def finish_background_claim_inputs(claims, request):
    binding = current_claim_execution()
    source = binding.gateway_input_source if binding is not None else None
    if source is None or source._authority is None:
        return claims.finish(request)
    import time

    from ..concurrency.interrupt import wait_interruptibly
    from ..conversation.input_resource_retry import input_resource_deadline
    deadline = input_resource_deadline()
    while True:
        source._budget(deadline)
        try:
            return source.finish_claim(request, deadline=deadline)
        except BlockingIOError as error:
            source.require_retryable_busy(source.agent, error)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise source.retry_exhausted(source.agent) from error
            wait_interruptibly(min(0.05, remaining))
