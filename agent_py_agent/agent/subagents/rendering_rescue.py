# LLM: Rescue rendering helpers keep main subagent rendering below code-size thresholds.
# 模块用途: 渲染 rescue packet 的简短 refs-only 摘要，不展开 artifact 正文。
from __future__ import annotations


# LLM: render_action_rescue_packet_lines shows policy and refs only for action-plan markdown.
# 函数用途: 把 rescue packet 的重试、人工确认和恢复入口 refs 渲染成紧凑 Markdown 行。
def render_action_rescue_packet_lines(packet: dict[str, object]) -> list[str]:
    retry = packet.get("retry_policy", {})
    manual = packet.get("manual_confirmation", {})
    retry_limit = retry.get("max_attempts", 0) if isinstance(retry, dict) else 0
    manual_required = manual.get("required", True) if isinstance(manual, dict) else True
    lines = [f"  - rescue_packet: retry_limit={retry_limit} manual_confirmation={manual_required}"]
    refs = packet.get("recovery_entrypoints", [])
    if isinstance(refs, list) and refs:
        lines.append("  - rescue_packet_refs:")
        lines.extend(f"    - `{ref}`" for ref in refs[:5])
    return lines
