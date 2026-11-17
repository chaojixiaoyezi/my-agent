"""Track C: a task-scoped credential can be listed, checked and revoked from a later request; secrets stay task-bound."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts.private_interaction_service import (
    resolve_private_interaction,
)
from agent_py_agent.agent.tooling.task_credential_tool import RequestCredentialsTool
from agent_py_agent.agent.user_space import task_credentials
from agent_py_agent.agent.user_space.task_credentials import (
    credential_headers,
    list_credentials_for_owner,
    manage_credential_for_owner,
)
from agent_py_agent.cli import credential_commands
from agent_py_agent.tests.test_background_tool_approval import background
from agent_py_agent.tests.test_task_credentials import _card

_FIXTURES = (background,)
_SECRET = "Synthetic-API-Secret-123"


def _second_request(ctx, task_id="second-work"):
    store = ctx.store
    thread = store.threads.get_or_create({
        "canonical_user_id": "local-agent", "channel": "chat", "owner_id": ctx.agent.home_paths.owner_id,
        "channel_conversation_id": "session-two", "channel_user_id": "local-agent",
    })
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": task_id, "goal": "后续请求", "status": "active"})
    store.tasks.select_workspace_task({"thread_id": thread.thread_id, "task_id": task_id})
    store.claims.acquire({"thread_id": thread.thread_id, "task_id": task_id, "reason": "thread_goal_continue"})
    ctx.agent._current_run_params = SimpleNamespace(run_id=task_id, task_id=task_id, task_attributes={})


def _call(agent, **params):
    outcome = RequestCredentialsTool(agent).execute(params)
    return outcome, (json.loads(outcome.output) if outcome.ok else {})


def _submitted(ctx):
    ref, path, body, _sent = _card(ctx)
    assert resolve_private_interaction(ctx.agent, body, channel="feishu", actor_id="ou_synthetic")["ok"]
    return ref, path


def test_status_list_and_revoke_work_from_a_later_request(background):
    ref, path = _submitted(background)
    _second_request(background)
    status, view = _call(background.agent, operation="status", credential_ref=ref)
    assert status.ok, (status.error_code, status.output)
    assert (view["status"], view["access_scope"], view["origin_task_id"]) == ("ready", "task", background.task_id)
    assert view["usable_in_current_task"] is False and view["secret_present"] is True
    listed, found = _call(background.agent, operation="list")
    assert listed.ok and found["task_scoped_count"] == 1
    entry = next(row for row in found["credentials"] if row["credential_ref"] == ref)
    assert entry["access_scope"] == "task" and entry["usable"] is False and entry["origin_task_id"] == background.task_id
    assert _SECRET not in listed.output and "synthetic-user" not in listed.output
    with pytest.raises(ValueError):
        credential_headers(background.agent, ref, "https://example.com/api")
    revoked, receipt = _call(background.agent, operation="revoke", credential_ref=ref)
    assert revoked.ok and receipt["revoked"] is True and receipt["deleted"] is True and receipt["status"] == "revoked"
    stored = json.loads(path.read_text())
    assert "ciphertext" not in stored and "session_ciphertext" not in stored and stored["status"] == "revoked"
    again, after = _call(background.agent, operation="status", credential_ref=ref)
    assert again.ok and after["status"] == "revoked" and after["secret_present"] is False


def test_other_actor_cannot_manage_but_verified_admin_can(background, monkeypatch):
    ref, _path = _submitted(background)
    _second_request(background)
    monkeypatch.setattr(task_credentials, "approval_recipient", lambda agent, scope: ("feishu", "ou_someone_else"))
    denied, _ = _call(background.agent, operation="status", credential_ref=ref)
    assert not denied.ok and denied.error_code == "CREDENTIAL_BINDING_INVALID"
    monkeypatch.setattr(task_credentials, "_manages_as_foreground_admin", lambda agent: True)
    allowed, view = _call(background.agent, operation="status", credential_ref=ref)
    assert allowed.ok and view["credential_ref"] == ref


def test_owner_cli_lists_and_revokes_without_a_task(background, monkeypatch, capsys):
    ref, path = _submitted(background)
    background.agent._current_run_params = None
    home = background.agent.home_paths
    rows = list_credentials_for_owner(home)["credentials"]
    assert [row["credential_ref"] for row in rows] == [ref] and rows[0]["secret_present"] is True
    assert manage_credential_for_owner(home, ref, operation="status")["status"] == "ready"
    monkeypatch.setenv("MY_AGENT_HOME", str(home.root))
    assert credential_commands.cmd_credentials(SimpleNamespace(credentials_action="list", owner="", json=True)) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["ok"] and listed["owner_id"] == "local/main" and listed["credentials"][0]["credential_ref"] == ref
    assert credential_commands.cmd_credentials(SimpleNamespace(credentials_action="revoke", owner="local/main",
                                                               credential_ref=ref, json=False)) == 0
    assert "revoked=true deleted=true" in capsys.readouterr().out
    assert "ciphertext" not in json.loads(path.read_text()) and _SECRET not in path.read_text()
    assert credential_commands.cmd_credentials(SimpleNamespace(credentials_action="status", owner="providers/feishu/users/ou_missing",
                                                               credential_ref=ref, json=True)) == 1
    assert json.loads(capsys.readouterr().out)["error_code"] == "OWNER_NOT_FOUND"
    missing = "cred_" + "0" * 32
    assert credential_commands.cmd_credentials(SimpleNamespace(credentials_action="status", owner="", credential_ref=missing,
                                                               json=True)) == 1
    assert json.loads(capsys.readouterr().out)["error_code"] == "CREDENTIAL_NOT_FOUND"
