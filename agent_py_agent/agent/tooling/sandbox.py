
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
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
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


# LLM: SandboxSpec 是唯一 bwrap 挂载合同，workspace 必须已由 PathAccessPolicy 裁决。
# 类用途: 描述当前用户可写 home、已授权工作目录和可选公共只读目录。
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
    full_access: bool = False


# LLM: 这是 bwrap 文件系统/进程隔离策略的唯一 argv 构造点。owner_home 和已授权
#   workspace 是仅有的可写挂载；新增挂载必须同步安全测试和设计文档。
# 函数用途: 根据用户目录和本次工作目录构造 bubblewrap 启动参数。
def build_bwrap_argv(spec: SandboxSpec) -> list[str]:
    """构造 bwrap argv:文件/进程隔离到 owner home,但放行外网。"""
    bwrap = spec.bwrap_path or find_bwrap()
    if not bwrap:
        raise SandboxUnavailable("bwrap 不可用(仓库内置缺失且系统未装)")
    if spec.full_access:
        argv = [
            bwrap,
            "--die-with-parent",
            "--new-session",
            "--share-net",
            "--bind",
            "/",
            "/",
        ]
        _append_persona_readonly_mounts(argv, spec.protected_persona_root or spec.owner_home)
        argv += ["--chdir", str(spec.workspace)]
        return argv
    argv = [
        bwrap,
        "--die-with-parent",   # 父进程死则沙箱死,不留孤儿
        "--unshare-pid",       # 进程隔离:ps 只看到自己
        "--unshare-uts",
        "--unshare-ipc",
        "--share-net",         # ★ 放行外网:不 unshare net,外部 API/web/url 照常
        "--new-session",
        # 嵌套容器内挂新 procfs 在 Docker Desktop/K8s hardened runtime 常被内核拒绝。
        # 保留 PID namespace，但给命令空 /proc：看不到其他进程且不需要 mount proc 权限。
        "--dir", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",     # 临时,rm -rf /tmp 无害
    ]
    for ro in _SYSTEM_RO_ROOTS:
        if Path(ro).exists():
            argv += ["--ro-bind", ro, ro]
    for ro in _NETWORK_RO_FILES:  # DNS + TLS 证书,外网/https 必需
        if Path(ro).exists():
            argv += ["--ro-bind", ro, ro]
    write_roots = _normalized_write_roots(spec.write_roots)
    if spec.write_roots is None:
        # 普通 owner shell 没有更窄的任务合同：自己家仍可读写。
        argv += ["--bind", str(spec.owner_home), str(spec.owner_home)]
    else:
        # 子代理/受约束任务：owner home 只是输入底图。先整体只读，再把结构化授权根
        # 逐个覆盖成可写，shell 重定向、脚本 open()、cp/mv 都无法绕过文件工具门。
        argv += ["--ro-bind", str(spec.owner_home), str(spec.owner_home)]
        for root in write_roots:
            if root.exists() and _is_relative_to(root, spec.owner_home):
                argv += ["--bind", str(root), str(root)]
    # 工作区可能在 owner home 外(例如容器把当前项目挂到 /workspace)。target 已经过
    # PathAccessPolicy 裁决；只挂本次已授权工作区，不挂它的父目录或其他用户目录。
    if not _is_relative_to(spec.workspace, spec.owner_home):
        writable_workspace = spec.write_roots is None or any(
            _is_relative_to(spec.workspace, root) for root in write_roots
        )
        argv += [
            "--bind" if writable_workspace else "--ro-bind",
            str(spec.workspace),
            str(spec.workspace),
        ]
    # 结构化授权也可以精确开放 owner home 外的挂载（例如管理员批准的项目根）。
    for root in write_roots:
        if root.exists() and not _is_relative_to(root, spec.owner_home):
            argv += ["--bind", str(root), str(root)]
    # 无论 owner home 当前是 rw 还是 ro，都把需要真人确认的长期人格文件明确覆盖成
    # 只读挂载。update_persona 在宿主进程执行，不走 shell，确认后的正式写入仍可完成。
    _append_persona_readonly_mounts(
        argv,
        spec.protected_persona_root or spec.owner_home,
    )
    # 公共区:只读
    for pub in spec.public_ro_roots:
        if pub.exists() and not _is_relative_to(pub, spec.owner_home):
            argv += ["--ro-bind", str(pub), str(pub)]
    argv += ["--chdir", str(spec.workspace)]
    return argv


def _normalized_write_roots(raw_roots: tuple[Path, ...] | None) -> tuple[Path, ...]:
    if raw_roots is None:
        return ()
    roots: list[Path] = []
    for raw in raw_roots:
        try:
            root = Path(raw).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        # 已开放父根时不重复挂子根；遇到更宽父根则替换已有子根。
        if any(_is_relative_to(root, existing) for existing in roots):
            continue
        roots = [existing for existing in roots if not _is_relative_to(existing, root)]
        roots.append(root)
    return tuple(roots)


def _append_persona_readonly_mounts(argv: list[str], root: Path) -> None:
    for name in ("SOUL.md", "USER.md", "AGENTS.md"):
        protected = root / name
        if protected.is_file():
            argv += ["--ro-bind", str(protected), str(protected)]


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
    "SandboxReadiness",
    "SandboxSpec",
    "SandboxUnavailable",
    "build_bwrap_argv",
    "find_bwrap",
    "probe_sandbox",
    "require_sandbox_ready",
    "wrap_shell_command",
]


if __name__ == "__main__":  # pragma: no cover - 容器/K8s 真实探针入口
    raise SystemExit(main())
