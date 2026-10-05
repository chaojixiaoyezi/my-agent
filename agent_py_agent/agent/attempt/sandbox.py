# LLM: attempt 沙箱负责平台执行边界；同步 run 复用 process_run 的取消/超时组回收且不继承 stdin，PTY 仍由 build_argv 构造独立终端。
# 模块用途: 为不同平台构造执行隔离，批处理等待可被本 run 取消，不消费 Gateway 输入、不绕过隔离。
"""AttemptExecutionSandbox（3.txt E.4-E.8）：attempt 级平台执行网关。

- Linux：bwrap/namespace（复用 agent.tooling.sandbox 的挂载构造与自检）。
- macOS：Seatbelt（sandbox-exec）SBPL profile。
- 共享 workspace / owner 系统状态只读或不可见；独立 attempt 默认只写 view + staging，
  生产进程有显式写边界时只写该边界，cwd 可以保持只读（E.4/E.5）。
- readiness 失败（E.6）：任何可能写文件的 shell/build/test/child 进程
  handler=0，抛 SandboxUnavailableError（SANDBOX_UNAVAILABLE）；绝不退回
  "只设 cwd" 的宿主 shell（E.7）。
- owner_scope_root 为空、单租户或 admin 不取消 sandbox（E.8）：单租户/全权
  形态走 full_access 档（文件语义不变：bwrap 整根 bind / Seatbelt 不 deny），
  网关统一 + 进程隔离仍生效；readiness 失败同样 fail-closed，不退回宿主。

R2 建立机制与平台测试；G6（2026-08-10）接入生产工具执行主链：ShellTool
前台/后台、PTY 会话的 spawn 统一经本网关（tooling/shell._sandbox_exec），
AttemptSandboxSpec 由执行时真实上下文构造（attempt_view=任务工作目录、
staging_root=同任务目录、shared_workspace=owner home）。
"""

from __future__ import annotations

import json
import os
import platform
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..common.cancellation import raise_if_cancelled
from ..tooling.sandbox import (
    SYSTEM_READ_ROOTS_BY_PLATFORM,
    SandboxReadiness,
    SandboxSpec,
    SandboxUnavailable,
    build_bwrap_argv,
    find_bwrap,
    probe_sandbox,
)
from . import landlock_launcher
from .process_run import run_sandbox_process

SandboxReadMode = Literal["hide_home", "allowlist"]


class SandboxUnavailableError(SandboxUnavailable):
    """SANDBOX_UNAVAILABLE：节点缺隔离能力时 handler=0 的载体。

    继承 tooling.sandbox.SandboxUnavailable（G6）：生产 spawn 的既有
    except SandboxUnavailable 捕获链无需改动即对本网关的 fail-closed 生效。
    """


# LLM: allowlist 的系统底图只能取当前平台声明的窄目录；解析后的根不得成为用户目录或 /private 下的别名。
# 函数用途: 校验并展开当前平台必需的只读系统目录，缺失/越界时失败关闭。
def system_read_roots_for_platform(platform_name: str | None = None) -> tuple[Path, ...]:
    selected = platform_name or platform.system()
    raw_roots = SYSTEM_READ_ROOTS_BY_PLATFORM.get(selected)
    if raw_roots is None:
        raise SandboxUnavailableError("SANDBOX_UNAVAILABLE: SYSTEM_READ_ROOTS_UNAVAILABLE")
    roots: list[Path] = []
    for raw in raw_roots:
        original = Path(raw).expanduser()
        if not original.is_absolute() or _is_forbidden_system_root(original):
            raise SandboxUnavailableError("SANDBOX_UNAVAILABLE: SYSTEM_READ_ROOT_INVALID")
        try:
            resolved = original.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if _is_forbidden_system_root(resolved):
            raise SandboxUnavailableError("SANDBOX_UNAVAILABLE: SYSTEM_READ_ROOT_INVALID")
        if not resolved.is_dir():
            continue
        roots.extend((original.absolute(), resolved))
    if not roots:
        raise SandboxUnavailableError("SANDBOX_UNAVAILABLE: SYSTEM_READ_ROOTS_UNAVAILABLE")
    return tuple(dict.fromkeys(roots))


# LLM: 系统白名单根既拒绝直接宽根，也拒绝解析后落入用户家目录或 private 的符号链接。
# 函数用途: 判断平台系统目录是否越过允许的底图边界。
def _is_forbidden_system_root(path: Path) -> bool:
    forbidden = (Path("/Users"), Path("/home"), Path("/private"))
    return path == Path("/") or any(root == path or root in path.parents for root in forbidden)


# LLM: AttemptSandboxSpec separates readable cwd from explicit writable roots; read_mode=allowlist must not infer cwd access.
#   Production process tools set implicit_attempt_write_roots=False with an explicit host boundary; hide_home keeps its prior projection.
# 类用途: 描述一次命令的读模式、授权根、保护路径和网络范围，供 Linux/macOS 共用。
@dataclass(frozen=True)
class AttemptSandboxSpec:
    """attempt 级沙箱合同：独立 attempt 写 view/staging；生产调用服从显式写根。"""

    attempt_view: Path       # 本 attempt 唯一可写工作根（E.1）
    staging_root: Path       # 发布源 staging 区（H 节），可写
    shared_workspace: Path   # 共享 workspace：只读或不可见（E.4/E.5）
    owner_home: Path         # owner home 底图（只读；persona 文件强制只读）
    # 网络开关是两个平台共用的通用选项：False 时 Linux bwrap 用 --unshare-net，macOS Seatbelt 在规则最后加 (deny network*)。
    network_access: bool = True
    bwrap_path: str | None = None
    macos_sandbox_exec: str | None = None
    # G6：生产 spawn 接线字段（tooling/shell._sandbox_exec 透传）。
    full_access: bool = False  # 单租户/显式全权：文件语义不变，网关统一+进程隔离
    protected_persona_root: Path | None = None  # SOUL/USER/AGENTS.md 强制只读
    extra_write_roots: tuple[Path, ...] = ()  # __sandbox_write_roots 结构化授权根
    public_read_roots: tuple[Path, ...] = ()  # __sandbox_read_roots 只读根
    protected_write_paths: tuple[Path, ...] = ()  # forbidden_write_roots，最后覆盖为只读
    # Standalone attempt views are writable by definition. Production shell sets False whenever
    # an explicit write-boundary list exists, so a read-only cwd never becomes an implicit RW root.
    implicit_attempt_write_roots: bool = True
    # 整根只读形态：Linux 根文件系统只读，private_read_roots 可 tmpfs 隐藏并按 public_read_roots / extra_write_roots 重挂授权范围；
    # macOS 仍由 Seatbelt 读拒绝规则表达同一合同，写权限只落显式写根。
    read_only_root: bool = False
    # B7 只读根模式；None 保留非插件调用的历史语义，hide_home 继续走原实现。
    read_mode: SandboxReadMode | None = None
    # allowlist 专用的平台系统只读根，和宿主授权的 public_read_roots 分开记录。
    system_read_roots: tuple[Path, ...] = ()
    # owner 隔离形态下要拒读的宿主根：总有 my-agent 家目录根（其它 owner、配置与密钥、发布记录），开关打开时对非本机管理员
    # 再加用户家目录。拒读后仍放行本 owner home、attempt view、staging、授权读根和写根。Linux bwrap 本来就不挂载这些路径；
    # macOS 由 Seatbelt 读拒绝实现。full_access 不生效。
    private_read_roots: tuple[Path, ...] = ()
    # H2：对模型命令完全隐藏的宿主托管存储目录（插件安装库、包库），任何模式都生效（含 full_access）：Linux 盖一层只读空
    # tmpfs，macOS 在规则最后拒读写。模型 shell 隐藏托管存储；G1 插件沙箱只隐藏宿主客户端凭据及 secrets 目录，不隐藏自身环境。
    hidden_paths: tuple[Path, ...] = ()
    # H3：按正则拒写的宿主托管位置（path_access_policy.host_readonly_patterns：所有任务的核验记录），只有 macOS Seatbelt 能表达；
    # Linux bwrap 只能挂已存在的路径，忽略这一项（只保护 protected_write_paths 里的本任务记录，已知边界）。
    protected_write_patterns: tuple[str, ...] = ()
    # G4（Gateway 本机信任，第 (1) 层）：模型命令沙箱要拒绝连本机 Gateway 的实际绑定端口（回环）。macOS 规则写
    # `(deny network-outbound (remote tcp "*:<port>"))`——必须用 `*:` 而不是 `localhost:`，否则沙箱里用 IPv6 连
    # `::ffff:127.0.0.1` 能绕过且 Gateway 认成 127.0.0.1（ae 实测、be 复核）。只由模型 shell 的 _sandbox_exec 填写；
    # 插件进程沙箱不填（它要连 /plugin-host/query，网络由 M 线 B7 管）。端口取运行中 Gateway 实际绑定值，不写死配置。
    deny_gateway_ports: tuple[int, ...] = ()


# LLM: 只负责平台规则和就绪门；同步 run 的取消及回收交给 process_run，不新增无沙箱旁路，联测 attempt 与 verifier。
# 类用途: 为本次命令构建 Linux/macOS 沙箱，并把同步等待接到共用的可取消进程执行器。
class AttemptExecutionSandbox:
    """跨平台 attempt 沙箱。按宿主平台选择 Linux bwrap 或 macOS Seatbelt。

    用法：
        sandbox = AttemptExecutionSandbox(spec)
        sandbox.require_ready()          # fail-closed：不可用抛异常
        result = sandbox.run([...argv...], timeout=120)

    G6：生产 spawn 每调用构造一个实例（spec 随任务目录变化）。readiness
    探测只依赖平台与二进制路径（自检用临时目录，与 spec 路径无关），故按
    (platform, bwrap_path, macos_sandbox_exec) 进程级缓存——高频工具轮不会
    每次重复 subprocess 试跑。
    """

    _READINESS_CACHE: dict[tuple, SandboxReadiness] = {}

    def __init__(self, spec: AttemptSandboxSpec):
        self.spec = spec
        self._ready: SandboxReadiness | None = None
        self._platform = platform.system()

    @classmethod
    def _cached_readiness(cls, key: tuple) -> SandboxReadiness | None:
        return cls._READINESS_CACHE.get(key)

    @classmethod
    def _cache_readiness(cls, key: tuple, report: SandboxReadiness) -> SandboxReadiness:
        cls._READINESS_CACHE[key] = report
        return report

    # ---------------------------------------------------------------- 探测
    def probe(self, *, binary_only: bool = False) -> SandboxReadiness:
        """平台 readiness 探测。Linux 走 bwrap 完整隔离自检；macOS 走
        sandbox-exec 存在 + profile 编译试跑；其他平台明确不可用。"""
        if self._platform == "Linux":
            return probe_sandbox(
                bwrap_path=self.spec.bwrap_path, binary_only=binary_only
            )
        if self._platform == "Darwin":
            return self._probe_macos(binary_only=binary_only)
        return SandboxReadiness(
            False,
            "SANDBOX_UNSUPPORTED_PLATFORM",
            f"当前平台无 attempt 沙箱实现: {self._platform}",
        )

    def _probe_macos(self, *, binary_only: bool) -> SandboxReadiness:
        sandbox_exec = self.spec.macos_sandbox_exec or shutil.which("sandbox-exec")
        if not sandbox_exec:
            return SandboxReadiness(
                False,
                "SEATBELT_NOT_FOUND",
                "macOS 缺少 sandbox-exec（Seatbelt）",
            )
        if binary_only:
            return SandboxReadiness(
                True,
                "SEATBELT_BINARY_READY",
                "sandbox-exec 存在",
                checks=("binary",),
            )
        # profile 编译试跑：真实沙箱内只读校验（写系统目录必须失败）。
        probe_root = tempfile.mkdtemp(prefix="my-agent-seatbelt-")
        try:
            try_path = Path(probe_root) / "shared"
            try_path.mkdir()
            profile = self._macos_profile(
                attempt_view=Path(probe_root) / "view",
                staging=Path(probe_root) / "staging",
                shared=try_path,
            )
            denied = subprocess.run(
                [
                    sandbox_exec,
                    "-p",
                    profile,
                    "--",
                    "/bin/sh",
                    "-c",
                    f"touch {shlex.quote(str(try_path / 'x'))}",
                ],
                capture_output=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return SandboxReadiness(
                False,
                "SEATBELT_PROBE_ERROR",
                f"Seatbelt 自检异常:{exc}",
                bwrap_path=sandbox_exec,
            )
        finally:
            shutil.rmtree(probe_root, ignore_errors=True)
        if denied.returncode == 0:
            return SandboxReadiness(
                False,
                "SEATBELT_ISOLATION_FAILED",
                "sandbox-exec 未能拦截共享目录写入",
                bwrap_path=sandbox_exec,
            )
        return SandboxReadiness(
            True,
            "SEATBELT_READY",
            "Seatbelt 编译+写拦截自检通过",
            bwrap_path=sandbox_exec,
            checks=("binary", "profile_compile", "shared_write_blocked"),
        )

    def require_ready(self) -> SandboxReadiness:
        """E.6：readiness 失败 → 抛 SANDBOX_UNAVAILABLE（handler=0 由调用方
        以异常拒绝执行实现）。探测结果按平台二进制缓存，避免高频 spawn 重复
        试跑；缓存只存 ready 事实（含失败），spec 路径不影响结论。"""
        if self._ready is None:
            key = (self._platform, self.spec.bwrap_path, self.spec.macos_sandbox_exec, self.spec.network_access)
            cached = self._cached_readiness(key)
            if cached is not None:
                self._ready = cached
            else:
                self._ready = self._cache_readiness(key, _network_checked(self.probe(), self._platform, self.spec))
        if not self._ready.ready:
            raise SandboxUnavailableError(
                f"SANDBOX_UNAVAILABLE: {self._ready.code}: {self._ready.detail}"
            )
        return self._ready

    # ---------------------------------------------------------------- 执行
    # LLM: 先验证跨平台共享的受限读模式；None 保留旧调用合同，未知值必须在平台分派前拒绝。
    # 函数用途: 检查平台就绪状态和读模式，再组装对应平台的命令包装参数。
    def build_argv(self, command_argv: list[str]) -> list[str]:
        """把命令包装进平台沙箱（E.9：LSP/构建器/validator/后台 shell/脚本
        子进程同一网关入口）。不探测直接构造——调用方必须先 require_ready。"""
        self.require_ready()
        if self.spec.read_mode not in (None, "hide_home", "allowlist"):
            raise SandboxUnavailableError("SANDBOX_UNAVAILABLE: RESTRICTED_READ_MODE_INVALID")
        if self._platform == "Linux":
            return self._linux_argv(command_argv)
        if self._platform == "Darwin":
            return self._macos_argv(command_argv)
        raise SandboxUnavailableError(
            f"SANDBOX_UNAVAILABLE: 平台无沙箱实现: {self._platform}"
        )

    # LLM: extra_write_roots 来自当前工具的结构化 allowed_write_roots，首项是 canonical
    # task work 临时根；v8 隐藏根必须仍存在才能挂 tmpfs，缺失要失败关闭；写根顺序保持原合同。
    # 函数用途: 为 Linux 组装 bwrap 参数，并让任务临时区与当前项目目录分离。
    def _linux_argv(self, command_argv: list[str]) -> list[str]:
        if self.spec.read_mode == "allowlist" and (
            not self.spec.read_only_root or self.spec.full_access or self.spec.implicit_attempt_write_roots
        ):
            raise SandboxUnavailableError("SANDBOX_UNAVAILABLE: ALLOWLIST_SCOPE_INVALID")
        if self.spec.read_only_root and any(
            not Path(root).is_dir() for root in self.spec.private_read_roots
        ):
            raise SandboxUnavailableError("SANDBOX_UNAVAILABLE: PRIVATE_READ_ROOT_MISSING")
        implicit_roots = (
            (self.spec.attempt_view, self.spec.staging_root)
            if self.spec.implicit_attempt_write_roots
            else ()
        )
        spec = SandboxSpec(
            owner_home=self.spec.owner_home,
            workspace=self.spec.attempt_view,
            public_ro_roots=self.spec.public_read_roots,
            write_roots=(
                *self.spec.extra_write_roots,
                *implicit_roots,
            ),
            bwrap_path=self.spec.bwrap_path,
            protected_persona_root=self.spec.protected_persona_root,
            read_only_paths=self.spec.protected_write_paths,
            full_access=self.spec.full_access,
            network_access=self.spec.network_access,
            read_only_root=self.spec.read_only_root,
            read_mode=self.spec.read_mode,
            system_read_roots=self.spec.system_read_roots,
            preserve_public_read_root_aliases=self.spec.read_only_root,
            # B7：整根只读形态下，private_read_roots（v8/受限策略的隐藏根）并进 hidden_paths，由 _read_only_root_argv
            # tmpfs 盖住再把工作目录/写根挂回去；H2 的 hidden_paths（插件库）照旧。owner 隔离形态的 private_read_roots
            # 仍走 bwrap 不挂载语义，不受影响（那条路径不读 hidden_paths 的收窄逻辑）。
            hidden_paths=((*self.spec.private_read_roots, *self.spec.hidden_paths)
                          if self.spec.read_only_root else self.spec.hidden_paths),
        )
        return self._wrap_with_landlock([*build_bwrap_argv(spec), "--", *command_argv])

    # LLM: G5：登记了 Gateway 端口且 Landlock 就绪时，用启动器把 bwrap argv 包一层（exec bwrap 前按端口拒绝）。包不包只看
    #   landlock_net_readiness——它和 gateway_isolation_fact 读同一个探测，所以“包了”必然 applied、“没包”必然 unavailable:<原因>，
    #   不会出现“报了 applied 其实没挡”。环境没能力时不包、命令照跑（如实报 unavailable、靠第 (2) 层兜底）；施加失败的 fail-closed
    #   在启动器里（exec 前失败即非零退出、不 exec）。没登记端口时原样返回，Linux 既有 argv 逐字节不变。
    # 函数用途: Linux 下按就绪情况给 bwrap argv 套 Landlock 端口拒绝启动器。
    def _wrap_with_landlock(self, bwrap_argv: list[str]) -> list[str]:
        ports = self.spec.deny_gateway_ports
        if not ports:
            return bwrap_argv
        ready, _reason = landlock_net_readiness(self.spec.bwrap_path)
        if not ready:
            return bwrap_argv
        deny = ",".join(str(port) for port in dict.fromkeys(ports))
        # -I 隔离模式跑启动器：忽略 PYTHONPATH/用户 site，避免 Gateway 的 PYTHONPATH 或脚本目录遮住 ctypes/os（ae 复审）。
        return [sys.executable, "-I", str(_LANDLOCK_LAUNCHER), "--deny", deny, "--", *bwrap_argv]

    # LLM: 本沙箱这次对 Gateway 端口隔离的结构化事实，唯一来源是模块级 gateway_isolation_status（见其注释）：
    #   按“平台 + 本 spec 登记的端口 + Landlock 就绪”算，和 /status、_wrap_with_landlock 的包裹决定同一个探测。不依赖副作用。
    # 函数用途: 返回本沙箱这次对 Gateway 端口隔离的结构化事实。
    def gateway_isolation_fact(self) -> str:
        return gateway_isolation_status(self.spec.deny_gateway_ports, bwrap_path=self.spec.bwrap_path)

    # LLM: macOS argv must be built from the same explicit write roots and protected overlays as
    # Linux; never infer write permission from attempt_view/cwd when the boundary is explicit.
    # 函数用途: 为 macOS 命令生成 Seatbelt 包装参数。
    def _macos_argv(self, command_argv: list[str]) -> list[str]:
        sandbox_exec = self.spec.macos_sandbox_exec or shutil.which("sandbox-exec")
        if not sandbox_exec:
            raise SandboxUnavailableError(
                "SANDBOX_UNAVAILABLE: SEATBELT_NOT_FOUND: 缺少 sandbox-exec"
            )
        profile = "\n".join([self._macos_profile(
            attempt_view=self.spec.attempt_view,
            staging=self.spec.staging_root,
            shared=self.spec.shared_workspace,
            protected_persona_root=self.spec.protected_persona_root,
            write_roots=self.spec.extra_write_roots,
            protected_write_paths=self.spec.protected_write_paths,
            implicit_attempt_write_roots=self.spec.implicit_attempt_write_roots,
            full_access=self.spec.full_access,
        ), *_spec_rules(self.spec)])
        return [sandbox_exec, "-p", profile, "--", *command_argv]

    # LLM: 就绪与 argv 仍走唯一平台边界；启动前复查取消，等待和 TERM→宽限→KILL 共用 process_run，不改显式 PTY 通道。
    # 函数用途: 执行非交互沙箱命令并响应本 run 的取消；已取消不启动，运行中取消回收完整组后抛 ToolCancelled。
    def run(
        self,
        command_argv: list[str],
        *,
        timeout: float = 120.0,
        grace_seconds: float = 2.0,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess:
        """超时返回原 143；取消抛共用 ToolCancelled。脱组后代的写边界仍由平台沙箱保护。"""
        raise_if_cancelled()
        self.require_ready()
        argv = self.build_argv(command_argv)
        return run_sandbox_process(argv, timeout, grace_seconds, capture_output)

    # ------------------------------------------------------ macOS SBPL 构造
    # LLM: The profile is a pure projection of structured sandbox facts. Full Access omits the
    # broad write deny but still applies precise persona/control-path denies.
    # 函数用途: 生成 Seatbelt 规则，区分 WorkspaceOnly 写根与 Full Access 精确只读覆盖。
    @staticmethod
    def _macos_profile(
        *,
        attempt_view: Path,
        staging: Path,
        shared: Path,
        protected_persona_root: Path | None = None,
        write_roots: tuple[Path, ...] = (),
        protected_write_paths: tuple[Path, ...] = (),
        implicit_attempt_write_roots: bool = True,
        full_access: bool = False,
    ) -> str:
        """Seatbelt 策略：默认放行（mach/网络/进程语义同 bwrap：隔离文件为主）
        → 禁所有文件写 → 只对 attempt view + staging + extra 授权根
        （+ /dev 设备）重新开写。共享 workspace 保持只读（file-read* 默认
        放行，file-write* 拒绝）。

        full_access（单租户/显式全权，G6）：不加 file-write deny，文件语义
        与宿主一致，网关统一 + readiness 检查仍生效；persona 文件若在可写
        区内仍以更精确的 literal deny 强制只读。"""
        if full_access:
            lines = ["(version 1)", "(allow default)"]
            lines.extend(_readonly_path_denies(protected_write_paths))
            persona_denies = _persona_file_literal_denies(protected_persona_root)
            if persona_denies:
                lines.extend(persona_denies)
            return "\n".join(lines)
        effective_write_roots = [str(path.resolve()) for path in write_roots]
        if implicit_attempt_write_roots:
            effective_write_roots.extend(
                (str(attempt_view.resolve()), str(staging.resolve()))
            )
        effective_write_roots = list(dict.fromkeys(effective_write_roots))
        lines = [
            "(version 1)",
            "(allow default)",
            "(deny file-write*)",
            '(allow file-write* (subpath "/dev") (literal "/dev/null")'
            + "".join(f' (subpath {json.dumps(root)})' for root in effective_write_roots)
            + ")",
        ]
        lines.extend(_readonly_path_denies(protected_write_paths))
        persona_denies = _persona_file_literal_denies(protected_persona_root)
        if persona_denies:
            # Seatbelt 后写覆盖先写（本机实测 2026-09-26）：这些 deny 写在写根 allow 之后才生效，
            # persona 文件在可写区内也保持只读。顺序不能调换。
            lines.extend(persona_denies)
        return "\n".join(lines)

    @staticmethod
    def sandbox_readiness_dict() -> dict[str, Any]:
        """跨平台 readiness 汇总（诊断/CLI 用）。"""
        return {"platform": platform.system()}


# LLM: 只读隔离事实由平台沙箱实现决定，是 Shell 回执 external_host_paths_hidden 的唯一来源：Linux bwrap 只挂载授权根，
#   未挂载的宿主路径看不到；macOS Seatbelt 是 allow default 加写拒绝，读取不受限，宿主路径读得到。不能把 owner 隔离模式
#   一律说成“宿主路径已隐藏”。平台读边界变化时（例如给 Seatbelt 加读拒绝）必须同步这里与 test_sandbox.py。
# 函数用途: 判断当前平台的进程沙箱是否隐藏未授权的宿主路径。
def sandbox_hides_host_paths(platform_name: str | None = None) -> bool:
    return (platform_name or platform.system()) == "Linux"


# LLM: Seatbelt 规则“后写覆盖先写”（本机实测 2026-09-26：先拒绝根再放行子目录，子目录可读；顺序颠倒则子目录也被拒）。
#   公开解释器根可能有 symlink 别名；拒读范围按真实路径裁决，但显式授权的原路径和 realpath 都必须放行。
#   所以先写全部拒读根（my-agent 根，开关打开时还有用户家目录），再逐个放行本 owner 的可见范围，最后放行上层目录的元数据；
#   与 Linux 挂载视图对齐。只动 file-read*，写规则保持原样。改动须同步 test_attempt_sandbox.py 里的真实 sandbox-exec 用例。
# 函数用途: 生成 owner 隔离形态下宿主私有目录的读拒绝与放行规则；Full Access 或没有拒读根时返回空列表。
def _private_read_rules(spec: AttemptSandboxSpec) -> list[str]:
    if spec.full_access or not spec.private_read_roots:
        return []
    hidden = tuple(sorted({Path(path).resolve(strict=False) for path in spec.private_read_roots}))
    visible = {Path(path).resolve(strict=False) for path in (
        spec.owner_home, spec.shared_workspace, spec.attempt_view, spec.staging_root,
        *spec.public_read_roots, *spec.extra_write_roots)}
    visible.update(Path(path).expanduser().absolute() for path in spec.public_read_roots)
    visible.update(Path(path).expanduser().absolute() for path in spec.extra_write_roots)
    allowed = sorted(json.dumps(str(path)) for path in visible)
    return [*(f"(deny file-read* (subpath {json.dumps(str(root))}))" for root in hidden),
            *(f"(allow file-read* (subpath {path}))" for path in allowed),
            *_ancestor_metadata_rules(hidden, visible)]


# LLM: allowlist 的 file-read* 总门必须先拒绝，再仅放行系统、R/E、R/W 授权根；祖先只获 literal metadata，不可列目录。
# 函数用途: 生成有限读 Seatbelt 规则，保留 symlink alias 与 realpath 的同一读授权。
def _allowlist_read_rules(spec: AttemptSandboxSpec) -> list[str]:
    visible: set[Path] = set()
    for raw in (*spec.system_read_roots, *spec.public_read_roots, *spec.extra_write_roots):
        try:
            path = Path(raw).expanduser().absolute()
            visible.update((path, path.resolve(strict=False)))
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
    allowed = sorted(json.dumps(str(path)) for path in visible)
    return ["(deny file-read*)",
            *(f"(allow file-read* (subpath {path}))" for path in allowed),
            *_ancestor_metadata_rules((Path("/"),), visible)]


# LLM: 拒读根按 subpath 连同根目录本身一起拒绝；放行子目录之后，根与放行目录之间的上层目录仍拿不到元数据。git、node 的
#   realpath、python -m venv 规范化路径时要逐级 lstat，会报 Operation not permitted 退出（2026-09-26 本机复现，owner 工作区
#   在 ~/.my-agent 之下）。所以只对这些上层目录按 literal 放行 file-read-metadata：能 stat 目录本身，仍不能列目录、不能读同级。
# 函数用途: 生成拒读根内、放行目录上层各级目录的元数据放行规则；没有这类目录时返回空列表。
def _ancestor_metadata_rules(hidden_roots: tuple[Path, ...], visible: set[Path]) -> list[str]:
    ancestors = sorted({json.dumps(str(parent)) for path in visible for parent in path.parents
                        if any(parent == root or root in parent.parents for root in hidden_roots)})
    if not ancestors:
        return []
    return ["(allow file-read-metadata " + " ".join(f"(literal {path})" for path in ancestors) + ")"]


# LLM: H2：宿主托管存储对模型命令既不可读也不可写。Seatbelt 后写覆盖先写，所以这些拒绝必须排在整份规则最后，压过 owner home 的读放行；
#   存储在 owner home 内，上层目录本来就可读，不需要额外的元数据放行。改动须同步 test_host_managed_store_access.py 的真实 sandbox-exec 用例。
# 函数用途: 把要隐藏的宿主托管存储目录转成 Seatbelt 的读写拒绝规则。
def _hidden_path_rules(paths: tuple[Path, ...]) -> list[str]:
    roots = sorted({json.dumps(str(Path(path).resolve(strict=False))) for path in paths})
    return [f"(deny file-read* file-write* (subpath {root}))" for root in roots]


def _persona_file_literal_denies(protected_persona_root: Path | None) -> list[str]:
    """persona 文件（SOUL/USER/AGENTS.md）的 Seatbelt literal deny 规则。

    与 bwrap 侧 `_append_persona_readonly_mounts` 同语义：这些文件是用户
    长期人格，shell 不得改写。只对实际存在的文件生成规则（探针临时目录里
    不存在 → 不生成，probe 语义与真实一致）。
    """
    if protected_persona_root is None:
        return []
    denies: list[str] = []
    for name in ("SOUL.md", "USER.md", "AGENTS.md"):
        candidate = Path(protected_persona_root) / name
        if candidate.is_file():
            denies.append(
                f"(deny file-write* (literal {json.dumps(str(candidate.resolve()))}))"
            )
    return denies


# LLM: 要求断网时，平台沙箱本身可用还不够，必须真能隔离网络：Linux 实跑一次 bwrap --unshare-net（容器里缺 NET_ADMIN
#   时回环配置会失败），macOS 实跑一次带 (deny network*) 的 Seatbelt。失败按沙箱不可用 fail-closed，
#   不能让命令以“bwrap 启动失败”的形态跑完再被误判成程序自己的输出。结果由 require_ready 随 readiness 缓存。
# 函数用途: 平台沙箱可用且要求断网时，再探测一次本机能否在沙箱里切断网络；其余情况原样返回平台探测结果。
def _network_checked(report: SandboxReadiness, platform_name: str, spec: AttemptSandboxSpec) -> SandboxReadiness:
    if not report.ready or spec.network_access:
        return report
    command = [sys.executable, "-I", "-S", "-c", "pass"]
    if platform_name == "Linux":
        argv = [str(spec.bwrap_path or find_bwrap()), "--unshare-net", "--ro-bind", "/", "/", "--dev", "/dev", "--", *command]
    else:
        binary = spec.macos_sandbox_exec or shutil.which("sandbox-exec")
        argv = [str(binary), "-p", "(version 1)\n(allow default)\n(deny network*)", "--", *command]
    try:
        completed = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        completed = None
    if completed is None or completed.returncode != 0:
        return SandboxReadiness(False, "SANDBOX_NETWORK_ISOLATION_UNAVAILABLE", "本机沙箱无法切断网络")
    return SandboxReadiness(True, "SANDBOX_READY", "沙箱可用且能切断网络")


# LLM: 由 spec 派生的附加 Seatbelt 规则按固定顺序拼接：私有读拒绝、上级目录写拒绝、隐藏路径、断网、Gateway 端口拒绝；
#   隐藏路径排在读放行之后，断网与端口拒绝排最后（Seatbelt 后写覆盖先写；两条 deny 先后不影响结果）。上级目录规则只拒写，
#   不影响前面的读规则。G4 的端口拒绝只在 deny_gateway_ports 非空时出现，空时逐字节不变。
# 函数用途: 汇总按 spec 生成的 macOS 附加规则。
def _spec_rules(spec: AttemptSandboxSpec) -> list[str]:
    read_rules = _allowlist_read_rules(spec) if spec.read_mode == "allowlist" else _private_read_rules(spec)
    return [*read_rules, *_ancestor_write_denies(spec), *_pattern_write_denies(spec.protected_write_patterns),
            *_hidden_path_rules(spec.hidden_paths), *_network_rules(spec.network_access),
            *_gateway_port_denies(spec.deny_gateway_ports)]


# LLM: 正则拒写（H3）：模式是 POSIX ERE，按 JSON 字符串写进规则（本机实测 SBPL 的 regex 认普通字符串）。只拒写，排在写根放行
#   之后才能盖过它（Seatbelt 后写覆盖先写）；不影响读规则。没有模式时返回空列表，现有配置逐字节不变。
# 函数用途: 把按正则声明的只读位置转成 Seatbelt 写拒绝。
def _pattern_write_denies(patterns: tuple[str, ...]) -> list[str]:
    return [f"(deny file-write* (regex {json.dumps(pattern)}))" for pattern in dict.fromkeys(patterns)]


# LLM: 沙箱通用规则（H3 探针发现，2026-10-02）：Seatbelt 的 subpath/literal 拒绝只认当前路径，命令先把上级目录改名
#   （mv ~/.my-agent ~/.my-agent2）、写完再改回，只读覆盖、人格文件、隐藏路径就全部落空。所以给每个受保护路径的全部上级目录
#   （直到 /）各加一条 literal 写拒绝：只拦改名、删除、chmod、touch 上级目录本身，上级目录里的普通读写照常（本机实测
#   mkdir -p、git、cp、tar 均正常）。只拒写，所以排在私有读规则之后、隐藏路径之前都不改变读裁决。Linux 只读挂载跟着
#   目录项走，不需要这条。改动须同步 test_host_files_access.py 的真实 sandbox-exec 用例。
# 函数用途: 生成受保护路径（只读覆盖、隐藏路径、人格根）所有上级目录的写拒绝；没有受保护路径时返回空列表。
def _ancestor_write_denies(spec: AttemptSandboxSpec) -> list[str]:
    protected = [*spec.protected_write_paths, *spec.hidden_paths]
    persona = [spec.protected_persona_root] if spec.protected_persona_root is not None else []
    ancestors = {parent for path in (*protected, *persona) for parent in Path(path).resolve(strict=False).parents}
    ancestors.update(Path(path).resolve(strict=False) for path in persona)
    if not ancestors:
        return []
    literals = sorted(json.dumps(str(path)) for path in ancestors)
    return ["(deny file-write* " + " ".join(f"(literal {path})" for path in literals) + ")"]


# LLM: 断网只看结构化 network_access；规则放在整份配置最后（Seatbelt 后写覆盖先写），full_access 也同样生效。
#   默认 True 时不加任何规则，现有调用方的配置逐字节不变。改动同步 test_attempt_sandbox.py。
# 函数用途: 按网络开关生成 macOS 的断网规则。
def _network_rules(network_access: bool) -> list[str]:
    return [] if network_access else ["(deny network*)"]


# LLM: G4（Gateway 本机信任第 (1) 层）：按端口拒绝连本机 Gateway。必须用 `*:<port>` 而不是 `localhost:<port>`——
#   后者挡不住沙箱里用 IPv6 连 `::ffff:127.0.0.1`（IPv4 映射地址），而 Gateway 只绑 IPv4、会把它认成 127.0.0.1 即本机管理员
#   （ae 实测、be 复核，证据 gateway-local-trust-20261003/mapped_v4_verify.py）。两条 deny 的先后不影响结果（都拒）；
#   端口为空（本进程没有运行中的 Gateway）时返回空列表，配置逐字节不变。改动同步 test_attempt_sandbox.py。
# 函数用途: 为每个 Gateway 绑定端口生成 macOS 的出站拒绝规则。
def _gateway_port_denies(ports: tuple[int, ...]) -> list[str]:
    return [f'(deny network-outbound (remote tcp "*:{port}"))' for port in dict.fromkeys(ports)]


# LLM: G4：进程内登记运行中 Gateway 的实际绑定端口（server_address 的端口），供模型命令沙箱按端口拒绝。
#   由 Gateway 启动时登记、停机时注销（gateway_parts.http_service）；模型命令在 Gateway 进程内执行，所以读得到。
#   不是运行 Gateway 的进程（CLI run、测试）注册表为空，不加端口规则——“同机其它 Gateway”不在第 (1) 层范围，靠第 (2) 层。
_LOCAL_GATEWAY_PORTS: set[int] = set()
_LOCAL_GATEWAY_PORTS_LOCK = threading.Lock()


# LLM: 端口必须是真正的 int（来自 server_address[1]）且为正；float/str/None/bool 一律不收——不做 int() 强转，
#   否则 1.5 会被当成 1。取不到合法端口时不登记（不等于要拒所有端口）。
# 函数用途: 把一个对象规整成合法端口号，非法返回 None。
def _coerce_port(port: object) -> int | None:
    if not isinstance(port, int) or isinstance(port, bool) or port <= 0:
        return None
    return port


# LLM: 幂等登记一个 Gateway 绑定端口。有副作用：改进程级注册表。
# 函数用途: Gateway 绑定成功后登记它的实际端口，供模型命令沙箱拒绝。
def register_gateway_bound_port(port: object) -> None:
    value = _coerce_port(port)
    if value is None:
        return
    with _LOCAL_GATEWAY_PORTS_LOCK:
        _LOCAL_GATEWAY_PORTS.add(value)


# LLM: 幂等注销；端口不在表里或非法时静默返回。有副作用：改进程级注册表。
# 函数用途: Gateway 停机时注销它的端口。
def unregister_gateway_bound_port(port: object) -> None:
    value = _coerce_port(port)
    if value is None:
        return
    with _LOCAL_GATEWAY_PORTS_LOCK:
        _LOCAL_GATEWAY_PORTS.discard(value)


# LLM: 读当前进程内已登记的 Gateway 端口快照（排序，稳定）；模型命令沙箱构造时调用。
# 函数用途: 返回本进程运行中 Gateway 的全部绑定端口。
def gateway_bound_ports() -> tuple[int, ...]:
    with _LOCAL_GATEWAY_PORTS_LOCK:
        return tuple(sorted(_LOCAL_GATEWAY_PORTS))


# G5：Landlock 端口拒绝启动器脚本路径（本包内自包含脚本，exec bwrap 前施加 Landlock）。
_LANDLOCK_LAUNCHER = Path(__file__).with_name("landlock_launcher.py")


# LLM: G5：判断本机 Linux 环境能不能用 Landlock 按端口拒绝（不施加、不限制本进程，只查询——在 Gateway 进程里调是安全的）。
#   逐项探 ae 列的前提：架构认识、内核 ABI≥4（网络规则需要 v4）、bwrap 不是 setuid（no_new_privs 下 setuid 位失效、bwrap 起不来）。
#   非 Linux 返回 NOT_LINUX。返回 (是否就绪, 原因码)：不就绪时原因码进 gateway_isolation 的 unavailable:<原因>，命令照跑、靠第 (2) 层兜底。
#   userns 等 bwrap 自身前提由既有 bwrap 就绪探测覆盖，这里不重复。
# 函数用途: 探测 Linux Landlock 网络端口拒绝是否可用，返回 (ready, reason_code)。
def landlock_net_readiness(bwrap_path: str | None = None) -> tuple[bool, str]:
    if platform.system() != "Linux":
        return False, "NOT_LINUX"
    nums = landlock_launcher.syscall_numbers()
    if nums is None:
        return False, "LANDLOCK_UNKNOWN_ARCH"
    import ctypes

    if landlock_launcher.landlock_abi(ctypes.CDLL(None, use_errno=True), nums) < 4:
        return False, "LANDLOCK_NET_UNSUPPORTED"
    if _bwrap_is_setuid(bwrap_path):
        return False, "BWRAP_SETUID"
    return True, "LANDLOCK_READY"


# LLM: bwrap 带 setuid 位时，no_new_privs（启动器施加 Landlock 必设）会让 setuid 失效，无非特权 userns 的机器上 bwrap 起不来。
#   路径取不到（缺 bwrap）时返回 False——缺 bwrap 由既有沙箱就绪探测拦，不在本判定里重复报。只读文件元数据。
# 函数用途: 判断 bwrap 可执行文件是否带 setuid 位。
def _bwrap_is_setuid(bwrap_path: str | None) -> bool:
    path = bwrap_path or find_bwrap()
    if not path:
        return False
    try:
        return bool(os.stat(path).st_mode & stat.S_ISUID)
    except OSError:
        return False


# LLM: G4/G5：Gateway 端口隔离的唯一事实来源，供沙箱对象（按本 spec 端口）和 /status（按进程登记端口）同口径调用——
#   一个事实只有一处定义。no_ports→not_applicable（没登记端口、无从隔离）；macOS→applied（Seatbelt 恒可表达）；
#   Linux→按 landlock_net_readiness 真实探测：就绪 applied，否则 unavailable:<原因码>；其它平台→unavailable:unsupported_platform。
#   bwrap_path 透传给 Linux 的 setuid 探测（/status 不传，内部用 find_bwrap）。只读结构化入参与系统探测，不施加任何隔离。
# 函数用途: 按平台与登记端口给出 Gateway 端口隔离的结构化事实。
def gateway_isolation_status(ports: tuple[int, ...], *, system: str | None = None,
                            bwrap_path: str | None = None) -> str:
    if not ports:
        return "not_applicable"
    resolved = system or platform.system()
    if resolved == "Darwin":
        return "applied"
    if resolved == "Linux":
        ready, reason = landlock_net_readiness(bwrap_path)
        return "applied" if ready else f"unavailable:{reason}"
    return "unavailable:unsupported_platform"


# LLM: Seatbelt needs explicit deny rules for host control metadata because broader owner-home
# write grants are allowed. Directories use subpath+literal; files use literal.
#   还不存在的路径也写规则（literal+subpath，H3）：Seatbelt 按路径匹配，不需要目标存在，这样命令也建不出它（本机实测）。
#   Linux 的只读挂载做不到，由 tooling/sandbox._append_readonly_mounts 只挂已存在的。
# 函数用途: 把 forbidden_write_roots 转成比宽泛写入白名单更具体的 macOS 只读规则。
def _readonly_path_denies(paths: tuple[Path, ...]) -> list[str]:
    denies: list[str] = []
    seen: set[Path] = set()
    for raw in paths:
        try:
            path = Path(raw).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        if path in seen:
            continue
        seen.add(path)
        encoded = json.dumps(str(path))
        if path.is_file():
            denies.append(f"(deny file-write* (literal {encoded}))")
        else:
            denies.append(f"(deny file-write* (literal {encoded}) (subpath {encoded}))")
    return denies


__all__ = [
    "AttemptSandboxSpec",
    "AttemptExecutionSandbox",
    "SandboxReadMode",
    "SandboxUnavailableError",
    "system_read_roots_for_platform",
    "sandbox_hides_host_paths",
    "register_gateway_bound_port",
    "unregister_gateway_bound_port",
    "gateway_bound_ports",
    "gateway_isolation_status",
    "landlock_net_readiness",
]
