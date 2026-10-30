# LLM: W仅运输资源并消费原C事实，不授予W创建进程、写P1或写ACK的权限，原内核对端校验和执行围栏保持。
# 模块用途: 串行管理原生执行器RPC，提供W持有的兼容Popen标准流。
from __future__ import annotations

import json
import os
import re
import selectors
import signal
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field

from ..common.executor_registration import ExecutorProcessOptions, ExecutorRegistrationError
from .multiprocess_executor_identity import process_birth
from .multiprocess_kernel_message import KernelMessageReceiver, KernelMessageSender, _seconds
from .multiprocess_native_commit_client import _decode_reply
from .multiprocess_native_control_receive import receive_control_packet
from .multiprocess_native_executor_io import prepare_native_executor_io
from .multiprocess_worker_commit import WorkerCommitRejected

# 参数用途: 原native RPC每次最多等待5秒；不扩大请求模型执行期限。
_RPC_TIMEOUT_SECONDS = 5
# 参数用途: 匿名命令上下文最多16MiB字节；超限拒绝，不截断成默认参数。
_CONTEXT_MAX_BYTES = 16 * 1024 * 1024


# LLM: Captured parent birth and per-packet kernel sender reject wrong counterparts; locators never authorize.
# 类用途: Own the original W endpoints and monotonically serialize all executor RPCs.
class NativeExecutorClient:
    # LLM: Capture actual parent birth and exact W-created endpoints; constructor performs no business RPC or P1 write.
    # 函数用途: 固定本SDK的创建进程、真实父出生和两端点，初始化同一持久transport状态。
    def __init__(
        self,
        sender,
        receiver,
        coordinator_pid,
        *,
        request_id=None,
        execution_attempt_id=None,
        tool_call_id=None,
    ):
        if type(sender) is not KernelMessageSender or type(receiver) is not KernelMessageReceiver:
            raise WorkerCommitRejected("original_native_endpoint_required")
        sender._check_creator()
        receiver._check_creator()
        birth = (
            process_birth(coordinator_pid)
            if type(coordinator_pid) is int and coordinator_pid > 0
            else None
        )
        if coordinator_pid != os.getppid() or birth is None:
            raise WorkerCommitRejected("original_coordinator_parent_required")
        binding = (
            None
            if (request_id, execution_attempt_id, tool_call_id) == (None, None, None)
            else _binding(request_id, execution_attempt_id, tool_call_id)
        )
        self._origin, self._creator_pid = self, os.getpid()
        self._parent = (coordinator_pid, birth)
        self._sender, self._receiver = sender, receiver
        self._endpoints = (sender, receiver)
        self._binding = binding
        self._lock = threading.RLock()
        self._seq, self._broken = 0, False
        self._unknown, self._pending_handles = {}, set()

    # LLM: A facade only freezes request locators; no endpoint/sequence allocation or C authority comes from binding.
    # 函数用途: 生成不可变请求定位facade，仍共享唯一原transport。
    def bind_request(self, request_id, execution_attempt_id, tool_call_id):
        self._require_original()
        binding = _binding(request_id, execution_attempt_id, tool_call_id)
        return NativeExecutorRequest(self, *binding)

    # LLM: Reject copies/forks and endpoint drift before inherited locks or touching current endpoints.
    # 函数用途: 核原SDK与构造时两端点、实际父出生，不把另一个本进程端点当原transport。
    def _require_original(self):
        if (
            self._origin is not self
            or self._creator_pid != os.getpid()
            or self._sender is not self._endpoints[0]
            or self._receiver is not self._endpoints[1]
        ):
            raise WorkerCommitRejected("original_native_executor_client_required")
        if os.getppid() != self._parent[0] or process_birth(self._parent[0]) != self._parent[1]:
            raise WorkerCommitRejected("original_coordinator_parent_required")
        self._sender._check_creator()
        self._receiver._check_creator()

    # LLM: After an unknown transport outcome no automatic replay or inferred cleanup occurs.
    # 函数用途: 只递增已知transport序号，未知交付后拒绝新调用。
    def _next(self):
        if self._broken:
            raise WorkerCommitRejected("native_executor_delivery_unknown")
        self._seq += 1
        return self._seq

    # LLM: Consume one actual no-FD C packet; exact sender/op/seq/handle checks precede caller state settlement.
    # 函数用途: 核同包真实父sender和精确关联，不用正文身份洗掉未知责任。
    def _receive(self, seq, expected, deadline, handle=None):
        raw, peer = receive_control_packet(self._receiver, timeout=_remaining(deadline))
        self._require_original()
        if type(peer) is not int or peer != self._parent[0]:
            raise WorkerCommitRejected("original_coordinator_message_required")
        value = _decode_reply(raw)
        fields = {"op", "seq"} | ({"result"} if expected in ("applied", "process") else set())
        if expected in ("applied", "finished"):
            fields.add("handle")
        if (
            type(value) is not dict
            or set(value) != fields
            or value.get("op") != expected
            or type(value.get("seq")) is not int
            or value["seq"] != seq
            or (
                handle is not None
                and (type(value.get("handle")) is not int or value["handle"] != handle)
            )
        ):
            raise WorkerCommitRejected("native_reply_unconfirmed")
        return value

    # LLM: One send per exact seq; all process tokens only locate original C Popen references.
    # 函数用途: 串行发送一次并读原C事实，异常保留关联且不重发。
    def _rpc(self, frame, expected, *, timeout=_RPC_TIMEOUT_SECONDS, handle=None):
        self._require_original()
        with self._lock:
            deadline = time.monotonic() + _seconds(timeout)
            seq = self._next()
            self._unknown[seq] = {"seq": seq, **frame}
            try:
                KernelMessageSender.send(
                    self._sender, _wire({**frame, "seq": seq}), timeout=_remaining(deadline)
                )
                reply = self._receive(seq, expected, deadline, handle)
                self._unknown.pop(seq)
                if expected == "finished":
                    self._pending_handles.discard(handle)
                return reply
            except BaseException:
                self._broken = True
                raise

    # LLM: Context is anonymous, read-only at C, bounded, sent before writing; no argv/env disk spill.
    # 函数用途: Send one original start, precisely finish its handle, then expose process facts and PIPE parents.
    def launch(self, kind, argv, options):
        self._require_original()
        if self._binding is None:
            raise WorkerCommitRejected("native_executor_request_binding_required")
        return self._launch(kind, argv, options, self._binding)

    # LLM: Prepare only owned IO/context; C owns start/P1; only fixed background_host delegate is supported.
    # 函数用途: 匿名管道运输原上下文，原C明确启动与结清后交回标准流代理。
    def _launch(self, kind, argv, options, binding):
        self._require_original()
        if (
            not isinstance(options, ExecutorProcessOptions)
            or options.shell
            or options.delegate not in (None, "background_host")
        ):
            raise ValueError("invalid_native_executor_options")
        if type(kind) is not str or not kind or isinstance(argv, (str, bytes)):
            raise ValueError("invalid_native_executor_command")
        args = list(argv)
        if options.delegate is not None:
            from .multiprocess_executor_delegation import _check_host_argv
            if kind != "background_host":
                raise ValueError("invalid_native_executor_options")
            _check_host_argv(args)
        env = dict(os.environ) if options.env is None else dict(options.env)
        cwd = os.path.abspath(os.fspath(options.cwd)) if options.cwd is not None else os.getcwd()
        if (
            not args
            or any(type(v) is not str or "\0" in v for v in args)
            or not args[0]
            or "\0" in cwd
            or any(
                type(k) is not str
                or not k
                or "=" in k
                or "\0" in k
                or type(v) is not str
                or "\0" in v
                for k, v in env.items()
            )
        ):
            raise ValueError("invalid_native_executor_context")
        data = _wire({"version": 1, "argv": args, "cwd": cwd, "env": env})
        if len(data) > _CONTEXT_MAX_BYTES:
            raise ValueError("native_executor_context_size_invalid")
        with self._lock:
            self._require_original()
            deadline = time.monotonic() + _RPC_TIMEOUT_SECONDS
            seq = self._next()
            try:
                with prepare_native_executor_io(options) as io:
                    read, write = os.pipe()
                    try:
                        frame = dict(
                            op="executor_delegate_start" if options.delegate is not None else "executor_start",
                            seq=seq,
                            **dict(
                                zip(("request_id", "execution_attempt_id", "tool_call_id"), binding)
                            ),
                            kind=kind,
                            stdio=io.wire,
                            text=asdict(options.text) if options.text is not None else None,
                        )
                        self._unknown[seq] = {"op": "executor_start", "seq": seq, "handle": seq}
                        KernelMessageSender.send_fds(
                            self._sender,
                            _wire(frame),
                            (read, *io.fds),
                            timeout=_remaining(deadline),
                        )
                        _write_context(write, data, deadline)
                    finally:
                        os.close(read)
                        os.close(write)  # EOF must precede C's applied reply.
                    applied = self._receive(seq, "applied", deadline, seq)
                    outcome = applied["result"]
                    if type(outcome) is not dict or type(outcome.get("ok")) is not bool:
                        raise WorkerCommitRejected("native_reply_unconfirmed")
                    if outcome["ok"] and (
                        set(outcome) != {"ok", "process_token", "pid"}
                        or type(outcome["process_token"]) is not str
                        or not outcome["process_token"]
                        or type(outcome["pid"]) is not int
                        or outcome["pid"] <= 0
                    ):
                        raise WorkerCommitRejected("native_reply_unconfirmed")
                    # A fresh no-FD finish references precisely the applied start handle.
                    self._unknown.pop(seq)
                    self._pending_handles.add(applied["handle"])
                    self._rpc(
                        {"op": "finish", "handle": applied["handle"]},
                        "finished",
                        timeout=_remaining(deadline),
                        handle=applied["handle"],
                    )
                    if not outcome["ok"]:
                        raise ExecutorRegistrationError(outcome)
                    proxy = self._make_process(outcome["process_token"], outcome["pid"], args, io)
                return proxy
            except ExecutorRegistrationError:
                # C explicitly reported rejection and confirmed exact finish; this is not unknown delivery.
                raise
            except BaseException:
                self._broken = True
                raise

    # LLM: Token is a locator only; original C validates process ownership and performs polling/signals.
    # 函数用途: 把进程查询或信号交原C，严格核返回码形状。
    def _process(self, token, signum=None, *, timeout=_RPC_TIMEOUT_SECONDS):
        frame = {
            "op": "executor_poll" if signum is None else "executor_signal",
            "process_token": token,
        }
        if signum is not None:
            if type(signum) is not int or signum <= 0:
                raise ValueError("invalid_executor_signal")
            frame["signal"] = signum
        reply = self._rpc(frame, "process", timeout=timeout)["result"]
        if (
            type(reply) is not dict
            or set(reply) != {"returncode"}
            or (reply["returncode"] is not None and type(reply["returncode"]) is not int)
        ):
            self._broken = True
            raise WorkerCommitRejected("native_reply_unconfirmed")
        return reply["returncode"]

    # LLM: Default W proxy semantics unchanged; closed host subclass may preserve no-reap observation until Store birth.
    # 函数用途: Construct the normal process proxy after exact start/finish, without changing W control protocol.
    def _make_process(self, token, pid, args, io):
        return NativeExecutorProcess(self, token, pid, args, io)


# LLM: 只验证运输用的请求/尝试/工具编号，不能用字符串形状证明原claim或native来源，实际RPC仍核原transport。
# 函数用途: 限制三个定位编号的形状并返回原值，非法时结构拒绝，不执行I/O。
def _binding(request_id, execution_attempt_id, tool_call_id):
    if (
        type(request_id) is not str
        or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,255}", request_id)
        or any(
            type(v) is not str or not v or len(v) > 512
            for v in (execution_attempt_id, tool_call_id)
        )
    ):
        raise WorkerCommitRejected("commit_request_invalid")
    return request_id, execution_attempt_id, tool_call_id


# LLM: Immutable request facade never owns transport state; copies/forks reject before the original client's lock.
# 类用途: 固定一次请求的三个定位编号并绑定原transport身份，不另建连接、序列或授权。
@dataclass(frozen=True, eq=False)
class NativeExecutorRequest:
    _transport: NativeExecutorClient = field(repr=False)
    request_id: str
    execution_attempt_id: str
    tool_call_id: str
    _origin: object = field(init=False, repr=False)
    _creator_pid: int = field(init=False, repr=False)

    # LLM: Frozen facade validates original transport and captures creator; it owns no sequence or endpoint.
    # 函数用途: 创建facade时固定自身出处与创建进程，不另建transport。
    def __post_init__(self):
        self._transport._require_original()
        _binding(self.request_id, self.execution_attempt_id, self.tool_call_id)
        object.__setattr__(self, "_origin", self)
        object.__setattr__(self, "_creator_pid", os.getpid())

    # LLM: Reject facade copy/fork before entering original transport; reuse exact frozen request locators.
    # 函数用途: 以原facade三项定位调用同一transport，锁前拒副本或fork。
    def launch(self, kind, argv, options):
        if self._origin is not self or self._creator_pid != os.getpid():
            raise WorkerCommitRejected("original_native_executor_client_required")
        self._transport._require_original()
        return self._transport._launch(
            kind, argv, options, (self.request_id, self.execution_attempt_id, self.tool_call_id)
        )


# LLM: 只编码调用者已核过的有界运输值，JSON内容不授信；修改时保持原native frame紧凑格式与RPC测试。
# 函数用途: 生成固定紧凑JSON字节，不写盘、不新增来源字段。
def _wire(value):
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8")

# LLM: 始终消费原绝对deadline，不为每次select重置预算；超时不证明命令未进入或可重放。
# 函数用途: 返回原期限剩余秒数，过期明确报超时，不发生I/O。
def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("native_executor_reply_timeout")
    return remaining


# LLM: FD已经由原协议送达；非阻塞背压等待仅在C/T门外，沿同一绝对deadline，不把argv/env落盘。
# 函数用途: 在原期限内向匿名上下文FD写完内容，超时和写失败原样上抛。
def _write_context(fd, data, deadline):
    os.set_blocking(fd, False)
    offset = 0
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_WRITE)
        while offset < len(data):
            if not selector.select(_remaining(deadline)):
                raise TimeoutError("native_executor_context_timeout")
            try:
                offset += os.write(fd, data[offset : offset + 65536])
            except BlockingIOError:
                continue


# LLM: pid仅供展示，轮询和信号始终通过原C进程令牌RPC；等待退避不得改变初次poll与管道EOF合同。
# 类用途: 保留W的管道父端及Popen等待语义，不在W中创建子进程。
class NativeExecutorProcess:
    # LLM: Created only after exact C start/finish; owns W PIPE parents, not C process or P1 authority.
    # 函数用途: 保存C进程定位与W标准流，初始化一次通信状态。
    def __init__(self, client, token, pid, args, io):
        self._client, self._token = client, token
        self._origin, self._creator_pid = self, os.getpid()
        self.pid, self.args, self.returncode = pid, args, None
        self.stdin, self.stdout, self.stderr = io.stdin, io.stdout, io.stderr
        self._communication_lock = threading.Lock()
        self._started = False
        self._threads, self._errors = [], []
        self._chunks = {name: [] for name in ("stdout", "stderr")}
        self._decode = {
            name: (
                getattr(getattr(self, name), "encoding", None),
                getattr(getattr(self, name), "errors", None),
            )
            for name in self._chunks
        }

    # LLM: Reject copy/fork before transport or stdio; original transport still validates actual parent/endpoints.
    # 函数用途: 核原进程代理及其原transport，不能借复制对象使用管道。
    def _require_original(self):
        if self._origin is not self or self._creator_pid != os.getpid():
            raise WorkerCommitRejected("original_native_executor_process_required")
        self._client._require_original()

    # LLM: Query actual C only when returncode cache empty; cache is never product drain or authorization evidence.
    # 函数用途: 从C更新返回码缓存，不以代理缓存裁决排空。
    def _poll(self, timeout=_RPC_TIMEOUT_SECONDS):
        self._require_original()
        if self.returncode is None:
            self.returncode = self._client._process(self._token, timeout=timeout)
        return self.returncode

    # LLM: Public Popen-like query delegates to guarded original process-token path.
    # 函数用途: 通过原代理查询进程返回码。
    def poll(self):
        return self._poll()

    # LLM: 原C查询与执行等待预算分开；零超时仍先查一次，随后只睡正剩余预算，有界退避不发信号或重启。
    # 函数用途: 轮询原C事实直至退出，超时不重启进程。
    def wait(self, timeout=None):
        self._require_original()
        deadline = None if timeout is None else time.monotonic() + _seconds(timeout)
        first = True
        sleep_step = 0.005
        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            if not first and remaining is not None and remaining <= 0:
                raise subprocess.TimeoutExpired(self.args, timeout)
            # Execution waiting deadline and bounded RPC delivery are separate:
            # a zero process timeout must not turn a real C query into an unknown 1ms transport timeout.
            rc = self._poll()
            if rc is not None:
                return rc
            first = False
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise subprocess.TimeoutExpired(self.args, timeout)
            time.sleep(sleep_step if remaining is None else min(sleep_step, remaining))
            sleep_step = 0.05 if sleep_step >= 0.02 else sleep_step * 2

    # LLM: Validate signum then ask original C by token; informational pid grants no signal authority.
    # 函数用途: 将合法信号交原C执行，不在W按正文PID发信号。
    def send_signal(self, signum):
        self._require_original()
        signum = int(signum) if isinstance(signum, signal.Signals) else signum
        if type(signum) is not int or signum <= 0:
            raise ValueError("invalid_executor_signal")
        if self.returncode is None:
            self.returncode = self._client._process(self._token, signum)

    # LLM: Reuse guarded send_signal and original C process token; no local W kill.
    # 函数用途: 经原C发送SIGTERM。
    def terminate(self):
        self.send_signal(signal.SIGTERM)

    # LLM: Reuse guarded send_signal and original C process token; no local W kill.
    # 函数用途: 经原C发送SIGKILL。
    def kill(self):
        self.send_signal(signal.SIGKILL)

    # LLM: Threads only pump W-owned pipes, never RPC or execute; partial bytes survive timeout for continuation.
    # 函数用途: 持续收集本代理输出字节并关闭自有流，保存读错误。
    def _reader(self, name):
        stream = getattr(self, name)
        try:
            while True:
                chunk = os.read(stream.fileno(), 32768)
                if not chunk:
                    break
                self._chunks[name].append(chunk)
        except BaseException as error:
            self._errors.append(error)
        finally:
            stream.close()

    # LLM: Pipe pump owns W stdin only; broken pipe is factual and never triggers retry or execution.
    # 函数用途: 写完输入并关闭自有stdin，保留非断管错误。
    def _writer(self, data):
        try:
            if data is not None:
                offset = 0
                while offset < len(data):
                    written = self.stdin.write(data[offset:])
                    if written is None or written <= 0:
                        raise OSError("native_executor_stdin_write_incomplete")
                    offset += written
            self.stdin.flush()
        except BrokenPipeError:
            pass
        except BaseException as error:
            self._errors.append(error)
        finally:
            try:
                self.stdin.close()
            except BrokenPipeError:
                pass

    # LLM: Read accumulated bytes; timeout callers use raw bytes while final callers apply original text decoding.
    # 函数用途: 拼接已有输出，终态按原编码和换行规则解码。
    def _output(self, name, *, text):
        if getattr(self, name) is None:
            return None
        data = b"".join(self._chunks[name])
        encoding, errors = self._decode[name]
        if text and encoding is not None:
            return (
                data.decode(encoding, errors or "strict").replace("\r\n", "\n").replace("\r", "\n")
            )
        return data

    # LLM: 零超时也先查询原C；退出且管道EOF才完成，退避只夹正剩余预算，超时保留原字节及pump供后续接续。
    # 函数用途: 一次启动标准流通信，可在超时后继续等待同一进程和管道。
    def communicate(self, input=None, timeout=None):
        self._require_original()
        deadline = None if timeout is None else time.monotonic() + _seconds(timeout)
        with self._communication_lock:
            if self._started and input is not None:
                raise ValueError("Cannot send input after starting communication")
            if not self._started:
                if self.stdin is not None and input is not None:
                    encoding = getattr(self.stdin, "encoding", None)
                    if encoding is not None and not isinstance(input, str):
                        raise TypeError("text stdin requires str input")
                    if encoding is None and not isinstance(input, (bytes, bytearray, memoryview)):
                        raise TypeError("binary stdin requires bytes input")
                self._started = True
                for name in ("stdout", "stderr"):
                    if getattr(self, name) is not None:
                        thread = threading.Thread(target=self._reader, args=(name,), daemon=True)
                        self._threads.append(thread)
                        thread.start()
                if self.stdin is not None and not self.stdin.closed:
                    thread = threading.Thread(target=self._writer, args=(input,), daemon=True)
                    self._threads.append(thread)
                    thread.start()
            first, sleep_step = True, 0.005
            while True:
                live = any(t.is_alive() for t in self._threads)
                remaining = None if deadline is None else deadline - time.monotonic()
                # Preserve partial bytes even in text mode, matching TimeoutExpired.
                if not first and remaining is not None and remaining <= 0 and (live or self.returncode is None):
                    raise subprocess.TimeoutExpired(
                        self.args,
                        timeout,
                        output=self._output("stdout", text=False),
                        stderr=self._output("stderr", text=False),
                    )
                rc = self._poll()
                first = False
                if not live and rc is not None:
                    break
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is None or remaining > 0:
                    time.sleep(sleep_step if remaining is None else min(sleep_step, remaining))
                    sleep_step = 0.05 if sleep_step >= 0.02 else sleep_step * 2
            if self._errors:
                raise self._errors[0]
            return self._output("stdout", text=True), self._output("stderr", text=True)

    # LLM: Context entry validates original proxy; creates no process or authority.
    # 函数用途: 核原代理后进入上下文，不执行新命令。
    def __enter__(self):
        self._require_original()
        return self

    # LLM: Close only owned W streams, then wait through original C; no inferred quota or responsibility release.
    # 函数用途: 关闭本代理标准流再等待实际退出，不代替原P1回收。
    def __exit__(self, exc_type, exc_value, traceback):
        self._require_original()
        for stream in (self.stdin, self.stdout, self.stderr):
            if stream is not None and not stream.closed:
                stream.close()
        self.wait()
