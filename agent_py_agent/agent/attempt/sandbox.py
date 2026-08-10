"""AttemptExecutionSandbox（3.txt E.4-E.8）：attempt 级平台执行网关。

- Linux：bwrap/namespace（复用 agent.tooling.sandbox 的挂载构造与自检）。
- macOS：Seatbelt（sandbox-exec）SBPL profile。
- 共享 workspace / owner 系统状态只读或不可见；仅 attempt view + staging
  可写（E.4/E.5）。
- readiness 失败（E.6）：任何可能写文件的 shell/build/test/child 进程
  handler=0，抛 SandboxUnavailableError（SANDBOX_UNAVAILABLE）；绝不退回
  "只设 cwd" 的宿主 shell（E.7）。
- owner_scope_root 为空、单租户或 admin 不取消 sandbox（E.8）。

R2 建立机制与平台测试；接入工具执行链路归 R4 cutover。
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


class SandboxUnavailableError(RuntimeError):
    """SANDBOX_UNAVAILABLE：节点缺隔离能力时 handler=0 的载体。"""


@dataclass(frozen=True)
class AttemptSandboxSpec:
    """attempt 级沙箱合同：attempt view 与 staging 可写，其余只读/不可见。"""

    attempt_view: Path       # 本 attempt 唯一可写工作根（E.1）
    staging_root: Path       # 发布源 staging 区（H 节），可写
    shared_workspace: Path   # 共享 workspace：只读或不可见（E.4/E.5）
    owner_home: Path         # owner home 底图（只读；persona 文件强制只读）
    network_access: bool = True
    bwrap_path: str | None = None
    macos_sandbox_exec: str | None = None


class AttemptExecutionSandbox:
    """跨平台 attempt 沙箱。按宿主平台选择 Linux bwrap 或 macOS Seatbelt。

    用法：
        sandbox = AttemptExecutionSandbox(spec)
        sandbox.require_ready()          # fail-closed：不可用抛异常
        result = sandbox.run([...argv...], timeout=120)
    """

    def __init__(self, spec: AttemptSandboxSpec):
        self.spec = spec
        self._ready: SandboxReadiness | None = None
        self._platform = platform.system()

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
        以异常拒绝执行实现）。"""
        if self._ready is None:
            self._ready = self.probe()
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

    def _linux_argv(self, command_argv: list[str]) -> list[str]:
        spec = SandboxSpec(
            owner_home=self.spec.owner_home,
            workspace=self.spec.attempt_view,
            write_roots=(self.spec.attempt_view, self.spec.staging_root),
            bwrap_path=self.spec.bwrap_path,
            network_access=self.spec.network_access,
        )
        return [*build_bwrap_argv(spec), "--", *command_argv]

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
    @staticmethod
    def _macos_profile(
        *,
        attempt_view: Path,
        staging: Path,
        shared: Path,
    ) -> str:
        """Seatbelt 策略：默认放行（mach/网络/进程语义同 bwrap：隔离文件为主）
        → 禁所有文件写 → 只对 attempt view + staging（+ /dev 设备）重新开写。
        共享 workspace 保持只读（file-read* 默认放行，file-write* 拒绝）。"""
        write_roots = [str(attempt_view.resolve()), str(staging.resolve())]
        lines = [
            "(version 1)",
            "(allow default)",
            "(deny file-write*)",
            '(allow file-write* (subpath "/dev") (literal "/dev/null")'
            + "".join(f' (subpath {json.dumps(root)})' for root in write_roots)
            + ")",
        ]
        return "\n".join(lines)

    @staticmethod
    def sandbox_readiness_dict() -> dict[str, Any]:
        """跨平台 readiness 汇总（诊断/CLI 用）。"""
        return {"platform": platform.system()}


__all__ = [
    "AttemptSandboxSpec",
    "AttemptExecutionSandbox",
    "SandboxUnavailableError",
]
