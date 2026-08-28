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
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..tooling.sandbox import (
    SandboxReadiness,
    SandboxSpec,
    SandboxUnavailable,
    build_bwrap_argv,
    probe_sandbox,
)


class SandboxUnavailableError(SandboxUnavailable):
    """SANDBOX_UNAVAILABLE：节点缺隔离能力时 handler=0 的载体。

    继承 tooling.sandbox.SandboxUnavailable（G6）：生产 spawn 的既有
    except SandboxUnavailable 捕获链无需改动即对本网关的 fail-closed 生效。
    """


# LLM: AttemptSandboxSpec separates readable cwd from explicit writable roots. Production process
# tools must set implicit_attempt_write_roots=False whenever a host write boundary is present.
# 类用途: 描述一次命令可读、可写、受保护和网络范围，供 Linux/macOS 共用。
@dataclass(frozen=True)
class AttemptSandboxSpec:
    """attempt 级沙箱合同：独立 attempt 写 view/staging；生产调用服从显式写根。"""

    attempt_view: Path       # 本 attempt 唯一可写工作根（E.1）
    staging_root: Path       # 发布源 staging 区（H 节），可写
    shared_workspace: Path   # 共享 workspace：只读或不可见（E.4/E.5）
    owner_home: Path         # owner home 底图（只读；persona 文件强制只读）
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
            key = (self._platform, self.spec.bwrap_path, self.spec.macos_sandbox_exec)
            cached = self._cached_readiness(key)
            if cached is not None:
                self._ready = cached
            else:
                self._ready = self._cache_readiness(key, self.probe())
        if not self._ready.ready:
            raise SandboxUnavailableError(
                f"SANDBOX_UNAVAILABLE: {self._ready.code}: {self._ready.detail}"
            )
        return self._ready

    # ---------------------------------------------------------------- 执行
    def build_argv(self, command_argv: list[str]) -> list[str]:
        """把命令包装进平台沙箱（E.9：LSP/构建器/validator/后台 shell/脚本
        子进程同一网关入口）。不探测直接构造——调用方必须先 require_ready。"""
        self.require_ready()
        if self._platform == "Linux":
            return self._linux_argv(command_argv)
        if self._platform == "Darwin":
            return self._macos_argv(command_argv)
        raise SandboxUnavailableError(
            f"SANDBOX_UNAVAILABLE: 平台无沙箱实现: {self._platform}"
        )

    # LLM: extra_write_roots 来自当前工具的结构化 allowed_write_roots，首项是 canonical
    # task work 临时根；必须先于 attempt_view，避免 build_bwrap_argv 把项目 cwd 当 /tmp 后端。
    # 函数用途: 为 Linux 组装 bwrap 参数，并让任务临时区与当前项目目录分离。
    def _linux_argv(self, command_argv: list[str]) -> list[str]:
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
        )
        return [*build_bwrap_argv(spec), "--", *command_argv]

    # LLM: macOS argv must be built from the same explicit write roots and protected overlays as
    # Linux; never infer write permission from attempt_view/cwd when the boundary is explicit.
    # 函数用途: 为 macOS 命令生成 Seatbelt 包装参数。
    def _macos_argv(self, command_argv: list[str]) -> list[str]:
        sandbox_exec = self.spec.macos_sandbox_exec or shutil.which("sandbox-exec")
        if not sandbox_exec:
            raise SandboxUnavailableError(
                "SANDBOX_UNAVAILABLE: SEATBELT_NOT_FOUND: 缺少 sandbox-exec"
            )
        profile = self._macos_profile(
            attempt_view=self.spec.attempt_view,
            staging=self.spec.staging_root,
            shared=self.spec.shared_workspace,
            protected_persona_root=self.spec.protected_persona_root,
            write_roots=self.spec.extra_write_roots,
            protected_write_paths=self.spec.protected_write_paths,
            implicit_attempt_write_roots=self.spec.implicit_attempt_write_roots,
            full_access=self.spec.full_access,
        )
        return [sandbox_exec, "-p", profile, "--", *command_argv]

    def run(
        self,
        command_argv: list[str],
        *,
        timeout: float = 120.0,
        grace_seconds: float = 2.0,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess:
        """在沙箱内执行命令。超时 → TERM → 宽限 → KILL 回收整进程组（E.11：
        TERM→宽限→KILL 只用于回收；沙箱保证即使孙进程脱组也只能写 staging）。"""
        self.require_ready()
        argv = self.build_argv(command_argv)
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.PIPE if capture_output else None,
            text=capture_output,
            start_new_session=True,  # 独立进程组，回收时整组 TERM/KILL
        )
        try:
            out, err = proc.communicate(timeout=timeout)
            return subprocess.CompletedProcess(argv, proc.returncode, out, err)
        except subprocess.TimeoutExpired:
            # TERM → 宽限 → KILL（E.11 回收，不向共享目录写任何东西）。
            self._terminate_group(proc, grace_seconds=grace_seconds)
            try:
                out, err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate()
            return subprocess.CompletedProcess(
                argv, 143, out, f"timeout after TERM->grace({grace_seconds}s)->KILL"
            )

    @staticmethod
    def _terminate_group(proc: subprocess.Popen, *, grace_seconds: float) -> None:
        import signal

        try:
            group = os.getpgid(proc.pid)
        except (ProcessLookupError, PermissionError):
            return
        try:
            os.killpg(group, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(max(0.0, grace_seconds))
        try:
            os.killpg(group, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

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
            # literal 比 subpath 精确，Seatbelt 同精确度取首条 → deny 优先于
            # 更宽的写根 allow；persona 文件在可写区内也保持只读。
            lines.extend(persona_denies)
        return "\n".join(lines)

    @staticmethod
    def sandbox_readiness_dict() -> dict[str, Any]:
        """跨平台 readiness 汇总（诊断/CLI 用）。"""
        return {"platform": platform.system()}


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


# LLM: Seatbelt needs explicit deny rules for host control metadata because broader owner-home
# write grants are allowed. Directories use subpath+literal; files use literal.
# 函数用途: 把 forbidden_write_roots 转成比宽泛写入白名单更具体的 macOS 只读规则。
def _readonly_path_denies(paths: tuple[Path, ...]) -> list[str]:
    denies: list[str] = []
    seen: set[Path] = set()
    for raw in paths:
        try:
            path = Path(raw).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        if path in seen or not path.exists():
            continue
        seen.add(path)
        encoded = json.dumps(str(path))
        if path.is_dir():
            denies.append(f"(deny file-write* (literal {encoded}) (subpath {encoded}))")
        else:
            denies.append(f"(deny file-write* (literal {encoded}))")
    return denies


__all__ = [
    "AttemptSandboxSpec",
    "AttemptExecutionSandbox",
    "SandboxUnavailableError",
]
