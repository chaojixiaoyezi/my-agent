# LLM: 在C内将实际Unix连接绑定原issuer；不是完成的native协议、耐久租约或W业务许可。
# 模块用途: 只让同一原连接登记与结清它的精确原提交责任。
import os
import select
import socket
import threading

from .multiprocess_kernel_peer import PeerCredentialsUnavailable, kernel_peer_pid
from .multiprocess_worker_commit import WorkerCommitIssuer, WorkerCommitRejected


# LLM: 原issuer负责Popen/出生/P4/P2/lease；本对象不由JSON构建，不以断线或超时清pin。
# 类用途: 限制一条真实worker连接自己的在途责任。
class PeerCommitChannel:
    # LLM: 仅可信C组合根交原issuer和实际连接，正文不参与peer核验；必须原C原worker来源。
    # 函数用途: 固定本连接、原issuer及创建进程，拒绝创建者socketpair冒充worker。
    def __init__(self, issuer, connection):
        if type(issuer) is not WorkerCommitIssuer:
            raise WorkerCommitRejected('original_commit_issuer_required')
        self._issuer, self._connection = issuer, connection
        self._creator_pid = os.getpid()
        self._pins = set()
        self._lock = threading.RLock()
        issuer._require_current()
        self._require_peer()

    # LLM: 内核peer核验与原issuer完整P2/lease准入后才返回真实pin；线程责任表不产生新权威。
    # 函数用途: 登记这条连接发起的精确原责任。
    def begin(self, owner):
        # 先拒fork副本，不能等争到可能由已消失线程持有的继承锁。
        if self._creator_pid != os.getpid():
            raise WorkerCommitRejected('original_channel_process_required')
        with self._lock:
            pid = self._require_peer()
            pin = self._issuer.begin(owner, peer_pid=pid)
            self._pins.add(pin)
            return pin

    # LLM: 只结束本连接自己的同一pin；closing只走原retire，不以EOF/字段等值推测结清。
    # 函数用途: 原C关闭中也可结清已登记责任，断线或来源失效则保留pin。
    def finish(self, pin):
        # 撤权结清同样不能先争继承锁；锁内peer检查再次核创建进程。
        if self._creator_pid != os.getpid():
            raise WorkerCommitRejected('original_channel_process_required')
        with self._lock:
            self._require_peer()
            if not any(held is pin for held in self._pins):
                raise WorkerCommitRejected('original_channel_pin_required')
            self._issuer.finish(pin)
            self._pins.remove(pin)

    # LLM: 每次操作核本C进程和真实内核peer，原issuer再核Popen/出生/P4；不调用新owner许可。
    # 函数用途: 拒绝fork副本、已关闭连接及不属于原worker的连接。
    def _require_peer(self):
        if self._creator_pid != os.getpid():
            raise WorkerCommitRejected('original_channel_process_required')
        try:
            pid = kernel_peer_pid(self._connection)
        except PeerCredentialsUnavailable as error:
            raise WorkerCommitRejected('original_channel_peer_required') from error
        self._require_stream()
        self._issuer._require_worker(pid)
        return pid

    # LLM: EOF必须独立于积压数据核验；Darwin kqueue/Linux poll读取内核挂断事实，不消费协议或清pin。
    # 函数用途: 即使peer凭据保留历史PID或缓冲非空也拒绝断线。
    def _require_stream(self):
        try:
            if _kernel_stream_hangup(self._connection):
                raise WorkerCommitRejected('original_channel_eof')
            readable, _, _ = select.select([self._connection], [], [], 0)
            if readable and self._connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b'':
                raise WorkerCommitRejected('original_channel_eof')
        except BlockingIOError:
            return
        except (OSError, ValueError) as error:
            raise WorkerCommitRejected('original_channel_stream_required') from error


# LLM: 目标平台内核挂断事件独立于可读字节数；缺能力失败关闭，内核事件仍非业务结清证明。
# 函数用途: 不耗掉缓冲数据也能发现对端已关闭。
def _kernel_stream_hangup(connection):
    if hasattr(select, 'kqueue'):
        event = select.kevent(connection.fileno(), filter=select.KQ_FILTER_READ,
                              flags=select.KQ_EV_ADD | select.KQ_EV_ENABLE)
        queue = select.kqueue()
        try:
            observed = queue.control([event], 1, 0)
        finally:
            queue.close()
        return any(row.flags & (select.KQ_EV_EOF | select.KQ_EV_ERROR) for row in observed)
    if hasattr(select, 'poll') and hasattr(select, 'POLLRDHUP'):
        mask = select.POLLRDHUP | select.POLLHUP | select.POLLERR | select.POLLNVAL
        poller = select.poll()
        poller.register(connection, mask)
        return any(flags & mask for _descriptor, flags in poller.poll(0))
    raise WorkerCommitRejected('kernel_stream_state_unavailable')
