# LLM: Parent acceptance controller plans upper-agent decisions without executing tests or mutating tasks.
# 模块用途: 读取子代理任务事实源，生成父级验收 dry-run 决策；这里只做判断，不跑命令、不改状态。
from __future__ import annotations

"""Dry-run decision model for parent-controlled subagent acceptance."""

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from .execution_executor import TestExecutor
from .execution_report import load_test_execution_report
from .models import SubAgentTask
from .parsing import _dict_list
from .utils import _read_json_object


# LLM: ParentAcceptanceRef is a compact pointer to a task-local fact source, not a file body.
# 类用途: 给父级决策记录保存证据引用类型和路径；它只定位事实源，不读取大正文。
@dataclass(frozen=True)
class ParentAcceptanceRef:
    """Reference to a fact source used by a parent acceptance decision."""

    __test__: ClassVar[bool] = False

    kind: str
    path: str
    summary: str = ""

    # LLM: to_dict keeps decision serialization stable for future report files.
    # 函数用途: 把引用转成 JSON 友好字典；不会检查路径是否存在。
    def to_dict(self) -> dict[str, str]:
        return asdict(self)


# LLM: ParentAcceptanceDecision is a dry-run plan; callers must explicitly apply any action later.
# 类用途: 表示父代理对一个子代理验收的下一步判断；当前不会自动执行 tests、rescue 或状态写回。
@dataclass(frozen=True)
class ParentAcceptanceDecision:
    """Dry-run decision from the parent acceptance controller."""

    __test__: ClassVar[bool] = False

    run_id: str
    decision: str
    reason: str
    risk_level: str = "low"
    requires_human_confirmation: bool = False
    evidence_refs: list[ParentAcceptanceRef] = field(default_factory=list)
    test_execution_ref: str = ""
    failure_handoff_ref: str = ""
    takeover_readiness_ref: str = ""
    next_actions: list[str] = field(default_factory=list)
    reserved: dict[str, Any] = field(default_factory=dict)

    # LLM: to_dict is the machine-facing shape that later report persistence can reuse.
    # 函数用途: 把父级验收决策转成 JSON 字典；保持 refs-only，不展开 artifact 或 report 正文。
    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["evidence_refs"] = [ref.to_dict() for ref in self.evidence_refs]
        return payload


# LLM: TestReportDecisionInput keeps test-report decision expansion bundle-shaped.
# 类用途: 保存基于 `test_execution.json` 继续决策所需的事实源，避免 helper 参数继续增长。
@dataclass(frozen=True)
class TestReportDecisionInput:
    """Bundle for deriving a parent decision from a test execution report."""

    __test__: ClassVar[bool] = False

    task: SubAgentTask
    output: dict[str, Any]
    refs: list[ParentAcceptanceRef]
    report_path: Path
    recovery_refs: tuple[str, str]


# LLM: build_parent_acceptance_decision is side-effect free and only reads task-local JSON facts.
# 函数用途: 根据 output、test_execution 和失败交接线索生成父级验收下一步；不会写文件、不会跑命令。
def build_parent_acceptance_decision(
    task: SubAgentTask,
    *,
    workspace_root: str | Path,
) -> ParentAcceptanceDecision:
    """Build a dry-run parent acceptance decision for one subagent task."""

    output = _read_json_object(Path(task.output_json))
    tests = _dict_list(output.get("tests", []))
    executable_tests, ignored_empty_command_count = _executable_tests(tests)
    refs = _base_refs(task, tests, executable_tests, ignored_empty_command_count)
    report_path = Path(task.reports_dir) / "test_execution.json"
    failure_ref = _existing_path(Path(task.reports_dir) / "failure_handoff.json")
    takeover_ref = _existing_path(Path(task.reports_dir) / "takeover_readiness.json")

    if _task_is_failed(task):
        return _rescue_for_task_failure(task, refs, failure_ref, takeover_ref)

    unsafe_reason = _unsafe_test_reason(executable_tests, workspace_root)
    if unsafe_reason:
        return _request_human_for_unsafe_test(task, refs, unsafe_reason)

    if executable_tests and not report_path.exists():
        return _execute_tests_for_missing_report(task, refs)

    if report_path.exists():
        return _decision_from_test_report(
            TestReportDecisionInput(
                task=task,
                output=output,
                refs=refs,
                report_path=report_path,
                recovery_refs=(failure_ref, takeover_ref),
            )
        )

    return _inspect_without_tests(task, output, refs)


# LLM: write_parent_acceptance_decision_file persists the dry-run decision as an audit artifact only.
# 函数用途: 写入父级验收决策 JSON；只保存 refs-only 决策，不执行 tests、不修改 task 状态。
def write_parent_acceptance_decision_file(
    task: SubAgentTask,
    decision: ParentAcceptanceDecision,
    *,
    generated_at: float | None = None,
) -> Path:
    path = Path(task.reports_dir) / "parent_acceptance_decision.json"
    payload = {
        "schema": "parent_acceptance_decision.v1",
        "generated_at": generated_at if generated_at is not None else time.time(),
        "dry_run": True,
        "run_id": task.id,
        "decision": decision.to_dict(),
        "reserved": {
            "future_apply_supported": True,
            "refs_only": True,
            "reads_artifact_bodies": False,
            "mutates_task_state": False,
        },
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


# LLM: _rescue_for_task_failure builds the conservative decision for already failed task states.
# 函数用途: 失败或阻塞任务不继续普通验收，先让父代理读取 handoff/takeover refs。
def _rescue_for_task_failure(
    task: SubAgentTask,
    refs: list[ParentAcceptanceRef],
    failure_ref: str,
    takeover_ref: str,
) -> ParentAcceptanceDecision:
    return ParentAcceptanceDecision(
        run_id=task.id,
        decision="rescue",
        reason="task is failed or blocked; parent should inspect handoff refs before acceptance",
        risk_level="medium",
        evidence_refs=refs,
        failure_handoff_ref=failure_ref,
        takeover_readiness_ref=takeover_ref,
        next_actions=["read failure handoff refs", "plan rescue or escalate"],
    )


# LLM: _request_human_for_unsafe_test records the safety stop without evaluating the command body.
# 函数用途: 命令预检高风险时要求人工确认或改写测试命令；不执行命令。
def _request_human_for_unsafe_test(
    task: SubAgentTask,
    refs: list[ParentAcceptanceRef],
    unsafe_reason: str,
) -> ParentAcceptanceDecision:
    return ParentAcceptanceDecision(
        run_id=task.id,
        decision="request_human",
        reason=unsafe_reason,
        risk_level="high",
        requires_human_confirmation=True,
        evidence_refs=refs,
        next_actions=["ask human to confirm or rewrite unsafe test command"],
    )


# LLM: _execute_tests_for_missing_report recommends explicit test execution but does not run it.
# 函数用途: 子代理声明 tests 但缺少真实执行报告时，提示父代理下一步执行验收测试。
def _execute_tests_for_missing_report(
    task: SubAgentTask,
    refs: list[ParentAcceptanceRef],
) -> ParentAcceptanceDecision:
    return ParentAcceptanceDecision(
        run_id=task.id,
        decision="execute_tests",
        reason="output.json declares tests but reports/test_execution.json is missing",
        risk_level="low",
        evidence_refs=refs,
        next_actions=["run explicit parent acceptance tests", "write test_execution.json"],
    )


# LLM: _decision_from_test_report turns a persisted machine report into a parent next step.
# 函数用途: 根据 `test_execution.json` 汇总通过或失败；只读取 JSON 事实源，不读 Markdown。
def _decision_from_test_report(params: TestReportDecisionInput) -> ParentAcceptanceDecision:
    task = params.task
    report = load_test_execution_report(params.report_path)
    failure_ref, takeover_ref = params.recovery_refs
    test_ref = ParentAcceptanceRef("test_execution", str(params.report_path), "test execution report")
    if report.total_tests > 0 and report.failed == 0:
        patch_review = _patch_review_decision(task, params.output, [*params.refs, test_ref])
        if patch_review is not None:
            return patch_review
        return ParentAcceptanceDecision(
            run_id=task.id,
            decision="inspect_only",
            reason="real test execution already passed; parent can continue normal acceptance inspection",
            risk_level="low",
            evidence_refs=[*params.refs, test_ref],
            test_execution_ref=str(params.report_path),
            next_actions=["run parent acceptance review without re-running tests"],
        )
    return ParentAcceptanceDecision(
        run_id=task.id,
        decision="rescue",
        reason=f"real test execution failed: total={report.total_tests} failed={report.failed}",
        risk_level="medium",
        evidence_refs=[*params.refs, test_ref],
        test_execution_ref=str(params.report_path),
        failure_handoff_ref=failure_ref,
        takeover_readiness_ref=takeover_ref,
        next_actions=["inspect test_execution.json", "plan retry, rescue, or escalate"],
    )


# LLM: _patch_review_decision keeps passed tests from skipping the explicit patch-review gate.
# 函数用途: 测试通过但存在未审核 applied patch 时，先建议父代理执行 patch review，不直接进入 apply 验收。
def _patch_review_decision(
    task: SubAgentTask,
    output: dict[str, Any],
    refs: list[ParentAcceptanceRef],
) -> ParentAcceptanceDecision | None:
    unreviewed = _unreviewed_applied_patches(output)
    if not unreviewed:
        return None
    return ParentAcceptanceDecision(
        run_id=task.id,
        decision="review_patches",
        reason=f"{len(unreviewed)} applied patch requires parent patch review before acceptance",
        risk_level="low",
        evidence_refs=refs,
        next_actions=["run explicit parent patch review", "re-run parent acceptance decision"],
        reserved={"unreviewed_applied_patch_count": len(unreviewed)},
    )


# LLM: _unreviewed_applied_patches reads only output.json patch metadata.
# 函数用途: 找出 status=applied 但 review_status 不是 APPROVED 的 patch，供父级流程明确下一步。
def _unreviewed_applied_patches(output: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in _dict_list(output.get("patches", []))
        if str(item.get("status", "")).lower() == "applied"
        and str(item.get("review_status", "")).upper() != "APPROVED"
    ]


# LLM: _inspect_without_tests is the safe default for runs that provide no executable validation items.
# 函数用途: 没有 tests 时不假装通过，只建议继续普通父级验收检查 evidence/verifier。
def _inspect_without_tests(
    task: SubAgentTask,
    output: dict[str, Any],
    refs: list[ParentAcceptanceRef],
) -> ParentAcceptanceDecision:
    patch_review = _patch_review_decision(task, output, refs)
    if patch_review is not None:
        return patch_review
    return ParentAcceptanceDecision(
        run_id=task.id,
        decision="inspect_only",
        reason="no executable tests were declared; parent should inspect evidence and verifier findings",
        risk_level="low",
        evidence_refs=refs,
        next_actions=["run parent acceptance review"],
    )


# LLM: _base_refs only records small fact-source pointers used by the parent decision.
# 函数用途: 收集 output 和已有 report 的路径引用；保持 refs-only，不读取 artifact 正文。
def _base_refs(
    task: SubAgentTask,
    tests: list[dict[str, Any]],
    executable_tests: list[dict[str, Any]],
    ignored_empty_command_count: int,
) -> list[ParentAcceptanceRef]:
    summary = f"tests={len(tests)} executable_tests={len(executable_tests)}"
    if ignored_empty_command_count:
        summary = f"{summary} ignored_empty_command_tests={ignored_empty_command_count}"
    refs = [ParentAcceptanceRef("output", str(task.output_json), summary)]
    runner_path = Path(task.runner_result_json)
    if runner_path.exists():
        refs.append(ParentAcceptanceRef("runner_result", str(runner_path), "runner result fact source"))
    return refs


# LLM: _executable_tests drops empty generated command placeholders so acceptance does not request humans for noise.
# 函数用途: 把 tests 中真正可执行的检查筛出来；空 command 只作为被忽略事实进入 refs，不当成高风险命令。
def _executable_tests(tests: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    executable: list[dict[str, Any]] = []
    ignored_empty_command_count = 0
    for item in tests:
        method = str(item.get("validation_method") or "command").strip() or "command"
        command = str(item.get("command") or "").strip()
        if method == "command" and not command:
            ignored_empty_command_count += 1
            continue
        executable.append(item)
    return executable, ignored_empty_command_count


# LLM: _task_is_failed maps task terminal risk states into a conservative parent rescue decision.
# 函数用途: 判断任务是否已经失败、阻塞或带失败类型；这些状态不应直接进入通过验收。
def _task_is_failed(task: SubAgentTask) -> bool:
    status = str(task.status or "").upper()
    verification = str(task.verification_status or "").upper()
    return bool(task.failure_type) or status in {"FAILED", "ERROR", "TIMEOUT", "BLOCKED"} or verification == "FAILED"


# LLM: _unsafe_test_reason preflights command syntax through TestExecutor validation without running it.
# 函数用途: 检查 tests 里的命令是否触发 allowlist 或 shell 字符风险；只做预检，不执行命令。
def _unsafe_test_reason(tests: list[dict[str, Any]], workspace_root: str | Path) -> str:
    executor = TestExecutor(workspace_root)
    for item in tests:
        method = str(item.get("validation_method") or "command").strip() or "command"
        if method != "command":
            continue
        command = str(item.get("command") or "").strip()
        error = executor._validate_command(command)
        if error:
            name = str(item.get("name") or command or "unknown").strip()
            return f"test command requires human confirmation: {name}: {error}"
    return ""


# LLM: _existing_path returns a string ref only when the task-local fact file exists.
# 函数用途: 生成可选 report 引用；不存在时返回空字符串，避免调用方误以为已有事实源。
def _existing_path(path: Path) -> str:
    return str(path) if path.exists() else ""
