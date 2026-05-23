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


# LLM: Auto recovery should handle chained findings without user intervention.
# 函数用途: 验证首次续跑仍失败时，runner 会继续使用新的 recovery_packet 开下一次 attempt。
def test_main_agent_task_execution_auto_resumes_multiple_attempts(tmp_path, monkeypatch):
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
        if calls["count"] == 3:
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
    assert calls["count"] == 3
    assert report.ok is True
    assert case.status == "DONE"
    assert "resumes/attempt-002/stdout.txt" in case.stdout_ref


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


# LLM: Real-task execution should use the same bounded auto-resume contract as task execution.
# 函数用途: 验证旧 real_task 入口不会因为双轨实现缺失而在第一次可修复失败后直接结束。
def test_main_agent_real_task_execution_auto_resumes_from_recovery_packet(tmp_path, monkeypatch):
    from agent_py_agent.agent.contracts import main_agent_real_task_execution as execution
    from agent_py_agent.agent.contracts.main_agent_real_task_execution import (
        MainAgentRealTaskExecutionRequest,
        run_main_agent_real_task_execution,
    )
    from agent_py_agent.agent.contracts.main_agent_real_task_subprocess import (
        RealTaskSubprocessResult,
    )

    calls = {"count": 0}

    def _run(request):
        calls["count"] += 1
        if calls["count"] == 1:
            return RealTaskSubprocessResult(exit_code=0, duration_seconds=1.0)
        _assert_resume_contract(request.command)
        _write_valid_real_furniture_artifact(tmp_path)
        return RealTaskSubprocessResult(exit_code=0, duration_seconds=1.0)

    monkeypatch.setattr(execution, "run_real_task_subprocess", _run)

    report = run_main_agent_real_task_execution(
        MainAgentRealTaskExecutionRequest(
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


# LLM: Auto resume budget should be a request/runtime fact rather than a hidden path-only constant.
# 函数用途: 验证恢复上限可由结构化请求收窄，且超限后保留失败报告而不是无限续跑。
def test_main_agent_task_execution_honors_request_auto_resume_limit(tmp_path, monkeypatch):
    from agent_py_agent.agent.contracts import main_agent_task_execution as execution
    from agent_py_agent.agent.contracts.main_agent_task_execution import (
        MainAgentTaskExecutionRequest,
        run_main_agent_task_execution,
    )
    from agent_py_agent.agent.contracts.main_agent_task_subprocess import (
        TaskRunSubprocessResult,
    )

    calls = {"count": 0}

    def _run(_request):
        calls["count"] += 1
        return TaskRunSubprocessResult(exit_code=0, duration_seconds=1.0)

    monkeypatch.setattr(execution, "run_task_subprocess", _run)

    report = run_main_agent_task_execution(
        MainAgentTaskExecutionRequest(
            workspace=tmp_path,
            max_workers=1,
            task_timeout_seconds=30,
            execute=True,
            case_ids=("furniture_homepage_html",),
            max_auto_recovery_attempts=1,
        )
    )

    case = report.cases[0]
    ledger = json.loads(
        (
            tmp_path
            / "main_agent_task_execution/tasks/furniture_homepage_html/auto_recovery_ledger.json"
        ).read_text(encoding="utf-8")
    )
    assert calls["count"] == 2
    assert report.ok is False
    assert case.status == "FAILED"
    assert ledger["attempts"] == 1


# LLM: Auto resume should share the original case timeout budget across attempts.
# 函数用途: 防止一次真实任务因为自动恢复拿到多份完整 timeout，导致外层 case 生命周期失控。
def test_auto_resume_spends_remaining_timeout_budget(tmp_path, monkeypatch):
    from agent_py_agent.agent.contracts import main_agent_task_execution as execution
    from agent_py_agent.agent.contracts.main_agent_task_execution import (
        MainAgentTaskExecutionRequest,
        run_main_agent_task_execution,
    )
    from agent_py_agent.agent.contracts.main_agent_task_subprocess import (
        TaskRunSubprocessResult,
    )

    requested_timeouts: list[int] = []

    def _run(request):
        requested_timeouts.append(request.timeout_seconds)
        if len(requested_timeouts) == 1:
            return TaskRunSubprocessResult(exit_code=0, duration_seconds=26.0)
        return TaskRunSubprocessResult(exit_code=0, duration_seconds=4.0)

    monkeypatch.setattr(execution, "run_task_subprocess", _run)

    report = run_main_agent_task_execution(
        MainAgentTaskExecutionRequest(
            workspace=tmp_path,
            max_workers=1,
            task_timeout_seconds=30,
            execute=True,
            case_ids=("furniture_homepage_html",),
            max_auto_recovery_attempts=3,
        )
    )

    case = report.cases[0]
    assert requested_timeouts == [30, 4]
    assert report.ok is False
    assert case.status == "FAILED"
    assert case.timeout_seconds == 4


def _assert_resume_contract(command: list[str]) -> None:
    contract_path = Path(command[command.index("--delivery-contract-file") + 1])
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    assert contract["recovery"]["schema_version"] in {
        "main-agent-task-recovery.v1",
        "main-agent-real-task-recovery.v1",
    }
    assert contract["recovery"]["recommended_action"] == "repair_then_resume_same_case"
    marker = contract_path.parents[2] / "workspace/.agent_delivery/recovery_attempt.json"
    marker_payload = json.loads(marker.read_text(encoding="utf-8"))
    assert marker_payload["schema_version"] == "delivery-recovery-attempt.v1"
    assert marker_payload["packet_ref"].endswith("recovery_packet.json")


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


def _write_valid_real_furniture_artifact(tmp_path: Path) -> None:
    artifact = (
        tmp_path
        / "main_agent_real_task_execution/tasks/furniture_homepage_html/workspace"
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
