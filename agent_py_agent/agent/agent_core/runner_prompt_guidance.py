# LLM: Runner guidance prompt rendering stays separate from the main runner prompt template.
# 模块用途: 将 send_guidance 投递给子代理的补充提示渲染成软提示块。

from __future__ import annotations

from ..subagent import SubAgentExecutionContext


# LLM: runtime_guidance_prompt_block renders delivered guidance as soft steering, not acceptance criteria.
# 函数用途: 子代理启动时展示 send_guidance/旧消息入口写入的补充提示。
def runtime_guidance_prompt_block(context: SubAgentExecutionContext) -> str:
    bundle = context.context_bundle if isinstance(context.context_bundle, dict) else {}
    reserved = bundle.get("reserved")
    guidance = reserved.get("runtime_guidance") if isinstance(reserved, dict) else None
    if not isinstance(guidance, list) or not guidance:
        return ""
    lines = [
        "## Runtime Guidance\n\n",
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
        sender_text = f"; sender={sender}" if sender else ""
        lines.append(f"{index}. priority={priority}{sender_text}: {message}\n")
    return "".join(lines) + "\n"
