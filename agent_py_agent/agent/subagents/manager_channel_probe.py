from __future__ import annotations

"""LLM contract: channel probe execution and report persistence.
Human version:
这个 mixin 只放一类 SubAgentManager 能力。它不单独实例化，
由 public SubAgentManager 组合使用，避免单个文件重新长成大杂烩。
"""
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..file_io import append_jsonl
from .models import ChannelProbeCheck, ChannelProbeReport, ChannelProbeResult, SubAgentTask
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
from .runner_rendering import (
    _render_runner_item_line,
    render_channel_probe_markdown,
    render_single_channel_probe_markdown,
)
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


def _channel_probe_summary(results: list[ChannelProbeResult]) -> dict[str, int]:
    summary: dict[str, int] = {"total": len(results)}
    for result in results:
        summary[result.channel_status] = summary.get(result.channel_status, 0) + 1
        failed_check_names = [check.name for check in result.checks if not check.ok]
        for name in failed_check_names:
            summary[name] = summary.get(name, 0) + 1
    return summary


class SubAgentChannelProbeMixin:
    def _probe_work_order_check(self, run_id: str, task: SubAgentTask, now: float) -> list[ChannelProbeCheck]:
        checks: list[ChannelProbeCheck] = []
        validation = self.validate_work_order(run_id)
        if validation.ok:
            checks.append(
                _probe_ok(
                    "work_order_files",
                    "标准工单目录和关键文件完整。",
                    severity="P0",
                    evidence_path=task.task_dir,
                    created_at=now,
                )
            )
        else:
            checks.append(
                _probe_fail(
                    "work_order_files",
                    f"缺少 {len(validation.missing)} 个关键路径。",
                    severity="P0",
                    error="; ".join(validation.missing[:10]),
                    evidence_path=task.task_dir,
                    created_at=now,
                )
            )
        return checks
    def _probe_json_files(self, task: SubAgentTask, now: float) -> list[ChannelProbeCheck]:
        return [
            _probe_json_file("task_json_readable", Path(task.task_dir) / "task.json", "P0", now),
            _probe_json_file("run_json_readable", Path(task.task_dir) / "run.json", "P1", now),
            _probe_json_file("output_json_readable", Path(task.output_json), "P1", now),
            _probe_json_file("dependencies_json_readable", Path(task.dependencies_json), "P1", now),
        ]
    def _update_task_from_probe(self, task: SubAgentTask, checks: list[ChannelProbeCheck], now: float) -> None:
        task.channel_checks = checks
        task.channel_status = _channel_status(checks)
        task.last_probe_at = now
        task.updated_at = now
        if task.channel_status == "BROKEN":
            task.failure_type = "channel"
        self.save(task)
    def probe_channel(self, run_id: str) -> ChannelProbeResult:
        """检查单个子代理运行的通道健康状态。
        这里的'通道'先指最基础的运行现场：
        工单文件、机器 JSON、任务目录写入和 probe 证据落盘。
        后续真正接入执行器时，再把模型 session、ACP adapter 等检查接进来。
        """
        task = self.load(run_id)
        _apply_missing_paths(task, self._build_work_order_paths(task.id, task.task_dir or None))
        now = time.time()
        checks: list[ChannelProbeCheck] = []
        checks.extend(self._probe_work_order_check(run_id, task, now))
        checks.extend(self._probe_json_files(task, now))
        checks.append(_probe_writable_dir("scratch_writable", Path(task.scratch_dir), "P0", now))
        status = _channel_status(checks)
        result = ChannelProbeResult(
            run_id=task.id,
            channel_status=status,
            checks=checks,
            task_dir=task.task_dir,
            goal=task.goal,
            created_at=now,
        )
        write_check = self._write_channel_probe_files(task, result)
        result.checks.append(write_check)
        task.channel_checks = result.checks
        task.channel_status = _channel_status(result.checks)
        result.channel_status = task.channel_status
        self._update_task_from_probe(task, result.checks, now)
        self._index_channel_probe(result)
        return result
    def probe_channels(
        self,
        run_ids: list[str] | None = None,
        *,
        limit: int = 0,
    ) -> ChannelProbeReport:
        selected = run_ids or [task.id for task in self.list_runs()]
        if limit > 0:
            selected = selected[:limit]
        results: list[ChannelProbeResult] = []
        for run_id in selected:
            try:
                results.append(self.probe_channel(run_id))
            except FileNotFoundError:
                continue
        return ChannelProbeReport(
            generated_at=time.time(),
            summary=_channel_probe_summary(results),
            results=results,
        )
    def write_channel_probe_report(
        self,
        run_ids: list[str] | None = None,
        *,
        limit: int = 0,
    ) -> ChannelProbeReport:
        report = self.probe_channels(run_ids, limit=limit)
        (self.workspace / "subagent_channel_probe.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.workspace / "SUBAGENT_CHANNEL_PROBE.md").write_text(
            render_channel_probe_markdown(report),
            encoding="utf-8",
        )
        self._index_report(
            "subagent_channel_probe_report",
            "latest",
            "Subagent channel probe report",
            report,
            event_type="subagent_channel_probe_report_written",
        )
        return report
    def _write_channel_probe_files(
        self,
        task: SubAgentTask,
        result: ChannelProbeResult,
    ) -> ChannelProbeCheck:
        now = time.time()
        try:
            probe_json = Path(task.logs_dir) / "last_channel_probe.json"
            probe_json.parent.mkdir(parents=True, exist_ok=True)
            probe_json.write_text(
                json.dumps(asdict(result), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            Path(task.channel_probe_file).write_text(
                render_single_channel_probe_markdown(result),
                encoding="utf-8",
            )
            return _probe_ok(
                "probe_evidence_writable",
                "probe 证据已写入任务目录。",
                severity="P1",
                evidence_path=str(probe_json),
                created_at=now,
            )
        except Exception as exc:
            return _probe_fail(
                "probe_evidence_writable",
                "probe 证据无法写入任务目录。",
                severity="P1",
                error=str(exc),
                evidence_path=task.channel_probe_file,
                created_at=now,
            )
