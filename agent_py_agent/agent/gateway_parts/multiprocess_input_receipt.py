# LLM: C仅在实际HTTP work内提交固定receipt元数据，不申请/释放P3或写guidance；原input T→controller→lease→Root，整份CAS且JSON只试原锁。
# 模块用途: 保存唯一输入身份及固定去向；同一纯deadline贯穿原CAS末写，actual HTTP source仍唯一来源。
import time
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import replace

from ..common.request_execution_ports import RequestExecutionContextUnavailable
from ..conversation.input_resource_retry import input_resource_deadline
from . import input_delivery_service as inputs
from .input_target_observation import (
    active_text_options_match,
    input_background_task_guard,
    input_owner_store,
    observe_input_target,
    target_record,
)
from .input_transport import transport_gateway_request_id
from .io import (
    _read_json_dict_unlocked_report,
    gateway_turn_transition,
    locked_file_transition,
    read_json_file_report,
    try_gateway_turn_transition,
    write_json_file_atomic,
)
from .multiprocess_http_transport import capture_http_request_work, require_http_request_work
from .multiprocess_input_reconcile import _identity_bytes


# LLM: 纯固定拒绝不携路径/正文/工作对象，不伪造claim或operation_entered。
# 函数用途: 报告这次HTTP原元数据来源已不可核实。
def _missing():
    return RequestExecutionContextUnavailable('original_http_input_receipt_source_required')


# LLM: 仅FileNotFound为真实未物化路径；权限/类型/其它IO错误原样保未知，不创建目录或M锁，不签权。
# 函数用途: 读取原Store目录存在事实，避免被动queue为冷owner初始化资料。
def _store_directory_exists(path):
    import stat
    try:
        info = path.stat()
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(info.st_mode):
        raise inputs.DataCorruptionError('owner conversation store is not a directory')
    return True


# LLM: 只比较本次GatewayInputPreparation与旧完整canonical；无IO/owner热解析/时间比较，不把数据bundle当来源，调用方仍须原actual work与末CAS。
# 函数用途: 保留原认证peer/key/digest/goal和宿主冻结owner，允许submitted_at及新host投影不同。
def _pending_replay_matches(receipt, request):
    from .control_service import active_turn_guidance_dedupe_key
    from .http_handlers import http_input_identity
    from .input_guidance_payload import input_scope
    from .request_owner_binding import REQUEST_OWNER_KEY, frozen_request_owner

    incoming = request.prepared_request
    metadata = incoming.get('metadata') or {}
    conversation = incoming.get('conversation') or {}
    scope = input_scope(receipt)
    request_id, _identity = http_input_identity(user_id=incoming.get('user_id'), channel=metadata.get('channel'),
        conversation_id=conversation.get('channel_conversation_id') or conversation.get('id'),
        message_id=request.client_message_id)
    owner = frozen_request_owner(receipt.prepared_request)
    asserted = metadata.get(REQUEST_OWNER_KEY)
    return not (request_id != receipt.request_id or request.request_id != receipt.request_id
            or request.client_input_digest != receipt.client_input_digest
            or request.client_message_id != receipt.client_message_id
            or request.guidance_dedupe_key != receipt.guidance_dedupe_key
            or receipt.guidance_dedupe_key != active_turn_guidance_dedupe_key(scope)
            or (incoming.get('user_id'), metadata.get('channel'),
                conversation.get('channel_conversation_id') or conversation.get('id'))
                != (scope.user_id, scope.channel, scope.conversation_id)
            or incoming.get('goal') != receipt.prepared_request.get('goal')
            or incoming.get('kind') != receipt.prepared_request.get('kind') or owner is None
            or frozen_request_owner(incoming) not in (None, owner)
            or asserted is not None and frozen_request_owner({REQUEST_OWNER_KEY: asserted}) != owner)


# LLM: 只由实际handler同步创建，保存原work和原设施；冻结运输、queue、原证据binding与定位补漏各自固定CAS，不能由phase/fresh bool/任意callback授权或跨请求沿用。
# 类用途: 在实际HTTP寿命内按同一资源预算提交固定动作；lookup/deadline都不能变成写许可。
class CoordinatorInputReceiptSource:
    # LLM: 当前work必须已由原pool出生且绑定本handler，upper/lower与control/runtime/paths准确同源；构造不读业务库。
    # 函数用途: 捕获原HTTP处理区间，拒绝另一个server或晚回调。
    def __init__(self, handler, server):
        self.handler, self.server = handler, server
        self.work = capture_http_request_work(handler)
        self.control, self.runtime, self.paths = server.control_plane, server.drain_runtime, server.paths
        self._origin, self._fresh, self._seen = self, None, None
        self._refs = (handler, server, self.work, self.control, self.runtime, self.paths)
        self.require_original()

    # LLM: work本身核PID/thread/寿命/原pool登记；再核upper对象和原Root，config/Home/同runid不是许可。
    # 函数用途: 在每次读写前拒绝原设施交换、复制和退出。
    def require_original(self):
        if (type(self) is not CoordinatorInputReceiptSource or self._origin is not self
                or any(a is not b for a, b in zip(
                    (self.handler, self.server, self.work, self.control, self.runtime, self.paths), self._refs))
                or self.server.server is not self.work.server or self.server.control_plane is not self.control
                or self.server.drain_runtime is not self.runtime or self.server.paths is not self.paths
                or self.work.runtime is not self.runtime or self.work.control is not self.control
                or self.work.paths is not self.paths):
            raise _missing()
        require_http_request_work(self.work, handler=self.handler)

    # LLM: deadline只有资源含义，先核actual HTTP work/source；过期不签W tuple或ordinary TimeoutError，保持原typed出口。
    # 函数用途: 在每个固定步骤与末写前拒绝本次已过期预算。
    def _require_budget(self, deadline):
        self.require_original()
        if deadline is not None and time.monotonic() >= deadline:
            raise RequestExecutionContextUnavailable('original_http_input_receipt_budget_expired')

    # LLM: 同一原input T和Root，不创建授权；有预算只try原锁，忙先退出再由原调用方等待或503，不持Root等资源。
    # 函数用途: 给queue/binding/locator的固定CAS共用原短提交范围。
    @contextmanager
    def _receipt_scope(self, expected, deadline):
        self._require_budget(deadline)
        transition = (gateway_turn_transition(self.paths, expected.request_id) if deadline is None
                      else try_gateway_turn_transition(self.paths, expected.request_id))
        with transition as acquired:
            if acquired is False:
                raise BlockingIOError('gateway input transition is busy')
            self._require_budget(deadline)
            with self._root_scope():
                self._require_budget(deadline)
                yield

    # LLM: 调用方先持原input T；门内只同步try读写，不等待目标M/线程/模型，不借owner quarantine拒绝耐久收件。
    # 函数用途: 将元数据末写与原C关闭/撤权互斥。
    @contextmanager
    def _root_scope(self):
        self.require_original()
        with self.work.controller._mutex, self.work.lease._lifecycle_gate, self.runtime._gate:
            self.require_original()
            yield

    # LLM: 比较整份原编码，含schema/类型/transport/正文/actor/media；不是局部字段校验或重prepare。
    # 函数用途: 在实际提交前核同一receipt没有变化。
    # LLM: 同一deadline从动作入口到原锁内末写，原source先核；过期不新发运输/队列，不据此签权或清责任。
    # 函数用途: 在原固定动作中保全部CAS和已提交事实，资源失败沿原typed/503出口。
    def _require_receipt(self, expected, *, deadline=None):
        self._require_budget(deadline)
        seen = self._seen
        if seen is None or expected is not seen[0] or _identity_bytes(expected.to_dict()) != seen[2]:
            raise _missing()
        report = read_json_file_report(inputs.gateway_input_receipt_path(self.paths, expected.request_id),
                                      context='gateway.input_receipt.read', blocking=False, deadline=deadline)
        inputs._raise_for_input_read_report(report, 'gateway input receipt is unreadable')
        if _identity_bytes(report.payload) != seen[1]:
            raise _missing()

        self._require_budget(deadline)
    # LLM: 仅记本work这一个原receipt的原raw编码和typed投影；不是新账/registry，防可变dict或JSON归一化掩盖类型交换。
    # 函数用途: 为接下来的固定动作保留精确CAS期望，不重读或签发写许可。
    def _remember(self, receipt, raw_bytes):
        self._seen = (receipt, raw_bytes, _identity_bytes(receipt.to_dict()))
        return receipt

    # LLM: 仅收同次GatewayInputPreparation；首次prepare只一次；既存完整pending仅在load/_seen固定后走命名恢复叶子，created仍False且不填_fresh，不重跑owner/IM snapshot。
    # 函数用途: 保存首次完整输入，既存未绑定pending重放只补原队列；不物化owner或业务Agent。
    def prepare(self, request: inputs.GatewayInputPreparation):
        self.require_original()
        request_id = request.request_id
        with gateway_turn_transition(self.paths, request_id):
            path = inputs.gateway_input_receipt_path(self.paths, request_id)
            report = read_json_file_report(path, context='gateway.input_receipt.read', blocking=False)
            inputs._raise_for_input_read_report(report, 'gateway input receipt is unreadable')
            expected = _identity_bytes(report.payload) if report.payload else None
            if expected is None and path.exists():
                raise inputs.DataCorruptionError('gateway input receipt is empty')

            committed = []

            # LLM: callback仅原prepare同步调用，expected为同次原读事实；门内再核完整原raw，不允许外部任意写者或proposed receipt。
            # 函数用途: 在首次完整摘要之后提交唯一receipt，原缺失才记录本调用的fresh资格。
            def commit(updated):
                with self._root_scope():
                    current = read_json_file_report(path, context='gateway.input_receipt.read', blocking=False)
                    inputs._raise_for_input_read_report(current, 'gateway input receipt is unreadable')
                    actual = _identity_bytes(current.payload) if current.payload else None
                    if actual != expected or actual is None and path.exists():
                        raise _missing()
                    self.require_original()
                    write_json_file_atomic(path, updated.to_dict(), blocking=False)
                    committed.append(_identity_bytes(updated.to_dict()))
                    if expected is None:
                        self._fresh = (updated, _identity_bytes(updated.to_dict()))
                    return self._remember(updated, _identity_bytes(updated.to_dict()))

            result = inputs.load_or_prepare_gateway_input_request_locked(
                self.paths, request, commit_receipt=commit, read_blocking=False)
            self.require_original()
            self._remember(result[0], committed[0] if committed else expected)
        receipt, created = result
        if not created and receipt.state == 'pending' and not receipt.target_turn_id and receipt.transport is None:
            return self._recover_pending_input(receipt, request), False
        return result

    # LLM: 只由本次实际prepare严格load/_seen后调用；纯完整消息比较后共用唯一queue叶子，created仍False且不填_fresh、不重prepare。
    # 函数用途: 恢复prepare后退出或queue部分提交的同ID元数据去向，不扫描或执行Agent。
    def _recover_pending_input(self, receipt, request):
        self.require_original()
        if not _pending_replay_matches(receipt, request):
            raise ValueError('gateway pending replay identity conflicts')
        inputs._validate_prepared_request(receipt)
        return self.queue_unbound(receipt)

    # LLM: pending仅未绑定/无transport；已queued可保留原冻结运输审计而只修原队列，所有consumer都先跳过queued，不能借此重绑或active追加。
    # 函数用途: 将确定不能active的输入完整排原队列，崩溃后按同id原writer补齐。
    # LLM: 同一deadline从动作入口到原锁内末写，原source先核；过期不新发运输/队列，不据此签权或清责任。
    # 函数用途: 在原固定动作中保全部CAS和已提交事实，资源失败沿原typed/503出口。
    def queue_unbound(self, expected, *, deadline=None):
        deadline = input_resource_deadline() if deadline is None else deadline
        self._require_budget(deadline)
        if (expected.state not in {'pending', 'queued'}
                or expected.state != 'queued' and (expected.target_turn_id or expected.transport is not None)):
            raise _missing()
        from ..concurrency.interrupt import wait_interruptibly
        while True:
            self._require_budget(deadline)
            try:
                with self._receipt_scope(expected, deadline):
                    self._require_receipt(expected, deadline=deadline)
                    return self._queue_unbound_locked(expected, deadline)
            except BlockingIOError as error:
                inputs._raise_input_transport_uncertain(error)
                self._require_budget(deadline)
                wait_interruptibly(min(0.05, max(0.0, deadline-time.monotonic())))

    # LLM: 所有fresh/既存pending恢复共用此唯一队列叶子；原input T/Root内严格canonical去向，已存在guidance不queue，末writer保原完整prepared/CAS/deadline。
    # 函数用途: 共用原inbox与queued CAS，避免fresh canonical M检查和原queue分叉。
    def _queue_unbound_locked(self, expected, deadline):
        inputs._validate_prepared_request(expected)
        with self._queue_guidance_receipt(expected, deadline) as guidance:
            if guidance is not None:
                return expected
            payload_bytes = _identity_bytes(expected.prepared_request)

            # LLM: 原materialize唯一同步writer，路径/完整payload必须原样；不持短门等待JSON锁，不允许换业务目标。
            # 函数用途: 在原inbox实际写前完成最后来源和receipt CAS。
            def writer(path, payload):
                self._require_receipt(expected, deadline=deadline)
                if path != self.paths.inbox / (expected.request_id + '.json') or _identity_bytes(payload) != payload_bytes:
                    raise _missing()
                write_json_file_atomic(path, payload, blocking=False, deadline=deadline)

            inputs._materialize_prepared_request_locked(self.paths, expected, write_request=writer, read_blocking=False)
            self._require_receipt(expected, deadline=deadline)
            updated = replace(expected, state='queued', updated_at=time.time(), reconcile_error={})
            write_json_file_atomic(inputs.gateway_input_receipt_path(self.paths, expected.request_id),
                                   updated.to_dict(), blocking=False, deadline=deadline)
            self._require_budget(deadline)
            return self._remember(updated, _identity_bytes(updated.to_dict()))

    # LLM: 调用方持同input T/Root；pending完整raw无transport/binding，queued消费者原样跳过。正count ordinary/explicit同key写者均须同T，旧rolling主体不由此证明退出。
    # 函数用途: 已物化Store取原M严格读；尚未物化仅纯定址读取且零mkdir，初始化出现后转原M复核，持原门至queue末写。
    @contextmanager
    def _queue_guidance_receipt(self, expected, deadline):
        from .control_service import active_turn_guidance_dedupe_key
        from .http_handlers import http_input_identity
        from .input_guidance_observation import read_guidance_observation
        from .input_guidance_payload import input_scope

        scope = input_scope(expected)
        request_id, _identity = http_input_identity(user_id=scope.user_id, channel=scope.channel,
            conversation_id=scope.conversation_id, message_id=expected.client_message_id)
        if (request_id != expected.request_id
                or expected.state != 'queued' and (expected.transport is not None or expected.target_turn_id)
                or expected.guidance_dedupe_key != active_turn_guidance_dedupe_key(scope)):
            raise _missing()
        store = input_owner_store(self.control, expected)
        canonical = store.storage.guidance_dedupe_path(expected.guidance_dedupe_key)
        transition = canonical.with_name('.'+canonical.name+'.transition')
        with ExitStack() as guards:
            materialized = _store_directory_exists(store.storage.root)
            if materialized:
                guards.enter_context(locked_file_transition(transition, blocking=False))
            self._require_budget(deadline)
            row = read_guidance_observation(canonical, blocking=False, strict=True, deadline=deadline)
            if not materialized and _store_directory_exists(store.storage.root):
                guards.enter_context(locked_file_transition(transition, blocking=False))
                self._require_budget(deadline)
                row = read_guidance_observation(canonical, blocking=False, strict=True, deadline=deadline)
            self._require_receipt(expected, deadline=deadline)
            yield row

    # LLM: 只有本次原fresh对象选运输；input T→try target T→Root，先locator后完整transport CAS；普通busy同deadline门外等，目标原事实改变则排完整prepared，不热改已提交目标。
    # 函数用途: 为新文字输入固定第一次真实前台目标，确定idle/不等价才排完整下一轮。
    # LLM: 同一deadline从动作入口到原锁内末写，原source先核；过期不新发运输/队列，不据此签权或清责任。
    # 函数用途: 在原固定动作中保全部CAS和已提交事实，资源失败沿原typed/503出口。
    def route_fresh(self, expected, scope, *, allow_active):
        deadline = input_resource_deadline()
        self._require_budget(deadline)
        if self._fresh is None or self._fresh[0] is not expected or self._fresh[1] != _identity_bytes(expected.to_dict()):
            raise _missing()
        if not allow_active or not active_text_options_match(expected.prepared_request):
            return self.queue_unbound(expected, deadline=deadline)
        self._require_budget(deadline)
        state, transport, observation = observe_input_target(self.control, expected, scope)
        self._require_budget(deadline)
        if state in {'idle', 'ineligible'}:
            return self.queue_unbound(expected, deadline=deadline)
        if state == 'unpublished':
            return self._queue_fresh_without_guidance(expected, scope, observation, deadline)
        if state != 'exact':
            return expected
        from ..concurrency.interrupt import wait_interruptibly

        while True:
            self._require_budget(deadline)
            try:
                result = self._publish_transport(expected, transport, observation, deadline=deadline)
                if result is None and transport['schema_version'] == 'gateway_input_transport.v2':
                    return expected  # 原发布观察漂移保pending；G0仅独立unpublished+actual fresh完整证据门。
                return self.queue_unbound(expected, deadline=deadline) if result is None else result
            except BlockingIOError as error:
                inputs._raise_input_transport_uncertain(error)
                self._require_budget(deadline)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._require_budget(deadline)
                    raise
                wait_interruptibly(min(0.05, remaining))

    # LLM: 仅本次actual HTTP亲自缺席创建的同fresh对象可走此门；旧pending/已发transport/已绑定无资格。原input T→try task→Root→try claim/task/thread JSON→M持到queue末CAS，不新增authority。
    # 函数用途: 在同消息完整身份和当前原raw、canonical guidance严格缺席下，把正常BG未发布首拍完整排原队列。
    def _queue_fresh_without_guidance(self, expected, scope, observation, deadline):
        from ..concurrency.interrupt import wait_interruptibly
        from .control_service import active_turn_guidance_dedupe_key
        from .input_guidance_payload import input_scope

        self._require_budget(deadline)
        original = input_scope(expected)
        if (self._fresh is None or self._fresh[0] is not expected
                or self._fresh[1] != _identity_bytes(expected.to_dict())
                or expected.state != 'pending' or expected.target_turn_id or expected.transport is not None
                or (original.user_id, original.channel, original.conversation_id)
                    != (scope.user_id, scope.channel, scope.conversation_id)
                or expected.client_message_id != scope.metadata.get('message_id')
                or expected.client_input_digest != scope.metadata.get('client_input_digest')
                or expected.guidance_dedupe_key != active_turn_guidance_dedupe_key(scope)):
            raise _missing()
        inputs._validate_prepared_request(expected)
        while True:
            self._require_budget(deadline)
            try:
                with self._unpublished_scope(expected, observation, deadline):
                    return self._queue_fresh_locked(expected, observation, deadline)
            except BlockingIOError as error:
                inputs._raise_input_transport_uncertain(error)
                self._require_budget(deadline)
                wait_interruptibly(min(0.05, max(0.0, deadline - time.monotonic())))

    # LLM: 调用方已持原input/task/Root及claim/task/thread JSON锁；全raw不变且原current claim及M内canonical严格缺席才共用原完整queue writer。
    # 函数用途: 在固定短门中一次提交fresh队列，raw变化或已有guidance仅延期，不热试旧观察。
    def _queue_fresh_locked(self, expected, observation, deadline):
        from .execution_target import _claim_is_current

        _store, claim_path, claim, task_path, task, thread_path, thread = observation
        self._require_receipt(expected, deadline=deadline)
        raw_pairs = ((claim_path, claim), (task_path, task), (thread_path, thread))
        if (any(_identity_bytes(_read_json_dict_unlocked_report(path)[0]) != _identity_bytes(raw)
                for path, raw in raw_pairs) or not _claim_is_current(claim, claim['thread_id'])):
            return expected
        return self._queue_unbound_locked(expected, deadline)

    # LLM: 原发布task→T→claim关系不倒序阻塞；这里只try原task及三份JSON锁，持原raw到queue完成，任何busy退出Root/input T后才重试同deadline。
    # 函数用途: 固定未发布观察的原claim/task/thread快照，避免心跳或绑定在排队中间漂移。
    @contextmanager
    def _unpublished_scope(self, expected, observation, deadline):
        store, claim_path, claim, task_path, _task, thread_path, _thread = observation
        with ExitStack() as guards:
            acquired = guards.enter_context(try_gateway_turn_transition(self.paths, expected.request_id))
            if not acquired:
                raise BlockingIOError('gateway input transition is busy')
            self._require_budget(deadline)
            guards.enter_context(input_background_task_guard(store, claim['task_id']))
            guards.enter_context(self._root_scope())
            for path in (claim_path, task_path, thread_path):
                guards.enter_context(locked_file_transition(path, blocking=False))
                self._require_budget(deadline)
            yield

    # LLM: 固定一次publish只在input T→try target T→Root内，任何busy退出全部门后才由route原deadline等待；完整观察改变不热选新目标。
    # LLM: v2的Gateway T取gateway_claim键，by_turn保业务invocation；原task guard只try且持到末CAS，合法raw漂移保BG pending/deferred，不借G0重选或排队。
    # 函数用途: 先发原locator再原子保存完整transport，旧目标变化不热重选；BG未知保持延期。
    # LLM: 同一deadline从动作入口到原锁内末写，原source先核；过期不新发运输/队列，不据此签权或清责任。
    # 函数用途: 在原固定动作中保全部CAS和已提交事实，资源失败沿原typed/503出口。
    def _publish_transport(self, expected, transport, observation, *, deadline=None):
        self._require_budget(deadline)
        with try_gateway_turn_transition(self.paths, expected.request_id) as input_acquired:
            if not input_acquired:
                raise BlockingIOError('gateway input transition is busy')
            self._require_budget(deadline)
            with try_gateway_turn_transition(self.paths, transport_gateway_request_id(transport)) as acquired:
                if not acquired:
                    raise BlockingIOError('gateway input target transition is busy')
                self._require_budget(deadline)
                guard = (input_background_task_guard(observation[0], transport['target']['task_id'])
                         if transport['schema_version'] == 'gateway_input_transport.v2' else nullcontext())
                with guard, self._root_scope():
                    self._require_budget(deadline)
                    self._require_receipt(expected, deadline=deadline)
                    _store, claim_path, claim, path, payload = observation[:5]
                    current = target_record(path)
                    self._require_budget(deadline)
                    if (_identity_bytes(target_record(claim_path)) != _identity_bytes(claim)
                            or _identity_bytes(current) != _identity_bytes(payload)
                            or len(observation) > 5 and (
                                _identity_bytes(target_record(observation[5])) != _identity_bytes(observation[6])
                                or _identity_bytes(target_record(observation[7])) != _identity_bytes(observation[8]))):
                        # LLM: transport尚未提交，原同次目标观察失配就排完整prepared；不热选另一个target，原队列准入仍独立守P3/会话。
                        # 函数用途: 给发布前的closing/换代或心跳竞争固定下一轮去向，不留下新未绑定欠账。
                        return None
                    updated = replace(expected, transport=transport, updated_at=time.time())
                    self._require_budget(deadline)
                    inputs._write_active_turn_ref(self.paths, updated, blocking=False,
                        target_turn_id=transport['target']['turn_id'], deadline=deadline)
                    self._require_receipt(expected, deadline=deadline)
                    write_json_file_atomic(inputs.gateway_input_receipt_path(self.paths, expected.request_id),
                                           updated.to_dict(), blocking=False, deadline=deadline)
                    self._require_budget(deadline)
                    return self._remember(updated, _identity_bytes(updated.to_dict()))

    # LLM: 仅已有完整canonical transport可补定位，当前HTTP work/full raw CAS仍是写源；不fresh选新目标或根据ref反签transport。
    # 函数用途: 在客户端重放时幂等补回原by_turn查找投影。
    # LLM: 同一deadline从动作入口到原锁内末写，原source先核；过期不新发运输/队列，不据此签权或清责任。
    # 函数用途: 在原固定动作中保全部CAS和已提交事实，资源失败沿原typed/503出口。
    def repair_transport_locator(self, expected, *, deadline=None):
        deadline = input_resource_deadline() if deadline is None else deadline
        self._require_budget(deadline)
        if expected.transport is None:
            return expected
        with self._receipt_scope(expected, deadline):
            self._require_receipt(expected, deadline=deadline)
            inputs._write_active_turn_ref(self.paths, expected, blocking=False,
                target_turn_id=expected.transport['target']['turn_id'], deadline=deadline)
            self._require_receipt(expected, deadline=deadline)
        return expected

    # LLM: 先按完整canonical运输补原lookup，再仅消费原v4同input全文摘要/owner/thread/目标证据修binding；不认领、不判ACK，缺失/legacy/中间态保pending，无Store副作用。
    # 函数用途: 在追加后崩溃或客户端重放时修回原receipt绑定和原by_turn查找投影。
    # LLM: 同一deadline从动作入口到原锁内末写，原source先核；过期不新发运输/队列，不据此签权或清责任。
    # 函数用途: 在原固定动作中保全部CAS和已提交事实，资源失败沿原typed/503出口。
    def bind_original_guidance(self, expected, *, deadline=None):
        deadline = input_resource_deadline() if deadline is None else deadline
        self._require_budget(deadline)
        if expected.transport is None or expected.state not in {'pending', 'active_pending', 'terminal_unknown', 'consumed'}:
            return expected
        from .input_guidance_payload import original_input_guidance
        from .input_target_observation import input_owner_store

        self.repair_transport_locator(expected, deadline=deadline)
        self._require_budget(deadline)
        store = input_owner_store(self.control, expected)
        observed = original_input_guidance(store, expected)
        if observed is None:
            return expected
        with self._receipt_scope(expected, deadline):
            self._require_receipt(expected, deadline=deadline)
            current = original_input_guidance(store, expected)
            if current is None or current.to_dict() != observed.to_dict():
                return expected
            updated = replace(expected, state='consumed' if expected.state == 'consumed' else 'active_pending',
                              target_turn_id=expected.transport['target']['turn_id'], updated_at=time.time())
            self._require_budget(deadline)
            write_json_file_atomic(inputs.gateway_input_receipt_path(self.paths, expected.request_id),
                                   updated.to_dict(), blocking=False, deadline=deadline)
            inputs._write_active_turn_ref(self.paths, updated, blocking=False, deadline=deadline)
            self._require_budget(deadline)
            return self._remember(updated, _identity_bytes(updated.to_dict()))
