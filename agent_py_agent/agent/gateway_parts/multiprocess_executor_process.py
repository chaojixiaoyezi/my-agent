# LLM: P1a 只创建可信等待垫片，不拥有账本/请求权限；放行须由 start 的持久读回门调用。
# 模块用途: 封装等待管道、极小垫片启动及自建进程的有界失败回收。
import json
import os
import selectors
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass

from . import multiprocess_executor_fds as descriptor_plan
from . import multiprocess_executor_shim as shim

# 等待垫片确认和失败回收各5秒；内部握手不依赖模型/命令耗时，超时不放行。
_EXECUTOR_WAIT_SECONDS = 5.0


# LLM: 先交回真实进程句柄再关闭子端；关闭失败时调用方仍有句柄可拒绝放行并回收。
# 类用途: 保存等待进程、父端及父进程暂持的子端描述符。
@dataclass
class WaitingExecutor:
    process: object
    release_fd: int
    ready_fd: int
    child_fds: tuple = ()
    context: bytes = b""
    allowed_fds: tuple = ()
    invalid_fds: bool = False
    context_prepared: bool = False


# LLM: 可信解释器仍空环境/固定根目录/Popen唯一调用；native域只包当前线程birth和原实例捕获，FD冻结及原C/完整帧/R不变；同步exec链精确迁移及namespace实测。
# 函数用途: 启动还不能执行用户命令的垫片，只继承标准流和明确允许的 FD。
def spawn_waiting(command) -> WaitingExecutor:
    context = _command_context(command)
    pinned = descriptor_plan.pin_descriptors(command.io)
    descriptors = list(pinned.owned)
    try:
        release_read, release_write = os.pipe()
        descriptors.extend((release_read, release_write))
        ready_read, ready_write = os.pipe()
        descriptors.extend((ready_read, ready_write))
        with _native_birth(command.native_domain) as domain:
            process = subprocess.Popen(
                [sys.executable, "-I", "-S", shim.__file__, str(release_read), str(ready_write),
                 json.dumps(pinned.allowed)],
                stdin=pinned.stdio[0], stdout=pinned.stdio[1], stderr=pinned.stdio[2],
                cwd="/", env={}, close_fds=True, start_new_session=True,
                pass_fds=(release_read, ready_write, *(fd for fd, _ in pinned.allowed)),
                **(command.io.text.popen_options() if command.io.text is not None else {}),
            )
            if domain is not None:
                domain.capture(process)
    except BaseException:
        _close_descriptors(descriptors, [])
        raise
    return WaitingExecutor(process, release_write, ready_read, (release_read, ready_write, *pinned.owned),
                           context, pinned.allowed, pinned.invalid)


# LLM: Count0和普通设施的None分支零新增I/O；只有精确原C域类型进入Linux内核birth，不能接受任意回调或wire对象，Popen仍唯一原调用。
# 函数用途: 在原实际spawn周围建立/恢复当前线程for_children，默认路径不改变。
@contextmanager
def _native_birth(domain):
    if domain is None:
        yield None
        return
    from .multiprocess_native_linux_domain import NativePidNamespaceDomain
    if type(domain) is not NativePidNamespaceDomain:
        raise ValueError('original_native_namespace_required')
    with domain.birth():
        yield domain


# LLM: 只存内存，不把环境或命令写账本/文件/错误；JSON转义保留POSIX环境的surrogateescape字节，冻结继承环境/cwd，bootstrap仍用空环境；同步检查shim及exec链合同。
# 函数用途: 构造有界且可无损运输非UTF8环境字节的放行帧，解释器加载绝不使用业务环境。
def _command_context(command) -> bytes:
    environment = dict(os.environ) if command.env is None else dict(command.env)
    if any(type(key) is not str or type(value) is not str or not key or "=" in key or "\x00" in key + value
           for key, value in environment.items()):
        raise ValueError("invalid_executor_environment")
    context = {"version": 1, "argv": command.argv, "cwd": os.path.abspath(command.cwd or os.getcwd()), "env": environment}
    content = json.dumps(context, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if len(content) > shim._EXECUTOR_CONTEXT_MAX_BYTES:
        raise ValueError("executor_context_too_large")
    return content


# LLM: 等 W 和 EOF 而不靠 sleep；EOF证明子端已关闭 ready，父不因 pipe 创建成功就假装垫片就绪。
# 函数用途: 在总5秒内确认垫片握手，否则拒绝登记和放行。
def await_waiting(waiting: WaitingExecutor) -> None:
    _close_child_fds(waiting)
    try:
        _wait_ready(waiting.ready_fd)
    finally:
        os.close(waiting.ready_fd)
        waiting.ready_fd = -1
    if waiting.process.poll() is not None:
        raise ValueError("executor_waiting_unconfirmed")


# LLM: 总预算内须看到唯一W及EOF；DefaultSelector没有select的FD上限，高号合法管道仍必须走同一握手。
# 函数用途: 按真实可读事件等就绪，不用固定 sleep 猜测进程时序。
def _wait_ready(descriptor: int) -> None:
    deadline = time.monotonic() + _EXECUTOR_WAIT_SECONDS
    with _descriptor_events(descriptor, selectors.EVENT_READ) as events:
        _read_ready(descriptor, events, deadline)


# LLM: 内容校验与就绪观察分开但使用同一总期限，收到EOF也不能把空帧或重复W当作就绪。
# 函数用途: 在真实可读事件后收齐唯一握手字节与EOF。
def _read_ready(descriptor: int, events, deadline: float) -> None:
    content = b""
    while True:
        _wait_event(events, deadline, "executor_waiting_timeout")
        fragment = os.read(descriptor, 2)
        if not fragment:
            break
        content += fragment
        if content != b"W":
            raise ValueError("executor_waiting_invalid")
    if content != b"W":
        raise ValueError("executor_waiting_unconfirmed")


# LLM: 每个成功 close 才移除所有权，异常保留剩余 FD 给独立回收，绝不能默默继续登记。
# 函数用途: 关闭父进程持有的子端，失败直接中止等待握手。
def _close_child_fds(waiting: WaitingExecutor) -> None:
    while waiting.child_fds:
        os.close(waiting.child_fds[0])
        waiting.child_fds = waiting.child_fds[1:]


# LLM: 登记读回后在原短门之外发送C与完整帧；C只准备，完整帧也不能exec，等待仍使用原有界预算。
# 函数用途: 先运输冻结上下文，后续独立短门才能发送最终R。
def prepare_waiting_context(waiting: WaitingExecutor) -> None:
    if waiting.context_prepared:
        raise ValueError("executor_context_already_prepared")
    os.set_blocking(waiting.release_fd, False)
    if os.write(waiting.release_fd, b"C") != 1:
        raise OSError("executor_context_short_write")
    _write_context(waiting.release_fd, waiting.context)
    waiting.context_prepared = True


# LLM: 这里只做单次非阻塞R写及关闭，不等selector/帧/用户命令；准备标志不是权限，原C门仍须最终复核。
# 函数用途: 在原实际短门内发最后一个放行字节，EAGAIN直接失败而非持门重试。
def release_prepared_waiting(waiting: WaitingExecutor) -> None:
    if not waiting.context_prepared:
        raise ValueError("executor_context_not_prepared")
    if os.write(waiting.release_fd, b"R") != 1:
        raise OSError("executor_release_short_write")
    os.close(waiting.release_fd)
    waiting.release_fd = -1


# LLM: 保留既有独立设施API的完整运输/放行行为；组合根绝不把此复合函数传入C短门。
# 函数用途: 无原C门的独立垫片设施兼容入口，生产start使用分开的prepare和final-release。
def release_waiting(waiting: WaitingExecutor) -> None:
    prepare_waiting_context(waiting)
    release_prepared_waiting(waiting)


# LLM: 帧可能超过PIPE_BUF；写帧和最终R的门外可写准备共用原5秒绝对期限，不能把等待移入C短门。
# 函数用途: 完整运输上下文并等管道留出放行空间，准备成功不等于获写权限。
def _write_context(descriptor: int, context: bytes) -> None:
    data = len(context).to_bytes(4, "big") + context
    deadline = time.monotonic() + _EXECUTOR_WAIT_SECONDS
    os.set_blocking(descriptor, False)
    with _descriptor_events(descriptor, selectors.EVENT_WRITE) as events:
        _write_frame(descriptor, data, lambda: _wait_event(events, deadline, "executor_context_timeout"))
        # 本父端是唯一写者；完整帧可能恰好填满管道，必须在原门外等消费空间后才宣告准备完。
        _wait_event(events, deadline, "executor_context_timeout")


# LLM: 可写事件只授一次非阻塞写机会，短写须推进原帧偏移；EAGAIN继续等待但不会重置总期限。
# 函数用途: 完整发送长度和冻结内容，不把部分发送当成功。
def _write_frame(descriptor: int, data: bytes, await_write) -> None:
    offset = 0
    while offset < len(data):
        await_write()
        try:
            count = os.write(descriptor, data[offset:])
        except BlockingIOError:
            continue
        if count <= 0:
            raise OSError("executor_context_short_write")
        offset += count


# LLM: selector自身FD只在本次等待范围内存在，注册或等待异常也必须关闭，不能改变调用者FD所有权。
# 函数用途: 为合法任意号管道建立一次有界事件观察。
@contextmanager
def _descriptor_events(descriptor: int, mode: int):
    with selectors.DefaultSelector() as events:
        events.register(descriptor, mode)
        yield events


# LLM: 每次都扣同一绝对期限，空就绪或预算耗尽按原错误失败；不因高FD添加重试/放行分支。
# 函数用途: 等待读写事件并保持原总超时合同。
def _wait_event(events, deadline: float, reason: str) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not events.select(remaining):
        raise TimeoutError(reason)


# LLM: 先关父端令 EOF，再 wait；所有流独立关闭，清理失败只作结构化诊断，不覆盖原失败。
# 函数用途: 有界终止并回收本次自建等待进程，不碰其它进程或把它当整树接手证明。
def abort_waiting(waiting: WaitingExecutor | None) -> dict:
    if waiting is None:
        return {"process_reaped": True, "failures": []}
    failures = []
    _close_descriptors([waiting.release_fd, waiting.ready_fd, *waiting.child_fds], failures)
    waiting.release_fd = waiting.ready_fd = -1
    waiting.child_fds = ()
    _reap(waiting.process, failures)
    for stream in (waiting.process.stdin, waiting.process.stdout, waiting.process.stderr):
        if stream is not None:
            _try_cleanup(stream.close, failures)
    return {"process_reaped": waiting.process.returncode is not None, "failures": failures}


# LLM: wait 超时或异常都不能当死亡，kill 失败仍独立重试有界 wait；只清理本次直接子进程。
# 函数用途: 回收没有被放行的垫片，保留每项真实清理失败。
def _reap(process, failures: list) -> None:
    try:
        process.wait(timeout=_EXECUTOR_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        _try_cleanup(process.kill, failures)
        _try_cleanup(lambda: process.wait(timeout=_EXECUTOR_WAIT_SECONDS), failures)
    except BaseException as exc:
        failures.append(type(exc).__name__)
        _try_cleanup(process.kill, failures)
        _try_cleanup(lambda: process.wait(timeout=_EXECUTOR_WAIT_SECONDS), failures)


# LLM: 描述符逐个尝试，未知关闭故障写诊断而非跳过其余项；-1 是已关闭句柄的明确哨兵。
# 函数用途: 关闭本次持有的 FD，不泄露后续资源。
def _close_descriptors(descriptors: list, failures: list) -> None:
    for descriptor in descriptors:
        if descriptor >= 0:
            _try_cleanup(lambda fd=descriptor: os.close(fd), failures)


# LLM: 清理不能改变原业务失败或中断，诊断仅存异常类型、不回显命令/凭据。
# 函数用途: 独立尝试一项清理。
def _try_cleanup(operation, failures: list) -> None:
    try:
        operation()
    except BaseException as exc:
        failures.append(type(exc).__name__)
