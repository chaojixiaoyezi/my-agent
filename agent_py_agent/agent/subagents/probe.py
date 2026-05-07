# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

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


# LLM: ProbeCheckParams 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存probe检查参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class ProbeCheckParams:

    severity: str
    evidence_path: str = ""
    created_at: float = 0.0
    error: str = ""


# LLM: _probe_ok 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理probeok相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
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


# LLM: _probe_fail 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理probefail相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
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


# LLM: _probe_json_file 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理probeJSON文件相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
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


# LLM: _probe_writable_dir 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理probewritabledir相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
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


# LLM: _channel_status 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理通道状态相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
def _channel_status(checks: list[ChannelProbeCheck]) -> str:
    """根据检查项计算通道状态。"""

    if not checks:
        return "UNKNOWN"
    if any(not check.ok and check.severity == "P0" for check in checks):
        return "BROKEN"
    if any(not check.ok for check in checks):
        return "DEGRADED"
    return "OK"

