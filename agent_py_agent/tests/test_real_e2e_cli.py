"""Focused tests for the real-e2e CLI command."""

from __future__ import annotations

import argparse
import json


# LLM: real-e2e should materialize a refs-first report without needing model calls in CI.
# 函数用途: 验证 CLI 能运行主代理基础测试矩阵、写报告文件，并在 JSON 模式输出机器可读摘要。
def test_cmd_real_e2e_writes_report_and_json_output(tmp_path, capsys):
    from agent_py_agent.cli.real_e2e_commands import cmd_real_e2e

    report_path = tmp_path / "report.json"
    args = argparse.Namespace(
        workspace=str(tmp_path / "workspace"),
        report=str(report_path),
        json=True,
        include_real_model=False,
        artifact=[],
    )

    exit_code = cmd_real_e2e(args)

    assert exit_code == 0
    saved = json.loads(report_path.read_text(encoding="utf-8"))
    printed = json.loads(capsys.readouterr().out)
    assert saved["ok"] is True
    assert saved["summary"]["total"] == 12
    assert printed["report_ref"] == str(report_path)
    assert printed["foundation"]["summary"]["failed"] == 0


# LLM: real-e2e should optionally validate produced artifacts through the same acceptance contract.
# 函数用途: 验证用户可以把真实模型生成的文件交给 real-e2e 命令做统一产物验收。
def test_cmd_real_e2e_includes_artifact_acceptance_findings(tmp_path, capsys):
    from agent_py_agent.cli.real_e2e_commands import cmd_real_e2e

    artifact = tmp_path / "index.html"
    artifact.write_text('<html><body><a href="#">Bad</a></body></html>', encoding="utf-8")
    args = argparse.Namespace(
        workspace=str(tmp_path / "workspace"),
        report="",
        json=True,
        include_real_model=False,
        artifact=[str(artifact)],
    )

    exit_code = cmd_real_e2e(args)

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["ok"] is False
    assert payload["artifact_acceptance"][0]["findings"][0]["code"] == "HTML_PLACEHOLDER_LINK"


# LLM: real-e2e real task suite must be a controlled plan, not an uncontrolled model launcher.
# 函数用途: 验证 CLI 可以生成主代理真实任务批量测试计划，报告只写引用和结构化合同。
def test_cmd_real_e2e_includes_main_agent_real_task_suite_plan(tmp_path, capsys):
    from agent_py_agent.cli.real_e2e_commands import cmd_real_e2e

    report_path = tmp_path / "report.json"
    args = argparse.Namespace(
        workspace=str(tmp_path / "workspace"),
        report=str(report_path),
        json=True,
        include_real_model=False,
        artifact=[],
        real_task_suite=True,
        run_real_tasks=False,
        real_task_case=[],
        real_task_max_workers=2,
        real_task_timeout=333,
    )

    exit_code = cmd_real_e2e(args)

    payload = json.loads(capsys.readouterr().out)
    saved = json.loads(report_path.read_text(encoding="utf-8"))
    suite = payload["main_agent_real_task_suite"]
    first_case = suite["cases"][0]
    assert exit_code == 0
    assert suite["summary"]["total"] >= 4
    assert suite["summary"]["planned"] == suite["summary"]["total"]
    assert first_case["worker_slot"] in {0, 1}
    assert first_case["timeout_seconds"] == 333
    assert "高端现代家具" not in json.dumps(payload, ensure_ascii=False)
    assert (tmp_path / "workspace" / first_case["prompt_ref"]).exists()
    assert saved["main_agent_real_task_suite"]["ok"] is True


# LLM: real-e2e should expose controlled execution separately from the planning contract.
# 函数用途: 验证 CLI 显式请求真实任务执行时，会返回执行报告和日志 refs。
def test_cmd_real_e2e_runs_controlled_echo_real_task(tmp_path, capsys):
    from agent_py_agent.cli.real_e2e_commands import cmd_real_e2e

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
    args = argparse.Namespace(
        workspace=str(tmp_path / "workspace"),
        report="",
        json=True,
        include_real_model=False,
        artifact=[],
        real_task_suite=True,
        run_real_tasks=True,
        real_task_case=["furniture_homepage_html"],
        real_task_max_workers=1,
        real_task_timeout=30,
        real_task_base_config=str(base_config),
    )

    exit_code = cmd_real_e2e(args)

    payload = json.loads(capsys.readouterr().out)
    execution = payload["main_agent_real_task_execution"]
    first_case = execution["cases"][0]
    assert exit_code == 0
    assert execution["summary"]["completed"] == 1
    assert first_case["exit_code"] == 0
    assert (tmp_path / "workspace" / first_case["stdout_ref"]).exists()
