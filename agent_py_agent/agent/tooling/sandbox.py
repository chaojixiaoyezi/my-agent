
from __future__ import annotations

"""run_command 沙箱(多用户隔离 1 层):用 bubblewrap 把命令隔离进 owner home。

0 层(path_access_policy)拦的是文件【工具】的 path 参数;但 run_command 跑的脚本【内部】
的 open()/rm/cat 绕过应用层检查——这层靠 bwrap 让命令跑在一个【根视图只有自己 owner home
+ 系统只读库】的 mount/pid namespace 里:`rm -rf /` 只删沙箱视图(系统库 ro 删不掉、别人家
根本没 mount)、`ps` 只看到自己进程;**但放行外网**(--share-net + DNS + TLS 证书),外部
API/web/url 正常——这是硬约束,沙箱只隔离文件/进程,不断网。

bwrap 优先用仓库内置(vendor/bin,内网免装)→ 系统 PATH → 都没有则降级(不隔离,记警告,
由调用方决定放行还是拒绝)。bwrap rootless(unprivileged userns)无需 root。
"""

import os
import platform
import shutil
from dataclasses import dataclass, field
from pathlib import Path

# 仓库内置 bwrap(随 my-agent 分发,内网装不了包也自带)。
_VENDOR_BWRAP = Path(__file__).resolve().parents[2] / "vendor" / "bin" / "bwrap.linux-x86_64"

# 命令运行需要的系统只读根(库/工具/证书目录)。只 bind 存在的。
_SYSTEM_RO_ROOTS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc/alternatives")
# 放行外网必须的:DNS 解析 + TLS 证书(https API),否则沙箱内连不上/证书校验失败。
_NETWORK_RO_FILES = (
    "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf",
    "/etc/ssl", "/etc/pki", "/etc/ca-certificates",
)


def find_bwrap() -> str | None:
    """找可用 bwrap:Linux x86_64 优先用仓库内置(免装)→ 系统 PATH → None(降级)。"""
    if platform.system() == "Linux" and platform.machine() in ("x86_64", "amd64"):
        if _VENDOR_BWRAP.exists() and os.access(_VENDOR_BWRAP, os.X_OK):
            return str(_VENDOR_BWRAP)
    return shutil.which("bwrap")


@dataclass(frozen=True)
class SandboxSpec:
    owner_home: Path           # 自己 owner home,沙箱内读写(自己家随便造)
    workspace: Path            # 命令的工作目录(chdir),应在 owner_home 下
    public_ro_roots: tuple[Path, ...] = ()  # 公共只读区(全局 skills 等)
    bwrap_path: str | None = None


def build_bwrap_argv(spec: SandboxSpec) -> list[str]:
    """构造 bwrap argv:文件/进程隔离到 owner home,但放行外网。"""
    bwrap = spec.bwrap_path or find_bwrap()
    if not bwrap:
        raise SandboxUnavailable("bwrap 不可用(仓库内置缺失且系统未装)")
    argv = [
        bwrap,
        "--die-with-parent",   # 父进程死则沙箱死,不留孤儿
        "--unshare-pid",       # 进程隔离:ps 只看到自己
        "--unshare-uts",
        "--unshare-ipc",
        "--share-net",         # ★ 放行外网:不 unshare net,外部 API/web/url 照常
        "--new-session",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",     # 临时,rm -rf /tmp 无害
    ]
    for ro in _SYSTEM_RO_ROOTS:
        if Path(ro).exists():
            argv += ["--ro-bind", ro, ro]
    for ro in _NETWORK_RO_FILES:  # DNS + TLS 证书,外网/https 必需
        if Path(ro).exists():
            argv += ["--ro-bind", ro, ro]
    # 自己 owner home:读写(自己家随便造、删也只删自己的)
    argv += ["--bind", str(spec.owner_home), str(spec.owner_home)]
    # 公共区:只读
    for pub in spec.public_ro_roots:
        if pub.exists() and not _is_relative_to(pub, spec.owner_home):
            argv += ["--ro-bind", str(pub), str(pub)]
    workspace = spec.workspace if _is_relative_to(spec.workspace, spec.owner_home) else spec.owner_home
    argv += ["--chdir", str(workspace)]
    return argv


def wrap_shell_command(command: str, spec: SandboxSpec) -> list[str]:
    """把一条 shell 命令包进 bwrap(经 /bin/sh -c 跑),返回 argv(给 subprocess,不用 shell=True)。"""
    return [*build_bwrap_argv(spec), "--", "/bin/sh", "-c", command]


class SandboxUnavailable(RuntimeError):
    """bwrap 不可用——由调用方决定降级(不隔离放行)还是拒绝。"""


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (ValueError, OSError):
        return False


__all__ = ["SandboxSpec", "SandboxUnavailable", "build_bwrap_argv", "find_bwrap", "wrap_shell_command"]
