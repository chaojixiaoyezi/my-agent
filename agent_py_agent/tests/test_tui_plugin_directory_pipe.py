from __future__ import annotations

import asyncio
import threading
from dataclasses import replace

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from agent_py_agent.agent.plugin_command_catalog import PluginCommandCatalog
from agent_py_agent.agent.plugin_command_service import execute_plugin_command
from agent_py_agent.cli.chat_parts import plugin_command_client, plugin_command_stream, rendering
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.cli.chat_parts.tui_ui_setup import make_tui_app
from agent_py_agent.tests.test_plugin_command_catalog import _plugin
from agent_py_agent.tests.test_tui_prompt_toolkit_pipe import _app_params


@pytest.mark.parametrize("restore", ["manual", "after_command"])
def test_native_keybindings_refresh_accept_stash_and_submit_original_revision(
    tmp_path, monkeypatch, restore
):
    initial = PluginCommandCatalog("scope", plugins=(_plugin(),))
    current = initial
    requests = []
    event_thread = threading.get_ident()

    def transport(port, owner, path, payload, **kwargs):
        assert threading.get_ident() != event_thread
        requests.append(payload)
        return 200, (
            {"ok": True, "catalog": current.to_payload()}
            if payload["operation"] == "catalog"
            else execute_plugin_command(
                current, payload["command"], revision=payload["catalog_revision"]
            )
        )

    def command_stream(port, owner, payload, interaction):
        assert payload["plugin_request_id"] == interaction.request_id
        assert interaction.gateway_paths is not None
        assert not interaction.cancellation_token.cancelled
        return transport(port, owner, "/client/plugins", payload)[1]

    monkeypatch.setattr(plugin_command_client, "post_gateway_json", transport)
    monkeypatch.setattr(plugin_command_stream, "post_plugin_command_stream", command_stream)
    monkeypatch.setattr(rendering, "_TUI_OUTPUT_SINK", None)

    async def scenario():
        nonlocal current
        runtime = TuiRuntime("directory-pipe")
        runtime.publish_session(version="test", model="fixture", workspace=str(tmp_path))
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                params = replace(_app_params(tmp_path, runtime), use_gateway=True)
                params.paths.root = tmp_path / "gateway"
                app = make_tui_app(params)
                run_task = asyncio.create_task(app.run_async())
                try:
                    await asyncio.sleep(0.06)
                    buffer = app.current_buffer
                    binding = buffer._my_agent_plugin_input
                    pipe.send_text("/plugins@s")
                    await asyncio.sleep(0.06)
                    assert requests == [] and buffer.complete_state is None
                    pipe.send_bytes(b"\t")
                    for _ in range(100):
                        if buffer.complete_state is not None:
                            break
                        await asyncio.sleep(0.01)
                    assert len(requests) == 1 and requests[0]["operation"] == "catalog"
                    assert buffer.complete_state is not None
                    assert buffer.text == "/plugins@s" and params.jobs.empty()
                    pipe.send_bytes(b"\t")
                    await asyncio.sleep(0.06)
                    assert (
                        buffer.text == "/plugins@sample " and binding.revision == initial.revision
                    )
                    buffer.cancel_completion()
                    pipe.send_bytes(b"\x13")
                    await asyncio.sleep(0.06)
                    assert buffer.text == "" and binding.revision == ""
                    current = PluginCommandCatalog(
                        "scope", plugins=(replace(_plugin(), package_version="2.0"),)
                    )
                    if restore == "manual":
                        await asyncio.to_thread(binding.client.refresh)
                        pipe.send_bytes(b"\x13")
                    else:
                        pipe.send_text("/plugins help")
                        pipe.send_bytes(b"\r")
                    await asyncio.sleep(0.12)
                    assert binding.client.snapshot() == current
                    assert (
                        buffer.text == "/plugins@sample " and binding.revision == initial.revision
                    )
                    pipe.send_bytes(b"\x12")
                    await asyncio.sleep(0.06)
                    pipe.send_bytes(b"\x03")
                    await asyncio.sleep(0.06)
                    assert app.current_buffer is buffer and binding.revision == initial.revision
                    pipe.send_text("read '中文 文件'")
                    pipe.send_bytes(b"\r")
                    for _ in range(100):
                        if requests[-1].get("command", "").startswith("/plugins@sample read"):
                            break
                        await asyncio.sleep(0.01)
                    command = requests[-1]
                    assert command["command"] == "/plugins@sample read '中文 文件'"
                    assert command["catalog_revision"] == initial.revision != current.revision
                    assert buffer.text == "" and params.jobs.empty()
                    assert binding.revision == ""
                    assert not params.stop_event.is_set() and params.pending_jobs_ref == [0]
                finally:
                    app.exit(result=0)
                    assert await run_task == 0

    asyncio.run(scenario())
