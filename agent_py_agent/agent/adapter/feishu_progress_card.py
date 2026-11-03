"""飞书执行进度卡片纯渲染；不发送消息、不修改任务状态。"""
from __future__ import annotations

import json
from collections.abc import Mapping

from ..delivery.execution_view import bounded_text

# 比渠道30KB限制留余量，按ASCII转义后的保守JSON字节核对。
MAX_CARD_JSON_BYTES = 28_000
_PHASE_LABELS = {"started": "开始", "updated": "执行中", "completed": "已完成", "succeeded": "已完成",
                 "failed": "失败", "cancelled": "取消", "interrupted": "中断", "blocked": "受阻", "denied": "被拒绝",
                 "finished": "已结束", "skipped": "已跳过", "approval_required": "等待审批", "unknown": "结果未确认"}


class ProgressCardOverflow(ValueError):
    """完整结论已经超预算；须在网络副作用前改走普通最终回复。"""


# LLM: 纯投影且兼容旧truncated；只丢最旧整步，结论不截断，超限在发送前抛出。
# 函数用途: 渲染最新50步、折叠面板、完整结论和状态栏，并核对保守JSON字节预算。
def render_progress_card(snapshot) -> dict:
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    raw_steps = snapshot.get("steps", ())
    raw_steps = raw_steps if isinstance(raw_steps, (list, tuple)) else ()
    steps = [step for step in raw_steps[-50:] if isinstance(step, Mapping)]
    truncation = snapshot.get("truncation")
    truncation = truncation if isinstance(truncation, Mapping) else {}
    observed = max(_count(truncation.get("observed_steps"), default=len(raw_steps)), len(raw_steps))
    total = snapshot.get("total_steps")
    total = total if type(total) is int and total >= observed else None
    request_id = bounded_text(snapshot.get("request_id"), max_bytes=240)
    total_text = "总数未知" if total is None else f"总数 {total}"
    state = _PHASE_LABELS.get(snapshot.get("request_phase"), "状态未知")
    summary = _plain(f"请求 {request_id}｜{state}｜已观察 {observed} 步，{total_text}")
    notice = _plain("")
    notice["element_id"] = "truncation"
    footer = _plain(f"状态：{state}｜{total_text}")
    footer['element_id'] = 'status_footer'
    elements = [_step_element(step) for step in steps]
    panel = {"tag": "collapsible_panel", "expanded": False,
             "header": {"title": {"tag": "plain_text", "content": "工具执行步骤"}}, "elements": elements}
    body = [summary, panel]
    final_text = snapshot.get('final_text')
    if isinstance(final_text, str) and final_text:
        body.append(_plain(final_text))
    body.extend((notice, footer))
    card = {"schema": "2.0", "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": "执行进度"}}, "body": {"elements": body}}
    while True:
        facts = {"observed_steps": observed, "displayed_steps": len(elements),
                 "omitted_steps": observed - len(elements),
                 "events_truncated": truncation.get("events_truncated") is True or snapshot.get('truncated') is True}
        earlier = f"+{facts['omitted_steps']} 条更早步骤\n" if facts['omitted_steps'] else ''
        notice["text"]["content"] = earlier + "truncation: " + json.dumps(facts, ensure_ascii=False)
        if len(json.dumps(card, ensure_ascii=True, allow_nan=False).encode("utf-8")) <= MAX_CARD_JSON_BYTES:
            return card
        # 每次移除最旧完整元素；完整结论不能为凑预算被悄悄截断。
        if not elements:
            raise ProgressCardOverflow('complete conclusion exceeds card byte budget')
        elements.pop(0)


# LLM: 只允许标准JSON 2.0的plain_text div，不把模型文本解释成动作、Markdown或URL。
# 函数用途: 创建无交互副作用的文本元素。
def _plain(text: str) -> dict:
    return {"tag": "div", "text": {"tag": "plain_text", "content": text}}


# LLM: finished结合显式ok展示结果，未知与审批不当作完成，不解析状态文案。
# 函数用途: 将一个步骤渲染成有界纯文本行，保留失败、未执行和耗时事实。
def _step_element(step: Mapping) -> dict:
    name = bounded_text(step.get("tool_name"), max_bytes=120)
    phase = bounded_text(step.get("phase"), max_bytes=64)
    label = bounded_text(step.get("label"), max_bytes=240)
    step_id = bounded_text(step.get("step_id"), max_bytes=120)
    state = _PHASE_LABELS.get(phase, f"状态未知({phase})")
    if phase == "finished":
        state = "已完成" if step.get("ok") is True else "失败" if step.get("ok") is False else "结果未确认"
    parts = [name, state, f"step={step_id}", label]
    for key in ("ok", "handler_executed"):
        value = step.get(key)
        parts.append(f"{key}={str(value).lower() if type(value) is bool else 'unknown'}")
    duration = step.get("duration_ms")
    parts.append(f"耗时 {duration}ms" if type(duration) is int and duration >= 0 else "耗时未知")
    reference = bounded_text(step.get("detail_ref"), max_bytes=160)
    if reference:
        parts.append(f"detail_ref={reference}")
    return _plain("｜".join(parts))


# LLM: 数量只接受真实非负整数，布尔不冒充数量；缺失时只用已观察长度。
# 函数用途: 安全读取截断统计，不猜任务总数。
def _count(value: object, *, default: int) -> int:
    return value if type(value) is int and value >= 0 else default
