"""Focused tests for the controlled main-agent real task suite."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


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
    assert payload["ok"] is True
    assert payload["summary"]["planned"] == payload["summary"]["total"]
    assert payload["concurrency"]["effective_max_workers"] == 2
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

    base_config = tmp_path / "base_config.yaml"
    base_config.write_text(
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
