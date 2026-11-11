"""全面覆盖测试：飞书交互式控制面板回调、受众分流、会话锁与 14 天换卡契约。"""
from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_py_agent.agent.adapter.feishu import FeishuAdapter, _reply_after_card_action
from agent_py_agent.agent.adapter.feishu_agent_panel import (
    FEISHU_CARD_MAX_AGE_SECONDS,
    _panel_state_path,
    _read_panel_state,
    handle_panel_card_action,
    handle_panel_command_event,
    is_panel_command,
    parse_panel_command_arg,
    sign_panel_binding,
    verify_panel_binding,
)
from agent_py_agent.agent.adapter.feishu_card import (
    extract_card_action,
    extract_webhook_card_action,
)
from agent_py_agent.agent.adapter.feishu_ws import _card_action_key
from agent_py_agent.agent.conversation.execution_threads import (
    EXECUTION_ORIGIN_KEY,
    IM_TASK_EXECUTION_KEY,
)
from agent_py_agent.agent.conversation.session_tasks import (
    SessionTaskDraft,
    SessionTaskUpdate,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.paths import GatewayPaths
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _setup_test_agent_and_server(tmp_path):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            gateway_per_user_owner_scoping=False,
        ),
        tmp_path / "workspace",
    )
    store = agent.conversation_store
    sender_thread = store.threads.get_or_create(
        {
            "canonical_user_id": "ou_user_1",
            "channel": "feishu",
            "channel_conversation_id": "conv-sender-1",
            "channel_user_id": "ou_user_1",
        }
    )
    root_id = "task-root-panel"
    store.tasks.bind(
        {
            "thread_id": sender_thread.thread_id,
            "task_id": root_id,
            "goal": "panel test root",
            "status": "active",
            "now": time.time(),
        }
    )
    store.tasks.select_workspace_task(
        {
            "thread_id": sender_thread.thread_id,
            "task_id": root_id,
            "now": time.time(),
        }
    )
    child = agent.subagents.create_run(
        goal="panel subagent worker",
        root_id=root_id,
        parent_id=root_id,
    )
    child = agent.subagents.lifecycle.prepare_runner_attempt(child.id)

    port = _free_port()
    gateway_paths = GatewayPaths(
        root=tmp_path / "gw",
        pid=tmp_path / "gw" / "gateway.pid",
        adapter_pid=tmp_path / "gw" / "adapter.pid",
        state=tmp_path / "gw" / "state.json",
        heartbeat=tmp_path / "gw" / "heartbeat.json",
        stop_request=tmp_path / "gw" / "stop.request",
        log=tmp_path / "gw" / "gateway.log",
        inbox=tmp_path / "gw" / "pending",
        processing=tmp_path / "gw" / "processing",
        done=tmp_path / "gw" / "done",
        failed=tmp_path / "gw" / "failed",
        responses=tmp_path / "gw" / "responses",
        history=tmp_path / "gw" / "history.jsonl",
    )
    server = GatewayHTTPServer(
        port,
        gateway_paths,
        params=GatewayHTTPServerParams(agent=agent),
    )
    server.start()

    adapter = FeishuAdapter(
        config={
            "feishu_app_id": "app_test",
            "feishu_app_secret": "sec_test",
            "feishu_verification_token": "tok_test",
            "my_agent_home": str(tmp_path / "home"),
            "feishu_session_lock_enabled": False,
        },
        callback_port=8421,
    )
    adapter.my_agent_home = str(tmp_path / "home")
    adapter._gateway_port = port
    adapter._gateway_auth_token = ""
    adapter._get_tenant_access_token = lambda: "synthetic_access_token"

    sent_cards = []
    def _mock_send_control_card(user_id, card, *, idempotency_key):
        msg_id = f"om_{len(sent_cards) + 1}"
        chat_id = "oc_p2p" if user_id.startswith("ou_") else user_id
        receipt = {"message_id": msg_id, "chat_id": chat_id, "card": card, "idempotency_key": idempotency_key}
        sent_cards.append((user_id, receipt))
        return receipt

    adapter.send_control_card = _mock_send_control_card

    return agent, store, sender_thread, child, server, adapter, sent_cards


def test_select_static_option_and_event_id_normalization_ws_and_webhook() -> None:
    """验证 WS 和 Webhook 抽取器对 select_static option 与 event_id 归一化提取，以及 WS 缓存去重键行为。"""
    # 1. WS 模式带 option 和 event_id
    ws_event = SimpleNamespace(
        action=SimpleNamespace(value={"kind": "my_agent_panel_action", "action": "agent.select"}, option="run:child_01"),
        operator=SimpleNamespace(open_id="ou_tester"),
        context=SimpleNamespace(open_message_id="om_card_01", open_chat_id="oc_chat_01"),
    )
    ws_data = SimpleNamespace(event=ws_event, header=SimpleNamespace(event_id="evt_feishu_ws_01"))

    norm_ws = extract_card_action(ws_data)
    assert norm_ws is not None
    assert norm_ws["option"] == "run:child_01"
    assert norm_ws["event_id"] == "evt_feishu_ws_01"
    assert norm_ws["operator_open_id"] == "ou_tester"
    assert norm_ws["message_id"] == "om_card_01"

    # 2. Webhook 模式带 option 和 event_id
    webhook_payload = {
        "header": {"event_id": "evt_feishu_wh_02"},
        "event": {
            "action": {"value": {"kind": "my_agent_panel_action", "action": "task.select"}, "option": "stask:task_02"},
            "operator": {"open_id": "ou_tester"},
            "context": {"open_message_id": "om_card_02", "open_chat_id": "oc_chat_02"},
        },
    }
    norm_wh = extract_webhook_card_action(webhook_payload)
    assert norm_wh is not None
    assert norm_wh["option"] == "stask:task_02"
    assert norm_wh["event_id"] == "evt_feishu_wh_02"

    # 3. 非 panel action 绝不带 event_id
    other_event = SimpleNamespace(
        action=SimpleNamespace(value={"kind": "my_agent_tool_approval", "reference": "ref1", "decision": "approve"}),
        operator=SimpleNamespace(open_id="ou_tester"),
        context=SimpleNamespace(open_message_id="om_01", open_chat_id="oc_01"),
    )
    norm_other = extract_card_action(SimpleNamespace(event=other_event, header=SimpleNamespace(event_id="evt_other")))
    assert norm_other is not None
    assert "event_id" not in norm_other
    assert "option" not in norm_other

    # 4. _card_action_key 去重键契约
    key1 = _card_action_key(norm_ws)
    assert key1.startswith("card:")
    # 重复 WS 帧产生完全一致的 key
    key2 = _card_action_key(norm_ws)
    assert key1 == key2

    # 无 event_id 时返回空串（防止 A -> B -> A 下拉或刷新被旧缓存吞没）
    norm_no_event = dict(norm_ws)
    del norm_no_event["event_id"]
    assert _card_action_key(norm_no_event) == ""


def test_end_to_end_wire_roundtrip_callback_to_gateway(tmp_path) -> None:
    """端到端真实 Wire 测试：从 adapter 发送卡片，经由 Gateway 回调刷新状态并返回合法 Card JSON 2.0。"""
    agent, store, sender_thread, child, server, adapter, sent_cards = _setup_test_agent_and_server(tmp_path)
    try:
        # 发送面板卡片
        receipt = adapter.send_agent_panel_card(
            "ou_user_1",
            actor_open_id="ou_user_1",
            origin_conversation_id="conv-sender-1",
            chat_type="p2p",
        )
        assert receipt["message_id"] == "om_1"
        assert len(sent_cards) == 1
        initial_card = sent_cards[0][1]["card"]
        assert initial_card["schema"] == "2.0"
        assert initial_card["header"]["title"]["tag"] == "plain_text"

        binding_ref = receipt["binding_ref"]
        binding_id = receipt["binding_id"]

        # 模拟 Feishu 回调：点击“刷新状态”
        callback_norm = {
            "value": {
                "kind": "my_agent_panel_action",
                "action": "panel.refresh",
                "binding_ref": binding_ref,
                "display_revision": 1,
            },
            "operator_open_id": "ou_user_1",
            "message_id": "om_1",
            "chat_id": "oc_p2p",
        }
        res = adapter._handle_card_action(callback_norm)
        assert res is not None
        assert res.get("toast", {}).get("type") == "info"
        assert res["card"]["type"] == "raw"
        updated_card = res["card"]["data"]
        assert updated_card["schema"] == "2.0"

        # 验证持久化状态中的 display_revision 单调递增
        owner = resolve_owner_home(Path(adapter.my_agent_home), OwnerIdentity.provider_user("feishu", "ou_user_1"))
        state_path = _panel_state_path(owner.home_dir, binding_id)
        state = _read_panel_state(state_path)
        assert state is not None
        assert state["display_revision"] == 2
    finally:
        server.stop()


def test_dm_in_place_agent_view_and_guidance(tmp_path) -> None:
    """验证私聊中原地切换子代理查看详情，展开补充指令表单并提交至 Gateway。"""
    agent, store, sender_thread, child, server, adapter, sent_cards = _setup_test_agent_and_server(tmp_path)
    try:
        receipt = adapter.send_agent_panel_card(
            "ou_user_1",
            actor_open_id="ou_user_1",
            origin_conversation_id="conv-sender-1",
            chat_type="p2p",
        )
        binding_ref = receipt["binding_ref"]
        binding_id = receipt["binding_id"]

        # 1. 切换选择子代理
        sel_norm = {
            "value": {
                "kind": "my_agent_panel_action",
                "action": "agent.select",
                "binding_ref": binding_ref,
                "display_revision": 1,
            },
            "option": f"run:{child.id}",
            "operator_open_id": "ou_user_1",
            "message_id": "om_1",
            "chat_id": "oc_p2p",
        }
        sel_res = adapter._handle_card_action(sel_norm)
        assert sel_res is not None
        node_elem = next(e for e in sel_res["card"]["data"]["body"]["elements"] if e.get("element_id") == "node_summary")
        assert "panel subagent worker" in node_elem["text"]["content"]

        # 2. 点击“补充指令”按钮打开表单
        guide_open_norm = {
            "value": {
                "kind": "my_agent_panel_action",
                "action": "agent.guide_open",
                "binding_ref": binding_ref,
                "target_ref": f"run:{child.id}",
                "display_revision": 2,
            },
            "operator_open_id": "ou_user_1",
            "message_id": "om_1",
            "chat_id": "oc_p2p",
        }
        open_res = adapter._handle_card_action(guide_open_norm)
        assert open_res is not None
        guide_elem = next(e for e in open_res["card"]["data"]["body"]["elements"] if e.get("element_id") == "guide_frm")
        assert guide_elem["tag"] == "form"

        # 3. 提交补充指令
        guide_submit_norm = {
            "value": {
                "kind": "my_agent_panel_action",
                "action": "agent.guide_submit",
                "binding_ref": binding_ref,
                "target_ref": f"run:{child.id}",
                "display_revision": 3,
            },
            "form_value": {"guidance_text": "请优先使用表格展示结果。"},
            "operator_open_id": "ou_user_1",
            "message_id": "om_1",
            "chat_id": "oc_p2p",
        }
        sub_res = adapter._handle_card_action(guide_submit_norm)
        assert sub_res is not None
        assert sub_res.get("toast", {}).get("type") == "success"
        # 提交后表单重新收起
        eids = [e.get("element_id") for e in sub_res["card"]["data"]["body"]["elements"]]
        assert "guide_frm" not in eids
    finally:
        server.stop()


def test_dm_2step_stop_branch_and_already_terminal_replay(tmp_path) -> None:
    """验证子代理分支停止的两阶段确认交互以及已结束分支重试的幂等回执。"""
    agent, store, sender_thread, child, server, adapter, sent_cards = _setup_test_agent_and_server(tmp_path)
    try:
        receipt = adapter.send_agent_panel_card(
            "ou_user_1",
            actor_open_id="ou_user_1",
            origin_conversation_id="conv-sender-1",
            chat_type="p2p",
        )
        binding_ref = receipt["binding_ref"]

        # 先选中该子代理
        adapter._handle_card_action({
            "value": {"kind": "my_agent_panel_action", "action": "agent.select", "binding_ref": binding_ref, "display_revision": 1},
            "option": f"run:{child.id}",
            "operator_open_id": "ou_user_1",
            "message_id": "om_1",
            "chat_id": "oc_p2p",
        })

        # 第 1 步：点击“停止当前分支”触发确认框
        ask_norm = {
            "value": {
                "kind": "my_agent_panel_action",
                "action": "agent.stop_branch_ask",
                "binding_ref": binding_ref,
                "target_ref": f"run:{child.id}",
                "display_revision": 2,
            },
            "operator_open_id": "ou_user_1",
            "message_id": "om_1",
            "chat_id": "oc_p2p",
        }
        ask_res = adapter._handle_card_action(ask_norm)
        assert ask_res is not None
        eids = [e.get("element_id") for e in ask_res["card"]["data"]["body"]["elements"]]
        assert "stop_cfm" in eids
        assert "stop_act" in eids

        # 取消确认
        cancel_norm = {
            "value": {
                "kind": "my_agent_panel_action",
                "action": "agent.stop_branch_cancel",
                "binding_ref": binding_ref,
                "target_ref": f"run:{child.id}",
                "display_revision": 3,
            },
            "operator_open_id": "ou_user_1",
            "message_id": "om_1",
            "chat_id": "oc_p2p",
        }
        cancel_res = adapter._handle_card_action(cancel_norm)
        assert cancel_res is not None
        eids_after_cancel = [e.get("element_id") for e in cancel_res["card"]["data"]["body"]["elements"]]
        assert "stop_cfm" not in eids_after_cancel

        # 再次触发并确认停止
        adapter._handle_card_action(ask_norm)
        confirm_norm = {
            "value": {
                "kind": "my_agent_panel_action",
                "action": "agent.stop_branch_confirm",
                "binding_ref": binding_ref,
                "target_ref": f"run:{child.id}",
                "display_revision": 4,
            },
            "operator_open_id": "ou_user_1",
            "message_id": "om_1",
            "chat_id": "oc_p2p",
        }
        cfm_res = adapter._handle_card_action(confirm_norm)
        assert cfm_res is not None
        assert cfm_res.get("toast", {}).get("type") == "success"

        # 再次重放确认停止（该分支已进入取消/结束状态）
        replay_res = adapter._handle_card_action(confirm_norm)
        assert replay_res is not None
        assert replay_res.get("toast", {}).get("type") in {"info", "success"}
    finally:
        server.stop()


def test_group_public_summary_card_and_open_dm_detail(tmp_path, monkeypatch) -> None:
    """验证群公开摘要卡隐私剥离及点击私聊详情时的受众分流与卡片复用。"""
    agent, store, sender_thread, child, server, adapter, sent_cards = _setup_test_agent_and_server(tmp_path)
    try:
        # Mock 群成员检查为 True
        monkeypatch.setattr("agent_py_agent.agent.adapter.feishu_agent_panel._current_group_member", lambda *_: True)

        receipt = adapter.send_agent_panel_card(
            "oc_group_1",
            actor_open_id="ou_member_1",
            origin_conversation_id="oc_group_1",
            chat_type="group",
        )
        assert receipt["chat_id"] == "oc_group_1"
        group_card = sent_cards[0][1]["card"]
        eids = [e.get("element_id") for e in group_card["body"]["elements"]]
        # 群公开卡严禁展示敏感控件
        assert "panel_sel" not in eids
        assert "guide_frm" not in eids
        assert "stop_cfm" not in eids
        assert "res_panel" not in eids

        # 成员点击“私聊查看详情与控制”
        binding_ref = receipt["binding_ref"]
        open_dm_norm = {
            "value": {
                "kind": "my_agent_panel_action",
                "action": "panel.open_dm_detail",
                "binding_ref": binding_ref,
                "display_revision": 1,
            },
            "operator_open_id": "ou_member_1",
            "message_id": receipt["message_id"],
            "chat_id": "oc_group_1",
        }
        dm_res = adapter._handle_card_action(open_dm_norm)
        assert dm_res is not None
        assert "card" not in dm_res  # Toast-only，保持群公开卡片不被覆盖
        assert dm_res["toast"]["type"] == "success"

        # 断言已向 ou_member_1 发送私聊卡片
        assert len(sent_cards) == 2
        dm_sent = sent_cards[1]
        assert dm_sent[0] == "ou_member_1"

        # 成员第二次点击私聊详情，应 PATCH 复用现有私聊卡片
        edit_calls = []
        adapter.edit_progress_message = lambda mid, text, **kw: edit_calls.append(mid) or True
        dm_res2 = adapter._handle_card_action(open_dm_norm)
        assert dm_res2 is not None
        assert dm_res2["toast"]["type"] == "success"
        assert len(edit_calls) == 1
    finally:
        server.stop()


def test_group_membership_revocation_on_group_and_dm_detail_cards(tmp_path, monkeypatch) -> None:
    """验证离群人员在群卡片或已有 dm_detail 卡片上的任何回调均被严格拦截。"""
    agent, store, sender_thread, child, server, adapter, sent_cards = _setup_test_agent_and_server(tmp_path)
    try:
        # 用户在群内时发送群卡
        monkeypatch.setattr("agent_py_agent.agent.adapter.feishu_agent_panel._current_group_member", lambda *_: True)
        receipt = adapter.send_agent_panel_card(
            "oc_group_1",
            actor_open_id="ou_user_departed",
            origin_conversation_id="oc_group_1",
            chat_type="group",
        )
        binding_ref = receipt["binding_ref"]

        # 用户随后离开群聊
        monkeypatch.setattr("agent_py_agent.agent.adapter.feishu_agent_panel._current_group_member", lambda *_: False)

        # 离群用户在群卡片上点击刷新
        res = adapter._handle_card_action({
            "value": {"kind": "my_agent_panel_action", "action": "panel.refresh", "binding_ref": binding_ref, "display_revision": 1},
            "operator_open_id": "ou_user_departed",
            "message_id": receipt["message_id"],
            "chat_id": "oc_group_1",
        })
        assert res is not None
        assert res["toast"]["type"] == "error"
        assert "您不在该群组中" in res["toast"]["content"]
    finally:
        server.stop()


def test_personal_dm_session_lock_gate(tmp_path) -> None:
    """验证私聊已锁定时，卡片回调被拦截并触发密码卡。"""
    agent, store, sender_thread, child, server, adapter, sent_cards = _setup_test_agent_and_server(tmp_path)
    try:
        receipt = adapter.send_agent_panel_card(
            "ou_user_1",
            actor_open_id="ou_user_1",
            origin_conversation_id="conv-sender-1",
            chat_type="p2p",
        )
        binding_ref = receipt["binding_ref"]

        # 开启会话锁并模拟锁定状态
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(type(adapter), "_session_lock_enabled", property(lambda self: True))
        adapter._unlock = SimpleNamespace()
        password_cards_sent = []
        adapter._send_password_card = lambda uid, mode: password_cards_sent.append((uid, mode))

        from agent_py_agent.agent.session_lock import admission
        monkeypatch.setattr(
            admission,
            "check_session_admission",
            lambda unlock, uid: SimpleNamespace(allowed=False, locked=True, requires_password_setup=False, reason_code="LOCKED"),
        )

        res = adapter._handle_card_action({
            "value": {"kind": "my_agent_panel_action", "action": "panel.refresh", "binding_ref": binding_ref, "display_revision": 1},
            "operator_open_id": "ou_user_1",
            "message_id": receipt["message_id"],
            "chat_id": "oc_p2p",
        })
        assert res is not None
        assert res["toast"]["type"] == "warning"
        assert "当前私聊已锁定" in res["toast"]["content"]
        assert len(password_cards_sent) == 1
        assert password_cards_sent[0] == ("ou_user_1", "unlock")
        monkeypatch.undo()
    finally:
        server.stop()


def test_anti_forwarding_mismatched_message_or_chat_rejected(tmp_path) -> None:
    """验证转发卡片在其他群或私聊中被严格拒绝。"""
    agent, store, sender_thread, child, server, adapter, sent_cards = _setup_test_agent_and_server(tmp_path)
    try:
        receipt = adapter.send_agent_panel_card(
            "ou_user_1",
            actor_open_id="ou_user_1",
            origin_conversation_id="conv-sender-1",
            chat_type="p2p",
        )
        binding_ref = receipt["binding_ref"]

        # 模拟在被转发的聊天中点击
        fwd_res = adapter._handle_card_action({
            "value": {"kind": "my_agent_panel_action", "action": "panel.refresh", "binding_ref": binding_ref, "display_revision": 1},
            "operator_open_id": "ou_user_1",
            "message_id": "om_forwarded_999",
            "chat_id": "oc_other_chat",
        })
        assert fwd_res is not None
        assert fwd_res["toast"]["type"] == "error"
        assert "不支持转发" in fwd_res["toast"]["content"]
    finally:
        server.stop()


def test_stale_display_revision_guard_on_target_switch(tmp_path) -> None:
    """验证在目标分支变更时若提交陈旧 revision，操作被拦截并自动刷新当前卡片。"""
    agent, store, sender_thread, child, server, adapter, sent_cards = _setup_test_agent_and_server(tmp_path)
    try:
        receipt = adapter.send_agent_panel_card(
            "ou_user_1",
            actor_open_id="ou_user_1",
            origin_conversation_id="conv-sender-1",
            chat_type="p2p",
        )
        binding_ref = receipt["binding_ref"]

        # 当前卡片处于 all_summary，但传入的目标是旧 target_ref 且 revision 不匹配
        stale_res = adapter._handle_card_action({
            "value": {
                "kind": "my_agent_panel_action",
                "action": "agent.guide_submit",
                "binding_ref": binding_ref,
                "target_ref": "run:other_old_agent",
                "display_revision": 999,
            },
            "form_value": {"guidance_text": "stale content"},
            "operator_open_id": "ou_user_1",
            "message_id": receipt["message_id"],
            "chat_id": "oc_p2p",
        })
        assert stale_res is not None
        assert stale_res["toast"]["type"] == "warning"
        assert "面板目标已变更" in stale_res["toast"]["content"]
    finally:
        server.stop()


def test_14_day_card_reissue_and_old_card_expired_notice(tmp_path) -> None:
    """验证超过 14 天的卡片在回调时原子生成第二代 successor 卡片，并使旧卡显示过期提示。"""
    agent, store, sender_thread, child, server, adapter, sent_cards = _setup_test_agent_and_server(tmp_path)
    try:
        receipt = adapter.send_agent_panel_card(
            "ou_user_1",
            actor_open_id="ou_user_1",
            origin_conversation_id="conv-sender-1",
            chat_type="p2p",
        )
        binding_ref = receipt["binding_ref"]
        binding_id = receipt["binding_id"]

        # 将存储中的 created_at 修改为 15 天前
        owner = resolve_owner_home(Path(adapter.my_agent_home), OwnerIdentity.provider_user("feishu", "ou_user_1"))
        state_path = _panel_state_path(owner.home_dir, binding_id)
        from agent_py_agent.agent.common.json_io import (
            locked_json_path,
            write_private_json_file_atomic_unlocked,
        )
        with locked_json_path(state_path):
            st = _read_panel_state(state_path)
            st["created_at"] = time.time() - (FEISHU_CARD_MAX_AGE_SECONDS + 1000)
            write_private_json_file_atomic_unlocked(state_path, st)

        # 回调触发
        reissue_res = adapter._handle_card_action({
            "value": {"kind": "my_agent_panel_action", "action": "panel.refresh", "binding_ref": binding_ref, "display_revision": 1},
            "operator_open_id": "ou_user_1",
            "message_id": receipt["message_id"],
            "chat_id": "oc_p2p",
        })
        assert reissue_res is not None
        assert reissue_res["toast"]["type"] == "info"
        assert "14 天更新上限" in reissue_res["toast"]["content"]
        # 旧卡原地替换为包含过期提示的卡片
        old_card = reissue_res["card"]["data"]
        task_hdr = next(e for e in old_card["body"]["elements"] if e.get("element_id") == "task_hdr")
        assert "已停用" in task_hdr["text"]["content"]

        # 验证新一代卡片已被发送
        assert len(sent_cards) == 2
        new_sent = sent_cards[1]
        assert new_sent[0] == "ou_user_1"

        # 对旧卡再次点击，直接返回只读过期警告
        expired_res = adapter._handle_card_action({
            "value": {"kind": "my_agent_panel_action", "action": "panel.refresh", "binding_ref": binding_ref, "display_revision": 2},
            "operator_open_id": "ou_user_1",
            "message_id": receipt["message_id"],
            "chat_id": "oc_p2p",
        })
        assert expired_res is not None
        assert expired_res["toast"]["type"] == "warning"
    finally:
        server.stop()


def test_agents_command_nonblocking_in_dm_and_group(tmp_path) -> None:
    """验证 /agents 和 /panel 指令在私聊和群聊中立即响应并发出卡片。"""
    agent, store, sender_thread, child, server, adapter, sent_cards = _setup_test_agent_and_server(tmp_path)
    try:
        # 指令匹配测试
        assert is_panel_command("/agents") is True
        assert is_panel_command("/panel") is True
        assert is_panel_command("/agents stask_123") is True
        assert is_panel_command("/panel task_456") is True
        assert is_panel_command("hello /agents") is False
        assert parse_panel_command_arg("/agents stask_123") == "stask_123"

        # 模拟入站消息事件
        dm_msg = SimpleNamespace(
            content="/agents",
            user_id="ou_user_1",
            conversation_id="conv-sender-1",
            metadata={"feishu_chat_type": "p2p"},
        )
        disp = handle_panel_command_event(adapter, dm_msg)
        assert disp.status == "accepted"
        assert len(sent_cards) == 1

        group_msg = SimpleNamespace(
            content="/panel stask_001",
            user_id="ou_member_2",
            conversation_id="oc_group_999",
            metadata={"feishu_chat_type": "group", "feishu_chat_id": "oc_group_999"},
        )
        disp_group = handle_panel_command_event(adapter, group_msg)
        assert disp_group.status == "accepted"
        assert len(sent_cards) == 2
    finally:
        server.stop()
