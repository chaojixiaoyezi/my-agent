# LLM: G5（Gateway 本机信任第 (1) 层，Linux）：在 exec bwrap 之前用 Landlock 按端口拒绝连本机 Gateway。
#   本模块必须自包含、只用标准库——它既被 sandbox.py 导入做就绪探测，又被当独立脚本 exec（沙箱内 PYTHONPATH 不保证），
#   所以不能有任何本包 import。挂法：父进程声明只管 CONNECT_TCP 的 ruleset、给被拒端口以外的所有端口加允许规则、
#   prctl(NO_NEW_PRIVS)、restrict_self，然后 os.execv(bwrap)。限制随凭据继承进 bwrap 及其所有子进程（ae 车道实测）。
#   不用 subprocess 的 preexec_fn：多线程 Gateway 下文档明说可能死锁，所以走“独立启动器 exec bwrap”。
#   失败语义（和就绪探测分工）：就绪探测在 Gateway 进程里判环境够不够（ABI、架构、bwrap 非 setuid），不够就不用启动器、
#   命令照跑并报 unavailable；启动器一旦被调用就只负责“施加成功再 exec”，任何一步失败都 fail-closed（非零退出、不 exec），
#   绝不在没限制的情况下把命令放进 bwrap。改动须同步 test_landlock_launcher.py 与 docs/design/GATEWAY_LOCAL_TRUST.md 2.2。
# 模块用途: Linux 下给模型命令沙箱套一层 Landlock 端口拒绝，再 exec bwrap；兼作就绪探测的底层系统调用封装。
from __future__ import annotations

import ctypes
import json
import os
import sys

# Landlock 常量（uapi/linux/landlock.h）。
_CREATE_RULESET_VERSION = 1  # landlock_create_ruleset 的 flags：查询 ABI 版本，不建 ruleset
_ACCESS_NET_CONNECT_TCP = 1 << 1
_RULE_NET_PORT_TYPE = 2  # LANDLOCK_RULE_NET_PORT 规则类型选择子（协议身份，非可调数值）
_PRCTL_NO_NEW_PRIVS_CODE = 38  # prctl PR_SET_NO_NEW_PRIVS 操作码（协议身份，非可调数值）

# 系统调用号按架构查表（ae：不认识的架构一律报不支持，绝不拿别的架构的号去调）。
# landlock_create_ruleset / landlock_add_rule / landlock_restrict_self。
_SYSCALLS: dict[str, tuple[int, int, int]] = {
    "x86_64": (444, 445, 446),
    "aarch64": (444, 445, 446),
}


# LLM: Landlock ruleset 属性；只声明管网络 CONNECT_TCP，fs 置 0。ABI 6 在尾部多了 scoped 字段，按本结构的 size 传旧长度
#   内核向后兼容（ae 实测 OK）。
# 类用途: 传给 landlock_create_ruleset 的 handled_access 声明。
class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64)]


# LLM: 单条端口规则；_pack_=1 对齐 uapi。allowed_access 必须与 ruleset 声明的访问位一致。
# 类用途: 传给 landlock_add_rule 的 LANDLOCK_RULE_NET_PORT 规则。
class _NetPortAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("port", ctypes.c_uint64)]


# LLM: 环境不具备 Landlock 网络能力（无 Landlock、LSM 没启用、ABI<4、架构不认识）。就绪探测据此判 unavailable，命令照跑。
# 类用途: 标记“环境没有这个能力”。
class LandlockUnavailable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# LLM: 有能力但这次施加失败（create/add_rule/prctl/restrict_self 报错）。启动器据此 fail-closed，不 exec。
# 类用途: 标记“施加失败”，必须 fail-closed。
class LandlockApplyError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# LLM: 返回当前（或指定）架构的三个系统调用号；不认识的架构返回 None。只读 os.uname，不做系统调用。
# 函数用途: 查 Landlock 系统调用号。
def syscall_numbers(machine: str | None = None) -> tuple[int, int, int] | None:
    return _SYSCALLS.get(machine or os.uname().machine)


# LLM: 查内核 Landlock ABI 版本：landlock_create_ruleset(NULL, 0, VERSION) 只返回版本号、不建 ruleset、不限制本进程，
#   所以在 Gateway 进程里调是安全的。返回负数表示不支持（-errno）。
# 函数用途: 返回内核 Landlock ABI 版本，不支持返回负数。
def landlock_abi(libc: ctypes.CDLL, nums: tuple[int, int, int]) -> int:
    value = libc.syscall(nums[0], None, ctypes.c_size_t(0), ctypes.c_uint32(_CREATE_RULESET_VERSION))
    return value if value >= 0 else -ctypes.get_errno()


# LLM: 进程级施加 CONNECT_TCP 端口白名单：给 0..65535 中不在 blocked 里的端口各加一条允许规则，再 NO_NEW_PRIVS + restrict_self。
#   任何一步失败抛 LandlockApplyError（fail-closed）；环境不具备（create 返回 ENOSYS/EOPNOTSUPP、ABI<4、架构不认识）抛
#   LandlockUnavailable。成功后本进程及其 exec 出来的子进程都只能连未被拒的端口。有副作用：永久限制本进程网络。
# 函数用途: 对本进程施加“拒连指定端口、放行其余”的 Landlock 限制。
def apply_connect_tcp_deny(blocked_ports: set[int], machine: str | None = None) -> None:
    nums = syscall_numbers(machine)
    if nums is None:
        raise LandlockUnavailable("LANDLOCK_UNKNOWN_ARCH")
    libc = ctypes.CDLL(None, use_errno=True)
    if landlock_abi(libc, nums) < 4:
        raise LandlockUnavailable("LANDLOCK_NET_UNSUPPORTED")
    fd = _create_net_ruleset(libc, nums)
    _add_allow_rules(libc, nums, fd, blocked_ports)
    if libc.prctl(_PRCTL_NO_NEW_PRIVS_CODE, 1, 0, 0, 0) != 0:
        raise LandlockApplyError("NO_NEW_PRIVS_FAILED")
    if libc.syscall(nums[2], ctypes.c_int(fd), ctypes.c_uint32(0)) != 0:
        raise LandlockApplyError("RESTRICT_SELF_FAILED")


# LLM: 建只管 CONNECT_TCP 的 ruleset。create 返回 ENOSYS/EOPNOTSUPP 视为环境不具备（unavailable），其它负值是施加失败。
# 函数用途: 创建网络 ruleset 并返回其 fd。
def _create_net_ruleset(libc: ctypes.CDLL, nums: tuple[int, int, int]) -> int:
    attr = _RulesetAttr(0, _ACCESS_NET_CONNECT_TCP)
    fd = libc.syscall(nums[0], ctypes.byref(attr), ctypes.c_size_t(ctypes.sizeof(attr)), ctypes.c_uint32(0))
    if fd >= 0:
        return fd
    err = ctypes.get_errno()
    if err in (errno_enosys(), errno_eopnotsupp()):
        raise LandlockUnavailable("LANDLOCK_NOT_SUPPORTED")
    raise LandlockApplyError(f"CREATE_RULESET_FAILED:{err}")


# LLM: 给被拒端口以外的所有端口（含 0）各加一条允许规则。任一条失败即 fail-closed。ae 车道实测约 0.045 秒。
# 函数用途: 批量加端口允许规则。
def _add_allow_rules(libc: ctypes.CDLL, nums: tuple[int, int, int], fd: int, blocked_ports: set[int]) -> None:
    for port in range(0, 65536):
        if port in blocked_ports:
            continue
        rule = _NetPortAttr(_ACCESS_NET_CONNECT_TCP, port)
        if libc.syscall(nums[1], ctypes.c_int(fd), ctypes.c_int(_RULE_NET_PORT_TYPE),
                        ctypes.byref(rule), ctypes.c_uint32(0)) != 0:
            raise LandlockApplyError(f"ADD_RULE_FAILED:{port}:{ctypes.get_errno()}")


# LLM: errno 值用标准库常量，避免写死（不同平台可能不同）。
# 函数用途: 返回 ENOSYS。
def errno_enosys() -> int:
    import errno
    return errno.ENOSYS


# 函数用途: 返回 EOPNOTSUPP。
def errno_eopnotsupp() -> int:
    import errno
    return errno.EOPNOTSUPP


# 启动器退出码：施加失败与参数不对各用专门码，和被包命令自己的退出码（常见 0/1/2/126/127）区分开，供宿主侧机器识别。
APPLY_FAILED_EXIT_CODE = 97   # Landlock 施加失败 / 环境不具备（本不该走到），fail-closed、未 exec
BAD_ARGV_EXIT_CODE = 96       # 启动器参数不对，fail-closed、未 exec
# fail-closed 时 stderr 第一行固定的结构化标记（宿主 shell 工具据此映射成 GATEWAY_ISOLATION_APPLY_FAILED）。
APPLY_FAILED_ERROR_CODE = "GATEWAY_ISOLATION_APPLY_FAILED"


# LLM: 解析 `--deny p1,p2 -- cmd arg...`：拿到被拒端口集合与要 exec 的命令。作为 fail-closed 组件从严：`--deny` 关键字必须在位、
#   `--` 必须有、被拒端口集合不能为空（空等于什么都不拒）、每个端口必须是 0–65535 的整数；任一不满足返回 (None, [])，由 main fail-closed。
# 函数用途: 从启动器 argv 解析被拒端口和待执行命令，参数不合规返回 (None, [])。
def parse_launcher_argv(argv: list[str]) -> tuple[set[int] | None, list[str]]:
    if len(argv) < 4 or argv[0] != "--deny" or "--" not in argv:
        return None, []
    sep = argv.index("--")
    command = argv[sep + 1:]
    if sep != 2 or not command:
        return None, []
    try:
        ports = {int(p) for p in argv[1].split(",") if p}
    except ValueError:
        return None, []
    if not ports or any(port < 0 or port > 65535 for port in ports):
        return None, []
    return ports, command


# LLM: 启动器入口：施加 Landlock 后 os.execv 命令。参数不对 → BAD_ARGV_EXIT_CODE；施加失败/环境不具备（本不该走到，就绪探测应已拦下）
#   → APPLY_FAILED_EXIT_CODE，并在 stderr 第一行打结构化标记（含 APPLY_FAILED_ERROR_CODE），让宿主 shell 工具把它映射成宿主错误码，
#   而不是和命令自己的退出码混淆；两者都不 exec。成功才 execv（替换进程，限制继承）。只在被当脚本 exec 时调用。有副作用：限制并替换本进程。
# 函数用途: 启动器主流程——施加端口拒绝再 exec 命令，失败不 exec。
def main(argv: list[str]) -> int:
    blocked, command = parse_launcher_argv(argv)
    if blocked is None:
        sys.stderr.write("landlock_launcher: bad argv\n")
        return BAD_ARGV_EXIT_CODE
    try:
        apply_connect_tcp_deny(blocked)
    except (LandlockUnavailable, LandlockApplyError) as exc:
        sys.stderr.write(json.dumps({"error_code": APPLY_FAILED_ERROR_CODE, "reason": exc.reason}) + "\n")
        return APPLY_FAILED_EXIT_CODE
    os.execv(command[0], command)
    return 4  # execv 成功不会到这里


# LLM: 宿主侧用它识别“启动器施加失败”：退出码是 APPLY_FAILED_EXIT_CODE 且 stderr 第一行是带 APPLY_FAILED_ERROR_CODE 的结构化标记。
#   只读返回码和文本，不执行任何东西。shell 工具据此把模型命令结果标成 GATEWAY_ISOLATION_APPLY_FAILED。
# 函数用途: 判断一次被包命令的结果是不是启动器 fail-closed（Landlock 施加失败）。
def is_apply_failed(returncode: int, stderr: str) -> bool:
    if returncode != APPLY_FAILED_EXIT_CODE:
        return False
    first = (stderr or "").splitlines()[0] if stderr else ""
    try:
        return json.loads(first).get("error_code") == APPLY_FAILED_ERROR_CODE
    except (ValueError, AttributeError):
        return False


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
