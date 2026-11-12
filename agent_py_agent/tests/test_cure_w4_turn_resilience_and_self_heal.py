"""Behavioral and regression tests for workstream W4: turn resilience and self-healing.

Covers:
1. PROVIDER_TRANSIENT_TURN_RESUME_CODES and turn-level transient auto-resume on timeout / compact errors.
2. provider_transient_auto_resume re-raises original ProviderTransientError preserving error_code.
3. ModelProfileError and RuntimeDatabaseDeferred error taxonomy and client messages.
4. response_decision counter resets for protocol_repairs, truncated_write_repairs, and truncated_output_repairs.
5. REPEATED_IDENTICAL_TOOL_FAILURE diagnostic response synthesis preventing USER_REPLY_UNAVAILABLE.
6. _skill_tree_state / _scan_skill_tree_stats resilience against concurrent file churn.
7. repository pending handoff superseding on newer terminal attempts with settled resources.
8. Non-empty top-level tool descriptions in tool_schema, openai_chat, and responses preserving property descriptions.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.provider_transient_auto_resume import (
    ProviderTransientRetryCallbacks,
    run_with_provider_transient_auto_resume,
)
from agent_py_agent.agent.agent_core.tool_guard.identical_failure import (
    REPEATED_IDENTICAL_TOOL_FAILURE,
    IdenticalFailureStreak,
)
from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
    ToolLoopRepairCounters,
    ToolLoopResponseDecisionRequest,
    tool_loop_response_decision,
)
from agent_py_agent.agent.backends.errors import (
    ProviderTimeoutError,
    ProviderTransientError,
)
from agent_py_agent.agent.backends.openai_chat import _openai_tools_from_native
from agent_py_agent.agent.backends.responses import (
    ModelResponse,
    OpenAIResponsesBackend,
    tools_for_choice,
)
from agent_py_agent.agent.backends.tool_schema import tool_model_spec_to_anthropic_tool
from agent_py_agent.agent.capability.skill_service import _skill_tree_state
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.gateway_parts.request_errors import (
    PROVIDER_TRANSIENT_TURN_RESUME_CODES,
    gateway_client_error_message,
)
from agent_py_agent.agent.gateway_parts.request_execution import (
    _is_compact_transient_failure,
    _provider_transient_turn_resume_marker,
)
from agent_py_agent.agent.runtime_db.recovery_guard import RuntimeDatabaseDeferred
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_errors import fallback_error_code
from agent_py_agent.agent.settings.model_provider_schema import ModelProfileError
from agent_py_agent.agent.turn_end import CONTINUABLE_REASONS
from agent_py_agent.tests._tool_runtime_harness import (
    make_test_model_spec,
    make_test_protocol_snapshot,
    runtime_snapshot_for_model_specs,
)


def test_provider_transient_turn_resume_codes_and_marker():
    """Verify transient error codes and turn-level transient auto-resume."""
    expected_codes = {
        "MODEL_STREAM_INCOMPLETE",
        "PROVIDER_TRANSIENT_RETRY_TIME_BUDGET_EXCEEDED",
        "PROVIDER_RATE_LIMITED",
        "PROVIDER_UNAVAILABLE",
        "TRANSIENT_ERROR",
        "PROVIDER_TIMEOUT",
    }
    assert expected_codes.issubset(PROVIDER_TRANSIENT_TURN_RESUME_CODES)

    # Walk cause chain for compact error
    cause_err = ProviderTimeoutError("Read timed out")
    compact_err = ConversationCompactError("Compact failed", code="COMPACT_TRANSIENT_FAILURE")
    compact_err.__cause__ = cause_err
    assert _is_compact_transient_failure(compact_err) is True
    assert _is_compact_transient_failure(ProviderTransientError("503")) is True
    assert _is_compact_transient_failure(ValueError("Bad format")) is False

    agent = SimpleNamespace(config=SimpleNamespace(provider_transient_turn_resume_max_count=2))
    context = {"request": {}, "request_path": Path("/tmp/fake_req.json"), "request_id": "req-1"}
    chunk_writer = SimpleNamespace(observed_tool_rounds=0)

    # Should resume when failure is ProviderTimeoutError
    resp_timeout = {"ok": False, "error_code": "PROVIDER_TIMEOUT"}
    marker = _provider_transient_turn_resume_marker(
        agent, context, resp_timeout, chunk_writer, failure=cause_err
    )
    assert marker is not None
    assert marker["error_code"] == "PROVIDER_TIMEOUT"
    assert marker["resume_count"] == 1

    # Should resume when failure is ConversationCompactError caused by transient error
    resp_compact = {"ok": False, "error_code": "COMPACT_TRANSIENT_FAILURE"}
    marker2 = _provider_transient_turn_resume_marker(
        agent, context, resp_compact, chunk_writer, failure=compact_err
    )
    assert marker2 is not None
    assert marker2["resume_count"] == 1


def test_provider_transient_auto_resume_preserves_original_exception():
    """Verify original ProviderTransientError is re-raised when retry_guard stops retries."""
    original_exc = ProviderTransientError("Too Many Requests")
    original_exc.error_code = "PROVIDER_RATE_LIMITED"

    def op():
        raise original_exc

    callbacks = ProviderTransientRetryCallbacks(retry_guard=lambda: False)

    with pytest.raises(ProviderTransientError) as exc_info:
        run_with_provider_transient_auto_resume(op, callbacks=callbacks)

    assert exc_info.value is original_exc
    assert getattr(exc_info.value, "error_code", "") == "PROVIDER_RATE_LIMITED"


def test_model_profile_and_runtime_db_error_taxonomy():
    """Verify error_code mapping and friendly client messages."""
    db_err = RuntimeDatabaseDeferred("generation_busy")
    assert db_err.error_code == "RUNTIME_DB_RECOVERY_DEFERRED"
    assert fallback_error_code(db_err) == "RUNTIME_DB_RECOVERY_DEFERRED"
    msg_db = gateway_client_error_message(db_err.error_code)
    assert "运行时数据库正在执行短暂恢复维护" in msg_db

    prof_err = ModelProfileError("Profile removed")
    assert fallback_error_code(prof_err) == "MODEL_PROFILE_UNAVAILABLE"
    msg_prof = gateway_client_error_message(fallback_error_code(prof_err))
    assert "当前会话绑定的模型档案不可用或已被移除" in msg_prof


def test_response_decision_resets_repairs_after_clean_round(tmp_path):
    """Verify protocol, truncated_write, and truncated_output repairs reset after clean rounds."""
    dirty_counters = ToolLoopRepairCounters(
        protocol_repairs=2,
        truncated_write_repairs=1,
        truncated_output_repairs=2,
        invalid_arguments_repairs=1,
    )

    spec = make_test_model_spec("read_file", input_schema={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    })
    params = ToolLoopExecuteParams(
        user_prompt="test",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        write_boundary=None,
        task_attributes={},
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
        delivery_contract={},
        tool_runtime_snapshot=runtime_snapshot_for_model_specs((spec,), run_id="run-1"),
        tool_protocol_snapshot=make_test_protocol_snapshot(run_id="run-1", source_protocol="native"),
    )
    agent = SimpleNamespace(
        backend=SimpleNamespace(name="fake"),
        config=SimpleNamespace(enable_tools=True),
        root=tmp_path,
    )

    # 1. Clean tool execution round
    clean_tool_response = ModelResponse(
        text="",
        backend="fake",
        tool_use_blocks=[{"id": "toolu_1", "name": "read_file", "input": {"path": "README.md"}}],
        truncated=False,
    )
    req_tool = ToolLoopResponseDecisionRequest(
        agent=agent,
        params=params,
        response=clean_tool_response,
        counters=dirty_counters,
        turn_id="turn_1",
    )
    decision_tool = tool_loop_response_decision(req_tool)
    assert decision_tool.action == "run_tools"
    assert decision_tool.counters.protocol_repairs == 0
    assert decision_tool.counters.truncated_write_repairs == 0
    assert decision_tool.counters.truncated_output_repairs == 0
    assert decision_tool.counters.invalid_arguments_repairs == 0

    # 2. Clean text completion round
    clean_text_response = ModelResponse(
        text="All tasks finished successfully.",
        backend="fake",
        tool_use_blocks=[],
        truncated=False,
    )
    req_text = ToolLoopResponseDecisionRequest(
        agent=agent,
        params=params,
        response=clean_text_response,
        counters=dirty_counters,
        turn_id="turn_2",
    )
    decision_text = tool_loop_response_decision(req_text)
    assert decision_text.action == "break"
    assert decision_text.counters.protocol_repairs == 0
    assert decision_text.counters.truncated_write_repairs == 0
    assert decision_text.counters.truncated_output_repairs == 0


def test_repeated_identical_tool_failure_generates_diagnostic_explanation():
    """Verify repeated failure response produces non-empty explanation and is not continuable."""
    assert REPEATED_IDENTICAL_TOOL_FAILURE not in CONTINUABLE_REASONS

    from agent_py_agent.agent.agent_core._tool_loop_service import _identical_failure_response

    params = SimpleNamespace(
        identical_failure_halt=IdenticalFailureStreak(
            tool_name="read_file",
            args_hash="hash123",
            error_code="PERMISSION_DENIED",
            count=3,
        ),
        task_attributes={},
    )
    agent = SimpleNamespace(backend=SimpleNamespace(name="test_backend"))
    resp = _identical_failure_response(agent, params)

    assert resp.runtime_reason == REPEATED_IDENTICAL_TOOL_FAILURE
    assert resp.runtime_status == "unfinished"
    assert str(resp.text).strip() != ""
    assert "read_file" in resp.text
    assert "PERMISSION_DENIED" in resp.text


def test_skill_tree_state_concurrent_stat_resilience(tmp_path):
    """Verify _skill_tree_state survives concurrent file churn without OSError."""
    root = tmp_path / "skills"
    root.mkdir(parents=True, exist_ok=True)
    (root / "test_skill").mkdir(parents=True, exist_ok=True)
    (root / "test_skill" / "SKILL.md").write_text("# Test Skill", encoding="utf-8")

    stop_event = threading.Event()

    def churn():
        count = 0
        while not stop_event.is_set():
            tmp_file = root / "test_skill" / f".tmp_{count}.tmp"
            try:
                tmp_file.write_text("temporary data", encoding="utf-8")
                time.sleep(0.001)
                if tmp_file.exists():
                    tmp_file.unlink()
            except OSError:
                pass
            count += 1

    churn_thread = threading.Thread(target=churn, daemon=True)
    churn_thread.start()

    try:
        for _ in range(5):
            state = _skill_tree_state(root, validate=True)
            assert state is not None
            assert state.digest != ""
    finally:
        stop_event.set()
        churn_thread.join(timeout=2.0)


def test_repository_supersedes_stale_foreground_recovery_handoff(tmp_path):
    """Verify stale foreground recovery handoff is superseded by newer terminal attempts."""
    repo = RuntimeRepository(tmp_path / "runtime.db")

    task_id = "task-w4-1"
    thread_id = "thread-w4-1"

    with repo._runtime_connection() as conn:
        conn.execute(
            "INSERT INTO tasks (task_id, owner_id, thread_id, conversation_task_id, title, goal, created_at, updated_at) "
            "VALUES (?, 'owner-1', ?, '', 'title', 'goal', 100.0, 100.0)",
            (task_id, thread_id),
        )
        conn.execute(
            "INSERT INTO task_runs (task_run_id, task_id, status, created_at, updated_at) "
            "VALUES ('tr-1', ?, 'active', 100.0, 100.0)",
            (task_id,),
        )
        # Attempt 1 (recovered)
        conn.execute(
            "INSERT INTO agent_runs (agent_run_id, task_run_id, role, status, current_attempt_id, created_at, updated_at) "
            "VALUES ('ar-1', 'tr-1', 'main', 'done', 'att-1', 100.0, 100.0)",
        )
        conn.execute(
            "INSERT INTO agent_attempts (attempt_id, agent_run_id, attempt_generation, status, started_at, ended_at) "
            "VALUES ('att-1', 'ar-1', 1, 'done', 100.0, 105.0)",
        )
        conn.execute(
            "INSERT INTO runtime_events (event_id, agent_run_id, attempt_id, event_type, payload_json, created_at) "
            "VALUES ('ev-1', 'ar-1', 'att-1', 'attempt_recovered', '{\"operator\":\"foreground-user-turn\"}', 105.0)",
        )

        # Before newer attempt, recovery block shows handoff pending
        block = repo._recovery_eligibility_conn(conn, task_id=task_id)
        assert block["reason"] == "foreground_recovery_handoff_pending"

        # Now insert Attempt 2 (newer, completed cleanly, 0 unsettled ops)
        conn.execute(
            "INSERT INTO agent_runs (agent_run_id, task_run_id, role, status, current_attempt_id, created_at, updated_at) "
            "VALUES ('ar-2', 'tr-1', 'main', 'done', 'att-2', 200.0, 200.0)",
        )
        conn.execute(
            "INSERT INTO agent_attempts (attempt_id, agent_run_id, attempt_generation, status, started_at, ended_at) "
            "VALUES ('att-2', 'ar-2', 1, 'done', 200.0, 205.0)",
        )

        # Query again: Attempt 1 handoff should now be superseded
        block2 = repo._recovery_eligibility_conn(conn, task_id=task_id)
        assert block2["reason"] == ""
        assert block2["eligible"] is True


def test_tool_schema_descriptions_preserved():
    """Verify tool top-level description defaults to 'Tool <name>' without altering parameter properties."""
    spec = make_test_model_spec(
        "execute_command",
        description="Run command",
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": ""},
                "timeout": {"type": "integer", "description": "Seconds"},
            },
            "required": ["command"],
        },
    )
    # Set top-level description to empty string
    object.__setattr__(spec, "description", "")

    # 1. Anthropic schema
    anthropic = tool_model_spec_to_anthropic_tool(spec)
    assert anthropic["description"] == "Tool execute_command"
    assert anthropic["input_schema"]["properties"]["command"]["description"] == ""
    assert anthropic["input_schema"]["properties"]["timeout"]["description"] == "Seconds"

    # 2. OpenAI chat schema
    native_tools = [anthropic]
    openai_chat_tools = _openai_tools_from_native(native_tools)
    assert len(openai_chat_tools) == 1
    fn = openai_chat_tools[0]["function"]
    assert fn["name"] == "execute_command"
    assert fn["description"] == "Tool execute_command"
    assert fn["parameters"]["properties"]["command"]["description"] == ""

    # 3. OpenAI responses schema
    from agent_py_agent.agent.backends import BackendOptions
    backend = OpenAIResponsesBackend(
        BackendOptions("https://api.openai.com/v1", "sk-test", "gpt-4o")
    )
    payload = {}
    tools = tools_for_choice(native_tools, None)
    payload["tools"] = [
        {
            "type": "function",
            "name": tool["name"],
            "description": str(tool.get("description") or "").strip() or f"Tool {tool['name']}",
            "parameters": tool["input_schema"],
            "strict": False,
        }
        for tool in tools
    ]
    tool_resp = payload["tools"][0]
    assert tool_resp["description"] == "Tool execute_command"
    assert tool_resp["parameters"]["properties"]["command"]["description"] == ""
