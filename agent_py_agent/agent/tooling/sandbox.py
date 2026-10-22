# LLM: 只扩展现有bwrap底图的人格拒写；最终精确限制不可被祖先bind擦除，B7合法重挂仍沿原合同。
# 模块用途: 构造唯一进程隔离参数与readiness，不在此签发新文件、网络或owner权限。

from __future__ import annotations

"""run_command 沙箱(多用户隔离 1 层):用 bubblewrap 把命令隔离进 owner home。

0 层(path_access_policy)拦的是文件【工具】的 path 参数;但 run_command 跑的脚本【内部】
的 open()/rm/cat 绕过应用层检查——这层靠 bwrap 让命令跑在一个【根视图只有自己 owner home
+ 系统只读库】的 mount/pid namespace 里:`rm -rf /` 只删沙箱视图(系统库 ro 删不掉、别人家
根本没 mount)、`ps` 只看到自己进程;**但放行外网**(--share-net + DNS + TLS 证书),外部
API/web/url 正常——这是硬约束,沙箱只隔离文件/进程,不断网。

bwrap 优先使用运行环境安装的兼容版本,再退到仓库内置离线版本。多用户 owner-scoped
命令把这层视为安全硬门:bwrap 缺失或自检失败时必须拒绝执行,不允许降级宿主 shell。
"""

import argparse
import json
import os
import platform
import shlex
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from ..path_access_policy import (
    HOST_STATE_LOCK_SUFFIX,
    SQLITE_SIDECAR_SUFFIXES,
    AgentsSourceProtectionError,
    persona_process_protection_snapshot,
    persona_sandbox_root,
    validate_persona_configuration,
    validate_persona_source_identity,
)
from .write_boundary import ordered_write_scope_roots

# 仓库内置 bwrap(随 my-agent 分发,内网装不了包也自带)。
_VENDOR_BWRAP = Path(__file__).resolve().parents[2] / "vendor" / "bin" / "bwrap.linux-x86_64"

# 命令运行需要的系统只读根(库/工具/证书目录)。只 bind 存在的。
_SYSTEM_RO_ROOTS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc/alternatives")

# B7 allowlist 底图按宿主平台列出最小系统依赖目录；不以根目录、用户目录或 private 别名兜底。
SYSTEM_READ_ROOTS_BY_PLATFORM: dict[str, tuple[str, ...]] = {
    "Linux": (
        "/bin", "/sbin", "/lib", "/lib64", "/usr/bin", "/usr/sbin",
        "/usr/lib", "/usr/lib64", "/usr/share", "/etc/alternatives",
    ),
    "Darwin": ("/System/Library", "/usr/bin", "/usr/lib", "/usr/share", "/bin", "/sbin"),
}

# /proc 挂载方式探测结果缓存: "proc"=真实 procfs 可用, "dir"=必须空目录, None=未探测。
_PROC_MOUNT_KIND: str | None = None


def _proc_mount_args() -> list[str]:
    """决定沙箱 /proc 挂载方式。

    真机(或有 mount 权限的容器)挂真实 procfs:`/proc/self/exe` 等自省路径对
    go/python 等发行版(trimmed)二进制成立——它们靠 /proc/self/exe 推导自身根目录
    (实测 测试机: bwrap 空 /proc 里 `go version` 报 "binary is trimmed and GOROOT
    is not set", --proc 挂真实 procfs 后正常)。挂载后立即 --remount-ro 锁只读
    (Docker 同款):/proc 只供读取,防改 /proc/sys 内核全局参数;进程隔离不变
    (bwrap 挂的是新 pid namespace 里的 procfs, 宿主进程列表照样不可见)。
    嵌套容器(Docker Desktop/K8s hardened runtime)内核常拒 mount("proc"), 回退旧行为
    `--dir /proc`(空目录, 进程隔离仍在)。
    """
    global _PROC_MOUNT_KIND
    if _PROC_MOUNT_KIND is None:
        bwrap = find_bwrap()
        _PROC_MOUNT_KIND = "dir"
        if bwrap:
            # 探测挂载与真实沙箱一致(_SYSTEM_RO_ROOTS):动态链接二进制(env/sh)的
            # interpreter /lib64/ld-linux-x86-64.so.2 独立于 /usr,不挂就 execvp 失败,
            # 会误判成"内核拒 mount(proc)"(实测 RHEL 系 测试机)。
            probe_args = [bwrap, "--unshare-pid", "--proc", "/proc"]
            for ro in _SYSTEM_RO_ROOTS:
                if Path(ro).exists():
                    probe_args += ["--ro-bind", ro, ro]
            probe_args += ["--chdir", "/", "--", "/usr/bin/env", "true"]
            try:
                probe = subprocess.run(
                    probe_args,
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                probe = None
            if probe is not None and probe.returncode == 0:
                _PROC_MOUNT_KIND = "proc"
    if _PROC_MOUNT_KIND == "proc":
        # 挂完立即锁只读(Docker 同款):/proc 只供读取(/proc/self/exe 自省等),
        # 防沙箱内进程改 /proc/sys 等内核全局参数;只读不影响 go/python 自省。
        return ["--proc", "/proc", "--remount-ro", "/proc"]
    return ["--dir", "/proc"]
# 放行外网必须的:DNS 解析 + TLS 证书(https API),否则沙箱内连不上/证书校验失败。
_NETWORK_RO_FILES = (
    "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf",
    "/etc/ssl", "/etc/pki", "/etc/ca-certificates",
)


# LLM: 这是多用户 shell 隔离二进制的唯一发现入口；生产镜像优先使用系统包版本，
#   vendor binary 只作同架构离线兜底。不要在调用方重复平台判断或另造发现逻辑。
# 函数用途: 在 Linux 执行节点寻找可运行的 bwrap 文件；其他平台明确返回不可用。
def find_bwrap() -> str | None:
    """找可用 bwrap:Linux 系统包优先→同架构内置版本→None。"""
    if platform.system() != "Linux":
        return None
    system_bwrap = shutil.which("bwrap")
    if system_bwrap and os.access(system_bwrap, os.X_OK):
        return system_bwrap
    if platform.machine() in ("x86_64", "amd64"):
        if _VENDOR_BWRAP.exists() and os.access(_VENDOR_BWRAP, os.X_OK):
            return str(_VENDOR_BWRAP)
    return None


# LLM: 这是镜像构建、worker 启动、K8s readiness 共用的结构化 sandbox 事实。
#   code 是机器裁决字段，detail 只作人类诊断；不要解析 detail 决定是否放行。
# 类用途: 保存 bwrap 自检是否通过、失败代码、二进制版本和已完成检查项。
@dataclass(frozen=True)
class SandboxReadiness:
    ready: bool
    code: str
    detail: str
    bwrap_path: str = ""
    version: str = ""
    checks: tuple[str, ...] = ()

    # LLM: 序列化字段供 CLI/K8s/doctor 消费，字段名属于稳定诊断协议。
    # 函数用途: 把自检结论转成可输出的 JSON 字典。
    def to_dict(self) -> dict[str, object]:
        return asdict(self)


# LLM: SandboxSpec 是唯一 bwrap 挂载合同，workspace 必须已由 PathAccessPolicy 裁决；allowlist 不挂根文件系统。
#   新 system_read_roots 只承载平台底图，不能替代 public_ro_roots 的宿主授权事实。
# 类用途: 描述已裁决工作目录、读模式、系统读根和本轮明确可写目录。
@dataclass(frozen=True)
class SandboxSpec:
    owner_home: Path           # 自己 owner home；默认读写，任务边界存在时改为只读底图
    workspace: Path            # 命令的工作目录(chdir),应在 owner_home 下
    public_ro_roots: tuple[Path, ...] = ()  # 公共只读区(全局 skills 等)
    # None 保留普通 owner shell 的“自己家可写”语义；显式 tuple 表示本轮存在结构化
    # write boundary，此时 owner home 只读，只有这些根以嵌套 rw bind 重新开放。
    write_roots: tuple[Path, ...] | None = None
    bwrap_path: str | None = None
    protected_persona_root: Path | None = None
    # Host-authored control metadata remains read-only even when a broader parent root is writable.
    read_only_paths: tuple[Path, ...] = ()
    write_scope_denied_roots: tuple[Path, ...] = ()
    full_access: bool = False
    # Most owner commands intentionally retain network access.  Pure helper
    # programs (for example a learned source request/response adapter) can opt
    # into a network namespace with no host interface instead of growing a
    # second sandbox implementation.
    network_access: bool = True
    # 整根只读形态：根文件系统先只读挂载，hidden_paths 可再覆盖隐藏区域并只重挂授权读根；write_roots 单独可写。
    # 默认 False 时两种既有形态的 argv 逐字节不变。
    read_only_root: bool = False
    # B7 插件只读根模式；None/hide_home 保持旧实现，allowlist 从空根构造有限视图。
    read_mode: str | None = None
    # allowlist 模式下，宿主平台所需的系统只读根；与用户授权根分开投影。
    system_read_roots: tuple[Path, ...] = ()
    # B7：收窄读形态只挂真实目标，再用 --symlink 恢复宿主已核验的入口路径。
    preserve_public_read_root_aliases: bool = False
    # H2：最后以只读空 tmpfs 隐藏宿主目录；模型命令隐藏托管存储，G1 整根只读插件形态隐藏宿主凭据 secrets 目录。
    hidden_paths: tuple[Path, ...] = ()


# LLM: 这是 bwrap 文件系统/进程隔离策略的唯一 argv 构造点。owner_home 和已授权
#   workspace 是仅有的可写挂载；新增挂载必须同步安全测试和设计文档。
# 函数用途: 根据用户目录和本次工作目录构造 bubblewrap 启动参数。
def _build_bwrap_base_argv(spec: SandboxSpec) -> list[str]:
    """构造 bwrap argv:文件/进程隔离到 owner home,但放行外网。"""
    bwrap = spec.bwrap_path or find_bwrap()
    if not bwrap:
        raise SandboxUnavailable("bwrap 不可用(仓库内置缺失且系统未装)")
    if spec.read_mode not in (None, "hide_home", "allowlist"):
        raise SandboxUnavailable("SANDBOX_UNAVAILABLE: RESTRICTED_READ_MODE_INVALID")
    if spec.read_mode == "allowlist":
        if not spec.read_only_root or spec.full_access or spec.write_roots is None:
            raise SandboxUnavailable("SANDBOX_UNAVAILABLE: ALLOWLIST_SCOPE_INVALID")
        return _allowlist_root_argv(bwrap, spec)
    if spec.read_only_root:
        return _read_only_root_argv(bwrap, spec)
    if spec.full_access:
        argv = [
            bwrap,
            "--die-with-parent",
            "--new-session",
            "--share-net" if spec.network_access else "--unshare-net",
            "--bind",
            "/",
            "/",
            # hardened Linux 上，根目录 bind 会继承 bwrap 的 nodev 语义；必须在其后
            # 显式重新挂真实设备树，否则 Full Access 连 /dev/null 都打不开，pytest
            # 和 subprocess capture 会报 EACCES。会话运行时 也在根挂载后重挂 /dev；本项
            # 使用 --dev-bind，才能在 SELinux enforcing 的 RHEL 系测试镜像保留设备能力。
            "--dev-bind",
            "/dev",
            "/dev",
        ]
        if spec.write_scope_denied_roots:
            _append_write_scope_mounts(argv, _normalized_write_roots(spec.write_roots, preserve_nested=True), spec.write_scope_denied_roots)
        _append_readonly_mounts(argv, spec.read_only_paths)
        _append_hidden_mounts(argv, spec.hidden_paths)
        argv += ["--chdir", str(spec.workspace)]
        return argv
    argv = [
        bwrap,
        "--die-with-parent",   # 父进程死则沙箱死,不留孤儿
        "--unshare-pid",       # 进程隔离:ps 只看到自己
        "--unshare-uts",
        "--unshare-ipc",
        "--share-net" if spec.network_access else "--unshare-net",
        "--new-session",
        # 优先挂真实 procfs(go/python 等靠 /proc/self/exe 推导根目录的 trimmed 二进制必需);
        # 嵌套容器内核拒 mount("proc") 时回退空 /proc 目录(旧行为,进程隔离仍在)。
        *_proc_mount_args(),
        "--dev", "/dev",
    ]
    # 持久 /tmp：owner-scoped 沙箱把临时目录 bind 到任务工作区的隐藏目录，
    # 模型下载的源码/依赖跨命令、跨请求保留。tmpfs 时 pip download 成功但
    # 下一请求源码消失，模型不知道丢了还谎称源码已备好（真机铁证 2026-08-08
    # celery/scrapy→Go 复刻）。rm -rf /tmp 只清任务区 .sandbox-tmp，
    # 沙箱隔离与写边界不变。
    # SANDBOX-01/02 真机: tmp 根必须落在任务本地目录——此前用 spec.workspace
    # (项目目录)导致①项目目录被 .sandbox-tmp 污染②任务收口无从发布产物。
    # process boundary 只在 canonical task_work_dir 属于 allowed_write_roots 时把它
    # 排在首位；Attempt sandbox 必须保留该顺序，再追加当前 cwd/staging。
    # 因而 write_roots[0] 是任务 work，而不是更深的项目目录或 output。
    # 无 write_roots(单租户 full_access)时保持 workspace 目录(旧行为, /tmp 即宿主)。
    if spec.write_roots:
        tmp_root = Path(spec.write_roots[0]) / ".sandbox-tmp"
    elif spec.write_roots is not None:
        # Explicit empty write scope means cwd is read-only. Framework-owned owner/tmp remains
        # the only scratch mount; never create .sandbox-tmp inside the read-only source tree.
        tmp_root = Path(spec.owner_home) / "tmp" / ".sandbox-tmp"
    else:
        tmp_root = spec.workspace / ".sandbox-tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    argv += ["--bind", str(tmp_root), "/tmp"]
    for ro in _SYSTEM_RO_ROOTS:
        if Path(ro).exists():
            argv += ["--ro-bind", ro, ro]
    for ro in _NETWORK_RO_FILES:  # DNS + TLS 证书,外网/https 必需
        if Path(ro).exists():
            argv += ["--ro-bind", ro, ro]
    write_roots = _normalized_write_roots(spec.write_roots, preserve_nested=bool(spec.write_scope_denied_roots))
    read_roots = _normalized_read_roots(
        spec.public_ro_roots, spec.preserve_public_read_root_aliases
    )
    # 会话运行时 的 split filesystem policy 同样先给进程可读根，再只对明确 writable roots
    # 开写权限。顺序不能反：read root 可以是 write root 的父目录。
    for root in read_roots:
        if root.exists() and not _is_relative_to(root, spec.owner_home):
            argv += ["--ro-bind", str(root), str(root)]
    if spec.write_roots is None:
        # 普通 owner shell 没有更窄的任务合同：自己家仍可读写。
        argv += ["--bind", str(spec.owner_home), str(spec.owner_home)]
    else:
        # 子代理/受约束任务：owner home 只是输入底图。先整体只读，再把结构化授权根
        # 逐个覆盖成可写，shell 重定向、脚本 open()、cp/mv 都无法绕过文件工具门。
        argv += ["--ro-bind", str(spec.owner_home), str(spec.owner_home)]
    # 工作区可能在 owner home 外(例如容器把当前项目挂到 /workspace)。target 已经过
    # PathAccessPolicy 裁决；如果它已在只读根里就不重复挂载。
    if not _is_relative_to(spec.workspace, spec.owner_home):
        writable_workspace = spec.write_roots is None or any(
            _is_relative_to(spec.workspace, root) for root in write_roots
        )
        visible_from_read_root = any(
            _is_relative_to(spec.workspace, root) for root in read_roots
        )
        if not visible_from_read_root:
            argv += [
                "--bind" if writable_workspace else "--ro-bind",
                str(spec.workspace),
                str(spec.workspace),
            ]
    # 最后覆盖结构化写根。这样只读源码树里可以有一个明确获批的构建/输出子目录，
    # 而不会把整个只读父目录升级为可写。
    _append_write_scope_mounts(argv, write_roots, spec.write_scope_denied_roots)
    # Broad owner-home write access must not include host-authoritative policy and ledgers.
    # Apply these mounts after writable roots so the more specific read-only paths win.
    _append_readonly_mounts(argv, spec.read_only_paths)
    # 无论 owner home 当前是 rw 还是 ro，都把需要真人确认的长期人格文件明确覆盖成
    # 只读挂载。update_persona 在宿主进程执行，不走 shell，确认后的正式写入仍可完成。
    _append_hidden_mounts(argv, spec.hidden_paths)
    argv += ["--chdir", str(spec.workspace)]
    return argv


# LLM: allowlist 不得隐式重开 cwd；插件规格工厂与 Attempt 同步 run 共用这个真实路径根覆盖判定，不按字符串前缀放行兄弟目录。
#   单个根解析失败（启动前被删、权限变化）只当它不覆盖 cwd，由调用方给结构化拒绝，不抛未处理异常（3a 终审补）。
# 函数用途: 判断工作目录是否已经包含在系统、只读授权或写根中，只读文件系统事实，不新授权限。
def allowlisted_directory(path: Path, roots: tuple[Path, ...]) -> bool:
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    return any(_is_relative_to(resolved, root) for root in _resolved_existing_roots(roots))


# LLM: 只做 strict 解析并跳过解析失败的根；不放宽任何根，结果只会让覆盖判定更严。
# 函数用途: 把根列表解析成真实路径，删掉已不存在或无法解析的根。
def _resolved_existing_roots(roots: tuple[Path, ...]) -> tuple[Path, ...]:
    resolved: list[Path] = []
    for root in roots:
        try:
            resolved.append(Path(root).expanduser().resolve(strict=True))
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
    return tuple(resolved)


# LLM: allowlist 从空 tmpfs 根搭视图，不挂宿主 /；只读系统/授权根与显式写根分层覆盖，别名只恢复获准入口。
# 函数用途: 构造 B7 有限读 bwrap 视图，未列出的宿主目录不会进入插件进程。
def _allowlist_root_argv(bwrap: str, spec: SandboxSpec) -> list[str]:
    if spec.write_roots is None or not spec.system_read_roots:
        raise SandboxUnavailable("SANDBOX_UNAVAILABLE: ALLOWLIST_ROOTS_UNAVAILABLE")
    mount_roots = (*spec.system_read_roots, *spec.public_ro_roots,
                   *spec.write_roots, *spec.hidden_paths, *spec.read_only_paths)
    argv = [
        bwrap, "--die-with-parent", "--unshare-pid", "--unshare-uts", "--unshare-ipc",
        "--share-net" if spec.network_access else "--unshare-net", "--new-session", "--tmpfs", "/",
    ]
    _append_allowlist_scaffold(argv, mount_roots)
    argv += ["--dev", "/dev", *_proc_mount_args()]
    _append_allowlist_system_mounts(argv, spec.system_read_roots)
    for root in _preserved_read_roots(spec.public_ro_roots):
        if root.exists():
            argv += ["--ro-bind", str(root), str(root)]
    _append_write_scope_mounts(argv, _normalized_write_roots(spec.write_roots, preserve_nested=bool(spec.write_scope_denied_roots)), spec.write_scope_denied_roots)
    _append_allowlist_symlink_aliases(argv, spec)
    _append_hidden_mounts(argv, spec.hidden_paths)
    _append_readonly_mounts(argv, spec.read_only_paths)
    argv += ["--remount-ro", "/", "--chdir", str(spec.workspace)]
    return argv


# LLM: 空根没有宿主祖先目录；先创建绑定点和 /dev、/proc、/tmp 的骨架，内容仍由后续精确挂载决定。
# 函数用途: 为有限读 bwrap 挂载准备最少的目录入口，不复制未授权父目录内容。
def _append_allowlist_scaffold(argv: list[str], roots: tuple[Path, ...]) -> None:
    directories = {Path("/dev"), Path("/proc"), Path("/tmp")}
    for raw in roots:
        path = Path(raw).expanduser().absolute()
        target = path if path.is_dir() else path.parent
        directories.add(target)
        directories.update(target.parents)
    for directory in sorted(directories, key=lambda item: (len(item.parts), str(item))):
        if directory != Path("/"):
            argv += ["--dir", str(directory)]


# LLM: 系统目录 symlink 以已解析来源挂到空 root 的原始位置和真实位置，避免把符号链接当作 mount destination。
# 函数用途: 将平台系统根挂为只读，并保留系统命令使用的标准目录别名。
def _append_allowlist_system_mounts(argv: list[str], roots: tuple[Path, ...]) -> None:
    mounts: list[tuple[Path, Path]] = []
    for raw in roots:
        original = Path(raw).expanduser().absolute()
        try:
            resolved = original.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not resolved.is_dir():
            continue
        mounts.extend(((resolved, resolved), (resolved, original)))
    for source, target in dict.fromkeys(mounts):
        argv += ["--ro-bind", str(source), str(target)]


# LLM: root 已按 realpath 只读挂载时，只在更宽的已挂目录未包含别名的情况下补 --symlink，避免覆盖系统目录自带入口。
# 函数用途: 恢复显式 R/W/E symlink 根，同时不重复创建已由系统或授权父目录带入的别名。
def _append_allowlist_symlink_aliases(argv: list[str], spec: SandboxSpec) -> None:
    roots = (*spec.public_ro_roots, *(spec.write_roots or ()))
    mounted_directories = [
        root for root in (*spec.system_read_roots, *_preserved_read_roots(spec.public_ro_roots),
                          *_normalized_write_roots(spec.write_roots))
        if Path(root).is_dir()
    ]
    for target, alias in _symlink_root_aliases(roots):
        alias_path = Path(alias).expanduser().absolute()
        if any(_lexically_within(alias_path, Path(root).expanduser().absolute()) for root in mounted_directories):
            continue
        argv += ["--symlink", str(target), str(alias)]


# LLM: 只比较POSIX词法字符串，不resolve别名，不通过高频ValueError判断普通不包含。
# 函数用途: 快速判断挂载路径层级，区分兄弟前缀、相对路径和双斜杠根。
def _lexically_within(path: Path, root: Path) -> bool:
    sp, sr = str(path), str(root)
    if sr == '/':
        return sp.startswith('/') and not sp.startswith('//')
    if sr == '//':
        return sp.startswith('//')
    if sr == '.':
        return not path.is_absolute()
    return sp == sr or sp.startswith(sr + '/')


# LLM: 整根只读后再挂新的 /dev、/proc，最后逐个可写绑定 write_roots；受保护只读路径最后覆盖。不挂私有 /tmp：
#   owner home 或工作区可能就在 /tmp 下（测试与部分部署），tmpfs 会把它们整个盖住；需要临时文件的进程应由调用方把
#   TMPDIR 指到某个写根里。不收窄读范围、不隔离网络，只防改写——这是插件进程试点的边界，不能替代 owner 墙形态。
# 函数用途: 构造"整个文件系统只读、个别目录可写"的 bwrap 参数。
def _read_only_root_argv(bwrap: str, spec: SandboxSpec) -> list[str]:
    argv = [
        bwrap, "--die-with-parent", "--unshare-pid", "--unshare-uts", "--unshare-ipc",
        "--share-net" if spec.network_access else "--unshare-net", "--new-session",
        "--ro-bind", "/", "/", "--dev", "/dev", *_proc_mount_args(),
    ]
    _append_hidden_root_remounts(argv, spec)
    _append_write_scope_mounts(argv, _normalized_write_roots(spec.write_roots, preserve_nested=bool(spec.write_scope_denied_roots)), spec.write_scope_denied_roots)
    _append_symlink_aliases(argv, spec)
    _append_readonly_mounts(argv, spec.read_only_paths)
    argv += ["--chdir", str(spec.workspace)]
    return argv


# LLM: B7 hide_home 保留原行为：private_read_roots 并入 hidden_paths 后先 tmpfs 覆盖，再挂回工作目录与授权读根；
#   符号链接别名以 --symlink 重建，写根仍按原权限挂回。allowlist 独立走 _allowlist_root_argv，不修改本路径。
# 函数用途: 在 hide_home 的整根只读视图里隐藏指定根，再恢复插件工作目录和授权读根。
def _append_hidden_root_remounts(argv: list[str], spec: SandboxSpec) -> None:
    hidden = _normalized_read_roots(spec.hidden_paths)
    if not hidden:
        return
    for root in hidden:
        if root.is_dir():
            argv += ["--tmpfs", str(root)]
    reexpose = (spec.workspace, *_normalized_read_roots(
        spec.public_ro_roots, spec.preserve_public_read_root_aliases
    ))
    for path in reexpose:
        if path.exists():
            argv += ["--ro-bind", str(path), str(path)]


# LLM: 有祖先禁止规则时不得合并窄授权，否则编译后会失去覆盖该禁止的事实；无禁止时保留原简化。
# 函数用途: 规范化 Linux 写挂载根，按实际边界决定是否保留父子根。
def _normalized_write_roots(raw_roots: tuple[Path, ...] | None, *, preserve_nested: bool = False) -> tuple[Path, ...]:
    if raw_roots is None:
        return ()
    roots: list[Path] = []
    for raw in raw_roots:
        try:
            root = Path(raw).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        if preserve_nested:
            if root not in roots:
                roots.append(root)
            continue
        # 已开放父根时不重复挂子根；遇到更宽父根则替换已有子根。
        if any(_is_relative_to(root, existing) for existing in roots):
            continue
        roots = [existing for existing in roots if not _is_relative_to(existing, root)]
        roots.append(root)
    return tuple(roots)


def _normalized_read_roots(
    raw_roots: tuple[Path, ...], preserve_aliases: bool = False
) -> tuple[Path, ...]:
    # LLM: 标准 shell 读根按 realpath 合并；收窄读保留每个授权根的真实目标，别名由统一 symlink helper 重建。
    # 函数用途: 规范只读挂载根，并在受限形态下避免把别名当作挂载目标。
    if preserve_aliases:
        return _preserved_read_roots(raw_roots)
    return _canonical_read_roots(raw_roots)


# LLM: symlink destination 不能作为 bwrap bind mount 目标；收窄读只挂 strict realpath，入口另用 --symlink 恢复。
# 函数用途: 得到不包含符号链接目的地的受限只读挂载根。
def _preserved_read_roots(raw_roots: tuple[Path, ...]) -> tuple[Path, ...]:
    roots: list[Path] = []
    for raw in raw_roots:
        try:
            resolved = Path(raw).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        roots = list(dict.fromkeys((*roots, resolved)))
    return tuple(roots)


# LLM: 仅恢复明确授权根；途中目录含软链接时，即使终端文件不是 symlink 也须恢复原始入口。
#   直接指向同一获准 realpath 绕过中间目录链，不挂父目录或兄弟数据，不推导新读根。
# 函数用途: 找出授权根真实目标与原始入口，不再仅检查最后一个节点是否为软链接。
def _symlink_root_aliases(raw_roots: tuple[Path, ...]) -> tuple[tuple[Path, Path], ...]:
    aliases: list[tuple[Path, Path]] = []
    for raw in raw_roots:
        try:
            alias = Path(raw).expanduser().absolute()
            target = alias.resolve(strict=True)
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
        if alias != target and (target, alias) not in aliases:
            aliases.append((target, alias))
    return tuple(aliases)


# LLM: --symlink 只补回 policy 明确授权的入口；必须在目标根挂载后、最后只读覆盖前执行。
# 函数用途: 在收窄读 bwrap 视图里恢复 R/W/E 根的 symlink 名称，不把别名父目录挂进沙箱。
def _append_symlink_aliases(argv: list[str], spec: SandboxSpec) -> None:
    if not spec.preserve_public_read_root_aliases:
        return
    roots = (*spec.public_ro_roots, *(spec.write_roots or ()))
    for target, alias in _symlink_root_aliases(roots):
        argv += ["--symlink", str(target), str(alias)]


# LLM: 非收窄读调用沿用 realpath 祖先合并，保持已有 bwrap shell 挂载集合不变。
# 函数用途: 合并普通只读根中的重复路径与子根。
def _canonical_read_roots(raw_roots: tuple[Path, ...]) -> tuple[Path, ...]:
    roots: list[Path] = []
    for raw in raw_roots:
        try:
            root = Path(raw).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        if any(_is_relative_to(root, existing) for existing in roots):
            continue
        roots = [existing for existing in roots if not _is_relative_to(existing, root)]
        roots.append(root)
    return tuple(roots)




# LLM: 共享规则排序与文件工具一致，普通祖先只读后可以重挂正式窄写根；绝对保护由调用方随后追加。只投影已有路径，不创建宿主文件。
# 函数用途: 把本次允许与禁止范围按路径具体程度编译成 Linux 挂载顺序。
def _append_write_scope_mounts(
    argv: list[str], allowed: tuple[Path, ...], forbidden: tuple[Path, ...],
) -> None:
    for root, writable in ordered_write_scope_roots(allowed, forbidden):
        if root.exists():
            argv += ["--bind" if writable else "--ro-bind", str(root), str(root)]


# LLM: 只按宿主策略的瞬时后缀判定；try-bind仅容忍源在exec前消失，存在时仍必须只读，不扩大持久路径例外。
# 函数用途: 识别SQLite侧车和宿主锁文件，避免删除竞态中止整个bwrap启动。
def _is_ephemeral_readonly_sidecar(path: Path) -> bool:
    return path.name.endswith((*SQLITE_SIDECAR_SUFFIXES, HOST_STATE_LOCK_SUFFIX))


# LLM: 仅识别landlock_launcher前置包装后的首个bwrap命令分隔符，不把用户命令内部的--当成挂载区结尾。
# 函数用途: 返回bwrap挂载选项的扫描上界下标，隔离--之后的用户命令参数。
def _bwrap_mount_limit(argv: list[str]) -> int:
    if "--" not in argv:
        return len(argv)
    first = argv.index("--")
    start = first + 1 if any("landlock_launcher" in str(item) for item in argv[:first]) else 0
    return argv.index("--", start) if "--" in argv[start:] else len(argv)


# LLM: 只裁剪stderr命中的瞬时try-bind及同时消失的侧车，不动硬RO/用户命令；调用方还须守住无stdout、失败码及重试上限。
# 函数用途: 返回可供有界重试的侧车挂载参数；未确认是瞬时侧车错误时不重试。
def prune_transient_bwrap_sidecar_mounts(argv: list[str], stderr: str) -> list[str] | None:
    if not any(marker in stderr for marker in ("bwrap: Can't bind mount ", "bwrap: Can't find source path ")):
        return None
    limit = _bwrap_mount_limit(argv)
    if any("bwrap:" in str(item) for item in argv[limit + 1:]):
        return None
    pruned: list[str] = []
    matched_stderr = False
    index = 0
    while index < limit:
        if index + 2 < limit and argv[index] == '--ro-bind-try':
            source, target = argv[index + 1:index + 3]
            if _is_ephemeral_readonly_sidecar(Path(source)):
                matched = source in stderr or target in stderr
                matched_stderr = matched_stderr or matched
                if matched or not Path(source).exists():
                    index += 3
                    continue
        pruned.append(argv[index])
        index += 1
    pruned.extend(argv[limit:])
    return pruned if matched_stderr and len(pruned) < len(argv) else None


# LLM: 缺失项快跳；瞬时侧车try-bind容忍exec前删除，持久路径仍硬RO；列表追加同次投影，有效同源硬RO祖先才覆盖去重，不跨调用缓存权限。
# 函数用途: 将存在的绝对保护路径只读覆盖到沙箱，省略精确或祖先仍生效的重复挂载。
def _append_readonly_mounts(argv: list[str], paths: tuple[Path, ...]) -> None:
    seen: set[Path] = set()
    mounts: list[_MountProjection] = list(_mount_snapshot(argv))
    for raw in paths:
        try:
            path = Path(raw).expanduser()
            if not path.exists() and not path.is_symlink():
                continue
            path = path.resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        if path in seen or not path.exists():
            continue
        seen.add(path)
        flag = '--ro-bind-try' if _is_ephemeral_readonly_sidecar(path) else '--ro-bind'
        arguments = (flag, str(path), str(path))
        if _readonly_mount_unchanged(path, arguments, mounts):
            continue
        argv.extend(arguments)
        mounts.append(_MountProjection(path, path, arguments=arguments))


# LLM: 只去重精确原参或同源硬RO祖先；逆序遇首个交叠后停止，后发RW、异源RO、alias或空覆盖均不能证明子树未变。
# 函数用途: 核对精确或祖先覆盖是否仍生效，避免冗余挂载又不漏掉需要重新加固的子树。
def _readonly_mount_unchanged(path: Path, arguments: tuple[str, str, str], mounts: Sequence[_MountProjection]) -> bool:
    for mount in reversed(mounts):
        if _lexically_within(mount.target, path) or _lexically_within(path, mount.target):
            if mount.target == path and mount.arguments == arguments:
                return True
            if (arguments == ("--ro-bind", str(path), str(path))
                    and _lexically_within(path, mount.target)
                    and not mount.writable and not mount.alias
                    and mount.arguments == ("--ro-bind", str(mount.target), str(mount.target))):
                return True
            return False
    return False


# LLM: H2：宿主托管存储对模型命令隐藏。只盖已存在的目录（宿主按 owner 布局列出），排在所有挂载之后才能压过可写父目录；
#   --tmpfs 换成空目录、--remount-ro 让它只读，写进去也不会落到真实存储。改动须同步 test_host_managed_store_access.py。
# 函数用途: 在 bwrap 参数末尾用只读空目录盖住要隐藏的宿主托管存储。
def _append_hidden_mounts(argv: list[str], paths: tuple[Path, ...]) -> None:
    seen: set[Path] = set()
    for raw in paths:
        path = Path(raw).resolve(strict=False)
        if path in seen or not path.is_dir():
            continue
        seen.add(path)
        argv += ["--tmpfs", str(path), "--remount-ro", str(path)]


# LLM: 所有 POSIX shell 命令都必须经此入口，保证管道任一阶段失败会成为命令失败。
# 函数用途: 找到 bash 并构造开启 pipefail 的 argv；缺失时明确拒绝而非静默退回 /bin/sh。
def strict_posix_shell_argv(command: str) -> list[str]:
    """构造严格 POSIX shell argv，避免 ``pytest | tail`` 被末段退出码伪装成成功。"""
    bash = next(
        (candidate for candidate in ("/bin/bash", "/usr/bin/bash") if Path(candidate).is_file()),
        shutil.which("bash"),
    )
    if not bash:
        raise SandboxUnavailable("STRICT_SHELL_NOT_FOUND:需要 bash -o pipefail")
    return [bash, "-o", "pipefail", "-c", command]


# LLM: ShellTool 前后台执行都必须经这个包装入口，返回 argv 后调用方必须 shell=False。
# 函数用途: 把用户命令包装成在 bwrap 中执行的严格 bash 命令。
def wrap_shell_command(command: str, spec: SandboxSpec) -> list[str]:
    """把命令包进 bwrap，并让任一管道阶段失败都返回非零。"""
    return [*build_bwrap_argv(spec), "--", *strict_posix_shell_argv(command)]


# LLM: owner-scoped shell 必须把此异常转换为 SANDBOX_UNAVAILABLE，禁止捕获后直跑。
# 类用途: 表示节点缺少可用隔离能力，调用方应拒绝本次命令或阻止 worker 启动。
class SandboxUnavailable(RuntimeError):
    """bwrap 不可用或未通过安全自检；owner-scoped 调用方必须拒绝执行。"""


# LLM: 版本读取只证明二进制可加载；full probe 还必须执行 namespace/mount 行为验证。
# 函数用途: 执行 bwrap --version，并把加载失败转换成结构化 readiness。
def _probe_binary(bwrap: str, *, timeout: float) -> SandboxReadiness:
    try:
        result = subprocess.run(
            [bwrap, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return SandboxReadiness(
            False,
            "BWRAP_BINARY_UNUSABLE",
            f"bwrap 二进制无法启动:{exc}",
            bwrap_path=bwrap,
        )
    version = (result.stdout or result.stderr).strip()
    if result.returncode != 0:
        return SandboxReadiness(
            False,
            "BWRAP_VERSION_FAILED",
            f"bwrap --version 退出码 {result.returncode}",
            bwrap_path=bwrap,
            version=version,
        )
    return SandboxReadiness(
        True,
        "SANDBOX_BINARY_READY",
        "bwrap 二进制可启动",
        bwrap_path=bwrap,
        version=version,
        checks=("binary",),
    )


# LLM: probe 失败统一复用 binary provenance，避免各分支丢失版本和已完成检查证据。
# 函数用途: 构造带 bwrap 路径、版本和前序 checks 的失败 readiness。
def _probe_failure(
    code: str,
    detail: str,
    *,
    bwrap: str,
    binary: SandboxReadiness,
) -> SandboxReadiness:
    return SandboxReadiness(
        False,
        code,
        detail,
        bwrap_path=bwrap,
        version=binary.version,
        checks=binary.checks,
    )


# LLM: 这里执行真实 namespace/mount 验收；临时路径和哨兵只活在一次 probe 内。
# 函数用途: 在给定临时根目录运行 bwrap，验证 owner 写入及隔离外文件不可见。
def _probe_isolation(
    probe_root: Path,
    *,
    bwrap: str,
    binary: SandboxReadiness,
    timeout: float,
) -> SandboxReadiness | None:
    owner_home = probe_root / "owner"
    owner_home.mkdir()
    outside = probe_root / "outside-sentinel"
    outside.write_text("must-not-be-visible", encoding="utf-8")
    marker = owner_home / "probe-ok"
    command = (
        "set -eu; printf sandbox-ready > ./probe-ok; "
        f"test ! -e {shlex.quote(str(outside))}; test ! -e /etc/passwd"
    )
    argv = wrap_shell_command(
        command,
        SandboxSpec(owner_home=owner_home, workspace=owner_home, bwrap_path=bwrap),
    )
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode != 0:
        output = (result.stderr or result.stdout).strip()[-1000:]
        return _probe_failure(
            "BWRAP_ISOLATION_FAILED",
            f"bwrap 隔离自检失败(exit={result.returncode}):{output}",
            bwrap=bwrap,
            binary=binary,
        )
    if not marker.exists() or marker.read_text(encoding="utf-8") != "sandbox-ready":
        return _probe_failure(
            "BWRAP_WRITE_CHECK_FAILED",
            "沙箱未能在 owner home 写入自检标记",
            bwrap=bwrap,
            binary=binary,
        )
    return None


# LLM: TemporaryDirectory 生命周期单独封装，避免 probe 主裁决函数堆叠 with/if 嵌套。
# 函数用途: 创建一次性 probe 根目录并返回隔离检查失败；通过时返回 None。
def _probe_temporary_root(
    *,
    bwrap: str,
    binary: SandboxReadiness,
    timeout: float,
) -> SandboxReadiness | None:
    with tempfile.TemporaryDirectory(prefix="my-agent-sandbox-") as tmp:
        return _probe_isolation(Path(tmp), bwrap=bwrap, binary=binary, timeout=timeout)


# LLM: 这是生产执行节点的真实隔离验收，不得退化成 exists()/--version 检查。
#   自测只在临时目录写 marker，不读用户数据，也不发网络请求。
# 函数用途: 验证 bwrap 真能创建隔离环境、写自己的目录且看不到隔离外哨兵文件。
def probe_sandbox(
    *,
    bwrap_path: str | None = None,
    binary_only: bool = False,
    timeout: float = 8.0,
) -> SandboxReadiness:
    bwrap = bwrap_path or find_bwrap()
    if not bwrap:
        return SandboxReadiness(
            False,
            "BWRAP_NOT_FOUND",
            f"当前执行环境不支持 bwrap:{platform.system()}/{platform.machine()}",
        )
    binary = _probe_binary(bwrap, timeout=timeout)
    if not binary.ready or binary_only:
        return binary
    try:
        failure = _probe_temporary_root(bwrap=bwrap, binary=binary, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _probe_failure(
            "BWRAP_ISOLATION_ERROR",
            f"bwrap 隔离自检异常:{exc}",
            bwrap=bwrap,
            binary=binary,
        )
    if failure:
        return failure
    return SandboxReadiness(
        True,
        "SANDBOX_READY",
        "bwrap 文件与进程隔离自检通过",
        bwrap_path=bwrap,
        version=binary.version,
        checks=("binary", "namespace", "owner_write", "outside_hidden", "host_etc_hidden"),
    )


# LLM: worker 启动和其他强隔离入口调用本函数形成 fail-closed 硬门；异常文本只诊断，
#   调用方不得捕获后继续执行用户命令。
# 函数用途: 要求当前节点通过完整 sandbox 自检，否则抛出明确异常阻止启动或执行。
def require_sandbox_ready() -> SandboxReadiness:
    report = probe_sandbox()
    if not report.ready:
        raise SandboxUnavailable(f"{report.code}:{report.detail}")
    return report


# LLM: 此 CLI 是镜像构建、worker startupProbe、人工诊断共用入口，不是模型工具。
# 函数用途: 输出 sandbox readiness JSON，并用退出码表达节点是否可以执行用户命令。
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查当前执行节点的 bwrap sandbox 是否可用")
    parser.add_argument("--binary-only", action="store_true", help="只验证二进制能启动，不创建隔离环境")
    parser.add_argument("--quiet", action="store_true", help="不输出 JSON，只返回退出码")
    args = parser.parse_args(argv)
    report = probe_sandbox(binary_only=args.binary_only)
    if not args.quiet:
        print(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0 if report.ready else 1


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (ValueError, OSError):
        return False


__all__ = [
    "SYSTEM_READ_ROOTS_BY_PLATFORM",
    "SandboxReadiness",
    "SandboxSpec",
    "SandboxUnavailable",
    "build_bwrap_argv",
    "find_bwrap",
    "probe_sandbox",
    "prune_transient_bwrap_sidecar_mounts",
    "require_sandbox_ready",
    "wrap_shell_command",
]


# LLM: 本次persona根只由冻结入口签发；namespace名称和host权威是同一不可变定位事实，不保存配置/CAS或授予权限。
# 类用途: 保存底图构造前的人格原名与canonical配置根，后续步骤不再次转换PathLike或选根。
@dataclass(frozen=True)
class _PersonaRootProjection:
    namespace_root: Path
    authority_root: Path


# LLM: 只在底图发出前解析人格根一次；返回canonical元数据权威，不授予读写且不冻结后续内核bind；解析错误必须拒绝启动。
# 函数用途: 一次保存人格原名及配置所属原host根，后改host-only别名或PathLike值不能选择另一个空配置。
def _freeze_persona_root(spec: SandboxSpec) -> _PersonaRootProjection | None:
    try:
        root = persona_sandbox_root(spec.owner_home, spec.protected_persona_root)
        if root is None:
            return None
        namespace_root = root.expanduser().absolute()
        return _PersonaRootProjection(namespace_root, namespace_root.resolve(strict=False))
    except (AgentsSourceProtectionError, OSError, RuntimeError) as error:
        raise SandboxUnavailable("SANDBOX_UNAVAILABLE: PATH_AGENTS_SOURCE_PROTECTION_INVALID") from error


# LLM: 只保存同次已发mount及原始参数以恢复更窄覆盖；空覆盖不授读权，不能从spec重新推导别名权限。
# 类用途: 记录人格投影所用挂载事实，祖先加固重挂时保留同一视图的精确子挂载。
@dataclass(frozen=True)
class _MountProjection:
    source: Path | None
    target: Path
    writable: bool = False
    alias: bool = False
    arguments: tuple[str, ...] = ()


# LLM: 只解析本模块argv，ro-bind及ro-bind-try同为RO；原参数供祖先重挂恢复，不从别名新增授权或模拟内核成功。
# 函数用途: 冻结底图源、目标、写位和必要的空视图只读参数，供投影及挂载次序保护共用。
def _mount_snapshot(argv: list[str]) -> tuple[_MountProjection, ...]:
    mounts: list[_MountProjection] = []
    for index, flag in enumerate(argv):
        if flag in {"--bind", "--ro-bind", "--ro-bind-try", "--dev-bind"}:
            mounts.append(_MountProjection(Path(argv[index + 1]), Path(argv[index + 2]), flag not in {"--ro-bind", "--ro-bind-try"},
                                           arguments=tuple(argv[index:index + 3])))
        elif flag == "--symlink":
            mounts.append(_MountProjection(Path(argv[index + 1]), Path(argv[index + 2]), alias=True,
                                           arguments=tuple(argv[index:index + 3])))
        elif flag in {"--tmpfs", "--proc", "--dev"}:
            mounts.append(_MountProjection(None, Path(argv[index + 1]),
                                           arguments=_empty_mount_arguments(argv, index)))
    return tuple(mounts)


# LLM: 只保留同次空视图紧邻的remount-ro，不把全根稍后的策略或其它mount复制成新授权。
# 函数用途: 提取一条空挂载及其精确只读标志，供祖先重挂恢复原覆盖。
def _empty_mount_arguments(argv: list[str], index: int) -> tuple[str, ...]:
    end = index + 2
    end += 2 if argv[end:end + 2] == ["--remount-ro", argv[index + 1]] else 0
    return tuple(argv[index:end])


# LLM: 后发父覆盖压过旧子mount；只比较已保存的词法坐标，不能再resolve根或把骨架/symlink当新读源。
# 函数用途: 找到最终底图覆盖一个namespace路径的有效挂载。
def _effective_mount(path: Path, mounts: tuple[_MountProjection, ...]) -> _MountProjection | None:
    return next((mount for mount in reversed(mounts) if _lexically_within(path, mount.target)), None)


# LLM: 已发symlink/异名bind沿同批mount映射回host源；元数据根缺mount时只准入口冻结权威，隐藏覆盖不能变成读授权。
#   其它已核验来源的宿主定位不授予读权；返回路径仍不保证最终内核bind前无竞争。
# 函数用途: 从冻结namespace名称链投影人格源，host-only根使用已保存权威而不晚读可变别名。
def _persona_projected_source(
    path: Path, mounts: tuple[_MountProjection, ...], *, unmounted_root: Path | None = None
) -> Path | None:
    location = path.expanduser().absolute()
    for _ in range(64):
        mount = _effective_mount(location, mounts)
        if mount is None or (mount.source is None and mount.target == Path("/")):
            return unmounted_root if unmounted_root is not None else location.resolve(strict=False)
        if mount.source is None:
            return None
        source = mount.source / location.relative_to(mount.target)
        if not mount.alias:
            return source.resolve(strict=False)
        location = source
    raise AgentsSourceProtectionError("PATH_AGENTS_SOURCE_PROTECTION_INVALID: namespace alias cycle")


# LLM: 逐段展开原名及每次readlink目标（包括..），每个symlink都按同次mount快照核父写位；不重解析授权根或扩大读写面。
#   canonical自绑定不能防目标链目录项unlink/recreate；缺失默认路径仍可选，异常和循环失败关闭，不承诺内核bind前无竞争。
# 函数用途: 对完整人格名称链及链接目标链中位于实际可写父目录的symlink失败关闭。
def _reject_persona_mutable_alias(path: Path, mounts: tuple[_MountProjection, ...]) -> None:
    original = path.expanduser().absolute()
    parent = Path(original.anchor)
    pending = list(original.parts[1:])
    links = 0
    while pending:
        part = pending.pop(0)
        if part == "..":
            parent = parent.parent
            continue
        node = parent / part
        try:
            metadata = node.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISLNK(metadata.st_mode):
            parent = node
            continue
        if _persona_parent_writable(parent, mounts):
            raise AgentsSourceProtectionError("PATH_AGENTS_SOURCE_PROTECTION_INVALID: mutable persona alias")
        links += 1
        if links > 64:
            raise AgentsSourceProtectionError("PATH_AGENTS_SOURCE_PROTECTION_INVALID: persona alias cycle")
        target = node.readlink()
        if target.is_absolute():
            parent = Path(target.anchor)
            pending = [*target.parts[1:], *pending]
        else:
            pending = [*target.parts, *pending]


# LLM: 对已冻结host源反向遍历bind/symlink事实；每个候选必须正向核回同一source且仍有有效mount，不能增读或跟随后改spec。
# 函数用途: 枚举同一保护源在当前namespace中的所有有效名称，包括/tmp异名和多层显式别名。
def _persona_namespace_names(path: Path, mounts: tuple[_MountProjection, ...]) -> tuple[Path, ...]:
    source = path.resolve(strict=False)
    names = {path, source}
    pending = list(names)
    while pending:
        location = pending.pop()
        for mount in mounts:
            if mount.source is None or not _lexically_within(location, mount.source):
                continue
            name = mount.target / location.relative_to(mount.source)
            if name not in names:
                names.add(name)
                pending.append(name)
        if len(names) > 4096:
            raise AgentsSourceProtectionError("PATH_AGENTS_SOURCE_PROTECTION_INVALID: namespace aliases")
    return tuple(sorted((name for name in names if _persona_path_visible(name, mounts)
                         and _persona_projected_source(name, mounts) == source), key=str))


# LLM: 异名祖先只按冻结底图原写位绑定同源，并恢复同次有效子mount；已有mount根不重挂，不能借/tmp投影重露限制。
# 函数用途: 给异名名称链添加防rename节点，同时保持原精确拒写、隐藏及业务子目录权限。
def _append_persona_namespace_ancestors(
    argv: list[str], paths: tuple[Path, ...], mounts: tuple[_MountProjection, ...]
) -> None:
    parents = {parent for name in paths for parent in name.parents if parent != Path("/")}
    for parent in sorted(parents, key=lambda path: (len(path.parts), str(path))):
        mount = _effective_mount(parent, mounts)
        source = _persona_projected_source(parent, mounts)
        if (mount is not None and mount.target != parent and not mount.alias and source is not None and source != parent
                and _persona_parent_writable(parent, mounts) and source.is_dir()):
            _append_mount_preserving_children(argv, ("--bind", str(source), str(parent)))


# LLM: 元数据权威由入口冻结；共享祖先先去重再核原写位和来源，不改变任何名称投影或配置身份复核。
#   身份/配置代次返回前复核，路径argv仍不消除最后核验到内核bind的竞争；普通同级业务写位保持原权限。
# 函数用途: 按同次来源和全部有效namespace别名保护人格及必要名称祖先，未知或不安全链失败关闭。
def _append_persona_readonly_mounts(
    argv: list[str], root: _PersonaRootProjection | None, spec: SandboxSpec, *, mounts_snapshot: tuple[_MountProjection, ...]
) -> list[str]:
    if root is None:
        return []
    placeholders: list[str] = []
    try:
        namespace_root = root.namespace_root
        authority_root = root.authority_root
        projected_root = _persona_projected_source(namespace_root, mounts_snapshot, unmounted_root=authority_root)
        if projected_root is not None and projected_root != authority_root:
            raise AgentsSourceProtectionError("PATH_AGENTS_SOURCE_PROTECTION_INVALID: changed authority root")
        _reject_persona_mutable_alias(namespace_root, mounts_snapshot)
        snapshot = persona_process_protection_snapshot(authority_root, all_owners=spec.full_access)
        for item in snapshot:
            _reject_persona_mutable_alias(item.path, mounts_snapshot)
        projection = [(name, item.path.resolve(strict=False)) for item in snapshot
                      for name in _persona_namespace_names(item.path, mounts_snapshot)]
        sources = dict(projection)
        paths = tuple(path for path in sources if sources[path].exists() or path.name != "persona"
                      or _persona_parent_writable(path.parent, mounts_snapshot))
        candidate_parents = {parent for _, canonical in projection for parent in canonical.parents if parent != Path('/')}
        ancestors = {parent for parent in candidate_parents if _persona_parent_writable(parent, mounts_snapshot)
                     and _persona_projected_source(parent, mounts_snapshot) == parent}
        identities = {item.path: item.identity for item in snapshot if item.identity is not None}
        _append_persona_ancestors(argv, ancestors)
        _append_persona_namespace_ancestors(argv, paths, mounts_snapshot)
        _append_persona_paths(argv, paths, placeholders, sources=sources, identities=identities)
        # Check even configured sources outside the visible view; lost identity is not an absent default path.
        for path, identity in identities.items():
            validate_persona_source_identity(path, identity)
        for item in snapshot:
            validate_persona_configuration(item)
    except (AgentsSourceProtectionError, OSError, RuntimeError) as error:
        raise SandboxUnavailable("SANDBOX_UNAVAILABLE: PATH_AGENTS_SOURCE_PROTECTION_INVALID") from error
    return placeholders


# LLM: 只绑定原可写祖先并保留仍有效的更窄覆盖；不得重露main隐藏/拒写，也不能吞掉明确允许的业务子目录。
# 函数用途: 建立防rename的祖先挂载节点，保留本轮底图已有的精确权限。
def _append_persona_ancestors(argv: list[str], ancestors: set[Path]) -> None:
    for parent in sorted(ancestors, key=lambda path: (len(path.parts), str(path))):
        if parent.is_dir():
            _append_mount_preserving_children(argv, ("--bind", str(parent), str(parent)))


# LLM: 原名称与canonical源分开；配置identity随同一投影传递，不能重读原别名或丢掉必需身份。
# 函数用途: 为底图内所有可见保护名称逐条添加同源只读覆盖。
def _append_persona_paths(
    argv: list[str], paths: tuple[Path, ...], placeholders: list[str], *,
    sources: dict[Path, Path] | None = None, identities: dict[Path, str] | None = None
) -> None:
    for protected in paths:
        if sources is None:
            _append_persona_path(argv, protected, placeholders)
        else:
            source = sources[protected]
            _append_persona_path(argv, protected, placeholders, source=source,
                                 expected_identity=(identities or {}).get(source))


# LLM: 必需配置identity先复核再检查存在，失踪/替换绝不能静默跳过；无配置默认缺persona仍仅做namespace占位。
# 函数用途: 从已批准canonical源覆盖一个保护名称，未知身份失败关闭，默认缺失控制目录只读占位。
def _append_persona_path(
    argv: list[str], protected: Path, placeholders: list[str], *,
    source: Path | None = None, expected_identity: str | None = None
) -> None:
    actual = protected if source is None else source
    if expected_identity is not None:
        validate_persona_source_identity(actual, expected_identity)
    if not actual.exists():
        if protected.name == "persona":
            placeholders += ["--tmpfs", str(protected), "--remount-ro", str(protected)]
        return
    if actual.is_file() and actual.stat().st_nlink != 1:
        raise AgentsSourceProtectionError("persona hardlink")
    argv += ["--ro-bind", str(actual), str(protected)]


# LLM: 保护名称必须已有最终mount授权，异名与canonical同等处理；精确source一致性由反投影调用方检查，隐藏覆盖不回退。
# 函数用途: 判断当前namespace名称是否有非空底图，不能为未挂宿主原名增加读取入口。
def _persona_path_visible(path: Path, mounts: tuple[_MountProjection, ...]) -> bool:
    location = path
    for _ in range(64):
        mount = _effective_mount(location, mounts)
        if mount is None or mount.source is None:
            return False
        if not mount.alias:
            return True
        location = mount.source / location.relative_to(mount.target)
    raise AgentsSourceProtectionError("PATH_AGENTS_SOURCE_PROTECTION_INVALID: namespace alias cycle")


# LLM: 只消费同次mount写位，symlink名称沿冻结namespace指向实际mount；readonly/隐藏覆盖和循环均不能授予父写权。
# 函数用途: 判断canonical或异名祖先的实际写位，不解析spec或从Full Access标志另开权限。
def _persona_parent_writable(path: Path, mounts: tuple[_MountProjection, ...]) -> bool:
    location = path
    for _ in range(64):
        mount = _effective_mount(location, mounts)
        if mount is None or mount.source is None:
            return False
        if not mount.alias:
            return mount.writable
        location = mount.source / location.relative_to(mount.target)
    raise AgentsSourceProtectionError("PATH_AGENTS_SOURCE_PROTECTION_INVALID: namespace alias cycle")


# LLM: 逆序单次遍历挂载，用已覆盖的路径前缀排除影子项；成本随总路径段数增长，不逐项重扫挂载表。
# 函数用途: 取得保持原顺序的最终有效挂载，保留异名、空覆盖和嵌套写根。
def _effective_mounts(mounts: tuple[_MountProjection, ...]) -> tuple[_MountProjection, ...]:
    covered: set[tuple[str, tuple[str, ...]]] = set()
    effective = []
    for mount in reversed(mounts):
        parts, anchor = mount.target.parts, mount.target.anchor
        if any((anchor, parts[:end]) in covered for end in range(len(parts), -1, -1)):
            continue
        covered.add((anchor, parts))
        effective.append(mount)
    return tuple(reversed(effective))


# LLM: 只移除随后恢复的子挂载影子，ro-bind-try与ro-bind同组三参数处理；非挂载选项与同目标挂载保留。
# 函数用途: 从argv清掉旧子挂载的影子副本，空挂载与紧邻的remount-ro成组移除。
def _remove_shadowed_mount_arguments(argv: list[str], arguments: set[tuple[str, ...]]) -> None:
    removed: set[int] = set()
    for index, flag in enumerate(argv):
        if flag in {'--bind', '--ro-bind', '--ro-bind-try', '--dev-bind', '--symlink'}:
            entry = tuple(argv[index:index + 3])
        elif flag in {'--tmpfs', '--proc', '--dev'}:
            entry = _empty_mount_arguments(argv, index)
        else:
            continue
        if entry in arguments:
            removed.update(range(index, index + len(entry)))
    argv[:] = [value for index, value in enumerate(argv) if index not in removed]


# LLM: 父挂载之后只重放同次有效子覆盖，并移除其旧影子项；不读取新授权，不改变B7及人格防rename语义。
# 函数用途: 线性筛选并压缩重挂子边界，避免多层祖先造成平方CPU与重复挂载膨胀。
def _append_mount_preserving_children(argv: list[str], arguments: tuple[str, ...]) -> None:
    target = Path(arguments[2])
    mounts = _mount_snapshot(argv)
    shadowed = {mount.arguments for mount in mounts if mount.target != target and _lexically_within(mount.target, target)}
    descendants = [mount for mount in _effective_mounts(mounts)
                   if mount.target != target and _lexically_within(mount.target, target)]
    _remove_shadowed_mount_arguments(argv, shadowed)
    argv.extend(arguments)
    for mount in descendants:
        argv.extend(mount.arguments)


# LLM: 严格H2隐藏只能最后生效；投影仍使用移除前的完整底图。read_only_root/hide_home允许原workspace/public重挂，不移动其隐藏。
# 函数用途: 暂移owner/full/allowlist的精确空目录覆盖，等待所有人格bind完成后最终重施。
def _defer_strict_hidden_mounts(argv: list[str], spec: SandboxSpec) -> None:
    if spec.read_only_root and spec.read_mode != "allowlist":
        return
    hidden = {str(Path(raw).resolve(strict=False)) for raw in spec.hidden_paths}
    for index in range(len(argv) - 4, -1, -1):
        if (argv[index] == "--tmpfs" and argv[index + 1] in hidden
                and argv[index + 2:index + 4] == ["--remount-ro", argv[index + 1]]):
            del argv[index:index + 4]


# LLM: 原普通deny允许更窄明确授权，绝对readonly不允许例外；严格隐藏最后盖住全部绑定。B7整根只读不重复隐藏private根。
# 函数用途: 在人格加固后收束原写边界和隐藏范围，所有恢复仅消费同次已挂视图。
def _append_final_mount_protections(argv: list[str], spec: SandboxSpec) -> None:
    allowed = _normalized_write_roots(spec.write_roots, preserve_nested=True)
    for path, writable in ordered_write_scope_roots(allowed, spec.write_scope_denied_roots):
        if not writable and path.exists():
            _append_mount_preserving_children(argv, ("--ro-bind", str(path), str(path)))
    _append_readonly_mounts(argv, spec.read_only_paths)
    if not spec.read_only_root or spec.read_mode == "allowlist":
        _append_hidden_mounts(argv, spec.hidden_paths)


# LLM: 授权只读原完整底图；加固祖先时保留子边界，最终重施main拒写/隐藏且chdir最后，不改变网络/owner/别名权限。
# 函数用途: 组合原沙箱与人格拒写，确保新增祖先挂载不会擦掉既有安全限制或合法B7重挂。
def build_bwrap_argv(spec: SandboxSpec) -> list[str]:
    authority_root = _freeze_persona_root(spec)
    argv = _build_bwrap_base_argv(spec)
    mounts = _mount_snapshot(argv)
    chdir = argv[-2:]
    del argv[-2:]
    _defer_strict_hidden_mounts(argv, spec)
    placeholders = _append_persona_readonly_mounts(argv, authority_root, spec, mounts_snapshot=mounts)
    _append_final_mount_protections(argv, spec)
    return [*argv, *placeholders, *chdir]


# LLM: CLI必须等所有argv构造器完成定义再启动；只移动启动时机，不把binary成功升级成完整隔离成功。
# 入口用途: 让模块运行与先import后main使用同一完整探针链和原退出码。
if __name__ == "__main__":  # pragma: no cover - 容器/K8s 真实探针入口
    raise SystemExit(main())
