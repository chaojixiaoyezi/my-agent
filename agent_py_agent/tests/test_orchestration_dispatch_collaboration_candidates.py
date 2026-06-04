from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.orchestration.dispatch.collaboration_candidates import (
    collaboration_request_runner_candidates_report,
)
from agent_py_agent.agent.agent_core.orchestration.dispatch.params import DispatchContext
from agent_py_agent.agent.agent_core.orchestration.dispatch.runner_batches import (
    _merge_runner_candidates,
)
from agent_py_agent.agent.agent_core.orchestration.dispatch.runner_records import (
    collaboration_candidate_load_error_record,
)


def test_collaboration_candidate_report_keeps_good_candidate_and_reports_dirty_ledger() -> None:
    tasks = [
        _task("agent-a"),
        _task("agent-b"),
    ]
    agent = SimpleNamespace(collaboration_store=_CandidateStore())

    report = collaboration_request_runner_candidates_report(agent, tasks)

    assert [item.id for item in report.candidates] == ["agent-a"]
    assert len(report.load_errors) == 2
    assert report.load_errors[0]["run_id"] == "agent-a"
    assert report.load_errors[0]["load_errors"][0]["context"] == "collaboration.requests.read"
    assert report.load_errors[1]["run_id"] == "agent-b"
    assert report.load_errors[1]["context"] == "dispatch.collaboration_candidates.pending_requests"


def test_collaboration_candidate_load_error_record_is_model_visible() -> None:
    ctx = DispatchContext(
        cfg=None,
        normalized_workflow_mode="off",
        planner=False,
        runner_instruction="",
        max_runners=1,
        limit=20,
        reviewer="parent-dispatch",
        note="",
        take_over_by="",
        locked_files=None,
        router=None,
    )
    agent = SimpleNamespace(
        subagents=SimpleNamespace(make_dispatch_record=lambda *, params: params)
    )
    load_errors = [{"run_id": "agent-a", "context": "collaboration.requests.read"}]

    record = collaboration_candidate_load_error_record(agent, ctx, load_errors)

    assert record.step == "collaboration_candidates"
    assert record.action == "candidate_scan_load_error"
    assert record.collaboration_candidate_load_errors == load_errors
    assert "不是没有待响应协作请求" in record.message


def test_runner_candidate_merge_limit_zero_means_unbounded() -> None:
    candidates = [_task("agent-a"), _task("agent-b")]

    merged = _merge_runner_candidates([], candidates, limit=0)

    assert [item.id for item in merged] == ["agent-a", "agent-b"]


class _CandidateStore:
    def pending_requests_for_agent_report(self, **kwargs):
        if kwargs["agent_id"] == "agent-a":
            return (
                [{"request_id": "request-1"}],
                [{"context": "collaboration.requests.read"}],
            )
        raise OSError("pending request ledger unavailable")


def _task(run_id: str):
    return SimpleNamespace(
        id=run_id,
        agent_name="",
        role="",
        status="PLANNING",
        channel_status="HEALTHY",
        capability_requests=[],
        capability_gaps=[],
    )
