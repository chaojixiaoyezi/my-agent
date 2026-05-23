# LLM: Main-agent task suite plans heavyweight model tests with refs and worker slots.
# 模块用途: 定义主代理通用任务批量测试的受控计划入口；默认只落结构化任务合同，不直接启动模型。

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

SCHEMA_VERSION = "main-agent-task-suite.v1"
ACCEPTANCE_SCHEMA_VERSION = "main-agent-task-acceptance.v1"
ARTIFACT_SCHEMA_VERSION = "main-agent-task-artifacts.v1"


# LLM: MainAgentTaskArtifact defines expected deliverables as machine facts.
# 类用途: 描述一个真实任务需要产出的文件类型、推荐路径和验收方式，不靠自然语言判断。
@dataclass(frozen=True)
class MainAgentTaskArtifact:
    artifact_id: str
    kind: str
    preferred_path: str
    validation_contract: dict[str, object]
    required: bool = True

    # LLM: to_dict gives reports and acceptance files one stable artifact shape.
    # 函数用途: 转成 JSON 友好的结构，给 CLI、测试和后续调度器读取。
    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "preferred_path": self.preferred_path,
            "required": self.required,
            "validation_contract": dict(self.validation_contract),
        }


# LLM: MainAgentTaskCase stores the human prompt beside structured machine contracts.
# 类用途: 描述一个真实主代理任务；prompt 只写到文件，人看，系统用 case_id/artifacts/contracts。
@dataclass(frozen=True)
class MainAgentTaskCase:
    case_id: str
    title: str
    user_prompt: str
    artifacts: tuple[MainAgentTaskArtifact, ...]
    acceptance_checks: tuple[dict[str, object], ...]


# LLM: MainAgentTaskSuiteRequest bundles execution controls without exposing many user knobs.
# 类用途: 描述真实任务套件的工作区、并发工位和单任务超时；默认只规划不执行。
@dataclass(frozen=True)
class MainAgentTaskSuiteRequest:
    workspace: Path
    max_workers: int = 4
    task_timeout_seconds: int = 480
    execute: bool = False
    prompt_overrides: dict[str, str] = field(default_factory=dict)


# LLM: MainAgentTaskCasePlan is one planned test task with refs to prompt and contracts.
# 类用途: 保存单个真实任务的计划状态、工位、超时、prompt/验收/产物引用。
@dataclass(frozen=True)
class MainAgentTaskCasePlan:
    case_id: str
    title: str
    status: str
    worker_slot: int
    timeout_seconds: int
    prompt_ref: str
    acceptance_ref: str
    expected_artifacts_ref: str
    issues: list[str] = field(default_factory=list)

    # LLM: to_dict keeps the task report refs-first and prompt-body-free.
    # 函数用途: 输出机器报告字段，不内联大 prompt、网页代码、xlsx 或 PDF 正文。
    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "title": self.title,
            "status": self.status,
            "worker_slot": self.worker_slot,
            "timeout_seconds": self.timeout_seconds,
            "prompt_ref": self.prompt_ref,
            "acceptance_ref": self.acceptance_ref,
            "expected_artifacts_ref": self.expected_artifacts_ref,
            "issues": list(self.issues),
        }


# LLM: MainAgentTaskCasePlanRequest bundles per-case plan writing controls.
# 类用途: 描述单个真实任务计划落盘所需参数，避免 helper 签名继续变长。
@dataclass(frozen=True)
class MainAgentTaskCasePlanRequest:
    root: Path
    case: MainAgentTaskCase
    worker_slot: int
    timeout_seconds: int
    execute: bool


# LLM: MainAgentTaskSuiteReport summarizes the controlled batch without hiding skipped execution.
# 类用途: 保存真实任务批量测试计划；报告可进入 CLI/CI，但默认不会消耗模型 API。
@dataclass(frozen=True)
class MainAgentTaskSuiteReport:
    ok: bool
    schema_version: str
    execution_mode: str
    summary: dict[str, int]
    cases: list[MainAgentTaskCasePlan]
    report_ref: str

    # LLM: to_dict gives the CLI one stable report payload for frontend and docs.
    # 函数用途: 转成 JSON 结构，只包含引用、状态和汇总，适合后续 Card/Message Runtime 接入。
    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "schema_version": self.schema_version,
            "execution_mode": self.execution_mode,
            "summary": dict(self.summary),
            "report_ref": self.report_ref,
            "cases": [case.to_dict() for case in self.cases],
        }


# LLM: plan_main_agent_task_suite creates a safe manifest for heavyweight real-model tasks.
# 函数用途: 生成真实任务套件的 prompt、验收合同、期望产物清单和总报告；默认不启动模型。
def plan_main_agent_task_suite(
    request: MainAgentTaskSuiteRequest,
    *,
    schema_version: str = SCHEMA_VERSION,
    acceptance_schema_version: str = ACCEPTANCE_SCHEMA_VERSION,
    artifact_schema_version: str = ARTIFACT_SCHEMA_VERSION,
    root_name: str = "main_agent_task_suite",
    cases: list[MainAgentTaskCase] | None = None,
) -> MainAgentTaskSuiteReport:
    if cases is None:
        from .main_agent_task_suite_cases import default_main_agent_task_cases

        cases = default_main_agent_task_cases()

    _validate_request(request)
    cases = _with_prompt_overrides(cases, request.prompt_overrides)
    workspace = Path(request.workspace)
    root = workspace / root_name
    planned_cases = [
        _write_case_plan(
            MainAgentTaskCasePlanRequest(
                root=root,
                case=case,
                worker_slot=index % request.max_workers,
                timeout_seconds=request.task_timeout_seconds,
                execute=request.execute,
            ),
            acceptance_schema_version=acceptance_schema_version,
            artifact_schema_version=artifact_schema_version,
        )
        for index, case in enumerate(cases)
    ]
    summary = _summary(planned_cases)
    report = MainAgentTaskSuiteReport(
        ok=not any(item.status == "FAILED" for item in planned_cases),
        schema_version=schema_version,
        execution_mode="execute_requested" if request.execute else "plan_only",
        summary=summary,
        cases=planned_cases,
        report_ref=_rel(root / "suite_report.json", workspace),
    )
    _write_json(root / "suite_report.json", report.to_dict())
    return report


# LLM: _validate_request prevents accidental unlimited process fan-out from invalid controls.
# 函数用途: 校验并发工位和任务超时，避免真实 E2E 入口无约束地启动大量任务。
def _validate_request(request: MainAgentTaskSuiteRequest) -> None:
    if request.max_workers < 1:
        raise ValueError("max_workers must be >= 1")
    if request.task_timeout_seconds < 1:
        raise ValueError("task_timeout_seconds must be >= 1")


# LLM: Prompt overrides are structured test inputs; default case code remains task-generic.
# 函数用途: 按 case_id 替换 prompt 文件内容，并拒绝未知 case 或空 prompt，避免专项词写入生产案例。
def _with_prompt_overrides(
    cases: list[MainAgentTaskCase], overrides: dict[str, str]
) -> list[MainAgentTaskCase]:
    if not overrides:
        return cases
    case_ids = {case.case_id for case in cases}
    unknown = sorted(set(overrides) - case_ids)
    if unknown:
        raise ValueError(f"unknown prompt_overrides case_id: {', '.join(unknown)}")
    return [
        replace(case, user_prompt=_override_prompt(case.case_id, overrides))
        if case.case_id in overrides
        else case
        for case in cases
    ]


# LLM: Empty prompt overrides are configuration errors, not model-time clarification problems.
# 函数用途: 读取某个 case 的 override prompt 并做最小结构校验。
def _override_prompt(case_id: str, overrides: dict[str, str]) -> str:
    prompt = str(overrides.get(case_id) or "").strip()
    if not prompt:
        raise ValueError(f"prompt_overrides for {case_id} must be non-empty")
    return prompt


# LLM: _write_case_plan writes prompt and machine contracts for one planned real task.
# 函数用途: 给单个任务落 prompt.md、acceptance.json、expected_artifacts.json，并返回引用。
def _write_case_plan(
    request: MainAgentTaskCasePlanRequest,
    *,
    acceptance_schema_version: str,
    artifact_schema_version: str,
) -> MainAgentTaskCasePlan:
    case = request.case
    case_root = request.root / "tasks" / case.case_id
    prompt_path = case_root / "prompt.md"
    acceptance_path = case_root / "acceptance.json"
    artifacts_path = case_root / "expected_artifacts.json"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(case.user_prompt, encoding="utf-8")
    _write_json(acceptance_path, _acceptance_payload(case, acceptance_schema_version))
    _write_json(artifacts_path, _artifact_payload(case, artifact_schema_version))
    return MainAgentTaskCasePlan(
        case_id=case.case_id,
        title=case.title,
        status="PENDING" if request.execute else "PLANNING",
        worker_slot=request.worker_slot,
        timeout_seconds=request.timeout_seconds,
        prompt_ref=_rel(prompt_path, request.root.parent),
        acceptance_ref=_rel(acceptance_path, request.root.parent),
        expected_artifacts_ref=_rel(artifacts_path, request.root.parent),
    )


# LLM: _acceptance_payload stores completion checks as structured contracts.
# 函数用途: 生成验收合同 JSON，后续验收器按 check_id/kind/artifact_id 判断，不解析 prompt 文本。
def _acceptance_payload(case: MainAgentTaskCase, schema_version: str) -> dict[str, object]:
    return {
        "schema_version": schema_version,
        "case_id": case.case_id,
        "required_artifact_ids": [
            artifact.artifact_id for artifact in case.artifacts if artifact.required
        ],
        "checks": [dict(check) for check in case.acceptance_checks],
    }


# LLM: _artifact_payload stores expected outputs with validation contracts and paths.
# 函数用途: 生成期望产物 JSON，真实 runner/验收器据此找文件和选择通用检查器。
def _artifact_payload(case: MainAgentTaskCase, schema_version: str) -> dict[str, object]:
    return {
        "schema_version": schema_version,
        "case_id": case.case_id,
        "artifacts": [artifact.to_dict() for artifact in case.artifacts],
    }


# LLM: _summary counts planned/queued/failed cases for fast CLI and CI display.
# 函数用途: 汇总真实任务套件状态，让用户知道准备跑多少任务、是否已经请求执行。
def _summary(cases: list[MainAgentTaskCasePlan]) -> dict[str, int]:
    planning = sum(case.status == "PLANNING" for case in cases)
    pending = sum(case.status == "PENDING" for case in cases)
    return {
        "total": len(cases),
        "planning": planning,
        "pending": pending,
        "planned": planning,
        "queued": pending,
        "failed": sum(case.status == "FAILED" for case in cases),
    }


# LLM: _write_json centralizes deterministic UTF-8 JSON writing for all suite artifacts.
# 函数用途: 写结构化合同文件，排序字段，便于 diff、复验和后续恢复。
def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


# LLM: _rel stores portable refs within the selected workspace.
# 函数用途: 把绝对路径转成相对工作区引用，避免报告绑定某台机器的路径。
def _rel(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


__all__ = [
    "MainAgentTaskArtifact",
    "MainAgentTaskCase",
    "MainAgentTaskCasePlan",
    "MainAgentTaskCasePlanRequest",
    "MainAgentTaskSuiteReport",
    "MainAgentTaskSuiteRequest",
    "plan_main_agent_task_suite",
]
