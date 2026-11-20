"""M33 回归使用临时操作账本和本地 OAuth 响应。"""

from __future__ import annotations

import errno
import io
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from threading import Barrier
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from agent_py_agent.agent.common.json_io import read_json_object_report
from agent_py_agent.agent.common.request_execution_ports import RequestExecutionContextUnavailable
from agent_py_agent.agent.contracts.effect_settlement import (
    classify_effect_settlement,
    operation_status_for_settlement,
)
from agent_py_agent.agent.contracts.error_taxonomy import error_contract
from agent_py_agent.agent.contracts.recovery import RECOVERY_ACTIONS
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.conversation.input_dependencies import input_dependency_path
from agent_py_agent.agent.delivery import ChannelCapabilities
from agent_py_agent.agent.gateway_parts.feishu_oauth_service import oauth_callback_payload
from agent_py_agent.agent.gateway_parts.turn_recovery_control import execute_turn_recovery_control
from agent_py_agent.agent.runtime_db.managed_operation_store import ManagedOperationStore
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.tooling import tool_operation_coordinator as coordinator
from agent_py_agent.agent.tooling.feishu_auth_tool import (
    FeishuAuthTool,
    _classify_oauth_runtime_failure,
)
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.agent.user_space import feishu_oauth as oauth
from agent_py_agent.cli.chat_parts.control_runtime import _recover_command_text
from agent_py_agent.tests.ft.test_ft_lock_wait_gate import _ensure_open_file_limit
from agent_py_agent.tests.test_feishu_oauth import _api, _row
from agent_py_agent.tests.test_feishu_oauth import background as background
from agent_py_agent.tests.test_feishu_oauth import ctx as ctx


@pytest.fixture
def ledger(tmp_path):
    repo = RuntimeRepository(tmp_path / "runtime.db")
    run = repo.record_run_creation(
        owner_id="owner-m33", goal="test operation", conversation_task_id="task-m33",
        thread_id="thread-m33", run_id="run-m33", role="main",
    )
    store = ManagedOperationStore(repo)
    owner = SimpleNamespace(subagents=SimpleNamespace(runtime_db=repo))
    thread = SimpleNamespace(thread_id="thread-m33", workspace_task_id="")
    return repo, run, store, owner, thread


def _request(ledger, operation_id, invoke):
    _, run, store, _, _ = ledger
    return coordinator.ToolOperationExecutionRequest(
        store=store, store_required=True, owner_id="owner-m33", run_id="run-m33", task_id="task-m33",
        operation_id=operation_id, tool_name="fixture_write", args_hash=f"args-{operation_id}",
        idempotency_key=operation_id, idempotency_scope="operation", idempotency_namespace="test",
        timeout_seconds=60, invoke=invoke, attempt_id=run["attempt_id"],
    )


def _saved(repo, operation_id):
    with repo._runtime_connection() as conn:
        record = conn.execute(
            "SELECT status, outcome_json FROM tool_operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
    return record["status"], json.loads(record["outcome_json"])


def _recover(ledger, *, limit=None, offset=0):
    _, _, _, owner, thread = ledger
    text = "/recover" if limit is None else f"/recover --limit {limit} --offset {offset}"
    command = parse_conversation_control(text)
    assert command.valid
    return execute_turn_recovery_control(owner, thread, command)


def test_explicit_unknown_overrides_error_code():
    result = ToolHandlerOutcome(
        "fixture_write", False, "no durable receipt", error_code="TOOL_INVALID_ARGUMENTS",
        effect_outcome="unknown", handler_executed=True,
    )
    assert coordinator._operation_status_for_result(result) == coordinator.TOOL_OPERATION_UNKNOWN


def test_post_execution_exception_persists_unknown_in_recover(ledger, monkeypatch):
    repo, *_ = ledger
    attach = coordinator._attach_operation_facts
    invocations = []

    def fail_projection(*args, action, **options):
        if action == "executed":
            raise OSError("simulated receipt construction error")
        return attach(*args, action=action, **options)

    def invoke():
        invocations.append(True)
        return ToolHandlerOutcome("fixture_write", True, "completed", handler_executed=True)

    monkeypatch.setattr(coordinator, "_attach_operation_facts", fail_projection)
    sid = "fixture-post-call"
    result = coordinator.execute_tool_operation(_request(ledger, sid, invoke))
    status, saved = _saved(repo, sid)
    assert status == "UNKNOWN"
    assert saved["result"]["effect_outcome"] == result.effect_outcome == "unknown"
    assert saved["result"]["handler_executed"] is True
    assert saved["unknown_reason"] == "coordinator_post_execution_error:OSError"
    assert result.result_envelope["tool_operation"]["status"] == "unknown"
    assert _recover(ledger).details["unsettled_operation_ids"] == [sid]
    assert invocations == [True]


def test_authorize_failure_has_phase():
    with pytest.raises((ValueError, RuntimeError)) as error:
        oauth.request_oauth(SimpleNamespace())
    cls = getattr(oauth, "FeishuOAuthError", None)
    assert cls is not None and isinstance(error.value, cls)
    assert error.value.phase == "authorize"


def test_legacy_unknown_is_counted_and_paged_without_rewriting(ledger):
    repo, run, *_ = ledger
    now = time.time()
    legacy = "fixture-legacy-unknown"
    with repo.transaction() as conn:
        for index in range(62):
            status = "UNKNOWN" if index < 60 else "FAILED"
            sid = f"fixture-{index:02d}" if index != 60 else legacy
            effect = "failed" if index == 61 else "unknown"
            started = now - index
            conn.execute(
                "INSERT INTO tool_operations(operation_id, agent_run_id, attempt_id, attempt_generation, "
                "tool_operation_generation, operation_type, status, handler_started_at, settled_at, "
                "created_at, updated_at, outcome_json) VALUES(?,?,?,1,1,?,?,?,?,?,?,?)",
                (sid, run["agent_run_id"], run["attempt_id"], "fixture_write", status,
                 started, now if status == "FAILED" else 0, started, started,
                 json.dumps({"result": {"effect_outcome": effect}})),
            )
    summary = _recover(ledger)
    assert summary.details["unsettled_operation_count"] == 61
    first = _recover(ledger, limit=50)
    second = _recover(ledger, limit=50, offset=50)
    assert first.details["unsettled_operation_count"] == second.details["unsettled_operation_count"] == 61
    assert len(first.details["unsettled_operation_ids"]) == 50
    assert len(second.details["unsettled_operation_ids"]) == 11
    assert second.details["unsettled_operations"][-1] == {
        "operation_id": legacy, "operation_type": "fixture_write", "status": "UNKNOWN",
        "handler_started_at": now - 60, "legacy_normalized": True,
    }
    assert _saved(repo, legacy) == ("FAILED", {"result": {"effect_outcome": "unknown"}})


@pytest.mark.parametrize(("text", "valid", "page"), [
    ("/recover", True, None),
    ("/recover --limit 50 --offset 50", True, (50, 50)),
    ("/recover --limit 50", True, (50, 0)),
    ("/recover --limit 51", False, None),
    ("/recover --limit 50 --offset -1", False, None),
])
def test_recovery_page_round_trips_through_cli(text, valid, page):
    command = parse_conversation_control(text)
    assert command.valid is valid
    assert command.recover_operations_page == page
    if valid:
        restored = parse_conversation_control(_recover_command_text(command))
        assert restored == command


def test_known_nonzero_exit_is_failed_with_its_effect(ledger):
    repo, *_ = ledger

    def invoke():
        completed = subprocess.run([sys.executable, "-c", "raise SystemExit(9)"], check=False, timeout=10)
        assert completed.returncode == 9
        return ToolHandlerOutcome(
            "fixture_write", False, "completed with exit code 9", error_code="COMMAND_FAILED",
            handler_executed=True, effect_outcome="failed",
            result_envelope={"exit_code": completed.returncode},
        )

    sid = "fixture-command-failed"
    result = coordinator.execute_tool_operation(_request(ledger, sid, invoke))
    status, receipt = _saved(repo, sid)
    assert status == "FAILED" and receipt["result"]["effect_outcome"] == result.effect_outcome == "failed"
    assert _recover(ledger).details is None


@pytest.mark.parametrize(
    ("path", "grant", "expected"),
    [
        ("/authen/v2/oauth/token", "authorization_code", "token_exchange"),
        ("/authen/v2/oauth/token", "refresh_token", "refresh"),
        ("/authen/v1/user_info", None, "probe"),
    ],
)
def test_provider_error_keeps_phase_and_structured_code(monkeypatch, path, grant, expected):
    monkeypatch.setattr(
        oauth, "open_authenticated_request", lambda *_args, **_options: io.BytesIO(b'{"code":17}'),
    )
    body = {"grant_type": grant} if grant else None
    with pytest.raises((ValueError, RuntimeError)) as caught:
        oauth._oauth_api("POST" if body else "GET", path, body)
    error = caught.value
    assert isinstance(error, getattr(oauth, "FeishuOAuthError", ()))
    assert error.phase == expected and error.provider_code == "17"


def test_revoke_failure_has_phase(tmp_path, monkeypatch):
    owner = SimpleNamespace(provider="feishu", owner_kind="user")
    agent = SimpleNamespace(config=object(), home_paths=SimpleNamespace(owner_home_dir=tmp_path))
    monkeypatch.setattr(oauth, "owner_identity_from_config", lambda _cfg: owner)
    monkeypatch.setattr(oauth, "oauth_actor", lambda *_args: "fixture-owner")

    def fail_io(*_args):
        raise OSError("simulated store error")

    monkeypatch.setattr(oauth, "_credential_mutation", fail_io)
    with pytest.raises((OSError, RuntimeError)) as caught:
        oauth.oauth_status(agent, revoke=True)
    error = caught.value
    assert isinstance(error, getattr(oauth, "FeishuOAuthError", ()))
    assert error.phase == "revoke"


def test_unknown_oauth_failure_is_independent_of_exception_text():
    message = "本人授权已有待处理请求，请完成或撤销原授权。"
    _, effect, _, details = _classify_oauth_runtime_failure(None, "authorize", ValueError(message), {})
    assert effect == "unknown" and details["reason_code"] == "OAUTH_RUNTIME_ERROR"


@pytest.mark.skipif(getattr(sys, "_is_gil_enabled", lambda: True)(), reason="requires free-threaded Python")
def test_c128_operation_status(ledger):
    with _worker_fds():
        _check_concurrent_settlement(ledger)


@contextmanager
def _worker_fds():
    # SQLite 的并行连接各持有数据库、WAL 和共享内存文件。
    try:
        import resource
    except ImportError:
        yield
        return
    saved = resource.getrlimit(resource.RLIMIT_NOFILE)
    _ensure_open_file_limit()
    try:
        yield
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, saved)


def _check_concurrent_settlement(ledger):
    repo, *_ = ledger
    workers, operations_per_worker = 128, 50
    start = Barrier(workers)
    facts = [
        (True, "", ""),
        (False, "not_started", "TOOL_PERMISSION_DENIED"),
        (False, "failed", "COMMAND_FAILED"),
        (False, "unknown", "TOOL_INVALID_ARGUMENTS"),
        (False, "", "TOOL_RUNTIME_ERROR"),
    ]

    def worker(worker_id):
        start.wait(timeout=15)
        for index in range(operations_per_worker):
            ok, effect, code = facts[(worker_id + index) % len(facts)]
            sid = f"fixture-c128-{worker_id}-{index}"
            result = coordinator.execute_tool_operation(_request(ledger, sid, lambda: ToolHandlerOutcome(
                "fixture_write", ok, "test result", error_code=code,
                handler_executed=True, effect_outcome=effect,
            )))
            expected = operation_status_for_settlement(
                classify_effect_settlement(True, effect, "succeeded" if ok else "unknown"), ok=ok,
            )
            assert result.result_envelope["tool_operation"]["status"] == expected, sid

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(worker, range(workers)))
    with repo._runtime_connection() as conn:
        rows = conn.execute("SELECT operation_id, status, outcome_json FROM tool_operations").fetchall()
    assert len(rows) == workers * operations_per_worker
    for row in rows:
        receipt = json.loads(row["outcome_json"])["result"]
        status = row["status"].lower()
        assert status == operation_status_for_settlement(classify_effect_settlement(
            receipt["handler_executed"], receipt["effect_outcome"], status,
        ), ok=receipt["ok"]), row["operation_id"]
        if status == "failed":
            assert receipt["effect_outcome"] in {"not_started", "failed"}


_OAUTH_REASONS = (
    "EXECUTION_SCOPE_INACTIVE", "GROUP_SESSION_OAUTH_UNSUPPORTED", "OAUTH_CARD_DELIVERY_FAILED",
    "OAUTH_CONFIGURATION_UNAVAILABLE", "OAUTH_PENDING_REQUEST_EXISTS", "PERSONAL_IDENTITY_UNAVAILABLE",
)
_DELIVERED = {"message_id": "om_m33", "chat_id": "oc_private"}


@pytest.mark.parametrize("reason", _OAUTH_REASONS)
def test_typed_oauth_reason_resolves_to_its_own_contract(reason):
    contract = error_contract(reason)
    assert contract.code == reason and contract.category not in {"", "unknown"}
    assert contract.retryable is False and contract.recommended_action in RECOVERY_ACTIONS
    assert contract.recovery_hint


@pytest.mark.parametrize(("reason", "effect"), [
    ("EXECUTION_SCOPE_INACTIVE", "not_started"),
    ("GROUP_SESSION_OAUTH_UNSUPPORTED", "not_started"),
    ("OAUTH_CONFIGURATION_UNAVAILABLE", "not_started"),
    ("OAUTH_PENDING_REQUEST_EXISTS", "not_started"),
    ("PERSONAL_IDENTITY_UNAVAILABLE", "not_started"),
    ("OAUTH_CARD_DELIVERY_FAILED", "failed"),
])
def test_typed_oauth_reason_keeps_its_tool_outcome(reason, effect):
    error = oauth.FeishuOAuthError("deterministic error case", phase="authorize", reason_code=reason)
    _, settled, _, details = _classify_oauth_runtime_failure(None, "authorize", error, {})
    assert settled == effect and details["phase"] == "authorize"
    assert error_contract(details["reason_code"]).code == details["reason_code"]


def _authorize_operation(ctx, ledger, operation_id):
    tool = FeishuAuthTool(ctx.agent)
    request = _request(ledger, operation_id, lambda: tool.execute({"action": "authorize"}))
    return coordinator.execute_tool_operation(replace(request, tool_name="feishu_auth"))


def _card_adapter(ctx, reply):
    accepted = []

    def send(_target, card, **_options):
        accepted.append(card)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    ctx.agent.channel_registry.register_adapter(
        "feishu", SimpleNamespace(send_control_card=send),
        capabilities=ChannelCapabilities(control_cards=True, proactive=True),
    )
    return accepted


def _input_status(ctx, input_id):
    payload = read_json_object_report(input_dependency_path(ctx.agent, input_id), context="test.m33").payload
    return (payload or {}).get("status")


@pytest.mark.parametrize(("failure", "stage", "delivered", "fact"), [
    ("acknowledgement_lost", "delivery", "unknown", ("exception_type", "TimeoutError")),
    ("adapter_exception", "delivery", "unknown", ("error_code", "CHANNEL_CONTROL_IO_FAILED")),
    ("receipt_missing", "delivery", "unknown", ("error_code", "CHANNEL_CONTROL_RECEIPT_MISSING")),
    ("receipt_commit", "receipt_commit", "confirmed", ("exception_type", "OSError")),
])
def test_unacknowledged_card_stays_unknown_with_its_pending_input(
    ctx, ledger, monkeypatch, failure, stage, delivered, fact,
):
    repo, *_ = ledger
    replies = {"adapter_exception": TimeoutError("simulated I/O error"),
               "receipt_missing": {"message_id": "", "chat_id": ""}}
    accepted = _card_adapter(ctx, replies.get(failure, _DELIVERED))
    if failure == "acknowledgement_lost":
        deliver = ctx.agent.delivery_service.deliver_control_card

        def lose_acknowledgement(*args, **options):
            assert deliver(*args, **options).ok
            raise TimeoutError("simulated I/O error")

        monkeypatch.setattr(ctx.agent.delivery_service, "deliver_control_card", lose_acknowledgement)
    if failure == "receipt_commit":
        save = oauth._save_credential_row

        def fail_receipt_save(admission, target, row):
            if "im_control" in row:
                raise OSError(errno.EIO, "simulated I/O error")
            return save(admission, target, row)

        monkeypatch.setattr(oauth, "_save_credential_row", fail_receipt_save)
    sid = f"oauth-{failure}"
    result = _authorize_operation(ctx, ledger, sid)
    status, receipt = _saved(repo, sid)
    details = result.result_envelope["error_details"]
    row = _row(ctx)
    assert (result.ok, result.error_code, result.reported_error_code, result.effect_outcome) == (
        False, "TOOL_OPERATION_OUTCOME_UNKNOWN", "FEISHU_USER_AUTH_REQUIRED", "unknown")
    assert status == "UNKNOWN" and receipt["result"]["effect_outcome"] == "unknown"
    assert sid in _recover(ledger).details["unsettled_operation_ids"]
    assert details["reason_code"] == "OAUTH_CARD_DELIVERY_FAILED" and details["phase"] == "authorize"
    delivery = details["delivery"]
    assert (delivery["stage"], delivery["effect_outcome"], delivery[fact[0]]) == (stage, delivered, fact[1])
    assert delivery["input_id"] == row["input_id"] and len(accepted) == 1
    assert row["status"] == "pending" and row["ciphertext"] and "im_control" not in row
    assert _input_status(ctx, row["input_id"]) == "pending"

    retry = FeishuAuthTool(ctx.agent).execute({"action": "authorize"})
    assert retry.effect_outcome == "not_started" and len(accepted) == 1
    assert retry.result_envelope["error_details"]["reason_code"] == "OAUTH_PENDING_REQUEST_EXISTS"

    state = parse_qs(urlsplit(accepted[0]["elements"][1]["actions"][0]["url"]).query)["state"][0]
    _api(ctx, monkeypatch)
    completed = oauth.complete_oauth(ctx.agent, oauth_callback_payload(ctx.agent, state), state, "Synthetic-Code")
    assert completed == {"ok": True, "status": "ready"}
    assert _row(ctx)["status"] == "ready" and _input_status(ctx, row["input_id"]) == "ready"


@pytest.mark.parametrize("failure", ["provider_rejected", "input_registration"])
def test_confirmed_non_delivery_settles_failed_and_closes_the_request(ctx, ledger, monkeypatch, failure):
    repo, *_ = ledger
    rejected = {"error_code": "CHANNEL_CONTROL_PROVIDER_REJECTED", "provider_code": 230002}
    accepted = _card_adapter(ctx, rejected if failure == "provider_rejected" else _DELIVERED)
    if failure == "input_registration":
        def refuse_registration(*_args, **_options):
            raise ValueError("TASK_INPUT_CAPACITY_EXCEEDED")

        monkeypatch.setattr(oauth, "create_input_dependency", refuse_registration)
    sid = f"oauth-{failure}"
    result = _authorize_operation(ctx, ledger, sid)
    status, receipt = _saved(repo, sid)
    delivery = result.result_envelope["error_details"]["delivery"]
    row = _row(ctx)
    assert (result.error_code, result.effect_outcome, status) == ("FEISHU_USER_AUTH_REQUIRED", "failed", "FAILED")
    assert receipt["result"]["effect_outcome"] == "failed" and _recover(ledger).details is None
    assert delivery["effect_outcome"] == "not_started" and delivery["input_id"] == row["input_id"]
    assert row["status"] == "failed" and not row["ciphertext"]
    if failure == "provider_rejected":
        assert delivery["stage"] == "delivery" and len(accepted) == 1
        assert (delivery["error_code"], delivery["provider_code"]) == ("CHANNEL_CONTROL_PROVIDER_REJECTED", 230002)
        assert _input_status(ctx, row["input_id"]) == "failed"
    else:
        assert delivery["stage"] == "input_registration" and accepted == []
        assert _input_status(ctx, row["input_id"]) is None


def test_unsettled_host_error_from_card_delivery_propagates_unchanged(ctx, monkeypatch):
    raised = RequestExecutionContextUnavailable("simulated execution context loss")

    def lose_context(*_args, **_options):
        raise raised

    monkeypatch.setattr(ctx.agent.delivery_service, "deliver_control_card", lose_context)
    with pytest.raises(RequestExecutionContextUnavailable) as caught:
        FeishuAuthTool(ctx.agent).execute({"action": "authorize"})
    row = _row(ctx)
    assert caught.value is raised
    assert row["status"] == "pending" and _input_status(ctx, row["input_id"]) == "pending"
