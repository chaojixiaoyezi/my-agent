from __future__ import annotations

"""LLM contract: SubAgentDispatchMixin methods grouped by one subagent responsibility.

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
from .rendering import (
    render_dispatch_markdown,
    render_dispatch_watch_markdown,
    render_parent_planner_markdown,
)
from .reports import (
    DispatchRecord,
    DispatchReport,
    DispatchWatchRecord,
    DispatchWatchReport,
    ParentPlannerRecord,
    ParentPlannerReport,
)
from .runner_rendering import _render_runner_item_line
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

class SubAgentDispatchMixin:
    def make_dispatch_record(
        self,
        *,
        step: str,
        action: str,
        run_id: str = "",
        dry_run: bool = True,
        applied: bool = False,
        ok: bool = True,
        message: str = "",
        before_status: str = "",
        after_status: str = "",
        before_verification_status: str = "",
        after_verification_status: str = "",
        evidence_paths: list[str] | None = None,
    ) -> DispatchRecord:
        """创建一条调度器审计记录。"""

        return DispatchRecord(
            id=_new_id("dispatch"),
            step=step,
            action=action,
            run_id=run_id,
            dry_run=dry_run,
            applied=applied,
            ok=ok,
            message=message,
            before_status=before_status,
            after_status=after_status,
            before_verification_status=before_verification_status,
            after_verification_status=after_verification_status,
            evidence_paths=evidence_paths or [],
            created_at=time.time(),
        )

    def build_dispatch_report(
        self,
        records: list[DispatchRecord],
        *,
        dry_run: bool,
    ) -> DispatchReport:
        """汇总调度器审计记录。"""

        summary: dict[str, int] = {"total": len(records)}
        for record in records:
            summary[record.step] = summary.get(record.step, 0) + 1
            summary[record.action] = summary.get(record.action, 0) + 1
            summary["ok" if record.ok else "failed"] = summary.get(
                "ok" if record.ok else "failed",
                0,
            ) + 1
            summary["applied" if record.applied else "dry_run"] = summary.get(
                "applied" if record.applied else "dry_run",
                0,
            ) + 1
        return DispatchReport(
            generated_at=time.time(),
            dry_run=dry_run,
            summary=summary,
            records=records,
        )

    def write_dispatch_report(
        self,
        report: DispatchReport,
        *,
        append_log: bool = False,
    ) -> DispatchReport:
        """写出调度器报告和可选审计日志。"""

        (self.workspace / "subagent_dispatch_report.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.workspace / "SUBAGENT_DISPATCH.md").write_text(
            render_dispatch_markdown(report),
            encoding="utf-8",
        )
        if append_log:
            for record in report.records:
                self._append_dispatch_log(record)
        for record in report.records:
            self._index_dispatch_record(record)
        self._index_report(
            "subagent_dispatch_report",
            "latest",
            "Subagent dispatch report",
            report,
            event_type="subagent_dispatch_report_written",
        )
        return report

    def make_dispatch_watch_record(
        self,
        *,
        cycle: int,
        dry_run: bool,
        ok: bool,
        message: str,
        dispatch_record_count: int,
        dispatch_summary: dict[str, int] | None = None,
        started_at: float = 0.0,
        ended_at: float = 0.0,
        evidence_paths: list[str] | None = None,
    ) -> DispatchWatchRecord:
        """创建一条 watch 循环记录。"""

        return DispatchWatchRecord(
            id=_new_id("watch"),
            cycle=cycle,
            dry_run=dry_run,
            ok=ok,
            message=message,
            dispatch_record_count=dispatch_record_count,
            dispatch_summary=dispatch_summary or {},
            started_at=started_at,
            ended_at=ended_at,
            evidence_paths=evidence_paths or [],
        )

    def build_dispatch_watch_report(
        self,
        records: list[DispatchWatchRecord],
        *,
        dry_run: bool,
    ) -> DispatchWatchReport:
        """汇总 watch 循环记录。"""

        summary: dict[str, int] = {"total": len(records)}
        for record in records:
            summary["ok" if record.ok else "failed"] = summary.get(
                "ok" if record.ok else "failed",
                0,
            ) + 1
            summary["dry_run" if record.dry_run else "applied"] = summary.get(
                "dry_run" if record.dry_run else "applied",
                0,
            ) + 1
            summary["dispatch_records"] = summary.get("dispatch_records", 0) + record.dispatch_record_count
        return DispatchWatchReport(
            generated_at=time.time(),
            dry_run=dry_run,
            summary=summary,
            records=records,
        )

    def write_dispatch_watch_report(self, report: DispatchWatchReport) -> DispatchWatchReport:
        """写出 watch 模式报告。"""

        (self.workspace / "subagent_dispatch_watch_report.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.workspace / "SUBAGENT_DISPATCH_WATCH.md").write_text(
            render_dispatch_watch_markdown(report),
            encoding="utf-8",
        )
        self._index_report(
            "subagent_dispatch_watch_report",
            "latest",
            "Subagent dispatch watch report",
            report,
            event_type="subagent_dispatch_watch_report_written",
        )
        return report

    def write_dispatch_watch_heartbeat(
        self,
        *,
        cycle: int,
        status: str,
        lock_path: str,
        pid: int,
        message: str = "",
    ) -> Path:
        """写出 watch heartbeat，方便外部知道父代理还活着。"""

        path = self.workspace / "subagent_dispatch_watch_heartbeat.json"
        payload = {
            "cycle": cycle,
            "status": status,
            "lock_path": lock_path,
            "pid": pid,
            "message": message,
            "updated_at": time.time(),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def write_parent_planner_exchange(self, prompt: str, response: str = "") -> tuple[str, str]:
        """写出最近一次父代理 planner 的 prompt / response。"""

        prompt_path = self.workspace / "parent_planner_prompt.md"
        response_path = self.workspace / "parent_planner_response.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        if response:
            response_path.write_text(response, encoding="utf-8")
        return str(prompt_path), str(response_path)

    def make_parent_planner_record(
        self,
        *,
        dry_run: bool,
        triggered: bool,
        ok: bool,
        decision: str,
        message: str,
        gate_summary: dict[str, int] | None = None,
        backend: str = "",
        tool_rounds: int = 0,
        parse_error: str = "",
        summary: str = "",
        actions: list[dict[str, object]] | None = None,
        blockers: list[str] | None = None,
        risks: list[str] | None = None,
        notes: list[str] | None = None,
        runner_instruction: str = "",
        suggested_max_runners: int = 0,
        prompt_path: str = "",
        response_path: str = "",
        evidence_paths: list[str] | None = None,
    ) -> ParentPlannerRecord:
        """创建父代理 planner 审计记录。"""

        return ParentPlannerRecord(
            id=_new_id("planner"),
            dry_run=dry_run,
            triggered=triggered,
            ok=ok,
            decision=decision,
            message=message,
            gate_summary=gate_summary or {},
            backend=backend,
            tool_rounds=tool_rounds,
            parse_error=parse_error,
            summary=summary,
            actions=actions or [],
            blockers=blockers or [],
            risks=risks or [],
            notes=notes or [],
            runner_instruction=runner_instruction,
            suggested_max_runners=suggested_max_runners,
            prompt_path=prompt_path,
            response_path=response_path,
            evidence_paths=evidence_paths or [],
            created_at=time.time(),
        )

    def build_parent_planner_report(
        self,
        records: list[ParentPlannerRecord],
        *,
        dry_run: bool,
    ) -> ParentPlannerReport:
        """汇总父代理 planner 记录。"""

        summary: dict[str, int] = {"total": len(records)}
        for record in records:
            summary["triggered" if record.triggered else "skipped"] = summary.get(
                "triggered" if record.triggered else "skipped",
                0,
            ) + 1
            summary["ok" if record.ok else "failed"] = summary.get(
                "ok" if record.ok else "failed",
                0,
            ) + 1
            summary[record.decision] = summary.get(record.decision, 0) + 1
        return ParentPlannerReport(
            generated_at=time.time(),
            dry_run=dry_run,
            summary=summary,
            records=records,
        )

    def write_parent_planner_report(
        self,
        report: ParentPlannerReport,
        *,
        append_log: bool = False,
    ) -> ParentPlannerReport:
        """写出父代理 planner 报告和可选审计日志。"""

        (self.workspace / "parent_planner_report.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.workspace / "PARENT_PLANNER.md").write_text(
            render_parent_planner_markdown(report),
            encoding="utf-8",
        )
        if append_log:
            for record in report.records:
                self.append_parent_planner_log(record)
        for record in report.records:
            self._index_parent_planner_record(record)
        self._index_report(
            "parent_planner_report",
            "latest",
            "Parent planner report",
            report,
            event_type="parent_planner_report_written",
        )
        return report

    def _append_dispatch_log(self, record: DispatchRecord) -> None:
        """写入全局调度器审计日志。"""

        jsonl = self.workspace / "subagent_dispatch_log.jsonl"
        append_jsonl(jsonl, asdict(record))

        markdown = self.workspace / "DISPATCH_LOG.md"
        if not markdown.exists():
            markdown.write_text("# DISPATCH LOG\n\n", encoding="utf-8")
        with markdown.open("a", encoding="utf-8") as handle:
            status = "OK" if record.ok else "FAIL"
            run = record.run_id or "global"
            handle.write(
                f"- [{status}] {record.id} step={record.step} action={record.action} "
                f"run={run} applied={record.applied} message={record.message}\n"
            )
        self._index_dispatch_record(record)

    def append_dispatch_watch_log(self, record: DispatchWatchRecord) -> None:
        """写入全局 watch 审计日志。"""

        jsonl = self.workspace / "subagent_dispatch_watch_log.jsonl"
        append_jsonl(jsonl, asdict(record))

        markdown = self.workspace / "DISPATCH_WATCH_LOG.md"
        if not markdown.exists():
            markdown.write_text("# DISPATCH WATCH LOG\n\n", encoding="utf-8")
        with markdown.open("a", encoding="utf-8") as handle:
            status = "OK" if record.ok else "FAIL"
            handle.write(
                f"- [{status}] {record.id} cycle={record.cycle} "
                f"records={record.dispatch_record_count} message={record.message}\n"
            )
        self._index_dispatch_watch_record(record)

    def append_parent_planner_log(self, record: ParentPlannerRecord) -> None:
        """写入全局父代理 planner 审计日志。"""

        jsonl = self.workspace / "parent_planner_log.jsonl"
        append_jsonl(jsonl, asdict(record))

        markdown = self.workspace / "PARENT_PLANNER_LOG.md"
        if not markdown.exists():
            markdown.write_text("# PARENT PLANNER LOG\n\n", encoding="utf-8")
        with markdown.open("a", encoding="utf-8") as handle:
            status = "OK" if record.ok else "FAIL"
            handle.write(
                f"- [{status}] {record.id} decision={record.decision} "
                f"triggered={record.triggered} message={record.message}\n"
            )
        self._index_parent_planner_record(record)
