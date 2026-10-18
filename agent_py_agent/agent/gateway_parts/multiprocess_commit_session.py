# LLM: 仅包装原PeerCommitChannel传输；线handle仅引用原pin，不构成许可或第二耐久账。
# 模块用途: 在C内把真实W连接的begin/finish转到原准入与精确责任门。
import json
import os
import struct
import threading
from dataclasses import asdict

from ..user_space.owner_resolver import OwnerIdentity
from .multiprocess_commit_channel import PeerCommitChannel
from .multiprocess_worker_commit import WorkerCommitRejected

# 只限制控制帧资源，不截断、不筛业务记录；正常owner标识帧远小于此值。
_COMMIT_FRAME_MAX_BYTES = 4096


# LLM: 同一连接单读取者；关联表仅握住原pin对象，断线/超时绝不清理或恢复许可。
# 类用途: 沿原C连接处理一次性提交协议。
class CommitServerSession:
    # LLM: 只接受真实原channel，不从JSON重建lease/issuer；构造不准入owner。
    # 函数用途: 固定原连接、创建进程与串行读取门。
    def __init__(self, channel):
        if type(channel) is not PeerCommitChannel:
            raise WorkerCommitRejected('original_peer_channel_required')
        self.channel = channel
        self._creator_pid = os.getpid()
        self._lock = threading.RLock()
        self._last_seq = 0
        self._handles = {}

    # LLM: 完整帧后才进原门，序号只防连接重放；send失败保留已登记原pin，不自动重试。
    # 函数用途: 接收并执行一次begin/finish，向实际W回发结果。
    def serve_once(self):
        if os.getpid() != self._creator_pid:
            raise WorkerCommitRejected('original_coordinator_process_required')
        with self._lock:
            request = read_commit_frame(self.channel._connection)
            seq, op = _request_identity(request, self._last_seq)
            self._last_seq = seq
            if op == 'begin':
                owner = _wire_owner(request['owner'])
                pin = self.channel.begin(owner)
                self._handles[seq] = pin
                result = {'op': 'begun', 'seq': seq, 'handle': seq}
            else:
                handle = request['handle']
                if type(handle) is not int or handle not in self._handles:
                    raise WorkerCommitRejected('original_commit_handle_required')
                self.channel.finish(self._handles[handle])
                del self._handles[handle]
                result = {'op': 'finished', 'seq': seq, 'handle': handle}
            write_commit_frame(self.channel._connection, result)
            return result


# LLM: 只解析有限传输数据，不采用peer_pid或正文授权；重复/坏序号在原准入前拒绝。
# 函数用途: 校验严格请求字段与同连接单调关联号。
def _request_identity(request, previous):
    op, seq = request.get('op'), request.get('seq')
    if type(op) is not str or op not in ('begin', 'finish'):
        raise WorkerCommitRejected('commit_request_invalid')
    fields = {'op', 'seq', 'owner' if op == 'begin' else 'handle'}
    if set(request) != fields or type(seq) is not int or seq <= previous:
        raise WorkerCommitRejected('commit_request_invalid')
    return seq, op


# LLM: 原OwnerIdentity纯构造且逐字段回比，净化后不同就拒绝；不物化owner、不默认本机。
# 函数用途: 从严格三字段还原规范owner供原issuer核桶。
def _wire_owner(raw):
    fields = {'provider', 'owner_kind', 'owner_id'}
    if type(raw) is not dict or set(raw) != fields or not all(type(value) is str and value for value in raw.values()):
        raise WorkerCommitRejected('commit_owner_invalid')
    kind = raw['owner_kind']
    if kind == 'main':
        owner = OwnerIdentity.local_main()
    elif kind in ('user', 'group'):
        factory = OwnerIdentity.provider_user if kind == 'user' else OwnerIdentity.provider_group
        owner = factory(raw['provider'], raw['owner_id'])
    else:
        raise WorkerCommitRejected('commit_owner_invalid')
    if asdict(owner) != raw:
        raise WorkerCommitRejected('commit_owner_invalid')
    return owner


# LLM: 部分读取必须补齐；EOF/timeout只表示未知，调用方不据此结清任何原责任。
# 函数用途: 从实际连接取得指定数量字节。
def _read_exact(connection, count):
    data = bytearray()
    try:
        while len(data) < count:
            part = connection.recv(count - len(data))
            if not part:
                raise WorkerCommitRejected('commit_connection_eof')
            data.extend(part)
    except OSError as error:
        raise WorkerCommitRejected('commit_connection_unconfirmed') from error
    return bytes(data)


# LLM: 长度先拒绝再分配，JSON坏结构仅是传输拒绝；不允许标量或任意第二权限对象。
# 函数用途: 读取有界大端长度JSON帧。
def read_commit_frame(connection):
    size = struct.unpack('!I', _read_exact(connection, 4))[0]
    if not 0 < size <= _COMMIT_FRAME_MAX_BYTES:
        raise WorkerCommitRejected('commit_frame_size_invalid')
    try:
        value = json.loads(_read_exact(connection, size).decode('utf-8'), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise WorkerCommitRejected('commit_frame_invalid') from error
    if type(value) is not dict:
        raise WorkerCommitRejected('commit_frame_invalid')
    return value


# LLM: 发送不重试；失败保留原pin引用，不能把本地写入或timeout称W收到许可。
# 函数用途: 将完整协议结果写到实际连接。
def write_commit_frame(connection, value):
    raw = json.dumps(value, separators=(',', ':')).encode('utf-8')
    if not 0 < len(raw) <= _COMMIT_FRAME_MAX_BYTES:
        raise WorkerCommitRejected('commit_frame_size_invalid')
    try:
        connection.sendall(struct.pack('!I', len(raw)) + raw)
    except OSError as error:
        raise WorkerCommitRejected('commit_connection_unconfirmed') from error


# LLM: 每一层JSON对象都检查重复键；拒绝歧义而非默认采用最后值，坏传输不创造任何许可。
# 函数用途: 防止请求字段或owner字段的重复键隐藏冲突值。
def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WorkerCommitRejected('commit_frame_duplicate_key')
        result[key] = value
    return result
