# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""Small markdown section renderers shared by runner rendering."""


# LLM: render_granted_card_lines 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 渲染或汇总grantedcardlines的展示文本，保持命令行、日志和审计输出一致；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def render_granted_card_lines(card: dict[str, object]) -> list[str]:
    lines = [
        f"- [{card.get('kind', 'unknown')}] {card.get('name', 'unknown')} "
        f"risk={card.get('risk_level', 'unknown')} source={card.get('source', 'unknown')}"
    ]
    if card.get("description"):
        lines.append(f"  - description: {card['description']}")
    if card.get("path"):
        lines.append(f"  - path: {card['path']}")
    if card.get("reasons"):
        lines.append(f"  - reasons: {card['reasons']}")
    return lines


# LLM: render_evidence_item_lines 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 渲染或汇总证据条目lines的展示文本，保持命令行、日志和审计输出一致；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def render_evidence_item_lines(item: dict[str, object]) -> list[str]:
    status = "OK" if item.get("ok") else "FAIL"
    lines = [f"- [{status}] {item.get('kind', 'unknown')}: {item.get('summary', '')}"]
    if item.get("command"):
        lines.append(f"  - command: `{item['command']}`")
    if item.get("path"):
        lines.append(f"  - path: {item['path']}")
    if item.get("url"):
        lines.append(f"  - url: {item['url']}")
    return lines
