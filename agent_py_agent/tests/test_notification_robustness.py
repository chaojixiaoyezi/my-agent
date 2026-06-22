"""通知子系统健壮性/隔离全面测试:跨用户隔离 + 通道故障隔离(混合态)+ 投递异常防御 + 边界。

重点(多租户/可靠性):① 跨用户不串(get_pending/list 按 user_id 过滤)② 一个通道死不阻塞另一个
(混合在线/离线)③ 适配器抛异常 deliver 不崩(记 failed)④ 损坏/缺字段的 session 不崩路由。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_py_agent.agent.notification.manager import NotificationManager
from agent_py_agent.agent.notification.router import NotificationRouter


def _config(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(
        session_workspace=str(tmp_path / "sessions"),
        adapter_workspace=str(tmp_path / "adapters"),
        notification_store_path=str(tmp_path / "notifications"),
        notification_channel_timeout_seconds=300,
    )


def _notif(mgr: NotificationManager, *, user: str, channel: str = "chat", message: str = "m"):
    return mgr.create_notification(task_id="t", user_id=user, session_id="s", channel=channel, message=message)


# --- 1. 跨用户隔离(多租户硬要求)----------------------------------------------


def test_cross_user_isolation_get_pending_and_list(tmp_path) -> None:
    mgr = NotificationManager(_config(tmp_path))
    _notif(mgr, user="alice", message="A的私密通知")
    _notif(mgr, user="bob", message="B的私密通知")

    pa = mgr.get_pending("alice")
    assert len(pa) == 1 and pa[0].user_id == "alice"  # ⭐ alice 只见自己
    pb = mgr.get_pending("bob")
    assert len(pb) == 1 and pb[0].user_id == "bob"
    # list_notifications 同样按 user 过滤,B 的内容绝不出现在 A 的列表
    a_messages = [n.message for n in mgr.list_notifications("alice")]
    assert "B的私密通知" not in a_messages and "A的私密通知" in a_messages


def test_get_pending_excludes_delivered_and_failed(tmp_path) -> None:
    mgr = NotificationManager(_config(tmp_path))
    n1 = _notif(mgr, user="u")  # pending
    n2 = _notif(mgr, user="u")
    mgr.mark_delivered(n2.notification_id, "chat")  # 已投递
    n3 = _notif(mgr, user="u")
    mgr.mark_failed(n3.notification_id, "x")  # 失败
    pending = mgr.get_pending("u")
    assert [n.notification_id for n in pending] == [n1.notification_id]  # 只剩 pending/stored


# --- 2. 通道故障隔离(混合在线/离线)+ 路由策略 --------------------------------


def test_origin_channel_offline_falls_back_to_online_chat(tmp_path) -> None:
    """发起通道 feishu 死 + chat 在线 → 落到 chat(一个通道死不阻塞投递)。"""
    router = NotificationRouter(_config(tmp_path))
    router._channel_checker.register_status("feishu", False)
    router._channel_checker.register_status("chat", True)
    assert router.route(_notif(router._manager, user="u", channel="feishu")) == "chat"


def test_route_prefers_origin_when_online(tmp_path) -> None:
    router = NotificationRouter(_config(tmp_path))
    router._channel_checker.register_status("feishu", True)
    assert router.route(_notif(router._manager, user="u", channel="feishu")) == "feishu"


def test_route_all_offline_returns_none(tmp_path) -> None:
    router = NotificationRouter(_config(tmp_path))
    for ch in ("chat", "feishu", "qq"):
        router._channel_checker.register_status(ch, False)
    assert router.route(_notif(router._manager, user="u", channel="feishu")) is None  # 全离线 → 待存储


def test_origin_excluded_from_fallback(tmp_path) -> None:
    """发起通道死后,fallback 不会再重试同一个发起通道,而是去别的在线通道。"""
    router = NotificationRouter(_config(tmp_path))
    router._channel_checker.register_status("feishu", False)  # 发起且死
    router._channel_checker.register_status("chat", False)
    router._channel_checker.register_status("qq", True)
    assert router.route(_notif(router._manager, user="u", channel="feishu")) == "qq"


# --- 3. 投递异常防御(适配器炸了不连带崩)-------------------------------------


def test_deliver_adapter_exception_marks_failed_not_crash(tmp_path) -> None:
    """_do_deliver 抛异常 → deliver 不崩,记 failed 返回(通道适配器炸了不拖垮通知系统)。"""
    router = NotificationRouter(_config(tmp_path))
    router._channel_checker.register_status("chat", True)
    notif = _notif(router._manager, user="u", channel="chat")
    with patch.object(router, "_do_deliver", side_effect=RuntimeError("adapter crashed")):
        success, info = router.deliver(notif.notification_id)  # 不崩
    assert success is False and "异常" in info
    assert router._manager.load_notification(notif.notification_id).status == "failed"  # 标记失败


def test_deliver_lifecycle_online_stored_failed(tmp_path) -> None:
    router = NotificationRouter(_config(tmp_path))
    # 在线 → delivered
    router._channel_checker.register_status("chat", True)
    n_ok = _notif(router._manager, user="u", channel="chat")
    assert router.deliver(n_ok.notification_id) == (True, "chat")
    assert router._manager.load_notification(n_ok.notification_id).status == "delivered"
    # 全离线 → stored
    for ch in ("chat", "feishu", "qq"):
        router._channel_checker.register_status(ch, False)
    n_store = _notif(router._manager, user="u", channel="feishu")
    ok, _info = router.deliver(n_store.notification_id)
    assert ok is False and router._manager.load_notification(n_store.notification_id).status == "stored"
    # 适配器返回 False → failed
    router._channel_checker.register_status("chat", True)
    n_fail = _notif(router._manager, user="u", channel="chat")
    with patch.object(router, "_do_deliver", return_value=False):
        router.deliver(n_fail.notification_id)
    assert router._manager.load_notification(n_fail.notification_id).status == "failed"


def test_flush_stored_retries_only_stored_when_channel_back(tmp_path) -> None:
    router = NotificationRouter(_config(tmp_path))
    for ch in ("chat", "feishu", "qq"):
        router._channel_checker.register_status(ch, False)
    notif = _notif(router._manager, user="u", channel="chat")
    router.deliver(notif.notification_id)  # 全离线 → stored
    assert router._manager.load_notification(notif.notification_id).status == "stored"
    router._channel_checker.register_status("chat", True)  # chat 回来
    assert router.flush_stored("u") == 1  # 重投成功
    assert router._manager.load_notification(notif.notification_id).status == "delivered"


# --- 4. 边界/健壮性:损坏或缺字段的 session 不崩路由 + 幂等/缺失 --------------


def _write_session(cfg, name: str, payload) -> None:
    sess = Path(cfg.session_workspace) / name
    sess.mkdir(parents=True, exist_ok=True)
    (sess / "session.json").write_text(payload, encoding="utf-8")


def test_corrupted_session_json_does_not_crash_routing(tmp_path) -> None:
    cfg = _config(tmp_path)
    _write_session(cfg, "s1", "{ this is broken json")
    router = NotificationRouter(cfg)
    # chat 靠 session(损坏)、feishu/qq 无 adapter → 全不在线;损坏 session 被跳过,不崩
    assert router.route(_notif(router._manager, user="u", channel="feishu")) is None


def test_session_missing_user_id_is_skipped(tmp_path) -> None:
    cfg = _config(tmp_path)
    _write_session(cfg, "s1", json.dumps({"last_active_channel": "chat", "updated_at": time.time()}))  # 无 user_id
    router = NotificationRouter(cfg)
    # session 无 user_id → 不匹配当前 user → 不路由到 chat;别的通道也不在线 → None(不串户、不崩)
    assert router.route(_notif(router._manager, user="u", channel="feishu")) is None


def test_mark_and_load_missing_notification(tmp_path) -> None:
    mgr = NotificationManager(_config(tmp_path))
    assert mgr.load_notification("ghost") is None
    assert mgr.mark_delivered("ghost", "chat") is False  # 缺失通知优雅返回 False,不崩


def test_idempotent_mark_delivered(tmp_path) -> None:
    mgr = NotificationManager(_config(tmp_path))
    n = _notif(mgr, user="u")
    assert mgr.mark_delivered(n.notification_id, "chat") is True
    assert mgr.mark_delivered(n.notification_id, "chat") is True  # 二次不崩,不损坏
    assert mgr.load_notification(n.notification_id).status == "delivered"


def test_corrupted_notification_file_loads_as_none(tmp_path) -> None:
    mgr = NotificationManager(_config(tmp_path))
    n = _notif(mgr, user="u")
    mgr._get_notification_path(n.notification_id).write_text("{ broken", encoding="utf-8")
    # 损坏的通知文件:load 返回 None 或被 get_pending 跳过,不崩
    try:
        assert mgr.load_notification(n.notification_id) is None
    except Exception as exc:  # noqa: BLE001 - 明确断言不该崩
        raise AssertionError(f"损坏通知文件不应让 load 崩: {type(exc).__name__}") from exc
