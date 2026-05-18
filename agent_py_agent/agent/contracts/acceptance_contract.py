# LLM: Acceptance contract combines state, tests, and artifact reports into one machine gate.
# 模块用途: 统一判断任务是否真的完成，避免主代理/子代理各自用自然语言声明“已完成”。

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .artifact_acceptance import ArtifactAcceptanceReport
from .state_machine import RunStateFacts, can_closeout


# LLM: AcceptanceContract is the task-level definition of done.
# 类用途: 保存验收条目、约束、最近测试和必须产出的 artifact 类型。
@dataclass(frozen=True)
class AcceptanceContract:
    items: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    latest_tests: list[str] = field(default_factory=list)
    required_artifact_kinds: list[str] = field(default_factory=list)
    reserved: dict[str, Any] = field(default_factory=dict)


# LLM: AcceptanceInput is the complete fact bundle for final completion decisions.
# 类用途: 汇总验收合同、产物报告、测试记录和状态机事实，供统一验收入口消费。
@dataclass(frozen=True)
class AcceptanceInput:
    contract: AcceptanceContract
    artifact_reports: list[ArtifactAcceptanceReport] = field(default_factory=list)
    test_records: list[Any] = field(default_factory=list)
    run_state: RunStateFacts = field(default_factory=lambda: RunStateFacts(status="PLANNING"))


# LLM: AcceptanceResult is a refs-first completion verdict.
# 类用途: 保存是否通过、状态和结构化 findings，后续 repair/QA/UI 都读这个结果。
@dataclass(frozen=True)
class AcceptanceResult:
    ok: bool
    status: str
    findings: list[dict[str, Any]] = field(default_factory=list)

    # LLM: to_dict keeps the acceptance verdict stable for JSON reports.
    # 函数用途: 转成普通 dict，避免调用方依赖 dataclass 内部结构。
    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "status": self.status, "findings": list(self.findings)}


# LLM: evaluate_acceptance_contract is the single machine gate for task completion.
# 函数用途: 用状态机、真实测试和产物验收一起判断是否完成；不执行工具、不读取大正文。
def evaluate_acceptance_contract(request: AcceptanceInput) -> AcceptanceResult:
    findings = [
        _state_finding(request.run_state),
        _artifact_finding(request.contract, request.artifact_reports),
        _test_finding(request.test_records),
        _criteria_finding(request.contract),
    ]
    ok = all(item["ok"] for item in findings if item["severity"] == "hard")
    return AcceptanceResult(ok=ok, status="accepted" if ok else "rejected", findings=findings)


# LLM: _state_finding requires DONE/VERIFIED before final completion.
# 函数用途: 把状态机是否允许收口转成验收 finding。
def _state_finding(run_state: RunStateFacts) -> dict[str, Any]:
    ok = can_closeout(run_state)
    return {
        "code": "ACCEPTANCE_STATE_OK" if ok else "ACCEPTANCE_STATE_INCOMPLETE",
        "ok": ok,
        "severity": "hard",
        "message": "run state is DONE/VERIFIED" if ok else "run state is not DONE/VERIFIED",
    }


# LLM: _artifact_finding checks required artifact kinds against machine validation reports.
# 函数用途: 确认必须产物类型都存在且验收通过，失败时给 repair 明确缺什么。
def _artifact_finding(contract: AcceptanceContract, reports: list[ArtifactAcceptanceReport]) -> dict[str, Any]:
    passed_kinds = {item.artifact_kind for item in reports if item.ok}
    required = {str(item) for item in contract.required_artifact_kinds if str(item).strip()}
    missing = sorted(required - passed_kinds)
    failed = [item.artifact_ref for item in reports if not item.ok]
    ok = not missing and not failed
    return {
        "code": "ACCEPTANCE_ARTIFACTS_OK" if ok else "ACCEPTANCE_ARTIFACTS_FAILED",
        "ok": ok,
        "severity": "hard",
        "message": "required artifacts passed" if ok else "required artifacts missing or failed",
        "missing_artifact_kinds": missing,
        "failed_artifact_refs": failed,
    }


# LLM: _test_finding treats executed failing records as hard blockers.
# 函数用途: 汇总真实测试记录；没有测试时只软提示，不让简单文件任务被空测试误杀。
def _test_finding(records: list[Any]) -> dict[str, Any]:
    failed = [getattr(item, "test_name", "") for item in records if not bool(getattr(item, "passed", False))]
    ok = not failed
    return {
        "code": "ACCEPTANCE_TESTS_OK" if ok else "ACCEPTANCE_TESTS_FAILED",
        "ok": ok,
        "severity": "hard" if failed else "soft",
        "message": "tests passed" if ok else "some tests failed",
        "failed_tests": failed,
    }


# LLM: _criteria_finding records whether the contract carried explicit completion criteria.
# 函数用途: 把验收条目是否存在写进结果；缺失时为软提示，避免假装没有风险。
def _criteria_finding(contract: AcceptanceContract) -> dict[str, Any]:
    ok = bool(contract.items or contract.required_artifact_kinds or contract.latest_tests)
    return {
        "code": "ACCEPTANCE_CRITERIA_RECORDED" if ok else "ACCEPTANCE_CRITERIA_MISSING",
        "ok": ok,
        "severity": "soft",
        "message": "acceptance criteria recorded" if ok else "acceptance criteria not recorded",
    }


__all__ = [
    "AcceptanceContract",
    "AcceptanceInput",
    "AcceptanceResult",
    "evaluate_acceptance_contract",
]
