"""S13: admin self-cleanup — cursor survives redaction, per-reason breakdown, wake dismiss/replay, cross-owner, exact hints."""
from __future__ import annotations

import base64
import errno
import hashlib
import json

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.conversation.models import ConversationThread
from agent_py_agent.agent.conversation.runtime_reconciliation import (
    decode_reconcile_cursor,
    reconcile_owner_runtime,
)
from agent_py_agent.agent.conversation.store_wake_attempts import WAKE_REPLAY_ARCHIVED
from agent_py_agent.agent.path_access_policy import HOST_STATE_READ_ONLY_ENV
from agent_py_agent.agent.tooling.runtime_governance_tool import (
    RuntimeGovernanceTool,
    _exempt_cursor,
    _reply,
)
from agent_py_agent.cli import host_state_guard
from agent_py_agent.tests.test_f155_w9_runtime_reconciliation import _terminal_root
from agent_py_agent.tests.test_schedule_control import _agent
from agent_py_agent.tests.test_wake_quarantine_archive import _quarantine, _raise

_SIG = hashlib.sha256(b"binding").hexdigest()


def _body(outcome):
    return json.loads(outcome.content if hasattr(outcome, "content") else outcome.output)


def _foreground(agent):
    agent._current_run_params = RunParams(host_foreground_interactive=True)


def _quarantined(store, thread="thread-a"):
    store.threads.write(ConversationThread(thread_id=thread, canonical_user_id="u"))
    signal = _raise(store, thread=thread)
    _quarantine(store, signal)
    return signal.wake_signal_id


def test_reply_keeps_only_grammar_valid_cursors_and_still_redacts_everything_else():
    cursor = json.dumps({"phase": "task_runs", "after": "tr_1", "created_at": 1.5, "owner": "local/main", "signature": _SIG},
                        separators=(",", ":"))
    out = json.loads(_reply({"next_cursor": cursor, "signature": _SIG}).output)
    assert out["next_cursor"] == cursor and out["signature"] != _SIG
    forged = json.dumps({"phase": "task_runs", "signature": _SIG, "token": "x"})
    assert _exempt_cursor(forged) is None
    assert _exempt_cursor(json.dumps({"phase": "elsewhere", "signature": _SIG})) is None
    leaking = json.dumps({"phase": "task_runs", "after": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456", "signature": _SIG})
    assert _exempt_cursor(leaking) is None
    wake = base64.urlsafe_b64encode(json.dumps({"binding": _SIG, "unreadable_seen": 0, "phase": "quarantine", "scan": {},
                                                "pending": [], "complete": False}).encode()).decode()
    assert _exempt_cursor(wake) == wake


def test_reconcile_preview_pages_through_the_tool_and_reports_reasons(tmp_path):
    agent = _agent(tmp_path)
    created = [_terminal_root(agent, root=f"root-{index}") for index in range(3)]
    tool = RuntimeGovernanceTool(agent, is_admin=True)
    seen, cursor, counts = [], "", {}
    for _ in range(5):
        body = _body(tool.execute({"action": "reconcile_preview", "limit": 1, **({"cursor": cursor} if cursor else {})}))
        assert not body["errors"], body
        seen += [row["task_run_id"] for row in body["items"] if row.get("kind") == "task_run"]
        for code, count in body["reason_counts"].items():
            counts[code] = counts.get(code, 0) + count
        cursor = body["next_cursor"]
        if not cursor or decode_reconcile_cursor(cursor)["phase"] != "task_runs":
            break
    assert sorted(seen) == sorted(row["task_run_id"] for row in created)
    assert sum(counts.values()) == 3, counts


def test_protected_rows_carry_decision_facts_and_guidance(tmp_path):
    agent = _agent(tmp_path)
    repo = agent.subagents.runtime_db
    active = repo.record_run_creation(owner_id=agent.home_paths.owner_id, conversation_task_id="live-root",
                                      run_id="live-root", role="scheduled", attempt_status="pending")
    repo.create_attempt(active["agent_run_id"], reuse_pending=True)
    result = reconcile_owner_runtime(agent, limit=10)
    row = next(item for item in result["items"] if item.get("task_run_id") == active["task_run_id"])
    assert row["decision"] == "protected" and "last_activity_at" in row
    assert result["reason_counts"][row["reason_code"]] >= 1
    if row["reason_code"] in result["reason_guidance"]:
        assert result["reason_guidance"][row["reason_code"]]


def test_dismiss_archives_without_executing_and_blocks_later_replay(tmp_path):
    agent = _agent(tmp_path)
    store = agent.conversation_store
    wake_id = _quarantined(store)
    assert store.wakes.attempts.quarantined_error_context(wake_id)["type"] == "RuntimeError"
    receipt = store.wakes.attempts.dismiss(wake_id, now=2_000_000.0, actor="local/main", reason="stale continuation")
    assert receipt == {"wake_signal_id": wake_id, "dismissed": True, "source": "record", "executed": False}
    assert store.wakes.attempts.quarantined_count() == 0
    assert store.wakes.pending_one(wake_id) is None
    assert store.wakes.attempts.replay_source(wake_id)[0] == WAKE_REPLAY_ARCHIVED
    dismissed = json.loads((store.storage.wake_quarantine_archive_dir / "dismissed" / f"{wake_id}.json").read_text())
    assert dismissed["actor"] == "local/main" and dismissed["reason"] == "stale continuation"
    assert store.wakes.attempts.dismiss(wake_id, now=2_000_001.0)["error_code"] == WAKE_REPLAY_ARCHIVED
    assert store.wakes.attempts.dismiss("../x", now=1.0)["error_code"] == "WAKE_SIGNAL_ID_INVALID"


def test_tool_wake_actions_need_foreground_admin_and_report_each_id(tmp_path):
    agent = _agent(tmp_path)
    store = agent.conversation_store
    first, second = _quarantined(store, "thread-a"), _quarantined(store, "thread-b")
    tool = RuntimeGovernanceTool(agent, is_admin=True)
    preview = _body(tool.execute({"action": "quarantined_wakes_preview", "limit": 10}))
    assert {row["wake_signal_id"] for row in preview["items"]} == {first, second}
    assert all(row["last_error"]["type"] == "RuntimeError" for row in preview["items"])
    denied = tool.execute({"action": "quarantined_wakes_dismiss", "admin_authorized": True, "wake_signal_ids": [first]})
    assert not denied.ok and store.wakes.attempts.quarantined_count() == 2
    _foreground(agent)
    unauthorized = tool.execute({"action": "quarantined_wakes_dismiss", "wake_signal_ids": [first]})
    assert not unauthorized.ok
    dismissed = _body(tool.execute({"action": "quarantined_wakes_dismiss", "admin_authorized": True,
                                    "wake_signal_ids": [first, "missing-wake"]}))
    assert [row.get("dismissed") for row in dismissed["results"]] == [True, False] and dismissed["applied"] == 1
    replayed = _body(tool.execute({"action": "quarantined_wakes_replay", "admin_authorized": True, "wake_signal_ids": [second, first]}))
    assert replayed["results"][0]["replayed"] is True and replayed["results"][1]["error_code"] == WAKE_REPLAY_ARCHIVED
    assert store.wakes.pending_one(second) is not None and store.wakes.attempts.quarantined_count() == 0


def test_cross_owner_reconcile_is_foreground_admin_only_and_task_runs_only(tmp_path):
    admin = _agent(tmp_path)
    group = _agent(tmp_path, group="oc_cross")
    created = _terminal_root(group, root="group-root")
    owner = group.home_paths.owner_id
    tool = RuntimeGovernanceTool(admin, is_admin=True)
    denied = tool.execute({"action": "reconcile_preview", "owner_id": owner})
    assert not denied.ok and denied.effect_outcome == "not_started"
    _foreground(admin)
    preview = _body(tool.execute({"action": "reconcile_preview", "owner_id": owner, "limit": 10}))
    assert preview["owner_id"] == owner and not preview["errors"], preview
    assert [row["task_run_id"] for row in preview["items"]] == [created["task_run_id"]]
    assert preview["next_cursor"] == "" and group.subagents.runtime_db.get_task_run(created["task_run_id"])["closed_at"] == 0
    applied = _body(tool.execute({"action": "reconcile_apply", "admin_authorized": True, "owner_id": owner, "limit": 10}))
    assert applied["repaired"] == 1, applied
    assert group.subagents.runtime_db.get_task_run(created["task_run_id"])["closed_at"] > 0
    roots = tool.effective_write_roots({"action": "reconcile_apply", "owner_id": owner}, None, None)
    assert roots == (str(group.home_paths.owner_home_dir),)
    for bad in ("../escape", "providers/feishu/groups/oc_missing", "local/main/extra"):
        refused = tool.execute({"action": "reconcile_preview", "owner_id": bad})
        assert not refused.ok and refused.error_code == "TOOL_INVALID_ARGUMENTS"
    with pytest.raises(ValueError, match="run_ids"):
        reconcile_owner_runtime(group, run_ids=("x",), task_runs_only=True)


def test_sandbox_hint_names_the_exact_tool_call(monkeypatch, capsys):
    monkeypatch.setenv(HOST_STATE_READ_ONLY_ENV, "1")
    monkeypatch.setattr("sys.argv", ["my-agent", "runtime-stale-attempts", "--settle"])
    with pytest.raises(SystemExit):
        with host_state_guard.host_state_read_only_guard():
            raise PermissionError(errno.EPERM, "denied", "/x/runtime.db")
    assert 'runtime_governance {"action":"reconcile_apply","admin_authorized":true}' in capsys.readouterr().err
    monkeypatch.setattr("sys.argv", ["my-agent", "unknown-command"])
    with pytest.raises(SystemExit):
        with host_state_guard.host_state_read_only_guard():
            raise PermissionError(errno.EROFS, "ro", "/x")
    assert "请改用对应的内置工具。" in capsys.readouterr().err


def test_reconcile_cursor_survives_the_model_facing_credential_mask(tmp_path):
    # Live 2026-11-17: the JSON cursor's "signature" field reached the model as [REDACTED] through
    # sanitize_credentials, so the second page failed with "cursor owner/过滤不匹配".
    from agent_py_agent.agent.tooling.mcp_client import sanitize_credentials

    agent = _agent(tmp_path)
    created = [_terminal_root(agent, root=f"masked-{index}") for index in range(3)]
    tool = RuntimeGovernanceTool(agent, is_admin=True)
    seen, cursor = [], ""
    for _ in range(5):
        outcome = tool.execute({"action": "reconcile_preview", "limit": 1, **({"cursor": cursor} if cursor else {})})
        body = json.loads(sanitize_credentials(outcome.output))
        assert outcome.ok and not body["errors"], body
        seen += [row["task_run_id"] for row in body["items"] if row.get("kind") == "task_run"]
        cursor = body["next_cursor"]
        assert "[REDACTED]" not in cursor
        if not cursor or decode_reconcile_cursor(cursor)["phase"] != "task_runs":
            break
    assert sorted(seen) == sorted(row["task_run_id"] for row in created)
    encoded = base64.urlsafe_b64encode(json.dumps(
        {"phase": "task_runs", "after": "tr_1", "owner": "local/main", "signature": _SIG}).encode()).decode()
    assert _exempt_cursor(encoded) == encoded


def test_reconcile_cursor_survives_the_host_output_redaction_between_pages(tmp_path):
    # R04: Full Access admin turns pass every tool result through redact_host_tool_outcome before the model
    # sees it; the next_cursor taken from that redacted page 1 must still open page 2.
    from agent_py_agent.agent.settings.secret_ref import HostCredentialSnapshot
    from agent_py_agent.agent.tooling.host_output_redaction import redact_host_tool_outcome

    agent = _agent(tmp_path)
    created = [_terminal_root(agent, root=f"host-masked-{index}") for index in range(3)]
    tool = RuntimeGovernanceTool(agent, is_admin=True)
    snapshot = HostCredentialSnapshot(secret_values=("unrelated-host-value",))
    seen, cursor, pages = [], "", 0
    for _ in range(5):
        outcome = tool.execute({"action": "reconcile_preview", "limit": 1, **({"cursor": cursor} if cursor else {})})
        safe = redact_host_tool_outcome(outcome, snapshot=snapshot, force_default=True)
        body = json.loads(safe.output)
        assert safe.ok and not body["errors"], body
        pages += 1
        seen += [row["task_run_id"] for row in body["items"] if row.get("kind") == "task_run"]
        cursor = body["next_cursor"]
        assert "<redacted>" not in cursor and "[REDACTED]" not in cursor
        if not cursor or decode_reconcile_cursor(cursor)["phase"] != "task_runs":
            break
    assert pages >= 2
    assert sorted(seen) == sorted(row["task_run_id"] for row in created)
