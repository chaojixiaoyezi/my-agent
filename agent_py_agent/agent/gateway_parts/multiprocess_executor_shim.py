# LLM: P1a 可信垫片以空环境和 -I -S 独立加载；只有父管道 R 及完整上下文帧才应用命令环境并 exec。
# 模块用途: 等待宿主完成登记，不读取 Home、命令文件或凭据，不打印命令正文。
import json
import os
import resource
import sys

# 上下文帧最多2MiB：覆盖操作系统常见 argv/env 总量，同时避免私有通道无界占内存。
_EXECUTOR_CONTEXT_MAX_BYTES = 2 * 1024 * 1024


# LLM: 只在 R 与完整帧之后应用 argv/cwd/env，PID/内核出生身份不变；异常静默返回126，不回显正文。
# 函数用途: 独立等待登记完成，失败不执行用户命令。
def main() -> int:
    try:
        release, ready = int(sys.argv[1]), int(sys.argv[2])
        inherited = _descriptor_map(json.loads(sys.argv[3]), {release, ready})
        _close_unneeded({0, 1, 2, release, ready, *(fd for fd, _ in inherited)})
        context = _await_release(release, ready)
        if context is None:
            return 125
        _restore_descriptors(inherited)
        os.chdir(context["cwd"])
        os.execvpe(context["argv"][0], context["argv"], context["env"])
    except (OSError, ValueError, IndexError, OverflowError, TypeError, KeyError):
        return 126
    return 126


# LLM: 只有创建前固定的副本能留在待命白名单；副本不与控制端或目标重叠，不把裸号存在当授权。
# 函数用途: 核验宿主提供的安全描述符映射。
def _descriptor_map(mapping, controls: set[int]) -> tuple:
    if type(mapping) is not list or any(type(pair) is not list or len(pair) != 2 for pair in mapping):
        raise ValueError("executor_fd_map_invalid")
    sources, targets = [pair[0] for pair in mapping], [pair[1] for pair in mapping]
    if (any(type(fd) is not int or fd < 3 for fd in sources + targets)
            or len(set(sources)) != len(sources) or len(set(targets)) != len(targets)
            or set(sources) & set(targets) or controls & set(sources)):
        raise ValueError("executor_fd_map_conflict")
    return tuple(tuple(pair) for pair in mapping)


# LLM: 只有 R 和完整帧读完、控制 FD 全关闭后才恢复原号；副本号高于所有原号，dup2 不会踩其它源。
# 函数用途: 保持真实命令的原 FD 数字语义，私有控制端和源副本均不跨 exec。
def _restore_descriptors(mapping: tuple) -> None:
    for source, target in mapping:
        os.dup2(source, target, inheritable=True)
        os.close(source)


# LLM: Popen 已 close_fds=True，这里只用非空区间收窄白名单，防 Linux 关闭控制端；同步核对 shim FD 测试。
# 函数用途: 在等待和真正 exec 前丢弃非必要描述符，保留标准流、批准 FD 和登记控制端。
def _close_unneeded(keep: set[int]) -> None:
    ceiling = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    if ceiling == resource.RLIM_INFINITY:
        ceiling = os.sysconf("SC_OPEN_MAX")
    first = 0
    for descriptor in sorted(keep):
        # Linux close_range 后端的 (0, 0) 末端会下溢成 UINT_MAX，不能传空区间。
        if first < descriptor:
            os.closerange(first, descriptor)
        first = descriptor + 1
    if first < int(ceiling):
        os.closerange(first, int(ceiling))


# LLM: ready仍为W+EOF；先完整验证C帧再阻塞等最后R，准备帧不能执行，EOF/半帧/未知信号失败关闭。
# 函数用途: 收冻结上下文但不应用，只有最后原父管道放行后才交给exec路径。
def _await_release(release: int, ready: int):
    os.set_inheritable(release, False)
    try:
        os.write(ready, b"W")
    finally:
        os.close(ready)
    try:
        if os.read(release, 1) != b"C":
            return None
        length = int.from_bytes(_read_exact(release, 4), "big")
        if not 0 < length <= _EXECUTOR_CONTEXT_MAX_BYTES:
            raise ValueError("executor_context_size")
        context = _validated_context(json.loads(_read_exact(release, length)))
        if os.read(release, 1) != b"R":
            return None
        return context
    finally:
        os.close(release)


# LLM: 管道读允许分片，但 EOF 只能判缺口，不能把部分 JSON 当默认上下文继续启动。
# 函数用途: 有界读取一个已声明长度的片段。
def _read_exact(descriptor: int, length: int) -> bytes:
    content = bytearray()
    while len(content) < length:
        fragment = os.read(descriptor, min(length - len(content), 65536))
        if not fragment:
            raise ValueError("executor_context_incomplete")
        content.extend(fragment)
    return bytes(content)


# LLM: 私有帧仍按结构核验，未知版本/字段不补默认环境，不解析代码，不输出上下文。
# 函数用途: 拒绝不完整或错误类型的命令上下文。
def _validated_context(context) -> dict:
    valid = (type(context) is dict and set(context) == {"version", "argv", "cwd", "env"}
             and type(context["version"]) is int and context["version"] == 1
             and type(context["argv"]) is list and bool(context["argv"])
             and all(type(item) is str and "\x00" not in item for item in context["argv"])
             and bool(context["argv"][0]) and type(context["cwd"]) is str and os.path.isabs(context["cwd"])
             and type(context["env"]) is dict
             and all(type(key) is str and type(value) is str and bool(key) and "=" not in key and "\x00" not in key + value
                     for key, value in context["env"].items()))
    if not valid:
        raise ValueError("executor_context_invalid")
    return context


if __name__ == "__main__":
    raise SystemExit(main())
