"""飞书执行进度卡片纯渲染；不发送消息、不修改任务状态。"""
from __future__ import annotations

import json
from collections.abc import Mapping

from ..delivery.execution_view import bounded_text, step_display_text

# 比渠道30KB限制留余量，按ASCII转义后的保守JSON字节核对。
MAX_CARD_JSON_BYTES = 28_000
# 仅作展示翻译；未知工具沿安全原名显示，不参与工具选择或执行权限。
_TOOL_LABELS = {"run_command": "运行命令", "read_file": "读取文件", "write_file": "写入文件",
                "edit_file": "编辑文件", "list_files": "查看目录", "search_files": "搜索文件",
                "scripts": "执行脚本", "workflows": "执行流程", "web_search": "搜索网页",
                "fetch_url": "读取网页", "create_subagents": "创建子代理"}
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
    total_text = "" if total is None else f"｜共 {total} 步"
    state = _PHASE_LABELS.get(snapshot.get("request_phase"), "状态未知")
    summary = _plain(f"{state}｜已观察 {observed} 步{total_text}")
    notice = _plain("")
    notice["element_id"] = "truncation"
    footer = _plain(f"状态：{state}{total_text}")
    footer['element_id'] = 'status_footer'
    elements = [_step_element(step) for step in steps]
    panel = {"tag": "collapsible_panel", "expanded": True,
             "header": {"title": {"tag": "plain_text", "content": "工具执行步骤"}}, "elements": elements}
    final_text = snapshot.get('final_text')
    final_elements = [_plain(final_text)] if isinstance(final_text, str) and final_text else []
    body = []
    card = {"schema": "2.0", "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": "执行进度"}}, "body": {"elements": body}}
    while True:
        facts = {"observed_steps": observed, "displayed_steps": len(elements),
                 "omitted_steps": observed - len(elements),
                 "events_truncated": truncation.get("events_truncated") is True or snapshot.get('truncated') is True}
        messages = ([f"另有 {facts['omitted_steps']} 条更早步骤未展示。"] if facts['omitted_steps'] else [])
        if facts['events_truncated']:
            messages.append("部分过程记录不完整。")
        notice["text"]["content"] = "\n".join(messages)
        body[:] = [summary, panel, *final_elements,
                   *([notice] if messages else []), footer]
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
# 函数用途: 在可见标题保留安全操作摘要，折叠结果详情但不隐藏失败、未执行和耗时事实。
def _step_element(step: Mapping) -> dict:
    tool = bounded_text(step_display_text(step.get("tool_name"), max_chars=120), max_bytes=120)
    name = _TOOL_LABELS.get(tool, tool or "工具")
    phase = step.get("phase")
    state = _PHASE_LABELS.get(phase, "状态未知") if isinstance(phase, str) else "状态未知"
    if phase == "finished":
        state = "已完成" if step.get("ok") is True else "失败" if step.get("ok") is False else "结果未确认"
    executed = step.get("handler_executed")
    execution = "已执行" if executed is True else "未执行" if executed is False else "执行情况未确认"
    duration = step.get("duration_ms")
    elapsed = f"{duration}ms" if type(duration) is int and duration >= 0 else "耗时未知"
    label = bounded_text(step_display_text(step.get("label"), max_chars=240), max_bytes=240)
    heading = f"{name} · {state} · {elapsed}"
    if executed is False:
        heading += " · 未执行"
    if label:
        heading += "\n" + bounded_text(label, max_bytes=160)
    details = [execution]
    outcome = step.get("ok")
    details.append("结果核验：成功" if outcome is True else "结果核验：失败" if outcome is False else "结果未确认")
    if label:
        details.append("操作：" + label)
    excerpt = step_display_text(step.get('result_excerpt'), max_chars=60)
    if excerpt:
        details.append("结果摘要：" + excerpt)
    batch_index, batch_total = step.get('batch_index'), step.get('batch_total')
    if type(batch_index) is int and type(batch_total) is int and 0 <= batch_index <= batch_total and batch_total > 0:
        details.append(f'批次：{batch_index}/{batch_total}')
    reference = bounded_text(step_display_text(step.get("detail_ref"), max_chars=160), max_bytes=160)
    if reference:
        details.append("详情引用：" + reference)
    step_id = bounded_text(step_display_text(step.get("step_id"), max_chars=120), max_bytes=120)
    if step_id:
        details.append("记录编号：" + step_id)
    return {"tag": "collapsible_panel", "expanded": False,
            "header": {"title": {"tag": "plain_text", "content": heading}},
            "elements": [_plain("\n".join(details))]}


# LLM: 数量只接受真实非负整数，布尔不冒充数量；缺失时只用已观察长度。
# 函数用途: 安全读取截断统计，不猜任务总数。
def _count(value: object, *, default: int) -> int:
    return value if type(value) is int and value >= 0 else default
