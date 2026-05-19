"""Focused tests for real-task recovery resume attempts."""

from __future__ import annotations

import json
from pathlib import Path


# LLM: resume attempts must be anchored by the machine recovery packet, not stdout prose.
# 函数用途: 验证真实任务续跑会把 recovery_packet 注入交付合同，并写入独立 attempt 日志。
def test_main_agent_real_task_resume_uses_recovery_packet_contract(tmp_path, monkeypatch):
    from agent_py_agent.agent.contracts import main_agent_real_task_execution as execution
    from agent_py_agent.agent.contracts.main_agent_real_task_execution import (
        MainAgentRealTaskExecutionRequest,
        run_main_agent_real_task_execution,
    )

    first_report = _timeout_report_with_recovery_packet(tmp_path, monkeypatch)
    first_case = first_report.cases[0]
    first_stdout = tmp_path / first_case.stdout_ref
    first_stdout.write_text("old failed stdout evidence", encoding="utf-8")
    monkeypatch.setattr(execution, "run_real_task_subprocess", _complete_run(tmp_path))

    resumed = run_main_agent_real_task_execution(
        MainAgentRealTaskExecutionRequest(
            workspace=tmp_path,
            max_workers=1,
            task_timeout_seconds=30,
            execute=True,
            recovery_packet_path=tmp_path / first_case.recovery_packet_ref,
        )
    )

    resumed_case = resumed.cases[0]
    assert resumed.ok is True
    assert resumed_case.status == "COMPLETED"
    assert "resumes/attempt-001/stdout.txt" in resumed_case.stdout_ref
    assert first_stdout.read_text(encoding="utf-8") == "old failed stdout evidence"


# LLM: failed resume attempts need their own recovery packet for chained continuation.
# 函数用途: 验证续跑再次失败时不会覆盖旧 recovery_packet，而是写到 attempt 目录。
def test_main_agent_real_task_failed_resume_writes_attempt_recovery_packet(tmp_path, monkeypatch):
    from agent_py_agent.agent.contracts import main_agent_real_task_execution as execution
    from agent_py_agent.agent.contracts.main_agent_real_task_execution import (
        MainAgentRealTaskExecutionRequest,
        run_main_agent_real_task_execution,
    )
    from agent_py_agent.agent.contracts.main_agent_real_task_subprocess import (
        RealTaskSubprocessResult,
    )

    first_report = _timeout_report_with_recovery_packet(tmp_path, monkeypatch)
    first_packet = tmp_path / first_report.cases[0].recovery_packet_ref
    original_packet_text = first_packet.read_text(encoding="utf-8")

    def _timeout_again(request):
        return RealTaskSubprocessResult(
            exit_code=124,
            duration_seconds=1.0,
            timed_out=True,
            timeout_reason="timeout",
        )

    monkeypatch.setattr(execution, "run_real_task_subprocess", _timeout_again)

    resumed = run_main_agent_real_task_execution(
        MainAgentRealTaskExecutionRequest(
            workspace=tmp_path,
            max_workers=1,
            task_timeout_seconds=30,
            execute=True,
            recovery_packet_path=first_packet,
        )
    )

    resumed_case = resumed.cases[0]
    assert resumed_case.status == "FAILED"
    assert "resumes/attempt-001/recovery_packet.json" in resumed_case.recovery_packet_ref
    assert first_packet.read_text(encoding="utf-8") == original_packet_text


# LLM: _timeout_report_with_recovery_packet creates a failed run fixture through public execution flow.
# 函数用途: 复用真实执行入口生成 recovery_packet，避免续跑测试手写内部包结构。
def _timeout_report_with_recovery_packet(tmp_path, monkeypatch):
    from agent_py_agent.agent.contracts import main_agent_real_task_execution as execution
    from agent_py_agent.agent.contracts.main_agent_real_task_execution import (
        MainAgentRealTaskExecutionRequest,
        run_main_agent_real_task_execution,
    )
    from agent_py_agent.agent.contracts.main_agent_real_task_subprocess import (
        RealTaskSubprocessResult,
    )

    def _timeout(request):
        return RealTaskSubprocessResult(
            exit_code=124,
            duration_seconds=1.0,
            timed_out=True,
            timeout_reason="timeout",
        )

    monkeypatch.setattr(execution, "run_real_task_subprocess", _timeout)
    return run_main_agent_real_task_execution(
        MainAgentRealTaskExecutionRequest(
            workspace=tmp_path,
            max_workers=1,
            task_timeout_seconds=1,
            execute=True,
            case_ids=("furniture_homepage_html",),
        )
    )


# LLM: _complete_run validates the generated resume command before producing a passing artifact.
# 函数用途: 返回假的 subprocess runner，确认交付合同包含 recovery 且没有内联旧 stdout。
def _complete_run(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_real_task_subprocess import (
        RealTaskSubprocessResult,
    )

    def _complete(request):
        _assert_resume_command(request.command)
        _assert_recovery_contract(request.command)
        _write_valid_furniture_artifact(tmp_path)
        return RealTaskSubprocessResult(exit_code=0, duration_seconds=1.0)

    return _complete


# LLM: _assert_resume_command checks path-level resume isolation.
# 函数用途: 确认续跑命令使用 attempt 目录，避免覆盖上一轮失败日志。
def _assert_resume_command(command: list[str]) -> None:
    argv = " ".join(command)
    assert "--delivery-contract-file" in command
    assert "resumes/attempt-001" in argv


# LLM: _assert_recovery_contract checks the machine facts passed to the resumed run.
# 函数用途: 确认 delivery_contract.recovery 来自恢复包字段，不包含旧 stdout 正文。
def _assert_recovery_contract(command: list[str]) -> None:
    contract_path = command[command.index("--delivery-contract-file") + 1]
    contract = json.loads(Path(contract_path).read_text(encoding="utf-8"))
    assert contract["recovery"]["schema_version"] == "main-agent-real-task-recovery.v1"
    assert contract["recovery"]["case_id"] == "furniture_homepage_html"
    assert contract["recovery"]["reason_codes"] == ["timeout", "artifact_acceptance_failed=1"]
    assert "old failed stdout evidence" not in json.dumps(contract, ensure_ascii=False)


# LLM: _write_valid_furniture_artifact creates a realistic artifact for the strict validator.
# 函数用途: 写完整、无外链且体量足够的 HTML，让续跑通过真实产物验收。
def _write_valid_furniture_artifact(tmp_path) -> None:
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
