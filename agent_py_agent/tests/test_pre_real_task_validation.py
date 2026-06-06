from __future__ import annotations

from pathlib import Path


def test_pre_real_task_validation_runs_phases_1_to_6(tmp_path: Path) -> None:
    from agent_py_agent.agent.contracts.pre_real_task_validation import (
        PreRealTaskValidationRequest,
        run_pre_real_task_validation,
    )

    report = run_pre_real_task_validation(PreRealTaskValidationRequest(workspace=tmp_path))

    assert report.ok is True
    assert report.summary == {"failed": 0, "passed": 6, "total": 6}
    assert [phase.phase_id for phase in report.phases] == [
        "small_real_runner_ready",
        "small_real_batch",
        "failure_sample_capture",
        "task_tree_small_scenario",
        "long_task_recovery_scenario",
        "medium_real_batch",
    ]
    assert (tmp_path / report.report_ref).is_file()
    assert "hello from real read_file wrapper" not in str(report.to_dict())


def test_small_real_acceptance_runner_executes_bounded_wrapper_cases(tmp_path: Path) -> None:
    from agent_py_agent.agent.contracts.small_real_acceptance_runner import (
        SmallRealAcceptanceRunRequest,
        run_small_real_acceptance,
    )

    report = run_small_real_acceptance(SmallRealAcceptanceRunRequest(workspace=tmp_path))

    assert report.ok is True
    assert report.summary == {"failed": 0, "passed": 2, "total": 2}
    assert {case.case_id for case in report.cases} == {
        "real_read_file",
        "controlled_exec_dry_run",
    }
    assert all(case.status == "PASSED" for case in report.cases)
    assert all(case.gate_case["real_execution_allowed"] is False for case in report.cases)
    assert all(case.verification_refs for case in report.cases)
    assert (tmp_path / report.report_ref).is_file()


def test_failure_sample_capture_converts_failed_case_to_replay_refs() -> None:
    from agent_py_agent.agent.contracts.failure_sample_capture import (
        failure_samples_from_case_results,
    )
    from agent_py_agent.agent.contracts.failure_sample_library_contract import (
        validate_failure_sample_library,
    )
    from agent_py_agent.agent.contracts.small_real_acceptance_runner import (
        SmallRealCaseResult,
    )

    samples = failure_samples_from_case_results(
        (
            SmallRealCaseResult(
                case_id="missing_artifact",
                status="FAILED",
                complexity="small",
                workspace_ref="workspace://isolated/missing_artifact",
                artifact_refs=[],
                verification_refs=["artifact://missing_artifact/verification.json"],
                tool_probe_refs=[],
                gate_case={"case_id": "missing_artifact"},
                issues=["ARTIFACT_MISSING"],
            ),
        )
    )

    result = validate_failure_sample_library(samples)

    assert result.ok is True
    assert samples[0]["expected_error_codes"] == ["ARTIFACT_MISSING"]


def test_task_tree_and_long_task_scenarios_materialize_refs(tmp_path: Path) -> None:
    from agent_py_agent.agent.contracts.long_task_recovery_scenario import (
        run_long_task_recovery_scenario,
    )
    from agent_py_agent.agent.contracts.task_tree_scenario import (
        run_task_tree_small_scenario,
    )

    tree = run_task_tree_small_scenario(tmp_path / "task-tree")
    recovery = run_long_task_recovery_scenario(tmp_path / "long-task")

    assert tree.ok is True
    assert recovery.ok is True
    assert (tmp_path / "task-tree" / tree.report_ref).is_file()
    assert (tmp_path / "long-task" / recovery.report_ref).is_file()
    assert tree.validation_error_codes == ()
    assert recovery.validation_error_codes == ()


def test_medium_real_acceptance_runner_uses_small_report_as_prerequisite(tmp_path: Path) -> None:
    from agent_py_agent.agent.contracts.medium_real_acceptance_runner import (
        MediumRealAcceptanceRunRequest,
        run_medium_real_acceptance,
    )
    from agent_py_agent.agent.contracts.small_real_acceptance_runner import (
        SmallRealAcceptanceRunRequest,
        run_small_real_acceptance,
    )

    small = run_small_real_acceptance(SmallRealAcceptanceRunRequest(workspace=tmp_path / "small"))
    medium = run_medium_real_acceptance(
        MediumRealAcceptanceRunRequest(workspace=tmp_path / "medium", small_report=small)
    )

    assert medium.ok is True
    assert medium.summary == {"failed": 0, "passed": 2, "total": 2}
    assert all(case.complexity == "medium" for case in medium.cases)
    assert all(case.gate_case["real_execution_allowed"] is False for case in medium.cases)
    assert (tmp_path / "medium" / medium.report_ref).is_file()
