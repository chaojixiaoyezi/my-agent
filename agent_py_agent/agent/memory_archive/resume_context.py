
from __future__ import annotations

"""builds optional auto-injected recovery context from memory archive evidence.

新手说明:
这个文件负责'用户带结构化 run/request/subagent 引用时，要不要自动把恢复线索塞进 prompt'。
它只读归档、LocalStore 和任务事实源，不写任何业务状态；默认配置关闭，避免拖慢普通对话。
"""

import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ..settings.defaults import default_config_int
from .query import (
    ResumeGuidanceRequest,
    build_resume_guidance,
    collect_archive_records,
    collect_gateway_payloads,
    collect_resume_task_ids,
    collect_task_payloads,
    filter_archive_records,
    local_hit_payload,
    resume_local_query,
)
from .resume_brief import build_resume_brief

_ID_PATTERN = re.compile(r"(subagent-[A-Za-z0-9_.:-]+|gwreq-[A-Za-z0-9_.:-]+|request-[A-Za-z0-9_.:-]+)")


@dataclass(frozen=True)
class ResumeContextResult:

    context_block: str = ""
    query: str = ""
    archive_match_count: int = 0
    local_match_count: int = 0
    task_fact_source_count: int = 0
    reason: str = ""
    error: str = ""

    @property
    def injected(self) -> bool:

        return bool(self.context_block.strip())


def build_auto_resume_context(
    agent: Any,
    user_prompt: str,
    *,
    enabled: bool | None = None,
) -> ResumeContextResult:

    effective_enabled = (
        bool(getattr(agent.config, "memory_resume_auto_context_enabled", False))
        if enabled is None
        else bool(enabled)
    )
    if not effective_enabled:
        return ResumeContextResult(reason="disabled")
    mode = str(getattr(agent.config, "memory_resume_auto_context_mode", "trigger") or "trigger").strip().lower()
    explicit_enabled = enabled is True
    if explicit_enabled and mode == "off":
        mode = "always"
    if mode == "off":
        return ResumeContextResult(reason="off")
    if not explicit_enabled and mode != "always" and not _has_resume_trigger(user_prompt):
        return ResumeContextResult(reason="no_trigger")
    try:
        return _build_resume_context(agent, user_prompt)
    except Exception as exc:  # noqa: BLE001 - recovery context must never break the user request
        return ResumeContextResult(reason="error", error=f"{type(exc).__name__}: {exc}")


def _build_resume_context(agent: Any, user_prompt: str) -> ResumeContextResult:

    limit = _config_int(agent, "memory_resume_auto_context_limit")
    records = _collect_resume_archive_records(agent)
    archive_matches, query = _first_archive_matches(records, user_prompt, limit=limit)
    args = _resume_args(query)
    local_query = resume_local_query(args, archive_matches)
    local_payloads = _resume_local_payloads(agent, local_query, limit)
    task_ids = collect_resume_task_ids(args, archive_matches, local_payloads)
    task_payloads = collect_task_payloads(agent, task_ids, limit=limit)
    # Gateway 恢复也必须回到 request/response JSON，而不是只注入 LocalStore 摘要。
    gateway_payloads = collect_gateway_payloads(local_payloads, limit=limit)
    if not archive_matches and not local_payloads and not task_payloads and not gateway_payloads:
        return ResumeContextResult(query=query, reason="no_evidence")
    resume = build_resume_guidance(
        ResumeGuidanceRequest(
            archive_matches=archive_matches,
            local_hits=local_payloads,
            task_payloads=task_payloads,
            gateway_payloads=gateway_payloads,
            recommended_read_paths_limit=int(
                getattr(agent.config, "memory_resume_recommended_read_paths_limit", 20) or 0
            ),
        )
    )
    brief = build_resume_brief(
        archive_matches,
        local_payloads,
        task_payloads,
        recommended_read_paths=resume["recommended_read_paths"],
        next_actions=resume["next_actions"],
    )
    return ResumeContextResult(
        context_block=brief["context_block"],
        query=query,
        archive_match_count=len(archive_matches),
        local_match_count=len(local_payloads),
        task_fact_source_count=len(task_payloads),
        reason="matched",
    )


def _collect_resume_archive_records(agent: Any) -> list[dict[str, Any]]:
    roots = _resume_archive_roots(agent)
    scan_limit = _config_int(agent, "memory_resume_archive_scan_limit")
    file_limit = _config_int(agent, "memory_archive_search_file_limit")
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    for root in roots:
        records.extend(_new_archive_records(root, seen, scan_limit, file_limit))
    records.sort(key=lambda item: (item["created_at_sort"], item["file_path"], item["line_no"]), reverse=True)
    return records[:scan_limit] if scan_limit > 0 else records


def _new_archive_records(
    root: Path,
    seen: set[tuple[str, str, int]],
    scan_limit: int,
    file_limit: int,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for record in collect_archive_records(root, layer="all", date_key=None, limit=scan_limit, file_limit=file_limit):
        key = _archive_record_key(record)
        if key not in seen:
            seen.add(key)
            records.append(record)
    return records


def _archive_record_key(record: dict[str, Any]) -> tuple[str, str, int]:
    return (str(record.get("file_path") or ""), str(record.get("id") or ""), int(record.get("line_no") or 0))


def _resume_archive_roots(agent: Any) -> tuple[Path, ...]:
    roots: list[Path] = []
    owner_home = getattr(getattr(agent, "home_paths", None), "owner_home_dir", None)
    if owner_home:
        roots.append(Path(owner_home))
    return tuple(roots)


def _resume_local_payloads(agent: Any, local_query: str, limit: int) -> list[dict[str, Any]]:
    local_hits = (
        agent.local_store.search(local_query, limit=limit)
        if local_query
        else agent.local_store.list_recent(limit=limit)
    )
    preview_chars = int(getattr(agent.config, "memory_query_content_preview_chars", 500) or 0)
    return [local_hit_payload(hit, preview_chars=preview_chars) for hit in local_hits]


def _first_archive_matches(records: list[dict[str, Any]], user_prompt: str, *, limit: int) -> tuple[list[dict[str, Any]], str]:

    for query in _candidate_queries(user_prompt):
        matches = filter_archive_records(records, query=query, filters={}, since=None, until=None)[:limit]
        if matches:
            return matches, query
    if _has_resume_trigger(user_prompt):
        return filter_archive_records(records, query="", filters={}, since=None, until=None)[:limit], ""
    return [], ""


def _candidate_queries(user_prompt: str) -> list[str]:

    text = user_prompt.strip()
    values: list[str] = []
    for match in _ID_PATTERN.findall(text):
        _append(values, match)
    _append(values, text)
    for token in re.split(r"[\s,，。.!！?？:：;；/\\]+", text):
        token = token.strip().strip("`'\"")
        if not token:
            continue
        if len(token) >= 3:
            _append(values, token)
    return values


def _has_resume_trigger(user_prompt: str) -> bool:

    text = str(user_prompt or "")
    return bool(_ID_PATTERN.search(text))


def has_resume_trigger(user_prompt: str) -> bool:
    return _has_resume_trigger(user_prompt)


def _resume_args(query: str) -> SimpleNamespace:

    return SimpleNamespace(
        query=query,
        run_id="",
        request_id="",
        session_id="",
        task_id="",
    )


def _append(items: list[str], value: str) -> None:

    text = str(value or "").strip()
    if text and text not in items:
        items.append(text)


def _config_int(agent: Any, key: str) -> int:
    try:
        return max(0, int(getattr(agent.config, key)))
    except (AttributeError, TypeError, ValueError):
        return default_config_int(key, minimum=0)
