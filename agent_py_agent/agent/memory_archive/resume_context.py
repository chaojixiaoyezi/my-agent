from __future__ import annotations

"""LLM: builds optional auto-injected recovery context from memory archive evidence.

给人看的解释：
这个文件负责“用户说继续时，要不要自动把恢复线索塞进 prompt”。
它只读归档、LocalStore 和任务事实源，不写任何业务状态；默认配置关闭，避免拖慢普通对话。
"""

from dataclasses import dataclass
import re
from types import SimpleNamespace
from typing import Any

from .query import (
    build_resume_guidance,
    collect_archive_records,
    collect_resume_task_ids,
    collect_task_payloads,
    filter_archive_records,
    local_hit_payload,
    resume_local_query,
)
from .resume_brief import build_resume_brief


_ID_PATTERN = re.compile(r"(subagent-[A-Za-z0-9_.:-]+|gwreq-[A-Za-z0-9_.:-]+|request-[A-Za-z0-9_.:-]+)")
_TRIGGER_KEYWORDS = (
    "继续",
    "接着",
    "刚刚",
    "上次",
    "恢复",
    "找回",
    "断片",
    "还没完",
    "resume",
    "recover",
    "memory-resume",
    "request_id",
    "run_id",
    "subagent-",
    "gwreq-",
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


@dataclass(frozen=True)
class ResumeContextResult:
    """LLM: result of optional recovery context lookup for one run.

    给人看的解释：
    这不是模型回答，只是“有没有找到可注入的恢复块”的结果。
    运行主流程会用它决定是否往 prompt 的 Runtime Injection 里加内容。
    """

    context_block: str = ""
    query: str = ""
    archive_match_count: int = 0
    local_match_count: int = 0
    task_fact_source_count: int = 0
    reason: str = ""
    error: str = ""

    @property
    def injected(self) -> bool:
        """LLM: true when a non-empty recovery block should enter the prompt.

        给人看的解释：
        只要 `context_block` 有内容，就说明这轮确实找到了恢复线索，可以注入。
        """

        return bool(self.context_block.strip())


def build_auto_resume_context(
    agent: Any,
    user_prompt: str,
    *,
    enabled: bool | None = None,
) -> ResumeContextResult:
    """LLM: conditionally build a recovery context block from recent archive evidence.

    给人看的解释：
    默认不开。配置打开后，只有“继续/恢复/刚刚/request_id/run_id”这类场景才查归档。
    如果查归档或 LocalStore 出错，不影响主对话，只返回 error 让结果里可观察。
    """

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


def _build_resume_context(agent: Any, user_prompt: str) -> ResumeContextResult:
    """LLM: query archive/local/task fact sources and render one stable recovery block.

    给人看的解释：
    这一步和 `memory-resume --context-only` 是同一套思路：
    archive 是线索，任务目录才是事实源，最终输出一段短小稳定的 `Recovery Brief`。
    """

    limit = int(getattr(agent.config, "memory_resume_auto_context_limit", 5) or 5)
    records = collect_archive_records(agent.root, layer="all", date_key=None, limit=0)
    archive_matches, query = _first_archive_matches(records, user_prompt, limit=limit)
    args = _resume_args(query)
    local_query = resume_local_query(args, archive_matches)
    local_hits = (
        agent.local_store.search(local_query, limit=limit)
        if local_query
        else agent.local_store.list_recent(limit=limit)
    )
    local_payloads = [local_hit_payload(hit) for hit in local_hits]
    task_ids = collect_resume_task_ids(args, archive_matches, local_payloads)
    task_payloads = collect_task_payloads(agent, task_ids, limit=limit)
    if not archive_matches and not local_payloads and not task_payloads:
        return ResumeContextResult(query=query, reason="no_evidence")
    resume = build_resume_guidance(archive_matches, local_payloads, task_payloads)
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


def _first_archive_matches(records: list[dict[str, Any]], user_prompt: str, *, limit: int) -> tuple[list[dict[str, Any]], str]:
    """LLM: try precise IDs, full prompt, then useful terms before falling back to recent trigger context.

    给人看的解释：
    用户经常说“继续 README 那个任务”，归档里未必有“那个”两个字。
    所以这里会先试完整句，没命中再试 README、request_id、run_id 这类更稳的词。
    """

    for query in _candidate_queries(user_prompt):
        matches = filter_archive_records(records, query=query, filters={}, since=None, until=None)[:limit]
        if matches:
            return matches, query
    if _has_resume_trigger(user_prompt):
        return filter_archive_records(records, query="", filters={}, since=None, until=None)[:limit], ""
    return [], ""


def _candidate_queries(user_prompt: str) -> list[str]:
    """LLM: derive small archive search terms from one user prompt.

    给人看的解释：
    不做复杂分词，只提取明显 ID、完整句和空格/标点切开的关键词。
    这样实现轻，不会为了恢复上下文引入重依赖。
    """

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


def _has_resume_trigger(user_prompt: str) -> bool:
    """LLM: detect prompts that are likely asking for recovery/continuation.

    给人看的解释：
    自动注入只在这些明显场景触发，避免每句普通聊天都去扫归档。
    """

    lowered = user_prompt.lower()
    return any(keyword.lower() in lowered for keyword in _TRIGGER_KEYWORDS)


def _resume_args(query: str) -> SimpleNamespace:
    """LLM: build the minimal args-like object reused by resume query helpers.

    给人看的解释：
    原来的恢复查询函数是给 CLI 用的，会从 `args.query/run_id` 这些字段读参数。
    自动注入不是命令行，所以这里造一个最小对象，让两边复用同一套逻辑。
    """

    return SimpleNamespace(
        query=query,
        run_id="",
        request_id="",
        session_id="",
        task_id="",
    )


def _append(items: list[str], value: str) -> None:
    """LLM: append a non-empty string once while preserving order.

    给人看的解释：
    候选搜索词要保持顺序，也不要重复。
    这个小函数就是“有内容才加、加过就不再加”。
    """

    text = str(value or "").strip()
    if text and text not in items:
        items.append(text)
