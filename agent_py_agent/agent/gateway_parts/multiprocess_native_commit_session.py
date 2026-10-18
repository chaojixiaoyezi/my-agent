# LLM: C只消费同一次native receive的内核sender，原issuer/lease/P0/P4核验仍是唯一责任来源；ACK不是W写许可。
# 模块用途: 把逐消息原生传输接到既有begin/finish责任门，不伪装datagram为stream，不新增P1/P3或准入名额。
import json
import os
import threading
from weakref import WeakValueDictionary

from .multiprocess_commit_session import _request_identity, _unique_object, _wire_owner
from .multiprocess_kernel_message import KernelMessageReceiver, KernelMessageSender
from .multiprocess_native_control_receive import receive_control_packet
from .multiprocess_worker_commit import WorkerCommitIssuer, WorkerCommitRejected


# LLM: 只在原C内使用；handles仅引用原pin，定位地址/正文PID/序号/ACK均无授权作用。
# 类用途: 处理真实原W逐消息责任，保留断线或发送失败后的未结责任。
class NativeCommitSession:
    # LLM: 受信组合根交原issuer与本C创建的两个endpoint；构造不为owner准入、不读取body声明的身份。
    # 函数用途: 固定原对象、创建进程和一个串行消息消费门。
    def __init__(self, issuer, receiver, sender):
        if (type(issuer) is not WorkerCommitIssuer or type(receiver) is not KernelMessageReceiver
                or type(sender) is not KernelMessageSender):
            raise WorkerCommitRejected('original_native_source_required')
        issuer._require_current()
        receiver._check_creator()
        sender._check_creator()
        self._origin = self
        self._creator_pid = os.getpid()
        self._sources = (issuer, receiver, sender)
        self._lock = threading.RLock()
        self._last_seq = 0
        self._handles = {}
        self._operations = WeakValueDictionary()
        self._closed = False

    # LLM: creator/self检查在所有继承锁之前，closing不在此撤掉接收权，否则旧pin无法finish。
    # 函数用途: 拒绝浅拷贝和fork副本；原C仍可消费旧责任结束消息。
    def _require_original(self):
        if self._creator_pid != os.getpid():
            raise WorkerCommitRejected('original_coordinator_process_required')
        if self._origin is not self:
            raise WorkerCommitRejected('original_native_session_required')
        issuer, receiver, sender = self._sources
        if issuer._issuer_origin is not issuer or issuer._creator_pid != self._creator_pid:
            raise WorkerCommitRejected('original_commit_issuer_required')
        receiver._check_creator()
        sender._check_creator()

    # LLM: 只由可信C组合根注册原T/owner/callback；同原C/原W/request/attempt/phase共用原对象，跨facade不能重造回调。
    # 函数用途: 登记C内一次性短提交的定位引用，正文不能注册操作或声明其权限。
    def register_short_operation(self, owner, transition, phase, operation):
        from .multiprocess_native_operation_reclaim import reclaim_terminal_operations
        from .multiprocess_native_short_operation import NativeShortOperation

        self._require_original()
        issuer = self._sources[0]
        reclaim_terminal_operations(issuer)
        # 先读原T再取session门；不可持原C内存门争T。
        candidate = NativeShortOperation(issuer, owner, transition, phase, operation)
        launch, process = issuer._source_origin
        key = (process, launch.instance_id, transition.request_path, transition.execution_attempt_id, phase)
        with self._lock, issuer._controller._mutex:
            if self._closed:
                raise WorkerCommitRejected('native_session_closed')
            registry = getattr(issuer._controller, '_native_short_operations', None)
            if registry is None:
                registry = issuer._controller._native_short_operations = {}
            original = registry.get(key)
            if original is not None:
                with original._lock:
                    if original._used:
                        raise WorkerCommitRejected('native_operation_consumed')
                    previous_issuer = original._sources[0]
                    if (original._sources[1:4] != candidate._sources[1:4]
                            or original._sources[4] is not operation
                            or previous_issuer._source_origin[0] is not launch
                            or previous_issuer._source_origin[1] is not process
                            or previous_issuer._controller is not issuer._controller
                            or previous_issuer._lease is not issuer._lease):
                        raise WorkerCommitRejected('original_native_operation_conflict')
                    candidate = original
            else:
                registry[key] = candidate
            self._operations[candidate.identifier] = candidate
            return candidate.identifier

    # LLM: 每条packet独立核真实sender/原W；无FD信封先拒异常，分派helper在本门内运行，apply只在原C/T短门执行；同步native commit测试。
    # 函数用途: 处理一次完整无FD责任或C短提交并实际回发；closing仍可精确finish。
    def serve_once(self, *, timeout=5):
        self._require_original()
        with self._lock:
            if self._closed:
                raise WorkerCommitRejected('native_session_closed')
            issuer, receiver, sender = self._sources
            payload, peer_pid = receive_control_packet(receiver, timeout=timeout)
            # 不能先解析body或选handle再借首次连接PID；这个PID来自刚完成的同一次native receive。
            issuer._require_worker(peer_pid)
            request = _decode_packet(payload)
            seq, op = _native_request_identity(request, self._last_seq)
            self._last_seq = seq
            result = self._dispatch_request(request, peer_pid, seq, op)
            # 发送失败保留已经记录的原责任；不重放begin，不将本地处理称W已接收。
            KernelMessageSender.send(sender, json.dumps(result, separators=(',', ':')).encode(), timeout=timeout)
            return result

    # LLM: caller持原session门且已经同次认证sender/推进seq；仅分派现有C短操作/责任，不给W写权，finish沿原issuer。
    # 函数用途: 拆平四种原责任消息的处理，保持未知回执和精确finish。
    def _dispatch_request(self, request, peer_pid, seq, op):
        issuer = self._sources[0]
        if op == 'materialize':
            from .multiprocess_native_materialize import register_owner_materialization

            identifier = register_owner_materialization(self, request)
            result = self._apply_short_operation({'seq': seq, 'operation': identifier}, peer_pid)
        elif op == 'apply':
            result = self._apply_short_operation(request, peer_pid)
        elif op == 'begin':
            pin = issuer.begin(_wire_owner(request['owner']), peer_pid=peer_pid)
            self._handles[seq] = (issuer, pin)
            result = {'op': 'begun', 'seq': seq, 'handle': seq}
        else:
            handle = request['handle']
            if type(handle) is not int or handle not in self._handles:
                raise WorkerCommitRejected('original_commit_handle_required')
            pin_issuer, pin = self._handles[handle]
            pin_issuer._require_worker(peer_pid)
            pin_issuer.finish(pin)
            del self._handles[handle]
            result = {'op': 'finished', 'seq': seq, 'handle': handle}
        return result

    # LLM: 只从本session注册表拿原C回调；记录原pin早于实际I/O，异常或reply失败都保留它。
    # 函数用途: 运行一份精确短提交并把原pin接到同一个finish关联表。
    def _apply_short_operation(self, request, peer_pid):
        identifier = request['operation']
        operation = self._operations.get(identifier) if type(identifier) is str else None
        if operation is None:
            raise WorkerCommitRejected('original_native_operation_required')
        seq = request['seq']

        # LLM: 原C在原T内实际登记后同步调用；保存实际创pin的issuer/pin引用，不因合法facade换对象而改归属。
        # 函数用途: 部分失败或跨facade复用仍可让原issuer精确收尾。
        def remember(pin):
            self._handles[seq] = (operation._sources[0], pin)

        result = operation.apply(peer_pid, remember)
        return {'op': 'applied', 'seq': seq, 'handle': seq, 'result': result}

    # LLM: 不自动放弃handles，不允许C收尾先停接收而留下pin；只能在责任已精确结清后关闭原endpoint。
    # 函数用途: 结束本原对象的传输资源，未结责任时明确拒绝。
    def close(self):
        self._require_original()
        with self._lock:
            if self._handles:
                raise WorkerCommitRejected('original_native_responsibility_pending')
            if not self._closed:
                self._sources[1].close()
                self._sources[2].close()
                self._closed = True


# LLM: packet有自己的完整边界，不构造伪stream；唯一键/严格op字段复用原纯codec，异常无授权。
# 函数用途: 解析最多4096字节的完整native JSON控制帧。
def _decode_packet(payload):
    if type(payload) is not bytes or not 0 < len(payload) <= 4096:
        raise WorkerCommitRejected('commit_frame_size_invalid')
    try:
        value = json.loads(payload.decode('utf-8'), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise WorkerCommitRejected('commit_frame_invalid') from error
    if type(value) is not dict:
        raise WorkerCommitRejected('commit_frame_invalid')
    return value


# LLM: apply定位C注册操作；materialize仅引用请求/attempt，C实读原T/P3选owner，不接受body路径/owner/callback。
# 函数用途: 严格解析原begin/finish和C物理短提交的有限定位字段。
def _native_request_identity(request, previous):
    if request.get('op') == 'materialize':
        from .multiprocess_native_materialize import require_materialize_request

        require_materialize_request(request, previous)
        return request['seq'], 'materialize'
    if request.get('op') != 'apply':
        return _request_identity(request, previous)
    seq = request.get('seq')
    if (set(request) != {'op', 'seq', 'operation'} or type(seq) is not int or seq <= previous
            or type(request.get('operation')) is not str or not request['operation']):
        raise WorkerCommitRejected('commit_request_invalid')
    return seq, 'apply'
