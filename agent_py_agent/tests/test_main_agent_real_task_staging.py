"""Tests for staged contracts in long main-agent real tasks."""

from __future__ import annotations

import json
from pathlib import Path


# LLM: long research deliverables need structured checkpoint refs before final artifacts.
# 函数用途: 验证 xlsx 真实任务合同包含数据缓存、通用构建工具和最终产物 refs，避免一次性大脚本走钢丝。
def test_main_agent_real_task_xlsx_case_has_staged_artifact_contract(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_real_task_suite import (
        MainAgentRealTaskSuiteRequest,
        plan_main_agent_real_task_suite,
    )

    report = plan_main_agent_real_task_suite(
        MainAgentRealTaskSuiteRequest(workspace=tmp_path, max_workers=1)
    )
    case = next(item for item in report.cases if item.case_id == "github_weekly_star_growth_xlsx")
    artifacts = json.loads((tmp_path / case.expected_artifacts_ref).read_text(encoding="utf-8"))
    contract = artifacts["artifacts"][0]["validation_contract"]

    assert contract["staging_contract"]["strategy"] == "data_then_tool_builder_then_workbook"
    assert contract["staging_contract"]["builder_tool"] == "data_to_workbook"
    assert contract["staging_contract"]["source_json_ref"] == "outputs/github_star_growth/source_data.json"
    assert contract["staging_contract"]["workbook_ref"] == "outputs/github_star_growth/github_star_growth.xlsx"
    assert contract["staging_contract"]["checkpoint_refs"] == [
        "outputs/github_star_growth/source_data.json",
        "outputs/github_star_growth/github_star_growth.xlsx",
    ]
    assert contract["collection_contract"]["source_json_ref"] == "outputs/github_star_growth/source_data.json"
    assert contract["collection_contract"]["groups_path"] == "sheets"
    assert contract["collection_contract"]["items_path"] == "rows"
    assert contract["collection_contract"]["min_groups"] >= 20
    assert contract["collection_contract"]["min_items_per_group"] == 10


# LLM: real tasks need resolved artifact path contracts so models do not infer roots from old files.
# 函数用途: 验证执行合同会把产物相对路径绑定到任务 workspace 下的绝对路径，并写出 manifest。
def test_main_agent_real_task_delivery_contract_binds_artifact_paths(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_real_task_execution_files import (
        case_paths,
        command_for_case,
    )
    from agent_py_agent.agent.contracts.main_agent_real_task_execution_models import (
        MainAgentRealTaskExecutionRequest,
    )
    from agent_py_agent.agent.contracts.main_agent_real_task_suite import (
        MainAgentRealTaskSuiteRequest,
        plan_main_agent_real_task_suite,
    )

    suite = plan_main_agent_real_task_suite(
        MainAgentRealTaskSuiteRequest(workspace=tmp_path, max_workers=1)
    )
    case = next(item for item in suite.cases if item.case_id == "github_weekly_star_growth_xlsx")
    paths = case_paths(tmp_path, case.case_id)

    command_for_case(
        case,
        MainAgentRealTaskExecutionRequest(workspace=tmp_path),
        config_path=tmp_path / "config.yaml",
        workspace=tmp_path,
        delivery_contract_path=paths["delivery_contract"],
    )
    payload = json.loads(paths["delivery_contract"].read_text(encoding="utf-8"))
    artifact = payload["artifacts"][0]

    assert artifact["path_contract"]["workspace_relative_path"] == "outputs/github_star_growth/github_star_growth.xlsx"
    assert artifact["path_contract"]["resolved_path"] == str(
        paths["workspace"] / "outputs/github_star_growth/github_star_growth.xlsx"
    )
    targets = payload["bootstrap_contract"]["materialization_targets"]
    target_paths = {item["workspace_relative_path"] for item in targets}
    assert "outputs/github_star_growth/source_data.json" in target_paths
    assert "outputs/github_star_growth/github_star_growth.xlsx" in target_paths
    startup_actions = payload["bootstrap_contract"]["startup_actions"]
    checkpoint_actions = [item for item in startup_actions if item.get("action") == "materialize_checkpoint"]
    assert checkpoint_actions
    assert checkpoint_actions[0]["checkpoint_ref"] == "outputs/github_star_growth/source_data.json"
    assert (paths["workspace"] / "outputs/github_star_growth").is_dir()
    assert (paths["workspace"] / ".my_agent_artifact_paths.json").is_file()


# LLM: directory-style deliverables should expose required files as generic materialization targets.
# 函数用途: 验证 web_project 这类目录产物不会只给一个目录路径，而会给出结构化的关键文件目标。
def test_main_agent_real_task_web_project_bootstrap_targets_required_files(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_task_execution_files import (
        case_paths,
        command_for_case,
    )
    from agent_py_agent.agent.contracts.main_agent_task_execution_models import (
        MainAgentTaskExecutionRequest,
    )
    from agent_py_agent.agent.contracts.main_agent_task_suite import (
        MainAgentTaskSuiteRequest,
        plan_main_agent_task_suite,
    )

    suite = plan_main_agent_task_suite(
        MainAgentTaskSuiteRequest(workspace=tmp_path, max_workers=1)
    )
    case = next(item for item in suite.cases if item.case_id == "shopping_site_flow")
    paths = case_paths(tmp_path, case.case_id)

    command_for_case(
        case,
        MainAgentTaskExecutionRequest(workspace=tmp_path),
        config_path=tmp_path / "config.yaml",
        workspace=tmp_path,
        delivery_contract_path=paths["delivery_contract"],
    )
    payload = json.loads(paths["delivery_contract"].read_text(encoding="utf-8"))
    validation_contract = payload["artifacts"][0]["validation_contract"]
    targets = payload["bootstrap_contract"]["materialization_targets"]
    target_paths = {item["workspace_relative_path"] for item in targets}

    assert validation_contract["require_complete_html"] is True
    assert "outputs/shopping_site/index.html" in target_paths
    assert "outputs/shopping_site/app.js" in target_paths


# LLM: real task execution should inherit the repo default backend instead of silently downgrading to echo.
# 函数用途: 验证未显式传 base_config_path 时，生成的 case config 仍沿用项目默认 model_backend。
def test_main_agent_real_task_case_config_uses_repo_default_backend(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_task_execution_files import (
        write_case_config,
    )

    config_path = tmp_path / "task" / "config.yaml"
    write_case_config(config_path, None, tmp_path / "task" / "workspace")
    text = config_path.read_text(encoding="utf-8")

    assert 'model_backend: "anthropic_compatible"' in text
    assert 'model_backend: "echo"' not in text


# LLM: controlled task subprocesses need owner-home isolation just like Live Lab sessions.
# 函数用途: 验证每个真实任务 case 的状态、gateway、记忆和子代理目录都关在本 case workspace 内。
def test_main_agent_real_task_case_config_isolates_runtime_home(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_task_execution_files import (
        write_case_config,
    )

    base_config = tmp_path / "base.yaml"
    base_config.write_text(
        "\n".join(
            [
                'agent_name: "base"',
                'model_backend: "echo"',
                'workspace_root: "/old/workspace"',
                'my_agent_home: "/old/home"',
                'local_store_path: "/old/local.db"',
                'gateway_workspace: "/old/gateway"',
                'subagent_workspace: "/old/subagents"',
            ]
        ),
        encoding="utf-8",
    )
    task_workspace = tmp_path / "task" / "workspace"
    config_path = tmp_path / "task" / "config.yaml"

    write_case_config(config_path, base_config, task_workspace)

    text = config_path.read_text(encoding="utf-8")
    assert f'workspace_root: "{task_workspace}"' in text
    assert f'my_agent_home: "{task_workspace / ".my_agent" / "home"}"' in text
    assert 'local_store_path: ".my_agent/local_store/local.db"' in text
    assert 'gateway_workspace: ".my_agent/gateway"' in text
    assert 'subagent_workspace: ".my_agent/subagents"' in text
    assert "/old/workspace" not in text
    assert "/old/home" not in text
    assert "/old/local.db" not in text


# LLM: command preparation should reconcile duplicate write sessions before the resumed model run starts.
# 函数用途: 验证真实任务续跑生成交付合同时，会先清理同目标重复 open file_write_session。
def test_command_for_case_reconciles_duplicate_open_write_sessions(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_real_task_execution_files import (
        case_paths,
        command_for_case,
    )
    from agent_py_agent.agent.contracts.main_agent_real_task_execution_models import (
        MainAgentRealTaskExecutionRequest,
    )
    from agent_py_agent.agent.contracts.main_agent_real_task_suite import (
        MainAgentRealTaskSuiteRequest,
        plan_main_agent_real_task_suite,
    )

    suite = plan_main_agent_real_task_suite(MainAgentRealTaskSuiteRequest(workspace=tmp_path))
    case = next(item for item in suite.cases if item.case_id == "github_weekly_star_growth_xlsx")
    paths = case_paths(tmp_path, case.case_id)
    packet = _write_duplicate_open_session_packet(tmp_path, paths["workspace"])

    command_for_case(
        case,
        MainAgentRealTaskExecutionRequest(workspace=tmp_path, recovery_packet_path=packet),
        config_path=tmp_path / "config.yaml",
        workspace=tmp_path,
        delivery_contract_path=paths["delivery_contract"],
    )

    payload = json.loads(paths["delivery_contract"].read_text(encoding="utf-8"))
    assert payload["recovery_reconciliation"]["groups"][0]["kept_session_id"] == "kept-session"
    assert _json(paths["workspace"] / ".agent_file_write_sessions/empty-session/manifest.json")["status"] == "aborted"
    assert _json(paths["workspace"] / ".agent_file_write_sessions/kept-session/manifest.json")["status"] == "open"


# LLM: missing artifact reports should expose structured alternate candidates for recovery.
# 函数用途: 验证模型写错目录时，验收报告能提供同名候选路径，而不是只说缺文件。
def test_real_task_acceptance_reports_candidate_artifact_paths(tmp_path):
    from openpyxl import Workbook

    from agent_py_agent.agent.contracts.main_agent_real_task_acceptance import (
        RealTaskAcceptanceRequest,
        validate_real_task_artifacts,
    )

    workspace = tmp_path / "task"
    wrong_dir = workspace / "data" / "outputs" / "github_star_growth"
    wrong_dir.mkdir(parents=True)
    workbook_path = wrong_dir / "github_star_growth.xlsx"
    wb = Workbook()
    wb.active["A1"] = "项目名"
    wb.save(workbook_path)
    expected = tmp_path / "expected_artifacts.json"
    expected.write_text(
        json.dumps(
            {
                "artifacts": [
                    {
                        "artifact_id": "github_star_growth_workbook",
                        "kind": "xlsx",
                        "preferred_path": "outputs/github_star_growth/github_star_growth.xlsx",
                        "required": True,
                        "validation_contract": {"validator": "spreadsheet_acceptance"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report = validate_real_task_artifacts(
        RealTaskAcceptanceRequest(
            expected_artifacts_path=expected,
            task_workspace=workspace,
            report_path=tmp_path / "acceptance_report.json",
        )
    )
    findings = report.artifacts[0].report["findings"]

    assert any(item["code"] == "ARTIFACT_MISSING" for item in findings)
    candidates = [item for item in findings if item["code"] == "ARTIFACT_CANDIDATE_PATH"]
    assert candidates
    assert str(Path(candidates[0]["value"]).relative_to(workspace)) == "data/outputs/github_star_growth/github_star_growth.xlsx"


# LLM: candidate artifacts must pass the same validation contract before recovery suggests reuse.
# 函数用途: 验证同名旧 xlsx 如果缺少必需列，只记录 rejected，不让主代理直接复用错表。
def test_real_task_acceptance_rejects_invalid_candidate_artifact(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_real_task_acceptance import (
        RealTaskAcceptanceRequest,
        validate_real_task_artifacts,
    )
    from agent_py_agent.agent.tooling.spreadsheet_builder import DataWorkbookTool

    workspace = tmp_path / "task"
    DataWorkbookTool(workspace).execute(
        {
            "path": "data/outputs/github_star_growth/github_star_growth.xlsx",
            "sheets": [{"name": "summary", "rows": [{"项目名": "demo"}]}],
        }
    )
    expected = tmp_path / "expected_artifacts.json"
    _write_candidate_expected_artifacts(expected)

    report = validate_real_task_artifacts(
        RealTaskAcceptanceRequest(
            expected_artifacts_path=expected,
            task_workspace=workspace,
            report_path=tmp_path / "acceptance_report.json",
        )
    )
    findings = report.artifacts[0].report["findings"]
    codes = {item["code"] for item in findings}

    assert "ARTIFACT_CANDIDATE_REJECTED" in codes
    assert "ARTIFACT_CANDIDATE_PATH" not in codes


# LLM: Accepted artifacts should not be re-blocked by stale open sessions that point to the same finished target.
# 函数用途: 验证目标产物已经通过验收时，同目标旧 open file_write_session 不会再把真实任务卡成 runtime finding。
def test_real_task_acceptance_ignores_open_session_for_accepted_target(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_real_task_acceptance import (
        RealTaskAcceptanceRequest,
        validate_real_task_artifacts,
    )
    from agent_py_agent.agent.tooling.spreadsheet_builder import DataWorkbookTool

    workspace = tmp_path / "task"
    DataWorkbookTool(workspace).execute(
        {
            "path": "outputs/github_star_growth/github_star_growth.xlsx",
            "sheets": [
                {"name": "summary", "rows": [{"项目名": "demo", "地址": "https://example.com"}]},
                {"name": "details", "rows": [{"项目名": "demo", "地址": "https://example.com"}]},
            ],
        }
    )
    workbook = workspace / "outputs/github_star_growth/github_star_growth.xlsx"
    _write_open_session_target_manifest(
        workspace,
        session_id="session-workbook",
        relative_target="outputs/github_star_growth/github_star_growth.xlsx",
        resolved_target=workbook,
    )
    expected = tmp_path / "expected_artifacts.json"
    _write_candidate_expected_artifacts(expected)

    report = validate_real_task_artifacts(
        RealTaskAcceptanceRequest(
            expected_artifacts_path=expected,
            task_workspace=workspace,
            report_path=tmp_path / "acceptance_report.json",
        )
    )

    assert report.ok is True
    assert report.runtime_findings == []
    assert report.artifacts[0].ok is True


# LLM: _write_candidate_expected_artifacts keeps invalid-candidate tests compact.
# 函数用途: 写一个要求两张表和必需列的 expected_artifacts.json。
def _write_candidate_expected_artifacts(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "artifacts": [
                    {
                        "artifact_id": "github_star_growth_workbook",
                        "kind": "xlsx",
                        "preferred_path": "outputs/github_star_growth/github_star_growth.xlsx",
                        "required": True,
                        "validation_contract": {
                            "validator": "spreadsheet_acceptance",
                            "required_columns": ["项目名", "地址"],
                            "required_sheets_min": 2,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


# LLM: staged checkpoint validation should explain why long tasks cannot continue.
# 函数用途: 验证 source_data 为空时会进入结构化 runtime findings，最终 xlsx 仍走 artifact 验收。
def test_real_task_acceptance_validates_staged_checkpoints(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_real_task_acceptance import (
        RealTaskAcceptanceRequest,
        validate_real_task_artifacts,
    )

    workspace = tmp_path / "task"
    stage_dir = workspace / "outputs" / "github_star_growth"
    stage_dir.mkdir(parents=True)
    (stage_dir / "source_data.json").write_text(json.dumps({"top10": []}), encoding="utf-8")
    expected = _write_staged_expected_artifacts(tmp_path)

    report = validate_real_task_artifacts(
        RealTaskAcceptanceRequest(
            expected_artifacts_path=expected,
            task_workspace=workspace,
            report_path=tmp_path / "acceptance_report.json",
        )
    )
    codes = {item["code"] for item in report.runtime_findings}

    assert "STAGED_JSON_NO_ROWS" in codes


# LLM: Skeleton-only staged JSON with empty nested collections must not count as ready row data.
# 函数用途: 验证只有 sheet 骨架和 metadata、没有真实项目行时，阶段 JSON 仍然会被标记为无数据。
def test_real_task_acceptance_treats_skeleton_only_staged_json_as_no_rows(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_real_task_acceptance import (
        RealTaskAcceptanceRequest,
        validate_real_task_artifacts,
    )

    workspace = tmp_path / "task"
    stage_dir = workspace / "outputs" / "github_star_growth"
    stage_dir.mkdir(parents=True)
    (stage_dir / "source_data.json").write_text(
        json.dumps(
            {
                "generated_date": "2026-05-20",
                "sheets": [{"week": "2026-W01", "projects": []}],
                "metadata": {"total_weeks": 20},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    expected = _write_staged_expected_artifacts(tmp_path)

    report = validate_real_task_artifacts(
        RealTaskAcceptanceRequest(
            expected_artifacts_path=expected,
            task_workspace=workspace,
            report_path=tmp_path / "acceptance_report.json",
        )
    )
    codes = {item["code"] for item in report.runtime_findings}

    assert "STAGED_JSON_NO_ROWS" in codes


# LLM: invalid staged JSON must be reported as truncated/invalid, not as merely empty data.
# 函数用途: 验证阶段 JSON 语法坏掉时会产出 STAGED_JSON_INVALID，避免恢复链误判为空数据。
def test_real_task_acceptance_reports_invalid_staged_json(tmp_path):
    from agent_py_agent.agent.contracts.main_agent_real_task_acceptance import (
        RealTaskAcceptanceRequest,
        validate_real_task_artifacts,
    )

    workspace = tmp_path / "task"
    stage_dir = workspace / "outputs" / "github_star_growth"
    stage_dir.mkdir(parents=True)
    (stage_dir / "source_data.json").write_text('[{"项目名":"demo","语言":"Py', encoding="utf-8")
    expected = _write_staged_expected_artifacts(tmp_path)

    report = validate_real_task_artifacts(
        RealTaskAcceptanceRequest(
            expected_artifacts_path=expected,
            task_workspace=workspace,
            report_path=tmp_path / "acceptance_report.json",
        )
    )
    finding = next(item for item in report.runtime_findings if item["code"] == "STAGED_JSON_INVALID")

    assert "parse_error" in finding
    assert finding["stage_ref"] == "outputs/github_star_growth/source_data.json"


# LLM: _write_staged_expected_artifacts keeps the staging test focused on assertions.
# 函数用途: 写一个带 source/final 和通用 builder 工具的 expected_artifacts.json。
def _write_staged_expected_artifacts(tmp_path):
    expected = tmp_path / "expected_artifacts.json"
    expected.write_text(json.dumps({"artifacts": [_staged_workbook_artifact()]}), encoding="utf-8")
    return expected


# LLM: _staged_workbook_artifact is the minimal xlsx artifact contract used by tests.
# 函数用途: 返回 GitHub xlsx 任务的阶段化产物合同，避免测试函数过长。
def _staged_workbook_artifact():
    return {
        "artifact_id": "github_star_growth_workbook",
        "kind": "xlsx",
        "preferred_path": "outputs/github_star_growth/github_star_growth.xlsx",
        "required": True,
        "validation_contract": {
            "validator": "spreadsheet_acceptance",
            "staging_contract": {
                "strategy": "data_then_tool_builder_then_workbook",
                "builder_tool": "data_to_workbook",
                "source_json_ref": "outputs/github_star_growth/source_data.json",
                "workbook_ref": "outputs/github_star_growth/github_star_growth.xlsx",
                "checkpoint_refs": [
                    "outputs/github_star_growth/source_data.json",
                    "outputs/github_star_growth/github_star_growth.xlsx",
                ]
            },
        },
    }


# LLM: _write_duplicate_open_session_packet creates recovery state with two open sessions for one target.
# 函数用途: 为 command_for_case 集成测试写恢复包和两个 manifest，一个空 session，一个已有 chunk 的 session。
def _write_duplicate_open_session_packet(workspace: Path, task_workspace: Path) -> Path:
    from agent_py_agent.agent.contracts.main_agent_real_task_recovery_packet import SCHEMA_VERSION

    empty = _write_open_session_manifest(task_workspace, "empty-session", {})
    kept = _write_open_session_manifest(task_workspace, "kept-session", {"0": {}})
    packet = workspace / "recovery_packet.json"
    packet.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "case_id": "github_weekly_star_growth_xlsx",
                "refs": {"task_workspace_ref": str(task_workspace.relative_to(workspace))},
                "acceptance": {"runtime_findings": [_session_finding(empty), _session_finding(kept)]},
            }
        ),
        encoding="utf-8",
    )
    return packet


# LLM: _write_open_session_manifest writes the minimal manifest needed by reconciliation.
# 函数用途: 创建 open file_write_session manifest，不写真实 chunk 正文。
def _write_open_session_manifest(task_workspace: Path, session_id: str, chunks: dict[str, object]) -> Path:
    path = task_workspace / ".agent_file_write_sessions" / session_id / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "session_id": session_id,
                "status": "open",
                "target_path": {"display": "outputs/github_star_growth/fetch.py"},
                "chunks": chunks,
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_open_session_target_manifest(
    task_workspace: Path,
    session_id: str,
    relative_target: str,
    resolved_target: Path,
) -> Path:
    path = task_workspace / ".agent_file_write_sessions" / session_id / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "session_id": session_id,
                "status": "open",
                "target_path": {
                    "raw": relative_target,
                    "resolved": str(resolved_target.resolve()),
                    "display": relative_target,
                },
                "chunks": {"0": {"index": 0}},
            }
        ),
        encoding="utf-8",
    )
    return path


# LLM: _session_finding mirrors acceptance OPEN_FILE_WRITE_SESSION facts.
# 函数用途: 从 manifest 生成结构化 runtime finding，供恢复包测试使用。
def _session_finding(path: Path) -> dict[str, object]:
    manifest = _json(path)
    chunks = sorted(int(index) for index in manifest.get("chunks", {}))
    return {
        "code": "OPEN_FILE_WRITE_SESSION",
        "session_id": manifest["session_id"],
        "manifest_path": str(path),
        "target_path": manifest["target_path"],
        "received_chunks": chunks,
        "next_chunk_index": (max(chunks) + 1) if chunks else 0,
    }


# LLM: _json keeps manifest assertions compact.
# 函数用途: 读取测试 JSON 文件并返回对象。
def _json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))
