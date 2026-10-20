# LLM: 原W只读取原父进程的同次kernel reply；返回值报告C已完成的事，绝不授权W侧文件/工具副作用。
# 模块用途: 提供无重试的W客户端；strict原父同seq负回执只清本RPC phantom，未知交付及业务责任保持。
import json
import os
import threading
import time
from dataclasses import asdict

from ..user_space.owner_resolver import OwnerIdentity
from .multiprocess_commit_session import _unique_object
from .multiprocess_executor_identity import process_birth
from .multiprocess_kernel_message import KernelMessageReceiver, KernelMessageSender
from .multiprocess_native_control_receive import receive_control_packet
from .multiprocess_worker_commit import WorkerCommitRejected

_REJECTED_REPLY_PROOF = object()


# LLM: 只由原父kernel消息、strict形状和本RPC seq关联成功后产生；不运输C实际异常对象，不释放claim/P3或允许自动重试。
# 类用途: 报告本次native RPC已被明确pre-pin拒绝，与超时或效果未知区分。
class NativeCommitNotStarted(WorkerCommitRejected):
    # LLM: proof限本模块实际receive路径；origin/client/seq用于调用方准确定位，不是业务授权。
    # 函数用途: 保存本地已认证的负回执，普通同文异常不可代替。
    def __init__(self, client, seq, *, proof):
        if proof is not _REJECTED_REPLY_PROOF:
            raise WorkerCommitRejected('native_reply_unconfirmed')
        self._origin, self._creator, self._client = self, os.getpid(), client
        self.seq = seq
        self._materialize_origin = None
        super().__init__('native_commit_not_started')


# LLM: 原endpoint仅定位传输；creator/父P0/kernel sender分别核验，父身份与ACK不授予W写权限。
# 类用途: 串行发送本原W的责任/短提交消息，并核真实C回复。
class NativeCommitClient:
    # LLM: endpoint由本W创建；父PID来自真实getppid而非argv/body，出生未知即拒绝。
    # 函数用途: 捕获本原对象、原父出生和有限的在途关联状态。
    def __init__(self, receiver, sender):
        if type(receiver) is not KernelMessageReceiver or type(sender) is not KernelMessageSender:
            raise WorkerCommitRejected('original_native_endpoint_required')
        receiver._check_creator()
        sender._check_creator()
        self._origin, self._creator_pid = self, os.getpid()
        parent_pid = os.getppid()
        parent_birth = process_birth(parent_pid)
        if parent_birth is None:
            raise WorkerCommitRejected('original_coordinator_parent_required')
        self._parent = (parent_pid, parent_birth)
        self._sources = (receiver, sender)
        self._lock = threading.RLock()
        self._seq = 0
        self._pending = set()
        self._uncertain = set()
        self._inflight = {}
        self._finish_seq = {}
        self._completed_finishes = {}
        self._rejections = {}
        self._closed = False

    # LLM: self/creator必须在继承锁之前；真实父出生持续相同也不是业务授权，只用于拒错对端。
    # 函数用途: 拒绝复制/fork/父进程退出或PID替用后的旧客户端。
    def _require_original(self):
        if self._creator_pid != os.getpid() or self._origin is not self:
            raise WorkerCommitRejected('original_native_client_required')
        parent_birth = process_birth(self._parent[0])
        if (os.getppid() != self._parent[0] or parent_birth is None
                or self._parent[1] is None or parent_birth != self._parent[1]):
            raise WorkerCommitRejected('original_coordinator_parent_required')
        self._sources[0]._check_creator()
        self._sources[1]._check_creator()

    # LLM: 只有责任登记；不可把这个handle包在W callback外面充当写门。
    # 函数用途: 请求原C为本owner记录责任，并返回仅作定位的关联号。
    def begin_responsibility(self, owner, *, timeout=5):
        if type(owner) is not OwnerIdentity:
            raise WorkerCommitRejected('commit_owner_invalid')
        return self._request({'op': 'begin', 'owner': asdict(owner)}, 'begun', timeout)['handle']

    # LLM: operation只定位C已注册原callback，所有物理提交在C内做；此方法不接受W callback。
    # 函数用途: 请求一次原C短提交并保留它的原责任关联号。
    def apply(self, operation, *, timeout=5):
        if type(operation) is not str or not operation:
            raise WorkerCommitRejected('original_native_operation_required')
        return self._request({'op': 'apply', 'operation': operation}, 'applied', timeout)

    # LLM: 固定wire只引用原request/attempt；call_origin仅本次词法调用内运输拒绝归属，不入wire/账、不授执行或结算权。
    # 函数用途: 请求原C物化owner，不接受路径、owner声明或W回调。
    def materialize_owner(self, request_id, execution_attempt_id, *, timeout=5, call_origin=None):
        try:
            return self._request({'op': 'materialize', 'request_id': request_id,
                                  'execution_attempt_id': execution_attempt_id}, 'applied', timeout)
        except NativeCommitNotStarted as error:
            if (type(error) is NativeCommitNotStarted and error._origin is error
                    and error._creator == os.getpid() and error._client is self
                    and error.seq == self._seq and call_origin is not None):
                error._materialize_origin = (call_origin, request_id, execution_attempt_id)
            raise

    # LLM: 未知finish保留原seq；再次调用只等原结果，不重发可能已成功的物理结清操作。
    # 函数用途: 精确收尾原handle，失败不假定释放，迟到ACK可继续消费。
    def finish(self, handle, *, timeout=5):
        self._require_original()
        with self._lock:
            if type(handle) is not int:
                raise WorkerCommitRejected('original_commit_handle_required')
            if handle in self._completed_finishes:
                return self._completed_finishes.pop(handle)
            if handle not in self._pending | self._uncertain:
                raise WorkerCommitRejected('original_commit_handle_required')
            if handle in self._finish_seq:
                return self._await_reply(self._finish_seq[handle], timeout)
            return self._request({'op': 'finish', 'handle': handle}, 'finished', timeout)

    # LLM: 每次只send一次；在send前保留精确seq/op/handle，失败不重放且不以未知结果授权W。
    # 函数用途: 发送原请求并等待其结果，历史合法ACK分别归到原调用。
    def _request(self, request, expected, timeout):
        self._require_original()
        with self._lock:
            if self._closed:
                raise WorkerCommitRejected('native_client_closed')
            self._seq += 1
            seq = self._seq
            handle = request.get('handle', seq)
            self._inflight[seq] = (expected, handle, False)
            if expected == 'finished':
                self._finish_seq[handle] = seq
            else:
                self._uncertain.add(handle)
            KernelMessageSender.send(self._sources[1], json.dumps({**request, 'seq': seq},
                                     separators=(',', ':')).encode(), timeout=timeout)
            return self._await_reply(seq, timeout)

    # LLM: 每个接收独立核原父及精确seq；strict NACK仅移除该begun/applied的phantom，不碰其它责任。乱序拒绝归原seq，当前等待共原预算，FD/finish/未知仍硬拒。
    # 函数用途: 消费无FD ACK或明确pre-pin拒绝，不自动重试或把回执当业务结算。
    def _await_reply(self, seq, timeout):
        deadline = time.monotonic() + timeout
        while True:
            if seq in self._rejections:
                raise self._rejections.pop(seq)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('native_reply_timeout')
            raw, peer_pid = receive_control_packet(self._sources[0], timeout=remaining)
            self._require_original()
            if type(peer_pid) is not int or peer_pid != self._parent[0]:
                raise WorkerCommitRejected('original_coordinator_message_required')
            value = _decode_reply(raw)
            reply_seq = value.get('seq') if type(value) is dict else None
            if type(reply_seq) is not int or reply_seq not in self._inflight:
                raise WorkerCommitRejected('native_reply_unconfirmed')
            expected, handle, released = self._inflight[reply_seq]
            if type(value) is dict and value.get('op') == 'rejected':
                if (set(value) != {'op', 'seq', 'error_code'} or value['error_code'] != 'WORKER_COMMIT_REJECTED'
                        or expected not in ('begun', 'applied') or handle != reply_seq or released
                        or handle in self._pending or handle in self._finish_seq):
                    raise WorkerCommitRejected('native_reply_unconfirmed')
                error = NativeCommitNotStarted(self, reply_seq, proof=_REJECTED_REPLY_PROOF)
                del self._inflight[reply_seq]
                self._uncertain.discard(handle)
                if reply_seq == seq:
                    raise error
                self._rejections[reply_seq] = error
                continue
            result = _reply(raw, expected, reply_seq, handle)
            del self._inflight[reply_seq]
            if expected == 'finished':
                self._pending.discard(handle)
                self._uncertain.discard(handle)
                self._finish_seq.pop(handle, None)
                # 其它RPC先收到的原finished仍可由该handle取回一次；不重新发送或复活责任。
                if reply_seq != seq:
                    self._completed_finishes[handle] = result
                # 若传输乱序，之后迟到的登记ACK也不能重新复活已明确结清的责任。
                previous = self._inflight.get(handle)
                if previous is not None and previous[0] != 'finished':
                    self._inflight[handle] = (*previous[:2], True)
            elif not released:
                self._uncertain.discard(handle)
                self._pending.add(handle)
            if reply_seq == seq:
                return result

    # LLM: 未知也不能先关闭自己的接收点；这里只关闭本客户端资源，不替C抹pin或释放quota。
    # 函数用途: 原对象无未结关联时停止传输。
    def close(self):
        self._require_original()
        with self._lock:
            if self._pending or self._uncertain or self._finish_seq:
                raise WorkerCommitRejected('original_native_responsibility_pending')
            if not self._closed:
                self._sources[0].close()
                self._sources[1].close()
                self._closed = True


# LLM: 唯一键与UTF-8检查在任何状态更新前；解码本身不证明原sender或已签收。
# 函数用途: 解析原ACK完整JSON，拒绝坏字节与重复字段。
def _decode_reply(raw):
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise WorkerCommitRejected('native_reply_invalid') from error


# LLM: ACK字段只作结果关联，严格类型/唯一键/seq/handle核验，不解释成权限。
# 函数用途: 拒绝普通JSON或缓存旧ACK伪装这次完成。
def _reply(raw, expected, seq, handle):
    value = _decode_reply(raw)
    fields = {'op', 'seq', 'handle'} | ({'result'} if expected == 'applied' else set())
    if (type(value) is not dict or set(value) != fields or value['op'] != expected
            or type(value['seq']) is not int or value['seq'] != seq
            or type(value['handle']) is not int or value['handle'] != handle):
        raise WorkerCommitRejected('native_reply_unconfirmed')
    return value
