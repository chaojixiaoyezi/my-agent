"""Focused tests for the controlled main-agent real task suite."""

from __future__ import annotations

import json
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


# LLM: _valid_furniture_html produces a realistic artifact that satisfies the strict furniture contract.
# 函数用途: 给真实任务验收测试写完整、无外链、体量足够的 HTML，避免旧的几行 fixture 误代表合格产物。
def _valid_furniture_html() -> str:
    sections = "\n".join(
        f'<section id="collection-{idx}"><h2>Collection {idx}</h2><p>{"高级家具体验 " * 24}</p></section>'
        for idx in range(16)
    )
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<title>Maison Furniture</title><style>body{font-family:serif;color:#222}"
        "section{padding:32px;border-bottom:1px solid #ddd}</style></head><body>"
        '<header><nav><a href="#story">Story</a><a href="#collection-1">Collection</a></nav></header>'
        '<main><section id="story"><h1>Maison</h1><p>高端现代家具品牌首页。</p></section>'
        f"{sections}</main><footer id=\"contact\">Contact</footer></body></html>"
    )


# LLM: _patch_subprocess_timeout makes timeout-path tests share the same process failure.
# 函数用途: 把真实任务 subprocess runner 临时替换成固定超时，避免测试启动真实长进程。
def _patch_subprocess_timeout(monkeypatch) -> None:
    from agent_py_agent.agent.contracts import main_agent_real_task_execution as execution
    from agent_py_agent.agent.contracts.main_agent_real_task_subprocess import (
        RealTaskSubprocessResult,
    )

    def _timeout(request):
        request.stdout_path.write_text("partial stdout", encoding="utf-8")
        request.stderr_path.write_text("", encoding="utf-8")
        return RealTaskSubprocessResult(
            exit_code=124,
            duration_seconds=1.25,
            timed_out=True,
            timeout_reason="timeout",
        )

    monkeypatch.setattr(execution, "run_real_task_subprocess", _timeout)


# LLM: _write_open_file_write_manifest creates a structured open session fact for acceptance tests.
# 函数用途: 写一个未关闭的 file_write_session manifest，用来验证真实任务超时不会误判完成。
def _write_open_file_write_manifest(workspace: Path, target_display: str) -> None:
    session_manifest = workspace / ".agent_file_write_sessions/session-open/manifest.json"
    session_manifest.parent.mkdir(parents=True)
    session_manifest.write_text(
        json.dumps(
            {
                "session_id": "session-open",
                "status": "open",
                "target_path": {"display": target_display},
                "chunks": {"0": {"index": 0}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


# LLM: _timeout_runtime builds a minimal case runtime for timeout result helpers.
# 函数用途: 创建只测超时收口所需的 CaseRuntime，避免测试函数重复大段样板字段。
def _timeout_runtime(tmp_path: Path):
    from agent_py_agent.agent.contracts.main_agent_real_task_execution_files import case_paths
    from agent_py_agent.agent.contracts.main_agent_real_task_execution_models import (
        MainAgentRealTaskExecutionRequest,
    )
    from agent_py_agent.agent.contracts.main_agent_real_task_execution_state import CaseRuntime
    from agent_py_agent.agent.contracts.main_agent_real_task_suite import MainAgentRealTaskCasePlan

    paths = case_paths(tmp_path, "timeout_case")
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
    return CaseRuntime(
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
        request=MainAgentRealTaskExecutionRequest(workspace=tmp_path, task_timeout_seconds=1),
        paths=paths,
        command=["my-agent", "run"],
        workspace=tmp_path,
    )


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
    argv = command_payload["argv"]
    prompt_arg = argv[argv.index("run") + 1]
    contract_path = Path(argv[argv.index("--delivery-contract-file") + 1])
    contract_payload = json.loads(contract_path.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["summary"]["planned"] == payload["summary"]["total"]
    assert payload["concurrency"]["effective_max_workers"] == 2
    assert "MACHINE_DELIVERY_CONTRACT_JSON" not in prompt_arg
    assert contract_payload["artifacts"][0]["preferred_path"] == "outputs/furniture_homepage/index.html"
    assert "HTML_PLACEHOLDER_LINK" in json.dumps(contract_payload, ensure_ascii=False)
    assert "forbidden_hrefs" in contract_payload["artifacts"][0]["validation_contract"]
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
    artifact.write_text(_valid_furniture_html(), encoding="utf-8")

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
    artifact.write_text(_valid_furniture_html(), encoding="utf-8")

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
    artifact.write_text(_valid_furniture_html(), encoding="utf-8")

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
    artifact.write_text(_valid_furniture_html(), encoding="utf-8")

    _patch_subprocess_timeout(monkeypatch)

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


# LLM: Open write sessions are machine runtime facts, so valid artifacts alone cannot complete a timed-out run.
# 函数用途: 验证真实任务超时时若仍有未 finish 的分块写入会话，即便目标产物合格也不能误判完成。
def test_main_agent_real_task_timeout_blocks_open_file_write_session(tmp_path, monkeypatch):
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
    artifact.write_text(_valid_furniture_html(), encoding="utf-8")
    _write_open_file_write_manifest(
        tmp_path / "main_agent_real_task_execution/tasks/furniture_homepage_html/workspace",
        "outputs/furniture_homepage/app.js",
    )

    _patch_subprocess_timeout(monkeypatch)

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
    acceptance = json.loads((tmp_path / first_case["acceptance_report_ref"]).read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert first_case["status"] == "FAILED"
    assert first_case["issues"] == ["timeout", "artifact_acceptance_failed=1"]
    assert acceptance["runtime_findings"][0]["code"] == "OPEN_FILE_WRITE_SESSION"
    assert acceptance["runtime_findings"][0]["session_id"] == "session-open"


# LLM: Exit code zero with no tools and repetitive output is a runtime symptom, not success.
# 函数用途: 验证真实任务没有产物但模型返回空转复读时，执行报告给出结构化诊断 issue。
def test_main_agent_real_task_reports_no_progress_output(tmp_path, monkeypatch):
    from agent_py_agent.agent.contracts import main_agent_real_task_execution as execution
    from agent_py_agent.agent.contracts.main_agent_real_task_execution import (
        MainAgentRealTaskExecutionRequest,
        run_main_agent_real_task_execution,
    )
    from agent_py_agent.agent.contracts.main_agent_real_task_subprocess import (
        RealTaskSubprocessResult,
    )

    def _no_progress(request):
        repeated = "\n".join(["Wait now."] * 40)
        request.stdout_path.write_text(
            f"{repeated}\n[backend=echo; tool_rounds=0; prompt_tokens≈10]",
            encoding="utf-8",
        )
        request.stderr_path.write_text("", encoding="utf-8")
        return RealTaskSubprocessResult(exit_code=0, duration_seconds=1.0)

    monkeypatch.setattr(execution, "run_real_task_subprocess", _no_progress)

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

    issues = report.to_dict()["cases"][0]["issues"]
    assert "artifact_acceptance_failed=1" in issues
    assert "model_no_tool_progress" in issues
    assert "model_repetitive_output" in issues


# LLM: Timeout logs stay on disk when the streamed subprocess runner stops a case.
# 函数用途: 验证真实任务超时时已有 stdout/stderr 文件会被保留，报告只写结构化超时状态。
def test_main_agent_real_task_timeout_preserves_streamed_output(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_real_task_execution import (
        _timeout_case_result,
    )
    from agent_py_agent.agent.contracts.main_agent_real_task_subprocess import (
        RealTaskSubprocessResult,
    )

    runtime = _timeout_runtime(tmp_path)
    paths = runtime.paths
    paths["stdout"].write_text("你好", encoding="utf-8")
    paths["stderr"].write_text("partial error", encoding="utf-8")

    result = _timeout_case_result(
        runtime,
        RealTaskSubprocessResult(
            exit_code=124,
            duration_seconds=1.25,
            timed_out=True,
            timeout_reason="timeout",
        ),
    )

    assert result.exit_code == 124
    assert paths["stdout"].read_text(encoding="utf-8") == "你好"
    assert paths["stderr"].read_text(encoding="utf-8") == "partial error"


# LLM: Failed real tasks need a structured recovery packet, not only long stdout logs.
# 函数用途: 验证真实任务超时失败后会写 recovery_packet.json，后续续跑可按 refs 接着查证。
def test_main_agent_real_task_timeout_writes_recovery_packet(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_real_task_execution import (
        _timeout_case_result,
    )
    from agent_py_agent.agent.contracts.main_agent_real_task_subprocess import (
        RealTaskSubprocessResult,
    )

    runtime = _timeout_runtime(tmp_path)
    paths = runtime.paths
    paths["stdout"].write_text("partial stdout", encoding="utf-8")
    paths["stderr"].write_text("", encoding="utf-8")

    result = _timeout_case_result(
        runtime,
        RealTaskSubprocessResult(
            exit_code=124,
            duration_seconds=2.0,
            timed_out=True,
            timeout_reason="timeout",
        ),
    )

    recovery_ref = result.to_dict()["recovery_packet_ref"]
    packet_path = tmp_path / recovery_ref
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    assert recovery_ref.endswith("recovery_packet.json")
    assert packet["schema_version"] == "main-agent-real-task-recovery.v1"
    assert packet["case_id"] == "timeout_case"
    assert packet["status"] == "FAILED"
    assert packet["recovery_required"] is True
    assert packet["reason_codes"] == ["timeout", "artifact_acceptance_failed=1"]
    assert packet["refs"]["stdout_ref"] == result.stdout_ref
    assert packet["refs"]["acceptance_report_ref"] == result.acceptance_report_ref
    assert packet["acceptance"]["summary"]["failed"] == 1
