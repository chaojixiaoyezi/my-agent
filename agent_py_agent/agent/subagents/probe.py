from __future__ import annotations

"""LLM contract: channel probe check constructors and low-level probe helpers.

Human version:
这些函数只负责检查通道现场，比如 JSON 文件能不能读、目录能不能写。
它们不决定任务状态怎么流转，只产出 probe check。
"""

import json
from dataclasses import dataclass
from pathlib import Path

from .models import ChannelProbeCheck


@dataclass(frozen=True)
class ProbeCheckParams:
    """LLM: bundle probe check metadata for ok/fail constructors."""

    severity: str
    evidence_path: str = ""
    created_at: float = 0.0
    error: str = ""


def _probe_ok(
    name: str,
    summary: str,
    *,
    params: ProbeCheckParams | None = None,
    severity: str,
    evidence_path: str = "",
    created_at: float,
) -> ChannelProbeCheck:
    """创建成功的 probe check。"""
    params = params or ProbeCheckParams(severity=severity, evidence_path=evidence_path, created_at=created_at)

    return ChannelProbeCheck(
        name=name,
        ok=True,
        summary=summary,
        severity=params.severity,
        evidence_path=params.evidence_path,
        created_at=params.created_at,
    )


def _probe_fail(
    name: str,
    summary: str,
    *,
    params: ProbeCheckParams | None = None,
    severity: str,
    error: str,
    evidence_path: str = "",
    created_at: float,
) -> ChannelProbeCheck:
    """创建失败的 probe check。"""
    params = params or ProbeCheckParams(
        severity=severity,
        evidence_path=evidence_path,
        created_at=created_at,
        error=error,
    )

    return ChannelProbeCheck(
        name=name,
        ok=False,
        summary=summary,
        severity=params.severity,
        evidence_path=params.evidence_path,
        error=params.error,
        created_at=params.created_at,
    )


def _probe_json_file(
    name: str,
    path: Path,
    severity: str,
    created_at: float,
) -> ChannelProbeCheck:
    """检查机器 JSON 是否存在且可读。"""

    try:
        json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return _probe_fail(
            name,
            "机器 JSON 不可读或格式不正确。",
            severity=severity,
            error=str(exc),
            evidence_path=str(path),
            created_at=created_at,
        )
    return _probe_ok(
        name,
        "机器 JSON 可读。",
        severity=severity,
        evidence_path=str(path),
        created_at=created_at,
    )


def _probe_writable_dir(
    name: str,
    directory: Path,
    severity: str,
    created_at: float,
) -> ChannelProbeCheck:
    """检查目录是否可写，并留下一个轻量证据文件。"""

    probe_file = directory / f"channel_probe_{int(created_at)}.txt"
    try:
        if not directory.exists():
            raise FileNotFoundError(f"目录不存在: {directory}")
        probe_file.write_text(
            f"channel probe ok at {created_at}\n",
            encoding="utf-8",
        )
    except Exception as exc:
        return _probe_fail(
            name,
            "目录不可写。",
            severity=severity,
            error=str(exc),
            evidence_path=str(probe_file),
            created_at=created_at,
        )
    return _probe_ok(
        name,
        "目录可写。",
        severity=severity,
        evidence_path=str(probe_file),
        created_at=created_at,
    )


def _channel_status(checks: list[ChannelProbeCheck]) -> str:
    """根据检查项计算通道状态。"""

    if not checks:
        return "UNKNOWN"
    if any(not check.ok and check.severity == "P0" for check in checks):
        return "BROKEN"
    if any(not check.ok for check in checks):
        return "DEGRADED"
    return "OK"

