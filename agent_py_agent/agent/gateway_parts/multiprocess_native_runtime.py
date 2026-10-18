# LLM: 正count组合根独占原endpoint/issuer/session；定位名不授信，bootstrap逐消息核原P0/P4，不写第二账本。
# 模块用途: 连接生产C与W的两个独立原生责任/资源桥，closing时保持接收直到原责任逐项结清。
import hashlib
import os
import sys
import threading
from contextlib import ExitStack

from .multiprocess_kernel_message import (
    KernelMessageError,
    KernelMessageReceiver,
    KernelMessageSender,
)
from .multiprocess_mode import _valid_launch
from .multiprocess_native_commit_client import NativeCommitClient
from .multiprocess_native_commit_session import NativeCommitSession
from .multiprocess_native_control_receive import receive_control_packet
from .multiprocess_native_executor_client import NativeExecutorClient
from .multiprocess_native_executor_lane import (
    NativeExecutorLane,
    executor_addresses,
    require_lane_issuer,
)
from .multiprocess_worker_commit import WorkerCommitIssuer, WorkerCommitRejected

_READY = b"worker-native-ready.v1"
_CONNECTED = b"coordinator-native-ready.v1"


# LLM: 名称纯定位且有原instance唯一性，不接受环境覆盖名称或据hash判断权限；无文件I/O。
# 函数用途: 从同一冻结launch生成双方可找到的有限原生地址，保留旧无FD控制地址格式。
def native_addresses(launch):
    if not _valid_launch(launch) or not launch.instance_id:
        raise WorkerCommitRejected("original_worker_launch_required")
    key = hashlib.sha256(
        f"{launch.gateway_root}\0{launch.partition.c_run_id}\0{launch.partition.index}\0{launch.instance_id}".encode()
    ).hexdigest()
    prefix = "com.myagent.native." if sys.platform == "darwin" else "@myagent.native."
    return prefix + key + ".c", prefix + key + ".w"


# LLM: 原C在spawn前创建两个独立receiver；spawn原门外握手，两个消费者只共用一个原issuer。
# 类用途: 组合同一真实原W的控制及执行资源通道，保留所有未结关联。
class NativeWorkerLane:
    # LLM: 原C仅建endpoint和捕获的本地生命周期门；不创issuer/owner，构造失败只清自有资源。
    # 函数用途: 在原W spawn前建两路接收点及单次启动/不可逆退役状态。
    def __init__(self, launch):
        self.launch = launch
        self._origin, self._creator = self, os.getpid()
        addresses = native_addresses(launch)
        self.receiver = KernelMessageReceiver(addresses[0])
        try:
            self.executor_lane = NativeExecutorLane(launch, addresses)
        except BaseException:
            self.receiver.close()
            raise
        self._lifecycle = threading.RLock()
        self._sources = (launch, self.receiver, self.executor_lane, self._lifecycle)
        self.issuer = self.session = self.thread = None
        self._bound = None
        self._stop = threading.Event()
        self._retired = False
        self.last_error = None

    # LLM: fork/copy/来源或本地门替换均在继承锁前拒；只核原身份，不撤掉closing精确finish接收权。
    # 函数用途: 核本原组合根及捕获引用，无current授权或W存活/EOF排空推断。
    def _require_original(self):
        if self._origin is not self or self._creator != os.getpid():
            raise WorkerCommitRejected("original_native_lane_required")
        launch, receiver, resource, lifecycle = self._sources
        if (
            self.launch is not launch
            or self.receiver is not receiver
            or self.executor_lane is not resource
            or self._lifecycle is not lifecycle
        ):
            raise WorkerCommitRejected("original_native_lane_source_required")
        receiver._check_creator()
        resource._require_original()
        if self._bound is not None:
            issuer, session, sender = self._bound
            if (
                self.issuer is not issuer
                or self.session is not session
                or session._sources != (issuer, receiver, sender)
            ):
                raise WorkerCommitRejected("original_native_lane_source_required")
            if (
                issuer._source_origin[0] is not launch
                or issuer.launch is not launch
                or issuer.process is not issuer._source_origin[1]
            ):
                raise WorkerCommitRejected("original_worker_source_required")
            require_lane_issuer(issuer, self._issuer_sources)
            session._require_original()

    # LLM: 单次绑定与退役共本地门；READY/CONNECTED传输在本地及T/C门外，最终控制启动另核原C/P4。
    # 函数用途: 固定唯一原issuer并启动两路消费者，旧握手不改变语义或授予业务许可。
    def attach(self, control, process):
        self._require_original()
        with self._lifecycle:
            self._require_original()
            if self._retired:
                raise WorkerCommitRejected("original_native_lane_retired")
            if self.issuer is not None:
                raise WorkerCommitRejected("original_native_lane_already_bound")
            self.issuer = WorkerCommitIssuer(control, self.launch, process)
            self._issuer_sources = (
                self.issuer.control,
                self.issuer._lease,
                self.issuer._controller,
                self.issuer._source_origin,
            )
        payload, peer = receive_control_packet(self.receiver, timeout=15)
        self.issuer._require_worker(peer)
        if payload != _READY:
            raise WorkerCommitRejected("native_bootstrap_invalid")
        sender = KernelMessageSender(native_addresses(self.launch)[1])
        try:
            with self._lifecycle:
                self._require_original()
                if self._retired:
                    raise WorkerCommitRejected("original_native_lane_retired")
                self.session = NativeCommitSession(self.issuer, self.receiver, sender)
                self._bound = (self.issuer, self.session, sender)
                self.executor_lane.attach(self.issuer)
                self.executor_lane.start()
            self._start_control_consumer()
            sender.send(_CONNECTED, timeout=5)
        except BaseException:
            if self.session is None:
                sender.close()
            # Original launcher calls close; don't close endpoints under a live consumer here.
            raise
        return self

    # LLM: 原C门同段核current/P4和实际Thread构造/start；先发布引用，异常不可重试，receive/join不占门。
    # 函数用途: 与本地退役及原C撤权排序地启动唯一控制消费者，保留已启动线程退出确认责任。
    def _start_control_consumer(self):
        self._require_original()
        with self._lifecycle:
            self._require_original()
            if self._retired:
                raise WorkerCommitRejected("original_native_lane_retired")
            if self._bound is None:
                raise WorkerCommitRejected("original_native_lane_unbound")
            if self.thread is not None or self._stop.is_set():
                raise WorkerCommitRejected("original_native_consumer_already_started")
            with self._issuer_sources[1]._lifecycle_gate:
                self._require_original()
                try:
                    self.issuer._require_current()
                    self.issuer._require_worker(self.issuer.process.pid)
                    thread = threading.Thread(
                        target=self._consume,
                        name=f"native-w{self.launch.partition.index}",
                        daemon=True,
                    )
                    self.thread = thread
                    thread.start()
                except BaseException:
                    self._retired = True
                    self._stop.set()
                    raise

    # LLM: 原session逐消息自行receive，超时不清pin；拒绝/副作用失败仍留责任并允许原finish。
    # 函数用途: 消费旧严格无FD责任通道，保留准确错误且不自动重放请求。
    def _consume(self):
        while not self._stop.is_set():
            try:
                self._require_original()
                with self.session._lock:
                    if self._stop.is_set():
                        return
                    self.session.serve_once(timeout=0.1)
            except KernelMessageError as error:
                if error.error_code != "KERNEL_MESSAGE_TIMEOUT":
                    self.last_error = error
                    self._stop.wait(0.05)
            except Exception as error:
                self.last_error = error
                self._stop.wait(0.05)
            self._stop.wait(0.001)

    # LLM: 本地门排序启动/退役；任一路有责任仍保持接收。已发布Thread必走实际join；启动确认未知也不关端点。
    # 函数用途: 无原责任时不可逆退役，门外确认真实消费者退出；未确认的原生启动保留原引用和端点。
    def close(self):
        self._require_original()
        resource = self.executor_lane
        with self._lifecycle, ExitStack() as stack:
            self._require_original()
            if self.session is not None:
                stack.enter_context(self.session._lock)
            if resource.session is not None:
                stack.enter_context(resource.session._lock)
                for host in resource.session._hosts.values():
                    if host.session is not None:
                        stack.enter_context(host.session._lock)
                    host.require_drained()
            if (
                (self.session is not None and self.session._handles)
                or (resource.session is not None and resource.session._handles)
                or (self.issuer is not None and self.issuer._pins)
            ):
                raise WorkerCommitRejected("original_native_responsibility_pending")
            self._retired = True
            self._stop.set()
            resource._stop.set()
            thread = self.thread
        if thread is not None:
            try:
                thread.join(timeout=2)
            except RuntimeError as error:
                # Native creation may precede Python's start ack; absence of ident is not an exit proof.
                raise WorkerCommitRejected("native_consumer_stop_unconfirmed") from error
            if thread.is_alive():
                raise WorkerCommitRejected("native_consumer_stop_unconfirmed")
        resource.close_stopped()
        if resource.session is not None:
            for host in resource.session._hosts.values():
                host.close()
        if self.session is None:
            self.receiver.close()
        else:
            self.session.close()


# LLM: Combined W close must retain unknown resource delivery and legacy control semantics; no new business authority.
# 类用途: Extend the returned legacy client with one persistent executor client and composition-owned resource cleanup.
class _WorkerNativeClient(NativeCommitClient):
    # LLM: Both endpoints are W-created before READY; executor coordinator PID is actual getppid, not launch metadata.
    # 函数用途: Construct exactly one resource transport alongside the original control client.
    def __init__(self, receiver, sender, executor_receiver, executor_sender):
        super().__init__(receiver, sender)
        self._control_sources = (receiver, sender)
        executor = NativeExecutorClient(executor_sender, executor_receiver, os.getppid())
        self.executor_client = executor
        self._executor_sources = (executor, executor_receiver, executor_sender)
        self._executor_closed = False

    # LLM: Self/creator checks precede inherited locks; captured client and endpoints reject public-source drift.
    # 函数用途: Preserve original legacy guard and require the original persistent resource transport.
    def _require_original(self):
        if (
            self._creator_pid != os.getpid()
            or self._origin is not self
            or self._sources != self._control_sources
        ):
            raise WorkerCommitRejected("original_native_client_required")
        super()._require_original()
        executor, receiver, sender = self._executor_sources
        if (
            self.executor_client is not executor
            or executor._receiver is not receiver
            or executor._sender is not sender
        ):
            raise WorkerCommitRejected("original_native_executor_client_required")
        executor._require_original()

    # LLM: No close exists on resource client; composition root checks its real unknown/pending under original lock.
    # 函数用途: Refuse unresolved resources, invoke legacy control close, then close own resource receiver/sender.
    def close(self):
        self._require_original()
        executor, receiver, sender = self._executor_sources
        with executor._lock:
            if executor._unknown or executor._pending_handles:
                raise WorkerCommitRejected("original_native_executor_responsibility_pending")
            # The legacy close remains responsible for its own pending/uncertain/finish and endpoints.
            super().close()
            if not self._executor_closed:
                receiver.close()
                sender.close()
                self._executor_closed = True


# LLM: W仅在正式mode/冻结launch中调用；原父身份由client内核观察，body和定位名不能授信。
# 函数用途: 旧READY之前创建两路W接收点，返回附单一executor_client的旧控制客户端。
def connect_worker_native(launch):
    from .multiprocess_bootstrap import freeze_worker_bootstrap

    freeze_worker_bootstrap(launch)
    addresses = native_addresses(launch)
    resource_addresses = executor_addresses(addresses)
    endpoints = []
    try:
        receiver = KernelMessageReceiver(addresses[1])
        endpoints.append(receiver)
        executor_receiver = KernelMessageReceiver(resource_addresses[1])
        endpoints.append(executor_receiver)
        sender = KernelMessageSender(addresses[0])
        endpoints.append(sender)
        executor_sender = KernelMessageSender(resource_addresses[0])
        endpoints.append(executor_sender)
        client = _WorkerNativeClient(receiver, sender, executor_receiver, executor_sender)
        sender.send(_READY, timeout=5)
        payload, peer = receive_control_packet(receiver, timeout=15)
        client._require_original()
        if type(peer) is not int or peer != os.getppid() or payload != _CONNECTED:
            raise WorkerCommitRejected("original_coordinator_message_required")
        return client
    except BaseException:
        # No business request was sent; constructor/bootstrap owns only these endpoints.
        for endpoint in reversed(endpoints):
            endpoint.close()
        raise
