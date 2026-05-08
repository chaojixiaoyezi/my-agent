# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""LLM contract: SubAgentAcceptanceMixin methods grouped by one subagent responsibility.

Human version:
这个 mixin 是 SubAgentManager 的一块业务能力，不单独实例化。
拆成 mixin 是为了让每个文件只有一个变化原因，而不是把所有父代理逻辑塞进一个巨型文件。
"""

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..file_io import append_jsonl
from .acceptance_review_service import (
    AcceptanceReviewOptions,
    AcceptanceReviewRequest,
    acceptance_review_options,
    review_acceptance_task,
)
from .manager_parent_acceptance import (
    ParentAcceptanceApplyResult,
    ParentAcceptanceAutoPolicy,
    ParentAcceptanceDecision,
    ParentAcceptanceNextAction,
    manager_apply_parent_acceptance_decision,
    manager_plan_parent_acceptance,
    manager_plan_parent_acceptance_auto_policy,
    manager_plan_parent_acceptance_next_action,
    manager_write_parent_acceptance_decision,
)
from .models import SubAgentTask
from .parsing import (
    _dict_list,
    _normalize_runner_items,
    _split_allowed_items,
    _string_dict,
    _string_list,
)
from .policies import (
    _action_for_issue,
    _capability_request_query,
    _commands_for_action,
    _dedupe_granted_cards,
    _default_forbidden_write_roots,
    _execution_context_instructions,
    _filter_action_plan_items,
    _is_active,
    _issue_weight,
    _make_due_issue,
    _risk_weight,
    _route_card_payload,
    _runner_next_action,
    _select_capability_hits,
    _severity_weight,
    _status_from_structured_output,
    _verification_from_runner_status,
)
from .probe import (
    _channel_status,
    _probe_fail,
    _probe_json_file,
    _probe_ok,
    _probe_writable_dir,
)
from .rendering import render_acceptance_record_markdown, render_acceptance_review_markdown
from .reports import AcceptanceReviewRecord, AcceptanceReviewReport
from .runner_rendering import _render_runner_item_line
from .services.indexing_params import IndexReportParams
from .utils import (
    _apply_missing_paths,
    _apply_paths,
    _merge_list,
    _new_id,
    _read_json_object,
    _write_if_missing,
    _write_json_if_missing,
)

if TYPE_CHECKING:
    from ..local_store import LocalStore

# LLM: _SubAgentAcceptanceFacade 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 拆分subagent验收门面流程片段，复用宿主对象上的状态和服务依赖；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
class _SubAgentAcceptanceFacade:
    # LLM: review_acceptance 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理审查验收相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    # LLM: plan_parent_acceptance is a dry-run upper-agent controller entrypoint; it must not execute tests or mutate tasks.
    # 函数用途: 让父代理先查看验收下一步建议；只读取任务事实源并返回决策，不写状态、不跑命令。
    def plan_parent_acceptance(self, run_id: str) -> ParentAcceptanceDecision:
        return manager_plan_parent_acceptance(self, run_id)

    # LLM: write_parent_acceptance_decision persists the dry-run plan but does not apply it.
    # 函数用途: 写入父级验收 dry-run 决策审计文件；不执行 tests、不修改 task 状态。
    def write_parent_acceptance_decision(self, run_id: str) -> ParentAcceptanceDecision:
        return manager_write_parent_acceptance_decision(self, run_id)

    # LLM: apply_parent_acceptance_decision only bridges inspect_only into the existing acceptance apply path.
    # 函数用途: 显式应用父级验收决策；execute_tests/request_human/rescue 只写入拦截审计，不自动跑命令或改状态。
    def apply_parent_acceptance_decision(
        self,
        run_id: str,
        *,
        reviewer: str = "parent",
        note: str = "",
    ) -> ParentAcceptanceApplyResult:
        return manager_apply_parent_acceptance_decision(self, run_id, reviewer=reviewer, note=note)

    # LLM: plan_parent_acceptance_next_action returns a scheduler-facing recommendation, not an execution.
    # 函数用途: 为父/上级代理生成下一步显式动作建议；不运行 tests、不 rescue、不改状态。
    def plan_parent_acceptance_next_action(self, run_id: str) -> ParentAcceptanceNextAction:
        return manager_plan_parent_acceptance_next_action(self, run_id)

    # LLM: plan_parent_acceptance_auto_policy is a dry-run policy gate for future automation.
    # 函数用途: 生成父级验收自动策略审计；不执行建议动作、不改状态。
    def plan_parent_acceptance_auto_policy(self, run_id: str) -> ParentAcceptanceAutoPolicy:
        return manager_plan_parent_acceptance_auto_policy(self, run_id)

    # LLM: review_acceptance runs one parent acceptance review with explicit opt-in execution options.
    # 函数用途: 对单个子代理 run 执行父级验收；只有 options 或 apply 参数允许时才写回任务状态。
    def review_acceptance(
        self,
        run_id: str,
        *,
        options: AcceptanceReviewOptions | None = None,
        apply: bool = False,
        reviewer: str = "parent",
        note: str = "",
    ) -> AcceptanceReviewRecord:

        task = self.load(run_id)
        opts = acceptance_review_options(
            options,
            apply=apply,
            reviewer=reviewer,
            note=note,
        )
        # LLM: pass the normalized options bundle so opt-in test execution fields are preserved.
        return _call_review_acceptance_task(self, task, opts)

    # LLM: review_acceptances 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理审查acceptances相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def review_acceptances(
        self,
        run_ids: list[str] | None = None,
        *,
        options: AcceptanceReviewOptions | None = None,
        apply: bool = False,
        reviewer: str = "parent",
        note: str = "",
        limit: int = 0,
    ) -> AcceptanceReviewReport:
        """批量验收子代理运行。

        不指定 run_id 时，只挑出正在等待验收的运行，避免误动历史任务。
        """

        opts = acceptance_review_options(
            options,
            apply=apply,
            reviewer=reviewer,
            note=note,
            limit=limit,
        )
        selected = _selected_acceptance_runs(self, run_ids, opts.limit)
        # LLM: preserve the same options bundle for every selected task, including execute_tests.
        records = [
            _call_review_acceptance_task(self, task, opts)
            for task in selected
        ]
        return AcceptanceReviewReport(
            generated_at=time.time(),
            dry_run=not opts.apply,
            summary=_acceptance_report_summary(records),
            records=records,
        )

    # LLM: write_acceptance_review_report 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 写入验收审查报告的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
    def write_acceptance_review_report(
        self,
        run_ids: list[str] | None = None,
        *,
        options: AcceptanceReviewOptions | None = None,
        apply: bool = False,
        reviewer: str = "parent",
        note: str = "",
        limit: int = 0,
    ) -> AcceptanceReviewReport:

        report = self.review_acceptances(
            run_ids,
            options=options,
            apply=apply,
            reviewer=reviewer,
            note=note,
            limit=limit,
        )
        (self.workspace / "subagent_acceptance_report.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.workspace / "SUBAGENT_ACCEPTANCE.md").write_text(
            render_acceptance_review_markdown(report),
            encoding="utf-8",
        )
        for record in report.records:
            self._write_acceptance_record_files(record)
            self._index_acceptance_review(record)
            if report.dry_run is False:
                self._append_acceptance_review_log(record)
        self._index_report(
            IndexReportParams(
                "subagent_acceptance_report",
                "latest",
                "Subagent acceptance report",
                report,
                "subagent_acceptance_report_written",
            ),
        )
        return report

    # LLM: _review_acceptance_task 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 处理审查验收任务相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
    def _review_acceptance_task(
        self,
        task: SubAgentTask,
        *,
        options: AcceptanceReviewOptions | None = None,
        apply: bool = False,
        reviewer: str = "parent",
        note: str = "",
    ) -> AcceptanceReviewRecord:
        # LLM: 管理器保留旧方法形态，服务层只接收归一后的请求包。
        opts = acceptance_review_options(
            options,
            apply=apply,
            reviewer=reviewer,
            note=note,
        )
        return review_acceptance_task(
            self,
            AcceptanceReviewRequest(
                task=task,
                apply=opts.apply,
                reviewer=opts.reviewer,
                note=opts.note,
                now=opts.now,
                # LLM: real test execution stays opt-in and is carried only through the request bundle.
                execute_tests=opts.execute_tests,
                test_timeout_seconds=opts.test_timeout_seconds,
            ),
        )

    # LLM: _write_acceptance_record_files 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 写入验收记录文件的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
    def _write_acceptance_record_files(self, record: AcceptanceReviewRecord) -> None:

        try:
            task = self.load(record.run_id)
        except FileNotFoundError:
            return
        record_json = Path(task.reports_dir) / "acceptance_review.json"
        record_md = Path(task.task_dir) / "ACCEPTANCE_REVIEW.md"
        record_json.write_text(
            json.dumps(asdict(record), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        record_md.write_text(render_acceptance_record_markdown(record), encoding="utf-8")
        with Path(task.acceptance_file).open("a", encoding="utf-8") as handle:
            handle.write("\n## Review\n\n")
            handle.write(f"- id: {record.id}\n")
            handle.write(f"- decision: {record.decision}\n")
            handle.write(f"- ok: {record.ok}\n")
            handle.write(f"- applied: {record.applied}\n")
            handle.write(f"- message: {record.message}\n")

    # LLM: _append_acceptance_review_log 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
    # 函数用途: 写入验收审查log的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
    def _append_acceptance_review_log(self, record: AcceptanceReviewRecord) -> None:

        jsonl = self.workspace / "subagent_acceptance_log.jsonl"
        append_jsonl(jsonl, asdict(record))

        markdown = self.workspace / "ACCEPTANCE_REVIEW_LOG.md"
        if not markdown.exists():
            markdown.write_text("# ACCEPTANCE REVIEW LOG\n\n", encoding="utf-8")
        with markdown.open("a", encoding="utf-8") as handle:
            status = "OK" if record.ok else "FAIL"
            handle.write(
                f"- [{status}] {record.id} run={record.run_id} decision={record.decision} "
                f"applied={record.applied} message={record.message}\n"
            )
        self._index_acceptance_review(record)


# LLM: SubAgentAcceptanceMixin 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 拆分subagent验收混入流程片段，复用宿主对象上的状态和服务依赖；关键副作用: 方法可能触发任务状态、执行器结果、验收和报告展示相关副作用，需保持公开契约稳定。
class SubAgentAcceptanceMixin(_SubAgentAcceptanceFacade):
    """Public compatibility mixin; implementation lives in the internal facade."""


# LLM: _selected_acceptance_runs 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 读取或查询selected验收runs需要的状态，返回调用方可继续处理的快照；关键副作用: 会影响任务状态、执行器结果、验收和报告展示，需保持重试、超时和状态迁移语义。
def _selected_acceptance_runs(manager, run_ids: list[str] | None, limit: int):
    selected = manager._select_runs(run_ids)
    if run_ids is None:
        selected = [
            task
            for task in selected
            if task.status == "AWAITING_ACCEPTANCE"
            or task.verification_status == "NEEDS_ACCEPTANCE"
        ]
    return selected[:limit] if limit > 0 else selected


# LLM: _call_review_acceptance_task keeps old tests/mocks working while preserving new option bundles.
# 函数用途: 默认验收仍按旧关键字调用；只有真实测试执行等新字段启用时才传完整 options 包。
def _call_review_acceptance_task(manager, task: SubAgentTask, opts: AcceptanceReviewOptions):
    default_timeout = AcceptanceReviewOptions().test_timeout_seconds
    needs_options_bundle = opts.execute_tests or opts.test_timeout_seconds != default_timeout or opts.now is not None
    if needs_options_bundle:
        return manager._review_acceptance_task(task, options=opts)
    return manager._review_acceptance_task(
        task,
        apply=opts.apply,
        reviewer=opts.reviewer,
        note=opts.note,
    )


# LLM: _acceptance_report_summary 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 处理验收报告summary相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
def _acceptance_report_summary(records: list[AcceptanceReviewRecord]) -> dict[str, int]:
    summary: dict[str, int] = {"total": len(records)}
    for record in records:
        summary[record.decision] = summary.get(record.decision, 0) + 1
        status_key = "ok" if record.ok else "failed"
        mode_key = "dry_run" if record.dry_run else "applied"
        summary[status_key] = summary.get(status_key, 0) + 1
        summary[mode_key] = summary.get(mode_key, 0) + 1
    return summary
