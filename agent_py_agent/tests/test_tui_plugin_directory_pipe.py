from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent_py_agent.agent.plugin_command_catalog import PluginCommandCatalog
from agent_py_agent.cli.chat_parts import rendering
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.chat_parts.tui_ui_setup import make_tui_app
from agent_py_agent.tests._gateway_stub import plugin_command_responder
from agent_py_agent.tests.test_plugin_command_catalog import _plugin
from agent_py_agent.tests.test_tui_decision_menu import wait_app
from agent_py_agent.tests.test_tui_prompt_toolkit_pipe import _app_params


@pytest.mark.parametrize("restore", ["manual", "after_command"])
def test_native_keybindings_refresh_accept_stash_and_submit_original_revision(
    tmp_path, monkeypatch, gateway_stub_factory, restore
):
    initial = PluginCommandCatalog("scope", plugins=(_plugin(),))
    current = initial

    # G3 回归：mock 下移到本机假 Gateway，让真实 HTTP 传输与凭据分流参与目录和命令派发。
    def respond(path, headers, payload):
        assert path == "/client/plugins"
        return plugin_command_responder(lambda: current)(path, headers, payload)

    stub = gateway_stub_factory(respond)
    monkeypatch.setattr(rendering, "_TUI_OUTPUT_SINK", None)

    async def scenario():
        nonlocal current
        runtime = TuiRuntime("directory-pipe")
        runtime.publish_session(version="test", model="fixture", workspace=str(tmp_path))
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                params = replace(_app_params(tmp_path, runtime), use_gateway=True)
                params.paths.root = tmp_path / "gateway"
                params.paths.processing = tmp_path / "gateway" / "processing"
                # 真实传输要连本机假 Gateway；隔离凭据根，避免读取真实宿主凭据。
                params.agent.config.gateway_port = stub.port
                params.agent.home_paths = SimpleNamespace(root=tmp_path / "host-data")
                app = make_tui_app(params)
                run_task = asyncio.create_task(app.run_async())
                try:
                    buffer = app.current_buffer
                    binding = buffer._my_agent_plugin_input
                    # 起跑和首帧在负载下可能超过固定睡眠；键会留在管道里等应用读取，后面的候选等待才会误判超时。
                    await wait_app(app, lambda: app.is_running and app.render_counter > 0, "TUI 起跑并画出首帧")
                    pipe.send_text("/plugins@s")
                    await wait_app(app, lambda: buffer.text == "/plugins@s" and buffer.complete_state is None,
                                   "输入到达且边打边补全不留候选")
                    assert stub.requests == []
                    pipe.send_bytes(b"\t")
                    # Tab 先同步建一个空的 complete_state，再到线程里拉目录；候选真的出现才说明目录请求已发出。
                    await wait_app(app, lambda: bool(getattr(buffer.complete_state, "completions", ())),
                                   "Tab 拉取目录后出现候选")
                    assert len(stub.requests) == 1 and stub.requests[0].payload["operation"] == "catalog"
                    assert buffer.text == "/plugins@s" and params.jobs.empty()
                    pipe.send_bytes(b"\t")
                    await wait_app(app, lambda: buffer.text == "/plugins@sample ", "接受候选并补空格")
                    assert binding.revision == initial.revision
                    buffer.cancel_completion()
                    pipe.send_bytes(b"\x13")
                    await wait_app(app, lambda: buffer.text == "", "Ctrl-S 暂存草稿")
                    assert binding.revision == ""
                    current = PluginCommandCatalog(
                        "scope", plugins=(replace(_plugin(), package_version="2.0"),)
                    )
                    if restore == "manual":
                        await asyncio.to_thread(binding.client.refresh)
                        pipe.send_bytes(b"\x13")
                    else:
                        pipe.send_text("/plugins help")
                        pipe.send_bytes(b"\r")
                    # /plugins help 在 asyncio.to_thread 里执行并刷新目录；按状态等，不赌固定时长。
                    await wait_app(app, lambda: binding.client.snapshot() == current and buffer.text == "/plugins@sample ",
                                   "插件目录刷新且草稿恢复")
                    assert binding.client.snapshot() == current
                    assert (
                        buffer.text == "/plugins@sample " and binding.revision == initial.revision
                    )
                    pipe.send_bytes(b"\x12")
                    await wait_app(app, lambda: app.current_buffer is not buffer, "Ctrl-R 进入历史搜索")
                    pipe.send_bytes(b"\x03")
                    await wait_app(app, lambda: app.current_buffer is buffer, "Ctrl-C 退出历史搜索")
                    assert binding.revision == initial.revision
                    pipe.send_text("read '中文 文件'")
                    pipe.send_bytes(b"\r")
                    await wait_app(app, lambda: bool(stub.requests)
                                   and str(stub.requests[-1].payload.get("command", "")).startswith("/plugins@sample read"),
                                   "命令按原 revision 提交")
                    command = stub.requests[-1].payload
                    assert command["command"] == "/plugins@sample read '中文 文件'"
                    assert command["catalog_revision"] == initial.revision != current.revision
                    assert buffer.text == "" and params.jobs.empty()
                    assert binding.revision == ""
                    assert not params.stop_event.is_set() and params.pending_jobs_ref == [0]
                finally:
                    app.exit(result=0)
                    assert await run_task == 0

    asyncio.run(scenario())
