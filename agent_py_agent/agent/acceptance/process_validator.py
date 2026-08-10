"""SandboxedProcessValidator（3.txt I.8/I.9）。

外部程序验收器通过本网关执行：
- 固定 argv：注册表条目 argv_template + artifact snapshot 路径（I.8，
  模型不能提交任意程序/argv）。
- 最小环境：PATH 白名单 + C locale（不继承宿主全量 env）。
- 禁网：network_access=False（bwrap 无网络接口；Seatbelt deny network-*）。
- 只读 artifact：validator 只读 immutable snapshot（H.9），零写根
  （Linux write_roots=()；macOS deny file-write* 全禁）。
- fail closed（I.9）：平台沙箱不可用 → UNAVAILABLE，绝不退回宿主
  直跑；artifact 不存在 → BLOCKED。

rehearsal/shadow compare（R2 §4 提到可 shadow compare）由调用方用
本网关并行跑对照实现，不在本模块。

"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..tooling.sandbox import (
    SandboxReadiness,
    SandboxSpec,
    SandboxUnavailable,
    build_bwrap_argv,
    probe_sandbox,
)
from .registry import ValidatorEntry

#: I.8 最小环境：固定白名单，不继承宿主全量 env。
MINIMAL_ENV: dict[str, str] = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}

VOP_VERIFIED = "VERIFIED"
VOP_FAILED = "FAILED"
VOP_UNAVAILABLE = "UNAVAILABLE"
VOP_BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class ValidatorOutcome:
    """一次 validator 执行结果（§6 统一要求字段齐备，落 validator_operations）。"""

    status: str
    validator_ref: str
    validator_kind: str
    code_digest: str
    argv: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    artifact_digests: list[str] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    exit_code: int = -1


def validator_platform_ready(*, bwrap_path: str | None = None) -> SandboxReadiness:
    """平台沙箱 readiness（I.9 fail-closed 判定源）。"""
    system = sys.platform
    if system.startswith("linux"):
        return probe_sandbox(bwrap_path=bwrap_path, binary_only=False)
    if system == "darwin":
        sandbox_exec = shutil.which("sandbox-exec")
        if not sandbox_exec:
            return SandboxReadiness(
                False,
                "SEATBELT_NOT_FOUND",
                "macOS 缺少 sandbox-exec（Seatbelt）",
            )
        return SandboxReadiness(True, "SEATBELT_READY", "sandbox-exec 存在")
    return SandboxReadiness(
        False,
        "SANDBOX_UNSUPPORTED_PLATFORM",
        f"当前平台无 validator 沙箱: {sys.platform}",
    )


def _read_only_argv(
    *,
    entry: ValidatorEntry,
    artifact_snapshot: Path,
    owner_home: Path,
) -> list[str] | None:
    """构造全只读包装 argv。平台无沙箱 → None（调用方落 UNAVAILABLE）。"""
    readiness = validator_platform_ready()
    if not readiness.ready:
        return None
    if sys.platform.startswith("linux"):
        spec = SandboxSpec(
            owner_home=Path(owner_home),
            workspace=Path(artifact_snapshot),
            write_roots=(),  # 全只读：validator 零写权限（I.8/H.9）
            network_access=False,  # 禁网
        )
        return [*build_bwrap_argv(spec), "--", *entry.argv_template, str(artifact_snapshot)]
    # macOS Seatbelt：默认放行读，禁一切文件写 + 禁网络。
    profile = (
        "(version 1)\n"
        "(allow default)\n"
        "(deny file-write*)\n"
        "(deny network*)\n"
    )
    sandbox_exec = shutil.which("sandbox-exec")
    if not sandbox_exec:
        return None
    return [
        sandbox_exec,
        "-p",
        profile,
        "--",
        *entry.argv_template,
        str(artifact_snapshot),
    ]


def run_process_validator(
    *,
    entry: ValidatorEntry,
    artifact_snapshot: Path,
    owner_home: Path,
    artifact_digests: list[str] | None = None,
    timeout: float = 60.0,
) -> ValidatorOutcome:
    """在只读沙箱内执行固定 argv 的外部验收器（I.8/I.9）。

    返回 ValidatorOutcome，不抛异常（fail-closed 状态进结果）：
    - UNAVAILABLE：平台缺沙箱（handler=0 语义的 validator 侧落地）。
    - BLOCKED：snapshot 不存在/不可读等执行前阻断。
    - VERIFIED / FAILED：exit_code==0 / !=0。
    """
    snapshot = Path(artifact_snapshot)
    ref = str(entry.name or "")
    base = ValidatorOutcome(
        status=VOP_BLOCKED,
        validator_ref=ref,
        validator_kind=str(entry.kind or ""),
        code_digest=str(entry.code_digest or ""),
        artifact_digests=list(artifact_digests or []),
        env=dict(MINIMAL_ENV),
    )
    if entry.kind != "process":
        return replace(
            base,
            stderr=f"validator {ref!r} 不是 process 类型（I.8）",
        )
    if not snapshot.exists():
        return replace(
            base,
            stderr=f"artifact snapshot 不存在: {snapshot}（H.9）",
        )
    argv = _read_only_argv(entry=entry, artifact_snapshot=snapshot, owner_home=Path(owner_home))
    if argv is None:
        readiness = validator_platform_ready()
        return replace(
            base,
            status=VOP_UNAVAILABLE,
            stderr=f"SANDBOX_UNAVAILABLE: {readiness.code}: {readiness.detail}",
        )
    try:
        proc = subprocess.run(
            argv,
            env=MINIMAL_ENV,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return replace(
            base,
            status=VOP_FAILED,
            stderr=f"validator 超时（{timeout}s）",
        )
    except OSError as exc:
        return replace(
            base,
            status=VOP_FAILED,
            stderr=f"validator 启动失败: {exc}",
        )
    return ValidatorOutcome(
        status=VOP_VERIFIED if proc.returncode == 0 else VOP_FAILED,
        validator_ref=ref,
        validator_kind=str(entry.kind or ""),
        code_digest=str(entry.code_digest or ""),
        argv=list(proc.args),
        env=dict(MINIMAL_ENV),
        artifact_digests=list(artifact_digests or []),
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        exit_code=proc.returncode,
    )
