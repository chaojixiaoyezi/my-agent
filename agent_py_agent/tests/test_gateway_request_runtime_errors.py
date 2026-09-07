from __future__ import annotations

"""Gateway request execution should surface bad request files as runtime errors."""

import json
from pathlib import Path

import pytest

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import request_execution
from agent_py_agent.agent.gateway_parts.io import gateway_response_path, read_json_file
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.gateway_parts.recovery import (
    recover_gateway_processing_requests_report,
    terminalize_gateway_request_file,
)
from agent_py_agent.agent.gateway_parts.request_execution import (
    _copy_final_lease_fields,
    _handle_gateway_request,
)
from agent_py_agent.agent.gateway_parts.request_worker import (
    _finish_claimed_gateway_request,
    _iter_pending_request_paths,
    _process_gateway_requests,
)
from agent_py_agent.agent.settings import AgentConfig


def _make_agent(tmp_path: Path) -> tuple[SimpleAgent, object]:
    cfg = AgentConfig(
        model_backend="echo",
        gateway_workspace="gateway",
        local_store_path="local_store/local.db",
        local_store_files_dir="local_store/files",
        local_store_events_path="local_store/events.jsonl",
    )
    agent = SimpleAgent(cfg, tmp_path)
    paths = gateway_paths(agent)
    for path in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses):
        path.mkdir(parents=True, exist_ok=True)
    return agent, paths


@pytest.mark.parametrize("reason", ["operation_outcome_uncertain", "identity_mismatch"])
def test_recovery_uncertainty_has_structured_safe_client_error(monkeypatch, reason):
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts.request_errors import (
        ConversationPersistenceError,
        gateway_client_error_message,
    )

    repo = SimpleNamespace(recover_recorded_active_turn_attempt=lambda **kwargs: {
        "status": "rejected", "reason": reason, "operation_id": "private-operation-id"
    })
    context = SimpleNamespace(
        request={}, request_id="request",
        agent=SimpleNamespace(subagents=SimpleNamespace(runtime_db=repo)),
    )
    monkeypatch.setattr(request_execution, "_gateway_request_is_active_turn_recovery", lambda *args: True)
    monkeypatch.setattr(request_execution, "_gateway_runtime_authority", lambda *args: {
        "task_id": "task", "run_id": "request"
    })
    with pytest.raises(ConversationPersistenceError) as caught:
        request_execution._recover_gateway_active_turn_authority(context, [])
    code = caught.value.error_code
    message = gateway_client_error_message(code)
    assert "private-operation-id" not in message
    assert "任务处理失败" not in message
    if reason == "operation_outcome_uncertain":
        assert code == "ACTIVE_TURN_OUTCOME_UNCERTAIN"
        assert "不要直接重做" in message
    else:
        assert code == "CONVERSATION_PERSISTENCE_UNAVAILABLE"
        assert "会话记录" in message


def test_terminal_and_provider_admission_share_exact_turn_winner(tmp_path: Path) -> None:
    agent, paths = _make_agent(tmp_path)
    request_id = "gw-terminal-wins-admission"
    attempt_id = "attempt-terminal-wins"
    request_path = paths.processing / f"{request_id}.json"
    request_path.write_text(
        json.dumps(
            {
                "id": request_id,
                "kind": "ask",
                "status": "processing",
                "turn_phase": "open",
                "execution_attempt_id": attempt_id,
            }
        ),
        encoding="utf-8",
    )
    dedupe_key = "thread/terminal-wins"
    entry = agent.conversation_store.append_guidance_once(
        {
            "target_type": "request",
            "target_id": request_id,
            "message": "终态先赢时不能触网",
            "metadata": {
                "dedupe_key": dedupe_key,
                "expected_turn_id": request_id,
            },
        },
        dedupe_key=dedupe_key,
    )
    assert agent.conversation_store.claim_guidance_once_for_turn(
        entry,
        expected_turn_id=request_id,
        attempt_id=attempt_id,
    )
    guard = request_execution._GatewayActiveTurnTransition(
        request_path,
        request_id,
        attempt_id,
    )

    terminalize_gateway_request_file(
        paths,
        request_path,
        paths.done,
        request_id,
        conversation_store=agent.conversation_store,
        terminal_response={"id": request_id, "ok": True, "status": "done"},
    )

    with pytest.raises(InterruptedError, match="closed"):
        guard(
            "submit",
            lambda: agent.conversation_store.mark_guidance_entries_submitted(
                request_id,
                [entry],
                attempt_id=attempt_id,
                provider_call_id="provider-after-terminal",
            ),
        )
    receipt = agent.conversation_store.guidance_once_receipt(dedupe_key)
    assert receipt is not None and receipt.status == "rejected"


def test_provider_admission_winner_stays_unknown_after_terminal(tmp_path: Path) -> None:
    agent, paths = _make_agent(tmp_path)
    request_id = "gw-provider-wins-admission"
    attempt_id = "attempt-provider-wins"
    request_path = paths.processing / f"{request_id}.json"
    request_path.write_text(
        json.dumps(
            {
                "id": request_id,
                "kind": "ask",
                "status": "processing",
                "turn_phase": "open",
                "execution_attempt_id": attempt_id,
            }
        ),
        encoding="utf-8",
    )
    dedupe_key = "thread/provider-wins"
    entry = agent.conversation_store.append_guidance_once(
        {
            "target_type": "request",
            "target_id": request_id,
            "message": "模型提交先赢时不能自动重排",
            "metadata": {
                "dedupe_key": dedupe_key,
                "expected_turn_id": request_id,
            },
        },
        dedupe_key=dedupe_key,
    )
    assert agent.conversation_store.claim_guidance_once_for_turn(
        entry,
        expected_turn_id=request_id,
        attempt_id=attempt_id,
    )
    guard = request_execution._GatewayActiveTurnTransition(
        request_path,
        request_id,
        attempt_id,
    )

    submitted = guard(
        "submit",
        lambda: agent.conversation_store.mark_guidance_entries_submitted(
            request_id,
            [entry],
            attempt_id=attempt_id,
            provider_call_id="provider-before-terminal",
        ),
    )
    terminalize_gateway_request_file(
        paths,
        request_path,
        paths.done,
        request_id,
        conversation_store=agent.conversation_store,
        terminal_response={"id": request_id, "ok": True, "status": "done"},
    )

    assert submitted == (entry.guidance_id,)
    receipt = agent.conversation_store.guidance_once_receipt(dedupe_key)
    assert receipt is not None and receipt.status == "submitted"


def test_missing_processing_never_creates_empty_terminal_authority(tmp_path: Path) -> None:
    _agent, paths = _make_agent(tmp_path)
    request_id = "gw-missing-processing"
    missing = paths.processing / f"{request_id}.json"

    with pytest.raises(FileNotFoundError, match="canonical terminal"):
        terminalize_gateway_request_file(
            paths,
            missing,
            paths.failed,
            request_id,
            terminal_response={"id": request_id, "ok": False, "status": "failed"},
        )

    assert not (paths.terminal / f"{request_id}.json").exists()


def test_handle_gateway_request_reports_bad_request_json(tmp_path: Path) -> None:
    agent, paths = _make_agent(tmp_path)
    request_path = paths.processing / "gw-bad-request.json"
    request_path.write_text("{bad json", encoding="utf-8")

    response = _handle_gateway_request(agent, request_path)

    assert response["ok"] is False
    assert response["error_code"] == "GATEWAY_REQUEST_LOAD_ERROR"
    assert response["request_load_error"]["context"] == "gateway.request_execution.request.read"
    assert "unsupported" not in response["error"].lower()


def test_invalid_client_workspace_fails_before_model_and_transcript(
    tmp_path: Path,
    monkeypatch,
) -> None:
    agent, paths = _make_agent(tmp_path)
    request_path = paths.processing / "gw-invalid-workspace.json"
    request_path.write_text(
        json.dumps(
            {
                "id": "gw-invalid-workspace",
                "kind": "ask",
                "prompt": "不得在 daemon cwd 兜底执行",
                "workspace": {
                    "cwd": str(tmp_path / "missing-project"),
                    "roots": [str(tmp_path)],
                },
                "conversation": {
                    "channel": "chat",
                    "channel_conversation_id": "invalid-workspace-session",
                    "channel_user_id": "local-agent",
                    "canonical_user_id": "local-agent",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        agent,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("invalid workspace must stop before model execution")
        ),
    )

    response = _handle_gateway_request(agent, request_path)

    assert response["ok"] is False
    assert response["error_code"] == "GATEWAY_WORKSPACE_INVALID"
    assert "当前工作目录不可用" in response["user_error"]
    assert agent.conversation_store.list_threads() == []


def test_handle_gateway_request_ignores_orphan_bad_response_projection(tmp_path: Path) -> None:
    agent, paths = _make_agent(tmp_path)
    request_id = "gw-bad-response"
    request_path = paths.processing / f"{request_id}.json"
    request_path.write_text(
        json.dumps({
            "id": request_id, "kind": "ask", "prompt": "should not rerun",
            "status": "processing", "turn_phase": "open", "execution_attempt_id": "claimed-attempt",
        }),
        encoding="utf-8",
    )
    gateway_response_path(paths, request_id).write_text("{bad json", encoding="utf-8")

    response = _handle_gateway_request(agent, request_path)

    assert response["ok"] is True
    assert "should not rerun" in response["response"]
    assert response.get("error_code") != "GATEWAY_RESPONSE_LOAD_ERROR"


def test_failed_request_preserves_structured_tool_progress_in_terminal_response(
    tmp_path: Path,
    monkeypatch,
) -> None:
    agent, paths = _make_agent(tmp_path)
    request_path = paths.processing / "gw-progress-then-fail.json"
    request_path.write_text(
        '{"id":"gw-progress-then-fail","kind":"ask","prompt":"work"}',
        encoding="utf-8",
    )

    def fail_after_progress(context):
        context.on_chunk.write_progress(
            {
                "round": 3,
                "call_index": 0,
                "tool": "run_command",
                "phase": "finished",
                "status": "completed",
            },
            "",
        )
        raise RuntimeError("provider failed after tools")

    monkeypatch.setattr(request_execution, "_run_gateway_ask", fail_after_progress)

    response = _handle_gateway_request(agent, request_path)

    assert response["ok"] is False
    assert response["status"] == "failed"
    assert response["tool_rounds"] == 3


def test_failed_request_archive_preserves_typed_terminal_cause(
    tmp_path: Path,
) -> None:
    _agent, paths = _make_agent(tmp_path)
    request_id = "gw-provider-timeout"
    processing_path = paths.processing / f"{request_id}.json"
    processing_path.write_text(
        '{"id":"gw-provider-timeout","kind":"ask","status":"processing"}',
        encoding="utf-8",
    )
    response = {
        "id": request_id,
        "ok": False,
        "status": "failed",
        "error_code": "MODEL_PROVIDER_TIMEOUT",
        "error": "ProviderTimeoutError: stream idle timeout",
        "ended_at": 123.0,
    }

    _finish_claimed_gateway_request(paths, processing_path, request_id, response)

    archived = read_json_file(paths.failed / f"{request_id}.json")
    assert archived["ok"] is False
    assert archived["error_code"] == "MODEL_PROVIDER_TIMEOUT"
    assert archived["error"] == "ProviderTimeoutError: stream idle timeout"


def test_cancelled_request_preserves_structured_tool_progress_in_terminal_response(
    tmp_path: Path,
    monkeypatch,
) -> None:
    agent, paths = _make_agent(tmp_path)
    request_path = paths.processing / "gw-progress-then-stop.json"
    request_path.write_text(
        '{"id":"gw-progress-then-stop","kind":"ask","prompt":"work"}',
        encoding="utf-8",
    )

    def stop_after_progress(_context, on_chunk):
        on_chunk.write_progress(
            {
                "round": 4,
                "call_index": 0,
                "tool": "run_command",
                "phase": "finished",
                "status": "completed",
            },
            "",
        )
        payload = read_json_file(request_path)
        payload["cancel_requested"] = True
        request_path.write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(
        request_execution,
        "_execute_gateway_request_body",
        stop_after_progress,
    )

    response = _handle_gateway_request(agent, request_path)

    assert response["ok"] is True
    assert response["status"] == "interrupted"
    assert response["error_code"] == "INTERRUPTED"
    assert response["tool_rounds"] == 4


def test_process_gateway_requests_preserves_bad_request_diagnostic(tmp_path: Path) -> None:
    agent, paths = _make_agent(tmp_path)
    request_path = paths.inbox / "gw-bad-inbox.json"
    request_path.write_text("{bad json", encoding="utf-8")

    assert _process_gateway_requests(agent, paths) == 1

    response = read_json_file(gateway_response_path(paths, "gw-bad-inbox"))
    assert response["error_code"] == "GATEWAY_REQUEST_LOAD_ERROR"
    assert response["request_load_error"]["context"] == "gateway.worker.request.read"
    assert (paths.failed / "gw-bad-inbox.json").exists()
    assert not request_path.exists()


def test_standalone_response_never_overrides_unreadable_processing(
    tmp_path: Path,
) -> None:
    agent, paths = _make_agent(tmp_path)
    request_id = "gw-untrusted-standalone-response"
    request_path = paths.inbox / f"{request_id}.json"
    request_path.write_text("{bad json", encoding="utf-8")
    gateway_response_path(paths, request_id).write_text(
        json.dumps(
            {
                "id": request_id,
                "ok": True,
                "status": "done",
                "response": "这份孤立结果不能成为权威",
            }
        ),
        encoding="utf-8",
    )

    assert _process_gateway_requests(agent, paths) == 1

    response = read_json_file(gateway_response_path(paths, request_id))
    canonical = read_json_file(paths.terminal / f"{request_id}.json")
    assert response["ok"] is False
    assert response["error_code"] == "GATEWAY_REQUEST_LOAD_ERROR"
    assert canonical["terminal_response"] == response


def test_claimed_request_identity_conflict_fails_without_provider(
    tmp_path: Path,
    monkeypatch,
) -> None:
    agent, paths = _make_agent(tmp_path)
    path_request_id = "gw-path-authority"
    conflicting_id = "gw-payload-conflict"
    request_path = paths.inbox / f"{path_request_id}.json"
    request_path.write_text(
        json.dumps(
            {
                "id": conflicting_id,
                "request_id": conflicting_id,
                "kind": "ask",
                "goal": "这条损坏请求不能进入模型",
            }
        ),
        encoding="utf-8",
    )

    def fail_if_provider_runs(*_args, **_kwargs):
        raise AssertionError("identity-conflicting request reached provider execution")

    monkeypatch.setattr(
        "agent_py_agent.agent.gateway_parts.request_worker._handle_gateway_request",
        fail_if_provider_runs,
    )

    assert _process_gateway_requests(agent, paths) == 1

    response = read_json_file(gateway_response_path(paths, path_request_id))
    assert response["id"] == path_request_id
    assert response["error_code"] == "GATEWAY_REQUEST_IDENTITY_CONFLICT"
    assert response["request_identity_error"]["payload_id"] == conflicting_id
    assert (paths.failed / f"{path_request_id}.json").exists()
    assert not gateway_response_path(paths, conflicting_id).exists()
    assert not (paths.terminal / f"{conflicting_id}.json").exists()


def test_pending_request_iteration_keeps_fresh_requests_ahead_of_recovery(tmp_path: Path) -> None:
    _agent, paths = _make_agent(tmp_path)
    (paths.inbox / "normal.json").write_text(
        '{"id": "normal", "priority": "interactive", "created_at": 1}',
        encoding="utf-8",
    )
    (paths.inbox / "recovery.json").write_text(
        '{"id": "recovery", "priority": "recovery", "created_at": 2}',
        encoding="utf-8",
    )

    assert [path.name for path in _iter_pending_request_paths(paths)] == ["normal.json", "recovery.json"]


def test_process_gateway_requests_skips_future_not_before_request(tmp_path: Path) -> None:
    agent, paths = _make_agent(tmp_path)
    request_path = paths.inbox / "future.json"
    request_path.write_text(
        '{"id": "future", "kind": "ask", "prompt": "later", "not_before_at": 99999999999}',
        encoding="utf-8",
    )

    assert _process_gateway_requests(agent, paths) == 0
    assert request_path.exists()
    assert not (paths.processing / "future.json").exists()


def test_recover_processing_request_reports_bad_request_json(tmp_path: Path) -> None:
    agent, paths = _make_agent(tmp_path)
    request_path = paths.processing / "gw-bad-processing.json"
    request_path.write_text("{bad json", encoding="utf-8")

    report = recover_gateway_processing_requests_report(paths, startup=True, agent=agent)

    assert report.summary["failed"] == 1
    assert report.load_errors[0]["context"] == "gateway.recovery.processing.read"
    response = read_json_file(gateway_response_path(paths, "gw-bad-processing"))
    assert response["error_code"] == "GATEWAY_REQUEST_LOAD_ERROR"
    assert response["request_load_error"]["context"] == "gateway.recovery.processing.read"
    assert (paths.failed / "gw-bad-processing.json").exists()


def test_copy_final_lease_fields_reports_bad_final_request_json(tmp_path: Path) -> None:
    request_path = tmp_path / "gw-final-bad.json"
    request_path.write_text("{bad json", encoding="utf-8")
    response = {"lease_owner": ""}

    _copy_final_lease_fields(response, request_path)

    assert response["final_request_load_error"]["context"] == "gateway.request_execution.final_request.read"


def test_finish_claimed_request_reports_bad_final_request_archive_json(tmp_path: Path) -> None:
    _agent, paths = _make_agent(tmp_path)
    request_id = "gw-final-archive-bad"
    processing_path = paths.processing / f"{request_id}.json"
    processing_path.write_text("{bad json", encoding="utf-8")
    response = {"id": request_id, "ok": True, "status": "done"}

    _finish_claimed_gateway_request(paths, processing_path, request_id, response)

    archived_response = read_json_file(gateway_response_path(paths, request_id))
    assert (
        archived_response["final_request_load_error"]["context"]
        == "gateway.terminalize.request.read"
    )


def test_stale_worker_result_never_materializes_missing_terminal(
    tmp_path: Path,
) -> None:
    _agent, paths = _make_agent(tmp_path)
    request_id = "gw-stale-worker-result"
    processing_path = paths.processing / f"{request_id}.json"
    processing_path.write_text(
        json.dumps(
            {
                "id": request_id,
                "kind": "ask",
                "status": "processing",
                "turn_phase": "open",
                "execution_attempt_id": "new-attempt",
                "lease_epoch": 2,
            }
        ),
        encoding="utf-8",
    )

    _finish_claimed_gateway_request(
        paths,
        processing_path,
        request_id,
        {"id": request_id, "ok": True, "status": "done", "response": "旧答复"},
        expected_execution_attempt_id="old-attempt",
        expected_lease_epoch=1,
    )

    current = read_json_file(processing_path)
    assert current["execution_attempt_id"] == "new-attempt"
    assert not (paths.terminal / f"{request_id}.json").exists()
    assert not gateway_response_path(paths, request_id).exists()


def test_claim_persists_server_request_fingerprint(tmp_path: Path) -> None:
    from agent_py_agent.agent.gateway_parts.io import write_json_file
    from agent_py_agent.agent.gateway_parts.queue_service import claim_request

    _agent, paths = _make_agent(tmp_path)
    request_id = "gw-fingerprint-claim"
    pending = paths.inbox / f"{request_id}.json"
    write_json_file(
        pending,
        {
            "id": request_id,
            "kind": "ask",
            "goal": "same id must preserve this exact request",
            "user_id": "u-1",
            "conversation": {"channel": "chat", "canonical_user_id": "u-1"},
        },
    )

    claim = claim_request(paths, pending)

    assert claim is not None
    claimed = read_json_file(claim.path)
    assert claimed["request_fingerprint_schema"] == "gateway_request_fingerprint.v1"
    assert len(claimed["request_fingerprint"]) == 64


# LLM: Build the real owner store and queue binding before a recovery-affine claim exists.
# 函数用途: 为断电边界测试准备真实请求和会话，测试本身不触网或伪造模型产物。
def _recoverable_claim_fixture(tmp_path):
    agent, paths = _make_agent(tmp_path)
    thread = agent.conversation_store.get_or_create_thread({
        "canonical_user_id": "owner", "channel": "chat",
        "channel_conversation_id": "session", "channel_user_id": "owner",
    })
    request_id = "request-recovery-claim"
    path = paths.processing / f"{request_id}.json"
    request = {
        "id": request_id, "kind": "ask", "prompt": "继续整理资料",
        "status": "processing", "execution_attempt_id": "transport-original",
    }
    path.write_text(json.dumps(request), encoding="utf-8")
    writer = request_execution._GatewayTaskBindingWriter(
        path, request_id, request, "transport-original",
    )
    writer.bind_conversation_claim(thread.thread_id)
    return agent, paths, thread.thread_id, request_id, path, writer


@pytest.mark.parametrize("status", ["done", "failed", "cancelled", "interrupted"])
@pytest.mark.parametrize("moment", ["acquired", "returned", "reclaimed"])
def test_terminal_commit_releases_exact_foreground_recovery_claim(tmp_path, monkeypatch, status, moment):
    from agent_py_agent.agent.conversation import store as store_module
    from agent_py_agent.agent.conversation.run_claim import (
        ConversationRunLaneRequest,
        conversation_run_lane,
    )

    agent, paths, thread_id, request_id, path, _writer = _recoverable_claim_fixture(tmp_path)
    store = agent.conversation_store
    claim_request = {
        "thread_id": thread_id, "task_id": f"gateway:{request_id}",
        "reason": "gateway_foreground_turn", "recover_same_task_only": True,
    }
    if moment == "returned":
        with conversation_run_lane(ConversationRunLaneRequest(
            store=store, thread_id=thread_id, claim_task_id=claim_request["task_id"],
            reason="gateway_foreground_turn", lease_seconds=90, heartbeat_interval_seconds=1,
            interrupt_check=lambda: False, recover_same_task_only=True,
        )) as claim:
            assert claim["task_id"] == claim_request["task_id"]
    else:
        claim = store.claim_background_run(claim_request)
    assert store.load_background_run_claim(thread_id)["status"] == "running"
    monkeypatch.setattr(store_module, "process_identity_is_live", lambda _: False)
    assert store.claim_background_run({
        "thread_id": thread_id, "task_id": request_id, "reason": "subagent_runner_finished",
    }) is None
    if moment == "reclaimed":
        replacement = store.claim_background_run(claim_request)
        assert replacement["claim_id"] != claim["claim_id"]
    target = paths.done if status == "done" else paths.failed
    terminalize_gateway_request_file(
        paths, path, target, request_id, conversation_store=store,
        terminal_response={"id": request_id, "status": status, "ok": status == "done"},
    )
    assert store.load_background_run_claim(thread_id)["status"] == (
        {"done": "finished", "interrupted": "cancelled"}.get(status, status)
    )
    assert not path.exists()
    later = store.claim_background_run({
        "thread_id": thread_id, "task_id": "gateway:later-request", "recover_same_task_only": True,
    })
    assert later is not None
    terminalize_gateway_request_file(
        paths, paths.terminal / path.name, target, request_id, conversation_store=store,
    )
    current = store.load_background_run_claim(thread_id)
    assert current["claim_id"] == later["claim_id"] and current["status"] == "running"


def test_recovery_claim_cleanup_failure_is_repaired_without_reexecuting(tmp_path, monkeypatch):
    agent, paths, thread_id, request_id, path, _writer = _recoverable_claim_fixture(tmp_path)
    store = agent.conversation_store
    store.claim_background_run({
        "thread_id": thread_id, "task_id": f"gateway:{request_id}", "recover_same_task_only": True,
    })
    finish = store.finish_background_run

    def fail_cleanup(_request):
        raise OSError("test claim cleanup unavailable")

    monkeypatch.setattr(store, "finish_background_run", fail_cleanup)
    with pytest.raises(OSError, match="cleanup unavailable"):
        terminalize_gateway_request_file(
            paths, path, paths.failed, request_id, conversation_store=store,
            terminal_response={"id": request_id, "status": "failed", "ok": False},
        )
    assert read_json_file(path)["schema_version"] == "gateway_terminal_request.v1"
    assert store.load_background_run_claim(thread_id)["status"] == "running"
    monkeypatch.setattr(store, "finish_background_run", finish)
    report = recover_gateway_processing_requests_report(paths, startup=True, agent=agent)
    assert report.summary["archived"] == 1, report
    assert not path.exists()
    assert store.load_background_run_claim(thread_id)["status"] == "failed"


@pytest.mark.parametrize("fault", ["write_failure", "thread_rebind", "cancelled"])
def test_recovery_claim_binding_fails_before_acquiring_execution(tmp_path, monkeypatch, fault):
    from agent_py_agent.agent.gateway_parts.request_errors import ConversationPersistenceError

    agent, _paths, thread_id, request_id, path, writer = _recoverable_claim_fixture(tmp_path)
    previous = read_json_file(path)
    if fault == "thread_rebind":
        with pytest.raises(ConversationPersistenceError):
            writer.bind_conversation_claim("another-thread")
    elif fault == "cancelled":
        path.write_text(json.dumps({**previous, "cancel_requested": True}), encoding="utf-8")
        with pytest.raises(InterruptedError):
            writer.bind_conversation_claim(thread_id)
    else:
        def fail_write(*_args, **_kwargs):
            raise OSError("test binding publication failed")

        monkeypatch.setattr(request_execution, "update_json_file_atomic", fail_write)
        context = request_execution._GatewayAskRunContext(
            agent, previous, path, path.with_suffix(".response"), request_id, None,
        )
        with pytest.raises(OSError, match="publication failed"):
            request_execution._gateway_conversation_execution_lane(context, thread_id)
    assert not agent.conversation_store.load_background_run_claim(thread_id)
    assert read_json_file(path)["conversation_claim"] == previous["conversation_claim"]


@pytest.mark.parametrize("fault", ["wrong_request", "wrong_thread", "missing_store"])
def test_terminal_claim_cleanup_does_not_release_another_lane(tmp_path, fault):
    agent, paths, thread_id, request_id, path, _writer = _recoverable_claim_fixture(tmp_path)
    store = agent.conversation_store
    original = store.claim_background_run({
        "thread_id": thread_id, "task_id": f"gateway:{request_id}", "recover_same_task_only": True,
    })
    data = read_json_file(path)
    if fault == "wrong_request":
        data["conversation_claim"]["request_id"] = "another-request"
    elif fault == "wrong_thread":
        data["conversation_claim"]["thread_id"] = "another-thread"
    path.write_text(json.dumps(data), encoding="utf-8")
    args = (paths, path, paths.failed, request_id)
    kwargs = {
        "conversation_store": None if fault == "missing_store" else store,
        "terminal_response": {"id": request_id, "status": "failed", "ok": False},
    }
    if fault == "wrong_thread":
        terminalize_gateway_request_file(*args, **kwargs)
    else:
        from agent_py_agent.agent.runtime_errors import DataCorruptionError

        expected_error = DataCorruptionError if fault == "wrong_request" else RuntimeError
        with pytest.raises(expected_error):
            terminalize_gateway_request_file(*args, **kwargs)
    current = store.load_background_run_claim(thread_id)
    assert current["claim_id"] == original["claim_id"] and current["status"] == "running"


def test_terminal_commit_between_claim_binding_and_acquisition_leaves_no_orphan(tmp_path):
    agent, paths, thread_id, request_id, path, _writer = _recoverable_claim_fixture(tmp_path)
    context = request_execution._GatewayAskRunContext(
        agent, read_json_file(path), path, paths.responses / path.name, request_id, None,
    )
    lane = request_execution._gateway_conversation_execution_lane(context, thread_id)
    terminalize_gateway_request_file(
        paths, path, paths.failed, request_id, conversation_store=agent.conversation_store,
        terminal_response={"id": request_id, "status": "interrupted", "ok": False},
    )
    with pytest.raises(InterruptedError):
        with lane:
            pytest.fail("已结束请求不能取得执行权")
    assert not agent.conversation_store.load_background_run_claim(thread_id)


def test_canonical_terminal_never_retires_different_hot_request(tmp_path: Path) -> None:
    agent, paths = _make_agent(tmp_path)
    request_id = "gw-terminal-hot-conflict"
    processing = paths.processing / f"{request_id}.json"
    original = {
        "id": request_id,
        "kind": "ask",
        "goal": "original request",
        "status": "processing",
    }
    processing.write_text(json.dumps(original), encoding="utf-8")
    terminalize_gateway_request_file(
        paths,
        processing,
        paths.done,
        request_id,
        terminal_response={
            "id": request_id,
            "ok": True,
            "status": "done",
            "response": "same-looking answer",
        },
    )
    changed = {
        **original,
        "goal": "different owner-visible request",
        "execution_attempt_id": "later-attempt",
        "lease_epoch": 2,
    }
    processing.write_text(json.dumps(changed), encoding="utf-8")

    report = recover_gateway_processing_requests_report(
        paths,
        startup=True,
        agent=agent,
    )

    assert report.summary["archived"] == 0
    assert report.load_errors
    assert processing.exists()
    canonical = read_json_file(paths.terminal / f"{request_id}.json")
    assert canonical["goal"] == "original request"
    assert canonical["terminal_response"]["response"] == "same-looking answer"


def test_terminalize_rejects_cross_request_path_and_id(tmp_path: Path) -> None:
    from agent_py_agent.agent.runtime_errors import DataCorruptionError

    _agent, paths = _make_agent(tmp_path)
    path_a = paths.processing / "request-a.json"
    path_a.write_text(
        json.dumps({"id": "request-a", "kind": "ask", "goal": "A"}),
        encoding="utf-8",
    )

    with pytest.raises(DataCorruptionError):
        terminalize_gateway_request_file(
            paths,
            path_a,
            paths.done,
            "request-b",
            terminal_response={
                "id": "request-b",
                "ok": True,
                "status": "done",
            },
        )

    assert path_a.exists()
    assert not (paths.terminal / "request-a.json").exists()
    assert not (paths.terminal / "request-b.json").exists()


def test_finish_claimed_request_archives_chunk_stream_with_request(tmp_path: Path) -> None:
    _agent, paths = _make_agent(tmp_path)
    request_id = "gw-chunk-archive"
    processing_path = paths.processing / f"{request_id}.json"
    processing_path.write_text(
        '{"id": "gw-chunk-archive", "kind": "ask", "prompt": "hello", "status": "processing"}',
        encoding="utf-8",
    )
    chunk_path = paths.processing / f"{request_id}.chunks.jsonl"
    chunk_path.write_text('{"t": 1, "text": "hello"}\n', encoding="utf-8")

    _finish_claimed_gateway_request(paths, processing_path, request_id, {"id": request_id, "ok": True, "status": "done"})

    archived_chunk_path = paths.done / f"{request_id}.chunks.jsonl"
    archived_response = read_json_file(gateway_response_path(paths, request_id))
    assert archived_chunk_path.exists()
    assert archived_response["chunk_stream_path"] == str(archived_chunk_path)
    assert not chunk_path.exists()


def test_finish_claimed_request_archives_authoritative_interrupted_status(tmp_path: Path) -> None:
    _agent, paths = _make_agent(tmp_path)
    request_id = "gw-interrupted-archive"
    processing_path = paths.processing / f"{request_id}.json"
    processing_path.write_text(
        '{"id": "gw-interrupted-archive", "kind": "ask", "status": "processing"}',
        encoding="utf-8",
    )

    _finish_claimed_gateway_request(
        paths,
        processing_path,
        request_id,
        {"id": request_id, "ok": True, "status": "interrupted", "error_code": "INTERRUPTED"},
    )

    archived_request = read_json_file(paths.done / f"{request_id}.json")
    assert archived_request["status"] == "interrupted"
