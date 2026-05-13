# LLM: Resume guidance builds compact refs-only recovery advice from archive/local/task/gateway facts.
# 模块用途: 根据恢复线索生成推荐读取路径和下一步动作。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# LLM: ResumeGuidanceRequest is the bundle interface for resume advice construction.
# 类用途: 打包 archive、local、task、gateway 四类恢复线索和读取路径上限。
@dataclass(frozen=True)
class ResumeGuidanceRequest:
    archive_matches: list[dict[str, Any]]
    local_hits: list[dict[str, Any]]
    task_payloads: list[dict[str, Any]]
    gateway_payloads: list[dict[str, Any]] | None = None
    recommended_read_paths_limit: int = 20


# LLM: build_resume_guidance returns counts, read-path refs, and recovery next actions without reading bodies.
# 函数用途: 组装恢复建议，告诉调用方应该先读哪些事实源、下一步怎么恢复。
def build_resume_guidance(request: ResumeGuidanceRequest) -> dict[str, Any]:
    gateway_items = request.gateway_payloads or []
    return {
        "archive_match_count": len(request.archive_matches),
        "local_match_count": len(request.local_hits),
        "task_fact_source_count": len(request.task_payloads),
        "gateway_fact_source_count": len(gateway_items),
        "recommended_read_paths": _recommended_resume_reads(request, gateway_items),
        "next_actions": _resume_next_actions(request.task_payloads, gateway_items),
    }


# LLM: _recommended_resume_reads deduplicates fact-source paths and applies the configured cap.
# 函数用途: 从 task/gateway/local 线索中生成去重后的推荐读取路径。
def _recommended_resume_reads(
    request: ResumeGuidanceRequest,
    gateway_items: list[dict[str, Any]],
) -> list[str]:
    recommended_reads: list[str] = []
    for payload in (*request.task_payloads, *gateway_items):
        _append_paths(recommended_reads, payload.get("recommended_read_paths", []) or [])
    _append_paths(recommended_reads, (str(hit.get("content_path", "") or "") for hit in request.local_hits))
    return recommended_reads[: max(0, int(request.recommended_read_paths_limit))]


# LLM: _append_paths keeps ordered unique path refs for recovery output.
# 函数用途: 向目标列表追加非空路径，并保持顺序去重。
def _append_paths(target: list[str], paths) -> None:
    for path in paths:
        if path and path not in target:
            target.append(path)


# LLM: _resume_next_actions separates task authority repair advice from generic archive guidance.
# 函数用途: 根据 task/gateway 线索生成恢复下一步建议。
def _resume_next_actions(
    task_payloads: list[dict[str, Any]],
    gateway_payloads: list[dict[str, Any]],
) -> list[str]:
    next_actions = [
        "Read task fact sources before deciding whether work can continue.",
        "Treat archive matches as recovery clues, not final authority.",
    ]
    invalid_authority = _invalid_authority_ids(task_payloads)
    if invalid_authority:
        next_actions.append("Repair missing task authority files before resume: " + ", ".join(invalid_authority[:5]))
    if not task_payloads and not gateway_payloads:
        next_actions.append("Use memory-archive-search to narrow request_id/run_id/session_id first.")
    return next_actions


# LLM: _invalid_authority_ids extracts run ids with missing fact sources for human-readable guidance.
# 函数用途: 找出权威事实源不完整的 task/run id。
def _invalid_authority_ids(task_payloads: list[dict[str, Any]]) -> list[str]:
    return [
        task.get("run_id", "")
        for task in task_payloads
        if isinstance(task.get("authority_validation"), dict) and not task["authority_validation"].get("ok", False)
    ]
