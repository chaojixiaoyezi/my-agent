# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

"""Parent planner record, report and log helpers."""

from __future__ import annotations

import time
from dataclasses import dataclass


# LLM: ParentPlannerRecordParams 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 集中保存父级规划器记录参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class ParentPlannerRecordParams:
    """Bundle of make_record parameters."""

    dry_run: bool
    triggered: bool
    ok: bool
    decision: str
    message: str
    gate_summary: dict[str, int] | None = None
    backend: str = ""
    tool_rounds: int = 0
    parse_error: str = ""
    summary: str = ""
    actions: list[dict[str, object]] | None = None
    blockers: list[str] | None = None
    risks: list[str] | None = None
    notes: list[str] | None = None
    runner_instruction: str = ""
    suggested_max_runners: int = 0
    prompt_path: str = ""
    response_path: str = ""
    evidence_paths: list[str] | None = None


# LLM: ParentPlannerBuilder 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 封装父级规划器构建器相关状态和行为，维持当前模块的职责边界；关键副作用: 方法可能触发任务状态、报告记录和持久化副作用相关副作用，需保持公开契约稳定。
class ParentPlannerBuilder:
    """Build parent planner records and reports."""

    # LLM: make_record 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 构建make记录所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
    @staticmethod
    def make_record(manager, *, params: ParentPlannerRecordParams) -> ParentPlannerRecord:
        """Create a parent planner audit record."""
        from agent_py_agent.agent.subagents.reports import ParentPlannerRecord

        return ParentPlannerRecord(
            id=manager._new_id("planner"),
            dry_run=params.dry_run,
            triggered=params.triggered,
            ok=params.ok,
            decision=params.decision,
            message=params.message,
            gate_summary=params.gate_summary or {},
            backend=params.backend,
            tool_rounds=params.tool_rounds,
            parse_error=params.parse_error,
            summary=params.summary,
            actions=params.actions or [],
            blockers=params.blockers or [],
            risks=params.risks or [],
            notes=params.notes or [],
            runner_instruction=params.runner_instruction,
            suggested_max_runners=params.suggested_max_runners,
            prompt_path=params.prompt_path,
            response_path=params.response_path,
            evidence_paths=params.evidence_paths or [],
            created_at=time.time(),
        )

    # LLM: build_summary 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 构建buildsummary所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
    @staticmethod
    def build_summary(records) -> dict[str, int]:
        """Summarize parent planner records."""
        summary: dict[str, int] = {"total": len(records)}
        for record in records:
            summary["triggered" if record.triggered else "skipped"] = summary.get(
                "triggered" if record.triggered else "skipped", 0,
            ) + 1
            summary["ok" if record.ok else "failed"] = summary.get(
                "ok" if record.ok else "failed", 0,
            ) + 1
            summary[record.decision] = summary.get(record.decision, 0) + 1
        return summary


# LLM: ParentPlannerLogAppender 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 封装父级规划器logappender相关状态和行为，维持当前模块的职责边界；关键副作用: 方法可能触发任务状态、报告记录和持久化副作用相关副作用，需保持公开契约稳定。
class ParentPlannerLogAppender:
    """Write global parent planner audit log."""

    # LLM: append 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 写入append的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
    @staticmethod
    def append(record, workspace, manager) -> None:
        """Write global parent planner audit log entry."""
        from dataclasses import asdict

        from agent_py_agent.agent.file_io import append_jsonl

        jsonl = workspace / "parent_planner_log.jsonl"
        append_jsonl(jsonl, asdict(record))

        markdown = workspace / "PARENT_PLANNER_LOG.md"
        if not markdown.exists():
            markdown.write_text("# PARENT PLANNER LOG\n\n", encoding="utf-8")
        with markdown.open("a", encoding="utf-8") as handle:
            status = "OK" if record.ok else "FAIL"
            handle.write(
                f"- [{status}] {record.id} decision={record.decision} "
                f"triggered={record.triggered} message={record.message}\n"
            )
        manager._index_parent_planner_record(record)