"""单元测试：飞书执行进度卡片与交互式面板 Card JSON 2.0 纯渲染与投递契约。"""
from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter.delivery import (
    GatewayReplyDeliveryStore,
    GatewayReplyDeliveryWorker,
    PendingGatewayReply,
)
from agent_py_agent.agent.adapter.feishu_progress_card import (
    ELEMENT_ID_REGEX,
    MAX_CARD_JSON_BYTES,
    ProgressCardOverflow,
    render_progress_card,
    safe_card_element_id,
)
from agent_py_agent.agent.adapter.manager import ChannelManager
from agent_py_agent.agent.adapter.progress_draft import (
    GatewayProgressBatch,
    send_gateway_draft,
)
from agent_py_agent.agent.delivery import (
    ChannelAdapterRegistry,
    ChannelCapabilities,
    DeliveryService,
)


def _collect_element_ids(node: object) -> list[str]:
    """递归收集整个 JSON 树中的所有 element_id。"""
    found: list[str] = []
    if isinstance(node, Mapping):
        if "element_id" in node and isinstance(node["element_id"], str):
            found.append(node["element_id"])
        for value in node.values():
            found.extend(_collect_element_ids(value))
    elif isinstance(node, (list, tuple)):
        for item in node:
            found.extend(_collect_element_ids(item))
    return found


def _assert_zero_action_tags(node: object) -> None:
    """验证 Card JSON 2.0 树中严禁出现 tag: action。"""
    if isinstance(node, Mapping):
        assert node.get("tag") != "action", f"Found prohibited tag='action' in {node}"
        for value in node.values():
            _assert_zero_action_tags(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _assert_zero_action_tags(item)


class SyntheticAdapter:
    supports_edit_progress = True
    supports_progress_cards = True
    provider_idempotent_delivery = True
    provider_idempotency_window_seconds = 3600

    def __init__(self) -> None:
        self.created: list[tuple[str, str, dict]] = []
        self.edits: list[tuple[str, str, dict]] = []
        self.finals: list[tuple[str, str]] = []
        self.edit_ok = True
        self.create_ok = True

    def start_progress_message(self, target: str, text: str, *, reply_to: str = "", idempotency_key: str = "", **options) -> str:
        if not self.create_ok:
            return ""
        self.created.append((target, text, options))
        return "om_SYNTHETIC_CARD_01"

    def edit_progress_message(self, message_id: str, text: str, **options) -> bool:
        if not self.edit_ok:
            return False
        self.edits.append((message_id, text, options))
        return True

    def send_message(self, target: str, message: object) -> bool:
        self.finals.append((target, getattr(message, "content", "")))
        return True


def test_safe_card_element_id_contract_and_regex_invariants() -> None:
    """验证 safe_card_element_id 对各种异常输入的格式约束与字符集契约。"""
    pattern = re.compile(ELEMENT_ID_REGEX)

    cases = [
        ("", ""),
        ("123", ""),
        ("_step", ""),
        ("___", ""),
        ("中文前缀", ""),
        ("混合_123_abc", ""),
        ("step", "0:call-001"),
        ("step", "中文:step/01?foo=bar#baz" * 10),
        ("act", "task.select"),
        ("act", "!@#$%^&*()_+-=[]{}|;':,.<>/?~"),
        ("a" * 100, "b" * 100),
        ("prefix_with_numbers_12345", ""),
        ("btn", ""),
    ]

    for prefix, raw_key in cases:
        eid = safe_card_element_id(prefix, raw_key)
        assert pattern.fullmatch(eid) is not None, f"Failed regex match: {eid!r} for prefix={prefix!r}, raw_key={raw_key!r}"
        assert len(eid) <= 20, f"Exceeded 20 chars: len={len(eid)} ({eid!r})"
        assert eid[0].isalpha() and eid[0].isascii(), f"Must start with ascii letter: {eid!r}"


def test_element_id_regex_and_uniqueness_across_pathological_inputs() -> None:
    """向卡片喂入极端超长 Unicode、特殊标点与重复 ID，断言整树 element_id 均合法且无重复。"""
    steps = [
        {
            "step_id": f"中文:step/01?foo=bar#baz_{i}" * 10,
            "tool_name": f"工具_{i}",
            "phase": "finished",
            "ok": True,
            "handler_executed": True,
            "duration_ms": 100 + i,
            "label": "测试步骤标签",
        }
        for i in range(10)
    ]
    # 追加空字符串、纯数字、纯符号等极端 step_id
    steps.append({"step_id": "", "tool_name": "tool_empty", "phase": "updated"})
    steps.append({"step_id": "1234567890", "tool_name": "tool_digits", "phase": "started"})
    steps.append({"step_id": "!@#$%^&*()_+", "tool_name": "tool_symbols", "phase": "failed", "ok": False})

    panel_view = {
        "task_title": "测试综合任务",
        "task_status_label": "进行中",
        "coordinator_label": "主协调代理",
        "branch_counts_text": "子代理：2 个运行 / 1 个完成",
        "detail_link_markdown": "[查看详细进度](https://example.com/tasks/demo)",
        "audience": "dm",
        "task_selector": {
            "placeholder": "切换任务",
            "options": [{"text": f"任务-{i}", "value": f"task_{i}"} for i in range(25)],  # 超过20项
            "selected_value": "task_2",
            "callback_value": {"action": "switch_task"},
        },
        "agent_selector": {
            "placeholder": "切换代理",
            "options": [{"text": f"代理-{i}", "value": f"agent_{i}"} for i in range(5)],
            "selected_value": "agent_0",
            "callback_value": {"action": "switch_agent"},
        },
        "selected_target": {
            "target_kind": "subagent",
            "title": "深度搜索分片",
            "parent_label": "根任务",
            "status_label": "运行中",
            "current_action": "正在执行第 3 轮检索",
            "latest_progress": "已完成数据清洗",
            "control_status": "补充指令已受理，等待消费",
            "result_summary": "已获取 15 条高相关结果。",
            "artifacts_summary": "output/report.json (4.2KB)",
        },
        "actions": [
            {"id": "act_refresh", "text": "刷新状态", "type": "default", "value": {"cmd": "refresh"}},
            {"id": "act_stop_branch", "text": "终止本路", "type": "danger", "value": {"cmd": "stop"}},
            {"id": "123_duplicate", "text": "重复键A", "type": "primary", "value": {"cmd": "a"}},
            {"id": "123_duplicate", "text": "重复键B", "type": "default", "value": {"cmd": "b"}},
            {"id": "中文操作", "text": "操作C", "type": "default", "value": {"cmd": "c"}},
            {"id": "", "text": "空操作D", "type": "default", "value": {"cmd": "d"}},
        ],
        "confirm_stop": {
            "visible": True,
            "prompt_text": "确认终止当前子代理分支吗？",
            "confirm_value": {"cmd": "confirm_stop"},
            "cancel_value": {"cmd": "cancel_stop"},
        },
        "guidance_form": {
            "visible": True,
            "placeholder": "输入补充要求...",
            "submit_label": "发送干预",
            "value": {"cmd": "submit_guidance"},
        },
    }

    snapshot = {"steps": steps, "panel_view": panel_view}
    card = render_progress_card(snapshot)

    eids = _collect_element_ids(card)
    assert len(eids) >= 10, "Card should contain multiple structural element_ids"

    pattern = re.compile(ELEMENT_ID_REGEX)
    for eid in eids:
        assert pattern.fullmatch(eid) is not None, f"Card element_id failed regex check: {eid!r}"
        assert len(eid) <= 20, f"Card element_id exceeds 20 chars: {eid!r}"

    # 验证整卡内 zero duplicates
    duplicates = [eid for eid in eids if eids.count(eid) > 1]
    assert not duplicates, f"Found duplicate element_ids in card tree: {duplicates}"

    # 验证严禁 tag: action
    _assert_zero_action_tags(card)


def test_steps_panel_collapsed_by_default_and_expanded_flag() -> None:
    """验证执行步骤面板默认收拢，仅在显式 steps_expanded=True 时展开。"""
    steps = [
        {"step_id": "c1", "tool_name": "read_file", "phase": "finished", "ok": True},
        {"step_id": "c2", "tool_name": "write_file", "phase": "finished", "ok": True},
    ]

    card_default = render_progress_card({"steps": steps})
    panel_default = next(e for e in card_default["body"]["elements"] if e.get("element_id") == "steps_panel")
    assert panel_default["expanded"] is False
    assert "工具执行步骤（已完成 2/2）" in panel_default["header"]["title"]["content"]

    card_explicit_true = render_progress_card({"steps": steps, "steps_expanded": True})
    panel_explicit = next(e for e in card_explicit_true["body"]["elements"] if e.get("element_id") == "steps_panel")
    assert panel_explicit["expanded"] is True

    # 0 步骤时的标题
    card_empty = render_progress_card({"steps": []})
    panel_empty = next(e for e in card_empty["body"]["elements"] if e.get("element_id") == "steps_panel")
    assert "工具执行步骤（暂无工具调用）" in panel_empty["header"]["title"]["content"]

    # 仅更新中无完成步骤时的标题
    card_started = render_progress_card({"steps": [{"step_id": "c0", "tool_name": "run_command", "phase": "started"}]})
    panel_started = next(e for e in card_started["body"]["elements"] if e.get("element_id") == "steps_panel")
    assert "工具执行步骤（已观察 1 步）" in panel_started["header"]["title"]["content"]


def test_final_reply_is_never_embedded_in_progress_card() -> None:
    """验证卡片体严禁渲染 final_text，无论 snapshot 是否携带。"""
    snapshot = {
        "steps": [{"step_id": "s1", "tool_name": "web_search", "phase": "finished", "ok": True}],
        "final_text": "CONFIDENTIAL_FINAL_BUSINESS_RESULT_TEXT",
    }
    card = render_progress_card(snapshot)
    card_json = json.dumps(card, ensure_ascii=False)
    assert "CONFIDENTIAL_FINAL_BUSINESS_RESULT_TEXT" not in card_json


def test_panel_view_group_public_privacy_stripping() -> None:
    """验证 group_public 受众下绝对不渲染 guidance_form、confirm_stop 与私密 result_summary。"""
    panel_view = {
        "task_title": "群聊公共任务",
        "task_status_label": "进行中",
        "audience": "group_public",
        "selected_target": {
            "target_kind": "subagent",
            "title": "公共分片",
            "result_summary": "SENSITIVE_PRIVATE_RESULT_NOT_FOR_PUBLIC",
            "artifacts_summary": "SENSITIVE_ARTIFACT_NOT_FOR_PUBLIC",
        },
        "confirm_stop": {
            "visible": True,
            "prompt_text": "确认终止此公共任务？",
            "confirm_value": {"action": "stop"},
        },
        "guidance_form": {
            "visible": True,
            "placeholder": "群内不可见干预框",
            "submit_label": "干预",
            "value": {"action": "guide"},
        },
    }

    card = render_progress_card({"steps": [], "panel_view": panel_view})
    card_json = json.dumps(card, ensure_ascii=False)

    assert "SENSITIVE_PRIVATE_RESULT_NOT_FOR_PUBLIC" not in card_json
    assert "SENSITIVE_ARTIFACT_NOT_FOR_PUBLIC" not in card_json
    assert "res_panel" not in _collect_element_ids(card)
    assert "stop_cfm" not in _collect_element_ids(card)
    assert "stop_act" not in _collect_element_ids(card)
    assert "guide_frm" not in _collect_element_ids(card)

    panel = next(e for e in card["body"]["elements"] if e.get("element_id") == "steps_panel")
    assert panel["expanded"] is False


def test_panel_view_expired_omits_interactive_controls() -> None:
    """验证 expired=True 时仅展示只读总览与过期提示，剔除下拉选择器、操作按钮与表单。"""
    panel_view = {
        "task_title": "已过期任务",
        "task_status_label": "已结束",
        "expired": True,
        "expired_notice": "控制面板已过期，请在飞书会话中重新拉起。",
        "task_selector": {"placeholder": "选任务", "options": [{"text": "T1", "value": "1"}]},
        "agent_selector": {"placeholder": "选代理", "options": [{"text": "A1", "value": "1"}]},
        "actions": [{"id": "act1", "text": "按钮", "value": {}}],
        "confirm_stop": {"visible": True, "confirm_value": {}},
        "guidance_form": {"visible": True, "value": {}},
        "selected_target": {"target_kind": "background_main", "title": "主进程"},
    }

    card = render_progress_card({"steps": [], "panel_view": panel_view})
    eids = _collect_element_ids(card)

    assert "task_hdr" in eids
    assert "node_summary" in eids
    assert "task_sel" not in eids
    assert "panel_sel" not in eids
    assert "panel_act" not in eids
    assert "stop_cfm" not in eids
    assert "guide_frm" not in eids

    task_hdr = next(e for e in card["body"]["elements"] if e.get("element_id") == "task_hdr")
    assert "控制面板已过期" in task_hdr["text"]["content"]


def test_panel_view_dm_detail_and_markdown_link() -> None:
    """验证 dm_detail 下正确渲染宿主提供的 detail_link_markdown 与完整控制组件。"""
    link_content = "[在浏览器中查看完整任务运行台账](https://host.local/tasks/t-01)"
    panel_view = {
        "task_title": "详情面板",
        "task_status_label": "运行中",
        "audience": "dm_detail",
        "detail_link_markdown": link_content,
        "selected_target": {
            "target_kind": "overview",
            "title": "全局看板",
            "result_summary": "执行进展正常",
        },
    }

    card = render_progress_card({"steps": [], "panel_view": panel_view})
    detail_elem = next((e for e in card["body"]["elements"] if e.get("element_id") == "detail_lnk"), None)
    assert detail_elem is not None
    assert detail_elem["tag"] == "markdown"
    assert detail_elem["content"] == link_content

    res_panel = next((e for e in card["body"]["elements"] if e.get("element_id") == "res_panel"), None)
    assert res_panel is not None
    assert res_panel["expanded"] is False


def test_28kb_eviction_order_steps_first_then_results_bounded() -> None:
    """验证 28KB 预算淘汰优先级：先弹出最旧步骤，再渐进压缩结果/产物摘要，严禁吞没结构化控制。"""
    huge_label = "超长步骤标签_测试数据_" * 50
    steps = [
        {"step_id": f"s_{i}", "tool_name": "read_file", "phase": "finished", "label": f"{i}_{huge_label}"}
        for i in range(60)
    ]

    panel_view = {
        "task_title": "大体积压测任务",
        "task_status_label": "处理中",
        "audience": "dm",
        "agent_selector": {
            "placeholder": "选择节点",
            "options": [{"text": f"Agent-{i}", "value": f"a_{i}"} for i in range(10)],
            "callback_value": {"kind": "my_agent_panel_action"},
        },
        "actions": [
            {"id": "refresh", "text": "刷新", "value": {"k": "v"}},
            {"id": "stop", "text": "终止", "type": "danger", "value": {"k": "v"}},
        ],
        "selected_target": {
            "target_kind": "subagent",
            "title": "节点",
            "result_summary": "多段落结果摘要\n" * 50,
            "artifacts_summary": "大产物清单\n" * 50,
        },
    }

    card = render_progress_card({"steps": steps, "panel_view": panel_view})
    card_bytes = len(json.dumps(card, ensure_ascii=True).encode("utf-8"))
    assert card_bytes <= MAX_CARD_JSON_BYTES

    eids = _collect_element_ids(card)
    assert "task_hdr" in eids
    assert "panel_sel" in eids
    assert "panel_act" in eids

    # 步骤数量被淘汰到 50 以下以满足预算
    panel = next(e for e in card["body"]["elements"] if e.get("element_id") == "steps_panel")
    assert len(panel["elements"]) < 50

    # 步骤全部为空，但 result_summary 极端庞大时，渐进压缩至 512 字节以内
    huge_summary = "极长结论文本段落_" * 3000  # 约 72KB
    card_huge_summary = render_progress_card({
        "steps": [],
        "panel_view": {
            "task_title": "大文本任务",
            "audience": "dm",
            "selected_target": {"target_kind": "subagent", "result_summary": huge_summary},
        },
    })
    assert len(json.dumps(card_huge_summary, ensure_ascii=True).encode("utf-8")) <= MAX_CARD_JSON_BYTES
    res_panel = next(e for e in card_huge_summary["body"]["elements"] if e.get("element_id") == "res_panel")
    content = res_panel["elements"][0]["text"]["content"]
    assert content.endswith("…")


def test_overflow_raised_when_structure_itself_exceeds_budget() -> None:
    """当不可淘汰的基础框架（无步骤、无结果）本身超出 28KB 时，抛出 ProgressCardOverflow。"""
    # 构造极长标题与无法通过淘汰步骤或结果解决的超预算结构
    huge_title = "不可压缩结构超长标题_" * 2000
    with pytest.raises(ProgressCardOverflow):
        render_progress_card({
            "steps": [],
            "panel_view": {
                "task_title": huge_title,
                "audience": "dm",
                "selected_target": {"target_kind": "overview"},
            },
        })


def test_standalone_final_reply_retry_idempotency(tmp_path, monkeypatch) -> None:
    """模拟最终独立回复投递遭遇瞬态故障与二次重试，验证进度卡片不重复创建且最终答复精准送达。"""
    clock = [1000.0]
    monkeypatch.setattr("agent_py_agent.agent.adapter.delivery.time.time", lambda: clock[0])
    monkeypatch.setattr("agent_py_agent.agent.adapter.progress_draft.time.time", lambda: clock[0])

    adapter = SyntheticAdapter()
    registry = ChannelAdapterRegistry()
    registry.register_adapter("feishu", adapter, capabilities=ChannelCapabilities(edit_progress=True))
    service = DeliveryService(registry)
    store = GatewayReplyDeliveryStore(tmp_path)

    normal_deliveries: list[tuple] = []
    attempt_counter = [0]

    def flaky_send_gateway_reply(*args) -> bool:
        attempt_counter[0] += 1
        if attempt_counter[0] == 1:
            return False  # 首次模拟网络瞬态失败
        normal_deliveries.append(args)
        return True

    manager = SimpleNamespace(
        _delivery_service=service,
        _adapters={"feishu": adapter},
        _send_gateway_reply=flaky_send_gateway_reply,
        _update_active_channel=lambda *args: None,
    )

    batch = [GatewayProgressBatch(("执行开始",), 1, "stream", True, steps=(), render_kind="card", source_revision=1)]
    final_holder = [None]

    def deliver_response(row, text):
        return ChannelManager._deliver_gateway_reply(manager, row, text)

    deliver_response.delivery_adapter = adapter

    worker = GatewayReplyDeliveryWorker(
        store,
        poll_response=lambda _: final_holder[0],
        deliver_response=deliver_response,
        poll_progress=lambda row: batch[0],
        deliver_progress=lambda row, text: send_gateway_draft(service, row, text),
    )

    worker.enqueue(PendingGatewayReply(
        request_id="REQ_FLAKY_RETRY",
        channel="feishu",
        user_id="ou_test_user",
        message_id="om_inbound_test",
    ))

    # Turn 1: 首次进度投递，创建进度卡片
    worker.run_once()
    assert len(adapter.created) == 1
    assert adapter.created[0][0] == "ou_test_user"

    # Turn 2: 任务完成，准备投递最终回复；首次 flaky 失败
    clock[0] += 2.0
    final_holder[0] = "这是最终完整的业务结论答复"
    worker.run_once()
    # 卡片已通过 closeout PATCH 更新为 completed，但因 standalone reply 失败，记录仍在 pending 中
    assert len(adapter.edits) == 1
    assert not normal_deliveries
    assert len(store.pending()) == 1

    # Turn 3: 时钟推进后进行二次重试调度
    clock[0] += 5.0
    worker.run_once()
    # 验证不重新创建卡片 (created 仍为 1)
    assert len(adapter.created) == 1
    # 独立最终答复精准送达
    assert len(normal_deliveries) == 1
    assert normal_deliveries[0][2] == "这是最终完整的业务结论答复"
    # 状态成功清空
    assert not store.pending()


def test_fail_open_terminal_patch_failure_does_not_block_final_reply(tmp_path) -> None:
    """验证当进度卡片的终态 PATCH 抛出异常时，fail-open 机制生效，业务最终答复仍正常发出。"""
    adapter = SyntheticAdapter()
    adapter.edit_ok = False  # 模拟飞书 API 编辑卡片返回失败
    registry = ChannelAdapterRegistry()
    registry.register_adapter("feishu", adapter, capabilities=ChannelCapabilities(edit_progress=True))
    service = DeliveryService(registry)

    normal_deliveries: list[tuple] = []
    manager = SimpleNamespace(
        _delivery_service=service,
        _adapters={"feishu": adapter},
        _send_gateway_reply=lambda *args: normal_deliveries.append(args) or True,
        _update_active_channel=lambda *args: None,
    )

    pending = PendingGatewayReply(
        request_id="REQ_PATCH_FAULT",
        channel="feishu",
        user_id="ou_test_user",
        message_id="om_inbound",
        progress_draft={"render_kind": "card", "display_message_id": "om_card_id"},
    )

    ok = ChannelManager._deliver_gateway_reply(manager, pending, "必须送达的最终结论")
    assert ok is True
    assert len(normal_deliveries) == 1
    assert normal_deliveries[0][2] == "必须送达的最终结论"
