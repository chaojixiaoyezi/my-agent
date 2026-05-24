from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


# LLM: A final answer must not bypass unresolved structured tool failures.
# 函数用途: 验证模型无工具调用收口前，会先检查 archive_tool_calls 里的未解决机器失败。
def test_no_tool_final_redirects_unresolved_artifact_integrity_issue(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_loop_repair_counters import ToolLoopRepairCounters
    from agent_py_agent.agent.agent_core.tool_loop_response_decision import (
        ToolLoopResponseDecisionRequest,
        tool_loop_response_decision,
    )
    from agent_py_agent.agent.backend import ModelResponse

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


# LLM: zero unresolved-runtime redirect max means unlimited repair contexts.
# 函数用途: 验证未解决运行问题 guard 的 0 次数预算不会让修复上下文提前消失。
def test_unresolved_runtime_issue_zero_redirect_limit_is_unlimited(tmp_path: Path, monkeypatch):
    from agent_py_agent.agent.agent_core import tool_unresolved_runtime_issue_guard as guard

    monkeypatch.setattr(guard, "_MAX_REDIRECTS", 0)
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


# LLM: A later successful integrity envelope for the same target clears the earlier failure.
# 函数用途: 验证 final gate 按结构化目标和后续成功记录消解问题，不靠最终自然语言自证。
def test_no_tool_final_allows_after_artifact_integrity_issue_is_cleared(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_loop_repair_counters import ToolLoopRepairCounters
    from agent_py_agent.agent.agent_core.tool_loop_response_decision import (
        ToolLoopResponseDecisionRequest,
        tool_loop_response_decision,
    )
    from agent_py_agent.agent.backend import ModelResponse

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


# LLM: Tool archive records must keep compact runtime failure envelopes for final gates and replay.
# 函数用途: 验证归档记录保存 error_code 与 artifact_integrity 小字段，避免最终 gate 看不见工具失败。
def test_archive_record_keeps_artifact_integrity_failure_envelope(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_call_archive_record import archive_tool_call_record
    from agent_py_agent.agent.agent_core.tool_round_execution import ToolCallRecordParams
    from agent_py_agent.agent.tools import ToolExecutionResult

    params = _params()
    result = ToolExecutionResult(
        "write_file",
        False,
        "site integrity failed",
        result_envelope={
            "artifact_integrity": {
                "kind": "web_project",
                "path": "app.js",
                "ok": False,
                "blocker_codes": ["STATIC_SITE_MISSING_DOM_ID_HITS"],
                "issues": [{"code": "STATIC_SITE_MISSING_DOM_ID_HITS", "severity": "blocker"}],
            }
        },
        error_code="ACCEPTANCE_FAILED",
    )

    record = archive_tool_call_record(
        _agent(tmp_path),
        ToolCallRecordParams(
            params=params,
            tool_rounds=1,
            idx=1,
            payload={"tool": "write_file", "path": "app.js"},
            result=result,
        ),
    )

    assert record["ok"] is False
    assert record["error_code"] == "ACCEPTANCE_FAILED"
    assert record["tool_result_envelope"]["artifact_integrity"]["ok"] is False
    assert record["tool_result_envelope"]["artifact_integrity"]["blocker_codes"] == [
        "STATIC_SITE_MISSING_DOM_ID_HITS"
    ]


def _agent(root: Path):
    class _Tools:
        workspace_root = root

        def parse_tool_calls(self, text: str):
            return []

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
        granted_capabilities=None,
        write_boundary=None,
        task_attributes={},
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=list(archive_tool_calls or []),
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
