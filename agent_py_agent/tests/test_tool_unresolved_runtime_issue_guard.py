from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.tooling.runtime_contracts import ToolFailureFacts, ToolResult
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    canonical_history_result,
    make_test_protocol_snapshot,
    runtime_snapshot_for_model_specs,
)


def test_no_tool_final_redirects_unresolved_artifact_integrity_issue(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
        ToolLoopRepairCounters,
        ToolLoopResponseDecisionRequest,
        tool_loop_response_decision,
    )
    from agent_py_agent.agent.backends import ModelResponse

    params = _params(
        archive_tool_calls=[
            _artifact_integrity_archive_record(
                ok=False,
                artifact_ok=False,
                path="app.js",
                codes=["STATIC_SITE_MISSING_DOM_ID_HITS"],
            )
        ]
    )

    decision = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=_agent(tmp_path),
            params=params,
            response=ModelResponse(text="已经完成了。", backend="fake"),
            counters=ToolLoopRepairCounters(),
        )
    )

    assert decision.action == "continue"
    assert decision.calls == []
    assert decision.counters.unresolved_runtime_issue_redirects == 1
    assert any("unresolved-runtime-issues" in item for item in params.tool_context)


def test_runtime_status_response_breaks_before_repair_redirects(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
        ToolLoopRepairCounters,
        ToolLoopResponseDecisionRequest,
        tool_loop_response_decision,
    )
    from agent_py_agent.agent.backends import ModelResponse

    params = _params(
        archive_tool_calls=[
            _artifact_integrity_archive_record(
                ok=False,
                artifact_ok=False,
                path="app.js",
                codes=["STATIC_SITE_MISSING_DOM_ID_HITS"],
            )
        ]
    )
    response = ModelResponse(
        text="当前上下文已达到压缩条件。",
        backend="fake",
        runtime_status="context_overflow",
        runtime_reason="context_overflow",
        runtime_source="preflight",
    )

    decision = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=_agent(tmp_path),
            params=params,
            response=response,
            counters=ToolLoopRepairCounters(),
        )
    )

    assert decision.action == "break"
    assert decision.response is response
    assert decision.counters.unresolved_runtime_issue_redirects == 0
    assert params.tool_context == []


def test_unresolved_runtime_issue_repair_context_redirects_once_then_allows_report(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_guard import unresolved_runtime_issue as guard
    from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
        ToolLoopRepairCounters,
        ToolLoopResponseDecisionRequest,
        tool_loop_response_decision,
    )
    from agent_py_agent.agent.backends import ModelResponse

    params = _params(
        archive_tool_calls=[
            _artifact_integrity_archive_record(
                ok=False,
                artifact_ok=False,
                path="app.js",
                codes=["STATIC_SITE_MISSING_DOM_ID_HITS"],
            )
        ]
    )

    assert guard.unresolved_runtime_issue_context(params, redirects=99)
    decision = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=_agent(tmp_path),
            params=params,
            response=ModelResponse(text="已经完成了。", backend="fake"),
            counters=ToolLoopRepairCounters(unresolved_runtime_issue_redirects=3),
        )
    )

    assert decision.action == "break"
    assert decision.response.text == "已经完成了。"
    assert decision.counters.unresolved_runtime_issue_redirects == 3
    assert not any(
        "repair_revalidate_or_report_real_blocker" in item for item in params.tool_context
    )


def test_no_tool_final_allows_after_artifact_integrity_issue_is_cleared(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
        ToolLoopRepairCounters,
        ToolLoopResponseDecisionRequest,
        tool_loop_response_decision,
    )
    from agent_py_agent.agent.backends import ModelResponse

    params = _params(
        archive_tool_calls=[
            _artifact_integrity_archive_record(
                ok=False,
                artifact_ok=False,
                path="app.js",
                codes=["STATIC_SITE_MISSING_DOM_ID_HITS"],
            ),
            _artifact_integrity_archive_record(ok=True, artifact_ok=True, path="app.js", codes=[]),
        ]
    )
    response = ModelResponse(text="已经完成了。", backend="fake")

    decision = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=_agent(tmp_path),
            params=params,
            response=response,
            counters=ToolLoopRepairCounters(),
        )
    )

    assert decision.action == "break"
    assert decision.response is response
    assert params.tool_context == []


def test_read_result_with_path_does_not_clear_artifact_integrity_issue(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_guard import unresolved_runtime_issue as guard

    params = _params(
        archive_tool_calls=[
            _artifact_integrity_archive_record(
                ok=False,
                artifact_ok=False,
                path="app.js",
                codes=["STATIC_SITE_MISSING_DOM_ID_HITS"],
            ),
            {
                "tool": "read_file",
                "ok": True,
                "tool_result_envelope": {
                    "path": "app.js",
                    "target_path": "app.js",
                },
            },
        ]
    )

    assert guard.unresolved_runtime_issues(params)


def test_archive_record_keeps_artifact_integrity_failure_envelope(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_call_archive_record import archive_tool_call_record
    from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallRecordParams

    params = _params()
    call = canonical_history_call(
        "write_file",
        {"path": "app.js"},
        call_id="1-1",
        source_protocol="native",
        run_id=params.run_id,
        turn_id="run-1:round-1",
        attempt_id=params.request_id,
    )
    result = canonical_history_result(
        call,
        "site integrity failed",
        ok=False,
        error_code="ACCEPTANCE_FAILED",
        handler_details={
            "artifact_integrity": {
                "kind": "web_project",
                "path": "app.js",
                "ok": False,
                "blocker_codes": ["STATIC_SITE_MISSING_DOM_ID_HITS"],
                "issues": [{"code": "STATIC_SITE_MISSING_DOM_ID_HITS", "severity": "blocker"}],
            }
        },
    )

    record = archive_tool_call_record(
        _agent(tmp_path),
        ToolCallRecordParams(
            params=params,
            tool_rounds=1,
            idx=1,
            call=call,
            result=result,
        ),
    )

    assert record["ok"] is False
    assert record["error_code"] == "ACCEPTANCE_FAILED"
    assert record["tool_result_envelope"]["artifact_integrity"]["ok"] is False
    assert record["tool_result_envelope"]["artifact_integrity"]["blocker_codes"] == [
        "STATIC_SITE_MISSING_DOM_ID_HITS"
    ]


def test_archive_record_keeps_safe_unknown_operation_facts(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_call_archive_record import archive_tool_call_record
    from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallRecordParams

    params = _params()
    call = canonical_history_call(
        "send_message",
        {"message": "hello"},
        call_id="1-1",
        source_protocol="native",
        run_id=params.run_id,
        turn_id="run-1:round-1",
        attempt_id=params.request_id,
    )
    handler_details = {
        "tool_operation": {
            "schema_version": "tool_operation.v1",
            "operation_id": "tool_call:call-9",
            "status": "unknown",
            "action": "completion_persistence_failed",
            "replayed": False,
            "idempotency_scope": "business",
            "diagnostic": "must-not-enter-compact-envelope",
        },
        "reported_tool_result": {
            "ok": True,
            "error_code": "",
            "effect_outcome": "",
            "effect_source_ref": "provider://message/9",
            "output": "must-not-enter-compact-envelope",
        },
    }
    result = ToolResult.failed(
        call,
        "operation outcome unknown",
        error_code="TOOL_OPERATION_OUTCOME_UNKNOWN",
        failure_stage="persistence",
        facts=ToolFailureFacts(
            handler_executed=True,
            effect_outcome="unknown",
            effect_source_ref="provider://message/9",
            status="unknown",
            metadata={"handler_details": handler_details},
        ),
    )

    record = archive_tool_call_record(
        _agent(tmp_path),
        ToolCallRecordParams(
            params=params,
            tool_rounds=1,
            idx=1,
            call=call,
            result=result,
        ),
    )

    assert record["operation_id"] == "tool_call:call-9"
    assert record["tool_operation_status"] == "unknown"
    assert record["tool_operation_action"] == "completion_persistence_failed"
    assert record["effect_outcome"] == "unknown"
    assert record["effect_source_ref"] == "provider://message/9"
    compact = record["tool_result_envelope"]
    assert compact["tool_operation"]["status"] == "unknown"
    assert "diagnostic" not in compact["tool_operation"]
    assert compact["reported_tool_result"]["ok"] is True
    assert "output" not in compact["reported_tool_result"]


def _agent(root: Path):
    class _Tools:
        workspace_root = root

    return SimpleNamespace(
        backend=SimpleNamespace(name="fake"),
        config=SimpleNamespace(enable_tools=True),
        root=root,
        tools=_Tools(),
    )


def _params(*, archive_tool_calls: list[dict[str, object]] | None = None):
    from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams

    return ToolLoopExecuteParams(
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
        archive_tool_calls=list(archive_tool_calls or []),
        tool_protocol_snapshot=make_test_protocol_snapshot(
            run_id="run-1",
            source_protocol="native",
        ),
        tool_runtime_snapshot=runtime_snapshot_for_model_specs((), run_id="run-1"),
    )


def _artifact_integrity_archive_record(
    *,
    ok: bool,
    artifact_ok: bool,
    path: str,
    codes: list[str],
) -> dict[str, object]:
    return {
        "tool": "write_file",
        "ok": ok,
        "error_code": "" if ok else "ACCEPTANCE_FAILED",
        "tool_result_envelope": {
            "artifact_integrity": {
                "kind": "web_project",
                "path": path,
                "ok": artifact_ok,
                "blocker_codes": codes,
                "issues": [{"code": code, "severity": "blocker"} for code in codes],
            }
        },
    }
