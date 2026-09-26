# LLM: 保留原验证记录/仓库、归档、_record_tool_call、设置与模型目录、worker 和调用账；只替换决策后端的同步 decide。
# 模块用途: 离线验证交付复核焦点从真实验证事件到 text/native 展示的完整接线、独立默认配置和 TUI 设置入口。
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent_py_agent.agent.agent_core import _tool_loop_service as loop
from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.model.call_runtime import model_call_ledger
from agent_py_agent.agent.agent_core.tool_context import decision_delivery_quality as module
from agent_py_agent.agent.agent_core.tool_context.reducer import render_tool_result_for_live_prompt
from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallRecordParams
from agent_py_agent.agent.backends.message_adapter import AnthropicMessageAdapter
from agent_py_agent.agent.backends.typesafe_decision import TypesafeDecisionBackend
from agent_py_agent.agent.backends.typesafe_decision_wire import parse_typesafe_response
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.settings.decision_settings import (
    execute_decision_settings_operation as settings,
)
from agent_py_agent.agent.settings.decision_settings_schema import decision_field_scopes
from agent_py_agent.agent.tooling.runtime_contracts import ToolResult
from agent_py_agent.agent.verification.runtime import record_tool_verification
from agent_py_agent.cli.chat_parts import tui_decision_menu
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    canonical_history_result,
    make_test_model_spec,
    make_test_protocol_snapshot,
    runtime_snapshot_for_model_specs,
)
from agent_py_agent.tests.test_decision_model_profiles import decision
from agent_py_agent.tests.test_decision_settings import host_at, patch
from agent_py_agent.tests.test_tui_decision_menu import (
    Gateway,
    choose,
    open_scope,
    press,
    running,
    visible,
)


# LLM: 宿主沿原 owner 设置、模型目录与会话仓库；owner 根只放验证账和工具归档，不写正式用户配置。
# 函数用途: 建立开启指定模式的临时宿主和同一 run 的工具循环参数。
def verified_host(tmp_path, mode):
    host = host_at(tmp_path / "home")
    host.root = tmp_path / "owner"
    key, _ = decision(host)
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    patch(host, {"enabled": True, "profile_id": key, f"points.{module._POINT}.mode": mode})
    specs = (make_test_model_spec("run_command"), make_test_model_spec("edit_file"))
    params = ToolLoopExecuteParams(
        user_prompt="修复登录并在交付前说明验证情况", memories=[], runtime_injections=[], prompt_files=[],
        tool_catalog_section="", tool_recommendations_section="", tool_context=[], effective_on_chunk=None,
        allowed_tools=None, write_boundary=None, task_attributes={"agent_thread_id": thread.thread_id},
        request_id="request-1", run_id="run-1", task_id="task-1", save=False,
        one_shot_tool_calls=set(), executed_tools=[], archive_tool_calls=[],
        tool_protocol_snapshot=make_test_protocol_snapshot(run_id="run-1"),
        tool_runtime_snapshot=runtime_snapshot_for_model_specs(specs, run_id="run-1"),
    )
    host._current_run_params = params
    return host, params


# LLM: 项目根按原 project_facts 的 .git 与 pyproject 声明识别，pytest 因此是可分类的验证命令。
# 函数用途: 建立一个带有登录模块的最小 Python 项目目录。
def project_at(tmp_path):
    project = tmp_path / "project-private"
    (project / ".git").mkdir(parents=True)
    (project / "pyproject.toml").write_text("[tool.pytest.ini_options]\naddopts = '-q'\n", encoding="utf-8")
    (project / "login.py").write_text("print('login')\n", encoding="utf-8")
    return project


# LLM: 每步先经真实 record_tool_verification 写验证账/附事实，再进原 _record_tool_call；命令本身不执行。
# 函数用途: 依次记录“局部测试失败→改文件→全量测试失败”，交回三条记录和最后结果的原始快照。
def record_story(host, params, project):
    steps = (("run_command", {"command": "pytest tests/test_login.py", "working_dir": str(project)}),
             ("edit_file", {"path": str(project / "login.py")}),
             ("run_command", {"command": "pytest", "working_dir": str(project)}))
    records, before = [], None
    for index, (tool, arguments) in enumerate(steps, 1):
        failed = tool == "run_command"
        call = canonical_history_call(tool, arguments, call_id=f"call-{index}", run_id="run-1",
                                      turn_id=f"turn-{index}", attempt_id="attempt-1")
        details = {"process": {"status": "exited", "return_code": 1}} if failed else {"path": arguments["path"]}
        result = canonical_history_result(call, "1 failed private-output" if failed else "edited", ok=not failed,
                                          error_code="COMMAND_FAILED", handler_details=details)
        records.append(ToolCallRecordParams(params=params, tool_rounds=index, idx=1, call=call,
                                            result=record_tool_verification(host, call, result)))
        before = records[-1].result.to_dict()
        loop._record_tool_call(host, records[-1])
    return records, before


# LLM: 只读 owner 根下的实际文件字节，覆盖工具归档和验证 SQLite；不解析内容。
# 函数用途: 生成文件快照，比较决策前后是否有任何落盘变化。
def owner_files(host):
    return {str(path): path.read_bytes() for path in Path(host.root).rglob("*") if path.is_file()}


# LLM: 唯一替身是供应商同步 decide；请求仍经原设置、绑定、worker、响应解析与调用账，并在决策时刻快照归档与文件。
# 函数用途: 固定选择第一个焦点，记录实际请求供隐私与绑定断言。
def install_backend(monkeypatch, host, params):
    calls = []

    def invoke(backend, request, *, deadline):
        payload = request.payload(backend.model_name)
        calls.append({"request": request, "payload": payload, "files": owner_files(host),
                      "archives": json.dumps(params.archive_tool_calls, sort_keys=True, default=str)})
        criteria = payload["questions"][module._QUESTION]["criteria"]
        answer = {"type": "choice", "choice": "focus_1", "confidence": 0.8,
                  "probabilities": {key: float(key == "focus_1") for key in criteria}}
        return parse_typesafe_response(request, backend.model_name, {
            "model": "fixture-decision", "answers": {module._QUESTION: answer}, "usage": {"input_tokens": 37}})

    monkeypatch.setattr(TypesafeDecisionBackend, "decide", invoke)
    return calls


# 函数用途: 取出第三次调用在 text 历史、原生消息和 IR 中各自的展示正文。
def rendered_third_result(params):
    record = next(item for item in params.tool_context if item.startswith("[tool-record round=3 index=1]"))
    text = record.split("[tool-output-record round=3 index=1]\n", 1)[1]
    messages = AnthropicMessageAdapter().to_provider_messages(params.tool_ir_history)
    native = [block["content"] for message in messages if isinstance(message.get("content"), list)
              for block in message["content"] if block.get("type") == "tool_result"]
    results = [item for item in params.tool_ir_history if isinstance(item, ToolResult)]
    return text, native[2], results[2].render_for_model_prompt()


@pytest.mark.parametrize("mode", ["off", "observe", "apply"])
def test_real_verification_story_shares_one_hint_without_changing_facts(tmp_path, monkeypatch, mode):
    host, params = verified_host(tmp_path, mode)
    calls = install_backend(monkeypatch, host, params)
    project = project_at(tmp_path)
    records, before = record_story(host, params, project)
    first = records[0].result.metadata["handler_details"]["verification_evidence"]
    assert records[1].result.metadata["handler_details"]["verification_state"][0]["status"] == "stale"
    text, native, ir_text = rendered_third_result(params)
    hint = (f"[delivery-review-focus]\n可选复核建议：交付前可先复核本轮验证事件 #{first['id']}"
            "（test/targeted，failed，其后有修改）；范围与结果以原事实为准，targeted 不代表全量。")
    assert text == native == ir_text
    assert text == render_tool_result_for_live_prompt(records[2].result, params.archive_tool_calls[2]) + (
        "\n" + hint if mode == "apply" else "")
    assert records[2].result.to_dict() == before
    ledger = model_call_ledger(host).records()
    assert len(calls) == len(ledger) == (0 if mode == "off" else 1)
    assert [row.metadata["purpose"] for row in ledger] == ["decision"] * len(ledger)
    for call in calls:
        assert_request_is_bound_and_private(call, params, str(tmp_path))
        assert call["files"] == owner_files(host)
        assert call["archives"] == json.dumps(params.archive_tool_calls, sort_keys=True, default=str)


# LLM: 请求只绑定当前 run/thread/归档引用；外发 payload 不含本地路径、原命令或工具输出。
# 函数用途: 核对一次真实请求的绑定与隐私边界。
def assert_request_is_bound_and_private(call, params, private_root):
    binding = call["request"].binding
    assert (binding.point, binding.run_id, binding.thread_id) == (
        module._POINT, "run-1", params.task_attributes["agent_thread_id"])
    assert binding.source_refs == (params.archive_tool_calls[2]["scoped_call_id"],)
    payload = json.dumps(call["payload"], ensure_ascii=False)
    for private in (private_root, "project-private", "pytest", "private-output", "login.py"):
        assert private not in payload
    focuses = call["payload"]["state"]["focuses"]
    assert [(row["scope"], row["status"], row["edited_after"]) for row in focuses] == [
        ("targeted", "failed", True), ("full", "failed", False)]


def test_config_defaults_are_off_and_thread_scoped(tmp_path):
    config = load_config(Path(__file__).parents[1] / "config" / "agent_config.yaml")
    defaults = AgentConfig()
    for suffix in ("mode", "timeout_seconds", "profile_id"):
        name = f"decision_{module._POINT}_{suffix}"
        assert getattr(config, name) == getattr(defaults, name) == ("off" if suffix == "mode" else None)
        assert decision_field_scopes()[f"points.{module._POINT}.{suffix}"] == ["owner", "thread"]
    host = host_at(tmp_path)
    thread = host.conversation_store.threads.get_or_create({"canonical_user_id": "alice", "owner_id": "alice"})
    view = settings(host, "read", {"scope": "thread"}, thread_id=thread.thread_id)
    point = view["effective"]["points"][module._POINT]
    assert point["mode"] == point["effective_mode"] == "off" and point["runtime_scope"] == "thread"
    patch(host, {"enabled": True, f"points.{module._POINT}.mode": "apply"}, thread_id=thread.thread_id, scope="thread")
    view = settings(host, "read", {"scope": "thread"}, thread_id=thread.thread_id)
    assert view["effective"]["points"][module._POINT]["effective_mode"] == "apply"
    assert settings(host, "read", {})["effective"]["points"][module._POINT]["mode"] == "off"


def test_original_tui_can_edit_delivery_quality_thread_mode(tmp_path):
    async def scenario():
        gateway = Gateway(tmp_path)
        async with running(tmp_path, gateway) as ui:
            await open_scope(ui, thread=True)
            await choose(ui, 5)
            view = settings(gateway.host, "read", {"scope": "thread"}, thread_id=gateway.thread.thread_id)
            points = [key for key in tui_decision_menu._POINTS if f"points.{key}.mode" in tui_decision_menu._fields(view)]
            assert "交付复核焦点" in visible(ui.app)
            await choose(ui, points.index(module._POINT))
            await choose(ui, 0)
            # 模式为"开启 + 观察模式"两个勾选：勾上开启、去掉观察即正式使用（apply），再 Tab 到保存
            await press(ui, b" \x1b[B ")
            await press(ui, b"\t\r")
            result = settings(gateway.host, "read", {"scope": "thread"}, thread_id=gateway.thread.thread_id)
            assert result["overrides"]["thread"][f"points.{module._POINT}.mode"] == "apply"
            assert not any(name in {"decision_probe", "select", "set_default"} for name, _payload in gateway.calls)
    asyncio.run(scenario())
