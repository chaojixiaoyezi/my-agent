"""TUI 单独输入 /effort 打开智能程度菜单：真实按键选档后经控制出站箱发出 /effort <档位>；Esc 不发任何命令；
带参数的文字形式照旧直接发给 Gateway。"""
import asyncio
import dataclasses
import threading
from types import SimpleNamespace

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent_py_agent.agent.backends.reasoning_control import REASONING_LEVELS
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.cli.chat_parts import tui_actions
from agent_py_agent.cli.chat_parts.tui_effort_menu import effort_menu_rows
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.chat_parts.tui_ui_setup import make_tui_app
from agent_py_agent.tests.test_tui_decision_menu import wait_app, wait_dialog_ready
from agent_py_agent.tests.test_tui_prompt_toolkit_pipe import _app_params

TITLE = "智能程度 /effort（只改本会话）"


class _Outbox:
    def __init__(self) -> None:
        self.entries: list[object] = []

    def enqueue(self, entry, *, on_persisted_before_dispatch=None) -> None:
        self.entries.append(entry)


def test_every_menu_row_is_a_valid_effort_command():
    rows = effort_menu_rows()
    assert [value for value, _ in rows] == ["status", *REASONING_LEVELS, "default", "probe"]
    parsed = {value: parse_conversation_control(f"/effort {value}", reject_unknown_slash=True) for value, _ in rows}
    assert all(command.kind == "effort" and command.valid for command in parsed.values())
    assert parsed["status"].operation == "view" and parsed["probe"].operation == "probe"
    assert {parsed[level].operation for level in (*REASONING_LEVELS, "default")} == {"set"}


def test_bare_effort_stays_local_while_text_forms_enter_the_outbox():
    outbox = _Outbox()
    params = SimpleNamespace(use_gateway=True, state_lock=threading.Lock(), is_running_ref=[False],
                             running_request_id_ref=[""], tui_runtime=TuiRuntime("effort-text"),
                             control_operation_reconciler=outbox)
    assert tui_actions._tui_submit_control_operation(params, "/effort") is False
    assert outbox.entries == []
    for text in ("/effort low", "/effort status", "/effort default"):
        assert tui_actions._tui_submit_control_operation(params, text) is True
    assert [(entry.command_kind, entry.command_text) for entry in outbox.entries] == [
        ("effort", "/effort low"), ("effort", "/effort status"), ("effort", "/effort default")]


def test_typing_bare_effort_opens_the_menu_and_arrow_keys_pick_a_level(tmp_path, monkeypatch):
    outbox = _Outbox()
    monkeypatch.setattr(tui_actions, "_ensure_control_operation_reconciler", lambda _params: outbox)

    async def scenario() -> None:
        runtime = TuiRuntime("effort-menu")
        runtime.publish_session(version="test", model="fixture", workspace=str(tmp_path))
        params = dataclasses.replace(_app_params(tmp_path, runtime), use_gateway=True,
                                     paths=SimpleNamespace(root=tmp_path / "gateway"))
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            app = make_tui_app(params)
            runner = asyncio.create_task(app.run_async())
            try:
                await wait_app(app, lambda: app.is_running, "TUI 启动")
                pipe.send_text("/effort")
                pipe.send_bytes(b"\r")
                await wait_dialog_ready(app, TITLE, "智能程度菜单")
                assert outbox.entries == []  # 只打开菜单，还没发任何命令
                pipe.send_bytes(b"\x1b[B\x1b[B\x1b[B\r")  # 查看 → auto → off → low
                await wait_app(app, lambda: len(outbox.entries) == 1, "选中后提交")
                assert (outbox.entries[0].command_kind, outbox.entries[0].command_text) == ("effort", "/effort low")
                await wait_app(app, lambda: not app._my_agent_model_float_container.floats, "菜单关闭")
                assert app._my_agent_model_menu_active is False

                pipe.send_text("/effort")
                pipe.send_bytes(b"\r")
                await wait_dialog_ready(app, TITLE, "再次打开菜单")
                pipe.send_bytes(b"\x1b")
                await wait_app(app, lambda: not app._my_agent_model_float_container.floats, "Esc 关闭菜单")
                await asyncio.sleep(0.1)
                assert len(outbox.entries) == 1  # Esc 不提交
                assert app._my_agent_model_menu_active is False
            finally:
                app.exit()
                await runner
                params.stop_event.set()

    asyncio.run(scenario())
