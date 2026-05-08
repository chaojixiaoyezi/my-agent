# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""single-task acceptance review helper for SubAgentAcceptanceMixin.

给人看的解释：
验收一条任务时既要读 runner 输出又可能写回状态，拆出后 mixin 保持薄门面。
"""

import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar

# LLM: imports stay ruff-clean because this service is shared by CLI, manager facades, and CI acceptance tests.
from .acceptance_review_verifier import build_verifier_checks
from .acceptance_test_execution import (
    AcceptanceTestExecutionRequest,
    build_acceptance_test_execution_findings,
)
from .models import SubAgentTask
from .parsing import _dict_list
from .reports import AcceptanceReviewFinding, AcceptanceReviewRecord
from .utils import _new_id, _read_json_object


# LLM: AcceptanceReviewOptions 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存验收审查选项字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class AcceptanceReviewOptions:
    """Options bundle for acceptance review report entrypoints."""

    __test__: ClassVar[bool] = False

    # LLM: report-level acceptance options stay bundled while per-task review uses a request.
    apply: bool = False
    reviewer: str = "parent"
    note: str = ""
    limit: int = 0
    now: float | None = None
    # LLM: execute_tests is explicit opt-in; default acceptance must not start running user commands.
    execute_tests: bool = False
    test_timeout_seconds: float = 120.0

    # LLM: from_values 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 转换values的数据表示，保持跨模块传递时的字段含义一致；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    @classmethod
    def from_values(
        cls,
        options: AcceptanceReviewOptions | None = None,
        *,
        apply: bool | None = None,
        reviewer: str | None = None,
        note: str | None = None,
        limit: int | None = None,
        now: float | None = None,
        execute_tests: bool | None = None,
        test_timeout_seconds: float | None = None,
    ):
        base = options or cls()
        updates = {
            "apply": apply,
            "reviewer": reviewer,
            "note": note,
            "limit": limit,
            "now": now,
            "execute_tests": execute_tests,
            "test_timeout_seconds": test_timeout_seconds,
        }
        clean = {key: value for key, value in updates.items() if value is not None}
        return replace(base, **clean)


# LLM: acceptance_review_options 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理验收审查选项相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
def acceptance_review_options(
    options: AcceptanceReviewOptions | None = None,
    *,
    apply: bool = False,
    reviewer: str = "parent",
    note: str = "",
    limit: int = 0,
) -> AcceptanceReviewOptions:
    """Coerce legacy fields into the report-level acceptance options bundle."""

    if options is not None and (apply, reviewer, note, limit) == (False, "parent", "", 0):
        return options
    return AcceptanceReviewOptions.from_values(
        options,
        apply=apply,
        reviewer=reviewer,
        note=note,
        limit=limit,
    )


# LLM: AcceptanceReviewRequest 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存验收审查请求字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
@dataclass(frozen=True)
class AcceptanceReviewRequest:

    task: SubAgentTask
    apply: bool = False
    reviewer: str = "parent"
    note: str = ""
    now: float | None = None
    # LLM: request carries real-test execution options through to findings without changing legacy defaults.
    execute_tests: bool = False
    test_timeout_seconds: float = 120.0


# LLM: AcceptanceReviewInputs 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存验收审查inputs字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
@dataclass(frozen=True)
class AcceptanceReviewInputs:

    output: dict
    runner: dict
    findings: list[AcceptanceReviewFinding]
    verifier_checks: list[AcceptanceReviewFinding]


# LLM: AcceptanceDecisionRequest 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存验收decision请求字段，让调用方按同一参数包传递上下文；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
@dataclass(frozen=True)
class AcceptanceDecisionRequest:

    ok: bool
    message: str
    now: float


# LLM: review_acceptance_task 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理审查验收任务相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
def review_acceptance_task(manager, request: AcceptanceReviewRequest) -> AcceptanceReviewRecord:
    """对单个任务执行验收判断，并按需写回状态。"""
    task = request.task
    now = request.now if request.now is not None else time.time()
    before_status = task.status
    before_verification = task.verification_status
    inputs = _acceptance_review_inputs(manager, request, now)
    review_checks = [*inputs.findings, *inputs.verifier_checks]
    ok = all(item.ok or item.severity == "P2" for item in review_checks)
    ready = task.status == "AWAITING_ACCEPTANCE" or task.verification_status == "NEEDS_ACCEPTANCE"
    decision = "ACCEPT" if ok else "REJECT"
    message = _acceptance_message(ok, review_checks)
    applied = False

    if request.apply and ready:
        _apply_acceptance_decision(manager, task, request=AcceptanceDecisionRequest(ok, message, now))
        applied = True
        manager._append_task_work_log(
            task,
            f"acceptance_review: decision={decision} reviewer={request.reviewer} message={message}",
        )
    elif request.apply and not ready:
        message = f"任务当前状态不在等待验收范围内，未写回: status={task.status} verify={task.verification_status}"

    return _acceptance_record(
        request,
        inputs,
        review_checks=review_checks,
        decision=decision,
        message=message,
        before_status=before_status,
        before_verification=before_verification,
        applied=applied,
        ok=ok,
        now=now,
    )


# LLM: _acceptance_review_inputs writes opt-in real test reports before findings so acceptance reads machine facts.
# 函数用途: 汇总验收输入；显式执行 tests 时先写 test_execution.json，再让普通 findings 以机器报告为测试事实源。
def _acceptance_review_inputs(manager, request: AcceptanceReviewRequest, now: float) -> AcceptanceReviewInputs:
    task = request.task
    output = _read_json_object(Path(task.output_json))
    runner = _read_json_object(Path(task.runner_result_json))
    test_findings: list[AcceptanceReviewFinding] = []
    if request.execute_tests:
        test_findings.extend(build_acceptance_test_execution_findings(
            AcceptanceTestExecutionRequest(manager, task, output, request, now)
        ))
    findings = manager.acceptance_findings(task, output, runner, now)
    findings.extend(test_findings)
    return AcceptanceReviewInputs(
        output=output,
        runner=runner,
        findings=findings,
        verifier_checks=build_verifier_checks(task, now),
    )


# LLM: _acceptance_record 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理验收记录相关的数据流，连接当前职责的前后步骤；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
def _acceptance_record(
    request: AcceptanceReviewRequest,
    inputs: AcceptanceReviewInputs,
    *,
    review_checks: list[AcceptanceReviewFinding],
    decision: str,
    message: str,
    before_status: str,
    before_verification: str,
    applied: bool,
    ok: bool,
    now: float,
) -> AcceptanceReviewRecord:
    task = request.task
    return AcceptanceReviewRecord(
        id=_new_id("accept"),
        run_id=task.id,
        dry_run=not request.apply,
        applied=applied,
        ok=ok,
        decision=decision,
        message=message,
        before_status=before_status,
        after_status=task.status,
        before_verification_status=before_verification,
        after_verification_status=task.verification_status,
        reviewer=request.reviewer,
        note=request.note,
        evidence_count=len(task.evidence),
        test_count=len(_dict_list(inputs.output.get("tests", []))),
        artifact_count=len(_dict_list(inputs.output.get("artifacts", []))),
        findings=inputs.findings,
        worker_claims=_worker_claims(task, inputs.output, inputs.runner),
        evidence_facts=_evidence_facts(task, inputs.output),
        parent_conclusions=_parent_conclusions(decision, message, review_checks),
        verifier_checks=inputs.verifier_checks,
        evidence_paths=[item.evidence_path for item in review_checks if item.evidence_path],
        created_at=now,
    )


# LLM: _acceptance_message 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理验收消息相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
def _acceptance_message(ok: bool, findings) -> str:
    if ok:
        return "验收通过。"
    failed = [item.message for item in findings if not item.ok and item.severity != "P2"]
    return "验收未通过: " + "；".join(failed[:3])


# LLM: _apply_acceptance_decision 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 更新验收decision对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新任务状态、执行器结果、验收和报告展示，需避免破坏既有状态机约定。
def _apply_acceptance_decision(
    manager,
    task: SubAgentTask,
    *,
    request: AcceptanceDecisionRequest,
) -> None:
    ok = request.ok
    message = request.message
    if ok:
        task.status = "DONE"
        task.verification_status = "VERIFIED"
        task.failure_type = ""
        task.result = task.result or message
    else:
        task.status = "BLOCKED"
        task.verification_status = "FAILED"
        task.failure_type = "acceptance_failed"
        task.result = message
    task.ended_at = request.now
    task.updated_at = request.now
    task.heartbeat_at = request.now
    manager.save(task)


# LLM: _worker_claims 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理工作器claims相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
def _worker_claims(task: SubAgentTask, output: dict, runner: dict) -> list[str]:
    claims: list[str] = []
    for value in [
        output.get("summary"),
        runner.get("message"),
        task.latest_summary,
        task.result,
    ]:
        text = str(value or "").strip()
        if text and text not in claims:
            claims.append(text)
    status = str(output.get("status") or task.status or "").strip()
    if status:
        claims.append(f"worker_status={status}")
    return claims[:8]


# LLM: _evidence_facts 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理证据facts相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
def _evidence_facts(task: SubAgentTask, output: dict) -> list[str]:
    tests = _dict_list(output.get("tests", []))
    artifacts = _dict_list(output.get("artifacts", []))
    facts = [
        f"verification_evidence={len(task.evidence)}",
        f"evidence_packets={len(task.evidence_packets)}",
        f"findings={len(task.findings)}",
        f"tests={len(tests)}",
        f"artifacts={len(artifacts)}",
    ]
    for packet in task.evidence_packets[:5]:
        refs = [*packet.evidence_refs, *packet.artifact_refs]
        facts.append(f"packet:{packet.id or 'none'} claim={packet.claim} refs={len(refs)}")
    return facts


# LLM: _parent_conclusions 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理父级conclusions相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
def _parent_conclusions(
    decision: str,
    message: str,
    checks: list[AcceptanceReviewFinding],
) -> list[str]:
    failed = [item for item in checks if not item.ok and item.severity != "P2"]
    conclusions = [f"decision={decision}", message]
    conclusions.extend(f"{item.severity}:{item.name}" for item in failed[:5])
    return conclusions
