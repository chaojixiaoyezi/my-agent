# LLM: Board payload helpers keep the model-facing subagent board compact and recovery-friendly.
# 模块用途: 整理 subagent_board 输出里的可执行 run id 和长文本，避免主 orchestration 工具文件继续膨胀。

from __future__ import annotations


# LLM: board_actionable_run_ids puts status buckets before verbose board rows for LLM recovery.
# 函数用途: 把看板条目按状态整理成可继续 dispatch、验收或排障的 run id 列表，方便模型先看到关键 id。
def board_actionable_run_ids(items) -> dict[str, list[str]]:
    buckets = {"planning": [], "running": [], "awaiting_acceptance": [], "blocked": [], "done": []}
    for item in items:
        status = str(item.status or "").lower()
        key = status if status in buckets else ""
        if key:
            buckets[key].append(item.id)
    return {key: value for key, value in buckets.items() if value}


# LLM: board_status_filter normalizes model-friendly aliases before filtering board rows.
# 函数用途: 把空值、ALL、* 这类“查看全部”的自然写法归一成不过滤，避免模型误把看板过滤空。
def board_status_filter(value: object) -> str:
    text = str(value or "").strip().upper()
    if text in {"", "ALL", "*", "ANY"}:
        return ""
    return text


# LLM: clip_board_text keeps subagent_board useful without flooding later model prompts.
# 函数用途: 截断看板中的长 goal，完整内容仍保留在 task_dir 或 context bundle 里，需要时再读。
def clip_board_text(value: str, *, limit: int = 260) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "...[truncated]"
