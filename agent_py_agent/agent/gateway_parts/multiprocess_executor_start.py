# LLM: 两阶段启动接口由四类 P1b 入口共用；默认 count=0 必须零新增 I/O。
# 模块用途: 在用户命令执行之前持久预留、登记并读回，红测先冻结接口。
import copy
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..common.executor_registration import ExecutorTextIO
from . import multiprocess_executor_identity as identities
from . import multiprocess_executor_process as processes
from .daemon_metadata import process_host_id
from .multiprocess_records import MultiprocessRecordError, validate_record


# LLM: 只运输调用方明确允许的标准流和 FD，不取环境测试标记。
# 类用途: 指定执行者必须继承的描述符。
@dataclass(frozen=True)
class ExecutorIO:
    stdio: tuple = (subprocess.DEVNULL, subprocess.DEVNULL, subprocess.DEVNULL)
    pass_fds: tuple[int, ...] = ()
    text: ExecutorTextIO | None = None


# LLM: 命令正文不进入P1/错误；native_domain只可为原C内核域对象，wire不能构造，默认None完全沿原设施；同步start/native/exec静态合同。
# 类用途: 保存真实命令及其启动上下文。
@dataclass(frozen=True)
class ExecutorCommand:
    argv: tuple[str, ...]
    cwd: Path | None = None
    env: dict | None = None
    io: ExecutorIO = ExecutorIO()
    native_domain: object | None = None


# LLM: 组合根注入观察者，生产默认空操作；三个边界不读取环境或改变安全判定。
# 类用途: 让设施在预留、创建、登记后的固定点观察或暂停真实进程。
class ExecutorStartObserver:
    # LLM: 在 reserved 已持久化、任何进程尚未创建时调用。
    # 函数用途: 观察预留完成。
    def after_reserved(self, record: dict) -> None:
        pass

    # LLM: 垫片已确认等待、登记尚未提交，不得执行用户命令。
    # 函数用途: 观察创建后的等待进程。
    def after_spawned_before_registered(self, record: dict, process) -> None:
        pass

    # LLM: 只在 registered 原文件读回一致后调用，放行信号尚未发送。
    # 函数用途: 观察放行前的可信登记。
    def after_registered_before_release(self, record: dict, process) -> None:
        pass


# LLM: 成功只证明放行前完整登记，不代表命令成功或整树退出；失败回执不回显命令和凭据。
# 类用途: 返回可供下一段组合根管理的进程或明确结构化拒绝。
@dataclass(frozen=True)
class ExecutorStartResult:
    process: object | None
    reservation: dict | None
    error: dict | None

    # LLM: 机器只读结构化错误，不用自然语言或进程存在判断业务成功。
    # 函数用途: 区分放行接口成功与失败。
    @property
    def ok(self) -> bool:
        return self.error is None


# LLM: 只有本次 durable reserve 成功后才持有可清理的预留；重复 ID 失败不能废弃别人的原事实。
# 类用途: 记录启动阶段及本次自建垫片，便于独立失败回收。
@dataclass
class _StartAttempt:
    ledger: object
    record: dict | None = None
    waiting: processes.WaitingExecutor | None = None
    stage: str = "reserved"
    registered_row: dict | None = None
    release_started: bool = False
    failed: bool = False
    cleanup: dict | None = None
    domain: object | None = None
    creator_pid: int = field(default_factory=os.getpid, init=False)
    _origin: object = field(init=False, repr=False)

    # LLM: Created only by the original start pipeline; this in-memory identity is not reconstructed from P1 or wire.
    # 函数用途: 固定本次实际启动尝试的原对象，供native保留表核对。
    def __post_init__(self):
        self._origin = self


# LLM: count=0先于全部验证/I/O/attempt；可选原观察者在物理创建前保留同一attempt，失败不丢实际对象。
# 函数用途: 供内部组合根调用完整两阶段启动，原短门只包spawn/R，未知失败保留责任。
def start_executor(ledger, reservation: dict, command: ExecutorCommand, observer=None):
    if type(ledger.partition.count) is int and ledger.partition.count == 0:
        return None
    attempt = _StartAttempt(ledger)
    observer = observer if observer is not None else ExecutorStartObserver()
    try:
        retain_attempt = getattr(observer, "after_attempt_created", None)
        if retain_attempt is not None:
            retain_attempt(attempt)
        _validate_command(command)
        attempt.record = ledger.reserve(reservation)
        observer.after_reserved(copy.deepcopy(attempt.record))
        _spawn_and_register(attempt, command, observer)
        attempt.stage = "release"
        processes.prepare_waiting_context(attempt.waiting)
        _physical_effect(attempt, "executor_release", lambda: _release_registered(attempt))
        return ExecutorStartResult(attempt.waiting.process, copy.deepcopy(attempt.record), None)
    except BaseException as exc:
        result = _failed_start(attempt, exc)
        if not isinstance(exc, Exception):
            raise
        return result


# LLM: 原短门内最终读回后记录实际R尝试；写后close异常不能把已经可能执行的命令降成未放行。
# 函数用途: 只在原canonical行仍精确一致时发R，保留放行不确定的责任事实。
def _release_registered(attempt: _StartAttempt) -> None:
    if attempt.ledger.read(attempt.record["reservation_id"]) != attempt.record:
        raise MultiprocessRecordError("executor_registration_readback_conflict")
    attempt.release_started = True
    processes.release_prepared_waiting(attempt.waiting)


# LLM: 可信组合根可注入原短门直接执行callback；无门的既有设施保持原行为，不据此声称W/C生产接线完成。
# 函数用途: 包实际创建或放行，不把等待垫片/用户命令放入生命周期门。
def _physical_effect(attempt, phase, callback):
    gate = getattr(attempt.ledger, "perform_short_effect", None)
    if gate is None:
        return callback()
    return gate(attempt.record, phase, callback)


# LLM: 原spawn callback先存WaitingExecutor，native域绑定同attempt/Popen并原R前随v2登记；ready等待仍门外，未知物理创建不丢引用；同步exec chain/start/native tests。
# 函数用途: 先保住真实创建引用，完成登记读回并冻结R前原行。
def _spawn_and_register(attempt: _StartAttempt, command: ExecutorCommand, observer) -> None:
    attempt.stage = "spawn"
    if command.native_domain is not None:
        command.native_domain.bind_attempt(attempt)

    # LLM: This callback contains only the actual creation and immediate reference retention, not pipe readiness or user execution.
    # 函数用途: 在原创建边界保留真实垫片，不能等门返回后才接收引用。
    def spawn():
        attempt.waiting = processes.spawn_waiting(command)
        return attempt.waiting

    _physical_effect(attempt, "executor_spawn", spawn)
    processes.await_waiting(attempt.waiting)
    process = attempt.waiting.process
    observer.after_spawned_before_registered(copy.deepcopy(attempt.record), process)
    if attempt.waiting.invalid_fds:
        raise MultiprocessRecordError("invalid_executor_fd_source")
    attempt.stage = "registration"
    registration = _registration(process.pid)
    if command.native_domain is not None:
        registration["execution_domain"] = command.native_domain.registration(registration)
    attempt.record = attempt.ledger.advance(attempt.record, "registered", registration)
    attempt.stage = "readback"
    if attempt.ledger.read(attempt.record["reservation_id"]) != attempt.record:
        raise MultiprocessRecordError("executor_registration_readback_conflict")
    attempt.registered_row = copy.deepcopy(attempt.record)
    if command.native_domain is not None:
        command.native_domain.registered(attempt.record)
    observer.after_registered_before_release(copy.deepcopy(attempt.record), process)


# LLM: 直接取内核出生身份，None 不补 PID 或时间；PGID/SID属真实等待实例，exec 后PID不变。
# 函数用途: 构造严格 P0 进程登记字段，不调用用户命令查询身份。
def _registration(pid: int) -> dict:
    identity = validate_record({"schema_version": "multiprocess_process_identity.v1", "host_id": process_host_id(),
                                "pid": pid, "start_time": identities.process_birth(pid)})
    return {"process_identity": identity, "pgid": os.getpgid(pid), "sid": os.getsid(pid)}


# LLM: Mark failure before cleanup; retained original attempt/Popen survive uncertain reap and R-after-write errors. Abandonment never proves a released tree empty.
# 函数用途: 保留实际失败进程和清理结果，不把返回None当未创建或域已消失。
def _failed_start(attempt: _StartAttempt, failure: BaseException) -> ExecutorStartResult:
    attempt.failed = True
    cleanup = processes.abort_waiting(attempt.waiting)
    attempt.cleanup = cleanup
    abandonment, abandonment_error = "not_reserved", None
    if attempt.record is not None:
        try:
            attempt.record = attempt.ledger.abandon(attempt.record)
            abandonment = "confirmed"
        except BaseException as exc:
            abandonment, abandonment_error = "unknown", type(exc).__name__
    error = {"ok": False, "error_code": "EXECUTOR_RESERVATION_FAILED", "stage": attempt.stage,
             "cause": type(failure).__name__, "cleanup": cleanup, "abandonment": abandonment,
             "abandonment_error": abandonment_error}
    process = None if attempt.waiting is None else attempt.waiting.process
    return ExecutorStartResult(process, copy.deepcopy(attempt.record), error)


# LLM: 原native域必须精确C内类型/原来源，不能先调用任意foreign.bind_attempt；坏参数在管道/预留前拒绝，text只原策略；同步source/Count0/exec静态合同。
# 函数用途: 验证最小启动合同，真实权限仍由原命令入口负责。
def _validate_command(command: ExecutorCommand) -> None:
    valid = (isinstance(command, ExecutorCommand) and type(command.argv) is tuple and bool(command.argv)
             and all(type(item) is str and "\x00" not in item for item in command.argv) and bool(command.argv[0]))
    if not valid or not isinstance(command.io, ExecutorIO) or len(command.io.stdio) != 3:
        raise MultiprocessRecordError("invalid_executor_command")
    if command.native_domain is not None:
        from .multiprocess_native_linux_domain import NativePidNamespaceDomain
        if type(command.native_domain) is not NativePidNamespaceDomain:
            raise MultiprocessRecordError('original_native_namespace_required')
        command.native_domain._require_original()
    descriptors = command.io.pass_fds
    if type(descriptors) is not tuple or any(type(fd) is not int or fd < 3 for fd in descriptors):
        raise MultiprocessRecordError("invalid_executor_fds")
    if len(set(descriptors)) != len(descriptors):
        raise MultiprocessRecordError("duplicate_executor_fds")
    text = command.io.text
    if text is not None and not _valid_text_policy(text):
        raise MultiprocessRecordError("invalid_executor_text_io")


# LLM: 只核既有UTF8严格/替换和搜索原locale严格两种声明，不以模式名绕过启动合同。
# 函数用途: 在建FD或登记前拒绝任意编码/忽略错误/非整数缓冲声明。
def _valid_text_policy(text) -> bool:
    if not isinstance(text, ExecutorTextIO):
        return False
    policy = ((text.encoding == "utf-8" and text.errors in {"replace", "strict"})
              or (text.encoding is None and text.errors is None))
    return policy and type(text.bufsize) is int and text.bufsize in {-1, 0, 1}
