# LLM: Memory archive module; keep task/run workspace files and long-term memory records stable.
# 模块用途: 维护任务工作区、运行记录、compact 链和长期记忆归档。

from __future__ import annotations

"""builds optional auto-injected recovery context from memory archive evidence.

新手说明:
这个文件负责'用户说继续时，要不要自动把恢复线索塞进 prompt'。
它只读归档、LocalStore 和任务事实源，不写任何业务状态；默认配置关闭，避免拖慢普通对话。
"""

import re
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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
_EXPLICIT_RESUME_KEYWORDS = (
    "刚刚",
    "上次",
    "上回",
    "之前",
    "昨天",
    "前面",
    "旧任务",
    "历史任务",
    "恢复",
    "找回",
    "断片",
    "还没完",
    "resume",
    "recover",
    "memory-resume",
    "handoff",
    "gateway",
    "request_id",
    "run_id",
    "subagent-",
    "gwreq-",
)
_CONTINUE_WORDS = ("继续", "接着")
_CURRENT_TASK_CONTINUE_PHRASES = (
    "继续往下",
    "继续向下",
    "继续看",
    "继续读",
    "继续阅读",
    "继续分析",
    "继续整理",
    "继续写",
    "继续推进",
    "继续完成",
    "继续处理",
    "继续工作",
    "继续做",
    "接着往下",
    "接着看",
    "接着读",
    "接着分析",
    "接着整理",
    "接着写",
    "接着做",
)
_CONTINUE_WITH_EXPLICIT_TARGET = re.compile(
    r"(^|[\s,，。.!！?？:：;；/\\])(?:继续|接着)\s*"
    r"(?:[A-Za-z0-9_.:/-]{3,}|README|上次|上回|刚刚|昨天|之前|前面|那个|这个|旧任务|历史任务|handoff|gateway)",
    re.IGNORECASE,
)
_STOP_TERMS = {
    "继续",
    "接着",
    "刚刚",
    "上次",
    "那个",
    "这个",
    "任务",
    "恢复",
    "找回",
}


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 ResumeContextResult 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 ResumeContextResult 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass(frozen=True)
class ResumeContextResult:

    context_block: str = ""
    query: str = ""
    archive_match_count: int = 0
    local_match_count: int = 0
    task_fact_source_count: int = 0
    reason: str = ""
    error: str = ""

    # LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 injected 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 完成 injected 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
    @property
    def injected(self) -> bool:

        return bool(self.context_block.strip())


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 build_auto_resume_context 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 build auto resume context 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
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
    if enabled is True and mode == "off":
        mode = "trigger"
    if mode == "off":
        return ResumeContextResult(reason="off")
    if mode != "always" and not _has_resume_trigger(user_prompt):
        return ResumeContextResult(reason="no_trigger")
    try:
        return _build_resume_context(agent, user_prompt)
    except Exception as exc:  # noqa: BLE001 - recovery context must never break the user request
        return ResumeContextResult(reason="error", error=f"{type(exc).__name__}: {exc}")


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _build_resume_context 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 build resume context 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
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


# LLM: resume context reads the owner archive first, with legacy workspace archive only as fallback.
# 函数用途: 从 owner-home 和旧工作区归档收集恢复候选，避免 owner 迁移后“继续”找不到刚写入的 raw archive。
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
    legacy_root = Path(getattr(agent, "root", "."))
    if legacy_root not in roots:
        roots.append(legacy_root)
    return tuple(roots)


# LLM: _resume_local_payloads keeps resume context assembly shallow and preview-size config-backed.
# 函数用途: 查询 LocalStore 恢复线索，并转成可注入的轻量 payload。
def _resume_local_payloads(agent: Any, local_query: str, limit: int) -> list[dict[str, Any]]:
    local_hits = (
        agent.local_store.search(local_query, limit=limit)
        if local_query
        else agent.local_store.list_recent(limit=limit)
    )
    preview_chars = int(getattr(agent.config, "memory_query_content_preview_chars", 500) or 0)
    return [local_hit_payload(hit, preview_chars=preview_chars) for hit in local_hits]


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _first_archive_matches 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 判断 first archive matches 是否满足规则、查询或上下文条件，返回确定性的筛选结果。
def _first_archive_matches(records: list[dict[str, Any]], user_prompt: str, *, limit: int) -> tuple[list[dict[str, Any]], str]:

    for query in _candidate_queries(user_prompt):
        matches = filter_archive_records(records, query=query, filters={}, since=None, until=None)[:limit]
        if matches:
            return matches, query
    if _has_resume_trigger(user_prompt):
        return filter_archive_records(records, query="", filters={}, since=None, until=None)[:limit], ""
    return [], ""


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _candidate_queries 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 candidate queries 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _candidate_queries(user_prompt: str) -> list[str]:

    text = user_prompt.strip()
    values: list[str] = []
    for match in _ID_PATTERN.findall(text):
        _append(values, match)
    _append(values, text)
    for token in re.split(r"[\s,，。.!！?？:：;；/\\]+", text):
        token = token.strip().strip("`'\"")
        if not token or token in _STOP_TERMS:
            continue
        if len(token) >= 3 or any(ord(ch) > 127 for ch in token):
            _append(values, token)
    return values


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _has_resume_trigger 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 has resume trigger 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _has_resume_trigger(user_prompt: str) -> bool:

    text = str(user_prompt or "")
    lowered = text.lower()
    if _ID_PATTERN.search(text):
        return True
    if any(keyword.lower() in lowered for keyword in _EXPLICIT_RESUME_KEYWORDS):
        return True
    if not any(word in text for word in _CONTINUE_WORDS):
        return False
    if any(phrase in text for phrase in _CURRENT_TASK_CONTINUE_PHRASES):
        return False
    return bool(_CONTINUE_WITH_EXPLICIT_TARGET.search(text))


# LLM: has_resume_trigger is the public intent check shared by prompt-memory scoping.
# 函数用途: 判断用户是否明确在恢复或继续旧任务；只返回布尔值，不读取任何历史上下文。
def has_resume_trigger(user_prompt: str) -> bool:
    return _has_resume_trigger(user_prompt)


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _resume_args 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 收集或查询 resume args 的候选结果，并按参数完成筛选、排序或数量限制。
def _resume_args(query: str) -> SimpleNamespace:

    return SimpleNamespace(
        query=query,
        run_id="",
        request_id="",
        session_id="",
        task_id="",
    )


# LLM: memory archive 维护任务工作区、归档文件、gate 结果和快照；修改 _append 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 append 在当前模块中的核心转换或协调步骤，衔接 memory archive 维护任务工作区、归档文件、gate 结果和快照。
def _append(items: list[str], value: str) -> None:

    text = str(value or "").strip()
    if text and text not in items:
        items.append(text)


# LLM: _config_int keeps auto-resume archive scan limits sourced from AgentConfig.
# 函数用途: 读取恢复上下文扫描预算，非法值只回退到配置 schema 默认，不在 memory 模块写死数字。
def _config_int(agent: Any, key: str) -> int:
    try:
        return max(0, int(getattr(agent.config, key)))
    except (AttributeError, TypeError, ValueError):
        from ..settings.config import AgentConfig

        return max(0, int(getattr(AgentConfig(), key)))
