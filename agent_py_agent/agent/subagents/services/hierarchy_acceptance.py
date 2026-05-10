# LLM: Derive minimal acceptance checks for scheduled descendants when model output omits them.
# 模块用途: 给子代理/孙代理调度补稳定验收事实，避免 Context Gate 因模型漏字段而误阻断。

from __future__ import annotations

"""Acceptance-check derivation helpers for hierarchy scheduling."""

from typing import Any


# LLM: scheduled_child_acceptance_checks is the public helper for child-run acceptance fallback.
# 函数用途: 优先使用模型显式 acceptance_checks；缺失时按角色和写产物意图派生最小验收项。
def scheduled_child_acceptance_checks(
    spec: Any,
    *,
    role: str,
    goal: str,
    leaf_write_intent: bool,
) -> list[str]:
    """Return explicit or derived acceptance checks for one scheduled child."""

    explicit = _unique_text_items(list(getattr(spec, "acceptance_checks", []) or []))
    if explicit:
        return explicit
    checks = [f"完成当前 child goal，并在证据或报告中引用结果：{_short_acceptance_goal(goal)}"]
    if _is_coordinator_acceptance(role, spec):
        checks.append("如果创建或推进下一层，必须汇总 child run ids、状态、下一步和 evidence/artifact refs。")
    elif leaf_write_intent or "leaf" in role:
        checks.append("目标产物必须存在，且文件名、路径、内容要求与当前 goal 保持一致。")
    else:
        checks.append("最终报告必须说明完成状态、阻塞原因或父级需要继续验收的 refs。")
    return checks


# LLM: _unique_text_items normalizes model-provided list fields without inventing facts.
# 函数用途: 清理空字符串和重复验收项，保持顺序稳定。
def _unique_text_items(items: list[str]) -> list[str]:
    result: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


# LLM: _short_acceptance_goal keeps derived acceptance useful without bloating runner prompts.
# 函数用途: 截断过长 goal，只保留足够让模型知道验收目标的文本。
def _short_acceptance_goal(goal: str) -> str:
    text = " ".join(str(goal or "").split())
    return text if len(text) <= 220 else f"{text[:217]}..."


# LLM: _is_coordinator_acceptance mirrors scheduler role detection without importing the scheduler.
# 函数用途: 判断派生验收项应强调协调汇总，还是强调产物落地。
def _is_coordinator_acceptance(role: str, spec: Any) -> bool:
    return "coordinator" in str(role or "").lower() or _is_coordinator_spec(spec)


# LLM: _is_coordinator_spec reads only stable role/name fields from a child spec bundle.
# 函数用途: 根据 role 和 agent_name 判断 child spec 是否明确是协调节点。
def _is_coordinator_spec(spec: Any) -> bool:
    role_text = f"{getattr(spec, 'role', '')} {getattr(spec, 'agent_name', '')}".lower()
    return "coordinator" in role_text or "lead" in role_text
