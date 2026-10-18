# LLM: Separate resource endpoint, same original issuer; names only locate, every packet keeps its kernel sender.
# 模块用途: Own the C resource receiver and independent executor consumer without creating a second responsibility system.
import os
import threading

from .multiprocess_kernel_message import (
    KernelMessageError,
    KernelMessageReceiver,
    KernelMessageSender,
)
from .multiprocess_native_executor_session import NativeExecutorSession
from .multiprocess_worker_commit import WorkerCommitRejected


# LLM: Closing removes NEW-write authority but retains registered original lease for exact finishes; never call current authorization here.
# 函数用途: Check captured issuer/controller/lease/source identity without rejecting existing responsibility during C closing.
def require_lane_issuer(issuer, sources):
    from .multiprocess_coordinator import _is_registered_coordinator_lease

    control, lease, controller, worker = sources
    if (
        issuer._issuer_origin is not issuer
        or issuer._creator_pid != os.getpid()
        or issuer.control is not control
        or issuer._lease is not lease
        or issuer._controller is not controller
        or issuer._source_origin is not worker
        or issuer.launch is not worker[0]
        or issuer.process is not worker[1]
        or getattr(control, "_coordinator_role_lease", None) is not lease
        or lease.controller is not controller
        or not _is_registered_coordinator_lease(lease)
    ):
        raise WorkerCommitRejected("original_worker_source_required")


# LLM: Suffixes distinguish transport resources only; they never grant identity, ownership or admission.
# 函数用途: Derive independent resource locators from the unchanged legacy native addresses.
def executor_addresses(addresses):
    return tuple(address + ".exec" for address in addresses)


# LLM: Original C creates this before spawning original W, then supplies its sole original issuer after P4.
# 类用途: Manage one independent executor receiver/session/consumer sharing the legacy lane's issuer.
class NativeExecutorLane:
    # LLM: No issuer/owner/P1/P3 allocation; local lifecycle mutex is captured with the original receiver, not an authority source.
    # 函数用途: 创建原C资源端点，并初始化单次启动/退役的本地同步状态。
    def __init__(self, launch, addresses):
        self._origin, self._creator = self, os.getpid()
        self.launch = launch
        self.receiver = KernelMessageReceiver(executor_addresses(addresses)[0])
        self._lifecycle = threading.RLock()
        self._sources = (launch, self.receiver, self._lifecycle)
        self._addresses = executor_addresses(addresses)
        self.session = self.thread = self.issuer = None
        self._bound = None
        self._stop = threading.Event()
        self._retired = False
        self.last_error = None

    # LLM: Copy/fork and original endpoint/mutex/source drift reject before any inherited lifecycle lock; closing finishes remain valid.
    # 函数用途: 核对原C创建对象及捕获引用，不把关闭期精确结束误判为新启动。
    def _require_original(self):
        if self._origin is not self or self._creator != os.getpid():
            raise WorkerCommitRejected("original_native_executor_lane_required")
        launch, receiver, lifecycle = self._sources
        if self.launch is not launch or self.receiver is not receiver or self._lifecycle is not lifecycle:
            raise WorkerCommitRejected("original_native_executor_lane_source_required")
        receiver._check_creator()
        if self._bound is not None:
            issuer, session, sender = self._bound
            if (
                self.issuer is not issuer
                or self.session is not session
                or session._sources != (issuer, receiver, sender)
            ):
                raise WorkerCommitRejected("original_native_executor_lane_source_required")
            if (
                issuer._source_origin[0] is not launch
                or issuer.launch is not launch
                or issuer.process is not issuer._source_origin[1]
            ):
                raise WorkerCommitRejected("original_worker_source_required")
            require_lane_issuer(issuer, self._issuer_sources)
            session._require_original()

    # LLM: Outside T/C gates; local attach/start/retirement serialize, old READY still proves W receivers and the sole original issuer remains.
    # 函数用途: 只给尚未退役的原lane绑定一次原issuer，不与启动或关闭交错。
    def attach(self, issuer):
        self._require_original()
        with self._lifecycle:
            self._require_original()
            if self._retired:
                raise WorkerCommitRejected("original_native_executor_lane_retired")
            if self._bound is not None:
                raise WorkerCommitRejected("original_native_executor_lane_already_bound")
            if issuer._source_origin[0] is not self._sources[0]:
                raise WorkerCommitRejected("original_worker_source_required")
            sender = KernelMessageSender(self._addresses[1])
            try:
                session = NativeExecutorSession(issuer, self.receiver, sender)
                self.issuer, self.session = issuer, session
                self._issuer_sources = (
                    issuer.control,
                    issuer._lease,
                    issuer._controller,
                    issuer._source_origin,
                )
                self._bound = (issuer, session, sender)
            except BaseException:
                sender.close()
                raise

    # LLM: Local startup serializes and original C gate orders P4 check/local Thread creation with withdrawal; consumer receive/business/join remain outside T/C.
    # 函数用途: 与原撤权门互斥地启动唯一消费者，不持门等消息、跑业务或收尾线程。
    def start(self):
        self._require_original()
        with self._lifecycle:
            self._require_original()
            if self._retired:
                raise WorkerCommitRejected("original_native_executor_lane_retired")
            if self._bound is None or self.issuer is None or self.session is None:
                raise WorkerCommitRejected("original_native_executor_lane_unbound")
            if self.thread is not None or self._stop.is_set():
                raise WorkerCommitRejected("original_native_executor_consumer_already_started")
            with self._issuer_sources[1]._lifecycle_gate:
                self._require_original()
                self.issuer._require_current()
                self.issuer._require_worker(self.issuer.process.pid)
                thread = threading.Thread(
                    target=self._consume,
                    name=f"native-executor-w{self.launch.partition.index}",
                    daemon=True,
                )
                self.thread = thread
                try:
                    thread.start()
                except BaseException:
                    self._retired = True
                    self._stop.set()
                    raise

    # LLM: Receive tick .1 is not the 5-second context/reply budget; requires session's independent receive_timeout API.
    # 函数用途: Deliver each same-kernel-receive resource packet to the original session and retain exact failures.
    def _consume(self):
        while not self._stop.is_set():
            try:
                self._require_original()
                with self.session._lock:
                    if self._stop.is_set():
                        return
                    self.session.serve_once(timeout=5, receive_timeout=0.1)
            except KernelMessageError as error:
                if error.error_code != "KERNEL_MESSAGE_TIMEOUT":
                    self.last_error = error
                    self._stop.wait(0.05)
            except Exception as error:
                self.last_error = error
                self._stop.wait(0.05)
            # Give the composition-root closer a chance to acquire the local endpoint mutex.
            self._stop.wait(0.001)

    # LLM: Parent checks responsibility tables; published Thread always requires real gate-free join, even when native start ack is unknown.
    # 函数用途: 固定退役后实际join；未确认启动或退出则保留端点和引用，不借ident空值复活或排空。
    def close_stopped(self):
        self._require_original()
        with self._lifecycle:
            self._require_original()
            if self.thread is not None and not self._stop.is_set():
                raise WorkerCommitRejected("native_executor_consumer_not_stopped")
            self._retired = True
            self._stop.set()
            thread = self.thread
        if thread is not None:
            try:
                thread.join(timeout=6)
            except RuntimeError as error:
                # Failed start acknowledgement may still own a native thread; keep endpoints for real exit confirmation.
                raise WorkerCommitRejected("native_executor_consumer_stop_unconfirmed") from error
            if thread.is_alive():
                raise WorkerCommitRejected("native_executor_consumer_stop_unconfirmed")
        if self.session is None:
            self.receiver.close()
        else:
            self.session.close()
