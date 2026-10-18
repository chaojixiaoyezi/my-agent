# LLM: 裸 FD 号可被控制管道复用；创建前先核验全部源，再持有独立 CLOEXEC 副本，副本只由本次启动关闭。
# 模块用途: 冻结标准流与明确继承的真实描述符对象，不扩大调用方的 FD 白名单。
import os
import subprocess
from dataclasses import dataclass


# LLM: owned 是父端必须关闭的全部副本；allowed 只含显式继承项的副本到原号映射，不包含控制描述符。
# 类用途: 保存待命进程的安全 IO 计划及无效源的拒绝事实。
@dataclass(frozen=True)
class ExecutorDescriptors:
    stdio: tuple
    allowed: tuple
    owned: tuple
    invalid: bool


# LLM: fstat全部发生在首个分配前；优先高位副本，上限边缘用低位空位，但失效源仍沿冻结事实拒绝。
# 函数用途: 创建私有描述符副本；无效项不继承，登记前必须拒绝，任何异常独立回收已建副本。
def pin_descriptors(io) -> ExecutorDescriptors:
    stdio = tuple(_source_descriptor(stream) for stream in io.stdio)
    sources = set(io.pass_fds) | {fd for fd in stdio if fd is not None and fd >= 0}
    identities = {fd: _identity(fd) for fd in sources}
    floor, pins = _pin_floor(sources), {}
    try:
        _pin_sources(identities, floor, pins)
        streams = tuple(pins.get(fd, subprocess.DEVNULL) if fd is not None and fd >= 0 else fd for fd in stdio)
        allowed = tuple((pins[fd], fd) for fd in io.pass_fds if fd in pins)
        return ExecutorDescriptors(streams, allowed, tuple(pins.values()), any(value is None for value in identities.values()))
    except BaseException:
        for descriptor in pins.values():
            os.close(descriptor)
        raise


# LLM: 调用者持有 pins 并负责异常回收；只复制创建前确实有效的源，复制后核对象身份防止源号换物。
# 函数用途: 向本次计划登记源副本，失效项不分配、不加入继承白名单。
def _pin_sources(identities: dict, floor: int, pins: dict) -> None:
    for source, identity in identities.items():
        if identity is None:
            continue
        pins[source] = _pin(source, floor)
        if _identity(pins[source]) != identity:
            raise ValueError("executor_fd_source_changed")


# LLM: 标准流对象只读 fileno；非法类型不补默认流，None/PIPE/DEVNULL/STDOUT 保留 Popen 的明确定义。
# 函数用途: 在分配控制管道前冻结每个标准流的原描述符号。
def _source_descriptor(stream):
    if stream is None or type(stream) is int:
        return stream
    descriptor = stream.fileno()
    if type(descriptor) is not int or descriptor < 0:
        raise ValueError("invalid_executor_stdio")
    return descriptor


# LLM: 只读内核对象身份；已关闭的号明确记为无效，不在之后的管道分配后重新验证裸号。
# 函数用途: 取得用于固定源对象的 fstat 事实。
def _identity(descriptor: int):
    try:
        value = os.fstat(descriptor)
        return value.st_dev, value.st_ino, value.st_mode, value.st_rdev
    except OSError:
        return None


# LLM: F_DUPFD_CLOEXEC原子取得同一内核对象的空位副本；floor可能在资源边缘降低，不覆盖任何打开的源。
# 函数用途: 持有独立可继承计划的源副本，父端回收前不依赖原号是否关闭。
def _pin(descriptor: int, floor: int) -> int:
    import fcntl
    return fcntl.fcntl(descriptor, fcntl.F_DUPFD_CLOEXEC, floor)


# LLM: 软上限之外的floor不能分配，选择标准流之后的空位仍由fcntl保证不覆盖原FD；冻结失效源不重新认领。
# 函数用途: 保留常规高位固定，在资源上限边缘允许合法源使用低位空位副本。
def _pin_floor(sources: set) -> int:
    import resource
    floor = max(sources, default=2) + 1
    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    return floor if soft == resource.RLIM_INFINITY or floor < soft else 3
