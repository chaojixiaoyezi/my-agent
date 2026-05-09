# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""dispatch record and reporting service.

给人看的解释：
这里承接调度记录生成、汇总和写出逻辑。
SubAgentManager 通过 facade 方法委托到这里。
"""

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .dispatch_params import DispatchRecordParams, DispatchWatchHeartbeatParams
from .indexing_params import IndexReportParams
from .parent_planner_builder import ParentPlannerBuilder

if TYPE_CHECKING:
    from ..reports import (
        DispatchRecord,
        DispatchReport,
        DispatchWatchRecord,
        DispatchWatchReport,
        ParentPlannerRecord,
        ParentPlannerReport,
    )

# LLM: SubAgentDispatchService 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 封装subagent调度服务操作，把状态读写和错误处理收束在服务层；关键副作用: 方法可能触发任务状态、报告记录和持久化副作用相关副作用，需保持公开契约稳定。
class SubAgentDispatchService:

    # LLM: __init__ 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 初始化实例依赖和配置字段，为后续方法调用准备共享状态；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
    def __init__(self, manager: Any):
        self.manager = manager

    # LLM: make_dispatch_record 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 构建make调度记录所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
    def make_dispatch_record(
        self,
        *,
        params: DispatchRecordParams,
    ) -> DispatchRecord:
        return self._make_dispatch_record(params)

    # LLM: _make_dispatch_record 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 构建make调度记录所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
    def _make_dispatch_record(self, params: DispatchRecordParams) -> DispatchRecord:
        from .dispatch_record_builder import DispatchRecordBuilder

        return DispatchRecordBuilder.make_record(self.manager, params)

    # LLM: build_dispatch_report 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 构建报告所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会影响任务状态、报告记录和持久化副作用，需保持重试、超时和状态迁移语义。
    def build_dispatch_report(
        self,
        records: list[DispatchRecord],
        *,
        dry_run: bool,
    ) -> DispatchReport:
        from ..reports import DispatchReport
        from .dispatch_record_builder import DispatchRecordBuilder

        summary = DispatchRecordBuilder.build_summary(records)
        return DispatchReport(
            generated_at=time.time(),
            dry_run=dry_run,
            summary=summary,
            records=records,
        )

    # LLM: write_dispatch_report 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 写入报告的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
    def write_dispatch_report(
        self,
        report: DispatchReport,
        *,
        append_log: bool = False,
    ) -> DispatchReport:
        (self.manager.workspace / "subagent_dispatch_report.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.manager.workspace / "SUBAGENT_DISPATCH.md").write_text(
            self.manager._render_dispatch_markdown(report),
            encoding="utf-8",
        )
        if append_log:
            from .dispatch_log_appender import DispatchLogAppender

            for record in report.records:
                DispatchLogAppender.append(record, self.manager.workspace)
        for record in report.records:
            self.manager._index_dispatch_record(record)
        self.manager._index_report(
            IndexReportParams(
                "subagent_dispatch_report", "latest",
                "Subagent dispatch report", report,
                "subagent_dispatch_report_written",
            ),
        )
        return _trace_written_dispatch_report(self.manager, report)

    # LLM: make_dispatch_watch_record 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 构建make调度监控记录所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
    def make_dispatch_watch_record(
        self,
        *,
        params: DispatchWatchRecordParams,
    ) -> DispatchWatchRecord:
        from .dispatch_watch_builder import DispatchWatchBuilder

        return DispatchWatchBuilder.make_record(self.manager, params=params)

    # LLM: build_dispatch_watch_report 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 构建报告所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会影响任务状态、报告记录和持久化副作用，需保持重试、超时和状态迁移语义。
    def build_dispatch_watch_report(
        self,
        records: list[DispatchWatchRecord],
        *,
        dry_run: bool,
    ) -> DispatchWatchReport:
        from ..reports import DispatchWatchReport
        from .dispatch_watch_builder import DispatchWatchBuilder

        summary = DispatchWatchBuilder.build_summary(records)
        return DispatchWatchReport(
            generated_at=time.time(),
            dry_run=dry_run,
            summary=summary,
            records=records,
        )

    # LLM: write_dispatch_watch_report 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 写入报告的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
    def write_dispatch_watch_report(self, report: DispatchWatchReport) -> DispatchWatchReport:
        (self.manager.workspace / "subagent_dispatch_watch_report.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.manager.workspace / "SUBAGENT_DISPATCH_WATCH.md").write_text(
            self.manager._render_dispatch_watch_markdown(report),
            encoding="utf-8",
        )
        self.manager._index_report(
            IndexReportParams(
                "subagent_dispatch_watch_report", "latest",
                "Subagent dispatch watch report", report,
                "subagent_dispatch_watch_report_written",
            ),
        )
        return _trace_written_dispatch_watch_report(self.manager, report)

    # LLM: write_dispatch_watch_heartbeat 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 写入heartbeat的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
    def write_dispatch_watch_heartbeat(
        self,
        *,
        params: DispatchWatchHeartbeatParams,
    ) -> Path:
        path = self.manager.workspace / "subagent_dispatch_watch_heartbeat.json"
        payload = {
            "cycle": params.cycle,
            "status": params.status,
            "lock_path": params.lock_path,
            "pid": params.pid,
            "message": params.message,
            "updated_at": time.time(),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    # LLM: write_parent_planner_exchange 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 写入父级规划器exchange的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
    def write_parent_planner_exchange(self, prompt: str, response: str = "") -> tuple[str, str]:
        prompt_path = self.manager.workspace / "parent_planner_prompt.md"
        response_path = self.manager.workspace / "parent_planner_response.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        if response:
            response_path.write_text(response, encoding="utf-8")
        return str(prompt_path), str(response_path)

    # LLM: make_parent_planner_record 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 构建父级规划器记录所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
    def make_parent_planner_record(
        self,
        *,
        params: ParentPlannerRecordParams,
    ) -> ParentPlannerRecord:
        from .parent_planner_builder import ParentPlannerBuilder

        return ParentPlannerBuilder.make_record(self.manager, params=params)

    # LLM: build_parent_planner_report 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 构建父级规划器报告所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
    def build_parent_planner_report(
        self,
        records: list[ParentPlannerRecord],
        *,
        dry_run: bool,
    ) -> ParentPlannerReport:
        from ..reports import ParentPlannerReport
        from .parent_planner_builder import ParentPlannerBuilder

        summary = ParentPlannerBuilder.build_summary(records)
        return ParentPlannerReport(
            generated_at=time.time(),
            dry_run=dry_run,
            summary=summary,
            records=records,
        )

    # LLM: write_parent_planner_report 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 写入父级规划器报告的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
    def write_parent_planner_report(
        self,
        report: ParentPlannerReport,
        *,
        append_log: bool = False,
    ) -> ParentPlannerReport:
        (self.manager.workspace / "parent_planner_report.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.manager.workspace / "PARENT_PLANNER.md").write_text(
            self.manager._render_parent_planner_markdown(report),
            encoding="utf-8",
        )
        if append_log:
            from .parent_planner_builder import ParentPlannerLogAppender

            for record in report.records:
                ParentPlannerLogAppender.append(record, self.manager.workspace, self.manager)
        for record in report.records:
            self.manager._index_parent_planner_record(record)
        self.manager._index_report(
            IndexReportParams(
                "parent_planner_report", "latest",
                "Parent planner report", report,
                "parent_planner_report_written",
            ),
        )
        return report

    # LLM: append_dispatch_watch_log 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 写入append调度监控log的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
    def append_dispatch_watch_log(self, record: DispatchWatchRecord) -> None:
        from .dispatch_watch_log_appender import DispatchWatchLogAppender

        DispatchWatchLogAppender.append(record, self.manager.workspace, self.manager)

    # LLM: append_parent_planner_log 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
    # 函数用途: 写入父级规划器log的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
    def append_parent_planner_log(self, record: ParentPlannerRecord) -> None:
        from .parent_planner_builder import ParentPlannerLogAppender

        ParentPlannerLogAppender.append(record, self.manager.workspace, self.manager)


# LLM: _trace_written_dispatch_report keeps dispatch trace glue outside the service class body.
# 函数用途: 写 dispatch 报告后的 bounded trace，详细记录仍留在报告文件。
def _trace_written_dispatch_report(manager: Any, report: DispatchReport) -> DispatchReport:
    from ..debug_trace_reports import trace_dispatch_report

    return trace_dispatch_report(manager, report)


# LLM: _trace_written_dispatch_watch_report keeps watch trace glue outside the service class body.
# 函数用途: 写 dispatch-watch 报告后的 bounded trace，避免在 watch 日志里重复正文。
def _trace_written_dispatch_watch_report(
    manager: Any,
    report: DispatchWatchReport,
) -> DispatchWatchReport:
    from ..debug_trace_reports import trace_dispatch_watch_report

    return trace_dispatch_watch_report(manager, report)
