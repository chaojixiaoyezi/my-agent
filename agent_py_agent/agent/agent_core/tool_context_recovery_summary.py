# LLM: Recovery-summary rendering helpers stay outside the main orchestration reducer for size control.
# 模块用途: 把 recovery strategy 的长对象压成 live prompt 可读的小预览。

from __future__ import annotations


# LLM: strategy_preview keeps packet-first recovery visible after large dispatch outputs are archived.
# 函数用途: 从 recovery_strategies 中提取首要恢复动作和 packet ref，不展开完整 fallback/takeover refs。
def strategy_preview(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    preview: list[dict[str, object]] = []
    for item in value[:3]:
        if not isinstance(item, dict):
            continue
        preview.append({
            "run_id": item.get("run_id", ""),
            "recommended_action": item.get("recommended_action", ""),
            "packet_status": item.get("packet_status", ""),
            "uses_continue_packet": bool(item.get("uses_continue_packet", False)),
            "runner_instruction": _clip(item.get("runner_instruction", ""), limit=220),
        })
    return preview


# LLM: _clip bounds runner_instruction snippets in recovery summaries.
# 函数用途: 防止恢复指令预览把完整任务正文重新带回父级 prompt。
def _clip(value: object, *, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit].rstrip() + f"...[truncated {len(text) - limit} chars]"
