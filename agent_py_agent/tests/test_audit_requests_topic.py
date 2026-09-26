"""audit_records 的 requests 主题：按宿主写入的 owner_id（旧记录退回会话）归属请求结果，只投影结构化字段，不读正文。"""
from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.request_binding import GatewayTaskBindingWriter
from agent_py_agent.agent.gateway_parts.request_execution import _executing_owner_id
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.admin_channel_identity import bind_admin_channel_identity
from agent_py_agent.agent.user_space.admin_password import set_admin_password
from agent_py_agent.agent.user_space.owner_admin_controls import set_owner_admin_controls

ALICE = "providers/feishu/users/ou-alice"
MAIN, ALICE_OWNER = ("local", "main", "main"), ("feishu", "user", "ou-alice")
SECRET = "SECRET-PROMPT-TEXT"


def _agent(tmp_path, monkeypatch, owner=MAIN) -> SimpleAgent:
    provider, owner_kind, owner_id = owner
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "home"))
    return SimpleAgent(AgentConfig(model_backend="echo", enable_tools=True, my_agent_owner_provider=provider,
                                   my_agent_owner_kind=owner_kind, my_agent_owner_id=owner_id), tmp_path / "project")


def _record(root, folder, request_id, *, owner_id="", thread_id="", error="", age_hours=0.0, chat_type="p2p"):
    path = root / "requests" / folder / f"{request_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"id": request_id, "kind": "ask", "goal": SECRET, "prompt": SECRET,
               "status": "failed" if error else "done", "error_code": error,
               "metadata": {"channel": "feishu", "channel_chat_type": chat_type, "user_id": "ou-alice"},
               "terminal_response": {"response": SECRET, "user_error": SECRET, "duration_seconds": 1.5, "tool_rounds": 0,
                                     **({"owner_id": owner_id} if owner_id else {})}}
    if thread_id:
        payload["conversation_claim"] = {"schema_version": "gateway_conversation_claim.v1", "request_id": request_id,
                                         "thread_id": thread_id, "task_id": f"gateway:{request_id}"}
    path.write_text(json.dumps(payload), encoding="utf-8")
    when = time.time() - age_hours * 3600
    os.utime(path, (when, when))


def _in_gateway_turn(agent, root, thread_id=""):
    writer = GatewayTaskBindingWriter(request_path=root / "requests" / "processing" / "current.json", request_id="current")
    agent._current_run_params = SimpleNamespace(conversation_task_binding_callback=writer,
                                                task_attributes={"conversation_thread_id": thread_id})


def _call(agent, params):
    outcome = agent.tools.tools["audit_records"].execute(params)
    return outcome, json.loads(outcome.output)


def _seed(tmp_path, monkeypatch):
    alice = _agent(tmp_path, monkeypatch, ALICE_OWNER)
    main = _agent(tmp_path, monkeypatch)
    main_thread = main.conversation_store.threads.get_or_create({"canonical_user_id": "local-agent", "channel": "chat",
                                                                 "channel_conversation_id": "tui", "channel_user_id": "local-agent"})
    queue = tmp_path / "gateway"
    for folder in ("failed", "terminal"):  # 同一请求两份，必须去重
        _record(queue, folder, "req-alice-fail", owner_id=ALICE, error="MODEL_NOT_CONFIGURED")
    _record(queue, "done", "req-main-legacy", thread_id=main_thread.thread_id)
    _record(queue, "failed", "req-unattributed", error="PROVIDER_CONNECTION_FAILED", chat_type="group")
    _record(queue, "done", "req-too-old", owner_id="local/main", age_hours=30)
    return alice, main, main_thread, queue


def test_owner_scope_uses_stamped_owner_then_thread_and_never_bodies(tmp_path, monkeypatch):
    alice, main, _thread, queue = _seed(tmp_path, monkeypatch)
    _in_gateway_turn(main, queue)
    outcome, report = _call(main, {"topic": "requests"})
    assert outcome.ok, outcome.output
    assert [row["request_id"] for row in report["requests"]["entries"]] == ["req-main-legacy"]
    assert report["sources"] == ["gateway_request_records", "error_taxonomy"]
    assert SECRET not in outcome.output
    _in_gateway_turn(alice, queue)
    outcome, report = _call(alice, {"topic": "requests"})
    entries = report["requests"]["entries"]
    assert [row["request_id"] for row in entries] == ["req-alice-fail"], "按宿主写入的 owner_id 归属，且两份去重"
    assert entries[0]["error_code"] == "MODEL_NOT_CONFIGURED" and entries[0]["chat_type"] == "p2p"
    assert entries[0]["error"]["recovery_hint"] and entries[0]["error"]["recommended_action"]
    assert "admin_identity" not in report, "只给管理员看管理员身份事实"


def test_all_owners_needs_explicit_permission_and_reports_admin_identity(tmp_path, monkeypatch):
    _alice, main, _thread, queue = _seed(tmp_path, monkeypatch)
    _in_gateway_turn(main, queue)
    refused, body = _call(main, {"topic": "requests", "scope": "all_owners"})
    assert not refused.ok and refused.error_code == "AUDIT_ACCESS_DENIED" and body["reason"] == "cross_owner_not_allowed"
    set_owner_admin_controls(main.home_paths, main.home_paths, {"cross_owner_audit_allowed": True}, actor="local/main")
    set_admin_password(main.home_paths.root, "Correct-Horse-42")
    bind_admin_channel_identity(main.home_paths.root, "feishu", "ou-alice")
    outcome, report = _call(main, {"topic": "requests", "scope": "all_owners"})
    assert outcome.ok, outcome.output
    by_id = {row["request_id"]: row for row in report["requests"]["entries"]}
    assert set(by_id) == {"req-alice-fail", "req-main-legacy", "req-unattributed"}
    assert by_id["req-alice-fail"]["owner_id"] == ALICE and by_id["req-unattributed"]["owner_id"] == "unattributed"
    assert report["requests"]["counts"]["error_code"]["MODEL_NOT_CONFIGURED"] == 1
    admin = report["admin_identity"]
    assert admin["password_configured"] is True
    assert [(row["channel"], row["user_id"]) for row in admin["bound_private_chats"]] == [("feishu", "ou-alice")]
    assert SECRET not in outcome.output and "Correct-Horse-42" not in outcome.output


def test_current_thread_scope_filters_by_claimed_thread(tmp_path, monkeypatch):
    _alice, main, thread, queue = _seed(tmp_path, monkeypatch)
    _record(queue, "done", "req-main-other-thread", owner_id="local/main", thread_id="thread-other")
    _in_gateway_turn(main, queue, thread.thread_id)
    outcome, report = _call(main, {"topic": "requests", "scope": "current_thread"})
    assert outcome.ok, outcome.output
    assert [row["request_id"] for row in report["requests"]["entries"]] == ["req-main-legacy"]


def test_requests_without_gateway_context_are_reported_unavailable(tmp_path, monkeypatch):
    main = _agent(tmp_path, monkeypatch)
    outcome, report = _call(main, {"topic": "requests"})
    assert outcome.ok and report["requests"] == {"available": False, "reason": "no_gateway_request_context"}


def test_gateway_response_owner_id_is_the_executing_owner(tmp_path, monkeypatch):
    alice = _agent(tmp_path, monkeypatch, ALICE_OWNER)
    main = _agent(tmp_path, monkeypatch)
    assert _executing_owner_id(alice) == ALICE and _executing_owner_id(main) == "local/main"
