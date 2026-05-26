# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""acceptance finding rules and artifact existence checks.

给人看的解释：
这里承接验收检查项生成逻辑。
SubAgentManager 通过 facade 方法委托到这里。
"""

from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...contracts.artifact_acceptance import ArtifactAcceptanceRequest, validate_artifact
from ..acceptance_helpers.readiness import _task_ready_or_already_accepted
from ..capability_status import is_pending_capability_status
from ..execution_report import TestExecutionReport, load_test_execution_report
from ..reports import AcceptanceReviewFinding
from .acceptance_artifacts import artifact_exists, resolve_artifact_path
from .acceptance_controlled_exec_findings import controlled_exec_contract_finding
from .acceptance_declared_outputs import _looks_like_local_output_path, declared_output_refs_finding
from .acceptance_descendant_health import descendant_health_finding
from .acceptance_evidence_findings import build_evidence_findings
from .acceptance_patch_findings import patch_findings
from .acceptance_product_findings import required_product_files_finding
from .acceptance_role_coverage import required_role_coverage_finding

if TYPE_CHECKING:
    from ..models import SubAgentTask

_CHILD_SPAWN_BOOL_FIELDS = {"child_spawn_required", "required_child_spawn", "require_child_spawn"}
_CHILD_SPAWN_COUNT_FIELDS = {"required_child_depth", "required_child_count", "required_children"}


# LLM: _dict_list 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理dictlist相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _dict_list(value: object) -> list[dict[str, object]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


# LLM: SubAgentAcceptanceFindingService 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 封装subagent验收finding服务操作，把状态读写和错误处理收束在服务层；关键副作用: 方法可能触发任务状态、报告记录和持久化副作用相关副作用，需保持公开契约稳定。
class SubAgentAcceptanceFindingService:

    # LLM: __init__ 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
    def __init__(self, manager: Any):
        self.manager = manager

    # LLM: _artifact_exists 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 处理产物exists相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
    def _artifact_exists(self, task: SubAgentTask, raw_path: str) -> bool:
        """Check if a runner-reported local artifact actually exists.

        Delegates to manager._artifact_exists if available (for backward compatibility
        with tests that mock the manager), otherwise uses the module-level function.
        """
        if hasattr(self.manager, "_artifact_exists"):
            return self.manager._artifact_exists(task, raw_path)
        return artifact_exists(self.manager, task, raw_path)

    # LLM: _findings_basic_state 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 读取或查询findingsbasic状态需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def _findings_basic_state(self, task: SubAgentTask, created_at: float) -> list[AcceptanceReviewFinding]:
        findings: list[AcceptanceReviewFinding] = []
        validation = self.manager.validate_work_order(task.id)
        findings.append(AcceptanceReviewFinding(
            name="work_order", ok=validation.ok, severity="P0",
            message="工单现场完整。" if validation.ok else f"工单缺少 {len(validation.missing)} 个关键路径。",
            evidence_path=task.task_dir, created_at=created_at,
        ))
        ready = _task_ready_or_already_accepted(task)
        findings.append(AcceptanceReviewFinding(
            name="ready_for_acceptance", ok=ready, severity="P1",
            message="任务处于等待验收状态。" if ready else f"任务未处于等待验收状态: status={task.status} verify={task.verification_status}",
            evidence_path=task.runner_result_json, created_at=created_at,
        ))
        findings.append(AcceptanceReviewFinding(
            name="channel_not_broken", ok=task.channel_status != "BROKEN", severity="P1",
            message="通道未标记为 BROKEN。" if task.channel_status != "BROKEN" else "通道为 BROKEN，不能验收。",
            evidence_path=task.channel_probe_file, created_at=created_at,
        ))
        return findings

    # LLM: _findings_runner_output 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 读取或查询findings执行器output需要的状态，返回调用方可继续处理的快照；关键副作用: 会影响任务状态、报告记录和持久化副作用，需保持重试、超时和状态迁移语义。
    def _findings_runner_output(self, task: SubAgentTask, runner: dict, created_at: float) -> list[AcceptanceReviewFinding]:
        findings: list[AcceptanceReviewFinding] = []
        runner_structured_found = bool(runner.get("structured_output_found", False))
        runner_structured_ok = bool(runner.get("structured_output_ok", False))
        findings.append(AcceptanceReviewFinding(
            name="structured_output", ok=(not runner_structured_found) or runner_structured_ok, severity="P1",
            message="runner 结构化输出可解析。" if runner_structured_found and runner_structured_ok else
                    "runner 未记录结构化输出，按人工证据验收。" if not runner_structured_found else
                    f"runner 结构化输出解析失败: {runner.get('structured_parse_error', '')}",
            evidence_path=task.runner_result_json, created_at=created_at,
        ))
        return findings

    # LLM: _findings_evidence 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 读取或查询findings证据需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def _findings_evidence(self, task: SubAgentTask, created_at: float) -> list[AcceptanceReviewFinding]:
        return build_evidence_findings(task, created_at)

    # LLM: _findings_capability 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 读取或查询findings能力需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def _findings_capability(self, task: SubAgentTask, created_at: float) -> list[AcceptanceReviewFinding]:
        findings: list[AcceptanceReviewFinding] = []
        open_requests = [item for item in task.capability_requests if item.status == "OPEN"]
        open_gaps = [item for item in task.capability_gaps if item.status == "OPEN"]
        findings.append(AcceptanceReviewFinding(
            name="no_open_capability_requests", ok=not open_requests, severity="P1",
            message="没有待处理 capability request。"
                    if not open_requests else f"仍有 {len(open_requests)} 条 OPEN capability request。",
            evidence_path=task.output_json, created_at=created_at,
        ))
        findings.append(AcceptanceReviewFinding(
            name="no_open_capability_gaps", ok=not open_gaps, severity="P1",
            message="没有待处理 capability gap。"
                    if not open_gaps else f"仍有 {len(open_gaps)} 条 OPEN capability gap。",
            evidence_path=task.output_json, created_at=created_at,
        ))
        return findings

    # LLM: _findings_output_content keeps output, child-spawn, descendant-health and role-coverage gates together.
    # 函数用途: 汇总 output.json、真实 child、所有后代健康状态、QA 角色覆盖和测试结果，决定父级能否被验收。
    def _findings_output_content(self, task: SubAgentTask, output: dict, created_at: float) -> list[AcceptanceReviewFinding]:
        findings: list[AcceptanceReviewFinding] = []
        findings.append(_pending_structured_status_finding(task, output, created_at))
        findings.append(_required_child_spawn_finding(task, created_at))
        findings.append(descendant_health_finding(task, created_at))
        findings.append(required_role_coverage_finding(task, created_at))
        findings.append(controlled_exec_contract_finding(task, output, created_at))
        blockers = [item for item in _string_list(output.get("blockers", [])) if item.strip()]
        findings.append(AcceptanceReviewFinding(
            name="no_output_blockers", ok=not blockers, severity="P1",
            message="output.json 没有 blocker。" if not blockers else f"output.json 仍有 blocker: {blockers[0]}",
            evidence_path=task.output_json, created_at=created_at,
        ))
        tests = _dict_list(output.get("tests", []))
        report = _existing_test_execution_report(task)
        if tests and report is not None:
            ok = report.total_tests > 0 and report.failed == 0
            findings.append(AcceptanceReviewFinding(
                name="tests_passed", ok=ok, severity="P1",
                message=f"真实测试报告记录的 {report.total_tests} 条测试均通过。" if ok else
                        f"真实测试报告存在失败: total={report.total_tests} failed={report.failed}。",
                evidence_path=str(report.json_path), created_at=created_at,
            ))
            return findings
        failed_tests = [item for item in tests if not bool(item.get("ok", False))]
        findings.append(AcceptanceReviewFinding(
            name="tests_passed", ok=not failed_tests, severity="P1",
            message=f"runner 记录的 {len(tests)} 条测试均通过。" if tests and not failed_tests else
                    "runner 未记录测试，允许仅凭证据进入人工验收。" if not tests else
                    f"存在 {len(failed_tests)} 条失败测试。",
            evidence_path=task.output_json, created_at=created_at,
        ))
        return findings

    # LLM: _findings_artifacts_patches 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 读取或查询findings产物patches需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def _findings_artifacts_patches(self, task: SubAgentTask, output: dict, created_at: float) -> list[AcceptanceReviewFinding]:
        findings: list[AcceptanceReviewFinding] = []
        findings.append(required_product_files_finding(task, created_at))
        findings.append(declared_output_refs_finding(self.manager, task, output, created_at))
        artifacts = _dict_list(output.get("artifacts", []))
        missing_artifacts = [
            str(item.get("path", "") or "")
            for item in artifacts
            if str(item.get("path", "") or "").strip()
            and _looks_like_local_output_path(str(item.get("path", "") or ""))
            and not self._artifact_exists(task, str(item.get("path", "") or ""))
        ]
        findings.append(AcceptanceReviewFinding(
            name="artifact_paths_exist", ok=not missing_artifacts, severity="P1",
            message=f"runner 记录的 {len(artifacts)} 个 artifact 路径可核对。"
                    if not missing_artifacts else f"存在 {len(missing_artifacts)} 个 artifact 路径不存在: {missing_artifacts[0]}",
            evidence_path=task.output_json, created_at=created_at,
        ))
        findings.append(self._artifact_acceptance_finding(task, artifacts, created_at))
        findings.extend(
            patch_findings(
                _dict_list(output.get("patches", [])),
                evidence_path=task.output_json,
                created_at=created_at,
            )
        )
        return findings

    # LLM: _artifact_acceptance_finding validates resolved artifacts through the shared contract layer.
    # 函数用途: 对真实存在的产物做格式/质量机器验收；无法解析路径时交给 artifact_paths_exist finding 阻断。
    def _artifact_acceptance_finding(
        self,
        task: SubAgentTask,
        artifacts: list[dict[str, object]],
        created_at: float,
    ) -> AcceptanceReviewFinding:
        reports = [
            validate_artifact(ArtifactAcceptanceRequest(path=path, workspace_root=self._workspace_root()))
            for item in artifacts
            if _looks_like_local_output_path(str(item.get("path", "") or ""))
            and (path := self._artifact_path(task, str(item.get("path", "") or ""))) is not None
        ]
        failed = [report.artifact_ref for report in reports if not report.ok]
        return AcceptanceReviewFinding(
            name="artifact_acceptance_passed",
            ok=not failed,
            severity="P1",
            message=(
                f"已通过 {len(reports)} 个 artifact 的格式/质量验收。"
                if not failed else f"存在 {len(failed)} 个 artifact 验收失败: {failed[0]}"
            ),
            evidence_path=task.output_json,
            created_at=created_at,
        )

    # LLM: _artifact_path resolves one output artifact using the same roots as artifact existence checks.
    # 函数用途: 返回真实文件路径，供通用 artifact acceptance 读取；外部 URL/空路径返回 None。
    def _artifact_path(self, task: SubAgentTask, raw_path: str) -> Path | None:
        return resolve_artifact_path(self.manager, task, raw_path)

    # LLM: _workspace_root exposes the manager workspace root to artifact validators when available.
    # 函数用途: 为 HTML 本地图片等相对路径检查提供 workspace 根目录。
    def _workspace_root(self) -> Path | None:
        root = getattr(self.manager, "workspace_root", None)
        return Path(root) if root else None

    # LLM: acceptance_findings 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 处理验收findings相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def acceptance_findings(
        self,
        task: SubAgentTask,
        output: dict[str, object],
        runner: dict[str, object],
        created_at: float,
    ) -> list[AcceptanceReviewFinding]:
        from ..reports import AcceptanceReviewFinding

        findings: list[AcceptanceReviewFinding] = []
        findings.extend(self._findings_basic_state(task, created_at))
        findings.extend(self._findings_runner_output(task, runner, created_at))
        findings.extend(self._findings_evidence(task, created_at))
        findings.extend(self._findings_capability(task, created_at))
        findings.extend(self._findings_output_content(task, output, created_at))
        findings.extend(self._findings_artifacts_patches(task, output, created_at))
        return findings


# LLM: _string_list 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理stringlist相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item not in (None, "")]
    return [str(value)]


# LLM: _pending_structured_status_finding prevents half-finished capability requests from being accepted.
# 函数用途: 检查 output.json 的 structured_output.status；仍在等待能力/工具时返回 P1 失败。
def _pending_structured_status_finding(task, output: dict, created_at: float) -> AcceptanceReviewFinding:
    status = _structured_status(output)
    pending = is_pending_capability_status(status)
    return AcceptanceReviewFinding(
        name="no_pending_structured_status",
        ok=not pending,
        severity="P1",
        message="structured_output.status 没有等待能力申请。"
        if not pending else f"structured_output.status 仍在等待能力申请: {status}",
        evidence_path=task.output_json,
        created_at=created_at,
    )


# LLM: _structured_status reads only compact output metadata instead of trusting free-form summaries.
# 函数用途: 从 output.json 中提取结构化状态；没有结构化块时返回空字符串以保持旧任务兼容。
def _structured_status(output: dict) -> str:
    structured = output.get("structured_output")
    if isinstance(structured, dict):
        return str(structured.get("status") or "")
    return ""


# LLM: _required_child_spawn_finding blocks coordinator self-claims when no child run exists.
# 函数用途: 任务目标明确要求创建下级时，必须看到真实 task.child_ids，不能只在结果里口头声明。
def _required_child_spawn_finding(task, created_at: float) -> AcceptanceReviewFinding:
    required = _child_spawn_required(task)
    child_ids = _task_child_ids(task)
    ok = (not required) or bool(child_ids)
    return AcceptanceReviewFinding(
        name="required_child_spawned",
        ok=ok,
        severity="P0",
        message=_required_child_spawn_message(required, child_ids),
        evidence_path=getattr(task, "output_json", ""),
        created_at=created_at,
    )


# LLM: _child_spawn_required keeps the hard check scoped to machine fields.
# 函数用途: 只从 child_spawn_required/required_child_depth 等结构化字段识别必须创建下级。
def _child_spawn_required(task) -> bool:
    role = str(getattr(task, "role", "") or "").lower()
    agent_name = str(getattr(task, "agent_name", "") or "").lower()
    leaf_self = role == "leaf_worker" or "leaf" in agent_name
    if leaf_self:
        return False
    attrs = getattr(task, "attributes", {})
    attrs = attrs if isinstance(attrs, dict) else {}
    for field in _CHILD_SPAWN_BOOL_FIELDS:
        if _truthy_machine_bool(attrs.get(field)):
            return True
    for field in _CHILD_SPAWN_COUNT_FIELDS:
        if _positive_int(attrs.get(field)):
            return True
    return False


# LLM: _positive_int keeps child-spawn numeric fields conservative.
# 函数用途: 只有明确大于 0 的结构化数字才表示需要真实 child_ids。
def _positive_int(value: object) -> bool:
    try:
        return int(str(value or "").strip()) > 0
    except ValueError:
        return False


# LLM: _truthy_machine_bool accepts exact structured booleans only.
# 函数用途: 识别 attributes 里的 true/1/required，不读取 goal 或 acceptance 文本。
def _truthy_machine_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().casefold() in {"1", "true", "yes", "required"}


# LLM: _task_child_ids normalizes persisted child ids without trusting model output refs.
# 函数用途: 只读取任务状态里的真实 child_ids；异常类型按空列表处理。
def _task_child_ids(task) -> list[str]:
    value = getattr(task, "child_ids", [])
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item) for item in value if item]


# LLM: _required_child_spawn_message keeps acceptance text precise for parent recovery models.
# 函数用途: 生成下级创建合同的验收提示，帮助上级直接接管或重派。
def _required_child_spawn_message(required: bool, child_ids: list[str]) -> str:
    if not required:
        return "当前任务未声明必须创建下级。"
    if child_ids:
        return f"任务要求创建下级，已记录 {len(child_ids)} 个真实 child run。"
    return "任务要求创建下级，但 task.child_ids 为空；不能只在输出里声称完成。"


# LLM: _existing_test_execution_report lets normal acceptance trust machine facts over worker test claims.
# 函数用途: 读取已存在的 test_execution.json；读取失败时返回 None，保持旧 output.json 验收路径可用。
def _existing_test_execution_report(task) -> TestExecutionReport | None:
    path = Path(task.reports_dir) / "test_execution.json"
    if not path.exists():
        return None
    try:
        return load_test_execution_report(path)
    except (OSError, ValueError, TypeError):
        return None
