# LLM: Main-agent task runtime result helpers are shared by task and real_task tracks.
# 模块用途: 统一 timeout 摘要和 case report 字段映射，轨道模块只负责模型类型和验收入口。

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any


# LLM: activity_timeout_seconds derives a live-observation bound from total timeout.
# 函数用途: 长任务如果很久没有日志或文件活动，就提前停止并保留 checkpoint/日志证据。
def activity_timeout_seconds(task_timeout_seconds: int) -> int:
    total = max(1, int(task_timeout_seconds))
    if total <= 600:
        return total
    return min(total, max(600, total // 2))


# LLM: timeout_issues separates hard timeouts from already-valid deliverables.
# 函数用途: 超时时根据产物验收结果输出稳定 issue code，避免有效产物被误判失败。
def timeout_issues(acceptance: Any, *, timeout_reason: str) -> tuple[str, ...]:
    if acceptance.ok:
        return ("process_timeout_after_valid_artifact",)
    failed = int(acceptance.summary.get("failed", 0))
    issues = [timeout_reason or "timeout"]
    if failed:
        issues.append(f"artifact_acceptance_failed={failed}")
    return tuple(issues)


# LLM: case_result_kwargs maps a result bundle to public report fields.
# 函数用途: 统一 stdout/stderr 外置引用、acceptance 摘要、退出码和 issues 字段。
def case_result_kwargs(
    bundle: Any,
    *,
    rel: Callable[[Path, Path], str],
) -> dict[str, object]:
    runtime = bundle.runtime
    acceptance = bundle.acceptance
    return {
        "case_id": runtime.case.case_id,
        "title": runtime.case.title,
        "status": bundle.status,
        "worker_slot": runtime.case.worker_slot,
        "timeout_seconds": runtime.case.timeout_seconds,
        "prompt_ref": runtime.case.prompt_ref,
        "config_ref": rel(runtime.paths["config"], runtime.workspace),
        "command_ref": rel(runtime.paths["command"], runtime.workspace),
        "stdout_ref": rel(runtime.paths["stdout"], runtime.workspace),
        "stderr_ref": rel(runtime.paths["stderr"], runtime.workspace),
        "acceptance_report_ref": rel(runtime.paths["acceptance_report"], runtime.workspace),
        "events_ref": rel(runtime.paths["events"], runtime.workspace),
        "recovery_packet_ref": bundle.recovery_packet_ref,
        "acceptance_summary": dict(acceptance.summary if acceptance else {}),
        "exit_code": bundle.exit_code,
        "duration_seconds": bundle.duration_seconds,
        "issues": list(bundle.issues),
    }


__all__ = ["activity_timeout_seconds", "case_result_kwargs", "timeout_issues"]
