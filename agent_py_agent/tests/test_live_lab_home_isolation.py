"""Live Lab 使用真实路径解析及 CLI，不通过放宽工作区安全门修夹具。"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from agent_py_agent.agent.conversation.workspace_context import (
    WorkspaceDirectoryError,
    validate_workspace_directory,
)
from agent_py_agent.agent.settings import load_config
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.cli.common import make_lightweight_cli_agent_context
from scripts.live_lab.cli import build_parser
from scripts.live_lab.constants import DEFAULT_CONFIG, REPO_ROOT
from scripts.live_lab.session import LabSessionManager


def test_live_lab_home_is_sibling_of_workspace_and_resolves_runtime_paths(tmp_path):
    args = build_parser().parse_args([
        "--runs-dir", str(tmp_path / "runs"), "--run-id", "isolated",
    ])
    session = LabSessionManager(args)
    session.setup()
    config = load_config(session.config_path)
    home = home_paths(config.my_agent_home)
    workspace = session.fixture_root.resolve()

    assert home.root == session.run_root / "home"
    assert home.root.parent == workspace.parent
    assert not home.root.is_relative_to(workspace)
    assert validate_workspace_directory(
        workspace, owner_home=home.owner_home_dir, home_root=home.root, is_admin=True,
    ) == workspace
    with pytest.raises(WorkspaceDirectoryError, match="宿主数据根"):
        validate_workspace_directory(
            session.run_root, owner_home=home.owner_home_dir, home_root=home.root,
            is_admin=True,
        )

    context = make_lightweight_cli_agent_context(argparse.Namespace(config=str(session.config_path)))
    paths = context.runtime_paths
    expected = {
        "memory_path": home.owner_memory_long_term_dir / "memory.jsonl",
        "local_store_path": context.workspace / ".my_agent/local_store/local.db",
        "local_store_files_dir": context.workspace / ".my_agent/local_store/files",
        "local_store_events_path": context.workspace / ".my_agent/local_store/events.jsonl",
        "subagent_workspace": context.workspace / ".my_agent/subagents",
        "gateway_workspace": home.owner_workspace_dir / ".my_agent/gateway",
        "adapter_workspace": home.owner_workspace_dir / ".my_agent/adapters/file",
    }
    for key, value in expected.items():
        assert paths[key].resolve() == value.resolve(), key
        assert paths[key].resolve().is_relative_to(session.run_root), key
    assert paths["local_store_events_path"].is_file()


def test_live_lab_original_default_echo_smoke_cli_completes(tmp_path):
    """原公开脚本必须完成 health 和 bad_weather，不能只断言配置文本。"""
    env = os.environ.copy()
    env["MY_AGENT_HOME"] = str(tmp_path / "caller-home")
    env["PYTHONPATH"] = os.pathsep.join((str(REPO_ROOT / "scripts"), str(REPO_ROOT)))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    run_root = tmp_path / "runs/smoke"
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/live_agent_lab.py"),
         "--config", str(DEFAULT_CONFIG), "--runs-dir", str(run_root.parent),
         "--run-id", run_root.name, "--timeout", "30"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads((run_root / "live_lab_summary.json").read_text(encoding="utf-8"))
    assert summary["ok"] is True
    assert summary["suite"] == "smoke" and summary["real_llm"] is False
    assert [(row["case"], row["status"]) for row in summary["results"]] == [
        ("health", "pass"), ("bad_weather", "pass"),
    ]
    reports = [json.loads(path.read_text()) for path in (run_root / "bad_weather").glob("*/scenario_summary.json")]
    retry = next(row for row in reports if row.get("case") == "runner-retry")
    assert retry["first_status"] == "FAILED"
    assert "scenario transient runner failure" in retry["first_error"]
    assert retry["final_status"] == "DONE" and retry["verification_status"] == "VERIFIED"
    assert retry["runner_attempts"] == 2
    report_file = Path(retry["report_file"])
    assert report_file.is_file() and report_file.is_relative_to(run_root)
    assert "runner retry" in report_file.read_text()
    assert "write_file" in retry["used_tools"]
    assert retry["artifact_registered"] is True
    home = home_paths(load_config(Path(retry["config"])).my_agent_home)
    with sqlite3.connect((home.owner_home_dir / "runtime.db").as_uri() + "?mode=ro", uri=True) as db:
        tree = db.execute(
            "SELECT tr.status FROM task_runs tr JOIN agent_runs ar ON ar.task_run_id=tr.task_run_id WHERE ar.run_id=?",
            (retry["run_id"],),
        ).fetchone()
        assert tree == ("done",), "场景 CLI 父级不能在结束后留下仍活动的 TaskRun"
        operations = db.execute(
            "SELECT op.status, op.settled_at FROM tool_operations op JOIN agent_runs ar ON ar.agent_run_id=op.agent_run_id "
            "WHERE ar.run_id=? AND op.operation_type='write_file'", (retry["run_id"],),
        ).fetchall()
        assert len(operations) == 1 and operations[0][0] == "SUCCEEDED" and operations[0][1] > 0


def test_retry_case_failed_acceptance_closes_parent_as_failed(tmp_path, monkeypatch):
    """场景自己的验收失败应结束父级执行，不能借子代理成功伪报 done。"""
    from agent_py_agent.cli.scenario_cases import runner_retry_case

    monkeypatch.setattr(runner_retry_case, "_verify_runner_retry", lambda request: False)
    args = argparse.Namespace(
        config=str(DEFAULT_CONFIG), workspace=str(tmp_path), timeout=30, count=1,
        capability_config=None, skill_dir=None,
    )
    assert runner_retry_case.run_scenario_runner_retry_case(args) == 2
    summary_path, = tmp_path.glob("scenario-*/scenario_summary.json")
    retry = json.loads(summary_path.read_text(encoding="utf-8"))
    assert retry["ok"] is False
    assert retry["final_status"] == "DONE" and retry["verification_status"] == "VERIFIED"
    home = home_paths(load_config(Path(retry["config"])).my_agent_home)
    with sqlite3.connect((home.owner_home_dir / "runtime.db").as_uri() + "?mode=ro", uri=True) as db:
        status, closed_at = db.execute(
            "SELECT tr.status,tr.closed_at FROM task_runs tr JOIN agent_runs ar ON ar.task_run_id=tr.task_run_id "
            "WHERE ar.run_id=?", (retry["run_id"],),
        ).fetchone()
        assert status == "failed" and closed_at > 0


def test_scenario_home_is_sibling_of_workspace_and_keeps_data_root_guard(tmp_path):
    from agent_py_agent.cli.scenario_workspace import ScenarioConfigRequest, write_scenario_config

    fixture = tmp_path / "scenario/fixture_project"
    fixture.mkdir(parents=True)
    target = fixture.parent / "scenario.yaml"
    write_scenario_config(ScenarioConfigRequest(DEFAULT_CONFIG, target, fixture, 30, 1))
    config = load_config(target)
    home = home_paths(config.my_agent_home)

    assert home.root == fixture.parent / "home"
    assert not home.root.is_relative_to(fixture)
    assert validate_workspace_directory(
        fixture, owner_home=home.owner_home_dir, home_root=home.root, is_admin=True,
    ) == fixture
    with pytest.raises(WorkspaceDirectoryError, match="宿主数据根"):
        validate_workspace_directory(
            fixture.parent, owner_home=home.owner_home_dir, home_root=home.root, is_admin=True,
        )


def test_restart_fixture_carries_processing_status_for_real_requeue(tmp_path):
    from agent_py_agent.agent.gateway_parts import requeue_gateway_processing_requests
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.cli.scenario_cases.gateway_cases import (
        _restart_case_write_processing_payload,
    )

    paths = gateway_paths_from_root(tmp_path / "gateway")
    paths.processing.mkdir(parents=True)
    processing, payload = _restart_case_write_processing_payload(paths, "restart-fixture")
    assert payload.get("status") == "processing"
    assert json.loads(processing.read_text())["status"] == "processing"
    assert requeue_gateway_processing_requests(paths) == 1
    assert not processing.exists()
    pending = json.loads((paths.inbox / processing.name).read_text())
    assert pending["id"] == payload["id"]
    assert pending["status"] == "pending"


def test_retry_backend_emits_native_tool_request_after_real_failure(tmp_path):
    from agent_py_agent.agent.tooling.runtime_contracts import ProviderToolCapability
    from agent_py_agent.cli.scenario_cases.runner_retry_backend import ScenarioRetryBackend

    backend = ScenarioRetryBackend()
    assert callable(getattr(backend, "probe_tool_capability", None))
    capability = backend.probe_tool_capability()
    assert isinstance(capability, ProviderToolCapability) and capability.native_supported is True
    backend.report_path = tmp_path / "retry.md"
    tools = [{"name": "write_file"}]
    with pytest.raises(RuntimeError, match="scenario transient runner failure"):
        backend.generate("runner task", tools=tools)
    response = backend.generate("runner task", tools=tools)
    assert response.text == "" and len(response.tool_use_blocks) == 1
    block = response.tool_use_blocks[0]
    assert block["name"] == "write_file" and block["id"]
    assert block["input"]["path"] == str(backend.report_path)
    assert "runner retry" in block["input"]["content"]
    assert not backend.report_path.exists(), "后端不能代替原生工具直接写产物"
