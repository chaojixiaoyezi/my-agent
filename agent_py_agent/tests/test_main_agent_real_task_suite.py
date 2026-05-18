"""Focused tests for the controlled main-agent real task suite."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


# LLM: _write_echo_config keeps subprocess config setup shared across runner tests.
# 函数用途: 写最小 echo backend 配置，让测试关注执行合同而不是重复 YAML 内容。
def _write_echo_config(path: Path) -> Path:
    path.write_text(
        "\n".join(
            [
                'agent_name: "echo-test"',
                'model_backend: "echo"',
                'system_prompt: "你是测试用 echo agent。"',
                "prompt_files: []",
                "auto_save_memory: false",
                "enable_subagents: true",
            ]
        ),
        encoding="utf-8",
    )
    return path


# LLM: _event_types reads the per-case event ledger without exposing event payload details.
# 函数用途: 从 events.jsonl 提取事件类型，验证长任务观察账本顺序。
def _event_types(path: Path) -> list[str]:
    return [
        json.loads(line)["event_type"] for line in path.read_text(encoding="utf-8").splitlines()
    ]


# LLM: The real task suite should write task prompts and contracts as refs, not inline report bodies.
# 函数用途: 验证主代理真实任务套件以结构化文件描述任务，报告里只放引用、工位和验收摘要。
def test_main_agent_real_task_suite_plan_is_refs_first(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_real_task_suite import (
        MainAgentRealTaskSuiteRequest,
        plan_main_agent_real_task_suite,
    )

    report = plan_main_agent_real_task_suite(
        MainAgentRealTaskSuiteRequest(workspace=tmp_path, max_workers=2, task_timeout_seconds=444)
    )

    payload = report.to_dict()
    first_case = payload["cases"][0]
    prompt_path = tmp_path / first_case["prompt_ref"]
    acceptance_path = tmp_path / first_case["acceptance_ref"]
    artifacts_path = tmp_path / first_case["expected_artifacts_ref"]
    acceptance = json.loads(acceptance_path.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["summary"]["total"] >= 4
    assert payload["summary"]["planned"] == payload["summary"]["total"]
    assert prompt_path.exists()
    assert artifacts_path.exists()
    assert acceptance["schema_version"] == "main-agent-real-task-acceptance.v1"
    assert "required_artifact_ids" in acceptance
    assert "高端现代家具" not in json.dumps(payload, ensure_ascii=False)
    assert "高端现代家具" in prompt_path.read_text(encoding="utf-8")


# LLM: Worker slot and timeout limits are execution controls, so invalid values fail before any model call.
# 函数用途: 验证真实任务批量测试入口会拒绝无效并发和超时配置，避免无控制地启动任务。
def test_main_agent_real_task_suite_rejects_invalid_controls(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_real_task_suite import (
        MainAgentRealTaskSuiteRequest,
        plan_main_agent_real_task_suite,
    )

    with pytest.raises(ValueError, match="max_workers"):
        plan_main_agent_real_task_suite(
            MainAgentRealTaskSuiteRequest(workspace=tmp_path, max_workers=0)
        )
    with pytest.raises(ValueError, match="task_timeout_seconds"):
        plan_main_agent_real_task_suite(
            MainAgentRealTaskSuiteRequest(workspace=tmp_path, task_timeout_seconds=0)
        )


# LLM: Real-task execution planning should produce per-case commands and isolated config refs.
# 函数用途: 验证受控执行入口默认只落运行命令和隔离配置，不启动模型进程。
def test_main_agent_real_task_execution_plan_writes_command_refs(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_real_task_execution import (
        MainAgentRealTaskExecutionRequest,
        run_main_agent_real_task_execution,
    )

    report = run_main_agent_real_task_execution(
        MainAgentRealTaskExecutionRequest(
            workspace=tmp_path,
            max_workers=2,
            task_timeout_seconds=333,
            execute=False,
            package_root=Path.cwd(),
        )
    )

    payload = report.to_dict()
    first_case = payload["cases"][0]
    command_payload = json.loads((tmp_path / first_case["command_ref"]).read_text(encoding="utf-8"))
    prompt_arg = command_payload["argv"][-2]
    assert payload["ok"] is True
    assert payload["summary"]["planned"] == payload["summary"]["total"]
    assert payload["concurrency"]["effective_max_workers"] == 2
    assert "MACHINE_DELIVERY_CONTRACT_JSON" in prompt_arg
    assert "outputs/furniture_homepage/index.html" in prompt_arg
    assert "HTML_PLACEHOLDER_LINK" in prompt_arg
    assert "forbidden_hrefs" in prompt_arg
    assert (tmp_path / first_case["command_ref"]).exists()
    assert (tmp_path / first_case["config_ref"]).exists()
    assert not (tmp_path / first_case["stdout_ref"]).exists()


# LLM: The controlled runner should execute echo tasks but still fail missing artifacts.
# 函数用途: 用离线 echo 配置真实启动一次 `my-agent run`，验证日志落盘且产物缺失不会误判完成。
def test_main_agent_real_task_execution_runs_echo_subset(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_real_task_execution import (
        MainAgentRealTaskExecutionRequest,
        run_main_agent_real_task_execution,
    )

    base_config = _write_echo_config(tmp_path / "base_config.yaml")

    report = run_main_agent_real_task_execution(
        MainAgentRealTaskExecutionRequest(
            workspace=tmp_path,
            max_workers=1,
            task_timeout_seconds=30,
            execute=True,
            case_ids=("furniture_homepage_html",),
            base_config_path=base_config,
            package_root=Path.cwd(),
        )
    )

    payload = report.to_dict()
    first_case = payload["cases"][0]
    assert payload["ok"] is False
    assert payload["summary"]["failed"] == 1
    assert payload["concurrency"]["case_count"] == 1
    assert first_case["exit_code"] == 0
    assert first_case["acceptance_summary"]["failed"] == 1
    assert _event_types(tmp_path / first_case["events_ref"]) == [
        "case_prepared",
        "case_started",
        "case_finished",
        "case_acceptance_failed",
    ]
    assert (tmp_path / first_case["stdout_ref"]).exists()
    assert (tmp_path / first_case["stderr_ref"]).exists()


# LLM: Execution success must not hide missing required artifacts.
# 函数用途: 验证主代理进程退出码为 0 但没有产物时，真实任务执行报告仍然失败。
def test_main_agent_real_task_execution_fails_missing_expected_artifact(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_real_task_execution import (
        MainAgentRealTaskExecutionRequest,
        run_main_agent_real_task_execution,
    )

    report = run_main_agent_real_task_execution(
        MainAgentRealTaskExecutionRequest(
            workspace=tmp_path,
            max_workers=1,
            task_timeout_seconds=30,
            execute=True,
            case_ids=("furniture_homepage_html",),
            package_root=Path.cwd(),
        )
    )

    payload = report.to_dict()
    first_case = payload["cases"][0]
    acceptance_path = tmp_path / first_case["acceptance_report_ref"]
    acceptance = json.loads(acceptance_path.read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert first_case["status"] == "FAILED"
    assert first_case["acceptance_summary"]["failed"] == 1
    assert acceptance["artifacts"][0]["report"]["findings"][0]["code"] == "ARTIFACT_MISSING"


# LLM: Expected artifact validation should pass when the structured preferred path exists.
# 函数用途: 验证 runner 会按 expected_artifacts.json 的 preferred_path 验收真实文件。
def test_main_agent_real_task_execution_accepts_expected_artifact(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_real_task_execution import (
        MainAgentRealTaskExecutionRequest,
        run_main_agent_real_task_execution,
    )

    artifact = (
        tmp_path
        / "main_agent_real_task_execution/tasks/furniture_homepage_html/workspace"
        / "outputs/furniture_homepage/index.html"
    )
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        "<!doctype html><html><head><title>Maison</title></head>"
        '<body><a href="#story">Story</a><section id="story">Done</section></body></html>',
        encoding="utf-8",
    )

    report = run_main_agent_real_task_execution(
        MainAgentRealTaskExecutionRequest(
            workspace=tmp_path,
            max_workers=1,
            task_timeout_seconds=30,
            execute=True,
            case_ids=("furniture_homepage_html",),
            package_root=Path.cwd(),
        )
    )

    payload = report.to_dict()
    first_case = payload["cases"][0]
    assert payload["ok"] is True
    assert first_case["status"] == "COMPLETED"
    assert first_case["acceptance_summary"]["passed"] == 1


# LLM: Revalidation should re-check existing artifacts without rerunning model subprocesses.
# 函数用途: 验证已有 execution report 可以只读复验，适合真实 API 跑完后反复检查产物。
def test_main_agent_real_task_execution_revalidates_existing_report(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_real_task_execution import (
        MainAgentRealTaskExecutionRequest,
        revalidate_main_agent_real_task_execution,
        run_main_agent_real_task_execution,
    )

    report = run_main_agent_real_task_execution(
        MainAgentRealTaskExecutionRequest(
            workspace=tmp_path,
            max_workers=1,
            task_timeout_seconds=30,
            execute=True,
            case_ids=("furniture_homepage_html",),
            package_root=Path.cwd(),
        )
    )
    case = report.to_dict()["cases"][0]
    artifact = (
        tmp_path
        / "main_agent_real_task_execution/tasks/furniture_homepage_html/workspace"
        / "outputs/furniture_homepage/index.html"
    )
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        '<!doctype html><html><body><a href="/story">Story</a></body></html>',
        encoding="utf-8",
    )

    revalidated = revalidate_main_agent_real_task_execution(
        tmp_path / report.report_ref,
        workspace=tmp_path,
    )

    payload = revalidated.to_dict()
    first_case = payload["cases"][0]
    assert case["status"] == "FAILED"
    assert payload["ok"] is True
    assert first_case["status"] == "COMPLETED"
    assert first_case["acceptance_summary"]["passed"] == 1


# LLM: Revalidation should keep timeout diagnostics aligned with live execution.
# 函数用途: 验证旧报告 exit_code=124 但产物复验通过时，issues 不再显示普通失败码。
def test_main_agent_real_task_revalidation_marks_valid_timeout_artifact(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_real_task_execution import (
        MainAgentRealTaskExecutionRequest,
        revalidate_main_agent_real_task_execution,
        run_main_agent_real_task_execution,
    )

    report = run_main_agent_real_task_execution(
        MainAgentRealTaskExecutionRequest(
            workspace=tmp_path,
            max_workers=1,
            task_timeout_seconds=30,
            execute=True,
            case_ids=("furniture_homepage_html",),
            package_root=Path.cwd(),
        )
    )
    report_path = tmp_path / report.report_ref
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["cases"][0]["exit_code"] = 124
    report_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    artifact = (
        tmp_path
        / "main_agent_real_task_execution/tasks/furniture_homepage_html/workspace"
        / "outputs/furniture_homepage/index.html"
    )
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        '<!doctype html><html><body><a href="#story">Story</a><section id="story">Done</section></body></html>',
        encoding="utf-8",
    )

    revalidated = revalidate_main_agent_real_task_execution(report_path, workspace=tmp_path)

    first_case = revalidated.to_dict()["cases"][0]
    assert first_case["status"] == "COMPLETED"
    assert first_case["issues"] == ["process_timeout_after_valid_artifact"]


# LLM: Timeout should not hide a valid deliverable; the machine artifact contract is authoritative.
# 函数用途: 验证进程没及时退出但产物已通过验收时，真实任务报告按结构化产物合同判完成。
def test_main_agent_real_task_timeout_accepts_valid_artifact(tmp_path, monkeypatch):
    from agent_py_agent.agent.contracts.main_agent_real_task_execution import (
        MainAgentRealTaskExecutionRequest,
        run_main_agent_real_task_execution,
    )

    artifact = (
        tmp_path
        / "main_agent_real_task_execution/tasks/furniture_homepage_html/workspace"
        / "outputs/furniture_homepage/index.html"
    )
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        '<!doctype html><html><body><a href="#story">Story</a><section id="story">Done</section></body></html>',
        encoding="utf-8",
    )

    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=["my-agent", "run"],
            timeout=30,
            output=b"partial stdout",
            stderr=b"",
        )

    monkeypatch.setattr(subprocess, "run", _timeout)

    report = run_main_agent_real_task_execution(
        MainAgentRealTaskExecutionRequest(
            workspace=tmp_path,
            max_workers=1,
            task_timeout_seconds=30,
            execute=True,
            case_ids=("furniture_homepage_html",),
            package_root=Path.cwd(),
        )
    )

    payload = report.to_dict()
    first_case = payload["cases"][0]
    assert payload["ok"] is True
    assert first_case["status"] == "COMPLETED"
    assert first_case["exit_code"] == 124
    assert first_case["acceptance_summary"]["passed"] == 1
    assert first_case["issues"] == ["process_timeout_after_valid_artifact"]


# LLM: Timeout logs must preserve subprocess bytes as readable refs for debugging real model runs.
# 函数用途: 验证真实任务超时时 stdout/stderr 会解码为文本，不把 Python bytes 表示写进日志。
def test_main_agent_real_task_timeout_decodes_partial_output(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_real_task_execution import (
        _CaseRuntime,
        _timeout_case_result,
    )
    from agent_py_agent.agent.contracts.main_agent_real_task_execution_files import case_paths
    from agent_py_agent.agent.contracts.main_agent_real_task_execution_models import (
        MainAgentRealTaskExecutionRequest,
    )
    from agent_py_agent.agent.contracts.main_agent_real_task_suite import MainAgentRealTaskCasePlan

    paths = case_paths(tmp_path, "timeout_case")
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
    runtime = _CaseRuntime(
        case=MainAgentRealTaskCasePlan(
            case_id="timeout_case",
            title="Timeout Case",
            status="PLANNED",
            worker_slot=1,
            timeout_seconds=1,
            prompt_ref="prompt.md",
            acceptance_ref="acceptance.json",
            expected_artifacts_ref="artifacts.json",
        ),
        request=MainAgentRealTaskExecutionRequest(
            workspace=tmp_path,
            task_timeout_seconds=1,
        ),
        paths=paths,
        command=["my-agent", "run"],
        workspace=tmp_path,
    )
    exc = subprocess.TimeoutExpired(
        cmd=["my-agent", "run"],
        timeout=1,
        output="你好".encode(),
        stderr=b"partial error",
    )

    result = _timeout_case_result(runtime, exc, duration=1.25)

    assert result.exit_code == 124
    assert paths["stdout"].read_text(encoding="utf-8") == "你好"
    assert paths["stderr"].read_text(encoding="utf-8") == "partial error"
