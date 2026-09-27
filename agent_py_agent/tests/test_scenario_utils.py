from __future__ import annotations

"""scenario-test helper regressions."""

import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import gateway_paths, is_pid_alive, write_json_file
from agent_py_agent.agent.gateway_parts.daemon_metadata import _get_process_start_time
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.process_control import terminate_pid_with_escalation
from agent_py_agent.cli import scenario_utils
from agent_py_agent.cli.scenario_utils import (
    build_scenario_prompt,
    build_scenario_runner_instruction,
    collect_scenario_report_files,
    create_scenario_workspace,
    load_scenario_agent,
    run_scenario_gateway_ask,
    scenario_gateway_ready_budget_seconds,
)
from agent_py_agent.cli.scenario_workspace import ScenarioConfigRequest, write_scenario_config


def test_collect_scenario_report_files_reads_agent_workspace_outputs():
    """场景测试最终核对要读取子代理 canonical agent workspace 产物。"""

    with tempfile.TemporaryDirectory() as td:
        fixture_root = Path(td)
        agent = SimpleAgent(
            AgentConfig(subagent_workspace=".my_agent/subagents"),
            fixture_root,
        )
        task = agent.subagents.create_run(
            goal="写场景报告",
            thought="报告必须落在 task_dir 内。",
            plan=["read", "write"],
            allowed_tools=["read_file", "write_file"],
        )
        report_dir = Path(task.agent_run_workspace_dir) / "scenario_outputs"
        report_dir.mkdir(parents=True)
        report_file = report_dir / f"{task.id}.md"
        report_file.write_text("ok\n", encoding="utf-8")

        reports = collect_scenario_report_files(agent, fixture_root, expected_count=1)

        assert reports == [report_file]


def test_collect_scenario_report_files_reads_output_json_artifacts():
    """场景测试最终核对应信任子代理 output.json 登记的真实产物。"""

    with tempfile.TemporaryDirectory() as td:
        fixture_root = Path(td)
        agent = SimpleAgent(
            AgentConfig(subagent_workspace=".my_agent/subagents"),
            fixture_root,
        )
        task = agent.subagents.create_run(
            goal="写场景报告",
            thought="报告可以落在 agent run workspace 内。",
            plan=["read", "write"],
            allowed_tools=["read_file", "write_file"],
        )
        report_dir = Path(task.agent_run_workspace_dir) / "scenario_outputs"
        report_dir.mkdir(parents=True)
        report_file = report_dir / f"{task.id}.md"
        report_file.write_text("ok\n", encoding="utf-8")
        Path(task.output_json).write_text(
            '{"artifacts":[{"path":"' + str(report_file) + '","kind":"report"}]}',
            encoding="utf-8",
        )

        reports = collect_scenario_report_files(agent, fixture_root, expected_count=1)

        assert reports == [report_file]


def test_runner_instruction_mentions_write_boundary_target():
    """runner 提示词要明确 canonical task_dir/allowed_write_roots，避免模型写旧 locator。"""

    instruction = build_scenario_runner_instruction()

    assert "allowed_write_roots" in instruction
    assert "task_dir/scenario_outputs/<run_id>.md" in instruction
    assert ".my_agent/subagents" not in instruction


def test_scenario_main_prompt_stays_plain_user_language():
    """真实主代理场景 prompt 不应把内部工具/协议名直接塞给模型。"""

    prompt = build_scenario_prompt(2)

    assert "2 个帮手" in prompt
    for internal in ("create_subagents", "dispatch_subagents", "start_runners", "SUBAGENT_RESULT"):
        assert internal not in prompt


def test_write_scenario_config_persists_runner_stress_overrides(tmp_path):
    source = tmp_path / "agent_config.yaml"
    target = tmp_path / "scenario_agent_config.yaml"
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    source.write_text("model_backend: echo\n", encoding="utf-8")

    write_scenario_config(
        ScenarioConfigRequest(
            source_config=source,
            target_config=target,
            fixture_root=fixture,
            request_timeout=180,
            max_subagents=10,
            runner_concurrency="5",
            runner_start_rate="10",
            model_request_timeout=240,
        )
    )

    text = target.read_text(encoding="utf-8")
    assert 'runner_concurrency: "5"' in text
    assert 'runner_start_rate: "10"' in text
    assert "request_timeout: 240" in text


# ── Gateway 生命周期：就绪超时 / 子进程超时 / 正常路径都必须把自己起的 Gateway 停干净 ──────────

_FAKE_GATEWAY_LAUNCHER = (
    "import subprocess, sys\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],"
    " start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    "print(child.pid)\n"
)


def _scenario_paths(tmp_path: Path):
    """按正式入口生成隔离场景工作区（echo 后端），不碰真实用户目录。"""
    source = tmp_path / "agent_config.yaml"
    source.write_text('model_backend: "echo"\n', encoding="utf-8")
    args = SimpleNamespace(workspace=str(tmp_path / "runs"), config=str(source), timeout=5.0, count=1)
    return create_scenario_workspace(args)


def _spawn_detached_fake_gateway(paths) -> int:
    """像真实 gateway start 一样：留下一个脱离父进程的常驻进程，并写同样格式的 pid 记录（含出生时间）。"""
    launcher = subprocess.run(
        [sys.executable, "-c", _FAKE_GATEWAY_LAUNCHER], capture_output=True, text=True, timeout=30, check=True,
    )
    pid = int(launcher.stdout.strip())
    pid_path = gateway_paths(load_scenario_agent(paths.config)).pid
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_file(pid_path, {"pid": pid, "kind": "my-agent-gateway", "start_time": _get_process_start_time(pid)})
    return pid


def _gateway_action(cmd: list[str]) -> str:
    """从场景命令行里取出 gateway 子命令（start/ask/stop），不看别的位置的同名词。"""
    if "gateway" not in cmd:
        return ""
    index = cmd.index("gateway")
    return cmd[index + 1] if index + 1 < len(cmd) else ""


@pytest.fixture
def reaped_pids():
    """测试自己起的假 Gateway 无论断言成败都要收掉，不给分片留后台进程。"""
    pids: list[int] = []
    yield pids
    for pid in pids:
        if is_pid_alive(pid):
            terminate_pid_with_escalation(pid, grace_seconds=0.5)


def test_ready_budget_follows_ask_timeout_within_bounds():
    """就绪预算跟随 ask 超时，夹在 10-60 秒，非法值落下限。"""

    assert scenario_gateway_ready_budget_seconds(0) == 10.0
    assert scenario_gateway_ready_budget_seconds(30) == 30.0
    assert scenario_gateway_ready_budget_seconds(300) == 60.0
    assert scenario_gateway_ready_budget_seconds("soon") == 10.0


def test_gateway_ask_stops_started_gateway_by_pid_after_readiness_timeout(tmp_path, monkeypatch, capsys, reaped_pids):
    """就绪超时（exit 2）时 Gateway 进程其实已经起来：必须调用 stop，正式 stop 停不掉也要按 pid 兜底。"""

    paths = _scenario_paths(tmp_path)
    commands: list[list[str]] = []

    def fake_subprocess(cmd, *, env, timeout):
        commands.append(list(cmd))
        action = _gateway_action(cmd)
        if action == "start":
            reaped_pids.append(_spawn_detached_fake_gateway(paths))
            return subprocess.CompletedProcess(cmd, 2, "gateway starting pid=1\n", "did not become ready\n")
        if action == "stop":
            # 假的正式 stop 只报“仍在运行”且不杀进程：还没发布 running 的 Gateway 可能不处理停止请求。
            return subprocess.CompletedProcess(cmd, 2, "", "gateway stop requested but still running\n")
        raise AssertionError(f"unexpected scenario command: {cmd}")

    monkeypatch.setattr(scenario_utils, "run_scenario_subprocess", fake_subprocess)

    payload = run_scenario_gateway_ask(paths, "hello", timeout=5)

    pid = reaped_pids[0]
    assert payload["ok"] is False
    assert payload["error"] == "gateway start failed"
    assert [_gateway_action(cmd) for cmd in commands] == ["start", "stop"]
    start_cmd, stop_cmd = commands
    assert "--force" in start_cmd
    assert start_cmd[start_cmd.index("--ready-timeout") + 1] == "10.0"
    assert "--kill" in stop_cmd
    stop = payload["gateway_stop"]
    assert stop["pid"] == pid
    assert stop["stop_returncode"] == 2
    assert stop["terminated_by_pid"] is True
    assert stop["alive_after_stop"] is False
    assert not is_pid_alive(pid)
    assert "gateway_stop=" in capsys.readouterr().out


def test_gateway_ask_stops_gateway_when_start_subprocess_times_out(tmp_path, monkeypatch, reaped_pids):
    """start 子进程本身超时抛错时进程可能已经 spawn：异常照抛，但 stop 仍要执行并按 pid 兜底。"""

    paths = _scenario_paths(tmp_path)
    commands: list[list[str]] = []

    def fake_subprocess(cmd, *, env, timeout):
        commands.append(list(cmd))
        action = _gateway_action(cmd)
        if action == "start":
            reaped_pids.append(_spawn_detached_fake_gateway(paths))
            raise subprocess.TimeoutExpired(cmd, timeout)
        if action == "stop":
            return subprocess.CompletedProcess(cmd, 0, "gateway 未在运行\n", "")
        raise AssertionError(f"unexpected scenario command: {cmd}")

    monkeypatch.setattr(scenario_utils, "run_scenario_subprocess", fake_subprocess)

    with pytest.raises(subprocess.TimeoutExpired):
        run_scenario_gateway_ask(paths, "hello", timeout=5)

    assert [_gateway_action(cmd) for cmd in commands] == ["start", "stop"]
    assert not is_pid_alive(reaped_pids[0])


def test_gateway_ask_returns_gateway_json_with_stop_facts(tmp_path, monkeypatch):
    """正常路径：start -> ask -> stop 各一次，JSON 回复原样返回并附结构化停止事实。"""

    paths = _scenario_paths(tmp_path)
    commands: list[list[str]] = []

    def fake_subprocess(cmd, *, env, timeout):
        commands.append(list(cmd))
        action = _gateway_action(cmd)
        if action == "start":
            return subprocess.CompletedProcess(cmd, 0, "gateway starting pid=1\n", "")
        if action == "ask":
            assert "--no-save" in cmd
            return subprocess.CompletedProcess(cmd, 0, '{"ok": true, "id": "req-1"}\n', "")
        if action == "stop":
            return subprocess.CompletedProcess(cmd, 0, "gateway 未在运行\n", "")
        raise AssertionError(f"unexpected scenario command: {cmd}")

    monkeypatch.setattr(scenario_utils, "run_scenario_subprocess", fake_subprocess)

    payload = run_scenario_gateway_ask(paths, "hello", timeout=5)

    assert [_gateway_action(cmd) for cmd in commands] == ["start", "ask", "stop"]
    assert payload["ok"] is True
    assert payload["id"] == "req-1"
    assert payload["gateway_stop"] == {
        "pid": None,
        "stop_returncode": 0,
        "stop_error": "",
        "terminated_by_pid": False,
        "alive_after_stop": False,
    }
