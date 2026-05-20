from __future__ import annotations

import json
from pathlib import Path


# LLM: Controlled real-task execution should auto-resume once from recovery packets.
# 函数用途: 验证长任务被 guard 中断后，runner 会用同一 workspace 和结构化恢复包自动开 attempt 续跑。
def test_main_agent_task_execution_auto_resumes_once_from_recovery_packet(tmp_path, monkeypatch):
    from agent_py_agent.agent.contracts import main_agent_task_execution as execution
    from agent_py_agent.agent.contracts.main_agent_task_execution import (
        MainAgentTaskExecutionRequest,
        run_main_agent_task_execution,
    )
    from agent_py_agent.agent.contracts.main_agent_task_subprocess import (
        TaskRunSubprocessResult,
    )

    calls = {"count": 0}

    def _run(request):
        calls["count"] += 1
        if calls["count"] == 1:
            return TaskRunSubprocessResult(exit_code=0, duration_seconds=1.0)
        _assert_resume_contract(request.command)
        _write_valid_furniture_artifact(tmp_path)
        return TaskRunSubprocessResult(exit_code=0, duration_seconds=1.0)

    monkeypatch.setattr(execution, "run_task_subprocess", _run)

    report = run_main_agent_task_execution(
        MainAgentTaskExecutionRequest(
            workspace=tmp_path,
            max_workers=1,
            task_timeout_seconds=30,
            execute=True,
            case_ids=("furniture_homepage_html",),
        )
    )

    case = report.cases[0]
    assert calls["count"] == 2
    assert report.ok is True
    assert case.status == "DONE"
    assert "resumes/attempt-001/stdout.txt" in case.stdout_ref


def test_main_agent_task_execution_materializes_suite_contracts_before_acceptance(tmp_path, monkeypatch):
    from agent_py_agent.agent.contracts import main_agent_task_execution as execution
    from agent_py_agent.agent.contracts.main_agent_task_execution import (
        MainAgentTaskExecutionRequest,
        run_main_agent_task_execution,
    )
    from agent_py_agent.agent.contracts.main_agent_task_subprocess import (
        TaskRunSubprocessResult,
    )

    def _run(_request):
        _write_valid_furniture_artifact(tmp_path)
        return TaskRunSubprocessResult(exit_code=0, duration_seconds=1.0)

    monkeypatch.setattr(execution, "run_task_subprocess", _run)

    report = run_main_agent_task_execution(
        MainAgentTaskExecutionRequest(
            workspace=tmp_path,
            max_workers=1,
            task_timeout_seconds=30,
            execute=True,
            case_ids=("furniture_homepage_html",),
        )
    )

    case = report.cases[0]
    assert report.ok is True
    assert case.status == "DONE"
    expected_artifacts = tmp_path / "main_agent_task_suite/tasks/furniture_homepage_html/expected_artifacts.json"
    acceptance = tmp_path / "main_agent_task_suite/tasks/furniture_homepage_html/acceptance.json"
    assert expected_artifacts.exists()
    assert acceptance.exists()
    assert "expected_artifacts_contract" not in json.dumps(
        json.loads((tmp_path / case.acceptance_report_ref).read_text(encoding="utf-8")),
        ensure_ascii=False,
    )


def test_auto_resume_keeps_suite_workspace_when_first_run_contract_files_are_missing(tmp_path, monkeypatch):
    from agent_py_agent.agent.contracts import main_agent_task_execution as execution
    from agent_py_agent.agent.contracts.main_agent_task_execution import (
        MainAgentTaskExecutionRequest,
        run_main_agent_task_execution,
    )
    from agent_py_agent.agent.contracts.main_agent_task_subprocess import (
        TaskRunSubprocessResult,
    )

    calls = {"count": 0}

    def _run(request):
        calls["count"] += 1
        if calls["count"] == 1:
            artifacts = tmp_path / "main_agent_task_suite/tasks/furniture_homepage_html/expected_artifacts.json"
            artifacts.unlink()
            return TaskRunSubprocessResult(exit_code=0, duration_seconds=1.0)
        _assert_resume_command_still_uses_suite_workspace(request.command, tmp_path)
        _write_valid_furniture_artifact(tmp_path)
        return TaskRunSubprocessResult(exit_code=0, duration_seconds=1.0)

    monkeypatch.setattr(execution, "run_task_subprocess", _run)

    report = run_main_agent_task_execution(
        MainAgentTaskExecutionRequest(
            workspace=tmp_path,
            max_workers=1,
            task_timeout_seconds=30,
            execute=True,
            case_ids=("furniture_homepage_html",),
        )
    )

    assert calls["count"] == 2
    assert report.ok is True


def _assert_resume_contract(command: list[str]) -> None:
    contract_path = Path(command[command.index("--delivery-contract-file") + 1])
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    assert contract["recovery"]["schema_version"] == "main-agent-task-recovery.v1"
    assert contract["recovery"]["recommended_action"] == "repair_then_resume_same_case"


def _assert_resume_command_still_uses_suite_workspace(command: list[str], workspace: Path) -> None:
    assert any("高端现代家具品牌" in item for item in command)
    contract_path = Path(command[command.index("--delivery-contract-file") + 1])
    assert str(contract_path).startswith(str(workspace / "main_agent_task_execution"))
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    assert contract["expected_artifacts_ref"] == "main_agent_task_suite/tasks/furniture_homepage_html/expected_artifacts.json"
    assert (workspace / contract["expected_artifacts_ref"]).exists()


def _write_valid_furniture_artifact(tmp_path: Path) -> None:
    artifact = (
        tmp_path
        / "main_agent_task_execution/tasks/furniture_homepage_html/workspace"
        / "outputs/furniture_homepage/index.html"
    )
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(_valid_furniture_html(), encoding="utf-8")


def _valid_furniture_html() -> str:
    body = "\n".join(
        f"<section><h2>Collection {index}</h2><p>Premium furniture detail {index}</p></section>"
        for index in range(80)
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>AUREL Home</title></head>
<body><header><h1>High-end modern furniture</h1></header>{body}</body>
</html>"""
