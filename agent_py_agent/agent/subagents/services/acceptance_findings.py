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

from ..execution_report import TestExecutionReport, load_test_execution_report
from ..reports import AcceptanceReviewFinding
from .acceptance_evidence_findings import build_evidence_findings

if TYPE_CHECKING:
    from ..models import SubAgentTask


# LLM: _artifact_exists 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理产物exists相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
def _artifact_exists(manager: Any, task: SubAgentTask, raw_path: str) -> bool:
    """Check if a runner-reported local artifact actually exists.

    Artifact paths may be relative to the task directory or workspace root.
    Since subagent workspace is typically `<root>/.my_agent/subagents`,
    we also try looking in `<root>`.
    """
    text = raw_path.strip()
    if not text or "://" in text:
        return True
    path = Path(text)
    candidates = [path] if path.is_absolute() else []
    roots: list[Path] = []
    if not path.is_absolute():
        roots.extend([Path(task.task_dir), manager.workspace, manager.workspace.parent])
        candidates.extend(
            [
                Path(task.task_dir) / path,
                manager.workspace / path,
                manager.workspace.parent / path,
            ]
        )
        if manager.workspace.name == "subagents" and manager.workspace.parent.name == ".my_agent":
            roots.append(manager.workspace.parent.parent)
            candidates.append(manager.workspace.parent.parent / path)
    if any(candidate.exists() for candidate in candidates):
        return True
    if not path.is_absolute():
        return _path_suffix_exists(path, roots)
    return False


# LLM: _path_suffix_exists recovers model-reported relative artifacts from nested task output dirs.
# 函数用途: 当 runner 少写了外层任务目录时，在安全候选根目录内按路径后缀查找真实文件。
def _path_suffix_exists(relative_path: Path, roots: list[Path]) -> bool:
    parts = relative_path.parts
    if not parts:
        return False
    return any(
        _path_has_suffix(item, parts)
        for root in roots
        if root.exists() and root.is_dir()
        for item in root.rglob(parts[-1])
    )


# LLM: _path_has_suffix keeps nested artifact recovery shallow enough for strict size guards.
# 函数用途: 判断真实文件路径是否以 runner 报告的相对路径片段结尾。
def _path_has_suffix(path: Path, parts: tuple[str, ...]) -> bool:
    return len(path.parts) >= len(parts) and path.parts[-len(parts):] == parts


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
        return _artifact_exists(self.manager, task, raw_path)

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
        ready = task.status == "AWAITING_ACCEPTANCE" or task.verification_status == "NEEDS_ACCEPTANCE"
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

    # LLM: _findings_output_content 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 读取或查询findingsoutput内容需要的状态，返回调用方可继续处理的快照；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
    def _findings_output_content(self, task: SubAgentTask, output: dict, created_at: float) -> list[AcceptanceReviewFinding]:
        findings: list[AcceptanceReviewFinding] = []
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
        artifacts = _dict_list(output.get("artifacts", []))
        missing_artifacts = [
            str(item.get("path", "") or "")
            for item in artifacts
            if str(item.get("path", "") or "").strip()
            and not self._artifact_exists(task, str(item.get("path", "") or ""))
        ]
        findings.append(AcceptanceReviewFinding(
            name="artifact_paths_exist", ok=not missing_artifacts, severity="P1",
            message=f"runner 记录的 {len(artifacts)} 个 artifact 路径可核对。"
                    if not missing_artifacts else f"存在 {len(missing_artifacts)} 个 artifact 路径不存在: {missing_artifacts[0]}",
            evidence_path=task.output_json, created_at=created_at,
        ))
        patches = _dict_list(output.get("patches", []))
        valid_patch_statuses = {"applied", "planned", "blocked"}
        unresolved_patches = [
            item for item in patches if str(item.get("status", "")).lower() in {"planned", "blocked"}
        ]
        invalid_patches = [
            item for item in patches
            if str(item.get("status", "")).lower() not in valid_patch_statuses
        ]
        unreviewed_applied_patches = [
            item for item in patches
            if str(item.get("status", "")).lower() == "applied"
            and str(item.get("review_status", "")).upper() != "APPROVED"
        ]
        findings.append(AcceptanceReviewFinding(
            name="no_unresolved_patches", ok=not unresolved_patches, severity="P1",
            message="没有未处理 patch。"
                    if not unresolved_patches else f"仍有 {len(unresolved_patches)} 个 patch 处于 planned/blocked。",
            evidence_path=task.output_json, created_at=created_at,
        ))
        findings.append(AcceptanceReviewFinding(
            name="patch_status_valid", ok=not invalid_patches, severity="P1",
            message="patch 状态均符合协议。" if not invalid_patches else f"存在 {len(invalid_patches)} 个未知 patch 状态。",
            evidence_path=task.output_json, created_at=created_at,
        ))
        findings.append(AcceptanceReviewFinding(
            name="patches_reviewed", ok=not unreviewed_applied_patches, severity="P1",
            message="所有 applied patch 已审核。"
                    if not unreviewed_applied_patches else f"仍有 {len(unreviewed_applied_patches)} 个 applied patch 未通过审核。",
            evidence_path=task.output_json, created_at=created_at,
        ))
        return findings

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
