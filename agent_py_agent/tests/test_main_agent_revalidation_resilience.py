from __future__ import annotations

import json
from pathlib import Path


# LLM: Corrupt stored reports must become structured failed reports, not raw JSONDecodeError crashes.
# 函数用途: 覆盖普通任务和真实任务 revalidate 的坏 execution_report.json 结构化失败路径。
def test_main_agent_revalidation_handles_corrupt_report_json(tmp_path: Path) -> None:
    from agent_py_agent.agent.contracts.main_agent_real_task_execution import (
        revalidate_main_agent_real_task_execution,
    )
    from agent_py_agent.agent.contracts.main_agent_task_execution import (
        revalidate_main_agent_task_execution,
    )

    task_report = _write_corrupt_report(tmp_path, "main_agent_task_execution")
    real_report = _write_corrupt_report(tmp_path, "main_agent_real_task_execution")

    task_payload = revalidate_main_agent_task_execution(task_report, workspace=tmp_path).to_dict()
    real_payload = revalidate_main_agent_real_task_execution(real_report, workspace=tmp_path).to_dict()

    _assert_invalid_report_payload(task_payload)
    _assert_invalid_report_payload(real_payload)
    assert _invalid_report_projection(task_payload) == _invalid_report_projection(real_payload)
    assert task_report.read_text(encoding="utf-8").lstrip().startswith("{")
    assert real_report.read_text(encoding="utf-8").lstrip().startswith("{")


# LLM: Legacy task execution reports must revalidate through the same structured refs on both tracks.
# 函数用途: 验证 task/real_task 复验都用 case_id、expected_artifacts_ref 和旧执行路径 fallback，不从 prompt 文本推断。
def test_task_and_real_task_revalidation_share_legacy_task_path_fallback(tmp_path: Path) -> None:
    from agent_py_agent.agent.contracts.main_agent_real_task_execution import (
        revalidate_main_agent_real_task_execution,
    )
    from agent_py_agent.agent.contracts.main_agent_task_execution import (
        revalidate_main_agent_task_execution,
    )

    task_payload = revalidate_main_agent_task_execution(
        _write_legacy_task_report(tmp_path),
        workspace=tmp_path,
    ).to_dict()
    real_payload = revalidate_main_agent_real_task_execution(
        _write_legacy_task_report(tmp_path),
        workspace=tmp_path,
    ).to_dict()

    assert _case_projection(task_payload) == _case_projection(real_payload)
    assert _case_projection(task_payload) == {
        "status": "DONE",
        "acceptance_report_ref": "main_agent_task_execution/tasks/legacy_case/acceptance_report.json",
        "acceptance_summary": {"failed": 0, "passed": 1, "total": 1},
        "issues": [],
    }
    assert _summary_projection(task_payload) == _summary_projection(real_payload)
    assert _summary_projection(task_payload) == {
        "completed": 1,
        "done": 1,
        "failed": 0,
        "planned": 0,
        "planning": 0,
        "total": 1,
    }


def _write_corrupt_report(root: Path, dirname: str) -> Path:
    report_path = root / dirname / "execution_report.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text("{bad json", encoding="utf-8")
    return report_path


def _write_legacy_task_report(root: Path) -> Path:
    case_id = "legacy_case"
    _write_expected_artifacts(root, case_id)
    workspace = root / "main_agent_task_execution" / "tasks" / case_id / "workspace"
    artifact = workspace / "outputs" / "result.txt"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("revalidated\n", encoding="utf-8")
    report_path = root / "main_agent_task_execution" / "execution_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            {
                "ok": True,
                "schema_version": "main-agent-task-execution.legacy",
                "execution_mode": "execute",
                "summary": {"total": 1, "done": 1, "failed": 0},
                "concurrency": {"requested_max_workers": 2, "effective_max_workers": 1, "case_count": 1},
                "suite_report_ref": "legacy_suite/report.json",
                "report_ref": "main_agent_task_execution/execution_report.json",
                "cases": [
                    {
                        "case_id": case_id,
                        "title": "Legacy case",
                        "status": "DONE",
                        "worker_slot": 0,
                        "timeout_seconds": 30,
                        "expected_artifacts_ref": f"legacy_suite/tasks/{case_id}/expected_artifacts.json",
                        "prompt_ref": "",
                        "config_ref": "",
                        "command_ref": "",
                        "stdout_ref": "",
                        "stderr_ref": "",
                        "acceptance_report_ref": "",
                        "events_ref": "",
                        "exit_code": 0,
                        "duration_seconds": 0.25,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return report_path


def _write_expected_artifacts(root: Path, case_id: str) -> Path:
    path = root / "legacy_suite" / "tasks" / case_id / "expected_artifacts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "artifacts": [
                    {
                        "artifact_id": "result_text",
                        "preferred_path": "outputs/result.txt",
                        "required": True,
                        "validation_contract": {"validator": "artifact_acceptance"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def _assert_invalid_report_payload(payload: dict[str, object]) -> None:
    cases = payload["cases"]
    assert payload["ok"] is False
    assert payload["summary"]["failed"] == 1
    assert isinstance(cases, list)
    assert cases[0]["case_id"] == "__report__"
    assert cases[0]["issues"][0] == "REVALIDATION_REPORT_INVALID_JSON"


def _invalid_report_projection(payload: dict[str, object]) -> dict[str, object]:
    case = payload["cases"][0]
    return {
        "ok": payload["ok"],
        "summary": payload["summary"],
        "case_id": case["case_id"],
        "status": case["status"],
        "acceptance_summary": case["acceptance_summary"],
        "issues": case["issues"],
    }


def _case_projection(payload: dict[str, object]) -> dict[str, object]:
    case = payload["cases"][0]
    return {
        "status": case["status"],
        "acceptance_report_ref": case["acceptance_report_ref"],
        "acceptance_summary": case["acceptance_summary"],
        "issues": case["issues"],
    }


def _summary_projection(payload: dict[str, object]) -> dict[str, object]:
    return dict(payload["summary"])
