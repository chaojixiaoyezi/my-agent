"""首次私聊未设置密码时，连续消息不能越过原飞书入站门；设置后沿原队列恢复。"""
from __future__ import annotations

import json
from threading import Event

from agent_py_agent.agent.adapter.feishu import FeishuAdapter


# LLM: 输入使用 webhook/WS 共用的飞书结构化信封；正文是合成测试数据，不授予角色或解除密码锁。
# 函数用途: 构造一个普通用户的新私聊消息，让实际入口完成归一化和锁门判定。
def event(message_id: str, *, user_id: str = "ou_new_user", chat_type: str = "p2p"):
    return {"event": {"sender": {"sender_id": {"open_id": user_id}}, "message": {
        "message_id": message_id, "chat_type": chat_type, "chat_id": "oc_test_private",
        "message_type": "text", "content": json.dumps({"text": "请计算 12 加 13"})}}}


def test_repeated_messages_without_setting_password_never_dispatch(tmp_path):
    adapter = FeishuAdapter(config={"my_agent_home": str(tmp_path)})
    delivered = []
    cards = []
    adapter.on_message(delivered.append)
    adapter._send_password_card = lambda user_id, mode: cards.append((user_id, mode)) or True
    for index in range(5):
        adapter._handle_feishu_event(event(f"new-{index}"))
    assert not adapter._unlock.store.has_password("ou_new_user")
    assert delivered == []
    assert cards == [("ou_new_user", "set")] * 5
    assert adapter._pending_unlock_messages.depth("ou_new_user") == 5


# LLM: 操作身份来自可信平台字段，表单值仅由密码服务验证；失败不产生认证、续送或回显。
# 函数用途: 在测试里模拟首设/解锁卡提交，保留对应用户的真实身份检查。
def submit(adapter, action="pwd_set", password="Abcd1234", operator="ou_new_user"):
    return adapter._handle_card_action({"value": {"session_lock_action": action, "user_id": "ou_new_user"},
                                       "form_value": {"pwd": password}, "operator_open_id": operator})


def test_invalid_setup_and_foreign_operator_never_dispatch(tmp_path):
    adapter = FeishuAdapter(config={"my_agent_home": str(tmp_path)})
    delivered = []
    adapter.on_message(delivered.append)
    adapter._send_password_card = lambda *_: True
    adapter._handle_feishu_event(event("new-input"))
    for response in [submit(adapter, password="bad"), submit(adapter, operator="ou_someone_else")]:
        assert response["toast"]["type"] == "error"
    assert not adapter._unlock.store.has_password("ou_new_user")
    assert delivered == []
    assert adapter._pending_unlock_messages.depth("ou_new_user") == 1


def test_successful_setup_resumes_original_messages_once(tmp_path):
    adapter = FeishuAdapter(config={"my_agent_home": str(tmp_path)})
    delivered = []
    done = Event()
    adapter.on_message(lambda msg: (delivered.append(msg.message_id), done.set() if len(delivered) == 3 else None))
    adapter._send_password_card = lambda *_: True
    for index in range(3):
        adapter._handle_feishu_event(event(f"setup-{index}"))
    assert delivered == []
    assert submit(adapter)["toast"]["type"] == "success"
    assert done.wait(2)
    assert delivered == ["setup-0", "setup-1", "setup-2"]
    submit(adapter)
    adapter._handle_feishu_event(event("setup-0"))
    assert delivered == ["setup-0", "setup-1", "setup-2"]


def test_card_failure_and_unavailable_lock_never_dispatch(tmp_path, monkeypatch):
    adapter = FeishuAdapter(config={"my_agent_home": str(tmp_path)})
    delivered = []
    adapter.on_message(delivered.append)
    adapter._send_password_card = lambda *_: False
    adapter._handle_feishu_event(event("send-failed"))
    assert delivered == []
    monkeypatch.setattr(adapter._unlock, "status", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("private-test-error")))
    adapter._handle_feishu_event(event("state-failed"))
    assert delivered == []
    adapter._unlock = None
    adapter._handle_feishu_event(event("service-failed"))
    assert delivered == []


def test_explicit_disable_still_works_without_password(tmp_path):
    adapter = FeishuAdapter(config={"my_agent_home": str(tmp_path), "feishu_session_lock_enabled": False})
    delivered = []
    adapter.on_message(delivered.append)
    adapter._handle_feishu_event(event("disabled"))
    assert [msg.message_id for msg in delivered] == ["disabled"]


def test_alerts_send_while_unenrolled_or_locked_without_unlocking(tmp_path, monkeypatch):
    import time

    from agent_py_agent.agent.adapter import feishu
    from agent_py_agent.agent.adapter.protocol import OutgoingMessage

    for phase in ["unset", "locked"]:
        adapter = FeishuAdapter(config={"my_agent_home": str(tmp_path / phase)})
        if phase == "locked":
            adapter._unlock.set_password("ou_new_user", "Abcd1234")
            adapter._unlock.store.record_activity("ou_new_user", now=time.time() - 4*60*60)
        before = adapter._unlock.status("ou_new_user")
        last = adapter._unlock.store.last_activity("ou_new_user")
        sent = []
        adapter._get_tenant_access_token = lambda: "test-token"
        monkeypatch.setattr(feishu, "_send_feishu_rendered", lambda *args: sent.append(args[0]) or True)
        assert adapter.send_message("ou_new_user", OutgoingMessage(channel="feishu", user_id="ou_new_user", content="合成测试告警"))
        assert sent == ["ou_new_user"]
        after = adapter._unlock.status("ou_new_user")
        assert (after.locked, after.requires_password_setup) == (before.locked, before.requires_password_setup)
        assert adapter._unlock.store.last_activity("ou_new_user") == last


def test_card_style_cannot_turn_failed_setup_into_authentication(tmp_path, monkeypatch):
    from agent_py_agent.agent.session_lock import feishu_cards

    adapter = FeishuAdapter(config={"my_agent_home": str(tmp_path)})
    delivered = []
    adapter.on_message(delivered.append)
    adapter._send_password_card = lambda *_: True
    adapter._handle_feishu_event(event("not-authorized"))
    monkeypatch.setattr(feishu_cards, "build_password_resolved_card", lambda **_: {"header": {"template": "green"}})
    response = submit(adapter, password="bad")
    assert response["toast"]["type"] == "error"
    assert delivered == []
    assert not adapter._unlock.store.has_password("ou_new_user")
