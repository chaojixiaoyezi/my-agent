"""飞书执行进度卡片纯渲染；不发送消息、不修改任务状态。"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from ..delivery.execution_view import bounded_text, step_display_text

# 比渠道30KB限制留余量，按ASCII转义后的保守JSON字节核对。
MAX_CARD_JSON_BYTES = 28_000
ELEMENT_ID_REGEX = r"^[a-zA-Z][a-zA-Z0-9_]{0,19}$"

# 仅作展示翻译；未知工具沿安全原名显示，不参与工具选择或执行权限。
_TOOL_LABELS = {
    "run_command": "运行命令",
    "read_file": "读取文件",
    "write_file": "写入文件",
    "edit_file": "编辑文件",
    "list_files": "查看目录",
    "search_files": "搜索文件",
    "scripts": "执行脚本",
    "workflows": "执行流程",
    "web_search": "搜索网页",
    "fetch_url": "读取网页",
    "create_subagents": "创建子代理",
}
_PHASE_LABELS = {
    "started": "开始",
    "updated": "执行中",
    "completed": "已完成",
    "succeeded": "已完成",
    "failed": "失败",
    "cancelled": "取消",
    "interrupted": "中断",
    "blocked": "受阻",
    "denied": "被拒绝",
    "finished": "已结束",
    "skipped": "已跳过",
    "approval_required": "等待审批",
    "unknown": "结果未确认",
}


class ProgressCardOverflow(ValueError):
    """完整结论已经超预算；须在网络副作用前改走普通最终回复。"""


def safe_card_element_id(prefix: str, raw_key: str = "") -> str:
    """生成符合飞书 Card JSON 2.0 规范的 element_id（^[a-zA-Z][a-zA-Z0-9_]{0,19}$）。"""
    clean = "".join(c for c in str(prefix) if c.isascii() and (c.isalnum() or c == "_"))
    if not clean or not clean[0].isalpha():
        clean = "e" + clean
    if not raw_key:
        return clean[:20]
    digest = hashlib.sha1(str(raw_key).encode("utf-8", "replace")).hexdigest()[:12]
    return f"{clean[:6]}_{digest}"


def render_progress_card(snapshot: Mapping) -> dict:
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
    footer["element_id"] = "status_footer"

    elements = [_step_element(step, idx) for idx, step in enumerate(steps)]
    done_count = sum(1 for s in steps if s.get("phase") in ("finished", "completed", "succeeded") or s.get("ok") is True)
    if observed == 0:
        panel_title = "工具执行步骤（暂无工具调用）"
    elif done_count > 0:
        panel_title = f"工具执行步骤（已完成 {done_count}/{len(elements)}）"
    else:
        panel_title = f"工具执行步骤（已观察 {observed} 步）"

    panel = {
        "tag": "collapsible_panel",
        "element_id": "steps_panel",
        "expanded": bool(snapshot.get("steps_expanded", False)),
        "header": {"title": {"tag": "plain_text", "content": panel_title}},
        "elements": elements,
    }

    panel_view = snapshot.get("panel_view")
    has_panel = isinstance(panel_view, Mapping)

    header_title = "执行进度"
    task_hdr = None
    detail_elements = []
    task_sel_elem = None
    agent_sel_elem = None
    node_summary_elem = None
    res_panel_elem = None
    orig_res_content = ""
    actions_elem = None
    stop_elements = []
    guide_elem = None

    if has_panel:
        task_title = str(panel_view.get("task_title") or "").strip()
        task_status_label = str(panel_view.get("task_status_label") or "").strip()
        if task_title:
            header_title = f"任务：{task_title} · {task_status_label}" if task_status_label else f"任务：{task_title}"
        else:
            header_title = "任务与子代理控制面板"

        coordinator_label = str(panel_view.get("coordinator_label") or "").strip()
        branch_counts_text = str(panel_view.get("branch_counts_text") or "").strip()
        expired_notice = str(panel_view.get("expired_notice") or "").strip()

        hdr_lines = []
        if task_status_label:
            hdr_lines.append(f"状态：{task_status_label}")
        if coordinator_label:
            hdr_lines.append(f"后台主 Agent：{coordinator_label}")
        if branch_counts_text:
            hdr_lines.append(branch_counts_text)
        if expired_notice:
            hdr_lines.append(expired_notice)
        task_hdr = {
            "tag": "div",
            "element_id": "task_hdr",
            "text": {"tag": "plain_text", "content": "\n".join(hdr_lines) or "任务总览"},
        }

        if isinstance(panel_view.get("detail_link_markdown"), str) and panel_view["detail_link_markdown"].strip():
            detail_elements.append({
                "tag": "markdown",
                "element_id": "detail_lnk",
                "content": panel_view["detail_link_markdown"].strip(),
            })

        expired = panel_view.get("expired") is True
        audience = str(panel_view.get("audience", "dm"))

        if not expired:
            task_sel = panel_view.get("task_selector")
            if isinstance(task_sel, Mapping):
                t_options_raw = task_sel.get("options") or ()
                t_options = []
                for opt in t_options_raw:
                    if isinstance(opt, Mapping):
                        t_content = str(opt.get("text") or opt.get("label") or opt.get("value", ""))
                        t_val = str(opt.get("value", ""))
                        t_options.append({"text": {"tag": "plain_text", "content": t_content}, "value": t_val})
                    if len(t_options) == 20:
                        break
                cb_val = task_sel.get("callback_value") or task_sel.get("value") or {}
                task_sel_elem = {
                    "tag": "select_static",
                    "element_id": "task_sel",
                    "placeholder": {"tag": "plain_text", "content": str(task_sel.get("placeholder") or "选择任务")},
                    "options": t_options,
                    "behaviors": [{"type": "callback", "value": dict(cb_val)}],
                }
                sel_val = task_sel.get("selected_value")
                if sel_val is not None and any(opt["value"] == str(sel_val) for opt in t_options):
                    task_sel_elem["initial_option"] = str(sel_val)

            agent_sel = panel_view.get("agent_selector")
            if isinstance(agent_sel, Mapping):
                a_options_raw = agent_sel.get("options") or ()
                a_options = []
                for opt in a_options_raw:
                    if isinstance(opt, Mapping):
                        a_content = str(opt.get("text") or opt.get("label") or opt.get("value", ""))
                        a_val = str(opt.get("value", ""))
                        a_options.append({"text": {"tag": "plain_text", "content": a_content}, "value": a_val})
                    if len(a_options) == 20:
                        break
                cb_val = agent_sel.get("callback_value") or agent_sel.get("value") or {}
                agent_sel_elem = {
                    "tag": "select_static",
                    "element_id": "panel_sel",
                    "placeholder": {"tag": "plain_text", "content": str(agent_sel.get("placeholder") or "选择代理")},
                    "options": a_options,
                    "behaviors": [{"type": "callback", "value": dict(cb_val)}],
                }
                sel_val = agent_sel.get("selected_value")
                if sel_val is not None and any(opt["value"] == str(sel_val) for opt in a_options):
                    agent_sel_elem["initial_option"] = str(sel_val)

        sel_target = panel_view.get("selected_target")
        if isinstance(sel_target, Mapping):
            t_kind = sel_target.get("target_kind", "")
            badge = (
                "后台主 Agent"
                if t_kind == "background_main"
                else "子代理"
                if t_kind == "subagent"
                else "任务总览"
                if t_kind == "overview"
                else (sel_target.get("role_badge") or "")
            )
            t_lines = []
            t_title = sel_target.get("title", "")
            if badge and t_title:
                t_lines.append(f"【{badge}】{t_title}")
            elif badge or t_title:
                t_lines.append(badge or t_title)
            if sel_target.get("parent_label"):
                t_lines.append(f"父级：{sel_target['parent_label']}")
            if sel_target.get("status_label"):
                t_lines.append(f"状态：{sel_target['status_label']}")
            if sel_target.get("current_action"):
                t_lines.append(f"当前动作：{sel_target['current_action']}")
            if sel_target.get("latest_progress"):
                t_lines.append(f"最新进展：{sel_target['latest_progress']}")
            if sel_target.get("control_status"):
                t_lines.append(f"控制状态：{sel_target['control_status']}")
            node_summary_elem = {
                "tag": "div",
                "element_id": "node_summary",
                "text": {"tag": "plain_text", "content": "\n".join(t_lines) or "节点状态"},
            }

            res_summary = str(sel_target.get("result_summary") or "").strip()
            art_summary = str(sel_target.get("artifacts_summary") or "").strip()
            if audience != "group_public" and (res_summary or art_summary):
                res_parts = []
                if res_summary:
                    res_parts.append(f"结果摘要：\n{res_summary}")
                if art_summary:
                    res_parts.append(f"产物列表：\n{art_summary}")
                orig_res_content = "\n\n".join(res_parts)
                res_panel_elem = {
                    "tag": "collapsible_panel",
                    "element_id": "res_panel",
                    "expanded": False,
                    "header": {"title": {"tag": "plain_text", "content": "结果与产物"}},
                    "elements": [_plain(orig_res_content)],
                }

        if not expired:
            raw_actions = panel_view.get("actions", ())
            if isinstance(raw_actions, (list, tuple)):
                action_columns = []
                seen_btn_eids = set()
                for i, act in enumerate(raw_actions[:6]):
                    if not isinstance(act, Mapping):
                        continue
                    btn_type = act.get("type", "default")
                    if btn_type not in {"default", "primary", "danger"}:
                        btn_type = "default"
                    btn_text = str(act.get("text") or act.get("label") or "操作")
                    btn_val = dict(act.get("value") or {})
                    btn_eid = safe_card_element_id("act", str(act.get("id") or i))
                    if btn_eid in seen_btn_eids:
                        btn_eid = safe_card_element_id("act", f"{i}:{act.get('id') or ''}")
                    seen_btn_eids.add(btn_eid)
                    action_columns.append({
                        "tag": "column",
                        "width": "auto",
                        "elements": [{
                            "tag": "button",
                            "element_id": btn_eid,
                            "type": btn_type,
                            "text": {"tag": "plain_text", "content": btn_text},
                            "behaviors": [{"type": "callback", "value": btn_val}],
                        }],
                    })
                if action_columns:
                    actions_elem = {
                        "tag": "column_set",
                        "element_id": "panel_act",
                        "flex_mode": "none",
                        "columns": action_columns,
                    }

            cfm = panel_view.get("confirm_stop")
            if audience != "group_public" and isinstance(cfm, Mapping) and cfm.get("visible") is True:
                prompt_text = str(cfm.get("prompt_text") or cfm.get("prompt") or "确认停止该代理或任务吗？")
                cfm_val = dict(cfm.get("confirm_value") or cfm.get("value") or {})
                cancel_val = dict(cfm.get("cancel_value") or {})
                stop_prompt = _plain(prompt_text)
                stop_prompt["element_id"] = "stop_cfm"
                stop_buttons = {
                    "tag": "column_set",
                    "element_id": "stop_act",
                    "flex_mode": "none",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "auto",
                            "elements": [{
                                "tag": "button",
                                "element_id": "stop_btn_ok",
                                "type": "danger",
                                "text": {"tag": "plain_text", "content": "确认停止"},
                                "behaviors": [{"type": "callback", "value": cfm_val}],
                            }],
                        },
                        {
                            "tag": "column",
                            "width": "auto",
                            "elements": [{
                                "tag": "button",
                                "element_id": "stop_btn_cancel",
                                "type": "default",
                                "text": {"tag": "plain_text", "content": "取消"},
                                "behaviors": [{"type": "callback", "value": cancel_val}],
                            }],
                        },
                    ],
                }
                stop_elements = [stop_prompt, stop_buttons]

            gf = panel_view.get("guidance_form")
            if audience != "group_public" and isinstance(gf, Mapping) and gf.get("visible") is True:
                g_placeholder = str(gf.get("placeholder") or "输入指导意见...")
                g_label = str(gf.get("submit_label") or gf.get("label") or "发送指导")
                g_val = dict(gf.get("value") or gf.get("submit_value") or {})
                guide_elem = {
                    "tag": "form",
                    "name": "guide_form",
                    "element_id": "guide_frm",
                    "elements": [
                        {
                            "tag": "input",
                            "name": "guidance_text",
                            "element_id": "guide_inp",
                            "required": True,
                            "placeholder": {"tag": "plain_text", "content": g_placeholder},
                        },
                        {
                            "tag": "button",
                            "name": "_guide_submit",
                            "element_id": "guide_sub",
                            "action_type": "form_submit",
                            "type": "primary",
                            "text": {"tag": "plain_text", "content": g_label},
                            "value": g_val,
                        },
                    ],
                }

    body = []
    card = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": header_title}},
        "body": {"elements": body},
    }

    res_bound_limit = len(orig_res_content.encode("utf-8")) if res_panel_elem is not None else 0

    while True:
        facts = {
            "observed_steps": observed,
            "displayed_steps": len(elements),
            "omitted_steps": observed - len(elements),
            "events_truncated": truncation.get("events_truncated") is True or snapshot.get("truncated") is True,
        }
        messages = [f"另有 {facts['omitted_steps']} 条更早步骤未展示。"] if facts["omitted_steps"] else []
        if facts["events_truncated"]:
            messages.append("部分过程记录不完整。")
        notice["text"]["content"] = "\n".join(messages)

        if has_panel:
            body[:] = [
                task_hdr,
                *detail_elements,
                *([task_sel_elem] if task_sel_elem is not None else []),
                *([agent_sel_elem] if agent_sel_elem is not None else []),
                *([node_summary_elem] if node_summary_elem is not None else []),
                *([res_panel_elem] if res_panel_elem is not None else []),
                *([actions_elem] if actions_elem is not None else []),
                *stop_elements,
                *([guide_elem] if guide_elem is not None else []),
                panel,
                *([notice] if messages else []),
                footer,
            ]
        else:
            body[:] = [
                summary,
                panel,
                *([notice] if messages else []),
                footer,
            ]

        if len(json.dumps(card, ensure_ascii=True, allow_nan=False).encode("utf-8")) <= MAX_CARD_JSON_BYTES:
            return card

        # Eviction Priority 1: Pop oldest step
        if elements:
            elements.pop(0)
            done_count = sum(1 for s in steps[len(steps) - len(elements):] if s.get("phase") in ("finished", "completed", "succeeded") or s.get("ok") is True)
            if observed == 0:
                panel["header"]["title"]["content"] = "工具执行步骤（暂无工具调用）"
            elif done_count > 0:
                panel["header"]["title"]["content"] = f"工具执行步骤（已完成 {done_count}/{len(elements)}）"
            else:
                panel["header"]["title"]["content"] = f"工具执行步骤（已观察 {observed} 步）"
            continue

        # Eviction Priority 2: Progressively bound result_summary / artifacts_summary down to 512 bytes
        if res_panel_elem is not None and res_bound_limit > 512:
            res_bound_limit = max(512, res_bound_limit // 2)
            bounded = bounded_text(orig_res_content, max_bytes=res_bound_limit) + "…"
            res_panel_elem["elements"] = [_plain(bounded)]
            continue

        # Eviction Priority 3: Exceeds budget even with steps evicted and results bounded
        raise ProgressCardOverflow("complete conclusion exceeds card byte budget")


def _plain(text: str) -> dict:
    return {"tag": "div", "text": {"tag": "plain_text", "content": text}}


def _step_element(step: Mapping, idx: int = 0) -> dict:
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
    excerpt = step_display_text(step.get("result_excerpt"), max_chars=60)
    if excerpt:
        details.append("结果摘要：" + excerpt)
    batch_index, batch_total = step.get("batch_index"), step.get("batch_total")
    if type(batch_index) is int and type(batch_total) is int and 0 <= batch_index <= batch_total and batch_total > 0:
        details.append(f"批次：{batch_index}/{batch_total}")
    reference = bounded_text(step_display_text(step.get("detail_ref"), max_chars=160), max_bytes=160)
    if reference:
        details.append("详情引用：" + reference)
    step_id = bounded_text(step_display_text(step.get("step_id"), max_chars=120), max_bytes=120)
    if step_id:
        details.append("记录编号：" + step_id)
    raw_step_id = str(step.get("step_id", ""))
    eid = safe_card_element_id("step", f"{idx}:{raw_step_id}")
    return {
        "tag": "collapsible_panel",
        "element_id": eid,
        "expanded": False,
        "header": {"title": {"tag": "plain_text", "content": heading}},
        "elements": [_plain("\n".join(details))],
    }


def _count(value: object, *, default: int) -> int:
    return value if type(value) is int and value >= 0 else default


__all__ = [
    "MAX_CARD_JSON_BYTES",
    "ELEMENT_ID_REGEX",
    "ProgressCardOverflow",
    "safe_card_element_id",
    "render_progress_card",
]
