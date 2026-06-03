from __future__ import annotations

"""Channel probe service for subagent run health checks."""

import json
import time
from dataclasses import asdict
from pathlib import Path

from ..models import (
    ChannelProbeCheck,
    ChannelProbeReport,
    ChannelProbeResult,
    SubAgentChannelProbeOptions,
    SubAgentTask,
)
from ..probe import (
    _channel_status,
    _probe_fail,
    _probe_json_file,
    _probe_ok,
    _probe_writable_dir,
)
from ..runner_rendering import (
    render_channel_probe_markdown,
    render_single_channel_probe_markdown,
)
from ..utils import _apply_missing_paths
from .indexing.params import IndexReportParams


class SubAgentChannelProbeService:
    """Checks run-local channel health and writes probe evidence."""

    def __init__(self, manager: object) -> None:
        self.manager = manager

    def probe_channel(self, run_id: str) -> ChannelProbeResult:
        task = self.manager.load(run_id)
        _apply_missing_paths(task, self.manager._build_work_order_paths(task.id, task.task_dir or None))
        now = time.time()
        checks: list[ChannelProbeCheck] = []
        checks.extend(self.probe_work_order_check(run_id, task, now))
        checks.extend(self.probe_json_files(task, now))
        checks.append(_probe_writable_dir("scratch_writable", Path(task.scratch_dir), "P0", now))
        result = ChannelProbeResult(
            run_id=task.id,
            channel_status=_channel_status(checks),
            checks=checks,
            task_dir=task.task_dir,
            goal=task.goal,
            created_at=now,
        )
        result.checks.append(self.write_channel_probe_files(task, result))
        result.channel_status = _channel_status(result.checks)
        self.update_task_from_probe(task, result.checks, now)
        self.manager._index_channel_probe(result)
        return result

    def probe_channels(
        self,
        run_ids: list[str] | None = None,
        *,
        params: SubAgentChannelProbeOptions | None = None,
        limit: int = 0,
    ) -> ChannelProbeReport:
        options = channel_probe_options(params, run_ids=run_ids, limit=limit)
        selected = options.run_ids or [task.id for task in self.manager.list_runs()]
        if options.limit > 0:
            selected = selected[: options.limit]
        results: list[ChannelProbeResult] = []
        for run_id in selected:
            try:
                results.append(self.probe_channel(run_id))
            except FileNotFoundError:
                continue
        return ChannelProbeReport(
            generated_at=time.time(),
            summary=channel_probe_summary(results),
            results=results,
        )

    def write_channel_probe_report(
        self,
        run_ids: list[str] | None = None,
        *,
        params: SubAgentChannelProbeOptions | None = None,
        limit: int = 0,
    ) -> ChannelProbeReport:
        report = self.probe_channels(
            params=channel_probe_options(params, run_ids=run_ids, limit=limit),
        )
        workspace = self.manager.workspace
        (workspace / "subagent_channel_probe.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (workspace / "SUBAGENT_CHANNEL_PROBE.md").write_text(
            render_channel_probe_markdown(report),
            encoding="utf-8",
        )
        self.manager._index_report(
            IndexReportParams(
                "subagent_channel_probe_report",
                "latest",
                "Subagent channel probe report",
                report,
                "subagent_channel_probe_report_written",
            ),
        )
        return report

    def probe_work_order_check(
        self,
        run_id: str,
        task: SubAgentTask,
        now: float,
    ) -> list[ChannelProbeCheck]:
        validation = self.manager.validate_work_order(run_id)
        if validation.ok:
            return [
                _probe_ok(
                    "work_order_files",
                    "标准工单目录和关键文件完整。",
                    severity="P0",
                    evidence_path=task.task_dir,
                    created_at=now,
                )
            ]
        return [
            _probe_fail(
                "work_order_files",
                f"缺少 {len(validation.missing)} 个关键路径。",
                severity="P0",
                error="; ".join(validation.missing[:10]),
                evidence_path=task.task_dir,
                created_at=now,
            )
        ]

    def probe_json_files(self, task: SubAgentTask, now: float) -> list[ChannelProbeCheck]:
        return [
            _probe_json_file("task_json_readable", Path(task.task_dir) / "task.json", "P0", now),
            _probe_json_file("run_json_readable", Path(task.task_dir) / "run.json", "P1", now),
            _probe_json_file("output_json_readable", Path(task.output_json), "P1", now),
            _probe_json_file("dependencies_json_readable", Path(task.dependencies_json), "P1", now),
        ]

    def update_task_from_probe(
        self,
        task: SubAgentTask,
        checks: list[ChannelProbeCheck],
        now: float,
    ) -> None:
        task.channel_checks = checks
        task.channel_status = _channel_status(checks)
        task.last_probe_at = now
        task.updated_at = now
        if task.channel_status == "BROKEN":
            task.failure_type = "channel"
        self.manager.save(task)

    def write_channel_probe_files(
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


def channel_probe_summary(results: list[ChannelProbeResult]) -> dict[str, int]:
    summary: dict[str, int] = {"total": len(results)}
    for result in results:
        summary[result.channel_status] = summary.get(result.channel_status, 0) + 1
        failed_check_names = [check.name for check in result.checks if not check.ok]
        for name in failed_check_names:
            summary[name] = summary.get(name, 0) + 1
    return summary


def channel_probe_options(
    params: SubAgentChannelProbeOptions | None,
    *,
    run_ids: list[str] | None,
    limit: int,
) -> SubAgentChannelProbeOptions:
    if params is not None:
        if not isinstance(params, SubAgentChannelProbeOptions):
            raise TypeError("channel probe requires params: SubAgentChannelProbeOptions")
        return params
    return SubAgentChannelProbeOptions(run_ids=run_ids, limit=limit)


__all__ = [
    "SubAgentChannelProbeService",
    "channel_probe_options",
    "channel_probe_summary",
]
