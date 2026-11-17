"""G4 命令行与 TUI 控制细节：/delegate 传输往返（R09）、未知运行 ID 的干净报错（R14a）、Skill 提案编号错误提示（R14b）、
credentials 的 --owner 写法（R14c）、一次性计划过期恢复的中文原因（R14d）、bench-model 测当前选定模型（R16）。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli.chat_parts.control_runtime import _command_text

_MISSING_RUN = "run-missing-r14"


# 函数用途: R09——/delegate 的参数经“解析→传输文本→解析”往返后不丢。
@pytest.mark.parametrize("text", ["/delegate", "/delegate enable", "/delegate disable", "/delegate status",
                                  "/delegate 旧写法 两段"])
def test_delegate_arguments_round_trip_through_gateway_transport(text):
    command = parse_conversation_control(text)
    assert command is not None and command.kind == "delegate"
    again = parse_conversation_control(_command_text(command))
    assert again is not None
    assert (again.kind, again.valid, again.operation, again.value) == (
        command.kind, command.valid, command.operation, command.value)


# 函数用途: 建一个空的真实 Agent（运行记录目录里没有任何子代理）。
def _agent(tmp_path):
    return SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home")), tmp_path)


# 函数用途: 断言命令以退出码 2 结束，输出中文原因与查看命令，且没有堆栈。
def _assert_clean_missing_run(capsys, rc):
    out = capsys.readouterr()
    assert rc == 2
    assert f"子代理运行记录不存在：{_MISSING_RUN}" in out.out
    assert "my-agent subagents --all" in out.out
    assert "Traceback" not in out.out + out.err


# 函数用途: R14a——`subagent <id>` 遇到不存在的运行 ID 时干净报错。
def test_subagent_detail_reports_a_missing_run_cleanly(tmp_path, monkeypatch, capsys):
    from agent_py_agent.cli import _board

    agent = _agent(tmp_path)
    monkeypatch.setattr(_board, "make_agent", lambda args: agent)
    _assert_clean_missing_run(capsys, _board.cmd_subagent_detail(SimpleNamespace(run_id=_MISSING_RUN)))


# 函数用途: R14a——`subagents-tests <id>` 遇到不存在的运行 ID 时干净报错。
def test_subagents_tests_reports_a_missing_run_cleanly(tmp_path, capsys):
    from agent_py_agent.cli import _review_tests

    agent = _agent(tmp_path)
    args = SimpleNamespace(run_id=_MISSING_RUN, re_run=False, timeout=1.0)
    _assert_clean_missing_run(capsys, _review_tests.cmd_subagents_tests(args, make_agent_fn=lambda _args: agent))


# 函数用途: R14a——`subagent-run <id>` 的 dry-run 与 --execute 都对不存在的运行 ID 干净报错。
@pytest.mark.parametrize("execute", [False, True])
def test_subagent_run_reports_a_missing_run_cleanly(tmp_path, monkeypatch, capsys, execute):
    from agent_py_agent.cli import _dispatch

    agent = _agent(tmp_path)
    monkeypatch.setattr(_dispatch, "make_agent", lambda args: agent)
    args = SimpleNamespace(run_id=_MISSING_RUN, execute=execute, instruction="", max_cards=0, no_probe=True)
    _assert_clean_missing_run(capsys, _dispatch.cmd_subagent_run(args))


# 函数用途: R14a——运行记录存在时，执行中其它文件缺失照常上抛，不被说成“运行记录不存在”。
def test_subagent_run_still_raises_other_missing_files(tmp_path, monkeypatch):
    from agent_py_agent.cli import _dispatch

    agent = _agent(tmp_path)
    task = agent.subagents.create_run(goal="读一份资料", thought="", plan=["read"], role="worker")
    monkeypatch.setattr(_dispatch, "make_agent", lambda args: agent)

    def missing_prompt(*_args):
        raise FileNotFoundError("runner prompt file missing")

    monkeypatch.setattr(_dispatch, "_execute_subagent_run_cli", missing_prompt)
    args = SimpleNamespace(run_id=task.id, execute=False, instruction="", max_cards=0, no_probe=True)
    with pytest.raises(FileNotFoundError, match="runner prompt file missing"):
        _dispatch.cmd_subagent_run(args)


# 函数用途: 写一份指向临时 home 的最小配置，返回 skills proposals show 的参数。
def _proposal_args(tmp_path, proposal_id: str, *, json_output: bool):
    config = tmp_path / "config.yaml"
    config.write_text(f"model_backend: echo\nmy_agent_home: {tmp_path / 'home'}\n", encoding="utf-8")
    return SimpleNamespace(config=str(config), proposal_id=proposal_id, json=json_output)


# 函数用途: R14b——提案编号写错或不存在时，给出已登记的恢复提示、编号格式与 list 查看命令。
@pytest.mark.parametrize(("proposal_id", "code"), [
    ("nothex", "SKILL_PROPOSAL_INVALID_ID"),
    ("0123456789abcdef01234567", "SKILL_PROPOSAL_NOT_FOUND"),
])
def test_skill_proposal_id_errors_explain_format_and_next_step(tmp_path, capsys, proposal_id, code):
    from agent_py_agent.agent.contracts.error_taxonomy import error_contract
    from agent_py_agent.cli import skill_proposal_commands as proposals

    assert proposals.cmd_skill_proposals_show(_proposal_args(tmp_path, proposal_id, json_output=False)) == 1
    out = capsys.readouterr().out
    assert f"操作未完成：{code}" in out
    assert "24 位小写十六进制" in out and "my-agent skills proposals list" in out
    contract = error_contract(code)
    # 已登记的错误码附原恢复提示；未登记的码不借用“未知失败”的通用提示。
    assert (contract.recovery_hint in out) is (contract.code == code)


# 函数用途: R14b——--json 失败输出的机器字段保持不变。
def test_skill_proposal_json_error_fields_are_unchanged(tmp_path, capsys):
    from agent_py_agent.cli import skill_proposal_commands as proposals

    assert proposals.cmd_skill_proposals_show(_proposal_args(tmp_path, "nothex", json_output=True)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"command", "detail", "error_code", "message", "ok"}
    assert payload["error_code"] == "SKILL_PROPOSAL_INVALID_ID"


# 函数用途: R14c——--owner 写法不合法时报 OWNER_NOT_FOUND，并在人读输出里给出写法示例。
@pytest.mark.parametrize("owner", ["bogus", "local", "providers/feishu/ou_x", "local/main/extra"])
def test_credentials_report_owner_not_found_for_a_malformed_owner(capsys, owner):
    from agent_py_agent.cli import credential_commands

    args = SimpleNamespace(credentials_action="list", owner=owner, json=True)
    assert credential_commands.cmd_credentials(args) == 1
    assert json.loads(capsys.readouterr().out)["error_code"] == "OWNER_NOT_FOUND"
    args = SimpleNamespace(credentials_action="list", owner=owner, json=False)
    assert credential_commands.cmd_credentials(args) == 1
    text = capsys.readouterr().out
    assert f"owner={owner} error=OWNER_NOT_FOUND" in text
    assert "local/main" in text and "providers/<平台>/users/<用户ID>" in text


# 函数用途: R14c——凭据管理失败的人读输出带上异常类型。
def test_credentials_failure_line_names_the_error_type():
    from agent_py_agent.cli.credential_commands import _render

    text = _render("status", {"ok": False, "owner_id": "local/main", "error_code": "CREDENTIAL_MANAGEMENT_FAILED",
                              "error_type": "OSError"})
    assert "error=CREDENTIAL_MANAGEMENT_FAILED error_type=OSError" in text


# 函数用途: R14d——过了补跑宽限的一次性计划恢复失败时，中文原因写清目标时刻、当前时刻、宽限秒数与下一步。
def test_resuming_an_expired_one_shot_explains_times_grace_and_next_step(tmp_path):
    from agent_py_agent.agent.scheduler.schedule import ScheduleValidationError
    from agent_py_agent.tests.test_scheduler_repository import _create, _repository

    repository = _repository(tmp_path)
    job, _created = _create(repository, now=900, grace=60,
                            schedule={"kind": "at", "at": "1970-01-01T00:16:40Z", "timezone": "UTC"})
    paused = repository.pause_job(str(job["job_id"]), expected_version=int(job["version"]), now=950)
    with pytest.raises(ScheduleValidationError) as raised:
        repository.resume_job(str(job["job_id"]), expected_version=int(paused["version"]), now=2_000)
    message = str(raised.value)
    assert "1970-01-01T00:16:40Z" in message and "1970-01-01T00:33:20Z" in message
    assert f"{int(job['misfire_grace_seconds'])} 秒" in message
    assert "update" in message and "删除" in message


# 函数用途: R16——bench-model 展示、测速与保存都用 owner 当前选定的模型，而不是部署配置里的占位模型。
def test_bench_model_reports_and_measures_the_selected_model(tmp_path, monkeypatch, capsys):
    from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation
    from agent_py_agent.cli import bench_model
    from agent_py_agent.tests.test_model_profiles import Host, add

    host = Host(tmp_path)
    profile_id, _result = add(host)
    execute_model_profile_operation(host, "set_default", {"profile_id": profile_id})
    measured, saved = [], []
    sample = SimpleNamespace(input_tokens=1000, output_tokens=500, latency_seconds=2.0,
                             tokens_per_second=lambda: 250.0)
    monkeypatch.setattr(bench_model, "load_config", lambda path: host.config)
    monkeypatch.setattr(bench_model, "make_agent", lambda args: host)
    monkeypatch.setattr(bench_model, "run_speed_benchmark",
                        lambda config: measured.append(config) or SimpleNamespace(samples=[sample]))
    monkeypatch.setattr(bench_model, "save_speed_profile", lambda profile, path: saved.append(path))
    args = SimpleNamespace(config=str(tmp_path / "config.yaml"), profile=tmp_path / "speed.json", show=False)
    assert bench_model.cmd_bench_model(args) == 0
    out = capsys.readouterr().out
    assert "Backend: anthropic_compatible" in out and "Model: MiniMax-M2.7" in out
    assert "deployment-model" not in out
    assert [config.model_name for config in measured] == ["MiniMax-M2.7"]
    assert saved == [tmp_path / "speed.json"]
