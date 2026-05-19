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


# LLM: real-e2e should expose controlled execution and artifact acceptance separately.
# 函数用途: 验证 CLI 显式执行 echo 任务时，会返回日志 refs，并因缺少产物给出失败退出码。
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
    assert exit_code == 2
    assert execution["summary"]["failed"] == 1
    assert first_case["exit_code"] == 0
    assert first_case["acceptance_summary"]["failed"] == 1
    assert (tmp_path / "workspace" / first_case["stdout_ref"]).exists()


# LLM: real-e2e should revalidate stored execution reports without launching new subprocesses.
# 函数用途: 验证 CLI 可以只读复验已有真实任务执行报告，适合真实 API 任务结束后反复验收。
def test_cmd_real_e2e_revalidates_existing_real_task_report(tmp_path, capsys):
    from agent_py_agent.agent.contracts.main_agent_real_task_execution import (
        MainAgentRealTaskExecutionRequest,
        run_main_agent_real_task_execution,
    )
    from agent_py_agent.cli.real_e2e_commands import cmd_real_e2e

    workspace = tmp_path / "workspace"
    report = run_main_agent_real_task_execution(
        MainAgentRealTaskExecutionRequest(
            workspace=workspace,
            max_workers=1,
            task_timeout_seconds=30,
            execute=True,
            case_ids=("furniture_homepage_html",),
        )
    )
    artifact = (
        workspace
        / "main_agent_real_task_execution/tasks/furniture_homepage_html/workspace"
        / "outputs/furniture_homepage/index.html"
    )
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(_valid_furniture_homepage_html(), encoding="utf-8")
    args = argparse.Namespace(
        workspace=str(workspace),
        report="",
        json=True,
        include_real_model=False,
        artifact=[],
        real_task_suite=False,
        run_real_tasks=False,
        real_task_case=[],
        real_task_max_workers=1,
        real_task_timeout=30,
        real_task_base_config="",
        revalidate_real_task_report=str(workspace / report.report_ref),
    )

    exit_code = cmd_real_e2e(args)

    payload = json.loads(capsys.readouterr().out)
    revalidation = payload["main_agent_real_task_revalidation"]
    assert exit_code == 0
    assert revalidation["execution_mode"] == "revalidate"
    assert revalidation["summary"]["completed"] == 1


# LLM: real-e2e resume should launch execution from a recovery packet without requiring a fresh case prompt.
# 函数用途: 验证 CLI 把 --resume-real-task-recovery-packet 传入结构化执行请求。
def test_cmd_real_e2e_passes_recovery_packet_to_execution(tmp_path, capsys, monkeypatch):
    from agent_py_agent.cli import real_e2e_commands

    captured = {}
    packet = tmp_path / "recovery_packet.json"
    packet.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        real_e2e_commands,
        "run_main_agent_real_task_execution",
        _fake_execution(captured),
    )
    args = _resume_real_task_args(tmp_path, packet)

    exit_code = real_e2e_commands.cmd_real_e2e(args)

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["main_agent_real_task_execution"]["ok"] is True
    assert captured["request"].recovery_packet_path == packet
    assert captured["request"].execute is True


class _FakeExecutionReport:
    def to_dict(self):
        return {
            "ok": True,
            "schema_version": "main-agent-real-task-execution.v1",
            "execution_mode": "execute",
            "summary": {"total": 1, "completed": 1, "failed": 0, "planned": 0},
            "concurrency": {
                "case_count": 1,
                "effective_max_workers": 1,
                "requested_max_workers": 1,
            },
            "suite_report_ref": "",
            "report_ref": "",
            "cases": [],
        }


def _fake_execution(captured: dict):
    def _fake_execute(request):
        captured["request"] = request
        return _FakeExecutionReport()

    return _fake_execute


def _resume_real_task_args(tmp_path, packet):
    return argparse.Namespace(
        workspace=str(tmp_path / "workspace"),
        report="",
        json=True,
        include_real_model=False,
        artifact=[],
        real_task_suite=False,
        run_real_tasks=False,
        real_task_case=[],
        real_task_max_workers=1,
        real_task_timeout=30,
        real_task_base_config="",
        revalidate_real_task_report="",
        resume_real_task_recovery_packet=str(packet),
    )


# LLM: _valid_furniture_homepage_html mirrors the structured real-task artifact contract.
# 函数用途: 为 revalidation 测试生成完整、无外链、大小足够的 HTML，避免旧 tiny fixture 绕开真实验收合同。
def _valid_furniture_homepage_html() -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AUREL Home</title>
  <style>
    body {{ margin:0; font-family:Inter, system-ui, sans-serif; color:#191715; background:#f8f5ef; }}
    header {{ min-height:72vh; display:grid; place-items:end start; padding:48px; background:linear-gradient(135deg,#efe7dc,#c7b69f); }}
    nav {{ position:fixed; inset:0 0 auto 0; display:flex; justify-content:space-between; padding:20px 48px; background:rgba(248,245,239,.82); backdrop-filter:blur(14px); }}
    a {{ color:#191715; text-decoration:none; }}
    h1 {{ max-width:820px; font-size:76px; line-height:.92; margin:0 0 22px; letter-spacing:0; }}
    .lead {{ max-width:620px; font-size:20px; line-height:1.7; }}
    .actions {{ display:flex; gap:16px; margin-top:28px; }}
    .button {{ border:1px solid #191715; padding:14px 18px; }}
    main {{ padding:72px 48px; }}
    .collection {{ display:grid; grid-template-columns:minmax(0,1fr) 280px; gap:32px; padding:34px 0; border-bottom:1px solid #ded4c7; }}
    .collection-visual {{ min-height:180px; background:radial-gradient(circle at 40% 30%, #876f55, transparent 31%), linear-gradient(145deg,#d8ccb9,#9a846b); }}
    footer {{ padding:44px 48px; background:#191715; color:#f8f5ef; }}
  </style>
</head>
<body>
  <nav><strong>AUREL Home</strong><a href="#collections">Collections</a></nav>
  <header>
    <div>
      <h1>Modern furniture with hotel-level restraint.</h1>
      <p class="lead">A refined homepage for a premium furniture brand, built as a single self-contained HTML artifact with complete structure and no broken external references.</p>
      <div class="actions"><a class="button" href="#collections">View collections</a><a class="button" href="#studio">Book studio visit</a></div>
    </div>
  </header>
  <main id="collections">
    {_furniture_homepage_sections()}
    <section id="studio"><h2>Private studio appointments</h2><p>Design advisors prepare material boards, room plans, and delivery timing before each visit.</p></section>
  </main>
  <footer>© AUREL Home. Crafted for calm commercial interiors.</footer>
</body>
</html>
"""


# LLM: _furniture_homepage_sections keeps the reusable HTML fixture below strict size limits.
# 函数用途: 生成足够丰富的家具首页区块内容，让产物大小合同能真实覆盖复验路径。
def _furniture_homepage_sections() -> str:
    return "\n".join(_furniture_homepage_section(index) for index in range(1, 9))


# LLM: _furniture_homepage_section produces one self-contained section with no external refs.
# 函数用途: 构造单个家具系列区块；避免 fixture 引入外链图片或占位链接。
def _furniture_homepage_section(index: int) -> str:
    return f"""
        <section class="collection" id="collection-{index}">
          <div class="collection-copy">
            <p class="eyebrow">Collection {index}</p>
            <h2>Quiet luxury for lived-in rooms</h2>
            <p>Layered walnut, boucle, linen, and smoked glass create a calm residential showroom with practical navigation, editorial spacing, and polished commercial copy.</p>
          </div>
          <div class="collection-visual" aria-label="Abstract furniture vignette {index}"></div>
        </section>
        """
