# LLM: 使用原携带包归一化及新循环入口验证 I5；仅检查结构化媒体引用，不读图片、不访问供应商或增加授权。
# 模块用途: 防止活动插话在 Compact 携带与 IR 重建时静默丢图。
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_support import _native_initial_tool_ir_history
from agent_py_agent.agent.backends.tool_ir import UserTurn
from agent_py_agent.agent.conversation.active_turn_input import merge_active_turn_user_inputs


# LLM: 合成引用只作为纯投影输入；不声称经过 owner 校验，不执行文件读取。
# 函数用途: 构造一条有稳定输入身份、正文和单图引用的插话携带包。
def _media_packet():
    digest = "a" * 64
    return {
        "schema_version": "active-turn-user-input.v1",
        "input_ids": ["guidance-image-1"],
        "text": "请结合这张图片继续。",
        "input_media": [{
            "path": f"/synthetic-owner/media/input/{digest}",
            "sha256": digest,
            "media_type": "image/png",
            "size_bytes": 3,
            "name": "photo.png",
        }],
    }


# LLM: I5 的原携带归一化不能将带图用户输入降成文字；比较完整引用及输入身份，不只检查字段存在。
# 函数用途: 验证 Compact 的同源包合并保留图片且重试不复制输入。
def test_i5_compact_packet_merge_preserves_media_and_identity():
    packet = _media_packet()
    merged = merge_active_turn_user_inputs([packet], [packet])
    assert len(merged) == 1
    assert merged[0]["input_ids"] == packet["input_ids"]
    assert merged[0]["text"] == packet["text"]
    assert merged[0].get("input_media") == packet["input_media"]


# LLM: I5 使用实际新循环 IR 构造入口，不替换归一化或 UserTurn；没有初始任务媒体可冒充插话附件。
# 函数用途: 验证 Compact 后重建的插话用户轮仍带原图、原输入身份及原时序。
def test_i5_new_loop_rebuilds_media_on_the_original_user_turn():
    packet = _media_packet()
    params = SimpleNamespace(
        user_prompt="原任务",
        task_attributes={},
        turn_trigger=None,
        carried_active_turn_user_inputs=[packet],
    )
    history = _native_initial_tool_ir_history(
        params, carried_handoff="", carried_user_inputs=[packet["text"]],
    )
    assert len(history) == 2
    assert isinstance(history[-1], UserTurn)
    assert history[-1].input_ids == tuple(packet["input_ids"])
    assert history[-1].text == packet["text"]
    assert not history[0].media
    assert history[-1].media == tuple(packet["input_media"])


# LLM: I1 只验证原 coordinator 的选项准入；媒体引用自身的文件与 owner 验证仍在宿主写入边界。
# 函数用途: 图片变化不应把等价执行选项的普通插话强制排成新任务。
def test_i1_media_is_input_not_an_execution_option_change():
    from agent_py_agent.agent.gateway_parts.input_target_observation import (
        active_text_options_match,
    )

    current = {"kind": "ask", "goal": "正在处理"}
    incoming = {**current, "input_media": _media_packet()["input_media"]}
    assert active_text_options_match(incoming, current)
    assert not active_text_options_match({**incoming, "prompt_files": ["extra.md"]}, current)
    assert not active_text_options_match({**incoming, "workspace": "/another"}, current)


# LLM: I1/I5 真实 guidance packet 投影必须保留媒体，不能通过只在测试构造带图包掩盖生产入口丢图。
# 函数用途: 验证已领取条目进入原携带协议时保留附件。
def test_i1_guidance_packet_carries_media():
    from agent_py_agent.agent.conversation.active_turn_input import packet_from_guidance

    packet = _media_packet()
    entry = SimpleNamespace(guidance_id=packet["input_ids"][0], metadata={"input_media": packet["input_media"]})
    result = packet_from_guidance([entry], packet["text"])
    assert result["input_ids"] == packet["input_ids"]
    assert result.get("input_media") == packet["input_media"]


# LLM: I3 同身份换图必须失败；不得把图片不一致伪装成幂等重试成功。
# 函数用途: 锁定 Compact 合并时同输入身份的附件冲突。
def test_i3_same_identity_with_changed_media_is_a_conflict():
    import pytest

    packet = _media_packet()
    changed = {**packet, "input_media": [{**packet["input_media"][0], "name": "different.png"}]}
    with pytest.raises(ValueError, match="冲突"):
        merge_active_turn_user_inputs([packet], [changed])


# LLM: I5 ACK 投影直接调用原实现，只替换原存储写口收集参数；不把 append 调用当作模型已确认。
# 函数用途: 验证已确认条目写入 transcript 时仍包含附件小引用。
def test_i5_ack_projection_preserves_media_refs():
    from agent_py_agent.agent.conversation.store_guidance_acknowledgements import (
        GuidanceAcknowledgements,
    )

    packet = _media_packet()
    rows = []
    projection = SimpleNamespace(_append_message_once=lambda row, **options: rows.append(row))
    entry = SimpleNamespace(
        guidance_id="guidance-image-1", target_type="request", target_id="turn-1", message=packet["text"],
        metadata={"record_in_transcript": True, "thread_id": "thread-1", "input_media": packet["input_media"]},
    )
    assert GuidanceAcknowledgements.project_transcript(projection, entry)
    assert rows[0]["metadata"].get("input_media") == packet["input_media"]


# LLM: I1 调用实际安全点投影，协议快照固定 native；媒体只用合成引用，完整 owner 验证另测。
# 函数用途: 验证插话进入原生 IR 和 Compact 携带包两个投影时都保留原图。
def test_i1_injection_preserves_media_in_ir_and_carrier():
    from agent_py_agent.agent.agent_core.runtime.guidance import _inject_claimed_guidance
    from agent_py_agent.agent.tooling.runtime_contracts import (
        ProviderToolCapability,
        ToolProtocolSnapshot,
    )

    packet = _media_packet()
    capability = ProviderToolCapability("test", "local://test", "test", False, True, "test")
    params = SimpleNamespace(
        tool_ir_history=[], active_turn_user_inputs=[], live_archive_state={},
        tool_protocol_snapshot=ToolProtocolSnapshot("run", "native", capability),
    )
    entry = SimpleNamespace(
        guidance_id=packet["input_ids"][0], message=packet["text"], sender="user", priority="high",
        metadata={"kind": "active_turn_user_input", "record_in_transcript": True,
                  "channel_message_id": "message-1", "input_media": packet["input_media"]},
    )
    _inject_claimed_guidance(params, [entry], [])
    assert params.tool_ir_history[-1].media == tuple(packet["input_media"])
    assert params.active_turn_user_inputs[0]["input_media"] == packet["input_media"]


# LLM: I1 覆盖真实 HTTP 转交函数，不让模型从 metadata 取得附件授权；这里只捕获路由出的 scope。
# 函数用途: 验证 HTTP 带图输入没有在转交活动回合时被截成纯文字。
def test_i1_http_route_retains_top_level_media(monkeypatch):
    from agent_py_agent.agent.gateway_parts import http_handlers as http

    scopes = []
    monkeypatch.setattr(http, "steer_active_conversation_if_running",
                        lambda *args, **options: scopes.append(options["scope"]))
    packet = _media_packet()
    http._route_ask_to_active_turn(SimpleNamespace(agent=object(), paths=object()),
        body={"conversation_id": "thread", "input_media": packet["input_media"]},
        goal=packet["text"], user_id="user", channel="chat")
    assert getattr(scopes[0], "input_media", ()) == tuple(packet["input_media"])


# LLM: I5 恢复只读空工具归档，不重放工具；真实恢复函数仍必须把原包的媒体送回 IR。
# 函数用途: 验证换后端后从 Compact 携带包恢复的用户输入仍带图片。
def test_i5_backend_switch_restore_preserves_media(monkeypatch):
    from agent_py_agent.agent.agent_core.model.selection_history import _restore_tool_history
    from agent_py_agent.agent.conversation import active_turn_compact

    monkeypatch.setattr(active_turn_compact, "model_visible_active_turn_tool_calls", lambda *args, **opts: [])
    packet = _media_packet()
    params = SimpleNamespace(tool_ir_history=[], task_attributes={}, archive_tool_calls=[],
        compact_context=SimpleNamespace(view=SimpleNamespace(summary="")), active_turn_user_inputs=[packet])
    history = _restore_tool_history(object(), params)
    assert history[0].media == tuple(packet["input_media"])
    assert history[0].input_ids == tuple(packet["input_ids"])


# LLM: I6 仅测试真实协议生成入口的不支持路径，不把合成引用当作供应商验收。
# 函数用途: 不支持图片的当前模型必须明确失败，不能忽略附图继续发纯文。
@pytest.mark.parametrize("protocol", ["native", "text"])
def test_i6_unsupported_model_cannot_silently_project_media(protocol):
    import pytest

    from agent_py_agent.agent.agent_core.tool_model_generation import _native_provider_messages
    from agent_py_agent.agent.conversation.input_media import InputMediaError
    from agent_py_agent.agent.tooling.runtime_contracts import (
        ProviderToolCapability,
        ToolProtocolSnapshot,
    )

    packet = _media_packet()
    capability = ProviderToolCapability("test", "local://test", "test", False, True, "test")
    params = SimpleNamespace(tool_ir_history=[UserTurn(packet["text"], media=tuple(packet["input_media"]))] if protocol == "native" else [],
        active_turn_user_inputs=[packet], task_attributes={},
        tool_protocol_snapshot=ToolProtocolSnapshot("run", protocol, capability), live_archive_state={})
    with pytest.raises(InputMediaError, match="图片"):
        _native_provider_messages(SimpleNamespace(config=SimpleNamespace(model_input_modalities=["text"]),
            backend=SimpleNamespace(supports_native_tool_ir=protocol == "native")), params)


# LLM: 空模态声明为未知而非拒绝；构造真实初始/插话 IR 后走原出站投影，不发请求或读取合成图片。
# 函数用途: 默认原生协议允许初始图片与运行中插话图片，并保持各自媒体与输入身份。
@pytest.mark.parametrize("interjected", [False, True], ids=["initial", "interjected"])
@pytest.mark.parametrize("modalities", [[], ()], ids=["empty-list", "empty-tuple"])
def test_i6_unknown_empty_modalities_allows_initial_and_interjected_images_on_native_protocol(interjected, modalities):
    from agent_py_agent.agent.agent_core.tool_model_generation import _native_provider_messages
    from agent_py_agent.agent.tooling.runtime_contracts import (
        ProviderToolCapability,
        ToolProtocolSnapshot,
    )

    packet = _media_packet()
    params = SimpleNamespace(user_prompt="原任务图片", turn_trigger=None,
        task_attributes={} if interjected else {"input_media": packet["input_media"]},
        carried_active_turn_user_inputs=[packet] if interjected else [], live_archive_state={},
        active_turn_user_inputs=[packet] if interjected else [],
        tool_protocol_snapshot=ToolProtocolSnapshot("run", "native",
            ProviderToolCapability("test", "local://test", "test", False, True, "test")))
    params.tool_ir_history = _native_initial_tool_ir_history(params, carried_handoff="",
        carried_user_inputs=[packet["text"]] if interjected else [])
    agent = SimpleNamespace(config=SimpleNamespace(model_input_modalities=modalities),
        backend=SimpleNamespace(supports_native_tool_ir=True))
    messages = _native_provider_messages(agent, params)
    assert messages is not None
    images = [part for row in messages for part in row["content"] if isinstance(part, dict) and part.get("type") == "image"]
    assert images == [{"type": "image", "source": {"type": "local_file", **packet["input_media"][0]}}]
    assert params.tool_ir_history[-1].media == tuple(packet["input_media"])
    if interjected:
        assert params.tool_ir_history[-1].input_ids == tuple(packet["input_ids"])


# LLM: I1/I3/I5/I6 经真实回环 HTTP、原 Store/claim/safe-point/ACK 和供应商转换器；不访问商业模型，不证明真机剪贴板。
# 函数用途: 验证工作中带图输入没有普通 job，幂等重放只消费一次且模型消息含原图字节。
def test_i1_http_to_safe_point_to_provider_media_and_ack(tmp_path, monkeypatch):
    import base64

    from agent_py_agent.agent.agent_core.runtime import guidance
    from agent_py_agent.agent.backends.message_adapter import AnthropicMessageAdapter
    from agent_py_agent.agent.conversation.input_media import (
        import_input_media_bytes,
        input_media_root,
        provider_media_messages,
    )
    from agent_py_agent.agent.gateway_parts import input_delivery_service as inputs
    from agent_py_agent.tests.test_gateway_active_input_transport import (
        foreground,
        params_for,
        send,
    )

    with foreground(tmp_path, monkeypatch) as (agent, _b, gate, _c, _r, server, claim, thread, _lane):
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN1cAAAAASUVORK5CYII=")
        ref = import_input_media_bytes(png, input_media_root(agent), name="pixel.png")
        code, result = send(server, message="image-on-active", input_media=[ref])
        assert code == 202 and result["input_state"] == "pending"
        assert send(server, message="image-on-active", input_media=[ref])[1]["request_id"] == result["request_id"]
        changed = dict(ref, name="changed.png")
        assert send(server, message="image-on-active", input_media=[changed])[0] == 409
        assert not (gate.paths.inbox / (result["request_id"] + ".json")).exists()
        params = params_for(agent, claim, thread)
        assert guidance.inject_pending_turn_input(agent, params)
        assert params.active_turn_user_inputs[0].get("input_media") == [ref]
        messages = AnthropicMessageAdapter().to_provider_messages(params.tool_ir_history)
        wire = provider_media_messages(messages)
        sources = [part["source"] for row in wire for part in row["content"] if part.get("type") == "image"]
        assert len(sources) == 1 and base64.b64decode(sources[0]["data"]) == png
        assert guidance.mark_injected_turn_input_submitted(agent, params, provider_call_id="fixture-call") == 1
        assert guidance.acknowledge_injected_turn_input(agent, params) == 1
        assert not guidance.inject_pending_turn_input(agent, params)
        assert inputs.reconcile_gateway_input_request(gate.paths, request_id=result["request_id"],
            conversation_store=agent.conversation_store) == "consumed"
        rows = agent.conversation_store.messages.recent(thread.thread_id)
        assert any(row.metadata.get("input_media") == [ref] for row in rows)


# LLM: 篡改仅发生在 pytest 临时 owner；实际安全点必须在 guidance 落账前校验，不能只测导入器已拒绝。
# 函数用途: 验证活动输入的跨 owner、同长度篡改、符号链接、数量越界和损坏引用都明确失败。
@pytest.mark.parametrize("fault", ["owner", "hash", "symlink", "count", "broken"])
def test_i4_active_media_validation_before_guidance_append(tmp_path, monkeypatch, fault):
    import base64
    from pathlib import Path

    from agent_py_agent.agent.agent_core.runtime import guidance
    from agent_py_agent.agent.conversation.input_media import (
        InputMediaError,
        import_input_media_bytes,
        input_media_root,
    )
    from agent_py_agent.tests.test_gateway_active_input_transport import (
        foreground,
        params_for,
        send,
    )

    with foreground(tmp_path, monkeypatch) as (agent, _b, gate, _c, _r, server, claim, thread, _lane):
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN1cAAAAASUVORK5CYII=")
        root = input_media_root(agent)
        ref = import_input_media_bytes(png, root, name="test.png")
        refs = [ref]
        if fault == "owner":
            refs = [import_input_media_bytes(png, tmp_path / "other-owner" / "media" / "input", name="test.png")]
        elif fault == "hash":
            Path(ref["path"]).write_bytes(b"x" * len(png))
        elif fault == "symlink":
            link = root / "link.png"
            link.symlink_to(Path(ref["path"]))
            refs = [{**ref, "path": str(link)}]
        elif fault == "count":
            refs = [ref] * 100
        else:
            refs = [{"path": ref["path"]}]
        code, result = send(server, message=f"bad-{fault}", input_media=refs)
        assert code == 202
        with pytest.raises(InputMediaError):
            guidance.inject_pending_turn_input(agent, params_for(agent, claim, thread))
        assert not agent.conversation_store.guidance.pending("request", claim.request_id)
        assert not (gate.paths.inbox / (result["request_id"] + ".json")).exists()


# LLM: I4 用真实另一目录媒体校验活动写入入口；原 receipt 可以保留待处置，绝不能进入 guidance 或用户 IR。
# 函数用途: 验证跨 owner 的媒体不会因活动插话绕过普通图片归属检查。
def test_i4_foreign_owner_media_never_enters_guidance(tmp_path, monkeypatch):
    import pytest

    from agent_py_agent.agent.agent_core.runtime import guidance
    from agent_py_agent.agent.conversation.input_media import (
        InputMediaError,
        import_input_media_bytes,
    )
    from agent_py_agent.tests.test_gateway_active_input_transport import (
        foreground,
        params_for,
        send,
    )

    with foreground(tmp_path, monkeypatch) as (agent, _b, _g, _c, _r, server, claim, thread, _lane):
        ref = import_input_media_bytes(b"foreign", tmp_path / "foreign" / "media" / "input")
        assert send(server, message="foreign-image", input_media=[ref])[0] == 202
        params = params_for(agent, claim, thread)
        with pytest.raises(InputMediaError):
            guidance.inject_pending_turn_input(agent, params)
        assert not params.active_turn_user_inputs
        assert not agent.conversation_store.guidance.pending("request", claim.request_id)


# LLM: 同批认领两条正文相同但图片不同的输入；每个 UserTurn 必须保持自己原来的身份和图片顺序。
# 函数用途: 防止一批插话被合成一个无图片归属的大包。
def test_i3_two_inputs_keep_separate_media_ownership():
    from agent_py_agent.agent.agent_core.runtime.guidance import _inject_claimed_guidance
    from agent_py_agent.agent.tooling.runtime_contracts import (
        ProviderToolCapability,
        ToolProtocolSnapshot,
    )

    capability = ProviderToolCapability("test", "local://test", "test", False, True, "test")
    params = SimpleNamespace(tool_ir_history=[], active_turn_user_inputs=[], live_archive_state={},
        tool_protocol_snapshot=ToolProtocolSnapshot("run", "native", capability))
    entries = [SimpleNamespace(guidance_id=f"guidance-{index}", message="同文", sender="user", priority="high",
        metadata={"kind": "active_turn_user_input", "record_in_transcript": True,
            "channel_message_id": f"message-{index}",
            "input_media": [{**_media_packet()["input_media"][0], "name": f"{index}.png"}]}) for index in range(2)]
    _inject_claimed_guidance(params, entries, [])
    assert [row.input_ids for row in params.tool_ir_history] == [("guidance-0",), ("guidance-1",)]
    assert [row.media[0]["name"] for row in params.tool_ir_history] == ["0.png", "1.png"]
    assert len(params.active_turn_user_inputs) == 2


# LLM: 使用真实 prompt_toolkit Enter、导入器和草稿状态；仅在投递边界替代远端，不声称商业供应商或位图剪贴板通过。
# 函数用途: 检查带图文本、纯图及多图先尝试活动投递，而不是提前创建普通 job。
@pytest.mark.parametrize("text,count,disk_failure", [("请看图片", 1, False), ("", 1, False), ("两张图片", 2, False), ("保留附件草稿", 1, True)])
def test_i1_tui_media_enters_active_delivery(tmp_path, monkeypatch, text, count, disk_failure):
    import asyncio

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from agent_py_agent.cli.chat_parts import tui_keybindings, tui_ui_setup
    from agent_py_agent.cli.chat_parts.tui_media import import_tui_media
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
    from agent_py_agent.tests.test_tui_prompt_toolkit_pipe import _app_params

    captured, keys = [], []
    original = tui_ui_setup._tui_create_keybindings
    monkeypatch.setattr(tui_ui_setup, "_tui_create_keybindings", lambda params: (keys.append(params), original(params))[1])
    if not disk_failure:
        monkeypatch.setattr(tui_keybindings, "_tui_submit_active_turn_input",
            lambda params, body, **options: (captured.append((body, options)), True)[1])
    sources = tuple(tmp_path / f"{index}.png" for index in range(count))
    for source in sources:
        source.write_bytes(b"png-example")

    # LLM: 管道只驱动当前测试应用，finally 关自己的 app；等断言的是业务状态而非按键调用成功。
    # 函数用途: 导入真实文件后按回车，核对媒体、草稿与队列状态。
    async def scenario():
        runtime = TuiRuntime("media-pipe")
        params = _app_params(tmp_path, runtime)
        params.agent.home_paths = SimpleNamespace(owner_home_dir=tmp_path)
        params.agent.config.input_media_max_bytes = 1024
        params.agent.config.input_media_max_files = 8
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            app = tui_ui_setup.make_tui_app(params)
            running = asyncio.create_task(app.run_async())
            try:
                await asyncio.sleep(0.05)
                await import_tui_media(SimpleNamespace(app=app), keys[0], sources)
                if disk_failure:
                    keys[0].use_gateway = True
                    params.agent.request_active_turn_input = lambda *args: None
                    monkeypatch.setattr(runtime, "execution_target", lambda: {"turn_id": "turn"})
                    keys[0].active_input_reconciler = SimpleNamespace(enqueue=lambda row: (_ for _ in ()).throw(OSError("disk busy")))
                pipe.send_text(text)
                pipe.send_bytes(b"\r")
                await asyncio.sleep(0.12)
                if disk_failure:
                    assert params.jobs.empty(), "outbox 保存失败不能静默另排普通任务"
                    assert text in app.current_buffer.text
                    assert keys[0].interaction_state.capture_draft(app.current_buffer.text, app.current_buffer.cursor_position).media_refs
                    return
                assert captured, "带图 Enter 绕过了活动投递"
                body, options = captured[0]
                assert body == (text or "请查看附件。")
                assert len(options["execution_options"].input_media) == count
                assert params.jobs.empty() and app.current_buffer.text == ""
            finally:
                app.exit(result=0)
                await running
    asyncio.run(scenario())
