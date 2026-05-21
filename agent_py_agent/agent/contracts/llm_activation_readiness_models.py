# LLM: LLM activation readiness models keep the public runner module thin.
# 模块用途: 定义 LLM 上场前总闸门的请求、阶段、报告和统计 helper。

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .pre_real_task_validation import PreRealTaskValidationReport


# LLM: LLMActivationReadinessRequest carries activation-gate inputs without prompt prose.
# 类用途: 描述总闸门输出目录和前置 1-6 预真实任务报告。
@dataclass(frozen=True)
class LLMActivationReadinessRequest:
    workspace: Path
    pre_real_report: PreRealTaskValidationReport | None = None


# LLM: LLMActivationReadinessPhase records one pre-live-model phase.
# 类用途: 保存阶段 id、状态、证据引用和结构化问题码。
@dataclass(frozen=True)
class LLMActivationReadinessPhase:
    phase_id: str
    status: str
    evidence_refs: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    # LLM: to_dict serializes one readiness phase for files and CLI callers.
    # 函数用途: 输出稳定机器字段，不内联 prompt 正文或工具大输出。
    def to_dict(self) -> dict[str, object]:
        return {
            "phase_id": self.phase_id,
            "status": self.status,
            "evidence_refs": list(self.evidence_refs),
            "issues": list(self.issues),
        }


# LLM: LLMActivationReadinessReport summarizes the seven activation gates.
# 类用途: 保存总闸门是否通过、阶段统计、报告引用和阶段列表。
@dataclass(frozen=True)
class LLMActivationReadinessReport:
    ok: bool
    summary: dict[str, int]
    report_ref: str
    phases: tuple[LLMActivationReadinessPhase, ...]

    # LLM: to_dict keeps activation reports refs-first and replay-friendly.
    # 函数用途: 将总报告转为 JSON 友好的结构，供测试、CI 和审计读取。
    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "summary": dict(self.summary),
            "report_ref": self.report_ref,
            "phases": [phase.to_dict() for phase in self.phases],
        }


# LLM: readiness_phase maps boolean contract checks to stable phase records.
# 函数用途: 统一生成 PASSED/FAILED 阶段对象。
def readiness_phase(
    phase_id: str,
    ok: bool,
    evidence_refs: list[str],
    issues: list[str],
) -> LLMActivationReadinessPhase:
    return LLMActivationReadinessPhase(
        phase_id=phase_id,
        status="PASSED" if ok else "FAILED",
        evidence_refs=evidence_refs,
        issues=issues,
    )


# LLM: readiness_summary counts phases by machine status.
# 函数用途: 生成 failed/passed/total 摘要，只读取结构化 status 字段。
def readiness_summary(phases: tuple[LLMActivationReadinessPhase, ...]) -> dict[str, int]:
    return {
        "failed": sum(phase.status == "FAILED" for phase in phases),
        "passed": sum(phase.status == "PASSED" for phase in phases),
        "total": len(phases),
    }


__all__ = [
    "LLMActivationReadinessPhase",
    "LLMActivationReadinessReport",
    "LLMActivationReadinessRequest",
    "readiness_phase",
    "readiness_summary",
]
