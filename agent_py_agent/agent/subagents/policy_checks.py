# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""individual policy check functions for risk weighting, status mapping, and due-check dispatch.

给人看的解释：
这些函数各自是一条规则——某个 issue 应该排多前面、某个 runner 状态应该映射成什么任务状态、
某个能力请求应该怎么检索。它们不读写文件，也不依赖运行时状态。
"""

import time
from dataclasses import dataclass
from pathlib import Path

from ..capabilities import CapabilitySearchHit
from ..capability_config import CapabilityConfig
from .capability_status import is_pending_capability_status
from .models import CapabilityGrant, CapabilityRequest, SubAgentParsedOutput, SubAgentTask
from .reports import ActionPlanItem, DueCheckIssue, SubAgentBoardItem


# LLM: _risk_weight 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理riskweight相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
def _risk_weight(flags: list[str]) -> int:

    weights = {
        "failed": 100,
        "timeout": 95,
        "channel_error": 90,
        "blocked": 80,
        "channel_broken": 75,
        "missing_work_order_files": 70,
        "done_without_evidence": 60,
        "done_without_verification": 55,
        "channel_degraded": 52,
        "open_capability_gap": 50,
        "open_capability_request": 40,
        "taken_over": 30,
    }
    return max((weights.get(item, 1) for item in flags), default=0)


# LLM: MakeDueIssueParams 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存make到期issue参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class MakeDueIssueParams:
    """Params bundle for _make_due_issue."""
    task: SubAgentTask
    severity: str
    kind: str
    message: str
    suggested_action: str
    risk_flags: list[str]
    open_request_count: int
    open_gap_count: int
    age_seconds: float
    stale_seconds: float
    # LLM: related_refs mirrors production policy payloads for structured rescue refs.
    related_refs: list[str] | None = None


# LLM: RunnerNextActionParams 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存执行器next动作参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class RunnerNextActionParams:
    """Params bundle for deciding the runner follow-up action."""

    dry_run: bool = False
    ok: bool = False
    status: str = ""
    capability_request_count: int = 0
    next_actions: list[str] | None = None


# LLM: _make_due_issue 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 构建到期issue所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _make_due_issue(*, params: MakeDueIssueParams) -> DueCheckIssue:
    return DueCheckIssue(
        run_id=params.task.id,
        severity=params.severity,
        kind=params.kind,
        message=params.message,
        suggested_action=params.suggested_action,
        status=params.task.status,
        owner=params.task.owner,
        supervisor=params.task.supervisor,
        final_owner=params.task.final_owner,
        goal=params.task.goal,
        task_dir=params.task.task_dir,
        risk_flags=params.risk_flags,
        evidence_count=len(params.task.evidence),
        open_request_count=params.open_request_count,
        open_gap_count=params.open_gap_count,
        age_seconds=params.age_seconds,
        stale_seconds=params.stale_seconds,
        related_refs=list(params.related_refs or []),
        created_at=time.time(),
    )


# LLM: _severity_weight 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理severityweight相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
def _severity_weight(severity: str) -> int:

    return {"P0": 1000, "P1": 500, "P2": 100}.get(severity, 0)


# LLM: _issue_weight 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理issueweight相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
def _issue_weight(issue: DueCheckIssue) -> int:

    severity_weight = _severity_weight(issue.severity)
    kind_weight = {
        "missing_work_order_files": 90,
        "fake_done_risk": 85,
        "run_timeout": 80,
        "no_progress_fuse": 78,
        "coordinator_needs_leadership_recovery": 77,
        "heartbeat_stale": 70,
        "channel_broken": 68,
        "status_failed": 65,
        "status_timeout": 65,
        "status_channel_error": 60,
        "parent_timeout_with_unfinished_children": 59,
        "coordinator_heartbeat_stale": 58,
        "status_blocked": 50,
        "channel_probe_missing": 48,
        "unverified_done": 45,
        "channel_degraded": 42,
        "open_capability_request": 40,
        "open_capability_gap": 20,
    }.get(issue.kind, 1)
    return severity_weight + kind_weight


# LLM: _action_for_issue 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理动作issue相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
def _action_for_issue(issue: DueCheckIssue) -> tuple[str, int, str]:

    kind = issue.kind
    if kind in {"channel_broken", "channel_probe_missing", "status_channel_error"}:
        return "probe_or_repair_channel", 980, "CHANNEL_ERROR"
    if kind == "channel_degraded":
        return "inspect_channel_probe", 780, ""
    if kind == "missing_work_order_files":
        return "repair_work_order", 960, "BLOCKED"
    if kind == "fake_done_risk":
        return "reopen_for_evidence", 940, "BLOCKED"
    if kind == "unverified_done":
        return "run_acceptance", 760, ""
    if kind in {"run_timeout", "heartbeat_stale", "status_timeout"}:
        return "takeover_or_reassign", 900, "TIMEOUT"
    if kind == "no_progress_fuse":
        return "stop_no_progress_and_escalate", 990, ""
    if kind == "coordinator_needs_leadership_recovery":
        return "recover_coordinator_leadership", 930, ""
    if kind == "parent_timeout_with_unfinished_children":
        return "recover_child_after_parent_timeout", 830, ""
    if kind == "coordinator_heartbeat_stale":
        return "recover_coordinator_leadership", 820, ""
    if kind == "status_failed":
        return "inspect_failure", 860, ""
    if kind == "status_blocked":
        return "classify_blocker", 740, ""
    if kind == "open_capability_request":
        return "route_capability_request", 700, ""
    if kind == "open_capability_gap":
        return "triage_capability_gap", 420, ""
    return issue.suggested_action or "inspect_manually", 100, ""


# LLM: _commands_for_action 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理commands动作相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
def _commands_for_action(action: str, run_id: str) -> list[str]:

    # LLM: 优先使用已安装命令行入口，绕开 Windows 的 python3 占位程序问题。
    cli = "my-agent"
    if action in _probe_command_actions():
        return [f"{cli} subagents-probe {run_id}", f"{cli} subagent {run_id}"]
    if action in _recovery_tree_command_actions():
        return [f"{cli} subagents-recovery-tree {run_id} --hide-healthy", f"{cli} subagent {run_id}"]
    return [f"{cli} subagent {run_id}"]


# LLM: _probe_command_actions groups actions whose safest first step is channel/work-order probing.
# 函数用途: 返回需要先查看 probe 再打开 subagent 详情的动作集合，避免命令映射函数继续变长。
def _probe_command_actions() -> set[str]:
    return {
        "probe_or_repair_channel",
        "inspect_channel_probe",
        "repair_work_order",
        "takeover_or_reassign",
    }


# LLM: _recovery_tree_command_actions groups actions that should inspect hierarchy refs first.
# 函数用途: 返回需要先查询 recovery-tree 的动作集合，保持父子恢复入口一致。
def _recovery_tree_command_actions() -> set[str]:
    return {
        "recover_coordinator_leadership",
        "recover_child_after_parent_timeout",
        "stop_no_progress_and_escalate",
    }


# LLM: _status_from_structured_output 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理来自状态structuredoutput相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
def _status_from_structured_output(parsed: SubAgentParsedOutput) -> str:

    status = parsed.status.upper().strip()
    if parsed.capability_requests or parsed.blocked_reason:
        return "BLOCKED"
    if is_pending_capability_status(status):
        return "BLOCKED"
    if status in {"BLOCKED", "FAILED", "CHANNEL_ERROR", "TIMEOUT"}:
        return status
    if status in {"DONE", "COMPLETED", "COMPLETE", "SUCCESS", "AWAITING_ACCEPTANCE"}:
        return "AWAITING_ACCEPTANCE"
    return "AWAITING_ACCEPTANCE"


# LLM: _verification_from_runner_status 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理来自verification执行器状态相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响任务状态、执行器结果、验收和报告展示，需保持重试、超时和状态迁移语义。
def _verification_from_runner_status(status: str) -> str:

    if status.upper() == "AWAITING_ACCEPTANCE":
        return "NEEDS_ACCEPTANCE"
    return "UNVERIFIED"


# LLM: _runner_next_action 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 推进执行器next动作的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响任务状态、执行器结果、验收和报告展示，需保持重试、超时和状态迁移语义。
def _runner_next_action(*, params: RunnerNextActionParams) -> str:

    if params.dry_run:
        return ""
    if params.capability_request_count:
        return "route_capability_request"
    if params.next_actions:
        return params.next_actions[0]
    if params.ok and params.status == "AWAITING_ACCEPTANCE":
        return "run_acceptance"
    if not params.ok:
        return "inspect_runner_failure"
    return ""


# LLM: _is_active 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 判断active条件是否成立，作为后续调度或分支决策的门禁；关键副作用: 主要返回判断或抛出明确异常，调用方依赖布尔语义稳定。
def _is_active(status: str) -> bool:

    return status.upper() not in {
        "DONE",
        "FAILED",
        "BLOCKED",
        "AWAITING_ACCEPTANCE",
        "TIMEOUT",
        "CHANNEL_ERROR",
        "TAKEN_OVER",
    }


# LLM: _default_forbidden_write_roots 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理defaultforbiddenwriteroots相关的数据流，连接当前职责的前后步骤；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
def _default_forbidden_write_roots() -> list[str]:

    home = Path.home()
    return [
        str(home),
        str(home / "Desktop"),
        str(home / "Downloads"),
        str(home / ".openclaw"),
    ]
