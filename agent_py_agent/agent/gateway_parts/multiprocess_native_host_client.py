# LLM: Closed native host client binds one original request; every process operation returns to the original C-held child.
# 模块用途: Preserve host-only background_command SDK restrictions, no-reap observation and strict v2 cleanup receipts.
import json
import os

from .multiprocess_native_commit_client import _decode_reply
from .multiprocess_native_control_receive import receive_control_packet
from .multiprocess_native_executor_client import (
    NativeExecutorClient,
    NativeExecutorProcess,
    _remaining,
)
from .multiprocess_worker_commit import WorkerCommitRejected


# LLM: Independent host transport has original-process/copy/fork guards and immutable frozen binding; only command starts.
# 类用途: Closed host client, unable to rebind another request or delegate another host.
class NativeHostClient(NativeExecutorClient):
    # LLM: Locators from init are not permission; parent and reply sender remain real kernel observations.
    # 函数用途: Create host-owned endpoints and freeze original command facade binding.
    def __init__(self,sender,receiver,row):
        super().__init__(sender,receiver,os.getppid(),request_id=row['request_id'],
                         execution_attempt_id=row['execution_attempt_id'],tool_call_id=row['tool_call_id'])
        self._frozen_binding = self._binding

    # LLM: Transport binding must stay equal to original init; no field mutation is a new host authority.
    # 函数用途: Preserve base creator/endpoint/parent guard and reject post-init binding drift.
    def _require_original(self):
        super()._require_original()
        if self._binding != self._frozen_binding:
            raise WorkerCommitRejected('original_native_host_binding_required')

    # LLM: Extra completion nonce is accepted only from the original real C packet with the exact sequence; base replies stay strict.
    # 函数用途: 核准准备完成回执，保留同包sender与出生核验，不宽化其它回复字段。
    def _receive(self, seq, expected, deadline, handle=None):
        if expected not in {'host_done', 'host_done_confirmed'}:
            return super()._receive(seq, expected, deadline, handle)
        self._require_original()
        raw, peer = receive_control_packet(self._receiver, timeout=_remaining(deadline))
        self._require_original()
        if type(peer) is not int or peer != self._parent[0]:
            raise WorkerCommitRejected('original_coordinator_message_required')
        value = _decode_reply(raw)
        if (type(value) is not dict or set(value) != {'op', 'seq', 'nonce'}
                or value.get('op') != expected or type(value.get('seq')) is not int or value['seq'] != seq
                or type(value.get('nonce')) is not str or len(value['nonce']) != 32
                or any(char not in '0123456789abcdef' for char in value['nonce'])):
            raise WorkerCommitRejected('native_reply_unconfirmed')
        return value

    # LLM: Serialize both strict replies, then report consumption through the original endpoint; terminal receipt has no RPC reply.
    #   The return reports only local receipt transmission. C completion is proved exclusively when original C consumes it.
    # 函数用途: 核准两个真实C回执后回报末回执已读，发送成功不冒充C已结清。
    def complete_host(self):
        self._require_original()
        with self._lock:
            self._require_original()
            if self._unknown or self._pending_handles:
                raise WorkerCommitRejected('original_native_host_responsibility_pending')
            prepared = self._rpc({'op': 'host_done'}, 'host_done')
            confirmed = self._rpc({'op': 'host_done_confirm', 'done_seq': prepared['seq'],
                                   'nonce': prepared['nonce']}, 'host_done_confirmed')
            self._seq += 1
            receipt = {'op': 'host_done_received', 'seq': self._seq,
                       'confirmed_seq': confirmed['seq'], 'nonce': confirmed['nonce']}
            self._unknown[self._seq] = receipt
            try:
                self._require_original()
                self._sender.send(json.dumps(receipt, separators=(',', ':')).encode(), timeout=5)
            except BaseException:
                self._broken = True
                raise
            self._unknown.pop(self._seq)
            return {'op': 'host_completion_reported', 'seq': self._seq}

    # LLM: 与原NativeExecutorClient保持显式三字段签名，host仍禁止公开重绑定；本方法不委托父类授信，联查host policy反例。
    # 函数用途: 即使三个字段描述合法同分区请求，也拒绝host重新选择请求、attempt或工具归属。
    def bind_request(self, request_id, execution_attempt_id, tool_call_id):
        self._require_original()
        raise WorkerCommitRejected('original_native_host_binding_required')

    # LLM: A facade/private entry also cannot bypass the closed host kind/delegate/binding gate.
    # 函数用途: Allow only background_command, with original binding, before IO or RPC.
    def _launch(self,kind,argv,options,binding):
        self._require_original()
        if kind != 'background_command' or options.delegate is not None or binding != self._frozen_binding:
            raise WorkerCommitRejected('original_native_host_binding_required')
        return super()._launch(kind,argv,options,binding)

    # LLM: Host owns no waitpid; peek must preserve C child identity until actual Store read and cleanup snapshot.
    # 函数用途: Query real C no-reap facts or send original-instance signal.
    def _process(self, token, signum=None, *, timeout=5, reap=False):
        frame = {'op':'executor_reap' if reap else ('executor_peek' if signum is None else 'executor_signal'),
                 'process_token':token}
        if signum is not None:
            frame['signal'] = signum
        reply = self._rpc(frame,'process',timeout=timeout)['result']
        if (type(reply) is not dict or set(reply) != {'returncode'}
                or reply['returncode'] is not None and type(reply['returncode']) is not int):
            self._broken = True
            raise WorkerCommitRejected('native_reply_unconfirmed')
        return reply['returncode']

    # LLM: Factory is local closed host behavior, never a wire module/class selector.
    # 函数用途: Return proxy with explicit C reap after host identity observation.
    def _make_process(self, token, pid, args, io):
        return NativeHostProcess(self,token,pid,args,io)


# LLM: Host no-reap poll preserves command kernel identity until Store/cleanup have observed birth.
# 类用途: Native command proxy with explicit C-owned wait/reap semantics.
class NativeHostProcess(NativeExecutorProcess):
    # LLM: Birth derives from C's unreaped actual child/P1, never an informational PID or generic ps success.
    # 函数用途: Read the exact C-associated Store birth even when the command exits before host observation.
    def capture_birth(self):
        self._require_original()
        result = self._client._rpc({'op':'executor_identity_v2','process_token':self._token},'process')['result']
        if type(result) is not dict or set(result) != {'version','birth'} or type(result['version']) is not int or result['version'] != 2 or type(result['birth']) is not str or not result['birth']:
            raise WorkerCommitRejected('native_reply_unconfirmed')
        return result['birth']

    # LLM: Original C owns waitpid and kernel cleanup; receipt is known-instance coverage, not arbitrary complete-tree proof.
    # 函数用途: Consume a strict native-v2 cleanup receipt for the saved command and refuse malformed/unknown fields.
    def terminate_tree(self):
        from ..tooling.process_registry import ProcessTerminationReceipt
        self._require_original()
        result = self._client._rpc({'op':'executor_tree_stop_v2','process_token':self._token},'process')['result']
        fields = {'method','confirmed','return_code','observed_processes','unresolved_pids'}
        if type(result) is not dict or set(result) != {'version','receipt'} or type(result['version']) is not int or result['version'] != 2:
            raise WorkerCommitRejected('native_reply_unconfirmed')
        receipt = result['receipt']
        if (type(receipt) is not dict or set(receipt) != fields or type(receipt['method']) is not str
                or type(receipt['confirmed']) is not bool or type(receipt['observed_processes']) is not int
                or receipt['observed_processes'] < 0 or type(receipt['unresolved_pids']) is not list
                or any(type(p) is not int or p <= 0 for p in receipt['unresolved_pids'])
                or receipt['return_code'] is not None and type(receipt['return_code']) is not int
                or receipt['confirmed'] and (receipt['unresolved_pids'] or receipt['return_code'] is None)):
            raise WorkerCommitRejected('native_reply_unconfirmed')
        self.returncode = receipt['return_code']
        return ProcessTerminationReceipt(**{**receipt,'unresolved_pids':tuple(receipt['unresolved_pids'])})

    # LLM: Wait return is direct child only; it never proves command tree stopped or releases P1.
    # 函数用途: Wait via no-reap observation then ask original C to reap this exact command.
    def wait(self, timeout=None):
        result = super().wait(timeout)
        reaped = self._client._process(self._token,reap=True)
        if reaped != result:
            raise WorkerCommitRejected('original_executor_reap_unconfirmed')
        return result
