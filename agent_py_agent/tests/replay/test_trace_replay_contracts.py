from __future__ import annotations

from pathlib import Path


def test_trace_replay_rejects_missing_artifact_success(tmp_path: Path):
    from agent_py_agent.tests.support.trace_replay import replay_contract_trace

    trace = Path(__file__).parent / "missing_artifact_success.jsonl"

    result = replay_contract_trace(trace, tmp_path)

    assert result.contract_result.ok is False
    assert "ARTIFACT_MISSING" in result.contract_result.error_codes
    assert "FINAL_STATUS_REJECTED" in result.contract_result.error_codes


def test_trace_replay_blocks_repeated_identical_tool_calls(tmp_path: Path):
    from agent_py_agent.tests.support.trace_replay import replay_contract_trace

    trace = Path(__file__).parent / "repeated_tool_should_block.jsonl"

    result = replay_contract_trace(trace, tmp_path)

    assert result.blocked is True
    assert result.block_reason == "TOOL_REPEATED_EXACT_FAILURE"


def test_trace_replay_rejects_missing_builder_call(tmp_path: Path):
    from agent_py_agent.tests.support.trace_replay import replay_contract_trace

    trace = Path(__file__).parent / "builder_not_called_success.jsonl"

    result = replay_contract_trace(trace, tmp_path)

    assert result.contract_result.ok is False
    assert "REQUIRED_SUCCESSFUL_TOOL_CALL_MISSING" in result.contract_result.error_codes
    assert "FINAL_STATUS_REJECTED" in result.contract_result.error_codes


def test_trace_replay_rejects_success_conflicting_with_blocked_state(tmp_path: Path):
    from agent_py_agent.tests.support.trace_replay import replay_contract_trace

    trace = Path(__file__).parent / "state_snapshot_conflicts_success.jsonl"
    (tmp_path / "output.md").write_text(
        "## Summary\nDone\n\n## Checked Files\n- input.txt\n",
        encoding="utf-8",
    )

    result = replay_contract_trace(trace, tmp_path)

    assert result.contract_result.ok is True
    assert "STATE_SNAPSHOT_FINAL_CONFLICT" in result.replay_error_codes
    assert result.state_snapshots[-1]["status"] == "BLOCKED"
    assert result.acceptance_reports[-1]["ok"] is False


def test_trace_replay_rejects_success_conflicting_with_runtime_issue(tmp_path: Path):
    from agent_py_agent.tests.support.trace_replay import replay_contract_trace

    trace = Path(__file__).parent / "runtime_issue_conflicts_success.jsonl"

    result = replay_contract_trace(trace, tmp_path)

    assert result.contract_result.ok is False
    assert "RUNTIME_ISSUE_SUCCESS_CONFLICT" in result.contract_result.error_codes
    assert "RUNTIME_ISSUE_FINAL_CONFLICT" in result.replay_error_codes
    assert {item["code"] for item in result.runtime_issues} == {"BOOTSTRAP_MATERIALIZATION_REQUIRED"}


def test_trace_replay_rejects_success_conflicting_with_closeout_snapshot(tmp_path: Path):
    from agent_py_agent.tests.support.trace_replay import replay_contract_trace

    trace = Path(__file__).parent / "closeout_snapshot_conflicts_success.jsonl"
    (tmp_path / "output.md").write_text(
        "Summary\nEnough content\nChecked Files\ninput.txt\n",
        encoding="utf-8",
    )

    result = replay_contract_trace(trace, tmp_path)

    assert result.contract_result.ok is True
    assert "CLOSEOUT_SNAPSHOT_FINAL_CONFLICT" in result.replay_error_codes
    assert result.closeout_snapshots[-1]["ok"] is False


def test_trace_replay_rejects_invalid_state_transition_sequence(tmp_path: Path):
    from agent_py_agent.tests.support.trace_replay import replay_contract_trace

    trace = Path(__file__).parent / "transition_conflicts_success.jsonl"
    (tmp_path / "output.md").write_text(
        "## Summary\nDone\n\n## Checked Files\n- input.txt\n",
        encoding="utf-8",
    )

    result = replay_contract_trace(trace, tmp_path)

    assert result.contract_result.ok is True
    assert "STATE_TRANSITION_SEQUENCE_CONFLICT" in result.replay_error_codes


def test_trace_replay_rejects_dry_run_claimed_as_real_success(tmp_path: Path):
    from agent_py_agent.tests.support.trace_replay import replay_contract_trace

    trace = Path(__file__).parent / "dry_run_claimed_success.jsonl"
    (tmp_path / "report.md").write_text("Summary\nDry-run plan only\n", encoding="utf-8")

    result = replay_contract_trace(trace, tmp_path)

    assert result.contract_result.ok is False
    assert "REQUIRED_REAL_TOOL_CALL_MISSING" in result.contract_result.error_codes
    assert "FINAL_STATUS_REJECTED" in result.contract_result.error_codes
