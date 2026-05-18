# LLM: Main-agent real task execution models stay separate from subprocess orchestration.
# 模块用途: 定义真实任务受控执行的请求、单项结果和总报告数据结构。

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = "main-agent-real-task-execution.v1"


# LLM: MainAgentRealTaskExecutionRequest bundles execution controls and avoids ad-hoc subprocess args.
# 类用途: 描述真实任务执行入口需要的工作区、并发、超时、配置和 case 过滤条件。
@dataclass(frozen=True)
class MainAgentRealTaskExecutionRequest:
    workspace: Path
    max_workers: int = 4
    task_timeout_seconds: int = 480
    execute: bool = False
    case_ids: tuple[str, ...] = ()
    base_config_path: Path | None = None
    package_root: Path | None = None


# LLM: MainAgentRealTaskExecutionCaseResult records one planned or executed subprocess.
# 类用途: 保存单个主代理真实任务的命令、配置、日志、退出码和问题引用。
@dataclass(frozen=True)
class MainAgentRealTaskExecutionCaseResult:
    case_id: str
    title: str
    status: str
    worker_slot: int
    timeout_seconds: int
    prompt_ref: str
    config_ref: str
    command_ref: str
    stdout_ref: str
    stderr_ref: str
    exit_code: int | None = None
    duration_seconds: float = 0.0
    issues: list[str] = field(default_factory=list)

    # LLM: to_dict keeps execution reports refs-first and bounded.
    # 函数用途: 输出可序列化报告，不内联 stdout/stderr 或 prompt 正文。
    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "title": self.title,
            "status": self.status,
            "worker_slot": self.worker_slot,
            "timeout_seconds": self.timeout_seconds,
            "prompt_ref": self.prompt_ref,
            "config_ref": self.config_ref,
            "command_ref": self.command_ref,
            "stdout_ref": self.stdout_ref,
            "stderr_ref": self.stderr_ref,
            "exit_code": self.exit_code,
            "duration_seconds": round(self.duration_seconds, 3),
            "issues": list(self.issues),
        }


# LLM: MainAgentRealTaskExecutionReport summarizes controlled execution without hiding failures.
# 类用途: 保存真实任务执行总报告；后续 CLI/Card Runtime 可以直接读取。
@dataclass(frozen=True)
class MainAgentRealTaskExecutionReport:
    ok: bool
    schema_version: str
    execution_mode: str
    summary: dict[str, int]
    suite_report_ref: str
    report_ref: str
    cases: list[MainAgentRealTaskExecutionCaseResult]

    # LLM: to_dict gives a stable machine report for CLI and future task cards.
    # 函数用途: 转成 JSON 结构，包含套件报告引用和每个任务的日志引用。
    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "schema_version": self.schema_version,
            "execution_mode": self.execution_mode,
            "summary": dict(self.summary),
            "suite_report_ref": self.suite_report_ref,
            "report_ref": self.report_ref,
            "cases": [case.to_dict() for case in self.cases],
        }


__all__ = [
    "MainAgentRealTaskExecutionCaseResult",
    "MainAgentRealTaskExecutionReport",
    "MainAgentRealTaskExecutionRequest",
    "SCHEMA_VERSION",
]
