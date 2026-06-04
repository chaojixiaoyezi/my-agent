
from __future__ import annotations

from ...subagents import SubAgentExecutionContext


def runtime_guidance_prompt_block(context: SubAgentExecutionContext) -> str:
    bundle = context.context_bundle if isinstance(context.context_bundle, dict) else {}
    guidance = bundle.get("runtime_guidance")
    if not isinstance(guidance, list) or not guidance:
        return ""
    lines = [
        "## GUIDANCE_DELIVERED\n\n",
        "以下是运行中追加给你的补充提示，只作为下一步参考；不要因为提示本身停止任务。\n",
    ]
    for index, item in enumerate(guidance[:20], start=1):
        if not isinstance(item, dict):
            continue
        message = str(item.get("message") or "").strip()
        if not message:
            continue
        priority = str(item.get("priority") or "normal").strip() or "normal"
        sender = str(item.get("sender") or "").strip()
        guidance_id = str(item.get("guidance_id") or "").strip()
        target_type = str(item.get("target_type") or "").strip()
        target_id = str(item.get("target_id") or "").strip()
        sender_text = f"; sender={sender}" if sender else ""
        lines.append(
            f"{index}. guidance_id={guidance_id}; target={target_type}:{target_id}; priority={priority}{sender_text}: {message}\n"
        )
    return "".join(lines) + "\n"
